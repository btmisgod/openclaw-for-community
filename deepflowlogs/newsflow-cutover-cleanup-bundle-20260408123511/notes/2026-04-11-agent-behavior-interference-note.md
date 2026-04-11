# 2026-04-11 Agent Behavior Interference Note

Design baseline:
- `docs/design/newsflow-agentic-workflow-spec.md`

Governance decision recorded in this note:
- do not change active agent behavior purely to make the current natural-run validation pass faster

Context:
- during the no-intervention `max_cycles=2` fresh-run validation, `material.collect` was observed to take roughly 8 to 10 minutes per section
- a candidate optimization was identified: change `material.collect` enrichment from per-item synchronous prompting to batched or whole-section prompting

Why this is being deferred:
- this is not just a runtime optimization
- it changes the effective agent working pattern for `material.collect`
- applying it in the middle of the current natural-run campaign would mix two different agent behaviors into one validation series
- that would make the current test less trustworthy as evidence of whether the present workflow can stand on its own

Recorded rule for the current campaign:
- keep the current active natural-run validation free of behavior-changing tweaks whose main purpose is to help the run pass
- only apply fixes during this campaign if they are direct blockers such as:
  - global orchestrator corruption
  - broken dispatch/gate behavior
  - provider/auth failures
  - crashes that prevent the current design from being exercised

Deferred item to revisit after the current validation completes:
- redesign `material.collect` enrichment so it no longer relies on one synchronous LLM call per candidate
- candidate directions to evaluate after the current run finishes:
  - batched enrichment with multiple candidates per request
  - whole-section generation with explicit `source_index` alignment
  - asynchronous `llm_job`-based enrichment instead of worker-blocking synchronous calls

Current status of this item:
- recorded
- explicitly deferred until the current natural-run validation is complete
