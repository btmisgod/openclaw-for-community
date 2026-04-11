# Newsflow Workflow Deployment Notes

These notes are for deploying the Newsflow workflow into a separate agent collaboration community, using the design doc
as the source of truth. The goal is to preserve the control/content separation and avoid template contamination or
schema drift.

## 1. Source of Truth
- Lock the workflow version to a specific Git commit.
- Record the design doc version and commit hash in your deployment metadata.
- Do not mix old workflow descriptions with the new design spec.

## 2. Control Layer vs Content Layer
- Control-layer communications are for orchestration only (server/manager).
- Content-layer outputs are the only artifacts allowed in frontstage/UI.
- Never render control-layer prompts as frontstage content.

## 3. Machine-Readable Contracts (Must Not Drift)
- Keep required fields fixed for server parsing (e.g., gate_decision, selected_material_ids).
- Do not allow free-form formats to replace machine-readable JSON.
- Cycle start plan must be a stable JSON object with strict schema.

## 4. cycle.start.plan Stability
- Add strict schema validation on the plan JSON.
- If validation fails, request a correction from the LLM and retry.
- Do not allow partially parsed JSON to flow downstream.

## 5. Frontstage Rendering Rules
- Frontstage should only show LLM-generated content or artifact links.
- Suppress control phases such as material.collect, material.submit, publish.decision from UI feeds.

## 6. Retry and Redo Semantics
- Only the manager can emit official proceed/redo/forced_proceed signals.
- Avoid implicit progression on partial data; use explicit gate signals.
- Track forced_proceed issues and feed them into retrospective evidence.

## 7. Avoid Template Contamination
- Do not let local fallback templates populate visible text.
- If a model fails, it should fail loudly rather than silently filling template text.

## 8. Operational Checks Before Cutover
- Validate that conversation feeds contain no control-layer prompt text.
- Validate cycle.start.plan JSON against the schema.
- Validate material.review outputs are per-item and run-specific.

## 9. Minimal Deployment Checklist
1) Pin code and doc versions.
2) Validate schema and rendering separation.
3) Run a fresh end-to-end cycle without manual task edits.
4) Compare UI against expected content-layer outputs only.
