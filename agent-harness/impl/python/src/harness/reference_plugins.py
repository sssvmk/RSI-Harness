"""Reference implementation classes for the plugin interfaces (design §18.11). Each subclasses its interface from `plugin_api` and is loaded through the host like any other plugin. They need no model server or host, so they work out
of the box: a deterministic code evaluator, an LLM judge and a typed-AI evaluator that take a `complete(prompt)` function (wire it to the harness's model gateway), a keyword retriever with per-passage access control, a scripted
proposer, and a counting user model. A deployment replaces them with real ones (an embedding index, a vision model, a container host) by implementing the same interface."""
from __future__ import annotations
import hashlib, inspect, json, math, re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

import jsonschema

from .plugin_api import (Evaluator, Findings, Health, IndexResult, PluginInfo, Passage, PredictionResult, Proposal, ProposalSet, Proposer, QueryResult, Recorded, Removed, Retriever, UserModel, Verdict)

VERDICTS = ("PASS", "FAIL", "PARTIAL", "UNCERTAIN")


def _path(obj: Any, path: str) -> tuple[bool, Any]:
    """A dotted path into nested dictionaries: (found, value)."""
    for part in path.split("."):
        if not isinstance(obj, dict) or part not in obj: return False, None
        obj = obj[part]
    return True, obj


# ------------------------------------------------------------------------------------------------ evaluators
def _check_equals(p, subject, evidence):
    ok, v = _path(subject, p["path"]); return (None, []) if not ok else (v == p["value"], [f"subject:{p['path']}"])


def _check_exact_decimal(p, subject, evidence):
    ok, v = _path(subject, p["path"])
    if not ok: return None, []
    try: return Decimal(str(v)) == Decimal(str(p["value"])), [f"subject:{p['path']}"]         # money is compared exactly, never as a float
    except InvalidOperation: return False, [f"subject:{p['path']}"]


def _check_contains(p, subject, evidence):
    ok, v = _path(subject, p["path"]); return (None, []) if not ok else (str(p["text"]) in str(v), [f"subject:{p['path']}"])


def _check_exists(p, subject, evidence):
    ok, _ = _path(subject, p["path"]); return ok, [f"subject:{p['path']}"]


class CodeEvaluator(Evaluator):
    """A deterministic evaluator: the criterion names a check (`equals`, `exact_decimal`, `contains`, `exists`, or one you add), and the answer follows from the subject alone. Missing evidence is UNCERTAIN, never a pass."""
    def __init__(self, id: str = "code-evaluator", version: str = "1.0.0", checks: dict[str, Callable] | None = None):
        self.id, self.version = id, version
        self.checks = {"equals": _check_equals, "exact_decimal": _check_exact_decimal, "contains": _check_contains, "exists": _check_exists, **(checks or {})}

    def describe(self) -> PluginInfo: return PluginInfo(self.id, "evaluator", self.version)
    def health(self) -> Health: return Health(True)

    def evaluate(self, criterion: dict, subject: dict, evidence: dict) -> Verdict:
        fn = self.checks.get(criterion.get("check"))
        if fn is None: return Verdict("UNCERTAIN", ["no_such_check"], [])
        try: ok, refs = fn(criterion.get("params") or {}, subject, evidence)
        except Exception: return Verdict("UNCERTAIN", ["check_error"], [])                     # a check that fails proves nothing
        if ok is None: return Verdict("UNCERTAIN", ["evidence_missing"], [])
        return Verdict("PASS" if ok else "FAIL", ["check_passed" if ok else "check_failed"], refs)


async def _complete(complete: Callable, prompt: str) -> str:
    r = complete(prompt); return await r if inspect.isawaitable(r) else r


def _prompt(rubric: str, criterion: dict, subject: dict, evidence: dict, tail: str) -> str:
    return f"{rubric}\n\nCRITERION:\n{json.dumps(criterion, sort_keys=True)}\n\nSUBJECT:\n{json.dumps(subject, sort_keys=True)}\n\nEVIDENCE (cite only these keys):\n{json.dumps(sorted(evidence))}\n\n{tail}"


class LlmJudgeEvaluator(Evaluator):
    """A language model scores the criterion against a rubric and answers in free text; the first `VERDICT:` line is the answer. Anything it cannot parse is UNCERTAIN. `complete(prompt) -> text` is the model call."""
    def __init__(self, complete: Callable, id: str = "llm-judge", version: str = "1.0.0", rubric: str = "You are a strict evaluator. Judge only from the evidence given."):
        self.complete, self.id, self.version, self.rubric = complete, id, version, rubric

    def describe(self) -> PluginInfo: return PluginInfo(self.id, "evaluator", self.version)
    def health(self) -> Health: return Health(True)

    async def evaluate(self, criterion: dict, subject: dict, evidence: dict) -> Verdict:
        text = await _complete(self.complete, _prompt(self.rubric, criterion, subject, evidence, "Answer on the first line as `VERDICT: PASS|FAIL|PARTIAL|UNCERTAIN`, then `REASONS: code1,code2`."))
        m = re.search(r"^\s*VERDICT:\s*(PASS|FAIL|PARTIAL|UNCERTAIN)\b", text or "", re.M | re.I)
        if not m: return Verdict("UNCERTAIN", ["unparseable_judgement"], [])
        r = re.search(r"^\s*REASONS:\s*(.+)$", text, re.M | re.I)
        return Verdict(m.group(1).upper(), [x.strip() for x in r.group(1).split(",") if x.strip()] if r else ["judged"], [])


VERDICT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["verdict", "reason_codes", "evidence_refs"], "properties": {"verdict": {"enum": list(VERDICTS)}, "reason_codes": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "evidence_refs": {"type": "array", "items": {"type": "string"}}, "confidence": {"type": "number", "minimum": 0, "maximum": 1}}}


class TypedAiEvaluator(Evaluator):
    """A model whose output must be a typed verdict. The reply is parsed as JSON and validated against the verdict schema, every evidence reference must name evidence that was actually supplied, and an invalid reply is retried
    and then becomes UNCERTAIN. It never guesses a verdict from malformed output."""
    def __init__(self, complete: Callable, id: str = "typed-ai", version: str = "1.0.0", rubric: str = "You are a strict evaluator. Judge only from the evidence given.", retries: int = 1):
        self.complete, self.id, self.version, self.rubric, self.retries = complete, id, version, rubric, retries

    def describe(self) -> PluginInfo: return PluginInfo(self.id, "evaluator", self.version)
    def health(self) -> Health: return Health(True)

    async def evaluate(self, criterion: dict, subject: dict, evidence: dict) -> Verdict:
        tail, why = "Reply with only a JSON object: " + json.dumps(VERDICT_SCHEMA["properties"]), ""
        for _ in range(self.retries + 1):
            text = await _complete(self.complete, _prompt(self.rubric, criterion, subject, evidence, tail + why))
            try:
                doc = json.loads(re.search(r"\{.*\}", text or "", re.S).group(0))
                bad = [e.message for e in jsonschema.Draft202012Validator(VERDICT_SCHEMA).iter_errors(doc)] or [f"evidence_ref {r!r} was not supplied" for r in doc["evidence_refs"] if r not in evidence]
            except (AttributeError, ValueError, KeyError): bad = ["the reply is not a JSON object"]
            if not bad: return Verdict(doc["verdict"], doc["reason_codes"], doc["evidence_refs"], doc.get("confidence"))
            why = f"\nYour previous reply was invalid: {bad[0]}. Reply again."
        return Verdict("UNCERTAIN", ["invalid_typed_output"], [])


# ------------------------------------------------------------------------------------------------ retrieval
_TOKEN = re.compile(r"[a-z0-9]+")


class KeywordRetriever(Retriever):
    """An in-memory index scored by TF-IDF (raw term frequency times inverse document frequency), with access control enforced at query time: a document is returned only if its permissions list the user or mark it public. A document with no permissions is returned to nobody."""
    def __init__(self, id: str = "keyword-retriever", version: str = "1.0.0"):
        self.id, self.version, self.docs, self._df = id, version, {}, Counter()

    def describe(self) -> PluginInfo: return PluginInfo(self.id, "retrieval", self.version)
    def health(self) -> Health: return Health(True)

    @staticmethod
    def _tok(text: str) -> list[str]: return _TOKEN.findall(text.lower())

    def _rebuild(self) -> None:
        self._df = Counter(t for d in self.docs.values() for t in set(d["tf"]))

    def index(self, documents: list) -> IndexResult:
        for d in documents:
            toks = self._tok(d["text"]); self.docs[d["id"]] = {"id": d["id"], "text": d["text"], "meta": d.get("metadata") or {}, "perm": d.get("permissions") or {}, "tf": Counter(toks), "n": len(toks) or 1}
        self._rebuild(); h = hashlib.sha256(json.dumps(sorted((i, hashlib.sha256(d["text"].encode()).hexdigest()) for i, d in self.docs.items())).encode()).hexdigest()[:12]
        return IndexResult(len(documents), h)

    @staticmethod
    def allowed(perm: dict, user: str) -> bool:
        return bool(perm) and (perm.get("public") is True or user in (perm.get("users") or []))

    def query(self, user: str, query: str, k: int, filters: dict) -> QueryResult:
        n, q = len(self.docs), self._tok(query); scored = []
        for d in self.docs.values():
            if not self.allowed(d["perm"], user) or any(d["meta"].get(f) != v for f, v in (filters or {}).items()): continue
            s = sum(d["tf"][t] * (math.log((1 + n) / (1 + self._df[t])) + 1) for t in q if t in d["tf"])    # raw term frequency: a document that matches more of the query ranks above a shorter one that matches less
            if s > 0: scored.append((-s, d["id"], d))
        return QueryResult([Passage(d["id"], d["text"], round(-s, 6), d["meta"].get("source", d["id"]), d["perm"]) for s, _, d in sorted(scored, key=lambda x: (x[0], x[1]))[:max(0, k)]])

    def forget(self, ids: list) -> Removed:
        gone = [i for i in ids if self.docs.pop(i, None) is not None]; self._rebuild(); return Removed(len(gone))


# ------------------------------------------------------------------------------------------------ proposer
class ScriptedProposer(Proposer):
    """Proposes the edits it was given, in order, and then nothing. It is the stand-in for a model while a campaign is tested, and a way to replay known edits."""
    def __init__(self, proposals: list[dict], findings: list | None = None, id: str = "scripted-proposer", version: str = "1.0.0"):
        self.id, self.version, self._queue, self._findings = id, version, list(proposals), list(findings or [])

    def describe(self) -> PluginInfo: return PluginInfo(self.id, "proposer", self.version)
    def health(self) -> Health: return Health(True)
    def propose(self, round: int, n: int, base: str, ctx: dict) -> ProposalSet:
        return ProposalSet([Proposal(p.get("edits", []), p.get("manifest", {}))] if self._queue and (p := self._queue.pop(0)) is not None else [])
    def analyze_weakness(self, report: dict) -> Findings: return Findings(list(self._findings))


# ------------------------------------------------------------------------------------------------ user model
class InMemoryUserModel(UserModel):
    """Predicts a user's most frequent next action from the actions it has observed. The confidence is that action's share of what it has seen, and `forget` removes everything about a user."""
    def __init__(self, id: str = "counting-user-model", version: str = "1.0.0"):
        self.id, self.version, self.counts = id, version, defaultdict(Counter)

    def describe(self) -> PluginInfo: return PluginInfo(self.id, "user_model", self.version)
    def health(self) -> Health: return Health(True)

    def observe(self, user: str, event: dict) -> Recorded:
        a = event.get("action")
        if not isinstance(a, str) or not a: return Recorded(False)
        self.counts[user][a] += 1; return Recorded(True)

    def predict(self, user: str, context: dict) -> PredictionResult:
        c = self.counts.get(user)
        if not c: return PredictionResult({}, 0.0)
        action, n = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0]; return PredictionResult({"action": action}, round(n / sum(c.values()), 6))

    def forget(self, user: str) -> Removed:
        return Removed(sum(self.counts.pop(user, Counter()).values()))
