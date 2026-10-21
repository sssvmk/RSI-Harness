"""In-process executor implementing the §10.4 method set against virtual roots (/workspace, /scratch, /tests).

Stand-in for the shared `harness-executor` binary: same method names/semantics, no stdio transport.
Identity switching needs root; when unavailable the call reports identity_enforced=false.
"""
from __future__ import annotations
import asyncio, fnmatch, os, posixpath, re, hashlib, signal
from pathlib import Path

MAX_OUTPUT = 256 * 1024
# Zero-credential sandboxing (H-4.2): processes started for the agent, the candidate or the judge get an allowlisted
# environment, never the daemon's. API keys, cloud credentials and tokens in the host environment must not be inherited.
SAFE_ENV_KEYS = ("PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TZ")


def sandbox_base_env(home: str) -> dict:
    env = {k: os.environ[k] for k in SAFE_ENV_KEYS if k in os.environ}
    env["HOME"] = home
    return env


class ExecError(Exception):
    def __init__(self, code: str, message: str, category: str = "tool", hint: str | None = None, alternatives: list | None = None, retryable: bool = False):
        super().__init__(message)
        self.code, self.category, self.hint, self.alternatives, self.retryable = code, category, hint, alternatives or [], retryable

    def to_dict(self) -> dict:
        return {"code": self.code, "category": self.category, "message": str(self), "retryable": self.retryable,
                "hint": self.hint, "alternatives": self.alternatives}


class LocalExecutor:
    def __init__(self, roots: dict[str, str], users: list[str] | None = None, limits: dict | None = None):
        self.limits = limits or {}                       # per-process CPU seconds and address-space megabytes (a safeguard while compute is unmetered, §7.4.4)
        self._live: set[int] = set()                     # process groups started here and not yet finished
        self.roots = {k: os.path.realpath(v) for k, v in roots.items()}
        self.users = users or ["agent", "candidate", "judge"]
        self.sessions: dict[str, dict] = {}
        self._execs: dict[str, asyncio.Task] = {}
        self._n = 0

    # ---- path handling (poka-yoke §9.6) ----
    def resolve(self, vpath: str, roots: list[str] | None = None) -> str:
        allowed = roots or list(self.roots)
        if not vpath.startswith("/"):
            raise ExecError("fs.path_not_absolute", f"path must be absolute: {vpath}", "tool",
                            "Use an absolute path under /workspace", [posixpath.join("/workspace", vpath)])
        norm = posixpath.normpath(vpath)
        for r in allowed:
            if norm == r or norm.startswith(r + "/"):
                real = os.path.realpath(self.roots[r] + norm[len(r):])
                if real != self.roots[r] and not real.startswith(self.roots[r] + os.sep):
                    raise ExecError("fs.path_escape", f"path escapes {r}: {vpath}", "security")
                return real
        raise ExecError("fs.path_not_allowed", f"path outside allowed roots {allowed}: {vpath}", "tool",
                        "Use an absolute path under /workspace or /scratch", [posixpath.join("/workspace", posixpath.basename(norm))])

    def _v(self, real: str) -> str:
        for k, v in self.roots.items():
            if real == v or real.startswith(v + os.sep):
                return k + real[len(v):]
        return real

    # ---- file methods ----
    def fs_read(self, path: str, start: int | None = None, end: int | None = None, roots=None) -> dict:
        p = self.resolve(path, roots)
        if not os.path.isfile(p):
            raise ExecError("fs.path_not_found", f"not a file: {path}", "tool", "Check the path", [self._v(str(x)) for x in Path(p).parent.glob("*")][:5] if Path(p).parent.exists() else [])
        data = Path(p).read_bytes()
        text = data.decode("utf-8", errors="replace")
        if start is not None or end is not None:
            text = "\n".join(text.split("\n")[(start or 1) - 1:end])
        return {"content": text, "bytes": len(data)}

    def fs_write(self, path: str, content: str, roots=None) -> dict:
        p = self.resolve(path, roots)
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(content, encoding="utf-8")
        return {"bytes": len(content.encode())}

    def fs_edit(self, path: str, old: str, new: str, roots=None) -> dict:
        p = self.resolve(path, roots)
        if not os.path.isfile(p):
            raise ExecError("fs.path_not_found", f"not a file: {path}")
        s = Path(p).read_text(encoding="utf-8")
        if old == "" or s.count(old) != 1:
            raise ExecError("fs.edit_no_unique_match", f"`old` must match exactly once (found {s.count(old) if old else 0})", "tool",
                            "Include more surrounding context in `old`")
        Path(p).write_text(s.replace(old, new, 1), encoding="utf-8")
        return {"replaced": 1}

    def fs_list(self, path: str, roots=None) -> dict:
        p = self.resolve(path, roots)
        if not os.path.isdir(p):
            raise ExecError("fs.path_not_found", f"not a directory: {path}")
        return {"entries": sorted(os.listdir(p))[:1000]}

    def fs_stat(self, path: str, roots=None) -> dict:
        p = self.resolve(path, roots)
        if not os.path.exists(p):
            raise ExecError("fs.path_not_found", f"no such path: {path}")
        st = os.stat(p)
        return {"size": st.st_size, "is_dir": os.path.isdir(p), "mode": st.st_mode & 0o777}

    def _walk(self, base: str):
        for d, dn, fn in os.walk(base):
            dn.sort()
            for f in sorted(fn):
                yield os.path.join(d, f)

    def fs_glob(self, pattern: str, roots=None) -> dict:
        root = pattern.split("*")[0].rsplit("/", 1)[0] or "/"
        base = self.resolve(root if root != "/" else "/workspace", roots)
        out = []
        for f in self._walk(base):
            v = self._v(f)
            if fnmatch.fnmatchcase(v, pattern):
                out.append(v)
        return {"paths": out[:1000]}

    def fs_grep(self, pattern: str, path: str = "/workspace", roots=None, max_results: int = 200) -> dict:
        base = self.resolve(path, roots)
        try:
            rx = re.compile(pattern)
        except re.error as e:
            raise ExecError("fs.bad_pattern", str(e))
        hits = []
        files = [base] if os.path.isfile(base) else self._walk(base)
        for f in files:
            try:
                for i, line in enumerate(Path(f).read_text(encoding="utf-8").split("\n"), 1):
                    if rx.search(line):
                        hits.append({"path": self._v(f), "line": i, "text": line[:300]})
                        if len(hits) >= max_results:
                            return {"matches": hits, "truncated": True}
            except (UnicodeDecodeError, OSError):
                continue
        return {"matches": hits, "truncated": False}

    # ---- workspace manifest (snapshots §10.7) ----
    def workspace_manifest(self, root: str = "/workspace", exclude: list[str] | None = None) -> dict:
        base = self.resolve(root)
        files = []
        for f in self._walk(base):
            rel = os.path.relpath(f, base)
            if any(fnmatch.fnmatch(rel, e) for e in (exclude or [])) or os.path.islink(f):
                continue
            data = Path(f).read_bytes()
            files.append({"path": rel, "size": len(data), "mode": os.stat(f).st_mode & 0o777, "sha256": hashlib.sha256(data).hexdigest()})
        return {"files": sorted(files, key=lambda x: x["path"])}

    def read_real(self, root: str, rel: str) -> bytes:
        return Path(self.resolve(root + "/" + rel)).read_bytes()

    def translate(self, argv: list[str]) -> list[str]:
        """Local backend has no mount namespaces: map virtual /workspace,/scratch,/tests prefixes to real paths."""
        out = []
        for a in argv:
            for k, v in self.roots.items():
                if a == k or a.startswith(k + "/"):
                    a = v + a[len(k):]
                    break
            out.append(a)
        return out

    def env_for_scripts(self) -> dict:
        return {"HARNESS_" + k.strip("/").upper(): v for k, v in self.roots.items()}

    # ---- process execution ----
    def _check_user(self, user: str | None) -> bool:
        if user is not None and user not in self.users:
            raise ExecError("exec.unknown_user", f"user {user!r} was not provisioned", "security")
        return os.geteuid() == 0 and user is not None  # identity switching only possible as root

    def _apply_limits(self):
        cpu, mem = self.limits.get("cpu_s"), self.limits.get("memory_mb")
        def f():
            import resource
            if cpu: resource.setrlimit(resource.RLIMIT_CPU, (int(cpu), int(cpu) + 1))        # SIGXCPU at the soft limit, SIGKILL one second later
            if mem: resource.setrlimit(resource.RLIMIT_AS, (int(mem) * 1024 * 1024, int(mem) * 1024 * 1024))
        return f if (cpu or mem) else None

    def kill_all(self) -> int:
        """Workflow-scoped cancellation: kill every process this sandbox still has running, so none is orphaned when the run ends."""
        n = 0
        for pg in list(self._live):
            try: os.killpg(pg, signal.SIGKILL); n += 1
            except (ProcessLookupError, PermissionError): pass
        self._live.clear(); return n

    async def _spawn(self, argv: list[str], cwd: str, env: dict, timeout_s: float, stdin: bytes | None = None) -> dict:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True, preexec_fn=self._apply_limits())
        self._live.add(proc.pid)
        timed_out = False
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout_s)
        except asyncio.TimeoutError:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = await proc.communicate()
        self._live.discard(proc.pid)
        return {"exit_code": proc.returncode if not timed_out else None, "timed_out": timed_out,
                "stdout": out[:MAX_OUTPUT].decode("utf-8", "replace"), "stderr": err[:MAX_OUTPUT].decode("utf-8", "replace"),
                "output_truncated": len(out) > MAX_OUTPUT or len(err) > MAX_OUTPUT}

    def exec_start(self, argv: list[str], cwd: str = "/workspace", env: dict | None = None, user: str | None = None,
                   timeout_s: float = 120, stdin: str | None = None) -> dict:
        enforced = self._check_user(user)
        self._n += 1
        eid = f"x{self._n}"
        e = {**sandbox_base_env(self.roots.get("/scratch", "/tmp")), **(env or {})}
        e.update(self.env_for_scripts())
        self._execs[eid] = asyncio.ensure_future(self._spawn(self.translate(argv), self.resolve(cwd), e, timeout_s, stdin.encode() if stdin else None))
        return {"exec_id": eid, "identity_enforced": enforced}

    async def exec_wait(self, exec_id: str) -> dict:
        return await self._execs.pop(exec_id)

    async def exec_run(self, argv, **kw) -> dict:
        r = self.exec_start(argv, **kw)
        out = await self.exec_wait(r["exec_id"])
        out["identity_enforced"] = r["identity_enforced"]
        return out

    # ---- persistent sessions (§10.5): cwd and exported env carried between calls ----
    def session_open(self, cwd: str = "/workspace", env: dict | None = None, user: str | None = None) -> dict:
        self._check_user(user)
        self._n += 1
        sid = f"s{self._n}"
        self.sessions[sid] = {"cwd": cwd, "env": dict(env or {}), "user": user}
        return {"session_id": sid}

    async def session_run(self, session_id: str, cmd: str, timeout_s: float = 120) -> dict:
        s = self.sessions.get(session_id)
        if s is None:
            raise ExecError("session.lost", "session no longer exists", "state", "Open a new session; shell state was lost")
        marker = "__HARNESS_STATE__"
        script = f'{cmd}\n__rc=$?\nprintf "\\n{marker}\\n%s\\n" "$PWD"\nenv -0 | base64 -w0\nexit $__rc'
        t0 = asyncio.get_event_loop().time()
        r = await self._spawn(["bash", "-c", script], self.resolve(s["cwd"]), {**sandbox_base_env(self.roots.get("/scratch", "/tmp")), **s["env"]}, timeout_s)
        if marker in r["stdout"]:
            body, _, tail = r["stdout"].rpartition("\n" + marker + "\n")
            pwd, _, envb = tail.partition("\n")
            r["stdout"] = body
            s["cwd"] = self._v(os.path.realpath(pwd)) if os.path.realpath(pwd).startswith(tuple(self.roots.values())) else s["cwd"]
            try:
                import base64
                for kv in base64.b64decode(envb).split(b"\0"):
                    k, _, v = kv.decode("utf-8", "replace").partition("=")
                    if not k or k in ("PWD", "OLDPWD", "_", "SHLVL"):
                        continue
                    if os.environ.get(k) != v:
                        s["env"][k] = v
            except Exception:
                pass
        r["cwd"] = s["cwd"]
        r["duration_ms"] = int((asyncio.get_event_loop().time() - t0) * 1000)
        return r

    def session_close(self, session_id: str) -> dict:
        self.sessions.pop(session_id, None)
        return {}
