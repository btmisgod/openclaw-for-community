# Newsflow Cutover Cleanup Bundle

- project: newsloop-cutover-0822
- cycle1 run: f5499af24248
- cycle2 run: 3532ccc60622
- redo validation project: newsloop-cutover-redo-0822a
- redo validation run: 8f5beadb4a73

Contents:
- workflow/app: current workflow code snapshot
- workflow/systemd: active services
- runs/: current output artifacts
- logs/systemd/: orchestrator + agent journals
- db_exports/: workflow_runs/tasks/agent_acks/manager_control_events/material_review_items/cycle_task_plans
