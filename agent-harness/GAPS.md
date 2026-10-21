# Closure report: open, soft closed, hard closed

*Generated from `closure/ledger.json` on 2026-10-21; do not edit by hand. Change states with `python -m harness.closure` (see README §4.16).*

| State | Meaning |
|---|---|
| **Open** | The design does not close the requirement yet: it is missing, deviates from the PRD, rests on an undecided question, or lags behind the code |
| **Soft closed** | **Closed in the design, waiting for implementation in code** |
| **Hard closed** | **Implemented as the PRD and the design say, and closed for good.** A change to the requirement or its design makes the close *stale*; only an explicit, logged `reopen` takes it back |

**309 requirements:** 86 hard closed, 222 soft closed, 1 open.

Of the 86 hard closes, **14 were verified clause by clause and 72 are graded**: they rest on the gap review of the code and tests, and have not been re-verified against the current PRD and design text. Treat a graded hard close as provisional until it is verified.

## Summary by profile

| Profile | Open | Soft closed | Hard closed | Total |
|---|---|---|---|---|
| Core | 0 | 36 | 32 | 68 |
| Standard | 1 | 153 | 54 | 208 |
| High | 0 | 33 | 0 | 33 |

## Summary by section

| Section | Open | Soft | Hard | Total |
|---|---|---|---|---|
| §3.1 | 0 | 40 | 0 | 40 |
| §3.2 | 0 | 4 | 8 | 12 |
| §3.3 | 0 | 4 | 14 | 18 |
| §3.4 | 0 | 6 | 2 | 8 |
| §3.5 | 0 | 6 | 0 | 6 |
| §3.6 | 0 | 11 | 10 | 21 |
| §3.7 | 0 | 6 | 6 | 12 |
| §3.8 | 0 | 3 | 2 | 5 |
| §3.9 | 0 | 5 | 8 | 13 |
| §4.1 | 0 | 9 | 1 | 10 |
| §4.2 | 0 | 5 | 1 | 6 |
| §4.3 | 0 | 10 | 7 | 17 |
| §5 | 0 | 14 | 3 | 17 |
| §6.1 | 0 | 2 | 3 | 5 |
| §6.2 | 0 | 0 | 5 | 5 |
| §6.3 | 0 | 7 | 8 | 15 |
| §6.4 | 0 | 4 | 3 | 7 |
| §6.5 | 1 | 10 | 0 | 11 |
| §6.6 | 0 | 4 | 0 | 4 |
| §6.7 | 0 | 3 | 1 | 4 |
| §6.10 | 0 | 1 | 0 | 1 |
| §6.11 | 0 | 1 | 3 | 4 |
| §6.12 | 0 | 14 | 0 | 14 |
| §7 | 0 | 8 | 1 | 9 |
| §8 | 0 | 16 | 0 | 16 |
| §9 | 0 | 11 | 0 | 11 |
| §10 | 0 | 18 | 0 | 18 |

## Health of the ledger

* Structural errors: **0**
* Hard closes that are **stale** (the requirement or its design changed since they were closed): **0**
* Soft closes whose design changed since they were closed: **0**
* Soft closes resting on a *proposed* design parameter that nobody has confirmed: **60**
* Soft closes **awaiting a plugin** (the harness defines and enforces the contract; a deployment supplies the capability): **112**

## Open: the design does not close these yet

| Requirement | Profile | Why it is open |
|---|---|---|
| §6.5.1 Hand-Authored Prompts as Versioned Seeds | Standard | The PRD requires every prompt change, by a human or the loop, to pass the gate. The owner decided hand-written prompts are the human's responsibility, and the PRD and design have not been reconciled with that decision. |

## Soft closed: closed in design, waiting for code

Ordered Core first. *Code* says how far the code is: **needs update** means it was implemented against an earlier version of the requirement or design; **partial** and **not implemented** are as they say. The last column is the gap review's note on what exists now, so what remains is the difference between it and the requirement and design. A star marks a close that rests on a proposed parameter (listed) not yet confirmed, and ⧉ names the plugin kind that will supply the capability.

| Requirement | Profile | Code | What the code has today |
|---|---|---|---|
| §3.1 Lossy Transformation Awareness ★ P67, P70, P71, P72, P73 | Core | partial | summary labelled with its span; raw log retrievable |
| §3.1 Relevance Filtering (Anti-Bloat) ★ P56, P57, P58, P59, P60, P61, P62 | Core | partial | clearing, compaction and hydration exist; no per-ingredient relevance scoring |
| §3.3 Multi-Vector Budgeting | Core | partial | Built in both languages: one central budget service for model calls, tool calls and submissions, reservations, hierarchy, bounded concurrency, declared-unmetered compute with safeguards. Remaining: compute is unmetered (no sandbox |
| §3.4 Evaluator Fallibility ★ P84 | Core | partial |  |
| §3.4 Evaluator Independence Levels ⧉ evaluator | Core | partial | Built: an `evaluator` plugin kind with one contract, three declared types, the independence level derived from the agent under test, and the profile policy for what may gate. Python only: a criterion verified by `evaluator` is jud |
| §3.5 Control Plane Ownership | Core | partial | definition pinning only |
| §3.5 Definition Pinning | Core | partial | Built in both languages: immutable hashed versions (the database refuses updates), a manifest of exact versions, resolution only through the manifest, hash verified before use, fail-closed mismatch, audited migration, floating ali |
| §3.6 Declared Test Visibility | Core | partial | Built in both languages: a validated declaration (contradictions refused at validation and at run start), the declaration in the pinned manifest, policy and verified runtime state recorded separately, exposed tests recorded, and p |
| §3.6 Outcome Contract and Verdicts ★ P85 | Core | partial | Built in both languages: a contract fixed before execution, the four verdicts with a stated precedence, evidence-only derivation, exact decimals, and outcome metrics with a documented denominator; a timeout on a gated action is no |
| §3.6 Transcript and Outcome Evaluation ★ P85 | Core | partial | Built in both languages: transcript and outcome recorded separately, outcome graded from environment state, and a deterministic TrajectoryAnalyzer (steps, tool errors, retries, budget use, policy denials, approvals, safety events, |
| §3.7 Human Operator Telemetry | Core | partial | Built in both languages: `harness events/usage/alerts/metrics` and `inspect --telemetry`, every category available or unavailable by what the build collects, redaction, and every row carrying its run id. Content is gated by a `--r |
| §4.1 Explicit Environment Provisioning ⧉ isolation | Core | partial | lifecycle events; image/mounts not honoured. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned,  |
| §4.1 Isolation Tiers ⧉ isolation | Core | partial | Design settled (§10.2.1). Python only: the tier gate provisions through the `isolation` plugin, compares `verify` with every required setting (a missing one is unmet; numbers can be bounded), destroys the sandbox and refuses the r |
| §4.2 Zero-Credential Sandboxing ⧉ isolation | Core | partial | environment scrubbed; no isolation, so host files remain readable. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted |
| §4.3 Egress as a Capability Grant ★ P81 ⧉ isolation | Core | partial | grants recorded; no enforcement proxy. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no |
| §5 Control vs. World State Reconciliation | Core | partial | Built in both languages: recovery resolves a started call on a gated resource from the resource's actual state (already applied: not repeated; not applied: re-executed; unreadable: halt for a human), and the same check runs on ret |
| §5 Resource Teardown and Retention ★ P82 | Core | partial | teardown yes; retention jobs no |
| §3.1 Active Context Selection ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Core | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update.  |
| §3.1 Compaction Strategy | Core | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update.  |
| §3.1 Exact-Fact Ledger ★ P67, P70, P71, P72, P73 | Core | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Hybrid Context Strategy ★ P74, P75, P76, P77 | Core | needs update | Implemented against the earlier PRD and design. Since then the requirement text and original lines of a cited design section changed (see CHANGES.md): check the code against it and update.  |
| §3.1 Minimal Loop Execution ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Core | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update.  |
| §3.1 Priority-Based Prompt Assembly ★ P56, P57, P58, P59, P60, P61, P62 | Core | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Progressive Disclosure ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Core | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update. skills index and tool exposure threshold |
| §3.1 Relevance-Ranked Selection ★ P56, P57, P58, P59, P60, P61, P62 | Core | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Retrieval of Compacted State ★ P67, P70, P71, P72, P73 | Core | needs update | Implemented against the earlier PRD and design. Since then the requirement text and original lines of a cited design section changed (see CHANGES.md): check the code against it and update. history.get / recall |
| §3.1 State Decoupling (Durable vs. Active) ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Core | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update. log vs projections |
| §3.1 Token-Budget Selection ★ P56, P57, P58, P59, P60, P61, P62 | Core | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.4 Agent Legibility ★ P85 | Core | not implemented | Not built. Needs `harness task validate --legibility` (a top-level index, documented build and test commands, logs reachable through tools, no hidden required state) and the PRD's discoverability tools. The earlier 'implemented' g |
| §3.5 Protocol Boundary | Core | not implemented | no MCP client |
| §3.5 State Decoupling | Core | not implemented |  |
| §4.1 Isolation Tier Enforcement ⧉ isolation | Core | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, b |
| §4.1 Untrusted Execution (Code on Demand) ⧉ isolation | Core | not implemented | no isolation. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the ha |
| §4.1 Verified Environment Provisioning ⧉ isolation | Core | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, b |
| §4.2 Orchestrator vs. Isolation Boundary ⧉ isolation | Core | not implemented | no real boundary. Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with th |
| §5 Durability & Resumption ★ P83 | Core | needs update | Implemented against the earlier PRD and design. Since then the requirement text changed (see CHANGES.md): check the code against it and update.  |
| §3.1 Artifact Injection ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | partial | working-state artifacts only |
| §3.1 Cache-Aware Context Assembly ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | partial | stable segments and hashes; no provider cache-control markers |
| §3.1 Governed User and Context Memory | Standard | partial | Design settled (§26.1.5): user memory is off by default and preflight PF-U01 refuses to enable it until the governed store exists. Built: the refusal. Remaining: the store itself (records with evidence, confidence and staleness; i |
| §3.1 Just-in-Time Retrieval ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | partial | recall tool only; no lazy file references |
| §3.1 Project-Scoped Memory ★ P63, P64, P65, P66, P74, P75, P76, P77 | Standard | partial | Built in both languages: when `memory.project.enabled` is set, a run is refused until the user has answered (a name, or none), by the caller supplying it or by being asked; the answer is recorded in the manifest as `project`. Rema |
| §3.1 Structured Working State ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | partial | named free-text artifacts, no schema |
| §3.2 Format Economy | Standard | partial | result truncation only |
| §3.2 Selection Cost Management (C_selection) ★ P80 | Standard | partial | Built in both languages: tool.search and dynamic exposure, observed selection errors by tool, and alerting on the error rate. Remaining: selection cost per task (tokens, search latency, retry cost) is not measured, and cost alerts |
| §3.2 Selection Error Alerting ★ P80 | Standard | partial | Built in both languages: rolling-window rules (window, minimum sample, warning and critical thresholds), de-duplication with resolve, an immediate security alert for an unauthorized selection that reaches execution, alert events w |
| §3.5 Server Trust | Standard | partial | hash pinning |
| §3.6 Deterministic Testing and Mocks | Standard | partial | scripted transport; no mock model server |
| §3.6 Replication and Variance | Standard | partial | sizing function exists, not wired |
| §3.6 Trials and Aggregation | Standard | partial | confirmation trials only |
| §3.7 Agent-Facing Observability | Standard | partial | budget and stuck guidance |
| §3.7 Correlation Identifiers | Standard | partial | correlation_id on events; no trace ids |
| §3.7 Evaluation-Integrity Closure | Standard | partial | Built in both languages: the detector registry (`spec/integrity/detectors.json`, one required detector per class), the seven statuses with strict transitions, independent verification of a fix, evidence tied to the detector's vers |
| §3.8 Cache Safety | Standard | partial | no cache exists |
| §3.8 Required Capabilities | Standard | partial | infrastructure, no standard hooks shipped |
| §3.9 Compensation for External Effects ★ P52 | Standard | partial | mechanism implemented (withheld until approved, or compensated in reverse, failure escalates); no harness tool uses it yet |
| §3.9 Effect Locality | Standard | partial | structural in the runtime (no separate uninstall path); harness subsystems still have their own teardown code |
| §3.9 Revertible Effects | Standard | partial | runtime implemented and tested (LIFO inverses, witnessed per state); the harness's own sandboxes, grants, timers and sessions are not yet routed through it |
| §3.9 Stated Equivalence | Standard | partial | stated and tested for 5 generic classes (table, counter, chain, workspace, opaque); not yet for the harness's own resource classes |
| §4.1 Persistent Execution State ★ P83 | Standard | partial | sessions persist in a run, not across restarts |
| §4.2 Least-Privilege Defaults | Standard | partial |  |
| §4.3 Consent Visibility | Standard | partial | approval events show redacted args; no UI |
| §4.3 Exfiltration Monitoring ★ P81 ⧉ isolation | Standard | partial | Built: `exfiltration_claim`, which derives the claim from what the isolation plugin demonstrates and never claims complete detection (both languages). Python also records the claim in the `isolation.verified` event when the tier g |
| §4.3 Memory Write-Path Integrity ★ P63, P64, P65, P66 | Standard | partial | provenance, quarantine, administrator-configurable low-trust sources and confirmation, regex screen, behaviour paths refused; no out-of-character detection |
| §4.3 Proactive Contextual Guidance ★ P81 | Standard | partial | budget and stuck guidance only |
| §4.3 Untrusted Evaluator Inputs ★ P84 | Standard | partial | report schema-validated; judge text shaped |
| §5 Atomic Submission Snapshots | Standard | partial | pause is a no-op on the dev backend |
| §5 Concurrency & State Consistency | Standard | partial | single writer; no run-concurrency limit |
| §5 Pre-Action Validation Gate | Standard | partial | Built in both languages: one gateway in ToolRuntime that the agent cannot skip, for any tool that declares a `world` block (today `fs.write`). It checks authorization, current approval, authoritative state, preconditions and versi |
| §5 Reconciliation States and Risk Tiers | Standard | partial | Built in both languages: the six states with explicit logged transitions (`unknown` never becomes `consistent` without an authoritative read, fixed by shared vectors), risk tiers by effect class with a per-tool critical override,  |
| §5 Snapshot Triggers | Standard | partial | on submission only |
| §6.1 Humans move up the stack, not out of the loop ⧉ proposer | Standard | partial | review tiers; no review UI. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no pl |
| §6.3 Change manifest ⧉ proposer | Standard | partial | A manifest is required and validated; checking its predictions against later rounds is not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract |
| §6.3 Preserved negative results ⧉ proposer | Standard | partial | Rejected candidates and reasons are stored in the archive and log; feeding them to a proposer is an interface only. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18 |
| §6.3 Proposal ⧉ proposer | Standard | partial | Proposal handling, screens, and gating exist; the model-driven proposer is an interface only. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is  |
| §6.3 Sequential vs. independent revision ⧉ proposer | Standard | partial | Independent candidates and parent selection exist; the controlled comparison the requirement asks for is not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18 |
| §6.3 Validity screens ⧉ proposer | Standard | partial | static screens done; contract and syntax checks not. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audi |
| §6.4 Reporting ⧉ proposer | Standard | partial | Gate decisions carry intervals; the campaign report and export are not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verifie |
| §6.4 Transfer and generalization ⧉ proposer | Standard | partial | Transfer and confirm set roles exist; no transfer evaluation has been run. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified a |
| §6.4 Updating vs. benefit ⧉ proposer | Standard | partial | Adherence and activation failures can be labelled; the separate measurement is not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defi |
| §6.6 Promotion of System-Level Proposals ⧉ proposer | Standard | partial | T2 approval path exists; no system-level proposer. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audite |
| §6.7 Itemized Deltas ⧉ proposer | Standard | partial | edits are itemized; no playbook curator. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinn |
| §7 Evaluation Integrity | Standard | partial |  |
| §7 Event Stream | Standard | partial | events exist; no subscription API |
| §10.2 Application Access ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.2 Feedback as Observation ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.2 Observers ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.2 Proposition Retrieval ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.2 Proposition Revision ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.2 Propositional User Model ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.3 Sample-Agreement Confidence ⧉ user_model | Standard | not implemented | next-action prediction: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinn |
| §10.3 Two-Phase Prediction With Trace Memory ⧉ user_model | Standard | not implemented | next-action prediction: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinn |
| §10.5 Next-Action Evaluation ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.5 User-Model Evaluation ⧉ user_model | Standard | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §3.1 Compacted-History Consultation ★ P67, P70, P71, P72, P73 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Compaction Miss Tracking ★ P67, P70, P71, P72, P73 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Confidence and Provenance Thresholds ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Contradiction Detection ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Cross-Run History Retrieval ★ P74, P75, P76, P77 | Standard | not implemented | The project a run belongs to is now recorded, which defines the default search scope. Remaining: the search over earlier runs' history, the citation of run and event, permission checks at query time, and deletion of derived entrie |
| §3.1 Decision and Constraint Ledger ★ P67, P70, P71, P72, P73 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Deduplication and Entity Resolution ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Deterministic Memory Policy ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Diversity and Deduplication ★ P56, P57, P58, P59, P60, P61, P62 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Dynamic Skill Loading ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update. skill.load |
| §3.1 Ephemeral Context Exclusion ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update. S5 guidance never enters history |
| §3.1 Hybrid and Reranked Candidate Retrieval ★ P56, P57, P58, P59, P60, P61, P62 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Incremental Context Updates ★ P56, P57, P58, P59, P60, P61, P62, P67, P70, P71, P72, P73, P74, P75, P76, P77 | Standard | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update. append-only body |
| §3.1 Incremental Topic Memory ★ P63, P64, P65, P66, P74, P75, P76, P77 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Memory Record Metadata ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Non-Destructive Tool Result Clearing | Standard | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update.  |
| §3.1 Query-Focused Summarization ★ P56, P57, P58, P59, P60, P61, P62 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Retention, Decay, and Eviction ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.1 Selective Hydration | Standard | needs update | Implemented against the earlier PRD and design. Since then original lines of a cited design section changed (see CHANGES.md): check the code against it and update. FTS-based |
| §3.1 Sensitive-Data Detection ★ P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.2 Format Optimization | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.4 Evaluator Approval and Abstention ★ P84 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.4 Judge Bias Controls ★ P84 | Standard | not implemented | no model judges |
| §3.4 Trajectory Access for Evaluators ★ P84 | Standard | not implemented |  |
| §3.5 Client-Host-Server Architecture | Standard | not implemented |  |
| §3.6 Assumption Expiration & Scaffolding Pruning | Standard | not implemented | no ScaffoldingAudit |
| §3.6 Capability and Regression Suites | Standard | not implemented |  |
| §3.6 Cost-Aware CI | Standard | not implemented |  |
| §3.6 Holistic Performance Measurement | Standard | not implemented |  |
| §3.7 Context and Memory Quality Metrics ★ P56, P57, P58, P59, P60, P61, P62, P63, P64, P65, P66 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §3.7 Privacy-Preserving Export | Standard | not implemented |  |
| §3.8 Gateway Routing | Standard | not implemented |  |
| §4.1 Evaluator Integrity Against Candidate Code | Standard | not implemented | identities not enforced |
| §4.2 Credential Proxies ⧉ isolation | Standard | not implemented | . Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, so th |
| §4.2 Separate Evaluator Credentials ⧉ isolation | Standard | not implemented | . Supplied by a `isolation` plugin (an isolation backend) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, so th |
| §4.3 Computer-Use Effect Classification | Standard | not implemented |  |
| §4.3 Data Compartmentalization | Standard | not implemented |  |
| §4.3 Passive Observation Consent | Standard | not implemented |  |
| §5 Background Job Management | Standard | not implemented |  |
| §5 Delegation Boundaries & Coordination Overhead | Standard | not implemented |  |
| §5 Demonstrated Justification for Delegation | Standard | not implemented |  |
| §5 Retention Enforcement ★ P82 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §5 Safe Session Resume ★ P83 | Standard | not implemented | New requirement written into the PRD and design on 2026-10-21; not built. |
| §5 Subagent Isolation and Return Contract | Standard | not implemented |  |
| §6.1 Simplicity over accumulation ⧉ proposer | Standard | not implemented | no scaffolding expiry. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin  |
| §6.11 Scaffolding Expiry ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.3 Production traces as input ⧉ proposer | Standard | not implemented | Trace export and masking for the loop are not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audi |
| §6.3 Weakness mining ⧉ proposer | Standard | not implemented | Weakness-analysis agents are not built (interface only). Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted,  |
| §6.4 Long-horizon value ⧉ proposer | Standard | not implemented | Not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with t |
| §6.5.1 Constrained Decoding ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.5.1 Declarative Signatures ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.5.1 Modular Execution ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.5.2 Diagnose via Reflection ⧉ proposer | Standard | not implemented | Optimizers are not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plug |
| §6.5.2 Genetic-Pareto Crossover ⧉ proposer | Standard | not implemented | Optimizers are not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plug |
| §6.5.2 Model Agnosticism & Downscaling ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.5.2 Multi-Stage Credit Assignment & Optimization (MIPRO Paradigm) ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.5.2 Reflective Prompt Evolution (GEPA Paradigm) ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.5.2 Sample Trajectories ⧉ proposer | Standard | not implemented | Optimizers are not built. Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plug |
| §6.5.2 Self-Bootstrapping Pipelines ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.6 Phase 1 (Prompt & Context Optimization) ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.6 Phase 2 (System-Level / Procedural Optimization) ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.6 Phase 3 (Alternate) ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §6.7 Separate Mechanism from Content ⧉ proposer | Standard | not implemented | . Supplied by a `proposer` plugin (model-driven self-improvement) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harnes |
| §7 Evaluation Hosting | Standard | not implemented |  |
| §7 Governed Topology Definitions | Standard | not implemented |  |
| §7 Metered Compute and Sandboxes | Standard | not implemented |  |
| §7 Per-Agent Scoping | Standard | not implemented |  |
| §7 Self-Improvement Layers | Standard | not implemented |  |
| §7 Untrusted Inter-Agent Boundary | Standard | not implemented |  |
| §8.1 Corrective Retrieval ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.1 Hybrid Retrieval ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.1 Late-Interaction Retrieval ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.1 Reranking ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.1 Semantic Chunking ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.2 Hierarchical Summary Index ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.2 Index Cost and Refresh ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.2 Knowledge Graph Index ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.3 Deletion and Update Propagation ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.3 Grounding and Citation ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.3 Ingestion Provenance and Allowlisting ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.3 Permission-Aware Retrieval ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.4 Component Ablation and Cost ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.4 Retrieval Evaluation ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.4 Retrieval Parameter Tuning ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §8.4 Versioned Index Artifacts ⧉ retrieval | Standard | not implemented | retrieval module not built. Supplied by a `retrieval` plugin (knowledge retrieval) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ship |
| §9.1 Isolated Interaction Environment ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.1 Session Scope Declaration ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.2 Downloads Are Untrusted ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.2 Login Handoff ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.2 Risky UI Event Confirmation ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.3 Cross-Application Workflows ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.3 GUI Grounding ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.3 Navigation Strategy ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.3 Observation Modes ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.4 Action Recording and Replay ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §9.4 Computer-Use Evaluation ⧉ computer_use | Standard | not implemented | computer use module not built. Supplied by a `computer_use` plugin (computer use) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships |
| §3.3 Compute, Submission, and Evaluator Budgets ⧉ assurance | High | partial | submissions enforced; compute and evaluator budgets not metered. Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budget |
| §3.9 Live Replacement ★ P50, P55 ⧉ assurance | High | partial | replacement by retire+insert, run pinning (opt-in to follow), declarative entries with per-field dispatch and the paper's transactional HMR algorithms; not wired to harness run creation or config_commit. Supplied by a `assurance`  |
| §10.1 Contextual-Integrity Audit ⧉ user_model | High | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.1 Deployment Approval ★ P53 ⧉ user_model | High | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.1 Local-First Processing ★ P53 ⧉ user_model | High | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.1 Sensitive Inference Policy ⧉ user_model | High | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §10.3 Online Learning ⧉ user_model | High | not implemented | next-action prediction: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinn |
| §10.3 Passive Trajectory Labeling ⧉ user_model | High | not implemented | next-action prediction: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinn |
| §10.3 Trained Predictors ⧉ user_model | High | not implemented | next-action prediction: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinn |
| §10.4 Execution Through Computer-Use Agents ⧉ user_model | High | not implemented | user modeling: not implemented (design only). Supplied by a `user_model` plugin (user modeling) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and n |
| §3.1 Scheduled Wake-Ups (Heartbeats) ⧉ assurance | High | not implemented | heartbeat scheduler. Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships  |
| §3.3 Confidence-Gated Action ⧉ assurance | High | not implemented | . Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, s |
| §3.3 Initiative and Interruption Policy ⧉ assurance | High | not implemented | . Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, s |
| §3.6 Action-Level Security and Privacy Evaluation ⧉ assurance | High | not implemented | . Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, s |
| §4.1 Backend Parity of Isolation ⧉ assurance | High | not implemented | . Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, s |
| §4.1 Brokered Child Sandboxes ⧉ assurance | High | not implemented | . Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, s |
| §4.3 Quarantined Reader ⧉ assurance | High | not implemented | . Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the harness, s |
| §6.10 Experience-Driven RL Optimization ⧉ assurance | High | not implemented | post-v1. Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the har |
| §6.12 Attested Guardrail Suite ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Blocking Triggers ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Escape Monitoring and Kill Switch ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Human-Control Safety Evaluations ★ P47 ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Independent Verification ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Lineage and Reproducibility ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Operator-Set Budgets ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Promotion Path ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Ratchet ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Research Audit ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Research Fork Isolation ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Review Package and Developer Gate ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Trust-Anchor Proposals ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.12 Unrestricted Fork Surface ⧉ research | High | not implemented | research mode: not implemented (design only). Supplied by a `research` plugin (research mode in an isolated fork) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited |
| §6.7 Search Over Mechanisms ⧉ assurance | High | not implemented | post-v1. Supplied by a `assurance` plugin (a High-assurance service) through the harness's plugin interface (design §18.11): the contract is defined, verified at load, budgeted, audited and pinned, and no plugin ships with the har |

## Hard closed: implemented and closed

| Requirement | Profile | Verification | Closed on | Evidence |
|---|---|---|---|---|
| §3.2 Contract (Input Schema & Descriptions) | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: ToolRegistry, ToolRuntime; design: §9 |
| §3.2 Feedback (Model-Usable Errors) | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: ToolRegistry, ToolRuntime; design: §9.7 |
| §3.2 Mistake-Proofing (Poka-yoke) | Core | graded | 2026-10-21 | gap review 2026-10-21: absolute-path rule with suggested fix; components: ToolRegistry, ToolRuntime; design: §9 |
| §3.2 Model-Usable Exposure | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: ToolRegistry, ToolRuntime; design: §9 |
| §3.2 Observation (High-Signal Context) | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: ToolRegistry, ToolRuntime; design: §9.4 |
| §3.3 Bounded Looping | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Budget Enforcement Mode | Core | verified | 2026-10-21 | impl/python/tests/test_owner_decisions.py::test_the_warning_fraction_is_configurable_globally_and_per_dimension_and_validated; impl/python/tests/test_owner_decisions.py::test_a_configured_warning_frac |
| §3.3 Completion vs. Termination | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Evidence-Based Completion | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 External Stopping Control | Core | graded | 2026-10-21 | gap review 2026-10-21: pause/kill; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Legible Failure and Deterministic Paths | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Mechanisms Over Prose (Deterministic Enforcement) | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Observable Verification | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Safety Over Task Completion | Core | graded | 2026-10-21 | gap review 2026-10-21: deny precedence; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Scope of Model Judgment | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.2, §7.4 |
| §3.3 Stuck-Loop Detection | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: BudgetController, LoopController; design: §7.4.3 |
| §3.3 Unmetered Resource Safeguards | Core | verified | 2026-10-21 | impl/python/tests/test_budget_service.py::test_an_unmetered_dimension_is_declared_never_zero_and_needs_independent_safeguards; impl/python/tests/test_budget_service.py::test_a_runaway_process_is_kille |
| §3.4 External Evidence Loops | Core | graded | 2026-10-21 | gap review 2026-10-21: command verifier; components: EvalRunner (evaluators); design: §13.4 |
| §3.4 Separation of Generation and Evaluation | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner (evaluators); design: §13.4 |
| §3.6 Configuration Versioning | Core | verified | 2026-10-21 | impl/python/tests/test_owner_decisions.py::test_configuration_is_pinned_to_a_git_commit_and_marked_when_dirty; impl/python/tests/test_owner_decisions.py::test_a_run_records_the_git_commit_and_prefligh |
| §3.6 Harness as a Behavioral Component | Core | graded | 2026-10-21 | gap review 2026-10-21: config commit pinned per run; reports compare only runs with the same commit, models, task and grader; components: EvalRunner, ConfigRepo; design: §13, §16 |
| §3.6 Run Provenance Manifest | Core | verified | 2026-10-21 | impl/python/tests/test_manifest_pinning.py::test_every_run_has_a_complete_manifest_captured_at_start_with_honest_statuses; impl/python/tests/test_manifest_pinning.py::test_the_required_field_policy_re |
| §3.7 Failure Classification Taxonomy | Core | verified | 2026-10-21 | impl/python/tests/test_failure_taxonomy.py; impl/typescript/test/owner-decisions.test.ts (taxonomy); spec/vectors/failure-taxonomy.json |
| §3.7 Minimum Operator Metrics | Core | verified | 2026-10-21 | impl/python/tests/test_telemetry.py::test_metrics_trajectory_usage_and_categories_match_the_shared_vectors; impl/python/tests/test_telemetry.py::test_the_required_metrics_are_all_reported_with_the_rig |
| §3.7 Trace Completeness | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: Observability, FailureClassifier; design: §5, §15.1 |
| §4.1 Trusted Execution (Predefined Tools) | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: SandboxManager; design: §10 |
| §4.2 Model Calls Through the Control Plane | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CredentialBroker; design: §12.2 |
| §4.3 Data Provenance Tracking | Core | graded | 2026-10-21 | gap review 2026-10-21: trust tags; components: PolicyEngine, ApprovalService; design: §11.5 |
| §4.3 Untrusted Observations & Prompt Injection | Core | graded | 2026-10-21 | gap review 2026-10-21: wrapping; policy enforcement is separate; components: PolicyEngine, ApprovalService; design: §10.6, §11 |
| §5 Accounting-Preserving Recovery | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EventWriter, RecoveryManager, SnapshotService; design: §5.9 |
| §5 Event Log Consistency | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EventWriter, RecoveryManager, SnapshotService; design: §5.3 |
| §5 Single Authoritative Audit Trail | Core | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EventWriter, RecoveryManager, SnapshotService; design: §5 |
| §3.2 Affordance (Capability Scoping) | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: ToolRegistry, ToolRuntime; design: §9.10 |
| §3.2 Tool Exposure Policy | Standard | graded | 2026-10-21 | gap review 2026-10-21: allowlists; components: ToolRegistry, ToolRuntime; design: §9.3 |
| §3.2 Tool-Specific Retry Policies & Idempotency | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: ToolRegistry, ToolRuntime; design: §9.1, §9.8 |
| §3.3 Budget Reservation and Reconciliation | Standard | verified | 2026-10-21 | impl/python/tests/test_budget_service.py::test_a_reservation_holds_headroom_and_commit_replaces_the_estimate_with_actual_usage; impl/python/tests/test_budget_service.py::test_a_reservation_that_would_ |
| §3.3 Hierarchical Budgets | Standard | verified | 2026-10-21 | impl/python/tests/test_budget_service.py::test_an_allocation_is_guaranteed_to_its_agent_and_never_exceeds_what_the_workflow_has_left; impl/python/tests/test_budget_service.py::test_total_consumption_o |
| §3.6 Agent vs. Evaluation Harness Separation | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner, ConfigRepo; design: §13, §16 |
| §3.6 Bounded Submission Loops | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner, ConfigRepo; design: §13.3, §13.5 |
| §3.6 Confirmatory Final Evaluation | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner, ConfigRepo; design: §13.6 |
| §3.6 Declarative Task Package | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner, ConfigRepo; design: §13.1 |
| §3.6 Fresh Evaluator Environment | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner, ConfigRepo; design: §13.3 |
| §3.6 Gated Change Promotion | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: EvalRunner, ConfigRepo; design: §13, §16 |
| §3.6 Optimizer-Produced Artifacts Are Versioned Config | Standard | graded | 2026-10-21 | gap review 2026-10-21: candidates are git commits; components: EvalRunner, ConfigRepo; design: §13, §16 |
| §3.7 Evaluation-Integrity Errors | Standard | verified | 2026-10-21 | impl/python/tests/test_failure_taxonomy.py; impl/python/tests/test_owner_decisions.py::test_the_byte_cap_on_feedback_is_enforced_and_recorded_as_an_integrity_signal; impl/typescript/test/owner-decisio |
| §3.7 Self-Modification Errors | Standard | verified | 2026-10-21 | impl/python/tests/test_failure_taxonomy.py; impl/python/tests/test_selfimprove_campaign.py; impl/typescript/test/owner-decisions.test.ts (self-modification); impl/typescript/test/selfimprove.campaign. |
| §3.7 Taxonomy Extensibility | Standard | graded | 2026-10-21 | gap review 2026-10-21: YAML rule table; components: Observability, FailureClassifier; design: §15.5 |
| §3.8 Hook Layer | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: MiddlewarePipeline, ModelGateway; design: §8.4 |
| §3.8 Ordering, Observability, and Fail-Closed Behavior | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: MiddlewarePipeline, ModelGateway; design: §8 |
| §3.9 Composability Verification | Standard | graded | 2026-10-21 | gap review 2026-10-21: the paper's theorems (Def. 63, Lemma 59, Thm. 64, 68-71, 73, 80) checked on every step of 2,000+ random runs in both languages across three scenario families, mutation checks, t |
| §3.9 Confinement | Standard | graded | 2026-10-21 | gap review 2026-10-21: undeclared reads/writes refused and recorded once (UNDECLARED_ACCESS vs INACTIVE_ACCESS as in Cordis), the declared set is reviewed at load time through a review hook, and polic |
| §3.9 Declarative Reconciliation | Standard | graded | 2026-10-21 | gap review 2026-10-21: Entry/Loader: id/url rebuild, isolate and intercept in place, config handed to the component, disabled, groups by keyed diff; components: ComponentRuntime; design: §3.5.8 |
| §3.9 Declared Dependencies | Standard | graded | 2026-10-21 | gap review 2026-10-21: namespaced keys, version ranges, disjoint provisions unless isolated, cycle rejection at registration; components: ComponentRuntime; design: §3.5.5 |
| §3.9 Independence Check Before Out-of-Order Revert | Standard | graded | 2026-10-21 | gap review 2026-10-21: per paper Theorem 47 (disjoint keys, shared keys commutative); the git promotion planner is a separate check; components: ComponentRuntime; design: §3.5.6 |
| §3.9 Isolation and Interception | Standard | graded | 2026-10-21 | gap review 2026-10-21: derived scopes: realm resolution and metadata interception without reloading or rewiring; components: ComponentRuntime; design: §3.5.7 |
| §3.9 Lifecycle Orchestration | Standard | graded | 2026-10-21 | gap review 2026-10-21: the orchestrator can only insert, retire and remove; lifecycle rules do the rest; components: ComponentRuntime; design: §3.5.5, §3.5.8 |
| §3.9 Reactive Activation | Standard | graded | 2026-10-21 | gap review 2026-10-21: notify classification, guard so a binding is withdrawn only after dependents finish, binding held while an activation runs; components: ComponentRuntime; design: §3.5.5 |
| §4.3 Approval vs. Containment | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: PolicyEngine, ApprovalService; design: §10.2, §11.4 |
| §4.3 Default Autonomy Posture | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: PolicyEngine, ApprovalService; design: §11.3 |
| §4.3 Scoped Standing Approvals | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: PolicyEngine, ApprovalService; design: §11.4 |
| §4.3 Tool Effect Classes | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: PolicyEngine, ApprovalService; design: §9.1, §11.3 |
| §4.3 Trust Escalation Prevention | Standard | verified | 2026-10-21 | impl/python/tests/test_owner_decisions.py::test_agent_written_skills_are_untrusted_and_need_approval_while_working_state_is_untrusted_on_read_back; impl/python/tests/test_owner_decisions.py::test_skil |
| §6.1 Escalating targets, escalating gates | Standard | verified | 2026-10-21 | impl/python/tests/test_selfimprove_units.py::test_policy_defaults_are_the_ones_the_deployment_owner_chose; impl/python/tests/test_selfimprove_units.py::test_policy_refuses_typos_out_of_range_values_an |
| §6.1 Evidence-grounded edits | Standard | graded | 2026-10-21 | gap review 2026-10-21: change manifests; components: CampaignController, Gate, PromotionService; design: §17.6 |
| §6.1 Improve the harness, never the evaluator | Standard | graded | 2026-10-21 | gap review 2026-10-21: PathPolicy, T4 screens, integrity checks; components: CampaignController, Gate, PromotionService; design: §17.1, §17.2 |
| §6.11 Audit Trail | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.14 |
| §6.11 Failure Classes | Standard | verified | 2026-10-21 | impl/python/tests/test_failure_taxonomy.py; impl/python/tests/test_selfimprove_campaign.py; impl/typescript/test/owner-decisions.test.ts (self-modification); impl/typescript/test/selfimprove.campaign. |
| §6.11 Risk-Tiered Human Review | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.14 |
| §6.2 Component registry | Standard | graded | 2026-10-21 | gap review 2026-10-21: Versioned registry with a declared tier per component; components: CampaignController, Gate, PromotionService; design: §17.2.2 |
| §6.2 Edit interface | Standard | graded | 2026-10-21 | gap review 2026-10-21: EditApplier: replace, create, and delete in git worktrees, with symlink and size checks; components: CampaignController, Gate, PromotionService; design: §17.6 |
| §6.2 Immutable control and evaluation surface | Standard | graded | 2026-10-21 | gap review 2026-10-21: T4 paths are protected; integrity of the improver and control repositories is checked before every gate; components: CampaignController, Gate, PromotionService; design: §17.2.3 |
| §6.2 Recursion bounds and human decision points | Standard | graded | 2026-10-21 | gap review 2026-10-21: Recursion depth cap; approval at T2 and sampled review at T1; components: CampaignController, Gate, PromotionService; design: §17.11, §17.14 |
| §6.2 Risk tiers | Standard | graded | 2026-10-21 | gap review 2026-10-21: T0 to T4 tiers declared in the component registry and enforced by the path policy; components: CampaignController, Gate, PromotionService; design: §17.2.4 |
| §6.3 All proposals are scored | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.7, §17.8 |
| §6.3 Improvement loop budgets | Standard | graded | 2026-10-21 | gap review 2026-10-21: Campaign budgets for proposals, evaluations, cost, and time, and a plateau stop; components: CampaignController, Gate, PromotionService; design: §17.3.3 |
| §6.3 Multi-objective acceptance | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.8.2, §17.9 |
| §6.3 Non-inferiority acceptance | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.8.2 |
| §6.3 Per-target-model validation | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.8.2, §17.13 |
| §6.3 Selection and search strategy | Standard | graded | 2026-10-21 | gap review 2026-10-21: Archive, novelty rejection, parent weighting, Pareto front; components: CampaignController, Gate, PromotionService; design: §17.9 |
| §6.3 Staged promotion and rollback | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.10 |
| §6.3 Validation and promotion | Standard | graded | 2026-10-21 | gap review 2026-10-21: The nine-step gate and the T0/T1/T2 promotion path with canary and rollback planning; components: CampaignController, Gate, PromotionService; design: §17.7, §17.10 |
| §6.4 Question sets and split discipline | Standard | graded | 2026-10-21 | gap review 2026-10-21: EvalSetRegistry: disjoint roles, near-duplicate rejection, held-out query cap; components: CampaignController, Gate, PromotionService; design: §17.4 |
| §6.4 Repeatedly queried scorers | Standard | graded | 2026-10-21 | gap review 2026-10-21: Held-out cap and exhaustion, Holm adjustment, confirmation on separate data; components: CampaignController, Gate, PromotionService; design: §17.4 |
| §6.4 Reward-hacking and over-optimism defenses | Standard | graded | 2026-10-21 | gap review 2026-10-21: Static screens, hashed answer index, Holm adjustment, held-out stage; components: CampaignController, Gate, PromotionService; design: §17.7, §17.2.3 |
| §6.7 File-Based Representation | Standard | graded | 2026-10-21 | gap review 2026-10-21: graded as implemented; components: CampaignController, Gate, PromotionService; design: §17.12.3 |
| §7 Failure Taxonomy Extension | Standard | verified | 2026-10-21 | impl/python/tests/test_failure_taxonomy.py::test_an_agent_developer_defines_application_classes_in_a_namespaced_layer_and_core_rules_still_win; impl/typescript/test/owner-decisions.test.ts (extension  |

## Reopened hard closes (permanent record)

| Requirement | Date | By | Reason |
|---|---|---|---|
| Agent Legibility | 2026-10-21 | implementation session 2026-10-21 | The initial hard close was a grading error: it rested on the note 'shaped feedback', which is a different requirement. Neither language has any legibility check (no `task validate --legibility`, no discoverable-environment tooling), which the design (§13.11) and the PRD require. |
