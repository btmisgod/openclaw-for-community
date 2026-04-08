from __future__ import annotations

import logging
import threading
import time
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .db import fetch_all, fetch_one
from .rendering import (
    get_run_meta,
    render_conversation_html,
    render_material_review_html,
    render_materials_html,
    render_proofread_detail_html,
    render_recheck_html,
    render_draft_review_html,
    render_final_report_html,
    render_product_report_html,
    render_single_product_report_html,
    render_project_overview_html,
    render_retrospective_html,
    render_review_thread_html,
)
from .workflow import (
    append_human_guidance,
    create_db_if_needed,
    create_project,
    new_run,
    orchestrator_tick,
    process_llm_jobs,
    pause_project,
    project_tick,
    resume_failed_run,
    resume_from_stage,
    resume_project,
    stop_project,
)


app = FastAPI(title="newsflow-mvp-orchestrator")
RUN_OUTPUT_ROOT = Path("/opt/newsflow-mvp/output")
LOGGER = logging.getLogger("newsflow.orchestrator")


class ProjectCreateRequest(BaseModel):
    project_id: str | None = None
    max_cycles: int | None = None
    max_consecutive_failures: int | None = None
    discussion_seconds: int | None = None
    retrospective_seconds: int | None = None
    next_cycle_delay_seconds: int | None = None


class GuidanceRequest(BaseModel):
    body: str
    category: str = "project"
    agent_id: str | None = None
    effective_from_cycle: int | None = None
    expires_after_cycle: int | None = None
    author: str = "human"
    source: str = "api"
    details: dict | None = None


@app.on_event("startup")
def startup():
    create_db_if_needed()

    def llm_loop():
        while True:
            try:
                process_llm_jobs()
            except Exception:
                LOGGER.exception("llm loop failed")
            time.sleep(1)

    def workflow_loop():
        while True:
            try:
                orchestrator_tick()
                project_tick()
            except Exception:
                LOGGER.exception("workflow loop failed")
            time.sleep(1)

    threading.Thread(target=llm_loop, daemon=True).start()
    threading.Thread(target=workflow_loop, daemon=True).start()


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/runs")
def create_run():
    run_id = new_run()
    return {"workflow_id": "intl-news-hotspots", "run_id": run_id}


@app.post("/projects")
def create_project_api(req: ProjectCreateRequest):
    return create_project(
        project_id=req.project_id,
        max_cycles=req.max_cycles,
        max_consecutive_failures=req.max_consecutive_failures,
        discussion_seconds=req.discussion_seconds,
        retrospective_seconds=req.retrospective_seconds,
        next_cycle_delay_seconds=req.next_cycle_delay_seconds,
    )


@app.post("/projects/{project_id}/pause")
def pause_project_api(project_id: str):
    pause_project(project_id)
    return {"project_id": project_id, "status": "paused"}


@app.post("/projects/{project_id}/resume")
def resume_project_api(project_id: str):
    resume_project(project_id)
    return {"project_id": project_id, "status": "running"}


@app.post("/projects/{project_id}/stop")
def stop_project_api(project_id: str):
    stop_project(project_id)
    return {"project_id": project_id, "status": "stopped"}


@app.post("/runs/{run_id}/resume")
def resume_failed_run_api(run_id: str):
    return resume_failed_run(run_id)


@app.post("/runs/{run_id}/resume/{stage_name}")
def resume_from_stage_api(run_id: str, stage_name: str):
    return resume_from_stage(run_id, stage_name)


@app.post("/projects/{project_id}/guidance")
def add_project_guidance(project_id: str, req: GuidanceRequest):
    return append_human_guidance(
        project_id,
        body=req.body,
        category=req.category,
        agent_id=req.agent_id,
        effective_from_cycle=req.effective_from_cycle,
        expires_after_cycle=req.expires_after_cycle,
        author=req.author,
        source=req.source,
        details=req.details,
    )


@app.get("/projects")
def list_projects():
    rows = fetch_all(
        """
        SELECT project_id, status, current_cycle_no, max_cycles, latest_run_id, next_cycle_at, paused_reason, created_at
        FROM projects
        ORDER BY created_at DESC
        """
    )
    return {"projects": rows}


@app.get("/projects/{project_id}")
def get_project(project_id: str):
    project = fetch_one(
        """
        SELECT project_id, workflow_id, status, current_cycle_no, max_cycles,
               max_consecutive_failures, consecutive_failures, discussion_seconds,
               retrospective_seconds, next_cycle_delay_seconds, latest_run_id, next_cycle_at,
               paused_reason, created_at, updated_at, notes::text
        FROM projects WHERE project_id=%s
        """,
        (project_id,),
    )
    cycles = fetch_all(
        """
        SELECT cycle_no, run_id, status, started_at, completed_at, retrospective_started_at,
               retrospective_completed_at, next_cycle_at, retrospective_summary
        FROM project_cycles WHERE project_id=%s ORDER BY cycle_no
        """,
        (project_id,),
    )
    return {"project": project, "cycles": cycles}


@app.get("/runs/{run_id}")
def get_run(run_id: str):
    run = fetch_one(
        """
        SELECT workflow_id, run_id, project_id, cycle_no, status, discussion_seconds, current_phase, started_at, completed_at, report_markdown_path, report_json_path
        FROM workflow_runs WHERE run_id=%s
        """,
        (run_id,),
    )
    tasks = fetch_all(
        """
        SELECT task_id, project_id, cycle_no, agent_id, agent_role, section, phase, retry_count, status, error_message, created_at, started_at, finished_at
        FROM tasks WHERE run_id=%s ORDER BY created_at
        """,
        (run_id,),
    )
    return {"run": run, "tasks": tasks}


@app.get("/runs/{run_id}/conversation.html", response_class=HTMLResponse)
def get_run_conversation(run_id: str):
    return render_conversation_html(run_id)


@app.get("/runs/{run_id}/review-thread.html", response_class=HTMLResponse)
def get_run_review_thread(run_id: str):
    return render_review_thread_html(run_id)


@app.get("/runs/{run_id}/draft-review.html", response_class=HTMLResponse)
def get_run_draft_review(run_id: str):
    return render_draft_review_html(run_id)


@app.get("/runs/{run_id}/materials.html", response_class=HTMLResponse)
def get_run_materials(run_id: str):
    return render_materials_html(run_id)


@app.get("/runs/{run_id}/material-review.html", response_class=HTMLResponse)
def get_run_material_review(run_id: str):
    return render_material_review_html(run_id)


@app.get("/runs/{run_id}/proofread.html", response_class=HTMLResponse)
def get_run_proofread_detail(run_id: str):
    return render_proofread_detail_html(run_id)


@app.get("/runs/{run_id}/recheck.html", response_class=HTMLResponse)
def get_run_recheck_detail(run_id: str):
    return render_recheck_html(run_id)


@app.get("/runs/{run_id}/draft.html", response_class=HTMLResponse)
def get_run_draft_html(run_id: str):
    path = RUN_OUTPUT_ROOT / run_id / "draft_report.html"
    return path.read_text() if path.exists() else "<p>暂无 draft report。</p>"


@app.get("/runs/{run_id}/retrospective.html", response_class=HTMLResponse)
def get_run_retrospective(run_id: str):
    return render_retrospective_html(run_id)


@app.get("/runs/{run_id}/final.html", response_class=HTMLResponse)
def get_run_final_html(run_id: str):
    return render_final_report_html(run_id)


@app.get("/runs/{run_id}/product.html", response_class=HTMLResponse)
def get_run_product_html(run_id: str):
    return render_product_report_html(run_id)


@app.get("/runs/{run_id}/discussion.html", response_class=HTMLResponse)
def get_run_discussion_html(run_id: str):
    path = RUN_OUTPUT_ROOT / run_id / "draft_review_summary.html"
    return path.read_text() if path.exists() else "<p>暂无 draft review summary。</p>"


@app.get("/runs/{run_id}/revised.html", response_class=HTMLResponse)
def get_run_revised_html(run_id: str):
    path = RUN_OUTPUT_ROOT / run_id / "revised_final_report.html"
    return path.read_text() if path.exists() else "<p>暂无 revised final report。</p>"


@app.get("/runs/{run_id}/retrospective-summary.html", response_class=HTMLResponse)
def get_run_retrospective_summary_html(run_id: str):
    path = RUN_OUTPUT_ROOT / run_id / "retrospective_summary.html"
    return path.read_text() if path.exists() else "<p>暂无 retrospective summary。</p>"


@app.get("/runs/{run_id}/benchmark.html", response_class=HTMLResponse)
def get_run_benchmark_html(run_id: str):
    return render_single_product_report_html(run_id, "benchmark_report")


@app.get("/runs/{run_id}/evaluation.html", response_class=HTMLResponse)
def get_run_evaluation_html(run_id: str):
    return render_single_product_report_html(run_id, "product_evaluation_report")


@app.get("/runs/{run_id}/meta")
def get_run_meta_api(run_id: str):
    return get_run_meta(run_id)


@app.get("/projects/{project_id}/overview.html", response_class=HTMLResponse)
def get_project_overview(project_id: str):
    return render_project_overview_html(project_id)
