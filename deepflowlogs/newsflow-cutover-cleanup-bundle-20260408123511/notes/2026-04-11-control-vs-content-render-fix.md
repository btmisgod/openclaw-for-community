# 2026-04-11 Control vs Content Render Fix

Design baseline:
- `docs/design/newsflow-agentic-workflow-spec.md`

Plan source:
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/notes/2026-04-11-control-vs-content-separation-plan.json`

Scope:
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/rendering.py`

Goal:
- stop rendering control-layer dispatch text into frontstage HTML
- keep orchestration payloads in storage, but never expose them in public feed

Changes:
1. `_dispatch_body()` was demoted to an internal no-op with an explicit control-layer comment.
2. `build_conversation_entries()` now handles `cycle.start` through a dedicated neutral public entry.
3. `cycle.start` no longer reads `cycle_task_plan` markdown into the conversation body.
4. `cycle.start` now renders as a neutral `system -> all` entry with only artifact link visibility.
5. `cycle.start` fact pairs were suppressed so public feed does not re-expand control metadata into body-like content.

Minimal validation:
1. Rendered pages from run `6e80bdd8e1d0` into `/tmp/newsflow-render-check/` using the workflow runtime venv.
2. Searched rendered files:
   - `conversation.html`
   - `material-review.html`
   - `proofread.html`
   - `recheck.html`
3. Grep phrase set:
   - `请审`
   - `请提交`
   - `请以 manager 身份`
4. Result: `0` hits.
5. Grep for `_dispatch_body()` public call sites in `rendering.py` excluding the function definition.
6. Result: `0` call sites.
7. Manually inspected rendered `conversation.html`.
8. Result: `cycle.start` appears as `system -> all` with artifact link only and empty body.

Notes:
- no workflow schema changes
- no control payload storage changes
- no content-layer prompt changes
- no live workflow test run in this fix
