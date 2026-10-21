"""Model gateway: retries (§8.3), middleware (§8.4), usage accounting, transports."""
from __future__ import annotations
import asyncio, json, random
from typing import Any, Awaitable, Callable, Protocol
from ..clock import Clock
from .adapters import ADAPTERS
from .canonical import validate_request

TRANSIENT_STATUS = {408, 429, 500, 502, 503, 504}


class GatewayError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.code, self.retryable, self.status = code, retryable, status


class Block(Exception):
    """Raised by a middleware hook to block the call."""


class Transport(Protocol):
    async def post(self, url: str, headers: dict, body: dict, timeout_s: float) -> tuple[int, dict]: ...


class HttpTransport:
    async def post(self, url, headers, body, timeout_s):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as c:
                r = await c.post(url, headers=headers, json=body)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as e:
            raise ConnectionResetError(str(e)) from e
        try:
            return r.status_code, r.json()
        except json.JSONDecodeError:
            return r.status_code, {"error": r.text[:500]}


class ScriptedTransport:
    """Plays back (status, body) tuples or exceptions; records every request. Used by tests and the demo."""

    def __init__(self, script: list):
        self.script, self.requests = list(script), []

    async def post(self, url, headers, body, timeout_s):
        self.requests.append({"url": url, "headers": headers, "body": body})
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class Middleware:
    name = "middleware"
    optional = True

    async def pre_request(self, req: dict) -> dict:
        return req

    async def post_response(self, resp: dict) -> dict:
        return resp


class ModelGateway:
    def __init__(self, models: dict[str, dict], transport: Transport, clock: Clock | None = None, secrets=None,
                 middleware: list[Middleware] | None = None, max_attempts: int = 4, timeout_s: float = 120.0,
                 rng: random.Random | None = None, id_factory=None):
        self.models, self.transport, self.clock = models, transport, clock or Clock()
        self.secrets, self.middleware = secrets, middleware or []
        self.max_attempts, self.timeout_s = max_attempts, timeout_s
        self.rng = rng or random.Random()
        self.id_factory = id_factory

    def _headers(self, m: dict) -> dict:
        try:
            key = self.secrets.get(m["secret"]).reveal() if (self.secrets and m.get("secret")) else ""
        except KeyError as e:
            raise GatewayError("gateway.credential_unavailable", f"secret {m['secret']} unavailable") from e
        if m["provider"] == "anthropic":
            return {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        if m["provider"] == "gemini":
            return {"x-goog-api-key": key, "content-type": "application/json"}
        return {"authorization": f"Bearer {key}", "content-type": "application/json"}

    @staticmethod
    def _url(m: dict) -> str:
        if m.get("endpoint"):
            return m["endpoint"]
        return {"openai": "https://api.openai.com/v1/chat/completions",
                "anthropic": "https://api.anthropic.com/v1/messages",
                "gemini": f"https://generativelanguage.googleapis.com/v1beta/models/{m['model_id']}:generateContent"}[m["provider"]]

    @staticmethod
    def backoff_ms(attempt: int, rng: random.Random, base_ms: float = 1000, cap_ms: float = 30000) -> float:
        return rng.uniform(0, min(cap_ms, base_ms * 2 ** (attempt - 1)))  # full jitter

    async def call(self, req: dict, id_map: dict | None = None, on_failure: Callable[[dict], Awaitable[None]] | None = None,
                   remaining_time_ms: float | None = None) -> dict:
        validate_request(req)
        m = self.models[req["model_ref"]]
        trace = []
        for h in self.middleware:
            try:
                req = await h.pre_request(req)
                trace.append({"hook": h.name, "phase": "pre", "ok": True})
            except Block as e:
                raise GatewayError("gateway.blocked", f"{h.name}: {e}")
            except Exception as e:
                if not h.optional:
                    raise GatewayError("gateway.hook_failed", f"{h.name}: {e}")  # safety hooks fail closed
                trace.append({"hook": h.name, "phase": "pre", "ok": False, "error": str(e)})
        adapter = ADAPTERS[m["provider"]](self.id_factory) if self.id_factory else ADAPTERS[m["provider"]]()
        wire = adapter.to_wire(req, m["model_id"], id_map)
        timeout = self.timeout_s if remaining_time_ms is None else max(0.001, min(self.timeout_s, remaining_time_ms / 1000))
        for attempt in range(1, self.max_attempts + 1):
            t0 = self.clock.mono_ms()
            err: GatewayError | None = None
            try:
                status, body = await asyncio.wait_for(self.transport.post(self._url(m), self._headers(m), wire, timeout), timeout)
                if status == 200:
                    resp = adapter.from_wire(body)
                    resp["latency_ms"] = int(self.clock.mono_ms() - t0)
                    resp["attempt"] = attempt
                    resp["provider_meta"]["hooks"] = trace
                    if getattr(adapter, "last_downgrades", None):
                        resp["provider_meta"]["schema_downgrades"] = adapter.last_downgrades
                    for h in self.middleware:
                        try:
                            resp = await h.post_response(resp)
                        except Block as e:
                            raise GatewayError("gateway.blocked", f"{h.name}: {e}")
                        except Exception as e:
                            if not h.optional:
                                raise GatewayError("gateway.hook_failed", f"{h.name}: {e}")
                    return resp
                err = GatewayError(f"gateway.http_{status}", json.dumps(body)[:300], status in TRANSIENT_STATUS, status)
            except (ConnectionError, asyncio.TimeoutError) as e:
                err = GatewayError("gateway.connection", repr(e), True)
            if not err.retryable or attempt == self.max_attempts:
                if on_failure:
                    await on_failure({"error": {"code": err.code, "message": str(err)}, "attempt": attempt, "retry_in_ms": None})
                raise err
            delay = self.backoff_ms(attempt, self.rng)
            if on_failure:
                await on_failure({"error": {"code": err.code, "message": str(err)}, "attempt": attempt, "retry_in_ms": int(delay)})
            await self.clock.sleep(delay)
        raise AssertionError("unreachable")
