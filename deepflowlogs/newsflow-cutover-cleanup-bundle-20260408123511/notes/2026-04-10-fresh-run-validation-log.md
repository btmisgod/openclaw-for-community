# 2026-04-10 Fresh Run Validation Log

Design baseline:
- `docs/design/newsflow-agentic-workflow-spec.md`

Fresh-run target:
- project_id: `content_freshrun_batch15_20260410_140153`
- run_id: `ca659a26bc04`

What this validation explicitly did not use:
- no seeded final artifact
- no manual task status edits
- no manual single-node claim
- no narrow downstream-only runner
- no minimal draft injection

Fresh-run campaign in this session:
1. `content_freshrun_batch11_20260410_133510` / `0e44dd37aebb`
   - blocked at `material.collect`
   - direct blocker: collection fetched detail-page images for every candidate before slice
2. `content_freshrun_batch12_20260410_133921` / `888c44a1d9c0`
   - blocked at `awaiting_ack`
   - direct blocker: hidden ACK path did not naturally recover, so fresh run stalled before workers could start
3. `content_freshrun_batch13_20260410_134408` / `749dca9c13f8`
   - blocked because workers kept seeing `pending` tasks from stopped runs first
   - direct blocker: `claim_task()` and `claim_ack_task()` were not filtering to active runs
4. `content_freshrun_batch14_20260410_134754` / `68597403599f`
   - naturally progressed through full `material.collect` and into `material.review`
   - then `科技` section failed in `material.collect.enrichment`
   - direct blocker: LLM returned non-numeric `candidate_rank`, local code did `int('A')`
5. `content_freshrun_batch15_20260410_140153` / `ca659a26bc04`
   - current clean sample after all above direct blockers were patched

Minimal code fixes applied only to unblock fresh-run validation:
- `workflow/app/news.py`
  - moved expensive detail-page image fetch to after sort+slice
  - reduced per-page timeout
  - prefers feed-native image extraction first
- `workflow/app/workflow.py`
  - added stalled hidden-ACK recovery for `awaiting_ack`
  - made `claim_task()` / `claim_ack_task()` ignore stopped runs/projects
  - made `material.collect.enrichment` tolerate non-numeric `candidate_rank`
- `workflow/app/content_layer.py`
  - tightened `candidate_rank` contract to integer

Fresh run `ca659a26bc04` snapshot at `2026-04-10 14:21:59 CST` and latest DB check afterward:
- `cycle.start` completed naturally
- `material.collect` completed for all 4 sections
- `material.submit` completed for all 4 sections
- `materials` rows: 50
- material generation modes:
  - `政治经济`: 13 `llm`
  - `科技`: 13 `llm`
  - `体育娱乐`: 12 `llm`
  - `其他`: 12 `llm`
- `material.review`:
  - `体育娱乐` review completed naturally
  - `material.review.decision` for `体育娱乐` completed naturally
  - `政治经济` review running
  - `其他` / `科技` review pending
- no manual recovery was used on these section reviews
- `draft_versions=0`, `proofread_issues=0`, `outputs=0`, `retrospectives=0` at the current snapshot

Fresh-run evidence files:
- `/opt/newsflow-mvp/output/ca659a26bc04/fresh_run_validation_summary.json`
- `/opt/newsflow-mvp/output/68597403599f/fresh_run_validation_summary.json`

What this fresh run already proves:
- the workflow can now naturally start from `cycle.start`
- `material.collect` front-end source objects are produced with `generation_mode=llm`
- the first `material.review` result is real run-specific prose, not a local fallback sentence
- hidden ACK and pending-task starvation no longer immediately kill the run at startup

What this fresh run does not prove yet:
- `draft.compose`
- `draft.proofread`
- `draft.revise`
- `draft.recheck`
- `publish.decision`
- `report.publish`
- `product.test`
- `product.benchmark`
- `product.cross_cycle_compare`
- `retrospective.plan`
- `retrospective.discussion`
- `retrospective.summary`
- `agent.optimization`

Current blocker classification:
- current fresh run is not blocked by a content-template fallback
- current limitation is throughput / duration on serial `material.review` work
- category: direct runtime/throughput blocker for full-chain validation, not a seeded-validation artifact

Current conclusion:
- the fresh run is now a true from-scratch sample and has crossed the earlier startup blockers
- the overall news workflow is not yet “overall passed”
- the missing evidence is still the downstream half of the chain after `material.review`
