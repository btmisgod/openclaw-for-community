# 2026-04-11 cycle.start.plan schema stability

## Scope
- Only touched `workflow/app/workflow.py`
- Only touched `workflow/app/content_layer.py`
- Did not change collect/review/publish/retro business flow

## Goal
- Make `cycle.start.plan` machine-readable output stable enough for server consumption
- Require usable structure, not just parseable JSON
- Fail closed after one LLM correction retry instead of letting malformed plan data leak into the main path

## Changes
1. Added `_validate_cycle_task_plan_schema(plan_json)` in `workflow.py`
   - `section_material_requirements` must cover all 4 sections
   - each section must include `owner`, `candidate_target`, `min_approved`, `min_with_images`
   - `publication_requirements.slot_counts` and `publication_requirements.image_limits` must exist and be dicts
   - `phase_assignments` and `phase_acceptance` must be dicts

2. Added cycle-start schema repair flow in `start_cycle()`
   - first run normal `cycle.start.plan`
   - if schema validation / normalization fails, send prior plan JSON plus schema error back to LLM for repair
   - one correction retry only
   - if still invalid, task fails and does not enter the main path

3. Hardened `_normalize_cycle_task_plan()`
   - non-dict `phase_assignments` becomes `{}`
   - non-dict `phase_acceptance` becomes `{}`
   - prevents `_write_cycle_task_plan_files()` from crashing on `.items()`

4. Tightened prompt contract in `content_layer.py`
   - `cycle.start.plan` explicitly requires `publication_requirements.slot_counts` and `image_limits`
   - explicitly requires `phase_assignments` and `phase_acceptance` to be JSON objects/dicts
   - added `cycle_start_repair_request()` for correction retry

## Minimal validation
1. Static compile passed
   - `python3 -m py_compile .../content_layer.py .../workflow.py`

2. Local schema check passed
   - raw plan with list-shaped `phase_assignments` now fails validation
   - normalized plan coerces those fields to `{}` and no longer crashes on `.items()`

3. Live validation
   - synced new `workflow.py` and `content_layer.py` to `/opt/newsflow-mvp/app/`
   - restarted `newsflow-orchestrator.service` and `newsflow-agent-33.service`
   - fresh project: `cycle_start_schema_fix_20260411_2120`
   - fresh run: `fbf13a483e4b`
   - result: `cycle.start` completed successfully and run moved into `material.collect`
   - this verifies the current path no longer crashes on malformed `phase_assignments`

## Residual note
- The repair branch is implemented and guarded, but this validation run succeeded on the first `cycle.start.plan` response, so the live sample did not need to exercise the correction retry path.
