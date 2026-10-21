"""Plugins that satisfy each kind's contract, for tests. `make(manifest)` is the factory a manifest's entry points at."""
from harness import plugin_api as A
from harness.plugin_api import PluginRejection
from harness.plugins import KINDS

OUT = {
    "proposer": {"propose": {"proposals": [{"edits": [], "manifest": {}}]}, "analyze_weakness": {"findings": []}},
    "retrieval": {"index": {"indexed": 1, "index_version": "v1"}, "query": {"passages": [{"id": "d1", "text": "hello", "score": 0.9, "source": "s", "permissions": {}}]}, "forget": {"removed": 1}},
    "user_model": {"observe": {"recorded": True}, "predict": {"prediction": {}, "confidence": 0.5}, "forget": {"removed": 1}},
    "computer_use": {"open": {"session": "s1"}, "observe": {"observation": {}}, "act": {"observation": {}}, "close": {"closed": True}},
    "research": {"fork": {"fork_id": "f1"}, "step": {"state": {}}, "stop": {"stopped": True}},
    "isolation": {"provision": {"sandbox_id": "sb1", "effective": {}}, "verify": {"ok": True, "observed": {}}, "destroy": {"destroyed": True}},
    "assurance": {"attest": {"attestation": {}}, "broker_child": {"child_id": "c1"}},
    "evaluator": {"evaluate": {"verdict": "PASS", "reason_codes": [], "evidence_refs": []}}}


class Fake:
    """A conforming plugin. `over` replaces what a method returns (or a callable replaces the method), and `calls` records what was called."""
    def __init__(self, manifest: dict, over: dict | None = None):
        self.m, self.over, self.calls = manifest, over or {}, []
    def __getattr__(self, name):
        if name not in KINDS[self.m["kind"]]["methods"]: raise AttributeError(name)                                                                       # only the contract's methods exist
        def call(**kw):
            self.calls.append((name, kw))
            if name == "describe": r = {"id": self.m["id"], "kind": self.m["kind"], "version": self.m["version"]}
            elif name == "health": r = {"ok": True}
            else: r = OUT[self.m["kind"]][name]
            r = self.over.get(name, r)
            if callable(r): return r(**kw)
            if isinstance(r, Exception): raise r
            return r
        return call


def manifest(kind: str, id: str | None = None, version: str = "1.0.0", trust: str = "in_process", **over) -> dict:
    m = {"id": id or f"{kind}-fake", "kind": kind, "version": version, "interface_version": KINDS[kind]["interface_version"], "trust": trust, "grants": [],
         "entry": {"module": "plugin_fakes", "factory": "make"} if trust == "in_process" else {"command": ["python3", "plugin_proc.py"]}}
    if kind == "evaluator": m["evaluator"] = {"type": "code"}
    m.update(over); return m


def make(m: dict) -> Fake:
    return Fake(m)


def make_retriever(m: dict):
    """A keyword retriever with two documents, for the tests that load a plugin from a manifest."""
    from harness.reference_plugins import KeywordRetriever
    r = KeywordRetriever(m["id"], m["version"])
    r.index([{"id": "d1", "text": "invoice payment terms are net 30 days", "permissions": {"users": ["u"]}}, {"id": "d2", "text": "invoice approval needs two signatures", "permissions": {"users": ["someone-else"]}}, {"id": "d3", "text": "payment calendar for the year", "permissions": {"public": True}}])
    return r


class ProbeAware(Fake):
    """A plugin that follows its contract's error rules: it raises the declared error for each of the spec's probe inputs, and otherwise behaves like Fake."""
    def __getattr__(self, name):
        inner = super().__getattr__(name)
        errors = KINDS[self.m["kind"]]["methods"][name].get("errors", [])
        def call(**kw):
            for e in errors:
                if e.get("probe") == kw: self.calls.append((name, kw)); raise PluginRejection(e["code"], f"{name}: {e['code']}")
            return inner(**kw)
        return call


def make_probe_aware(m: dict) -> ProbeAware:
    return ProbeAware(m)


class HalfRetriever(A.Retriever):
    """A class that claims to be a retriever and leaves methods unimplemented, like a Java class missing interface methods."""
    def describe(self): return A.PluginInfo("half", "retrieval", "1.0.0")
    def health(self): return A.Health(True)
