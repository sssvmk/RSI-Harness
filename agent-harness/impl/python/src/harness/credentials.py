"""Credentials and secrets (design §12): SecretProvider, Redactor, TokenSource with leases."""
from __future__ import annotations
import os, re
from pathlib import Path
from .clock import Clock


class Secret:
    def __init__(self, value: str):
        self._v = value

    def reveal(self) -> str:
        return self._v

    def __repr__(self) -> str:  # never leak through logs
        return "Secret(***)"


class SecretProvider:
    def get(self, name: str) -> Secret:
        raise NotImplementedError


class EnvSecretProvider(SecretProvider):
    def get(self, name: str) -> Secret:
        v = os.environ.get(name.upper().replace("/", "_"))
        if v is None:
            raise KeyError(f"secret {name} unavailable")
        return Secret(v)


class FileSecretProvider(SecretProvider):
    def __init__(self, root: str):
        self.root = Path(root)

    def get(self, name: str) -> Secret:
        return Secret((self.root / name).read_text().strip())


class DictSecretProvider(SecretProvider):
    def __init__(self, d: dict[str, str]):
        self.d = d

    def get(self, name: str) -> Secret:
        if name not in self.d:
            raise KeyError(f"secret {name} unavailable")
        return Secret(self.d[name])


SECRET_SHAPES = [re.compile(p) for p in (r"sk-[A-Za-z0-9_\-]{16,}", r"AKIA[0-9A-Z]{16}", r"(?i)bearer\s+[A-Za-z0-9._\-]{16,}",
                                         r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")]


class Redactor:
    """Defense in depth (§12.5): known values and secret-shaped strings."""

    def __init__(self, known: list[str] | None = None):
        self.known = [k for k in (known or []) if k]

    def add(self, value: str) -> None:
        if value:
            self.known.append(value)

    def redact(self, text: str) -> str:
        for k in sorted(self.known, key=len, reverse=True):
            text = text.replace(k, "[REDACTED]")
        for rx in SECRET_SHAPES:
            text = rx.sub("[REDACTED]", text)
        return text

    def redact_obj(self, o):
        if isinstance(o, str):
            return self.redact(o)
        if isinstance(o, dict):
            return {k: self.redact_obj(v) for k, v in o.items()}
        if isinstance(o, list):
            return [self.redact_obj(v) for v in o]
        return o


class CredentialUnavailable(Exception):
    code = "gateway.credential_unavailable"


class TokenSource:
    """Refreshes proactively at 50% lifetime; never revokes a token that has live leases (§12.3)."""

    def __init__(self, fetch, clock: Clock | None = None):
        self.fetch, self.clock = fetch, clock or Clock()  # fetch() -> (token, lifetime_s)
        self.cur = None  # [token, issued_ms, lifetime_ms, leases]
        self.old: list[list] = []
        self.failed = False

    def _refresh(self) -> None:
        try:
            tok, life = self.fetch()
        except Exception:
            self.failed = True
            return
        self.failed = False
        if self.cur:
            self.old.append(self.cur)
        self.cur = [tok, self.clock.now_ms(), life * 1000, 0]

    def lease(self):
        now = self.clock.now_ms()
        if self.cur is None or now - self.cur[1] >= self.cur[2] * 0.5:
            self._refresh()
        if self.cur is None or self.failed and now - self.cur[1] >= self.cur[2]:
            raise CredentialUnavailable("token refresh failed")  # fail fast instead of stalling
        self.cur[3] += 1
        return self.cur

    def release(self, lease) -> None:
        lease[3] -= 1

    def revocable(self) -> list[str]:
        """Superseded tokens with no live leases; handed out once so the caller can revoke them."""
        free = [o for o in self.old if o[3] <= 0]
        self.old = [o for o in self.old if o[3] > 0]
        return [o[0] for o in free]
