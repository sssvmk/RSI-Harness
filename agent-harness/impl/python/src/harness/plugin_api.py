"""The plugin interfaces (design §18.11): one abstract class per kind of plugin, like a Java interface. A deployment names an implementation class, the harness loads it, and it is called only through these methods. A class
that leaves a method unimplemented cannot be created. What each method returns and the errors it may raise are the contract in `spec/plugins/kinds.json` (rendered in docs/plugin_contract.md); the docstrings summarise it.

Methods may be plain or `async`, and they return the result classes below, which `to_plain` turns into the JSON the contract validates. A plugin signals an *expected* failure by raising `PluginRejection(code)` with a code
that the contract declares for that method. Any other exception is a plugin failure, which the harness reports as `PluginFailed`; an undeclared code is reported as `ContractViolation`.

NOTE: this file was rebuilt on 2026-10-21 after an accidental overwrite, from the code and tests that depend on it (`reference_plugins.py`, `tests/test_reference_plugins.py`, `plugins.py`); see CHANGES.md."""
from __future__ import annotations
import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

ERROR_CODES = ("invalid_argument", "not_found", "permission_denied", "conflict", "unsupported", "unavailable", "rate_limited")
RETRYABLE = frozenset({"unavailable", "rate_limited"})


class PluginRejection(Exception):
    """Raised by a plugin for a failure that the contract declares for the method (an invalid argument, a missing session, ...). `code` is one of ERROR_CODES."""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code); self.code, self.message = code, message or code


# ---------------------------------------------------------------------------------------------- result classes
@dataclass
class PluginInfo: id: str; kind: str; version: str
@dataclass
class Health: ok: bool
@dataclass
class IndexResult: indexed: int; index_version: str
@dataclass
class Passage: id: str; text: str; score: float; source: str; permissions: dict = field(default_factory=dict)
@dataclass
class QueryResult: passages: list
@dataclass
class Removed: removed: int
@dataclass
class Recorded: recorded: bool
@dataclass
class PredictionResult: prediction: dict; confidence: float
@dataclass
class Opened: session: str
@dataclass
class Observed: observation: dict
@dataclass
class Closed: closed: bool
@dataclass
class Forked: fork_id: str
@dataclass
class Stepped: state: dict
@dataclass
class Stopped: stopped: bool
@dataclass
class Provisioned: sandbox_id: str; effective: dict
@dataclass
class Verification: ok: bool; observed: dict
@dataclass
class Destroyed: destroyed: bool
@dataclass
class Attested: attestation: dict
@dataclass
class Child: child_id: str
@dataclass
class Verdict: verdict: str; reason_codes: list; evidence_refs: list; confidence: float | None = None
@dataclass
class Proposal: edits: list; manifest: dict
@dataclass
class ProposalSet: proposals: list
@dataclass
class Findings: findings: list


def to_plain(x: Any) -> Any:
    """A result class (or a list or dict holding some) as plain JSON. A field that is None is left out; empty lists and objects are kept."""
    if dataclasses.is_dataclass(x) and not isinstance(x, type): return {f.name: to_plain(getattr(x, f.name)) for f in dataclasses.fields(x) if getattr(x, f.name) is not None}
    if isinstance(x, list): return [to_plain(i) for i in x]
    if isinstance(x, dict): return {k: to_plain(v) for k, v in x.items()}
    return x


# ---------------------------------------------------------------------------------------------- interfaces
class Plugin(ABC):
    """What every plugin implements. `describe` says who it is (its id, kind and version must equal the manifest's) and never raises; `health` reports ok=false when the plugin cannot serve calls and never raises."""
    KIND: str = ""

    @abstractmethod
    def describe(self) -> PluginInfo: ...

    @abstractmethod
    def health(self) -> Health: ...


class Retriever(Plugin):
    KIND = "retrieval"

    @abstractmethod
    def index(self, documents: list) -> IndexResult:
        """Add or replace documents ({id, text, ...}). Raises invalid_argument (an empty id or text), unavailable."""

    @abstractmethod
    def query(self, user: str, query: str, k: int, filters: dict) -> QueryResult:
        """Search for what `user` may read. At most k passages, highest score first; an empty list is not an error. Raises invalid_argument (k < 1, empty query), permission_denied, unavailable, rate_limited."""

    @abstractmethod
    def forget(self, ids: list) -> Removed:
        """Remove documents; idempotent (an absent id is not an error). Raises invalid_argument (empty list), unavailable."""


class UserModel(Plugin):
    KIND = "user_model"

    @abstractmethod
    def observe(self, user: str, event: dict) -> Recorded:
        """Record one event about a user. Raises invalid_argument (empty user), unavailable."""

    @abstractmethod
    def predict(self, user: str, context: dict) -> PredictionResult:
        """Predict the next action; confidence is in [0, 1] and is 0 for a user with no observations (not an error). Raises invalid_argument (empty user), unavailable."""

    @abstractmethod
    def forget(self, user: str) -> Removed:
        """Delete everything about a user; idempotent. Raises invalid_argument (empty user), unavailable."""


class ComputerUse(Plugin):
    KIND = "computer_use"

    @abstractmethod
    def open(self, spec: dict) -> Opened:
        """Open a session on a virtual display. Raises unsupported (unknown environment), unavailable."""

    @abstractmethod
    def observe(self, session: str) -> Observed:
        """Read the screen. Raises not_found (unknown or closed session), unavailable."""

    @abstractmethod
    def act(self, session: str, action: dict) -> Observed:
        """Perform one action and return the screen afterwards. Validate arguments first: invalid_argument (an action without a type) comes before not_found (unknown session). Raises unavailable."""

    @abstractmethod
    def close(self, session: str) -> Closed:
        """Close a session; its id is not valid afterwards. Raises not_found."""


class ResearchRunner(Plugin):
    KIND = "research"

    @abstractmethod
    def fork(self, spec: dict) -> Forked:
        """Start an isolated fork. Raises unsupported, unavailable."""

    @abstractmethod
    def step(self, fork_id: str) -> Stepped:
        """Advance a fork one step. Raises not_found, unavailable."""

    @abstractmethod
    def stop(self, fork_id: str) -> Stopped:
        """Stop a fork; idempotent (stopping a stopped fork returns stopped=true). Raises not_found for a fork that never existed."""


class IsolationBackend(Plugin):
    KIND = "isolation"

    @abstractmethod
    def provision(self, spec: dict) -> Provisioned:
        """Create a sandbox. `effective` lists the settings actually in force, not those requested. Raises unsupported (a tier or option this backend cannot provide), unavailable."""

    @abstractmethod
    def verify(self, sandbox_id: str, required: dict) -> Verification:
        """ok is true only if every required setting is observed with the required value; never ok for a setting it did not observe. `observed` may carry an `egress_inspection` record (design §10.6). Raises not_found, unavailable."""

    @abstractmethod
    def destroy(self, sandbox_id: str) -> Destroyed:
        """Remove a sandbox; idempotent (destroyed=false when there was none; not an error). Raises unavailable."""


class Assurance(Plugin):
    KIND = "assurance"

    @abstractmethod
    def attest(self, subject: str, evidence: dict) -> Attested:
        """Produce an attestation that identifies its issuer and the subject and binds the evidence. Raises invalid_argument (empty subject), unavailable."""

    @abstractmethod
    def broker_child(self, spec: dict) -> Child:
        """Create a child sandbox through the broker. Raises unsupported, unavailable."""


class Evaluator(Plugin):
    KIND = "evaluator"

    @abstractmethod
    def evaluate(self, criterion: dict, subject: dict, evidence: dict) -> Verdict:
        """Judge one criterion: PASS, FAIL, PARTIAL or UNCERTAIN with reason codes and evidence references; UNCERTAIN (never PASS) when evidence is missing or insufficient. Raises invalid_argument (empty criterion id), unavailable, rate_limited."""


class Proposer(Plugin):
    KIND = "proposer"

    @abstractmethod
    def propose(self, round: int, n: int, base: str, ctx: dict) -> ProposalSet:
        """Propose at most n edits for a round. Raises invalid_argument (n < 1), unavailable, rate_limited."""

    @abstractmethod
    def analyze_weakness(self, report: dict) -> Findings:
        """Find weaknesses in an evaluation report; a list, possibly empty. Raises unavailable."""


INTERFACES: dict[str, type[Plugin]] = {c.KIND: c for c in (Retriever, UserModel, ComputerUse, ResearchRunner, IsolationBackend, Assurance, Evaluator, Proposer)}
