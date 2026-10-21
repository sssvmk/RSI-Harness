# Plugin contract

*Generated from `spec/plugins/kinds.json` and `harness/plugin_api.py` by `harness plugins --contract`. Do not edit by hand; `tests/test_plugin_contract.py` fails if it is out of date.*

A plugin is an implementation of one of the interfaces below. A deployment names the implementation class in its configuration (`plugins.load[].entry.class`, as `package.module:ClassName`, with `entry.config` as its constructor arguments); the harness loads it, checks that it implements the kind's interface, calls every method with its sample input and checks the answer against the contract, and then calls it only through that interface. A class that leaves a method unimplemented cannot be created.

## How errors work

A plugin signals an **expected** failure by raising `PluginRejection(code)` with one of the codes below, and only with a code that the method declares. The harness turns what happens into one of its own exceptions:

| What happens | The harness throws | Counts toward quarantine |
|---|---|---|
| The plugin raises a declared code | `PluginRejected` (`reason` = the code, `retryable`) | No, except `unavailable` |
| The plugin raises a code the method does not declare | `ContractViolation` | Yes |
| The plugin raises any other exception | `PluginFailed` | Yes |
| The plugin returns the wrong shape | `ContractViolation` | Yes |
| The plugin is too slow | `PluginTimeout` | Yes |
| The harness's caller sends a bad request | `ContractViolation`, before the plugin is called | No |
| No active plugin of the kind, or it is quarantined | `PluginUnavailable` | n/a |
| The run's tool budget cannot cover the call | `PluginBudgetExhausted` | n/a |
| At load: a bad manifest, a refused profile, a class that does not implement the interface, a failed conformance run | `ManifestInvalid`, `PolicyDenied`, `ConformanceFailed` | n/a |

Three consecutive faults quarantine a plugin, and a success resets the count. Arguments are validated **before** any resource is looked up, so `invalid_argument` comes before `not_found`.

| Code | Retryable | Meaning |
|---|---|---|
| `invalid_argument` | no | The arguments are well-formed but not acceptable (an empty id, k below 1, an action without a type). Arguments are validated before any resource is looked up. |
| `not_found` | no | The session, fork or sandbox that the call names does not exist. |
| `permission_denied` | no | The caller may not do this. The plugin enforces it; the harness does not rely on the model to. |
| `conflict` | no | The request conflicts with the current state. |
| `unsupported` | no | The plugin does not support this request (an unknown tier or environment). |
| `unavailable` | yes | A backend that the plugin depends on is down or cannot be reached. |
| `rate_limited` | yes | A limit of a backend was reached; try again later. |

## Probes

A probe is a safe input that must make the method raise exactly the named error without changing state. A plugin must treat each reserved id as nonexistent or unauthorized. Because probes are run when a plugin is loaded, they never touch real data.

Reserved ids: user `__no_access__ (has no permissions on anything)`; session `__no_such_session__`; fork `__no_such_fork__`; sandbox `__no_such_sandbox__`. With `plugins.verify_errors: true`, the host runs every probe when it loads a plugin and refuses a plugin that does not raise exactly the declared error.

## `assurance`: `Assurance`

High-assurance services: attestation and brokered child sandboxes. Needs microVMs, brokered children and an attested review plane.

Interface version 1.0.0; needs a **high** deployment or stricter; the plugin may hold the grants ['host']; its answers are `trusted`.

### `Assurance.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Assurance.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Assurance.attest(subject: str, evidence: dict)`

Produce an attestation about a subject from evidence.

**Returns** attestation: an object that identifies its issuer and the subject and binds the evidence (for example by digest).

| Field | Type |
|---|---|
| `attestation` | dict |

Effect: read only. Timeout: 60 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The subject is empty. | `{"subject": "", "evidence": {}}` |
| `unavailable` | The attestation service is down. | none (cannot be triggered safely) |

### `Assurance.broker_child(spec: dict)`

Create a child sandbox through the broker.

**Returns** child_id: an id for the child.

| Field | Type |
|---|---|
| `child_id` | str |

Effect: write. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `unsupported` | The spec asks for something the broker cannot provide. | `{"spec": {"__unsupported__": true}}` |
| `unavailable` | The broker is down. | none (cannot be triggered safely) |

## `computer_use`: `ComputerUse`

Operates a virtual display. Needs a browser or desktop environment and a vision model.

Interface version 1.0.0; needs a **standard** deployment or stricter; the plugin may hold the grants ['model_calls', 'network']; its answers are `untrusted`.

### `ComputerUse.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `ComputerUse.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `ComputerUse.open(spec: dict)`

Open a session on a virtual display.

**Returns** session: an id that observe, act and close accept until the session is closed.

| Field | Type |
|---|---|
| `session` | str |

Effect: write. Timeout: 60 s.

| May raise | When | Probe |
|---|---|---|
| `unsupported` | The environment named in the spec is not available. | `{"spec": {"environment": "__unsupported__"}}` |
| `unavailable` | No display could be started. | none (cannot be triggered safely) |

### `ComputerUse.observe(session: str)`

Read the current screen of a session.

**Returns** observation: what is on the screen, as an object.

| Field | Type |
|---|---|
| `observation` | dict |

Effect: read only. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `not_found` | The session does not exist or was closed. | `{"session": "__no_such_session__"}` |
| `unavailable` | The display is not reachable. | none (cannot be triggered safely) |

### `ComputerUse.act(session: str, action: dict)`

Perform one action (a click, a key, a scroll) in a session.

**Returns** observation: the screen after the action. The action must have a `type`.

| Field | Type |
|---|---|
| `observation` | dict |

Effect: write. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The action has no type. This is checked before the session is looked up. | `{"session": "__no_such_session__", "action": {}}` |
| `not_found` | The session does not exist or was closed. | `{"session": "__no_such_session__", "action": {"type": "noop"}}` |
| `unavailable` | The display is not reachable. | none (cannot be triggered safely) |

### `ComputerUse.close(session: str)`

Close a session.

**Returns** closed: true. Closing releases the display, and the session id is not valid afterwards.

| Field | Type |
|---|---|
| `closed` | bool |

Effect: write. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `not_found` | The session does not exist. | `{"session": "__no_such_session__"}` |

## `evaluator`: `Evaluator`

Evaluation of one criterion: a deterministic script (`code`), a language model scoring against a rubric (`llm_judge`), or a model whose output is a typed verdict (`typesafe_ai`). The harness derives its independence from the agent under test and decides whether it may gate.

Interface version 1.0.0; needs a **core** deployment or stricter; the plugin may hold the grants ['model_calls']; its answers are `untrusted`.

### `Evaluator.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Evaluator.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Evaluator.evaluate(criterion: dict, subject: dict, evidence: dict)`

Judge one criterion against the evidence.

**Returns** verdict (PASS, FAIL, PARTIAL or UNCERTAIN), reason_codes, evidence_refs, and optionally confidence. UNCERTAIN, never PASS, when the evidence is missing or insufficient.

| Field | Type |
|---|---|
| `verdict` | PASS | FAIL | PARTIAL | UNCERTAIN |
| `reason_codes` | list[str] |
| `evidence_refs` | list[str] |
| `confidence` | float (optional) |

Effect: read only. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The criterion has an empty id. | `{"criterion": {"id": ""}, "subject": {}, "evidence": {}}` |
| `unavailable` | The judge model is down. | none (cannot be triggered safely) |
| `rate_limited` | The judge model refused the call for now. | none (cannot be triggered safely) |

## `isolation`: `IsolationBackend`

A container, microVM or other isolation backend. The harness requires the effective settings to match the tier before a workload starts.

Interface version 1.0.0; needs a **core** deployment or stricter; the plugin may hold the grants ['host']; its answers are `trusted`.

### `IsolationBackend.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `IsolationBackend.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `IsolationBackend.provision(spec: dict)`

Create a sandbox for a tier.

**Returns** sandbox_id, and effective: every setting that is actually in force (not the settings that were requested).

| Field | Type |
|---|---|
| `sandbox_id` | str |
| `effective` | dict |

Effect: write. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `unsupported` | The tier or an option in the spec is not supported by this backend. | `{"spec": {"tier": "__unsupported__"}}` |
| `unavailable` | The container host is not reachable. | none (cannot be triggered safely) |

### `IsolationBackend.verify(sandbox_id: str, required: dict)`

Check a sandbox against the settings a tier requires.

**Returns** ok: true only if every required setting is present in observed with the required value; observed: the settings in force, including any `egress_inspection` record (design §10.6). It never reports ok for a setting it did not observe.

| Field | Type |
|---|---|
| `ok` | bool |
| `observed` | dict |

Effect: read only. Timeout: 60 s.

| May raise | When | Probe |
|---|---|---|
| `not_found` | The sandbox does not exist. | `{"sandbox_id": "__no_such_sandbox__", "required": {}}` |
| `unavailable` | The container host is not reachable. | none (cannot be triggered safely) |

### `IsolationBackend.destroy(sandbox_id: str)`

Remove a sandbox. Idempotent.

**Returns** destroyed: true if a sandbox was removed, false if there was none. Not an error.

| Field | Type |
|---|---|
| `destroyed` | bool |

Effect: write. Timeout: 60 s.

| May raise | When | Probe |
|---|---|---|
| `unavailable` | The container host is not reachable. | none (cannot be triggered safely) |

## `proposer`: `Proposer`

Self-improvement: proposes harness edits and mines weaknesses. Needs a model.

Interface version 1.0.0; needs a **core** deployment or stricter; the plugin may hold the grants ['model_calls']; its answers are `untrusted`.

### `Proposer.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Proposer.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Proposer.propose(round: int, n: int, base: str, ctx: dict)`

Propose harness edits for a round of self-improvement.

**Returns** proposals: at most n, each with its edits and a manifest.

| Field | Type |
|---|---|
| `proposals` | list[dict] |

Effect: read only. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | n is below 1. | `{"round": 1, "n": 0, "base": "c0", "ctx": {}}` |
| `unavailable` | The model is down. | none (cannot be triggered safely) |
| `rate_limited` | The model refused the call for now. | none (cannot be triggered safely) |

### `Proposer.analyze_weakness(report: dict)`

Find weaknesses in an evaluation report.

**Returns** findings: a list, possibly empty.

| Field | Type |
|---|---|
| `findings` | list |

Effect: read only. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `unavailable` | The model is down. | none (cannot be triggered safely) |

## `research`: `ResearchRunner`

Research mode in an isolated fork. Needs real models and isolated forks.

Interface version 1.0.0; needs a **high** deployment or stricter; the plugin may hold the grants ['model_calls']; its answers are `untrusted`.

### `ResearchRunner.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `ResearchRunner.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `ResearchRunner.fork(spec: dict)`

Start an isolated fork for research.

**Returns** fork_id: an id that step and stop accept.

| Field | Type |
|---|---|
| `fork_id` | str |

Effect: write. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `unsupported` | The spec asks for something the plugin cannot isolate. | `{"spec": {"__unsupported__": true}}` |
| `unavailable` | No fork could be started. | none (cannot be triggered safely) |

### `ResearchRunner.step(fork_id: str)`

Advance the research in a fork by one step.

**Returns** state: the fork's state after the step, as an object.

| Field | Type |
|---|---|
| `state` | dict |

Effect: write. Timeout: 300 s.

| May raise | When | Probe |
|---|---|---|
| `not_found` | The fork does not exist. | `{"fork_id": "__no_such_fork__"}` |
| `unavailable` | The fork is not reachable. | none (cannot be triggered safely) |

### `ResearchRunner.stop(fork_id: str)`

Stop a fork. Idempotent.

**Returns** stopped: true, also when the fork was already stopped.

| Field | Type |
|---|---|
| `stopped` | bool |

Effect: write. Timeout: 60 s.

| May raise | When | Probe |
|---|---|---|
| `not_found` | The fork never existed. | `{"fork_id": "__no_such_fork__"}` |

## `retrieval`: `Retriever`

Knowledge retrieval over an index. Needs an embedding model and an index. Passages are untrusted; access control is enforced by the plugin and checked again by the harness.

Interface version 1.0.0; needs a **core** deployment or stricter; the plugin may hold the grants ['model_calls']; its answers are `untrusted`.

### `Retriever.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Retriever.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `Retriever.index(documents: list[dict])`

Add or replace documents in the index.

**Returns** indexed (the number of documents now in the index from this call) and index_version (changes whenever the index content changes).

| Field | Type |
|---|---|
| `indexed` | int |
| `index_version` | str |

Effect: write. Timeout: 120 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | A document has an empty id or empty text. | `{"documents": [{"id": "", "text": "x"}]}` |
| `unavailable` | The index backend is down. | none (cannot be triggered safely) |

### `Retriever.query(user: str, query: str, k: int, filters: dict)`

Search the index for what a user may read.

**Returns** passages: at most k, ordered by score from highest to lowest, each with its id, text, score, source and permissions. An empty list when nothing matches; that is not an error.

| Field | Type |
|---|---|
| `passages` | list[dict] |

Effect: read only. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | k is below 1, or the query is empty. | `{"user": "u", "query": "q", "k": 0, "filters": {}}` |
| `permission_denied` | The user may not read anything (the plugin, not the harness, enforces access). | `{"user": "__no_access__", "query": "q", "k": 1, "filters": {}}` |
| `unavailable` | The index backend is down. | none (cannot be triggered safely) |
| `rate_limited` | The embedding or index backend refused the call for now. | none (cannot be triggered safely) |

### `Retriever.forget(ids: list[str])`

Remove documents from the index. Idempotent.

**Returns** removed: how many of the ids were present. Removing an id that is not there is not an error.

| Field | Type |
|---|---|
| `removed` | int |

Effect: write. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The list of ids is empty. | `{"ids": []}` |
| `unavailable` | The index backend is down. | none (cannot be triggered safely) |

## `user_model`: `UserModel`

User modeling and next-action prediction. User-owned; needs real user data and models.

Interface version 1.0.0; needs a **standard** deployment or stricter; the plugin may hold the grants ['model_calls']; its answers are `untrusted`.

### `UserModel.describe()`

Say who the plugin is.

**Returns** id, kind and version, which must equal the manifest's.

| Field | Type |
|---|---|
| `id` | str |
| `kind` | str |
| `version` | str |

Effect: read only. Timeout: 5 s. Raises nothing.

### `UserModel.health()`

Say whether the plugin can serve calls now.

**Returns** ok: false when the plugin cannot serve calls. Health reports a problem and never raises one.

| Field | Type |
|---|---|
| `ok` | bool |

Effect: read only. Timeout: 5 s. Raises nothing.

### `UserModel.observe(user: str, event: dict)`

Record one event about a user.

**Returns** recorded: true when the event was stored.

| Field | Type |
|---|---|
| `recorded` | bool |

Effect: write. Timeout: 10 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The user id is empty. | `{"user": "", "event": {}}` |
| `unavailable` | The store is down. | none (cannot be triggered safely) |

### `UserModel.predict(user: str, context: dict)`

Predict the user's next action from the context.

**Returns** prediction (any object) and confidence in [0, 1]. A user with no observations gets confidence 0, not an error.

| Field | Type |
|---|---|
| `prediction` | dict |
| `confidence` | float |

Effect: read only. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The user id is empty. | `{"user": "", "context": {}}` |
| `unavailable` | The model is down. | none (cannot be triggered safely) |

### `UserModel.forget(user: str)`

Delete everything held about a user. Idempotent.

**Returns** removed: how many records were deleted (0 if there were none).

| Field | Type |
|---|---|
| `removed` | int |

Effect: write. Timeout: 30 s.

| May raise | When | Probe |
|---|---|---|
| `invalid_argument` | The user id is empty. | `{"user": ""}` |
| `unavailable` | The store is down. | none (cannot be triggered safely) |
