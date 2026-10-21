"""Train 2 governance core (design §17): the editable surface and its enforcement, screens, the gate, promotion and campaigns.

Everything here is deterministic control-plane machinery. The model-driven parts (analysis agents, LLM proposers) plug in through
small interfaces (`ProposalSource`, `ObservationProvider`) and are *not* implemented."""
