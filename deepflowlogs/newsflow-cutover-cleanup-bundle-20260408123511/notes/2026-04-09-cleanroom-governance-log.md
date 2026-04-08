# 2026-04-09 Clean-Room Governance Log

## Scope

This round kept the control layer intact and continued the clean-room removal of local front-visible template generation.

Uploaded source files in this round:

- `docs/design/newsflow-agentic-workflow-spec.md`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/content_layer.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/db.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/llm.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/rendering.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/workflow.py`

## Main clean-room changes

### 1. Front-visible page shell removal

Removed local display shells from active pages:

- `materials.html`: removed `brief=` and `why_selected=` labels
- `material-review.html`: removed `candidate_note=` and `review=` labels
- `proofread.html` / `recheck.html`: removed `reported_by=` / `decision=` / `severity=` / `closed_at=` labels
- product report HTML writer: removed local payload dump (`<h3>` / `<pre>`) from front pages
- `final.html`: removed local `run_id / project_id / cycle_no` header and `Structured Main Items`
- `retrospective.html`: removed `Retro Topics`, `evidence_refs`, and optimization log control metadata from the front page

### 2. Retro / optimization evidence de-template

Removed local default review-signal injection from `workflow.py`:

- `_review_signal()` now returns only real tester review text
- retrospective discussion and self-optimize evidence no longer receive default section complaints when real review text is absent

### 3. LLM-only content path preservation

The active content path remains:

- `content_layer.*_request` defines stage objects and visible fields
- `workflow._run_content_request()` calls the LLM
- `workflow._require_llm_visible_text()` rejects missing visible text
- `workflow._reject_template_shell()` blocks known legacy report shells

## Validation runs referenced

### Current clean validation project

- `project_id=content_cleanroom_batch8_20260409_021042`
- `run_id=889881f39e3f`

Status at upload time:

- run still naturally progressing
- currently paused by upstream `material.review` latency
- no manual state pushing was used on this run

### Regression-check pages used after live sync

- `run_id=fad6777af5d0`

Used to confirm that live pages no longer expose:

- `candidate_note=`
- `review=`
- `reported_by=`
- `decision=`
- `Retro Topics`
- `author:` / `effective_from_cycle`
- `Structured Main Items`

## Live verification notes

Verified via live HTTP pages after sync:

- `/runs/889881f39e3f/materials.html`
- `/runs/889881f39e3f/conversation.html`
- `/runs/fad6777af5d0/material-review.html`
- `/runs/fad6777af5d0/proofread.html`
- `/runs/fad6777af5d0/product.html`
- `/runs/fad6777af5d0/retrospective.html`
- `/runs/fad6777af5d0/final.html`

## Remaining content-layer follow-up

Still needs fresh full-run evidence after `material.review` completes:

- `draft.compose`
- `draft.proofread`
- `draft.revise`
- `report.publish`
- full retrospective/optimization chain on a newly completed clean run

The goal of the next round is not more control-layer work, but fresh end-to-end evidence on the already-cleaned content path.
