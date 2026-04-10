# 2026-04-10 Product Test + Retro Plan Governance Log

## Scope

This round only fixed two residual content-layer problems confirmed in the previous review:

1. `product.test` still forced tester output into questionnaire-style responsibility attribution.
2. `retrospective.plan` still carried hard quantity rules and hard slicing.

No additional work was done on ACK concurrency, proofread gate, orchestrator side paths, or other infrastructure issues.

## Why these changes were made

### 1. product.test

The active content path still required `execution_link`, `most_obvious_problems`, and `priority_improvements`.
That forced tester output back into a blame-style questionnaire instead of a unified reader-side product review.

The fix was:

- prompt the tester from a reader/product-experience viewpoint
- replace the required machine-readable fields with:
  - `reader_findings`
  - `reader_improvement_opportunities`
- keep front-visible prose in `report_markdown`
- remove `execution_link` from required active output validation
- keep backward-reading compatibility for older stored reports only

### 2. retrospective.plan

The active path still had:

- prompt text requiring “at least 2 topics”
- workflow slicing:
  - `product_problems[:5]`
  - `behavior_problems[:3]`
  - `topics[:5]`

That could force topic padding when evidence was thin and silently truncate valid topics when evidence was richer.

The fix was:

- change the prompt contract to require only `topics` non-empty
- remove hard slicing from plan persistence
- remove hard slicing from `_retrospective_plan_topics()`
- keep the single required gate: `topics` must contain at least 1 topic
- pass the full topic list into retrospective opening context instead of truncating it to 4 items

## Files changed

- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/content_layer.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/workflow.py`

## Validation runs

### Natural clean project started first

- `project_id=content_cleanroom_batch9_20260410_125719`
- `run_id=b6f960ade5c2`

This run naturally started from `cycle.start` and reached `material.collect`, then was intentionally stopped because this round only needed downstream validation for `product.test` and `retrospective.plan`.

### Downstream clean validation run used for evidence

- `project_id=content_product_retro_batch10_20260410_130214`
- `run_id=1d7e3ae5d3ad`
- seeded from existing final artifact: `fad6777af5d0`

This validation run did not hand-fill any front-visible prose.
It copied an existing `final_artifact` as the stage input object, then executed new-code tasks on the new run:

- `product.test`
- `product.benchmark`
- `product.cross_cycle_compare`
- `retrospective.plan`
- `retrospective.discussion`

## Validation results

### product.test

Evidence from `run_id=1d7e3ae5d3ad`:

- `product_reports.report_json` contains:
  - `reader_findings`
  - `reader_improvement_opportunities`
- `product_reports.report_json` does **not** contain:
  - `execution_link`
- `generation_mode=llm`

Observed front-visible report characteristics:

- talks about reader-side breadth, readability, information density, classification precision, title attraction
- does not contain “谁负责/执行关联/责任归因” style questionnaire prose
- product page renders the generated markdown directly

### retrospective.plan

Evidence from `run_id=1d7e3ae5d3ad`:

- `generation_mode=llm`
- stored counts were:
  - `product_problems=3`
  - `behavior_problems=2`
  - `topics=2`
- no hard slicing logic remained on the active path

Observed behavior:

- the plan kept exactly the counts generated from the current evidence
- it was not forced up to a fixed minimum > 1
- it was not truncated down to a hard maximum on persistence
- the generated `topics` flowed into `retrospective.discussion`, which opened successfully on the first topic

### server-readable fields still intact

Observed machine-readable evidence on `run_id=1d7e3ae5d3ad`:

- `product_test_keys = (reader_findings=True, reader_improvement_opportunities=True, execution_link=False, generation_mode='llm')`
- `retro_plan_counts = (3, 2, 2, 'llm')`
- retrospective thread rows still preserved:
  - `topic_id`
  - `message_id`
  - `from_agent`
  - `to_agent`
  - `intent`
- agent ACK rows still existed for:
  - `product.test`
  - `product.benchmark`
  - `product.cross_cycle_compare`
  - `retrospective.discussion`

## Remaining high-priority content-layer issues

- `material.review` still needs a fresh clean run proving no residual fallback-style review language remains.
- `draft.proofread` / `draft.revise` still need the same level of clean-room evidence as `product.test`.
- `report.publish` / final delivery still need a new clean downstream run proving the current renderer path against freshly generated content rather than historical stored markdown.
