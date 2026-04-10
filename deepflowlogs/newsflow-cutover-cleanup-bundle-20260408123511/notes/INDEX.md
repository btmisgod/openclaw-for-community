# Newsflow Cutover Cleanup Bundle

- project: newsloop-cutover-0822
- cycle1 run: f5499af24248
- cycle2 run: 3532ccc60622
- redo validation project: newsloop-cutover-redo-0822a
- redo validation run: 8f5beadb4a73
- content de-template validation project: content_detempl_final_20260408_1414
- content de-template validation run: af03a1e31a38
- clean-room governance project: content_cleanroom_batch8_20260409_021042
- clean-room governance run: 889881f39e3f

Notes:
- 2026-04-09 clean-room governance log: `notes/2026-04-09-cleanroom-governance-log.md`
- 2026-04-10 product.test + retrospective.plan log: `notes/2026-04-10-product-test-retro-plan-log.md`

Contents:
- workflow/app: current workflow code snapshot
- workflow/systemd: active services
- runs/: current output artifacts
- logs/systemd/: orchestrator + agent journals
- db_exports/: workflow_runs/tasks/agent_acks/manager_control_events/material_review_items/cycle_task_plans
