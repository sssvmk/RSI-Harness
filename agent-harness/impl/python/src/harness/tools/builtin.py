"""Built-in tools (design §7.5, §9.6): control-plane tools and executor-backed sandbox tools."""
from __future__ import annotations
import json, os
from pathlib import Path
from ..canonical import sha256_hex
from ..gateway.canonical import message_text
from .registry import ToolRegistry
from .runtime import ToolContext, ToolError, ToolResult

S = lambda **props: {"type": "object", "properties": props, "required": [k for k, v in props.items() if not v.pop("optional", False)], "additionalProperties": False}
STR = lambda **kw: {"type": "string", **kw}
INT = lambda **kw: {"type": "integer", **kw}


def _man(name, desc, schema, loc, idem, eff, **kw):
    return {"name": name, "version": "1.0.0", "description": desc, "input_schema": schema, "location": loc,
            "idempotency": idem, "effect_class": eff, **kw}


# ---- sandbox tools ----
async def fs_read(a, t: ToolContext):
    r = t.sandbox.executor.fs_read(a["path"], a.get("start_line"), a.get("end_line"))
    return r["content"]

async def fs_write(a, t):
    r = t.sandbox.executor.fs_write(a["path"], a["content"])
    return f"wrote {r['bytes']} bytes to {a['path']}"

async def fs_edit(a, t):
    t.sandbox.executor.fs_edit(a["path"], a["old"], a["new"])
    return f"edited {a['path']}"

async def fs_search(a, t):
    r = t.sandbox.executor.fs_grep(a["pattern"], a.get("path", "/workspace"))
    return "\n".join(f"{m['path']}:{m['line']}: {m['text']}" for m in r["matches"]) or "(no matches)"

async def fs_glob(a, t):
    return "\n".join(t.sandbox.executor.fs_glob(a["pattern"])["paths"]) or "(no matches)"

async def shell_run(a, t):
    ex = t.sandbox.executor
    sid = t.state.get("session_id")
    if sid not in ex.sessions:  # first use, or session lost after recovery: reopen and replay recorded cwd
        sid = ex.session_open(cwd=t.state.get("cwd", "/workspace"), env=t.state.get("env"), user="agent")["session_id"]
        t.state["session_id"] = sid
    r = await ex.session_run(sid, a["cmd"], a.get("timeout_s", 120))
    t.state["cwd"] = r["cwd"]
    out = r["stdout"] + (("\n[stderr]\n" + r["stderr"]) if r["stderr"] else "")
    out += f"\n[timed out]" if r["timed_out"] else f"\n[exit code {r['exit_code']}]"
    return ToolResult(out, extra={"cwd": r["cwd"]})


# ---- control-plane tools ----
async def recall(a, t):
    hits = t.svc.store.search(t.run.run_id, a["query"], a.get("k", 5))
    return ToolResult("\n".join(f"[seq {h['seq']} {h['type']}] {h['text'][:500]}" for h in hits) or "(no results)", trust="untrusted")

async def history_get(a, t):
    store = t.svc.store
    if "tool_call_id" in a:
        ev = next((e for e in store.events(t.run.run_id, types=["tool.call.completed"]) if e["payload"]["tool_call_id"] == a["tool_call_id"]), None)
        if not ev:
            raise ToolError("history.not_found", "state", "no such tool call")
        ref = ev["payload"].get("result_ref")
        text = t.svc.blobs.get(ref).decode() if ref else message_text(ev["payload"]["message"])
        return ToolResult(text[a.get("offset", 0):], trust="untrusted")
    lo, hi = a["seq_from"], a.get("seq_to", a["seq_from"])
    if hi - lo > 50:
        raise ToolError("history.range_too_large", "tool", "request at most 50 events", hint="Narrow the range")
    out = []
    for e in store.events(t.run.run_id, after_seq=lo - 1, up_to_seq=hi):
        p = e["payload"]
        body = p.get("search_text") or (message_text(p["message"]) if "message" in p else json.dumps(p)[:500])
        out.append(f"[seq {e['seq']} {e['type']}] {body}")
    return ToolResult("\n".join(out) or "(empty range)", trust="untrusted")

STATE_DIR = "/workspace/.harness/state"

async def state_write(a, t):
    name = a["name"]
    if not all(c.isalnum() or c in "_.-" for c in name) or name.startswith("."):
        raise ToolError("state.bad_name", "tool", "name must be [A-Za-z0-9_.-]", hint="Use a simple name such as plan.md")
    path = f"{STATE_DIR}/{name}"

    async def write_file():
        t.sandbox.executor.fs_write(path, a["content"])
    res = await t.memory_guard.write(t.run, f"state/{name}", a["content"], {"source": "agent", "trust": "tool_trusted"}, write_file)
    if res["status"] == "written":
        await t.run.emit("state.written", {"name": name, "content_hash": res["content_hash"], "path": path, "search_text": f"{name}: {a['content'][:2000]}"})
    return f"state {name}: {res['status']}"

async def state_read(a, t):
    return ToolResult(t.sandbox.executor.fs_read(f"{STATE_DIR}/{a['name']}")["content"], trust="untrusted")

async def skill_load_untrusted(a, t):
    """Skills the agent wrote (workspace) or a third party supplied are untrusted boundaries: the result is wrapped as untrusted and the call needs approval (PRD §4.3 Trust Escalation Prevention)."""
    name, source = a["name"], a.get("source")
    if not isinstance(name, str) or "/" in name or ".." in name or name.startswith("."):
        raise ToolError("skill.bad_name", "security", "invalid skill name")
    if source == "workspace":
        try:
            text = t.sandbox.executor.fs_read(f"/workspace/skills/{name}/SKILL.md")["content"]
        except Exception:
            raise ToolError("skill.not_found", "tool", f"no workspace skill {name!r}")
    elif source == "third_party":
        d = ((t.run.svc.config or {}).get("skills") or {}).get("third_party_dir")
        if not d:
            raise ToolError("skill.unavailable", "environment", "no third-party skills directory is configured")
        p = Path(d) / name / "SKILL.md"
        if not p.is_file():
            raise ToolError("skill.not_found", "tool", f"no third-party skill {name!r}")
        text = p.read_text()
    else:
        raise ToolError("skill.bad_source", "tool", "source must be 'workspace' or 'third_party'")
    return ToolResult(text, trust="untrusted")


async def skill_load(a, t):
    if not t.config_dir:
        raise ToolError("skill.unavailable", "environment", "no config repo configured")
    name = a["name"]
    if "/" in name or ".." in name:
        raise ToolError("skill.bad_name", "security", "invalid skill name")
    pins = t.run.pins if hasattr(t.run, "pins") else None
    if pins is not None and pins.active:                                  # a pinned skill is read from the manifest, never from whatever is on disk now
        from ..definitions import DefinitionMismatch
        try: return ToolResult(pins.resolve(f"skill.{name}"), trust="tool_trusted")
        except DefinitionMismatch as e:
            if e.expected is None and not (Path(t.config_dir) / "skills" / name / "SKILL.md").is_file():
                raise ToolError("skill.not_found", "tool", f"no skill {name!r}")                 # it does not exist at all
            raise ToolError("definition.mismatch", "security", f"skill {name!r} is not available as pinned: {e.reason}")      # it exists now but is not what the run began with
    p = Path(t.config_dir) / "skills" / name / "SKILL.md"
    if not p.is_file():
        raise ToolError("skill.not_found", "tool", f"no skill {name!r}")
    return ToolResult(p.read_text(), trust="tool_trusted")

async def tool_search(a, t):
    found = t.registry.search(a["query"], t.agent["tool_allowlist"])
    names = [m["name"] for m in found]
    if names:
        await t.run.emit("tools.exposed", {"names": names})
    return "\n".join(f"{m['name']}: {m['description']}" for m in found) or "(no matching tools)"

async def submit(a, t):
    if "submit" not in t.hooks:
        raise ToolError("submit.unavailable", "tool", "submit is only available in evaluation runs")
    return await t.hooks["submit"](a.get("note"))

async def submit_reconcile(a, t):
    """After a crash the submit call is re-scored from its snapshot (§5.9) and the feedback delivered as its result."""
    fn = t.hooks.get("submit_resume")
    return await fn() if fn else None


async def finish(a, t):
    await t.hooks["finish"](a["summary"])
    return "completion proposed; verification will run"


def register_builtins(reg: ToolRegistry) -> None:
    P = {"path": STR()}
    reg.register(_man("fs.read", "Read a file (absolute path under /workspace or /scratch).", S(path=STR(), start_line=INT(optional=True), end_line=INT(optional=True)), "sandbox", "idempotent", "read_only"), fs_read)
    reg.register(_man("fs.write", "Create or overwrite a file.", S(path=STR(), content=STR()), "sandbox", "idempotent", "reversible_write", world={"adapter": "workspace_file", "resource_arg": "path", "preconditions": [], "expected_effects": ["content_equals"]}), fs_write)
    reg.register(_man("fs.edit", "Replace one exact, unique occurrence of `old` with `new`.", S(path=STR(), old=STR(), new=STR()), "sandbox", "keyed", "reversible_write"), fs_edit)
    reg.register(_man("fs.search", "Regex search over files.", S(pattern=STR(), path=STR(optional=True)), "sandbox", "idempotent", "read_only"), fs_search)
    reg.register(_man("fs.glob", "Glob files by absolute pattern.", S(pattern=STR()), "sandbox", "idempotent", "read_only"), fs_glob)
    reg.register(_man("shell.run", "Run a shell command in a persistent session.", S(cmd=STR(), timeout_s=INT(minimum=1, optional=True)), "sandbox", "non_idempotent", "irreversible", timeout_s=600), shell_run)
    reg.register(_man("recall", "Search this run's history.", S(query=STR(), k=INT(minimum=1, maximum=20, optional=True)), "control_plane", "idempotent", "read_only"), recall)
    reg.register(_man("history.get", "Retrieve original content from the log by seq range or tool_call_id.",
                      {"type": "object", "properties": {"seq_from": INT(minimum=1), "seq_to": INT(minimum=1), "tool_call_id": STR(), "offset": INT(minimum=0)}, "additionalProperties": False,
                       "anyOf": [{"required": ["seq_from"]}, {"required": ["tool_call_id"]}]}, "control_plane", "idempotent", "read_only"), history_get)
    reg.register(_man("state.write", "Write a named working-state artifact (plan, todo, notes).", S(name=STR(), content=STR()), "control_plane", "idempotent", "reversible_write"), state_write)
    reg.register(_man("state.read", "Read a named working-state artifact.", S(name=STR()), "control_plane", "idempotent", "read_only"), state_read)
    reg.register(_man("skill.load", "Load a skill's full instructions.", S(name=STR()), "control_plane", "idempotent", "read_only"), skill_load)
    reg.register(_man("skill.load_untrusted", "Load a skill the agent wrote or a third party supplied. Untrusted; requires approval.", S(name=STR(), source=STR()), "control_plane", "idempotent", "external_facing"), skill_load_untrusted)
    reg.register(_man("tool.search", "Find tools beyond the always-loaded set.", S(query=STR()), "control_plane", "idempotent", "read_only"), tool_search)
    reg.register(_man("submit", "Submit the current workspace for scoring.", S(note=STR(optional=True)), "control_plane", "non_idempotent", "reversible_write"), submit, submit_reconcile)
    reg.register(_man("finish", "Propose completion of the task.", S(summary=STR()), "control_plane", "idempotent", "reversible_write"), finish)

ALWAYS_LOADED = ["fs.*", "shell.run", "recall", "history.get", "state.*", "skill.load", "tool.search", "submit", "finish"]
