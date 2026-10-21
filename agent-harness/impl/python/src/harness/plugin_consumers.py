"""Where the harness uses plugins (design §18.11): the campaign's proposer, the `retrieve` tool, and the evaluator check in outcome verification. Each goes through the host's handle, so every call is validated, budgeted, audited
and fails closed, and what a plugin returns is treated according to its kind's trust."""
from __future__ import annotations
from .plugins import PluginError, PluginHost, independence_level, may_gate
from .retrieval_acl import allowed_for
from .tools.builtin import _man
from .tools.registry import ToolRegistry
from .tools.runtime import ToolError, ToolResult


class PluginProposer:
    """The campaign's proposer, taken from a `proposer` plugin. It returns the first proposal, or None when the plugin has no more (which the campaign reads as exhausted)."""
    def __init__(self, host: PluginHost, run=None):
        self.host, self.run = host, run

    async def propose(self, r: int, n: int, base: str, ctx: dict) -> dict | None:
        res = await self.host.get("proposer").call("propose", {"round": r, "n": n, "base": base, "ctx": ctx}, run=self.run, budget=self.run is not None)
        return (res.value["proposals"] or [None])[0]


def register_retrieve_tool(registry: ToolRegistry, host: PluginHost) -> None:
    """The `retrieve` tool exists only while a retrieval plugin is loaded. Its results are untrusted observations, and the harness checks each passage's permissions again, because access control is enforced by the policy and not by
    trusting the plugin."""
    async def retrieve(a, t):
        user = t.run.owner
        try: res = await host.get("retrieval").call("query", {"user": user, "query": a["query"], "k": a.get("k", 5), "filters": a.get("filters") or {}}, run=t.run)
        except PluginError as e: raise ToolError(e.code, "tool", str(e), retryable=False) from e
        passages = [p for p in res.value["passages"] if allowed_for(p.get("permissions"), user)]
        await t.run.emit("retrieval.queried", {"plugin_id": res.plugin_id, "k": a.get("k", 5), "returned": len(passages), "withheld_by_harness": len(res.value["passages"]) - len(passages)})   # never the query text
        return ToolResult("\n".join(f"[{p['id']} score={p['score']:.3f} source={p['source']}] {p['text'][:500]}" for p in passages) or "(no results)", trust="untrusted")
    registry.register(_man("retrieve", "Search the knowledge base for passages relevant to a query.", {"type": "object", "properties": {"query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 20}, "filters": {"type": "object"}}, "required": ["query"], "additionalProperties": False},
                           "control_plane", "idempotent", "read_only"), retrieve)


STATUS = {"PASS": "pass", "FAIL": "fail", "PARTIAL": "fail", "UNCERTAIN": "unavailable"}      # a partly met criterion is not met, and an uncertain verdict is never a pass


async def run_evaluators(contract: dict, host: PluginHost, agent: dict, profile: str, subject: dict, evidence: dict, run=None) -> dict[str, dict]:
    """For each criterion verified by an evaluator plugin: the plugin's verdict, unless the evaluator is not independent enough to gate at this profile, in which case the criterion is unavailable and not a pass.
    agent: {model_ref, credential_ref, context}. Returns {criterion_id: {status, ref}} for `outcome.evaluate_contract`."""
    out = {}
    for x in contract["acceptance_criteria"]:
        if x["verification"] != "evaluator": continue
        p = x.get("params") or {}
        try: h = host.get("evaluator")
        except PluginError: out[x["id"]] = {"status": "unavailable", "ref": "no evaluator plugin is loaded"}; continue
        lvl = independence_level(h.manifest, agent); ok, why = may_gate(lvl["level"], profile, bool(p.get("material")), bool(p.get("backed")))
        if not ok: out[x["id"]] = {"status": "unavailable", "ref": f"evaluator {h.id} is {lvl['level']} (shares {', '.join(lvl['shared']) or 'nothing'}): {why}"}; continue
        crit = {"id": x["id"], **({"check": p["check"]} if "check" in p else {}), "params": {k: v for k, v in p.items() if k not in ("check", "material", "backed")}}      # `material` and `backed` are policy inputs for the harness, not for the judge
        try: res = await h.call("evaluate", {"criterion": crit, "subject": subject, "evidence": evidence}, run=run)
        except PluginError as e: out[x["id"]] = {"status": "unavailable", "ref": f"evaluator failed: {e.code}"}; continue
        v = res.value; out[x["id"]] = {"status": STATUS[v["verdict"]], "ref": f"evaluator:{h.id}:{lvl['level']}:{v['verdict']}:{','.join(v['reason_codes'])}"}
        if run is not None: await run.emit("evaluator.verdict", {"criterion_id": x["id"], "plugin_id": h.id, "type": lvl["type"], "independence": lvl["level"], "shared": lvl["shared"], "verdict": v["verdict"], "reason_codes": v["reason_codes"], "evidence_refs": v["evidence_refs"]})
    return out


class TierNotMet(RuntimeError):
    """The isolation plugin could not show that the sandbox it provisioned has the settings the tier requires, so no workload starts in it."""
    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems)); self.problems = problems


def unmet_settings(required: dict, observed: dict) -> list[str]:
    """What the tier requires that the sandbox does not show. A required value must be present and equal; `{"at_most": n}` and `{"at_least": n}` bound a number. A setting that is missing is unmet, never assumed."""
    out = []
    for k, want in sorted(required.items()):
        if k not in observed: out.append(f"{k} is required and was not reported"); continue
        got = observed[k]
        if isinstance(want, dict) and ("at_most" in want or "at_least" in want):
            if not isinstance(got, (int, float)) or isinstance(got, bool): out.append(f"{k} is {got!r}, which is not a number"); continue
            if "at_most" in want and got > want["at_most"]: out.append(f"{k} is {got}, above the limit of {want['at_most']}")
            if "at_least" in want and got < want["at_least"]: out.append(f"{k} is {got}, below the minimum of {want['at_least']}")
        elif got != want: out.append(f"{k} is {got!r} and the tier requires {want!r}")
    return out


async def provision_for_tier(host: PluginHost, spec: dict, required: dict, run=None) -> dict:
    """Provision a sandbox through the isolation plugin and start nothing in it until the plugin's `verify` shows every required setting (design §10.2.1). A sandbox that falls short is destroyed and the run is refused, with no
    fall back to a weaker tier. The exfiltration claim the settings support is recorded alongside."""
    from .plugins import exfiltration_claim
    h = host.get("isolation"); prov = (await h.call("provision", {"spec": spec}, run=run)).value; sid = prov["sandbox_id"]
    ver = (await h.call("verify", {"sandbox_id": sid, "required": required}, run=run)).value
    problems = ([] if ver["ok"] else ["the isolation plugin reports the sandbox is not ok"]) + unmet_settings(required, ver["observed"])
    if problems:
        await h.call("destroy", {"sandbox_id": sid}, run=run, budget=False)
        if run is not None: await run.emit("isolation.tier_unmet", {"plugin_id": h.id, "sandbox_id": sid, "problems": problems})
        raise TierNotMet(problems)
    claim = exfiltration_claim(ver["observed"])
    if run is not None: await run.emit("isolation.verified", {"plugin_id": h.id, "sandbox_id": sid, "required": sorted(required), "exfiltration_claim": claim["claim"], "exfiltration_reasons": claim["reasons"]})
    return {"sandbox_id": sid, "effective": prov["effective"], "observed": ver["observed"], "exfiltration_claim": claim}
