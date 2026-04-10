# 2026-04-10 Fresh Run Campaign Summary

Design baseline:
- `docs/design/newsflow-agentic-workflow-spec.md`

Scope of this round:
- run a real fresh workflow from `cycle.start`
- do not seed a historical final artifact
- do not hand-edit task state
- do not hand-claim a single node to force the chain forward

Fresh-run attempts covered here:

1. `project_id=newsflow_freshrun_full_20260410_151612`
   - `run_id=49e709b22453`
2. `project_id=newsflow_freshrun_2cycles_clean_20260410_181027`
   - `run_id=26af0780b1c5`

## How far the workflow got

### Run `49e709b22453`

Natural progress reached:

- `cycle.start`
- `material.collect`
- `material.submit`
- `material.review` (2 sections completed, 2 still pending when this run was superseded)
- `material.review.decision` (1 completed)

Observed DB state before superseding the run:

- `material.collect`: 4 completed
- `material.submit`: 4 completed
- `material.review`: 2 completed, 2 pending
- `materials`: 50 rows
- completed section reviews:
  - `体育娱乐`
  - `政治经济`

### Run `26af0780b1c5`

Natural progress reached:

- `cycle.start`
- `material.collect`
- `material.submit`
- `material.review`
- `material.review.decision` (3 completed before the project was stopped on operator request)

Observed DB state when the run was stopped:

- `material.collect`: 4 completed
- `material.submit`: 4 completed
- `material.review`: 4 completed
- `material.review.decision`: 3 completed
- `materials`: 50 rows

This run was stopped after the user explicitly asked to pause the current work and inspect the five OpenClaw agent folders, so it did not naturally continue into `draft.compose`.

## Problems encountered

### 1. DeepFlow/Grafana board looked empty even though the run had started

Root cause:

- the restored 19-panel dashboard expects both `project_id` and `run_id`
- the earlier shared URL only carried `run_id`
- with a stale/default `project_id`, panels looked empty even though the workflow had already written tasks

Fix:

- restored the 19-panel dashboard variant
- switched to links that always pass both `var-project_id` and `var-run_id`

### 2. `material.review` throughput was too slow for full-chain fresh-run validation

Root cause:

- the active review path was still sending one material object per LLM review request
- prompt payloads were too large because review inputs carried long summaries and notes
- the node timeout was too short for fresh-run traffic

Fix:

- increased `LLM_NODE_CONFIG["material.review"].timeout_ms` from `120000` to `180000`
- trimmed `summary_zh`, `brief_zh`, and `relevance_note` before sending them into the review prompt
- raised review batching from `1` item to `3` items per request

### 3. `draft.compose.translation` could stall on full-section translation

Root cause:

- the active translation path was still trying to translate the whole ranked item list in one shot
- that made compose sensitive to a single oversized section prompt

Fix:

- changed `_translate_ranked_items()` to batch translation requests in groups of 4
- added single-item recovery for missing indexes
- kept the same `generation_mode=llm` requirement and did not restore any visible local fallback

### 4. `material.collect.enrichment` could return partial item sets

Root cause:

- some LLM enrichment responses did not return a usable item for every source index

Fix:

- added missing-index recovery in `_enrich_collected_materials()`
- reruns only the missing candidate as a single-item request
- still hard-fails if visible candidate content is not LLM-generated

### 5. `draft.render` and `draft.proofread` were still timeout-prone

Root cause:

- fresh-run content is longer than the earlier downstream probes
- render/proofread nodes still used smaller timeout budgets tuned for lighter traffic

Fix:

- `draft.render`
  - timeout `150000 -> 300000`
  - max completion tokens `1400 -> 2200`
- `draft.proofread`
  - node timeout `120000 -> 240000`
  - section request timeout `150000 -> 240000`
  - rollup request timeout `90000 -> 120000`
  - section/rollup request attempts `1 -> 2`

### 6. OpenClaw community webhook wrappers are currently restart-looping

Current observed state:

- `openclaw-community-webhook-openclaw-33-editor.service`
- `openclaw-community-webhook-openclaw-33-tester.service`
- `openclaw-community-webhook-openclaw-33-worker-xhs.service`

Current error:

- `Request failed for /agents: agent name already exists`

Important scope note:

- the fresh workflow tasks above were executed by the `newsflow-agent-*` services
- the OpenClaw webhook wrappers are a separate integration layer
- they are relevant to operator-facing ingress health, but they were not the reason the already-started fresh runs wrote `material.collect` / `material.review` rows

### 7. Bootstrap defaults could overwrite explicitly provided agent environment

Root cause:

- the bootstrap script sourced the config file wholesale
- explicit environment values supplied by the caller could be overwritten by template defaults
- that increases the risk of agent identity drift across reinstalls or service recreation

Fix:

- changed `scripts/bootstrap-community-agent-template.sh` to load config defaults only for keys that are not already set in the environment
- this preserves explicit caller-provided agent identity and ingress values

## Files changed in this round

- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/workflow.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/content_layer.py`
- `deepflowlogs/newsflow-cutover-cleanup-bundle-20260408123511/workflow/app/rendering.py`
- `scripts/bootstrap-community-agent-template.sh`

## What is now proven

- fresh runs can start naturally from `cycle.start`
- `material.collect` can complete all 4 sections on a fresh run
- `material.review` can produce section-specific review prose on a fresh run
- no local visible fallback was reintroduced to get these stages through
- the running bundle copy and the live `/opt/newsflow-mvp/app` workflow files were re-synced before this commit

## What is not yet proven

- `draft.compose -> draft.proofread -> draft.revise -> draft.recheck`
- `publish.decision -> report.publish`
- `product.test / benchmark / cross_cycle_compare` on a truly fresh final artifact
- `retrospective.plan / discussion / summary / agent.optimization` on a truly fresh final artifact

## Current conclusion

The workflow is no longer blocked at startup.
The fresh-run chain now naturally reaches and completes `material.collect` and `material.review`.

The remaining missing evidence is the downstream half of the chain after review.
That gap is now dominated by runtime duration/throughput and downstream long-text node stability, not by reintroduced visible templates.
