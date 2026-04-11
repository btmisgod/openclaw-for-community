from __future__ import annotations

import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from deep_translator import GoogleTranslator

from . import content_layer
from .config import ROOT, load_settings
from .db import execute, execute_returning, fetch_all, fetch_one, get_conn, init_schema, jdump
from .llm import chat_json, chat_text
from .news import HTTP, collect_news, search_benchmark_samples
from .rendering import (
    parse_trace_id,
    render_conversation_html,
    render_draft_review_html,
    render_final_report_html,
    render_product_report_html,
    render_retrospective_html,
    render_review_thread_html,
    write_final_report_html,
    write_product_report_html,
)
from .telemetry import extract_context, inject_current_context, workflow_span


SETTINGS = load_settings()
LOGGER = logging.getLogger("newsflow.workflow")
WORKFLOW_ID = "intl-news-hotspots"
RUN_OUTPUT_DIR = ROOT / "output"
PROJECT_OUTPUT_DIR = ROOT / "projects"
TRANSLATOR = GoogleTranslator(source="auto", target="zh-CN")
MANAGER_AGENT_ID = "33"
EDITOR_AGENT_ID = "editor"
TESTER_AGENT_ID = "tester"
WORKER_33_AGENT_ID = "worker-33"
WORKER_XHS_AGENT_ID = "worker-xhs"
AGENT_SECTIONS = {
    TESTER_AGENT_ID: ["政治经济", "科技", "体育娱乐", "其他"],
    WORKER_33_AGENT_ID: ["政治经济", "科技"],
    WORKER_XHS_AGENT_ID: ["体育娱乐", "其他"],
}
AGENT_ROLES = {
    MANAGER_AGENT_ID: "manager",
    EDITOR_AGENT_ID: "editor",
    TESTER_AGENT_ID: "tester",
    WORKER_33_AGENT_ID: "worker-33",
    WORKER_XHS_AGENT_ID: "worker-xhs",
}
ALL_AGENT_IDS = [MANAGER_AGENT_ID, EDITOR_AGENT_ID, TESTER_AGENT_ID, WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID]
ALL_SECTION_ASSIGNMENTS = [
    ("政治经济", WORKER_33_AGENT_ID),
    ("科技", WORKER_33_AGENT_ID),
    ("体育娱乐", WORKER_XHS_AGENT_ID),
    ("其他", WORKER_XHS_AGENT_ID),
]
RETRO_PARTICIPANTS = [EDITOR_AGENT_ID, WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID, TESTER_AGENT_ID]
BENCHMARK_URLS = [
    ("BBC News", "https://www.bbc.com/news"),
    ("Reuters World", "https://www.reuters.com/world/"),
    ("Google News", "https://news.google.com/topstories?hl=en-US&gl=US&ceid=US:en"),
]

TASK_INITIALIZATION_OBJECTIVE = {
    "workflow_objective": {
        "name": "近24小时国际新闻热点工作流",
        "audience": "中文读者",
        "goal": "围绕近24小时国际热点产出一份可发布的中文新闻成品，并在发布后完成产品评估、复盘与下一轮优化。",
    },
    "delivery_requirements": {
        "publication_requirements": {
            "section_order": ["政治经济", "科技", "体育娱乐", "其他"],
            "slot_counts": {"main": 1, "secondary": 2, "brief": 7},
            "image_limits": {"main": 3, "secondary": 1, "brief": 0},
        },
        "material_copy_requirements": {
            "required_fields": ["title_zh", "summary_zh", "brief_zh", "relevance_note"],
            "length_guidance": {
                "title_zh": {"max_chars": 22},
                "summary_zh": {"min_chars": 32, "max_chars": 48},
                "brief_zh": {"min_chars": 14, "max_chars": 22},
                "relevance_note": {"max_chars": 14},
            },
        },
        "retrospective_requirements": {
            "minimum_product_problems": 5,
            "minimum_behavior_problems": 2,
        },
    },
    "agent_assignment_constraints": {
        "manager": MANAGER_AGENT_ID,
        "editor": EDITOR_AGENT_ID,
        "tester": TESTER_AGENT_ID,
        "section_owners": {
            "政治经济": WORKER_33_AGENT_ID,
            "科技": WORKER_33_AGENT_ID,
            "体育娱乐": WORKER_XHS_AGENT_ID,
            "其他": WORKER_XHS_AGENT_ID,
        },
    },
    "control_contract": {
        "manager_signals": ["proceed", "partial_pass", "redo", "forced_proceed", "pause", "publish_approved", "fail", "resume"],
        "server_responsibility": ["phase dispatch", "gate", "timeout/retry/recovery", "machine-readable parsing"],
    },
}

LLM_NODE_CONFIG = {
    "cycle.start.plan": {"timeout_ms": 180000, "max_attempts": 2, "backoff_ms": 4000, "critical": True, "max_completion_tokens": 2200},
    "material.collect.enrichment": {"timeout_ms": 300000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 2600},
    "material.review": {"timeout_ms": 300000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 2400},
    "draft.compose.translation": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 900},
    "draft.render": {"timeout_ms": 300000, "max_attempts": 2, "backoff_ms": 4000, "critical": True, "max_completion_tokens": 2200},
    "draft.proofread": {"timeout_ms": 360000, "max_attempts": 4, "backoff_ms": 12000, "critical": False, "max_completion_tokens": 3200},
    "proofread.decision.explanation": {"timeout_ms": 60000, "max_attempts": 2, "backoff_ms": 5000, "critical": False, "max_completion_tokens": 260},
    "draft.revise": {"timeout_ms": 300000, "max_attempts": 2, "backoff_ms": 12000, "critical": True, "max_completion_tokens": 450},
    "draft.recheck": {"timeout_ms": 210000, "max_attempts": 2, "backoff_ms": 6000, "critical": False, "max_completion_tokens": 1800},
    "product.test": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 10000, "critical": False, "max_completion_tokens": 380},
    "product.benchmark": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 1200},
    "product.cross_cycle_compare": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 1000},
    "product.report": {"timeout_ms": 140000, "max_attempts": 2, "backoff_ms": 12000, "critical": True, "max_completion_tokens": 450},
    "retro_decision": {"timeout_ms": 110000, "max_attempts": 2, "backoff_ms": 10000, "critical": False, "max_completion_tokens": 320},
    "retrospective.plan": {"timeout_ms": 140000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 1200},
    "retrospective.discussion": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 2500, "critical": False, "max_completion_tokens": 700},
    "retrospective.summary": {"timeout_ms": 140000, "max_attempts": 2, "backoff_ms": 12000, "critical": True, "max_completion_tokens": 520},
    "discussion.comment": {"timeout_ms": 90000, "max_attempts": 2, "backoff_ms": 2500, "critical": False, "max_completion_tokens": 500},
    "draft.review.comment": {"timeout_ms": 90000, "max_attempts": 2, "backoff_ms": 2500, "critical": False, "max_completion_tokens": 550},
    "discussion.summary": {"timeout_ms": 100000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 850},
    "agent.self_optimize": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 4000, "critical": False, "max_completion_tokens": 1000},
}

PROOFREAD_BLOCKER_TYPES = {"lead_sentence_rule", "fact_integrity"}
PHASES_REQUIRING_ACK = {
    "material.collect",
    "material.review",
    "draft.compose",
    "draft.proofread",
    "draft.revise",
    "draft.recheck",
    "report.publish",
    "product.test",
    "product.benchmark",
    "product.cross_cycle_compare",
    "retrospective.discussion",
}

STALE_RUNNING_PHASE_TIMEOUTS = {
    "material.review.decision": 180,
    "publish.decision": 180,
    "report.publish": 180,
    "product.benchmark": 900,
    "product.cross_cycle_compare": 900,
    "retrospective.plan": 900,
    "retrospective.discussion": 600,
    "agent.optimization": 1800,
}

MAX_DRAFT_AUDIT_FAILURES_BEFORE_REDO = 3
MAX_AUDIT_PARTIAL_PASS_BEFORE_REDO = 3
MAX_AUDIT_REDO_BEFORE_FORCE_PROCEED = 2

AUDIT_PROCEED = "proceed"
AUDIT_PARTIAL_PASS = "partial_pass"
AUDIT_REDO = "redo"
AUDIT_FORCED_PROCEED = "forced_proceed"

PHASE_ROLE_CONTEXT = {
    "cycle.start": {
        "role_identity": "manager",
        "phase_goal": "启动新 cycle，注入标准、优先级和上一轮优化建议",
        "artifact_scope": "全局项目标准和本轮目标",
        "allowed_actions": ["set priorities", "inject standards", "assign section owners"],
        "forbidden_actions": ["draft authoring", "proofread execution", "product test authoring"],
        "decision_authority": "final standards and cycle kickoff",
        "output_contract": ["cycle task plan", "section requirements", "publication requirements", "previous optimization context"],
    },
    "material.collect": {
        "role_identity": "section worker",
        "phase_goal": "收集本板块候选素材池",
        "artifact_scope": "自己负责的 section",
        "allowed_actions": ["collect materials", "source filtering", "attach image candidates"],
        "forbidden_actions": ["final hierarchy decision", "final publish", "product evaluation"],
        "decision_authority": "section candidate pool only",
        "output_contract": ["title", "source", "published_time", "original_link", "image_candidates", "short_relevance_note"],
    },
    "material.review": {
        "role_identity": "tester",
        "phase_goal": "审核候选素材是否可进入编辑阶段",
        "artifact_scope": "已提交的 section materials",
        "allowed_actions": ["approve", "reject", "request rework"],
        "forbidden_actions": ["compose draft", "publish report"],
        "decision_authority": "material usability gate",
        "output_contract": ["timeliness", "relevance", "authenticity", "text_image_consistency", "material_usability"],
    },
    "material.review.decision": {
        "role_identity": "manager",
        "phase_goal": "基于 tester 的全量审核结果做最小业务验收，决定 proceed / partial_pass / redo，必要时 forced_proceed",
        "artifact_scope": "material review result",
        "allowed_actions": ["proceed", "partial_pass", "request redo", "forced proceed", "pause"],
        "forbidden_actions": ["rewrite tester review", "compose draft"],
        "decision_authority": "partial_pass / redo / forced_proceed / proceed",
        "output_contract": ["signal_type", "reason", "required_rework", "forced_progress_issue"],
    },
    "draft.compose": {
        "role_identity": "editor",
        "phase_goal": "把已通过素材整合成初稿",
        "artifact_scope": "全稿 draft_v1",
        "allowed_actions": ["integrate approved materials", "decide hierarchy", "write draft"],
        "forbidden_actions": ["material review", "product test", "retrospective moderation"],
        "decision_authority": "draft integration and hierarchy",
        "output_contract": ["per_section_main_secondary_briefs", "metadata retained"],
    },
    "draft.proofread": {
        "role_identity": "tester",
        "phase_goal": "校对 draft 正确性，不做产品体验评价",
        "artifact_scope": "draft correctness",
        "allowed_actions": ["raise issues", "recheck fixes", "block publish when blockers remain"],
        "forbidden_actions": ["product evaluation", "draft authoring"],
        "decision_authority": "proofread blocker gate",
        "output_contract": ["concrete_issues", "affected_objects", "blocker_or_non_blocker", "recheck_requirement"],
    },
    "draft.revise": {
        "role_identity": "editor",
        "phase_goal": "根据 proofread issue 修订 draft",
        "artifact_scope": "affected draft slices",
        "allowed_actions": ["apply patches", "rewrite draft slices", "preserve structure"],
        "forbidden_actions": ["close blockers unilaterally", "product evaluation"],
        "decision_authority": "draft revision only",
        "output_contract": ["revised draft", "applied patch summary"],
    },
    "draft.recheck": {
        "role_identity": "tester",
        "phase_goal": "对修订稿逐项复查上一轮 proofread issue 是否真正解决",
        "artifact_scope": "resolved proofread issues",
        "allowed_actions": ["close issue", "reopen issue", "keep blocker open"],
        "forbidden_actions": ["rewrite draft", "product evaluation"],
        "decision_authority": "proofread blocker recheck gate",
        "output_contract": ["per_issue_resolution", "reopened_issue_ids", "closed_issue_ids"],
    },
    "publish.decision": {
        "role_identity": "manager",
        "phase_goal": "只做最小业务放行，决定是否批准发布",
        "artifact_scope": "proofread closed state + final handoff readiness",
        "allowed_actions": ["publish approval", "pause", "fail"],
        "forbidden_actions": ["edit final report"],
        "decision_authority": "publish approval",
        "output_contract": ["approved", "reason", "publish_status"],
    },
    "report.publish": {
        "role_identity": "editor_handoff_with_manager_gate",
        "phase_goal": "在 manager gate 放行后交付最终成品",
        "artifact_scope": "final report",
        "allowed_actions": ["handoff final artifact", "persist final files"],
        "forbidden_actions": ["bypass proofread gate"],
        "decision_authority": "editor handoff, manager gate approval",
        "output_contract": ["final markdown", "final html", "final json", "publish gate reason"],
    },
    "product.test": {
        "role_identity": "tester",
        "phase_goal": "从统一读者/产品体验视角评价最终成品",
        "artifact_scope": "final artifact",
        "allowed_actions": ["evaluate product usability", "identify reader pain points"],
        "forbidden_actions": ["producer excuse text", "proofread-only comments"],
        "decision_authority": "product usability report",
        "output_contract": ["visible problems", "reading continuity impact", "highest priority fixes", "why they matter"],
    },
    "product.benchmark": {
        "role_identity": "tester",
        "phase_goal": "对标相近外部产品",
        "artifact_scope": "final artifact vs external examples",
        "allowed_actions": ["search", "compare", "extract practical takeaways"],
        "forbidden_actions": ["large exhaustive market report"],
        "decision_authority": "benchmark report",
        "output_contract": ["selected targets", "selection reason", "important gaps", "practical takeaways"],
    },
    "product.cross_cycle_compare": {
        "role_identity": "tester",
        "phase_goal": "对比本轮与上一轮成品及复盘结论",
        "artifact_scope": "cross-cycle product quality",
        "allowed_actions": ["compare cycles", "identify improvements/regressions", "check unimplemented suggestions"],
        "forbidden_actions": ["retrospective moderation"],
        "decision_authority": "cross-cycle comparison report",
        "output_contract": ["improved_issues", "unimproved_issues", "regressed_areas", "unimplemented_previous_optimization_suggestions"],
    },
    "pre-retro.review": {
        "role_identity": "manager",
        "phase_goal": "对 tester 的三份报告做最小验收，决定 proceed / partial_pass / redo，必要时 forced_proceed 进入 retrospective",
        "artifact_scope": "product.test / benchmark / cross_cycle_compare",
        "allowed_actions": ["proceed", "partial_pass", "request redo", "forced proceed", "pause"],
        "forbidden_actions": ["rewrite tester reports"],
        "decision_authority": "pre-retro tri-state gate",
        "output_contract": ["signal_type", "reason", "forced_progress_issue"],
    },
    "retrospective.plan": {
        "role_identity": "manager",
        "phase_goal": "基于证据构建复盘议程",
        "artifact_scope": "tester reports + execution evidence",
        "allowed_actions": ["aggregate evidence", "prioritize issues", "open topics"],
        "forbidden_actions": ["invent unsupported issues"],
        "decision_authority": "retrospective plan",
        "output_contract": ["product problems", "behavior problems", "topics", "priority"],
    },
    "retrospective.discussion": {
        "role_identity": "manager_or_participant_by_payload_mode",
        "phase_goal": "围绕一个具体问题讨论到分歧、原因、责任和下一步明确",
        "artifact_scope": "one evidence-backed retrospective topic",
        "allowed_actions": ["critique", "debate", "propose", "defend", "moderate"],
        "forbidden_actions": ["round-robin reporting", "checklist recitation", "premature summary"],
        "decision_authority": "manager moderates",
        "output_contract": ["concrete issue discussion", "ownership", "next action"],
    },
    "retrospective.summary": {
        "role_identity": "manager",
        "phase_goal": "输出正式复盘总结",
        "artifact_scope": "plan + discussion + tester reports + execution outcomes",
        "allowed_actions": ["decide accepted improvements", "assign responsibilities"],
        "forbidden_actions": ["quote stitching"],
        "decision_authority": "formal retrospective report",
        "output_contract": ["product problems", "execution problems", "root causes", "accepted/rejected suggestions", "per-agent responsibilities"],
    },
    "agent.optimization": {
        "role_identity": "manager",
        "phase_goal": "给每个 agent 生成下一轮优化指令并写回上下文",
        "artifact_scope": "next-cycle instructions and rules",
        "allowed_actions": ["write per-agent instructions", "compile next-cycle rules"],
        "forbidden_actions": ["ignore accepted retrospective decisions"],
        "decision_authority": "next-cycle optimization context",
        "output_contract": ["per_agent_targeted_actions", "next_cycle_rule_or_context_updates"],
    },
}


def now_local() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_local().isoformat()


def _phase_context(phase: str, agent_id: str) -> dict:
    base = dict(PHASE_ROLE_CONTEXT.get(phase, {}))
    if not base:
        return {}
    role_identity = base.get("role_identity", "")
    if phase == "retrospective.discussion":
        if agent_id == MANAGER_AGENT_ID:
            role_identity = "manager_moderator"
        elif agent_id == EDITOR_AGENT_ID:
            role_identity = "editor_participant"
        elif agent_id == TESTER_AGENT_ID:
            role_identity = "tester_participant"
        elif agent_id == WORKER_33_AGENT_ID:
            role_identity = "worker_33_participant"
        elif agent_id == WORKER_XHS_AGENT_ID:
            role_identity = "worker_xhs_participant"
    base["role_identity"] = role_identity
    return base


def _load_json(text: str | None) -> dict:
    return json.loads(text) if text else {}


def _read_json(path: Path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text())


def create_db_if_needed():
    init_schema()
    execute(
        """
        UPDATE retrospectives
        SET message_id=COALESCE(message_id, CONCAT('retro-', id)),
            from_agent=COALESCE(from_agent, agent_id),
            to_agent=COALESCE(to_agent, 'all'),
            target_type=COALESCE(target_type, 'team'),
            topic=COALESCE(topic, '问题'),
            intent=COALESCE(intent, 'comment'),
            body=COALESCE(body, comment_text)
        WHERE message_id IS NULL
           OR from_agent IS NULL
           OR to_agent IS NULL
           OR target_type IS NULL
           OR topic IS NULL
           OR intent IS NULL
           OR body IS NULL
        """
    )
    _recover_stale_llm_jobs(startup_only=True)


def _resource_guard() -> dict:
    mem_available_mb = 0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                mem_available_mb = int(line.split()[1]) // 1024
                break
    except Exception:
        mem_available_mb = 0
    free_disk_gb = shutil.disk_usage(ROOT).free / (1024**3)
    issues = []
    if mem_available_mb and mem_available_mb < SETTINGS.project_min_available_memory_mb:
        issues.append(
            f"available memory {mem_available_mb}MB < {SETTINGS.project_min_available_memory_mb}MB"
        )
    if free_disk_gb < SETTINGS.project_min_free_disk_gb:
        issues.append(
            f"free disk {free_disk_gb:.1f}GB < {SETTINGS.project_min_free_disk_gb}GB"
        )
    return {
        "ok": not issues,
        "available_memory_mb": mem_available_mb,
        "free_disk_gb": round(free_disk_gb, 2),
        "reason": "; ".join(issues),
    }


def _project_row(project_id: str):
    return fetch_one(
        """
        SELECT project_id, workflow_id, status, current_cycle_no, max_cycles,
               max_consecutive_failures, consecutive_failures, discussion_seconds,
               retrospective_seconds, next_cycle_delay_seconds, latest_run_id,
               next_cycle_at, paused_reason, notes::text
        FROM projects WHERE project_id=%s
        """,
        (project_id,),
    )


def _run_row(run_id: str):
    return fetch_one(
        """
        SELECT project_id, cycle_no, discussion_seconds, status, current_phase, notes::text
        FROM workflow_runs WHERE run_id=%s
        """,
        (run_id,),
    )


def get_run_trace_context(run_id: str) -> dict:
    row = fetch_one("SELECT notes::text FROM workflow_runs WHERE run_id=%s", (run_id,))
    if not row or not row[0]:
        return {}
    notes = json.loads(row[0])
    return notes.get("trace_context", {})


def get_run_project_context(run_id: str) -> tuple[str | None, int | None]:
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    if not row:
        return None, None
    return row[0], row[1]


def _manager_control_event(
    *,
    run_id: str,
    stage_name: str,
    signal_type: str,
    created_by: str = MANAGER_AGENT_ID,
    section: str = "全局",
    payload: dict | None = None,
) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    event_id = f"mce-{uuid.uuid4().hex[:10]}"
    execute(
        """
        INSERT INTO manager_control_events(
            event_id, project_id, cycle_no, run_id, stage_name, section, signal_type, payload, created_by
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        """,
        (event_id, project_id, cycle_no, run_id, stage_name, section, signal_type, jdump(payload or {}), created_by),
    )
    return {
        "event_id": event_id,
        "project_id": project_id,
        "cycle_no": cycle_no,
        "run_id": run_id,
        "stage_name": stage_name,
        "section": section,
        "signal_type": signal_type,
        "payload": payload or {},
    }


def _latest_manager_signal(run_id: str, stage_name: str, section: str = "全局") -> dict | None:
    row = fetch_one(
        """
        SELECT event_id, signal_type, payload::text, created_at
        FROM manager_control_events
        WHERE run_id=%s AND stage_name=%s AND section=%s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (run_id, stage_name, section),
    )
    if not row:
        return None
    return {
        "event_id": row[0],
        "signal_type": row[1],
        "payload": _load_json(row[2]),
        "created_at": row[3].isoformat() if row[3] else None,
    }


def _manager_signal_history(run_id: str, stage_name: str, section: str = "全局") -> list[dict]:
    rows = fetch_all(
        """
        SELECT event_id, signal_type, payload::text, created_at
        FROM manager_control_events
        WHERE run_id=%s AND stage_name=%s AND section=%s
        ORDER BY created_at DESC
        """,
        (run_id, stage_name, section),
    )
    return [
        {
            "event_id": row[0],
            "signal_type": row[1],
            "payload": _load_json(row[2]),
            "created_at": row[3].isoformat() if row[3] else None,
        }
        for row in rows
    ]


def _consecutive_manager_signal_count(
    run_id: str,
    stage_name: str,
    *,
    section: str = "全局",
    signal_types: tuple[str, ...] | list[str] | set[str],
) -> int:
    wanted = {str(item).strip() for item in signal_types if str(item).strip()}
    if not wanted:
        return 0
    count = 0
    for row in _manager_signal_history(run_id, stage_name, section):
        if row["signal_type"] in wanted:
            count += 1
            continue
        break
    return count


def _redo_signal_count_since_progress(run_id: str, stage_name: str, *, section: str = "全局") -> int:
    count = 0
    for row in _manager_signal_history(run_id, stage_name, section):
        signal_type = row["signal_type"]
        if signal_type in {AUDIT_PROCEED, AUDIT_FORCED_PROCEED}:
            break
        if signal_type == AUDIT_REDO:
            count += 1
    return count


def _workflow_notes(run_id: str) -> dict:
    row = fetch_one("SELECT notes::text FROM workflow_runs WHERE run_id=%s", (run_id,))
    return _load_json(row[0]) if row and row[0] else {}


def _update_workflow_notes(run_id: str, notes: dict) -> None:
    execute("UPDATE workflow_runs SET notes=%s::jsonb WHERE run_id=%s", (jdump(notes), run_id))


def _forced_progress_issues(run_id: str) -> list[dict]:
    notes = _workflow_notes(run_id)
    issues = notes.get("forced_progress_issues") or []
    return issues if isinstance(issues, list) else []


def _record_forced_progress_issue(
    run_id: str,
    *,
    stage_name: str,
    section: str,
    title: str,
    reason: str,
    payload: dict,
) -> dict:
    notes = _workflow_notes(run_id)
    issues = notes.get("forced_progress_issues") or []
    if not isinstance(issues, list):
        issues = []
    issue = {
        "stage_name": stage_name,
        "section": section,
        "title": title,
        "reason": reason,
        "payload": payload,
        "mandatory_retrospective_optimization": True,
        "recorded_at": now_iso(),
    }
    signature = json.dumps(
        {
            "stage_name": stage_name,
            "section": section,
            "title": title,
            "anchor": payload.get("review_task_id")
            or payload.get("draft_version_no")
            or payload.get("proofread_round")
            or payload.get("cycle_no"),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    for existing in issues:
        existing_signature = json.dumps(
            {
                "stage_name": existing.get("stage_name"),
                "section": existing.get("section"),
                "title": existing.get("title"),
                "anchor": ((existing.get("payload") or {}).get("review_task_id"))
                or ((existing.get("payload") or {}).get("draft_version_no"))
                or ((existing.get("payload") or {}).get("proofread_round"))
                or ((existing.get("payload") or {}).get("cycle_no")),
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        if existing_signature == signature:
            return existing
    issues.append(issue)
    notes["forced_progress_issues"] = issues
    _update_workflow_notes(run_id, notes)
    return issue


def _cycle_task_plan_row(run_id: str) -> dict | None:
    row = fetch_one(
        """
        SELECT project_id, cycle_no, summary_text, plan_json::text, created_at
        FROM cycle_task_plans
        WHERE run_id=%s
        """,
        (run_id,),
    )
    if not row:
        return None
    return {
        "project_id": row[0],
        "cycle_no": row[1],
        "summary_text": row[2],
        "plan_json": _load_json(row[3]),
        "created_at": row[4].isoformat() if row[4] else None,
    }


def _cycle_task_plan_json(run_id: str) -> dict:
    return (_cycle_task_plan_row(run_id) or {}).get("plan_json") or {}


def _task_initialization_objective(project_id: str | None, cycle_no: int | None) -> dict:
    previous_cycle_summary = ""
    if project_id and cycle_no and cycle_no > 1:
        row = fetch_one(
            "SELECT retrospective_summary FROM project_cycles WHERE project_id=%s AND cycle_no=%s",
            (project_id, cycle_no - 1),
        )
        if row and row[0]:
            previous_cycle_summary = row[0]
    return TASK_INITIALIZATION_OBJECTIVE | {
        "project_id": project_id,
        "cycle_no": cycle_no,
        "previous_cycle_summary": previous_cycle_summary,
    }


def _candidate_owner(section: str) -> str:
    return WORKER_33_AGENT_ID if section in {"政治经济", "科技"} else WORKER_XHS_AGENT_ID


def _normalize_section_requirement_payload(raw_sections) -> dict:
    if isinstance(raw_sections, dict):
        return raw_sections
    normalized = {}
    for item in raw_sections or []:
        if not isinstance(item, dict):
            continue
        section = str(
            item.get("section")
            or item.get("section_name")
            or item.get("name")
            or item.get("title")
            or ""
        ).strip()
        if not section:
            continue
        normalized[section] = item
    return normalized


def _coerce_plan_int(value, default: int = 0) -> int:
    parsed = _safe_int(value, default)
    if parsed != default or value in {0, "0"}:
        return parsed
    match = re.search(r"\d+", str(value or ""))
    return int(match.group(0)) if match else default


def _count_slot_tokens(slot_allocation: dict, token: str) -> int:
    count = 0
    for slots in (slot_allocation or {}).values():
        if not isinstance(slots, list):
            continue
        for item in slots:
            name = str(item or "").strip().lower()
            if name.startswith(token):
                count += 1
    return count


def _normalize_publication_requirements_payload(publication: dict, workflow_objective: dict) -> dict:
    objective_publication = (workflow_objective.get("delivery_requirements") or {}).get("publication_requirements") or {}
    objective_copy_requirements = (workflow_objective.get("delivery_requirements") or {}).get("material_copy_requirements") or {}
    publication = publication if isinstance(publication, dict) else {}
    copy_requirements = publication.get("copy_requirements") if isinstance(publication.get("copy_requirements"), dict) else {}
    slot_counts = publication.get("slot_counts") if isinstance(publication.get("slot_counts"), dict) else {}
    slot_allocation = copy_requirements.get("slot_allocation") or publication.get("slot_allocation") or {}
    image_limits = publication.get("image_limits") if isinstance(publication.get("image_limits"), dict) else {}
    image_constraints = publication.get("image_constraints") if isinstance(publication.get("image_constraints"), dict) else {}
    main_slots = _coerce_plan_int(slot_counts.get("main"))
    secondary_slots = _coerce_plan_int(slot_counts.get("secondary"))
    brief_slots = _coerce_plan_int(slot_counts.get("brief"))
    if main_slots <= 0 and slot_allocation:
        main_slots = _count_slot_tokens(slot_allocation, "main")
    if secondary_slots < 0 or (secondary_slots == 0 and slot_allocation):
        secondary_slots = _count_slot_tokens(slot_allocation, "secondary")
    if brief_slots < 0 or (brief_slots == 0 and slot_allocation):
        brief_slots = _count_slot_tokens(slot_allocation, "brief")
    if main_slots <= 0:
        main_slots = _coerce_plan_int((objective_publication.get("slot_counts") or {}).get("main"))
    if secondary_slots < 0:
        secondary_slots = _coerce_plan_int((objective_publication.get("slot_counts") or {}).get("secondary"))
    if brief_slots < 0:
        brief_slots = _coerce_plan_int((objective_publication.get("slot_counts") or {}).get("brief"))
    if secondary_slots == 0 and not slot_counts and objective_publication.get("slot_counts"):
        secondary_slots = _coerce_plan_int((objective_publication.get("slot_counts") or {}).get("secondary"))
    if brief_slots == 0 and not slot_counts and objective_publication.get("slot_counts"):
        brief_slots = _coerce_plan_int((objective_publication.get("slot_counts") or {}).get("brief"))
    if main_slots <= 0 or secondary_slots < 0 or brief_slots < 0:
        raise RuntimeError("cycle.start.plan missing publication slot_counts")
    main_images = _coerce_plan_int(image_limits.get("main"))
    secondary_images = _coerce_plan_int(image_limits.get("secondary"))
    brief_images = _coerce_plan_int(image_limits.get("brief"))
    if main_images == 0 and image_constraints:
        main_images = _coerce_plan_int(image_constraints.get("主新闻配图"), _coerce_plan_int((objective_publication.get("image_limits") or {}).get("main")))
    if secondary_images == 0 and image_constraints:
        secondary_images = _coerce_plan_int(image_constraints.get("次新闻配图"), _coerce_plan_int((objective_publication.get("image_limits") or {}).get("secondary")))
    if brief_images == 0 and image_constraints:
        brief_images = _coerce_plan_int(image_constraints.get("简讯配图"), _coerce_plan_int((objective_publication.get("image_limits") or {}).get("brief")))
    merged_copy_requirements = dict(objective_copy_requirements)
    merged_copy_requirements.update(copy_requirements)
    section_order = (
        publication.get("section_order")
        or merged_copy_requirements.get("section_order")
        or objective_publication.get("section_order")
        or [section for section, _ in ALL_SECTION_ASSIGNMENTS]
    )
    return {
        "section_order": section_order,
        "slot_counts": {"main": main_slots, "secondary": secondary_slots, "brief": brief_slots},
        "image_limits": {
            "main": max(0, main_images),
            "secondary": max(0, secondary_images),
            "brief": max(0, brief_images),
        },
        "copy_requirements": merged_copy_requirements,
    }


def _normalize_cycle_task_plan(plan_json: dict, *, workflow_objective: dict) -> dict:
    if not isinstance(plan_json, dict):
        raise RuntimeError("cycle.start.plan returned invalid plan_json")
    section_requirements = {}
    raw_sections = _normalize_section_requirement_payload(plan_json.get("section_material_requirements") or {})
    for section, default_owner in ALL_SECTION_ASSIGNMENTS:
        item = raw_sections.get(section) or {}
        owner = str(item.get("owner") or default_owner).strip() or default_owner
        if owner not in {WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID}:
            raise RuntimeError(f"cycle.start.plan produced invalid owner for section={section}: {owner}")
        candidate_target = _coerce_plan_int(item.get("candidate_target"))
        min_approved = _coerce_plan_int(item.get("min_approved"))
        min_with_images = _coerce_plan_int(item.get("min_with_images"))
        if candidate_target <= 0 or min_approved <= 0 or min_with_images < 0:
            raise RuntimeError(f"cycle.start.plan missing section thresholds for section={section}")
        section_requirements[section] = {
            "owner": owner,
            "candidate_target": candidate_target,
            "min_approved": min_approved,
            "min_with_images": min_with_images,
            "collection_goal": str(item.get("collection_goal") or "").strip(),
            "review_focus": _clean_string_list(item.get("review_focus")),
        }
    publication_requirements = _normalize_publication_requirements_payload(
        plan_json.get("publication_requirements") or {},
        workflow_objective,
    )
    phase_assignments = plan_json.get("phase_assignments")
    if not isinstance(phase_assignments, dict):
        phase_assignments = {}
    phase_acceptance = plan_json.get("phase_acceptance")
    if not isinstance(phase_acceptance, dict):
        phase_acceptance = {}
    return {
        "summary": str(plan_json.get("summary") or "").strip(),
        "completion_definition": str(plan_json.get("completion_definition") or "").strip(),
        "section_material_requirements": section_requirements,
        "publication_requirements": publication_requirements,
        "phase_assignments": phase_assignments,
        "phase_acceptance": phase_acceptance,
        "manager_watchpoints": _clean_string_list(plan_json.get("manager_watchpoints")),
        "risk_notes": _clean_string_list(plan_json.get("risk_notes")),
    }


def _validate_cycle_task_plan_schema(plan_json: dict) -> None:
    if not isinstance(plan_json, dict):
        raise RuntimeError("cycle.start.plan returned invalid plan_json")
    raw_sections = plan_json.get("section_material_requirements")
    if not isinstance(raw_sections, dict):
        raise RuntimeError("cycle.start.plan section_material_requirements must be a dict")
    expected_sections = [section for section, _ in ALL_SECTION_ASSIGNMENTS]
    for section in expected_sections:
        item = raw_sections.get(section)
        if not isinstance(item, dict):
            raise RuntimeError(f"cycle.start.plan missing section requirement for section={section}")
        owner = str(item.get("owner") or "").strip()
        candidate_target = _coerce_plan_int(item.get("candidate_target"))
        min_approved = _coerce_plan_int(item.get("min_approved"))
        min_with_images = _coerce_plan_int(item.get("min_with_images"))
        if not owner or candidate_target <= 0 or min_approved <= 0 or min_with_images < 0:
            raise RuntimeError(f"cycle.start.plan missing section thresholds for section={section}")
    publication = plan_json.get("publication_requirements")
    if not isinstance(publication, dict):
        raise RuntimeError("cycle.start.plan publication_requirements must be a dict")
    slot_counts = publication.get("slot_counts")
    image_limits = publication.get("image_limits")
    if not isinstance(slot_counts, dict) or not isinstance(image_limits, dict):
        raise RuntimeError("cycle.start.plan publication_requirements must include dict slot_counts and image_limits")
    phase_assignments = plan_json.get("phase_assignments")
    if not isinstance(phase_assignments, dict):
        raise RuntimeError("cycle.start.plan phase_assignments must be a dict")
    phase_acceptance = plan_json.get("phase_acceptance")
    if not isinstance(phase_acceptance, dict):
        raise RuntimeError("cycle.start.plan phase_acceptance must be a dict")


def _cycle_start_plan_candidate(result: dict) -> dict:
    summary = _require_llm_visible_text(result, field="summary", node_type="cycle.start.plan")
    return {
        "summary": summary,
        "completion_definition": _require_llm_visible_text(
            result,
            field="completion_definition",
            node_type="cycle.start.plan",
            aliases=("summary",),
        ),
        "section_material_requirements": result.get("section_material_requirements") or {},
        "publication_requirements": result.get("publication_requirements") or {},
        "phase_assignments": result.get("phase_assignments") or {},
        "phase_acceptance": result.get("phase_acceptance") or {},
        "manager_watchpoints": result.get("manager_watchpoints") or [],
        "risk_notes": result.get("risk_notes") or [],
    }


def _section_requirement(run_id: str, section: str) -> dict:
    requirements = (_cycle_task_plan_json(run_id).get("section_material_requirements") or {}).get(section) or {}
    if not requirements:
        raise RuntimeError(f"cycle task plan missing section requirement for section={section}")
    candidate_target = _safe_int(requirements.get("candidate_target"))
    min_approved = _safe_int(requirements.get("min_approved"))
    min_with_images = _safe_int(requirements.get("min_with_images"))
    if candidate_target <= 0 or min_approved <= 0 or min_with_images < 0:
        raise RuntimeError(f"cycle task plan contains invalid thresholds for section={section}")
    return {
        "candidate_target": candidate_target,
        "min_approved": min_approved,
        "min_with_images": min_with_images,
        "owner": str(requirements.get("owner") or _candidate_owner(section)).strip() or _candidate_owner(section),
        "collection_goal": str(requirements.get("collection_goal") or "").strip(),
        "review_focus": _clean_string_list(requirements.get("review_focus")),
    }


def _publication_requirements(run_id: str) -> dict:
    requirements = _cycle_task_plan_json(run_id).get("publication_requirements") or {}
    if not requirements:
        raise RuntimeError("cycle task plan missing publication_requirements")
    slot_counts = requirements.get("slot_counts") or {}
    image_limits = requirements.get("image_limits") or {}
    main_slots = max(0, _safe_int(slot_counts.get("main")))
    secondary_slots = max(0, _safe_int(slot_counts.get("secondary")))
    brief_slots = max(0, _safe_int(slot_counts.get("brief")))
    if main_slots <= 0:
        raise RuntimeError("cycle task plan publication_requirements missing positive main slot count")
    return {
        "section_order": requirements.get("section_order") or [section for section, _ in ALL_SECTION_ASSIGNMENTS],
        "slot_counts": {
            "main": main_slots,
            "secondary": secondary_slots,
            "brief": brief_slots,
        },
        "image_limits": {
            "main": max(0, _safe_int(image_limits.get("main"))),
            "secondary": max(0, _safe_int(image_limits.get("secondary"))),
            "brief": max(0, _safe_int(image_limits.get("brief"))),
        },
        "copy_requirements": requirements.get("copy_requirements") or {},
    }


def get_project_memory(project_id: str | None, agent_id: str) -> dict:
    if not project_id:
        return {}
    row = fetch_one(
        "SELECT current_memory::text FROM project_agent_memory WHERE project_id=%s AND agent_id=%s",
        (project_id, agent_id),
    )
    return _load_json(row[0]) if row and row[0] else {}


def get_effective_optimization_log(project_id: str | None, agent_id: str, cycle_no: int | None) -> dict:
    if not project_id or not cycle_no:
        return {"agent_generated": [], "human_guidance": [], "combined": [], "compiled_rules": []}
    rows = fetch_all(
        """
        SELECT source_type, source, author, category, effective_from_cycle, expires_after_cycle, body, details::text, created_at
        FROM optimization_logs
        WHERE project_id=%s
          AND effective_from_cycle <= %s
          AND (expires_after_cycle IS NULL OR expires_after_cycle >= %s)
          AND (agent_id IS NULL OR agent_id=%s)
        ORDER BY created_at
        """,
        (project_id, cycle_no, cycle_no, agent_id),
    )
    agent_generated = []
    human_guidance = []
    combined = []
    for source_type, source, author, category, effective_from_cycle, expires_after_cycle, body, details_text, created_at in rows:
        item = {
            "source_type": source_type,
            "source": source,
            "author": author,
            "category": category,
            "effective_from_cycle": effective_from_cycle,
            "expires_after_cycle": expires_after_cycle,
            "body": body,
            "details": _load_json(details_text),
            "created_at": created_at.isoformat() if created_at else None,
        }
        if source_type == "human_guidance":
            human_guidance.append(item)
        else:
            agent_generated.append(item)
        combined.append(item)
    return {
        "agent_generated": agent_generated,
        "human_guidance": human_guidance,
        "combined": combined,
        "compiled_rules": [
            {
                "rule_type": row[0],
                "target_agent": row[1],
                "target_section": row[2],
                "rule_payload": _load_json(row[3]),
                "rationale": row[4],
                "effective_from_cycle": row[5],
            }
            for row in fetch_all(
                """
                SELECT rule_type, target_agent, target_section, rule_payload::text, rationale, effective_from_cycle
                FROM optimization_rules
                WHERE project_id=%s
                  AND effective_from_cycle <= %s
                  AND status='active'
                  AND (target_agent IS NULL OR target_agent=%s)
                ORDER BY created_at
                """,
                (project_id, cycle_no, agent_id),
            )
        ],
    }


def append_human_guidance(
    project_id: str,
    *,
    body: str,
    category: str = "project",
    agent_id: str | None = None,
    effective_from_cycle: int | None = None,
    expires_after_cycle: int | None = None,
    author: str = "human",
    source: str = "api",
    details: dict | None = None,
) -> dict:
    project = _project_row(project_id)
    if not project:
        raise KeyError(project_id)
    effective_cycle = effective_from_cycle or max(project[3] + 1, 1)
    row = execute_returning(
        """
        INSERT INTO optimization_logs(
            project_id, cycle_no, run_id, agent_id, source_type, source, author, category,
            effective_from_cycle, expires_after_cycle, body, details
        )
        VALUES (%s,%s,%s,%s,'human_guidance',%s,%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING id
        """,
        (
            project_id,
            project[3],
            project[10],
            agent_id,
            source,
            author,
            category,
            effective_cycle,
            expires_after_cycle,
            body.strip(),
            jdump(details or {}),
        ),
    )
    return {
        "id": row[0] if row else None,
        "project_id": project_id,
        "agent_id": agent_id,
        "effective_from_cycle": effective_cycle,
        "expires_after_cycle": expires_after_cycle,
        "body": body.strip(),
    }


def _memory_summary(memory: dict) -> str:
    if not memory:
        return "默认基线策略"
    return memory.get("summary") or memory.get("strategy_label") or "已加载上一轮优化策略"


def _safe_chat_json(system: str, user: str, fallback: dict) -> dict:
    try:
        data = chat_json(system, user)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return fallback


def _stage_chat_json(
    node_type: str,
    system: str,
    user: str,
    fallback: dict,
    *,
    timeout_ms: int | None = None,
    max_completion_tokens: int | None = None,
    max_attempts: int | None = None,
) -> dict:
    config = LLM_NODE_CONFIG.get(node_type, {})
    effective_timeout_ms = int(timeout_ms or config.get("timeout_ms") or 90000)
    effective_tokens = int(max_completion_tokens or config.get("max_completion_tokens") or 700)
    effective_attempts = max(1, int(max_attempts or config.get("max_attempts") or 1))
    effective_backoff_ms = max(0, int(config.get("backoff_ms") or 0))
    started = now_local()
    last_error = ""
    for attempt in range(1, effective_attempts + 1):
        try:
            data = chat_json(
                system,
                user,
                timeout_seconds=max(1, effective_timeout_ms // 1000),
                max_retries=0,
                max_completion_tokens=effective_tokens,
            )
            if not isinstance(data, dict):
                raise RuntimeError("llm returned non-dict json payload")
            finished = now_local()
            return {
                **data,
                "generation_mode": "llm",
                "generation_error": "",
                "timeout_ms": effective_timeout_ms,
                "prompt_size": len(system) + len(user),
                "input_size": len(system) + len(user),
                "attempt_count": attempt,
                "max_attempts": effective_attempts,
                "started_at": started.isoformat(),
                "finished_at": finished.isoformat(),
            }
        except Exception as exc:
            last_error = f"{exc.__class__.__name__}: {exc}"
            error_kind = getattr(exc, "error_kind", "technical_failure")
            raw_excerpt = getattr(exc, "raw_excerpt", "")
            if attempt >= effective_attempts:
                break
            if effective_backoff_ms > 0:
                time.sleep(effective_backoff_ms / 1000)
    finished = now_local()
    return {
        **fallback,
        "generation_mode": "technical_failure",
        "generation_error": last_error,
        "technical_error_kind": error_kind,
        "raw_response_excerpt": raw_excerpt,
        "timeout_ms": effective_timeout_ms,
        "prompt_size": len(system) + len(user),
        "input_size": len(system) + len(user),
        "attempt_count": effective_attempts,
        "max_attempts": effective_attempts,
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
    }


def _run_content_request(request: dict) -> dict:
    LOGGER.info(
        "content.request.start node_type=%s evidence_object_count=%s timeout_ms=%s max_attempts=%s max_completion_tokens=%s prompt_size=%s",
        request["node_type"],
        request.get("evidence_object_count"),
        request.get("timeout_ms"),
        request.get("max_attempts"),
        request.get("max_completion_tokens"),
        len(request.get("prompt_system") or "") + len(request.get("prompt_user") or ""),
    )
    result = _stage_chat_json(
        request["node_type"],
        request["prompt_system"],
        request["prompt_user"],
        request["fallback_payload"],
        timeout_ms=request.get("timeout_ms"),
        max_completion_tokens=request.get("max_completion_tokens"),
        max_attempts=request.get("max_attempts"),
    )
    result["evidence_object_count"] = request.get("evidence_object_count")
    log_method = LOGGER.warning if result.get("generation_mode") != "llm" else LOGGER.info
    log_method(
        "content.request.finish node_type=%s generation_mode=%s attempt_count=%s max_attempts=%s evidence_object_count=%s prompt_size=%s generation_error=%s",
        request["node_type"],
        result.get("generation_mode"),
        result.get("attempt_count"),
        result.get("max_attempts"),
        request.get("evidence_object_count"),
        result.get("prompt_size"),
        result.get("generation_error", ""),
    )
    return result


def _best_effort_translate(text: str | None, *, limit: int = 220) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "")).strip()
    if not cleaned:
        return ""
    try:
        translated = TRANSLATOR.translate(cleaned)
        if translated:
            cleaned = re.sub(r"\s+", " ", translated).strip()
    except Exception:
        pass
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1].rstrip("，。；;,. ") + "。"
    return cleaned


def _llm_node_config(node_type: str) -> dict:
    return LLM_NODE_CONFIG[node_type]


def _llm_job_key(node_type: str, run_id: str, task_id: str | None = None, extra: str | None = None) -> str:
    parts = [node_type, run_id]
    if task_id:
        parts.append(task_id)
    if extra:
        parts.append(extra)
    return "::".join(parts)


def _create_llm_job(
    *,
    job_key: str,
    node_type: str,
    project_id: str | None,
    run_id: str,
    cycle_no: int | None,
    task_id: str | None,
    prompt_system: str,
    prompt_user: str,
    fallback_payload: dict,
    provider_model: str,
    timeout_ms: int,
    max_attempts: int,
    backoff_ms: int,
    evidence_object_count: int,
) -> dict:
    existing = fetch_one(
        """
        SELECT job_id, status, attempt_count, generation_mode, generation_error, queue_delay_ms, model_latency_ms,
               timeout_ms, prompt_size, input_size, evidence_object_count, created_at, started_at, finished_at
        FROM llm_jobs
        WHERE job_key=%s
        """,
        (job_key,),
    )
    if existing:
        return {
            "job_id": existing[0],
            "status": existing[1],
            "attempt_count": existing[2],
            "generation_mode": existing[3],
            "generation_error": existing[4],
            "queue_delay_ms": existing[5],
            "model_latency_ms": existing[6],
            "timeout_ms": existing[7],
            "prompt_size": existing[8],
            "input_size": existing[9],
            "evidence_object_count": existing[10],
            "created_at": existing[11].isoformat() if existing[11] else None,
            "started_at": existing[12].isoformat() if existing[12] else None,
            "finished_at": existing[13].isoformat() if existing[13] else None,
        }
    job_id = f"llm-{uuid.uuid4().hex[:16]}"
    prompt_size = len(prompt_system) + len(prompt_user)
    row = execute_returning(
        """
        INSERT INTO llm_jobs(
            job_id, job_key, node_type, project_id, run_id, cycle_no, task_id, status,
            timeout_ms, prompt_size, input_size, evidence_object_count, max_attempts, backoff_ms,
            provider_model, prompt_system, prompt_user, fallback_payload
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        RETURNING created_at
        """,
        (
            job_id,
            job_key,
            node_type,
            project_id,
            run_id,
            cycle_no,
            task_id,
            timeout_ms,
            prompt_size,
            prompt_size,
            evidence_object_count,
            max_attempts,
            backoff_ms,
            provider_model,
            prompt_system,
            prompt_user,
            jdump(fallback_payload),
        ),
    )
    return {
        "job_id": job_id,
        "status": "pending",
        "attempt_count": 0,
        "generation_mode": None,
        "generation_error": "",
        "queue_delay_ms": None,
        "model_latency_ms": None,
        "timeout_ms": timeout_ms,
        "prompt_size": prompt_size,
        "input_size": prompt_size,
        "evidence_object_count": evidence_object_count,
        "created_at": row[0].isoformat() if row and row[0] else None,
        "started_at": None,
        "finished_at": None,
    }


def _mark_task_waiting_for_job(task_id: str, job_id: str) -> None:
    execute(
        """
        UPDATE tasks
        SET status='waiting',
            result=jsonb_set(COALESCE(result, '{}'::jsonb), '{llm_job_id}', to_jsonb(%s::text), true)
        WHERE task_id=%s
        """,
        (job_id, task_id),
    )


def _llm_job_row(job_id: str) -> dict:
    row = fetch_one(
        """
        SELECT job_id, job_key, node_type, project_id, run_id, cycle_no, task_id, status, attempt_count, generation_mode,
               generation_error, queue_delay_ms, model_latency_ms, timeout_ms, prompt_size, input_size,
               evidence_object_count, max_attempts, backoff_ms, provider_model, prompt_system, prompt_user,
               fallback_payload::text, result_json::text, created_at, started_at, finished_at
        FROM llm_jobs WHERE job_id=%s
        """,
        (job_id,),
    )
    if not row:
        return {}
    return {
        "job_id": row[0],
        "job_key": row[1],
        "node_type": row[2],
        "project_id": row[3],
        "run_id": row[4],
        "cycle_no": row[5],
        "task_id": row[6],
        "status": row[7],
        "attempt_count": row[8],
        "generation_mode": row[9],
        "generation_error": row[10],
        "queue_delay_ms": row[11],
        "model_latency_ms": row[12],
        "timeout_ms": row[13],
        "prompt_size": row[14],
        "input_size": row[15],
        "evidence_object_count": row[16],
        "max_attempts": row[17],
        "backoff_ms": row[18],
        "provider_model": row[19],
        "prompt_system": row[20],
        "prompt_user": row[21],
        "fallback_payload": _load_json(row[22]),
        "result_json": _load_json(row[23]),
        "created_at": row[24],
        "started_at": row[25],
        "finished_at": row[26],
    }


def _claim_next_llm_job() -> dict:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT job_id
                FROM llm_jobs
                WHERE status IN ('pending', 'retrying')
                  AND next_attempt_at <= NOW()
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return {}
            cur.execute(
                """
                UPDATE llm_jobs
                SET status='running',
                    attempt_count=attempt_count+1,
                    started_at=NOW(),
                    generation_error='',
                    queue_delay_ms=EXTRACT(EPOCH FROM (NOW() - created_at)) * 1000
                WHERE job_id=%s
                """,
                (row[0],),
            )
        conn.commit()
    return _llm_job_row(row[0])


def _complete_llm_job(job_id: str, *, status: str, generation_mode: str, generation_error: str, result_json: dict, model_latency_ms: int, started_at: datetime, finished_at: datetime) -> None:
    execute(
        """
        UPDATE llm_jobs
        SET status=%s,
            generation_mode=%s,
            generation_error=%s,
            result_json=%s::jsonb,
            model_latency_ms=%s,
            finished_at=%s
        WHERE job_id=%s
        """,
        (status, generation_mode, generation_error, jdump(result_json), model_latency_ms, finished_at, job_id),
    )


def _retry_llm_job(job: dict, *, generation_error: str, model_latency_ms: int, finished_at: datetime) -> None:
    next_attempt_at = now_local() + timedelta(milliseconds=job["backoff_ms"])
    execute(
        """
        UPDATE llm_jobs
        SET status='retrying',
            generation_mode=NULL,
            generation_error=%s,
            model_latency_ms=%s,
            finished_at=%s,
            next_attempt_at=%s
        WHERE job_id=%s
        """,
        (generation_error, model_latency_ms, finished_at, next_attempt_at, job["job_id"]),
    )


def _recover_stale_llm_jobs(*, startup_only: bool = False) -> int:
    rows = fetch_all(
        """
        SELECT job_id, node_type, status, attempt_count, max_attempts, timeout_ms, created_at, started_at
        FROM llm_jobs
        WHERE status='running'
        ORDER BY created_at
        """
    )
    recovered = 0
    now = now_local()
    for job_id, node_type, _, attempt_count, max_attempts, timeout_ms, created_at, started_at in rows:
        baseline = started_at or created_at or now
        elapsed_ms = int((now - baseline.astimezone()).total_seconds() * 1000)
        timeout_with_grace = int(timeout_ms or 0) + 15000
        if not startup_only and elapsed_ms < timeout_with_grace:
            continue
        error_text = f"Recovered stale llm job after {elapsed_ms}ms without completion"
        if attempt_count < max_attempts:
            execute(
                """
                UPDATE llm_jobs
                SET status='retrying',
                    generation_mode=NULL,
                    generation_error=%s,
                    model_latency_ms=%s,
                    finished_at=NOW(),
                    next_attempt_at=NOW()
                WHERE job_id=%s
                """,
                (error_text, elapsed_ms, job_id),
            )
        else:
            config = _llm_node_config(node_type)
            terminal_status = "failed"
            execute(
                """
                UPDATE llm_jobs
                SET status=%s,
                    generation_mode=%s,
                    generation_error=%s,
                    model_latency_ms=%s,
                    finished_at=NOW(),
                    result_json=jsonb_set(COALESCE(result_json, '{}'::jsonb), '{generation_error}', to_jsonb(%s::text), true)
                WHERE job_id=%s
                """,
                (terminal_status, "technical_failure", error_text, elapsed_ms, error_text, job_id),
            )
        recovered += 1
    return recovered


def process_llm_jobs() -> None:
    _recover_stale_llm_jobs()
    job = _claim_next_llm_job()
    if not job:
        return
    started = now_local()
    timeout_seconds = max(1, int(job["timeout_ms"] / 1000))
    try:
        payload = chat_json(
            job["prompt_system"],
            job["prompt_user"],
            timeout_seconds=timeout_seconds,
            max_retries=0,
            max_completion_tokens=_llm_node_config(job["node_type"]).get("max_completion_tokens", 700),
        )
        finished = now_local()
        result = {
            **payload,
            "generation_mode": "llm",
            "generation_error": "",
            "timeout_ms": job["timeout_ms"],
            "prompt_size": job["prompt_size"],
            "input_size": job["input_size"],
            "evidence_object_count": job["evidence_object_count"],
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
        }
        _complete_llm_job(
            job["job_id"],
            status="succeeded",
            generation_mode="llm",
            generation_error="",
            result_json=result,
            model_latency_ms=int((finished - started).total_seconds() * 1000),
            started_at=started,
            finished_at=finished,
        )
    except Exception as exc:
        finished = now_local()
        error_text = f"{exc.__class__.__name__}: {exc}"
        error_kind = getattr(exc, "error_kind", "technical_failure")
        raw_excerpt = getattr(exc, "raw_excerpt", "")
        model_latency_ms = int((finished - started).total_seconds() * 1000)
        if job["attempt_count"] < job["max_attempts"]:
            _retry_llm_job(job, generation_error=error_text, model_latency_ms=model_latency_ms, finished_at=finished)
            return
        config = _llm_node_config(job["node_type"])
        if config["critical"]:
            _complete_llm_job(
                job["job_id"],
                status="failed",
                generation_mode="technical_failure",
                generation_error=error_text,
                result_json={
                    **job["fallback_payload"],
                    "generation_mode": "technical_failure",
                    "generation_error": error_text,
                    "technical_error_kind": error_kind,
                    "raw_response_excerpt": raw_excerpt,
                    "timeout_ms": job["timeout_ms"],
                    "prompt_size": job["prompt_size"],
                    "input_size": job["input_size"],
                    "evidence_object_count": job["evidence_object_count"],
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                },
                model_latency_ms=model_latency_ms,
                started_at=started,
                finished_at=finished,
            )
        else:
            _complete_llm_job(
                job["job_id"],
                status="failed",
                generation_mode="technical_failure",
                generation_error=error_text,
                result_json={
                    **job["fallback_payload"],
                    "generation_mode": "technical_failure",
                    "generation_error": error_text,
                    "technical_error_kind": error_kind,
                    "raw_response_excerpt": raw_excerpt,
                    "timeout_ms": job["timeout_ms"],
                    "prompt_size": job["prompt_size"],
                    "input_size": job["input_size"],
                    "evidence_object_count": job["evidence_object_count"],
                    "started_at": started.isoformat(),
                    "finished_at": finished.isoformat(),
                },
                model_latency_ms=model_latency_ms,
                started_at=started,
                finished_at=finished,
            )


def _topic_label(text: str | None) -> str:
    value = (text or "").strip()
    return value or "问题"


def _truncate(text: str | None, limit: int = 80) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _compact_prompt_object(
    item: dict | None,
    *,
    title_limit: int = 120,
    summary_limit: int = 120,
    note_limit: int = 80,
) -> dict:
    if not item:
        return {}
    metadata = item.get("metadata") or {}
    summary = (
        item.get("summary_zh")
        or item.get("brief_zh")
        or metadata.get("summary_en")
        or ""
    )
    compact = {
        "id": item.get("id") or item.get("material_id"),
        "title": _truncate(item.get("title"), title_limit),
        "source_media": _truncate(item.get("source_media"), 64),
        "published_at": item.get("published_at", ""),
        "summary_zh": _truncate(summary, summary_limit),
        "brief_zh": _truncate(item.get("brief_zh"), min(90, summary_limit)),
        "image_count": len(item.get("images") or []) if "images" in item else int(item.get("image_count") or 0),
        "relevance_note": _truncate(item.get("relevance_note") or metadata.get("relevance_note"), note_limit),
    }
    return {
        key: value
        for key, value in compact.items()
        if value not in (None, "", [])
    }


def _first_nonempty(parts: list[str], fallback: str) -> str:
    for part in parts:
        value = (part or "").strip()
        if value:
            return value
    return fallback


def _unique_join(parts: list[str]) -> str:
    seen = set()
    ordered = []
    for part in parts:
        value = (part or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return "；".join(ordered)


def _review_signal(section: str, approved: bool, reason: str | None) -> str:
    value = (reason or "").strip().strip("。")
    if value and value not in {"审核通过", "审核通过。"}:
        return f"{section}：{value}"
    return ""


def _product_report_by_type(run_id: str, report_type: str) -> dict:
    return next((row for row in _product_report_rows(run_id) if row["report_type"] == report_type), {})


def _main_titles(run_id: str) -> dict[str, str]:
    final_json = (_load_output_bundle(run_id).get("final_json") or {})
    titles = {}
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        titles[section] = ((final_json.get(section) or {}).get("main") or {}).get("title", "")
    return titles


def _retro_topic_participants(item: dict) -> list[str]:
    agents = [str(item.get("owner") or "").strip()]
    agents.extend(
        agent
        for agent in (
            item.get("counterpart")
            if isinstance(item.get("counterpart"), list)
            else _parse_agents(item.get("counterpart") or item.get("next_agents") or item.get("to_agent") or "")
        )
        if agent
    )
    participants = [agent for agent in agents if agent in RETRO_PARTICIPANTS]
    return list(dict.fromkeys(participants)) or RETRO_PARTICIPANTS[:]


def _retrospective_plan_topics(run_id: str) -> list[dict]:
    retro_plan = _product_report_by_type(run_id, "retrospective_plan")
    topics = _normalize_retro_plan_topics((retro_plan.get("report_json", {}) or {}).get("topics"))
    if not topics:
        raise RuntimeError("retrospective.plan 未提供可讨论的 topics，禁止进入 retrospective.discussion")
    return topics


def _retro_topic_seed(item: dict) -> dict:
    title = str(item.get("title") or item.get("topic") or "问题").strip() or "问题"
    body = str(item.get("body") or item.get("problem") or title).strip()
    next_agents = _retro_topic_participants(item)
    return {
        "title": title,
        "topic": title,
        "body": body,
        "owner": str(item.get("owner") or MANAGER_AGENT_ID).strip() or MANAGER_AGENT_ID,
        "counterpart": str(item.get("counterpart") or ",".join(next_agents)).strip() or ",".join(next_agents),
        "next_agents": next_agents,
        "to_agent": ",".join(next_agents),
        "intent": "moderate",
        "target_type": "team",
    }


def _pick_retro_controversies(run_id: str) -> list[dict]:
    raise RuntimeError("legacy local retrospective controversy generator is disabled; use retrospective.plan topics via content_layer")


def _retro_thread_rows(run_id: str) -> list[dict]:
    rows = fetch_all(
        """
        SELECT topic_id, message_id, reply_to_message_id, COALESCE(from_agent, agent_id), COALESCE(to_agent, 'all'),
               COALESCE(target_type, 'team'), COALESCE(topic, '问题'), COALESCE(intent, 'comment'),
               round_no, COALESCE(body, comment_text), created_at
        FROM retrospectives
        WHERE run_id=%s
        ORDER BY created_at, id
        """,
        (run_id,),
    )
    return [
        {
            "topic_id": row[0],
            "message_id": row[1],
            "reply_to_message_id": row[2],
            "from_agent": row[3],
            "to_agent": row[4],
            "target_type": row[5],
            "topic": row[6],
            "intent": row[7],
            "round_no": row[8],
            "body": row[9],
            "created_at": row[10],
        }
        for row in rows
    ]


def _retro_thread_for_prompt(run_id: str, *, limit: int | None = None, body_limit: int = 160) -> list[dict]:
    rows = _retro_thread_rows(run_id)
    excerpt = rows[-limit:] if limit and limit > 0 else rows
    return [
        {
            "topic_id": msg.get("topic_id"),
            "message_id": msg.get("message_id"),
            "reply_to_message_id": msg.get("reply_to_message_id"),
            "from_agent": msg.get("from_agent"),
            "to_agent": msg.get("to_agent"),
            "topic": msg.get("topic"),
            "intent": msg.get("intent"),
            "round_no": msg.get("round_no"),
            "body": _truncate(msg.get("body"), body_limit),
        }
        for msg in excerpt
    ]


def _retro_thread_text(run_id: str) -> str:
    lines = []
    for msg in _retro_thread_rows(run_id):
        lines.append(
            f"[round {msg['round_no']}] {msg['from_agent']} -> {msg['to_agent']} "
            f"[{msg['topic']}/{msg['intent']}] {msg['body']}"
        )
    return "\n".join(lines) if lines else "暂无复盘消息。"


def _run_context_summary(run_id: str) -> str:
    review_rows = fetch_all(
        """
        SELECT section, approved, reason
        FROM reviews
        WHERE run_id=%s
        ORDER BY created_at
        """,
        (run_id,),
    )
    discussion_rows = fetch_all(
        "SELECT agent_id, comment_text FROM discussions WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    final_row = fetch_one(
        "SELECT revision_plan, final_markdown FROM outputs WHERE run_id=%s",
        (run_id,),
    )
    lines = ["审核结果："]
    for section, approved, reason in review_rows:
        lines.append(f"- {section}: {'通过' if approved else '打回'} | {reason}")
    lines.append("讨论意见：")
    for agent_id, comment_text in discussion_rows:
        lines.append(f"- {agent_id}: {comment_text}")
    if final_row:
        lines.append("修订方案：")
        lines.append(final_row[0] or "无")
        lines.append("终稿摘要：")
        lines.append((final_row[1] or "无")[:1200])
    return "\n".join(lines)


def _local_retro_opening(run_id: str) -> dict:
    raise RuntimeError("legacy local retrospective opening is disabled; use retrospective.discussion opening via content_layer")


def _local_retro_comment(agent_id: str, payload: dict, relevant_context: dict) -> dict:
    raise RuntimeError("legacy local retrospective comment generator is disabled; use retrospective.discussion comment via content_layer")


def _retro_route_defaults(payload: dict, topic_row: dict | None) -> dict:
    current_topic = str(payload.get("topic") or (topic_row or {}).get("title") or "复盘讨论").strip() or "复盘讨论"
    to_agent = str(payload.get("to_agent") or MANAGER_AGENT_ID).strip() or MANAGER_AGENT_ID
    next_agents = [
        agent
        for agent in (
            payload.get("next_agents")
            if isinstance(payload.get("next_agents"), list)
            else _parse_agents(payload.get("next_agents") or to_agent)
        )
        if agent in RETRO_PARTICIPANTS
    ]
    return {
        "topic": current_topic,
        "intent": str(payload.get("intent") or "comment").strip() or "comment",
        "target_type": str(payload.get("target_type") or "team").strip() or "team",
        "to_agent": to_agent,
        "body": "",
        "next_agents": next_agents,
        "mode": str(payload.get("mode") or "").strip(),
    }


def _agent_role_scope(agent_id: str) -> dict:
    scope_map = {
        MANAGER_AGENT_ID: {
            "role": "manager",
            "owned_sections": ["全局"],
            "owned_stages": [
                "cycle.start",
                "material.review.decision",
                "publish.decision",
                "pre-retro.review",
                "retrospective.plan",
                "retrospective.summary",
                "agent.optimization",
            ],
        },
        EDITOR_AGENT_ID: {
            "role": "editor",
            "owned_sections": ["全局"],
            "owned_stages": ["draft.compose", "draft.revise", "report.publish"],
        },
        TESTER_AGENT_ID: {
            "role": "tester",
            "owned_sections": ["全局"],
            "owned_stages": [
                "material.review",
                "draft.proofread",
                "draft.recheck",
                "product.test",
                "product.benchmark",
                "product.cross_cycle_compare",
            ],
        },
        WORKER_33_AGENT_ID: {
            "role": "worker-33",
            "owned_sections": ["政治经济", "科技"],
            "owned_stages": ["material.collect"],
        },
        WORKER_XHS_AGENT_ID: {
            "role": "worker-xhs",
            "owned_sections": ["体育娱乐", "其他"],
            "owned_stages": ["material.collect"],
        },
    }
    return scope_map.get(
        agent_id,
        {
            "role": agent_id,
            "owned_sections": ["全局"],
            "owned_stages": [],
        },
    )


def _agent_memory_seed(agent_id: str, cycle_no: int, summary: str, previous: dict, optimization_log: dict) -> dict:
    seed = {
        "agent_id": agent_id,
        "version_cycle": cycle_no,
        "updated_at": now_iso(),
        "retrospective_memory": summary,
        "role_scope": _agent_role_scope(agent_id),
    }
    for key in ("source_whitelist", "source_blacklist", "review_standards"):
        if previous.get(key):
            seed[key] = previous.get(key)
    active_rules = []
    for item in (optimization_log.get("compiled_rules") or []):
        active_rules.append(
            {
                "rule_type": item.get("rule_type"),
                "target_section": item.get("target_section"),
                "rule_payload": item.get("rule_payload") or {},
                "rationale": _truncate(item.get("rationale"), 140),
            }
        )
    if active_rules:
        seed["active_rules"] = active_rules
    return seed


def _self_optimize_evidence(
    run_id: str,
    agent_id: str,
    summary: str,
    previous: dict,
    relevant: list[dict],
    memory_seed: dict,
    optimization_log: dict,
) -> dict:
    sections = AGENT_SECTIONS.get(agent_id) or ["政治经济", "科技", "体育娱乐", "其他"]
    final_json = (_load_output_bundle(run_id).get("final_json") or {})
    final_sections = {}
    for section in sections:
        payload = final_json.get(section) or {}
        main = payload.get("main") or {}
        secondary = payload.get("secondary") or []
        briefs = payload.get("briefs") or []
        final_sections[section] = {
            "main": {
                "title": _truncate(main.get("title"), 120),
                "summary_zh": _truncate(main.get("summary_zh"), 180),
                "image_count": len(main.get("images") or []),
            },
            "secondary": [
                {
                    "title": _truncate(item.get("title"), 100),
                    "summary_zh": _truncate(item.get("summary_zh"), 120),
                }
                for item in secondary
            ],
            "briefs": [
                {
                    "title": _truncate(item.get("title"), 90),
                    "summary_zh": _truncate(item.get("summary_zh"), 90),
                }
                for item in briefs
            ],
        }
    product_reports = []
    for report in _product_report_rows(run_id):
        report_json = report.get("report_json") or {}
        compact = {
            "report_type": report.get("report_type"),
            "summary_text": _truncate(report.get("summary_text"), 180),
        }
        if report.get("report_type") == "product_test":
            compact["reader_findings"] = _product_test_reader_findings(report_json)
            compact["reader_improvement_opportunities"] = _product_test_reader_improvements(report_json)
        elif report.get("report_type") == "benchmark_report":
            compact["most_visible_gap"] = _truncate(report_json.get("most_visible_gap"), 180)
            compact["next_cycle_actions"] = _clean_string_list(report_json.get("next_cycle_actions"))
        elif report.get("report_type") == "product_evaluation_report":
            compact["top_product_issues"] = _clean_string_list(report_json.get("top_product_issues"))
            compact["next_cycle_recommendations"] = _clean_string_list(report_json.get("next_cycle_recommendations"))
        elif report.get("report_type") == "cross_cycle_compare_report":
            compact["improved_issues"] = _clean_string_list(report_json.get("improved_issues"))
            compact["unimproved_issues"] = _clean_string_list(report_json.get("unimproved_issues"))
            compact["regressed_areas"] = _clean_string_list(report_json.get("regressed_areas"))
            compact["unimplemented_previous_optimization_suggestions"] = _clean_string_list(
                report_json.get("unimplemented_previous_optimization_suggestions"),
            )
        product_reports.append(compact)
    return {
        "agent_id": agent_id,
        "retrospective_summary": _truncate(summary, 320),
        "relevant_retrospective_messages": [
            {
                "from_agent": msg.get("from_agent"),
                "to_agent": msg.get("to_agent"),
                "topic": msg.get("topic"),
                "intent": msg.get("intent"),
                "body": _truncate(msg.get("body"), 180),
            }
            for msg in relevant
        ],
        "product_reports": product_reports,
        "final_sections": final_sections,
        "previous_memory": {
            "summary": _truncate(previous.get("summary"), 180),
            "exposed_issues": _clean_string_list(previous.get("exposed_issues")),
            "next_cycle_strategy": _clean_string_list(previous.get("next_cycle_strategy") or previous.get("execution_strategy")),
            "next_cycle_quality_checks": _clean_string_list(
                previous.get("next_cycle_quality_checks") or previous.get("quality_checks"),
            ),
            "role_improvement_plan": _truncate(previous.get("role_improvement_plan"), 180),
        },
        "role_scope": memory_seed.get("role_scope") or {},
        "active_optimization_rules": [
            {
                "rule_type": item.get("rule_type"),
                "target_section": item.get("target_section"),
                "rationale": _truncate(item.get("rationale"), 120),
                "rule_payload": item.get("rule_payload") or {},
            }
            for item in (optimization_log.get("compiled_rules") or [])
        ],
        "recent_guidance": [
            {
                "source_type": item.get("source_type"),
                "category": item.get("category"),
                "body": _truncate(item.get("body"), 140),
            }
            for item in (optimization_log.get("combined") or [])
            if item.get("body")
        ],
        "current_memory_state": {
            "source_whitelist": memory_seed.get("source_whitelist", []),
            "source_blacklist": memory_seed.get("source_blacklist", []),
            "review_standards": memory_seed.get("review_standards", []),
        },
        "forced_progress_issues": _forced_progress_issues(run_id),
    }


def _local_self_optimize(run_id: str, agent_id: str, cycle_no: int, summary: str, previous: dict, relevant: list[dict], blueprint: dict) -> dict:
    raise RuntimeError("legacy local self_optimize path is disabled; use self_optimize_agent with run-bound evidence")


def _thread_excerpt(run_id: str) -> str:
    rows = _retro_thread_rows(run_id)
    if not rows:
        return "暂无复盘线程。"
    return "\n".join(
        [
            f"[round {msg['round_no']}] {msg['from_agent']} -> {msg['to_agent']} "
            f"[{msg['topic']}/{msg['intent']}] {msg['body']}"
            for msg in rows
        ]
    )


def _load_output_bundle(run_id: str) -> dict:
    row = fetch_one(
        "SELECT draft_markdown, revision_plan, final_markdown, final_json::text, project_id, cycle_no FROM outputs WHERE run_id=%s",
        (run_id,),
    )
    if not row:
        return {}
    return {
        "draft_markdown": row[0] or "",
        "revision_plan": row[1] or "",
        "final_markdown": row[2] or "",
        "final_json": _load_json(row[3]),
        "project_id": row[4],
        "cycle_no": row[5],
    }


def _final_report_excerpt(run_id: str) -> str:
    bundle = _load_output_bundle(run_id)
    final_json = bundle.get("final_json") or {}
    if not final_json:
        return bundle.get("final_markdown", "")[:800]
    lines = []
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        section_data = final_json.get(section) or {}
        main = section_data.get("main") or {}
        secondary = section_data.get("secondary") or []
        lines.append(f"{section} 主推：{main.get('title', '')}")
        for item in secondary:
            lines.append(f"{section} 副推：{item.get('title', '')}")
    return "\n".join(lines)


def _insert_product_report(
    *,
    project_id: str | None,
    cycle_no: int | None,
    run_id: str,
    task_id: str | None,
    agent_id: str | None,
    report_type: str,
    title: str,
    summary_text: str,
    report_json: dict,
) -> None:
    execute(
        """
        INSERT INTO product_reports(project_id, cycle_no, run_id, task_id, agent_id, report_type, title, summary_text, report_json)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        """,
        (project_id, cycle_no, run_id, task_id, agent_id, report_type, title, summary_text, jdump(report_json)),
    )


def _product_report_rows(run_id: str) -> list[dict]:
    rows = fetch_all(
        """
        SELECT agent_id, report_type, title, summary_text, report_json::text, created_at
        FROM product_reports WHERE run_id=%s ORDER BY created_at, id
        """,
        (run_id,),
    )
    return [
        {
            "agent_id": row[0],
            "report_type": row[1],
            "title": row[2],
            "summary_text": row[3],
            "report_json": _load_json(row[4]),
            "created_at": row[5],
        }
        for row in rows
    ]


def _write_aux_report_files(run_id: str, stem: str, title: str, body_md: str, payload: dict) -> dict:
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / f"{stem}.md"
    json_path = run_dir / f"{stem}.json"
    md_path.write_text(body_md)
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    html_path = write_product_report_html(run_id, title, body_md, payload, stem)
    return {"markdown_path": str(md_path), "html_path": str(html_path), "json_path": str(json_path)}


def _write_cycle_task_plan_files(run_id: str, summary: str, plan_json: dict) -> dict:
    lines = [
        "# cycle_task_plan",
        "",
        f"- run_id: {run_id}",
        f"- completion_definition: {plan_json.get('completion_definition', '')}",
        f"- summary: {summary}",
        "",
        "## section_material_requirements",
    ]
    for section, item in (plan_json.get("section_material_requirements") or {}).items():
        lines.append(
            f"- {section}: owner={item.get('owner')} candidate_target={item.get('candidate_target')} min_approved={item.get('min_approved')} min_with_images={item.get('min_with_images')}"
        )
        if item.get("collection_goal"):
            lines.append(f"  collection_goal={item.get('collection_goal')}")
        review_focus = item.get("review_focus") or []
        if review_focus:
            lines.append(f"  review_focus={json.dumps(review_focus, ensure_ascii=False)}")
    lines.extend(
        [
            "",
            "## publication_requirements",
            "```json",
            json.dumps(plan_json.get("publication_requirements") or {}, ensure_ascii=False, indent=2),
            "```",
            "",
            "## phase_assignments",
            *[f"- {phase}: {agent}" for phase, agent in (plan_json.get("phase_assignments") or {}).items()],
            "",
            "## phase_acceptance",
            *[f"- {phase}: {value}" for phase, value in (plan_json.get('phase_acceptance') or {}).items()],
            "",
            "## manager_watchpoints",
            *[f"- {item}" for item in (plan_json.get("manager_watchpoints") or [])],
            "",
            "## risk_notes",
            *[f"- {item}" for item in (plan_json.get("risk_notes") or [])],
        ]
    )
    return _write_aux_report_files(run_id, "cycle_task_plan", "Cycle Task Plan", "\n".join(lines), {"summary": summary, "plan": plan_json})


def _apply_collection_guidance(items: list[dict], optimization_log: dict) -> list[dict]:
    entries = optimization_log.get("combined") or []
    compiled_rules = optimization_log.get("compiled_rules") or []
    whitelist = set()
    blacklist = set()
    prefer_images = False
    min_images = 0
    for entry in entries:
        details = entry.get("details") or {}
        whitelist.update(details.get("source_whitelist") or [])
        blacklist.update(details.get("source_blacklist") or [])
        if details.get("prefer_images"):
            prefer_images = True
    for rule in compiled_rules:
        payload = rule.get("rule_payload") or {}
        if rule.get("rule_type") == "source_whitelist":
            whitelist.update(payload.get("sources") or [])
        elif rule.get("rule_type") == "source_blacklist":
            blacklist.update(payload.get("sources") or [])
        elif rule.get("rule_type") == "image_availability_threshold":
            prefer_images = True
            min_images = max(min_images, int(payload.get("min_images") or 0))
    filtered = []
    for item in items:
        media = item.get("source_media", "")
        if blacklist and any(term in media for term in blacklist):
            continue
        if whitelist and not any(term in media for term in whitelist):
            continue
        if min_images and len(item.get("images") or []) < min_images:
            continue
        filtered.append(item)
    if not filtered:
        filtered = items
    if prefer_images:
        filtered.sort(key=lambda x: (len(x.get("images") or []), x.get("published_at", "")), reverse=True)
    return filtered


def _compiled_rule_payload(optimization_log: dict, rule_type: str) -> list[dict]:
    return [
        (rule.get("rule_payload") or {})
        for rule in (optimization_log.get("compiled_rules") or [])
        if rule.get("rule_type") == rule_type
    ]


def _writer_guidance_settings(optimization_log: dict) -> dict:
    lead_rules = _compiled_rule_payload(optimization_log, "lead_sentence_rule")
    short_rules = _compiled_rule_payload(optimization_log, "short_brief_compression_rule")
    brief_limit = 50
    for rule in short_rules:
        brief_limit = min(brief_limit, int(rule.get("max_chars") or 50))
    return {
        "impact_first": any((rule.get("style") or "") == "impact_first" for rule in lead_rules),
        "brief_limit": brief_limit,
    }


def _apply_writer_guidance(sections_payload: dict, optimization_log: dict) -> dict:
    return sections_payload


def _insert_retrospective_message(
    *,
    project_id: str,
    cycle_no: int,
    run_id: str,
    task_id: str | None,
    topic_id: str | None,
    agent_id: str,
    message_id: str,
    reply_to_message_id: str | None,
    to_agent: str,
    target_type: str,
    topic: str,
    intent: str,
    round_no: int,
    body: str,
):
    execute(
        """
        INSERT INTO retrospectives(
            project_id, cycle_no, run_id, task_id, topic_id, agent_id, message_id, reply_to_message_id,
            from_agent, to_agent, target_type, topic, intent, round_no, body, comment_text
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (message_id) DO NOTHING
        """,
        (
            project_id,
            cycle_no,
            run_id,
            task_id,
            topic_id,
            agent_id,
            message_id,
            reply_to_message_id,
            agent_id,
            to_agent,
            target_type,
            _topic_label(topic),
            intent,
            round_no,
            body,
            body,
        ),
    )


def _task_requires_ack(phase: str, agent_id: str) -> bool:
    return agent_id != MANAGER_AGENT_ID and phase in PHASES_REQUIRING_ACK


def _evaluate_agent_ack(task: dict) -> dict:
    payload = task["payload"] or {}
    phase = task["phase"]
    if not payload.get("role_identity") or not payload.get("phase_goal"):
        return {
            "ack_status": "blocked",
            "understood_goal": "",
            "known_dependencies": ["缺少 phase role context"],
            "risk_note": "缺少角色上下文，不能开始该阶段。",
        }
    if phase == "material.collect" and not int(payload.get("target_count") or 0):
        return {
            "ack_status": "needs_clarification",
            "understood_goal": f"需要先明确【{task['section']}】候选素材目标数量。",
            "known_dependencies": ["target_count"],
            "risk_note": "未收到 manager 明确的候选素材目标。",
        }
    return {
        "ack_status": "ready",
        "understood_goal": payload.get("phase_goal") or payload.get("message_body") or phase,
        "known_dependencies": payload.get("allowed_actions") or [],
        "risk_note": "",
    }


def _insert_agent_ack(task: dict, ack: dict) -> str:
    ack_id = f"ack-{uuid.uuid4().hex[:12]}"
    execute(
        """
        INSERT INTO agent_acks(
            ack_id, run_id, project_id, cycle_no, phase_name, section, agent_id, ack_status,
            understood_goal, known_dependencies, risk_note
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        """,
        (
            ack_id,
            task["run_id"],
            task.get("project_id"),
            task.get("cycle_no"),
            task["phase"],
            task["section"],
            task["agent_id"],
            ack["ack_status"],
            ack.get("understood_goal") or "",
            jdump(ack.get("known_dependencies") or []),
            ack.get("risk_note") or "",
        ),
    )
    return ack_id


def claim_ack_task(agent_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, run_id, project_id, cycle_no, parent_task_id, agent_id, agent_role, section, phase, retry_count, payload::text
                FROM tasks
                WHERE agent_id=%s
                  AND status='awaiting_ack'
                  AND EXISTS (
                      SELECT 1
                      FROM workflow_runs wr
                      WHERE wr.run_id=tasks.run_id AND wr.status='running'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM projects p
                      WHERE p.project_id=tasks.project_id AND p.status='stopped'
                  )
                ORDER BY created_at
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                (agent_id,),
            )
            row = cur.fetchone()
            if not row:
                conn.commit()
                return None
        conn.commit()
    keys = [
        "task_id",
        "run_id",
        "project_id",
        "cycle_no",
        "parent_task_id",
        "agent_id",
        "agent_role",
        "section",
        "phase",
        "retry_count",
        "payload",
    ]
    task = dict(zip(keys, row))
    task["payload"] = json.loads(task["payload"])
    return task


def complete_agent_ack(task_id: str, ack_id: str, ack: dict):
    next_status = "pending" if ack["ack_status"] == "ready" else "failed"
    error_message = None if next_status == "pending" else ack.get("risk_note") or ack["ack_status"]
    execute(
        """
        UPDATE tasks
        SET status=%s,
            error_message=%s,
            payload=jsonb_set(payload,'{ack_id}',to_jsonb(%s::text),true)
        WHERE task_id=%s
        """,
        (next_status, error_message, ack_id, task_id),
    )


def _recover_stalled_agent_acks(limit: int = 16) -> int:
    recovered = 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, run_id, project_id, cycle_no, parent_task_id, agent_id, agent_role, section, phase, retry_count, payload::text
                FROM tasks
                WHERE status='awaiting_ack'
                  AND EXISTS (
                      SELECT 1
                      FROM workflow_runs wr
                      WHERE wr.run_id=tasks.run_id AND wr.status='running'
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM projects p
                      WHERE p.project_id=tasks.project_id AND p.status='stopped'
                  )
                  AND created_at <= NOW() - INTERVAL '5 seconds'
                ORDER BY created_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (limit,),
            )
            rows = cur.fetchall()
            if not rows:
                conn.commit()
                return 0
            keys = [
                "task_id",
                "run_id",
                "project_id",
                "cycle_no",
                "parent_task_id",
                "agent_id",
                "agent_role",
                "section",
                "phase",
                "retry_count",
                "payload",
            ]
            for row in rows:
                task = dict(zip(keys, row))
                task["payload"] = json.loads(task["payload"])
                ack = _evaluate_agent_ack(task)
                ack_id = f"ack-{uuid.uuid4().hex[:12]}"
                cur.execute(
                    """
                    INSERT INTO agent_acks(
                        ack_id, run_id, project_id, cycle_no, phase_name, section, agent_id, ack_status,
                        understood_goal, known_dependencies, risk_note
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
                    """,
                    (
                        ack_id,
                        task["run_id"],
                        task.get("project_id"),
                        task.get("cycle_no"),
                        task["phase"],
                        task["section"],
                        task["agent_id"],
                        ack["ack_status"],
                        ack.get("understood_goal") or "",
                        jdump(ack.get("known_dependencies") or []),
                        ack.get("risk_note") or "",
                    ),
                )
                next_status = "pending" if ack["ack_status"] == "ready" else "failed"
                error_message = None if next_status == "pending" else ack.get("risk_note") or ack["ack_status"]
                cur.execute(
                    """
                    UPDATE tasks
                    SET status=%s,
                        error_message=%s,
                        payload=jsonb_set(payload,'{ack_id}',to_jsonb(%s::text),true)
                    WHERE task_id=%s
                    """,
                    (next_status, error_message, ack_id, task["task_id"]),
                )
                recovered += 1
        conn.commit()
    return recovered


def _recover_stale_running_tasks(limit: int = 8) -> int:
    recovered = 0
    phases = list(STALE_RUNNING_PHASE_TIMEOUTS.keys())
    if not phases:
        return 0
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, phase, started_at
                FROM tasks
                WHERE status='running'
                  AND phase = ANY(%s)
                  AND started_at IS NOT NULL
                  AND EXISTS (
                      SELECT 1
                      FROM workflow_runs wr
                      WHERE wr.run_id=tasks.run_id AND wr.status='running'
                  )
                ORDER BY started_at
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (phases, limit),
            )
            rows = cur.fetchall()
            if not rows:
                conn.commit()
                return 0
            for task_id, phase, started_at in rows:
                timeout_seconds = int(STALE_RUNNING_PHASE_TIMEOUTS.get(phase) or 0)
                if timeout_seconds <= 0 or not started_at:
                    continue
                age_seconds = (datetime.now(started_at.tzinfo) - started_at).total_seconds()
                if age_seconds < timeout_seconds:
                    continue
                cur.execute(
                    """
                    SELECT COUNT(*)
                    FROM llm_jobs
                    WHERE task_id=%s
                      AND status IN ('pending','running','retrying')
                    """,
                    (task_id,),
                )
                active_jobs = int(cur.fetchone()[0] or 0)
                if active_jobs > 0:
                    continue
                cur.execute(
                    """
                    UPDATE tasks
                    SET status='pending',
                        started_at=NULL,
                        finished_at=NULL,
                        error_message=NULL,
                        result=CASE
                            WHEN result ? 'llm_job_id' THEN result - 'llm_job_id'
                            ELSE result
                        END
                    WHERE task_id=%s
                      AND status='running'
                    """,
                    (task_id,),
                )
                recovered += 1
        conn.commit()
    return recovered


def _dispatch_dedupe_key(
    run_id: str,
    *,
    parent_task_id: str | None,
    agent_id: str,
    section: str,
    phase: str,
    retry_count: int,
    payload: dict,
) -> str | None:
    key_parts = [run_id, phase, agent_id, section, str(retry_count)]
    if phase == "material.review.decision":
        key_parts.append(parent_task_id or "root")
        return "|".join(key_parts)
    if phase in {"retrospective.discussion", "discussion.comment"}:
        key_parts.extend(
            [
                str(payload.get("topic_id") or "root"),
                str(payload.get("reply_to_message_id") or "root"),
                str(payload.get("mode") or ""),
                str(payload.get("round_no") or 0),
            ]
        )
        return "|".join(key_parts)
    if phase in {
        "cycle.start",
        "material.collect",
        "material.submit",
        "material.review",
        "draft.compose",
        "draft.proofread",
        "draft.revise",
        "draft.recheck",
        "proofread.decision.explanation",
        "publish.decision",
        "report.publish",
        "product.test",
        "product.benchmark",
        "product.cross_cycle_compare",
        "pre-retro.review",
        "retrospective.plan",
        "retrospective.summary",
        "agent.optimization",
        "agent.self_optimize",
    }:
        return "|".join(key_parts)
    return None


def create_task(
    run_id: str,
    parent_task_id: str | None,
    agent_id: str,
    agent_role: str,
    section: str,
    phase: str,
    retry_count: int,
    payload: dict,
    project_id: str | None = None,
    cycle_no: int | None = None,
    initial_status: str = "pending",
    dispatch_key: str | None = None,
) -> str:
    task_id = uuid.uuid4().hex[:16]
    row = execute_returning(
        """
        INSERT INTO tasks(
            task_id, run_id, project_id, cycle_no, dispatch_key, parent_task_id, agent_id, agent_role,
            section, phase, retry_count, status, payload
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (dispatch_key) DO NOTHING
        RETURNING task_id
        """,
        (
            task_id,
            run_id,
            project_id,
            cycle_no,
            dispatch_key,
            parent_task_id,
            agent_id,
            agent_role,
            section,
            phase,
            retry_count,
            initial_status,
            jdump(payload),
        ),
    )
    if row:
        return row[0]
    if dispatch_key:
        existing = fetch_one("SELECT task_id FROM tasks WHERE dispatch_key=%s", (dispatch_key,))
        if existing:
            return existing[0]
    return task_id


def dispatch_task(
    run_id: str,
    parent_task_id: str | None,
    agent_id: str,
    agent_role: str,
    section: str,
    phase: str,
    retry_count: int,
    payload: dict,
    parent_trace: dict | None = None,
    project_id: str | None = None,
    cycle_no: int | None = None,
) -> str:
    attrs = {
        "workflow_id": WORKFLOW_ID,
        "project_id": project_id,
        "cycle_no": cycle_no,
        "run_id": run_id,
        "task_id": None,
        "parent_task_id": parent_task_id,
        "agent_id": agent_id,
        "agent_role": agent_role,
        "section": section,
        "phase": "task.dispatch",
        "retry_count": retry_count,
        "status": "dispatched",
    }
    with workflow_span("orchestrator", "task.dispatch", attrs, context=extract_context(parent_trace)):
        enriched_payload = dict(payload)
        if "message_body" not in enriched_payload:
            if phase == "cycle.start":
                enriched_payload["message_body"] = (
                    "请以 manager 身份启动新 cycle，读取 server 注入的任务目标与推进规范，生成本轮 cycle_task_plan。"
                )
            elif phase == "material.collect" and retry_count == 0:
                enriched_payload["message_body"] = (
                    f"请按 manager 的 cycle_task_plan 执行【{section}】板块素材采集，提交可交接的候选素材包。"
                )
            elif phase == "material.collect" and retry_count > 0:
                enriched_payload["message_body"] = (
                    f"请按 manager 的返工要求重做【{section}】板块素材包。重点修复：{payload.get('rework_reason', '根据审核意见调整素材包')}。"
                )
            elif phase == "material.review":
                enriched_payload["message_body"] = (
                    f"请审核【{section}】板块候选素材，核对时效、真实性、图文一致性、来源和可用性。"
                )
            elif phase == "material.review.decision":
                enriched_payload["message_body"] = "请以 manager 身份基于 tester 的全量审核结果做最小验收，明确决定 proceed、partial_pass、redo，必要时 forced_proceed。"
            elif phase == "draft.proofread":
                enriched_payload["message_body"] = (
                    "请以 tester 身份对 draft 做 correctness proofread，检查素材一致性、字段正确性、图片、归位和重复问题。"
                )
            elif phase == "proofread.decision":
                enriched_payload["message_body"] = "系统正在基于 proofread issue 执行结构化规则决策，确认 blocker、required actions 与是否进入修订。"
            elif phase == "proofread.decision.explanation":
                enriched_payload["message_body"] = "请输出一份面向 manager 的 proofread 决策解释，说明为什么这些问题需要修或可关闭。"
            elif phase == "draft.compose":
                enriched_payload["message_body"] = "请以 editor 身份把所有已通过素材按 manager 给定的交付要求整合为 draft。"
            elif phase == "draft.revise":
                enriched_payload["message_body"] = "请以 editor 身份根据 proofread issue 和 manager 给定的交付要求修订 draft。"
            elif phase == "draft.recheck":
                enriched_payload["message_body"] = "请以 tester 身份逐项复查上一轮 proofread issue 是否真正解决。"
            elif phase == "publish.decision":
                enriched_payload["message_body"] = "请以 manager 身份做最小业务放行，只决定是否批准 publish。"
            elif phase == "report.publish":
                enriched_payload["message_body"] = "请以 editor 交付 final artifact；系统会基于 blocker 清零和 recheck 结果执行 manager publish gate 放行。"
            elif phase == "product.test":
                enriched_payload["message_body"] = "请以 tester 身份从统一读者/产品体验视角测试本轮 final artifact。"
            elif phase == "product.benchmark":
                enriched_payload["message_body"] = "请以 tester 身份联网对标相近产品，提炼最重要差距和可执行建议。"
            elif phase == "product.cross_cycle_compare":
                enriched_payload["message_body"] = "请以 tester 身份对比本轮与上一轮 final artifact 以及上一轮复盘建议，指出改善、未改善和退步。"
            elif phase == "pre-retro.review":
                enriched_payload["message_body"] = "请以 manager 身份对 tester 的三份报告做最小验收，明确决定 proceed、partial_pass、redo，必要时 forced_proceed。"
            elif phase == "retrospective.plan":
                enriched_payload["message_body"] = "请以 manager 身份基于 tester 的三份报告和执行证据，生成本轮 retrospective plan。"
            elif phase == "retrospective.discussion":
                enriched_payload["message_body"] = "请围绕当前 retrospective topic 给出证据、分歧、责任或下一步，不要做 checklist 复述。"
            elif phase == "retrospective.summary":
                enriched_payload["message_body"] = "请以 manager 身份基于 plan、discussion 和执行证据输出正式 retrospective summary。"
            elif phase == "agent.optimization":
                enriched_payload["message_body"] = "请以 manager 身份生成针对 editor/tester/worker-33/worker-xhs 的下一轮优化指令。"
        enriched_payload["trace_context"] = inject_current_context()
        enriched_payload["project_id"] = project_id
        enriched_payload["cycle_no"] = cycle_no
        enriched_payload.update(_phase_context(phase, agent_id))
        if project_id:
            enriched_payload["agent_memory_snapshot"] = get_project_memory(project_id, agent_id)
            enriched_payload["optimization_log_snapshot"] = get_effective_optimization_log(project_id, agent_id, cycle_no or 0)
        if phase == "material.collect":
            requirement = _section_requirement(run_id, section)
            plan_json = _cycle_task_plan_json(run_id)
            enriched_payload["section_target"] = requirement
            enriched_payload["target_count"] = int(enriched_payload.get("target_count") or requirement["candidate_target"])
            enriched_payload["quality_requirements"] = {
                "min_approved": requirement["min_approved"],
                "min_with_images": requirement["min_with_images"],
            }
            enriched_payload["manager_watchpoints"] = plan_json.get("manager_watchpoints", [])
            enriched_payload["copy_requirements"] = (_publication_requirements(run_id).get("copy_requirements") or {})
        initial_status = "awaiting_ack" if _task_requires_ack(phase, agent_id) else "pending"
        dispatch_key = _dispatch_dedupe_key(
            run_id,
            parent_task_id=parent_task_id,
            agent_id=agent_id,
            section=section,
            phase=phase,
            retry_count=retry_count,
            payload=enriched_payload,
        )
        return create_task(
            run_id,
            parent_task_id,
            agent_id,
            agent_role,
            section,
            phase,
            retry_count,
            enriched_payload,
            project_id,
            cycle_no,
            initial_status=initial_status,
            dispatch_key=dispatch_key,
        )


def new_run(
    discussion_seconds: int | None = None,
    project_id: str | None = None,
    cycle_no: int | None = None,
) -> str:
    run_id = uuid.uuid4().hex[:12]
    discussion_seconds = discussion_seconds or SETTINGS.discussion_test_seconds
    root_attrs = {
        "workflow_id": WORKFLOW_ID,
        "project_id": project_id,
        "cycle_no": cycle_no,
        "run_id": run_id,
        "task_id": None,
        "parent_task_id": None,
        "agent_id": "orchestrator",
        "agent_role": "orchestrator",
        "section": "全局",
        "phase": "workflow.run",
        "retry_count": 0,
        "status": "started",
    }
    with workflow_span("orchestrator", "workflow.run", root_attrs):
        run_trace = inject_current_context()
        trace_id = parse_trace_id(run_trace.get("traceparent"))
        execute(
            """
            INSERT INTO workflow_runs(workflow_id, run_id, project_id, cycle_no, status, discussion_seconds, current_phase, notes)
            VALUES (%s,%s,%s,%s,'running',%s,'created',%s::jsonb)
            """,
            (
                WORKFLOW_ID,
                run_id,
                project_id,
                cycle_no,
                discussion_seconds,
                jdump(
                    {
                        "trace_context": run_trace,
                        "trace_id": trace_id,
                        "project_id": project_id,
                        "cycle_no": cycle_no,
                    }
                ),
            ),
        )
        dispatch_task(
            run_id=run_id,
            parent_task_id=None,
            agent_id=MANAGER_AGENT_ID,
            agent_role=AGENT_ROLES[MANAGER_AGENT_ID],
            section="全局",
            phase="cycle.start",
            retry_count=0,
            payload={"task_initialization_objective": TASK_INITIALIZATION_OBJECTIVE},
            parent_trace=run_trace,
            project_id=project_id,
            cycle_no=cycle_no,
        )
    return run_id


def claim_task(agent_id: str):
    while True:
        with get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT task_id, run_id, project_id, cycle_no, parent_task_id, agent_id, agent_role, section, phase, retry_count, payload::text
                    FROM tasks
                    WHERE agent_id=%s
                      AND status='pending'
                      AND EXISTS (
                          SELECT 1
                          FROM workflow_runs wr
                          WHERE wr.run_id=tasks.run_id AND wr.status='running'
                      )
                      AND NOT EXISTS (
                          SELECT 1
                          FROM projects p
                          WHERE p.project_id=tasks.project_id AND p.status='stopped'
                      )
                    ORDER BY created_at
                    LIMIT 1
                    FOR UPDATE SKIP LOCKED
                    """,
                    (agent_id,),
                )
                row = cur.fetchone()
                if not row:
                    conn.commit()
                    return None
                obsolete_message = None
                phase = row[8]
                retry_count = int(row[9] or 0)
                if phase in {"draft.proofread", "draft.recheck"}:
                    gate_phase, gate_retry, _ = _proofread_gate_settled(row[1])
                    peer_completed = fetch_one(
                        """
                        SELECT COUNT(*)
                        FROM tasks
                        WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed' AND task_id<>%s
                        """,
                        (row[1], phase, retry_count, row[0]),
                    )[0]
                    if phase != gate_phase or retry_count != gate_retry:
                        obsolete_message = f"obsolete {phase} task outside active proofread gate ({gate_phase} round {gate_retry})"
                    elif int(peer_completed or 0) > 0:
                        obsolete_message = f"obsolete duplicate {phase} task after peer completion in round {retry_count}"
                if obsolete_message:
                    cur.execute(
                        """
                        UPDATE tasks
                        SET status='completed', finished_at=NOW(), error_message=NULL, result=%s::jsonb
                        WHERE task_id=%s
                        """,
                        (jdump({"status": "obsolete", "message_body": obsolete_message}), row[0]),
                    )
                    conn.commit()
                    continue
                cur.execute(
                    "UPDATE tasks SET status='running', started_at=NOW() WHERE task_id=%s",
                    (row[0],),
                )
            conn.commit()
        break
    keys = [
        "task_id",
        "run_id",
        "project_id",
        "cycle_no",
        "parent_task_id",
        "agent_id",
        "agent_role",
        "section",
        "phase",
        "retry_count",
        "payload",
    ]
    task = dict(zip(keys, row))
    task["payload"] = json.loads(task["payload"])
    return task


def complete_task(task_id: str, result: dict):
    execute(
        """
        UPDATE tasks
        SET status='completed', finished_at=NOW(), result=%s::jsonb
        WHERE task_id=%s
        """,
        (jdump(result), task_id),
    )


def fail_task(task_id: str, message: str):
    execute(
        """
        UPDATE tasks
        SET status='failed', finished_at=NOW(), error_message=%s
        WHERE task_id=%s
        """,
        (message, task_id),
    )


def _enrich_collected_materials(
    section: str,
    items: list[dict],
    *,
    target_count: int,
    quality_requirements: dict | None = None,
    manager_watchpoints: list[str] | None = None,
    copy_requirements: dict | None = None,
    memory_summary: str = "",
    optimization_log: dict | None = None,
) -> list[dict]:
    if not items:
        return []

    primary_cutoff = max(3, min(target_count, 5))
    minimum_required = max(1, int((quality_requirements or {}).get("min_approved") or 0))
    prompt_items = [
        {
            "source_index": idx,
            "title": item.get("title", ""),
            "source_media": item.get("source_media", ""),
            "published_at": item.get("published_at", ""),
            "image_count": len(item.get("images") or []),
            "summary_en": (item.get("summary_en") or "")[:140],
        }
        for idx, item in enumerate(items)
    ]
    prompt_memory_summary = _truncate(memory_summary, 32) if memory_summary else ""
    optimization_hints = [
        _truncate(entry.get("body", ""), 36)
        for entry in ((optimization_log or {}).get("combined") or [])
        if entry.get("body")
    ]
    request = content_layer.material_collect_request(
        section=section,
        target_count=target_count,
        quality_requirements=quality_requirements or {},
        manager_watchpoints=manager_watchpoints or [],
        copy_requirements=copy_requirements or {},
        memory_summary=prompt_memory_summary,
        optimization_hints=optimization_hints,
        items=prompt_items,
    )
    LOGGER.info(
        "material.collect.enrichment.start section=%s item_count=%s target_count=%s min_required=%s watchpoint_count=%s optimization_hint_count=%s",
        section,
        len(prompt_items),
        target_count,
        minimum_required,
        len(manager_watchpoints or []),
        len(optimization_hints),
    )
    result = _run_content_request(request)
    if result.get("generation_mode") != "llm":
        raise RuntimeError(
            f"material.collect.enrichment requires llm-generated content for section={section}"
        )
    generated = {}
    generation_meta = {}
    for item in result.get("items") or []:
        try:
            source_index = int(item.get("source_index"))
        except Exception:
            continue
        generated[source_index] = item
        generation_meta[source_index] = {
            "generation_mode": result.get("generation_mode", ""),
            "generation_error": result.get("generation_error", ""),
        }
    missing_indexes = [idx for idx in range(len(prompt_items)) if idx not in generated]
    LOGGER.info(
        "material.collect.enrichment.finish section=%s llm_item_count=%s missing_count=%s generation_mode=%s returned_source_indexes=%s missing_source_indexes=%s",
        section,
        len(generated),
        len(missing_indexes),
        result.get("generation_mode"),
        sorted(generated.keys()),
        missing_indexes,
    )
    if missing_indexes:
        raise RuntimeError(
            f"material.collect.enrichment missing llm outputs for section={section} source_indexes={missing_indexes}"
        )
    enriched = []
    for idx, item in enumerate(items):
        extra = generated.get(idx, {})
        meta = generation_meta.get(idx, {})
        title_zh = str(extra.get("title_zh") or "").strip()
        summary_zh = str(extra.get("summary_zh") or "").strip()
        brief_zh = str(extra.get("brief_zh") or "").strip()
        relevance_note = str(extra.get("relevance_note") or "").strip()
        if not all([title_zh, summary_zh, brief_zh, relevance_note]):
            continue
        enriched.append(
            item
            | {
                "title": title_zh,
                "summary_zh": summary_zh,
                "brief_zh": brief_zh,
                "relevance_note": relevance_note,
                "is_primary_candidate": idx < primary_cutoff,
                "candidate_rank": idx + 1,
                "generation_mode": meta.get("generation_mode", "llm"),
                "generation_error": meta.get("generation_error", ""),
            }
        )
    enriched = sorted(enriched, key=lambda item: item.get("candidate_rank") or 999)
    if len(enriched) < minimum_required:
        raise RuntimeError(
            f"material.collect.enrichment only produced {len(enriched)} llm-enriched candidates "
            f"for section={section}; minimum_required={minimum_required}"
        )
    return enriched[:target_count]


def save_materials(run_id: str, task_id: str, section: str, source_agent: str, items: list[dict]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO materials(
                        run_id, task_id, section, source_agent, title, source_media, published_at, link,
                        images, summary_zh, brief_zh, is_primary_candidate, metadata
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb)
                    """,
                    (
                        run_id,
                        task_id,
                        section,
                        source_agent,
                        item["title"],
                        item["source_media"],
                        item["published_at"],
                        item["link"],
                        jdump(item.get("images", [])),
                        item.get("summary_zh"),
                        item.get("brief_zh"),
                        bool(item.get("is_primary_candidate")),
                        jdump(
                            {
                                "summary_en": item.get("summary_en", ""),
                                "relevance_note": item.get("relevance_note", ""),
                                "generation_mode": item.get("generation_mode", ""),
                                "generation_error": item.get("generation_error", ""),
                                "candidate_rank": item.get("candidate_rank"),
                            }
                        ),
                    ),
                )
        conn.commit()


def get_materials(run_id: str, section: str, source_task_id: str | None = None) -> list[dict]:
    if source_task_id is None:
        latest_submit = fetch_one(
            """
            SELECT task_id
            FROM tasks
            WHERE run_id=%s AND section=%s AND phase='material.submit' AND status='completed'
            ORDER BY retry_count DESC, finished_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """,
            (run_id, section),
        )
        if latest_submit:
            source_task_id = latest_submit[0]
    if source_task_id:
        source_task = fetch_one(
            """
            SELECT phase, parent_task_id, result::text
            FROM tasks
            WHERE task_id=%s
            """,
            (source_task_id,),
        )
        if source_task:
            phase = source_task[0]
            parent_task_id = source_task[1]
            result = _load_json(source_task[2])
            if phase == "material.submit":
                source_task_id = result.get("source_task_id") or parent_task_id or source_task_id
    if source_task_id:
        rows = fetch_all(
            """
            SELECT id, title, source_media, published_at, link, images::text, summary_zh, brief_zh, is_primary_candidate, metadata::text
            FROM materials WHERE run_id=%s AND section=%s AND task_id=%s ORDER BY published_at DESC
            """,
            (run_id, section, source_task_id),
        )
    else:
        rows = fetch_all(
            """
            SELECT id, title, source_media, published_at, link, images::text, summary_zh, brief_zh, is_primary_candidate, metadata::text
            FROM materials WHERE run_id=%s AND section=%s ORDER BY published_at DESC
            """,
            (run_id, section),
        )
    items = []
    for row in rows:
        items.append(
            {
                "id": row[0],
                "title": row[1],
                "source_media": row[2],
                "published_at": row[3].isoformat(),
                "link": row[4],
                "images": json.loads(row[5]),
                "summary_zh": row[6] or "",
                "brief_zh": row[7] or "",
                "is_primary_candidate": bool(row[8]),
                "metadata": json.loads(row[9]),
            }
        )
    return items


def review_section(run_id: str, section: str, task_id: str) -> dict:
    latest_submit = fetch_one(
        """
        SELECT task_id
        FROM tasks
        WHERE run_id=%s AND section=%s AND phase='material.submit' AND status='completed'
        ORDER BY retry_count DESC, finished_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        (run_id, section),
    )
    source_task_id = latest_submit[0] if latest_submit else None
    materials = get_materials(run_id, section, source_task_id)
    req = _section_requirement(run_id, section)
    prompt_materials = [
        {
            "material_id": material["id"],
            "title": material["title"],
            "source_media": material["source_media"],
            "published_at": material["published_at"],
            "image_count": len(material.get("images") or []),
            "summary_zh": str(material.get("summary_zh", ""))[:220],
            "brief_zh": str(material.get("brief_zh", ""))[:80],
            "relevance_note": str((material.get("metadata") or {}).get("relevance_note", ""))[:120],
        }
        for material in materials
    ]
    LOGGER.info(
        "material.review.start run_id=%s section=%s material_count=%s min_approved=%s min_with_images=%s",
        run_id,
        section,
        len(prompt_materials),
        req.get("min_approved"),
        req.get("min_with_images"),
    )
    review_result = _run_content_request(
        content_layer.material_review_request(
            run_id=run_id,
            section=section,
            requirements=req,
            materials=prompt_materials,
        )
    )
    LOGGER.info(
        "material.review.result run_id=%s section=%s generation_mode=%s gate_decision_raw=%s reviewed_item_count=%s",
        run_id,
        section,
        review_result.get("generation_mode"),
        review_result.get("gate_decision"),
        len(review_result.get("items") or []),
    )
    review_summary = _require_llm_visible_text(
        review_result,
        field="review_summary",
        node_type="material.review",
    )
    llm_gate_reason = _require_llm_visible_text(
        review_result,
        field="gate_reason",
        node_type="material.review",
        aliases=("review_summary",),
    )
    gate_decision = _require_llm_choice(
        review_result,
        field="gate_decision",
        node_type="material.review",
        allowed=(AUDIT_PROCEED, AUDIT_PARTIAL_PASS, AUDIT_REDO),
    )
    review_map = {}
    selected_rank = {}
    for item in review_result.get("items") or []:
        try:
            material_id = int(item.get("material_id"))
        except Exception:
            continue
        review_map[material_id] = item
        try:
            selected_rank[material_id] = int(item.get("selection_priority") or 999)
        except Exception:
            selected_rank[material_id] = 999
    review_items = []
    approved_pool = []
    returned_issues = []
    for material in materials:
        item = review_map.get(material["id"], {})
        verdict = "approved" if str(item.get("verdict") or "").lower() == "approved" else "rejected"
        reason = str(item.get("reason") or "").strip()
        if not reason:
            raise RuntimeError(f"material.review missing llm item reason for section={section} material_id={material['id']}")
        review_items.append(
            {
                "material_id": material["id"],
                "title": material["title"],
                "source_media": material["source_media"],
                "link": material["link"],
                "image_count": len(material.get("images") or []),
                "verdict": verdict,
                "reason": reason,
                "recommended_slot": item.get("recommended_slot") or "",
                "selection_priority": selected_rank.get(material["id"], 999),
            }
        )
        execute(
            """
            INSERT INTO material_review_items(run_id, review_task_id, section, material_id, verdict, reason)
            VALUES (%s,%s,%s,%s,%s,%s)
            """,
            (run_id, task_id, section, material["id"], verdict, reason),
        )
        if verdict == "approved":
            approved_pool.append(material)
        else:
            returned_issues.append({"material_id": material["id"], "title": material["title"], "reason": reason})
    approved_by_id = {material["id"]: material for material in approved_pool}
    selected_ids = []
    for raw_id in review_result.get("selected_material_ids") or []:
        try:
            material_id = int(raw_id)
        except Exception:
            continue
        if material_id not in approved_by_id or material_id in selected_ids:
            continue
        selected_ids.append(material_id)
    selected = [approved_by_id[material_id] for material_id in selected_ids]
    with_images = [m for m in selected if len(m.get("images") or []) >= 1]
    thresholds_met = len(selected) >= req["min_approved"] and len(with_images) >= req["min_with_images"]
    approved = thresholds_met and gate_decision == "proceed"
    gate_deficits = []
    if len(selected) < req["min_approved"]:
        gate_deficits.append(
            {
                "type": "approved_count_shortage",
                "expected": int(req["min_approved"]),
                "actual": int(len(selected)),
            }
        )
    if len(with_images) < req["min_with_images"]:
        gate_deficits.append(
            {
                "type": "image_count_shortage",
                "expected": int(req["min_with_images"]),
                "actual": int(len(with_images)),
            }
        )
    if not approved and llm_gate_reason:
        returned_issues.append(
            {
                "material_id": None,
                "title": section,
                "reason": llm_gate_reason,
                "source": "llm_gate_reason",
            }
        )
    reason = review_summary.strip()
    selected_ids = [m["id"] for m in selected]
    execute(
        """
        INSERT INTO reviews(run_id, section, review_task_id, reviewer_agent, approved, reason, selected_material_ids)
        VALUES (%s,%s,%s,'tester',%s,%s,%s::jsonb)
        """,
        (run_id, section, task_id, approved, reason, jdump(selected_ids)),
    )
    return {
        "approved": approved,
        "reason": reason,
        "source_task_id": source_task_id,
        "selected_material_ids": selected_ids,
        "approved_material_pool": [
            {
                "material_id": item["id"],
                "title": item["title"],
                "source_media": item["source_media"],
                "published_at": item["published_at"],
                "link": item["link"],
                "image_count": len(item.get("images") or []),
            }
            for item in selected
        ],
        "returned_material_issues": returned_issues,
        "all_review_items": review_items,
        "reviewed_material_count": len(review_items),
        "required_candidates": req["candidate_target"],
        "required_approved": req["min_approved"],
        "required_with_images": req["min_with_images"],
        "gate_decision": gate_decision,
        "audit_decision": gate_decision,
        "thresholds_met": thresholds_met,
        "review_gate_deficits": gate_deficits,
        "generation_mode": review_result.get("generation_mode", ""),
        "generation_error": review_result.get("generation_error", ""),
    }


def _translate_ranked_items(section: str, items: list[dict], guidance: dict | None = None) -> dict[int, dict]:
    brief_limit = int((guidance or {}).get("brief_limit") or 50)
    prompt_items = []
    for idx, item in enumerate(items):
        slot = str(item.get("_slot") or ("main" if idx == 0 else "secondary" if idx in {1, 2} else "brief"))
        compact = _compact_prompt_object(item, title_limit=96, summary_limit=120, note_limit=70)
        prompt_items.append(
            {
                "source_index": idx,
                "slot": slot,
                "title": compact.get("title", ""),
                "source_media": compact.get("source_media", ""),
                "published_at": compact.get("published_at", ""),
                "image_count": compact.get("image_count", 0),
                "candidate_summary": compact.get("summary_zh", ""),
                "candidate_brief": compact.get("brief_zh", ""),
            }
        )
    translated = {}

    def _merge_translation_result(result: dict):
        if result.get("generation_mode") != "llm":
            raise RuntimeError(
                "draft.compose.translation requires llm-generated titles and summaries; "
                f"error={result.get('generation_error') or ''}"
            )
        for item in result.get("items") or []:
            try:
                source_index = int(item.get("source_index"))
            except Exception:
                continue
            title_zh = str(item.get("title_zh") or "").strip()
            summary_zh = str(item.get("summary_zh") or "").strip()
            if not title_zh or not summary_zh:
                raise RuntimeError(f"draft.compose.translation missing llm content for section={section} source_index={source_index}")
            translated[source_index] = {
                "source_index": source_index,
                "title_zh": title_zh,
                "summary_zh": summary_zh,
            }

    request = content_layer.compose_translation_request(
        section=section,
        guidance=guidance or {},
        brief_limit=brief_limit,
        items=prompt_items,
    )
    _merge_translation_result(_run_content_request(request))

    for idx in range(len(prompt_items)):
        if idx not in translated:
            raise RuntimeError(f"draft.compose.translation missing active output for section={section} source_index={idx}")
    return translated


def generate_section_content(
    section: str,
    items: list[dict],
    *,
    guidance: dict | None = None,
    publication_requirements: dict,
) -> dict:
    slot_counts = (publication_requirements.get("slot_counts") or {})
    image_limits = (publication_requirements.get("image_limits") or {})
    main_slots = max(1, _safe_int(slot_counts.get("main"), 1))
    secondary_slots = max(0, _safe_int(slot_counts.get("secondary"), 0))
    brief_slots = max(0, _safe_int(slot_counts.get("brief"), 0))
    if main_slots != 1:
        raise RuntimeError(f"draft.compose currently requires exactly one main slot; got main_slots={main_slots}")
    total_slots = main_slots + secondary_slots + brief_slots
    ranked = sorted(items[:total_slots], key=lambda item: (len(item.get("images", [])), item["published_at"]), reverse=True)
    if len(ranked) < main_slots:
        raise RuntimeError(f"draft.compose requires at least {main_slots} approved materials for section={section}")
    slotted = []
    for idx, item in enumerate(ranked):
        if idx < main_slots:
            slot = "main"
        elif idx < main_slots + secondary_slots:
            slot = "secondary"
        else:
            slot = "brief"
        slotted.append(item | {"_slot": slot})
    mains = slotted[:main_slots]
    secondaries = slotted[main_slots : main_slots + secondary_slots]
    briefs = slotted[main_slots + secondary_slots : main_slots + secondary_slots + brief_slots]
    translated = _translate_ranked_items(section, slotted, guidance=guidance)

    def enrich(item: dict, idx: int, image_limit: int) -> dict:
        translated_item = translated.get(idx, {})
        title_zh = str(translated_item.get("title_zh") or "").strip()
        summary_zh = str(translated_item.get("summary_zh") or "").replace("\n", " ").strip()
        if not title_zh or not summary_zh:
            raise RuntimeError(f"draft.compose.translation missing active output for section={section} source_index={idx}")
        return item | {"title": title_zh, "summary_zh": summary_zh, "images": item.get("images", [])[:image_limit]}

    main_payload = enrich(mains[0], 0, max(0, _safe_int(image_limits.get("main"), 0)))
    return {
        "main": main_payload,
        "secondary": [
            enrich(item, idx + main_slots, max(0, _safe_int(image_limits.get("secondary"), 0)))
            for idx, item in enumerate(secondaries)
        ],
        "briefs": [
            enrich(item, idx + main_slots + secondary_slots, max(0, _safe_int(image_limits.get("brief"), 0)))
            for idx, item in enumerate(briefs)
        ],
    }


def _publication_item_for_prompt(item: dict, *, image_limit: int) -> dict:
    if not item:
        return {}
    return {
        "title": str(item.get("title") or "").strip(),
        "summary_zh": str(item.get("summary_zh") or "").strip(),
        "source_media": str(item.get("source_media") or "").strip(),
        "published_at": str(item.get("published_at") or "").strip(),
        "link": str(item.get("link") or "").strip(),
        "image_count": len(item.get("images") or []),
        "images": [str(img).strip() for img in (item.get("images") or [])[:image_limit] if str(img).strip()],
    }


def _publication_sections_for_prompt(sections_payload: dict, publication_requirements: dict) -> list[dict]:
    image_limits = publication_requirements.get("image_limits") or {}
    blocks = []
    for section in publication_requirements.get("section_order") or ["政治经济", "科技", "体育娱乐", "其他"]:
        data = sections_payload.get(section) or {}
        blocks.append(
            {
                "section": section,
                "main": _publication_item_for_prompt(data.get("main") or {}, image_limit=max(0, _safe_int(image_limits.get("main"), 0))),
                "secondary": [
                    _publication_item_for_prompt(item, image_limit=max(0, _safe_int(image_limits.get("secondary"), 0)))
                    for item in (data.get("secondary") or [])
                ],
                "briefs": [
                    _publication_item_for_prompt(item, image_limit=max(0, _safe_int(image_limits.get("brief"), 0)))
                    for item in (data.get("briefs") or [])
                ],
            }
        )
    return blocks


def _render_draft_markdown_via_llm(
    run_id: str,
    *,
    stage_name: str,
    draft_version_no: int | None,
    sections_payload: dict,
    writer_guidance: dict | None = None,
    revision_context: list[dict] | None = None,
) -> dict:
    request = content_layer.draft_render_request(
        run_id=run_id,
        stage_name=stage_name,
        draft_version_no=draft_version_no,
        writer_guidance=writer_guidance or {},
        revision_context=revision_context or [],
        sections=_publication_sections_for_prompt(sections_payload, _publication_requirements(run_id)),
    )
    result = _run_content_request(request)
    summary = _require_llm_visible_text(result, field="summary", node_type="draft.render")
    report_markdown = _reject_template_shell(
        _require_llm_visible_text(result, field="report_markdown", node_type="draft.render"),
        node_type="draft.render",
        markers=[
            "workflow_id:",
            "project_id:",
            "cycle_no:",
            "run_id:",
            "时区:",
            "### 主推",
            "### 副推",
            "### 其他 7 条",
        ],
    )
    return {
        "summary": summary,
        "report_markdown": report_markdown,
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
        "timeout_ms": result.get("timeout_ms"),
        "prompt_size": result.get("prompt_size"),
        "input_size": result.get("input_size"),
        "evidence_object_count": result.get("evidence_object_count"),
        "started_at": result.get("started_at"),
        "finished_at": result.get("finished_at"),
    }


def _report_markdown_from_sections(
    sections_payload: dict,
    *,
    run_id: str,
    project_id: str | None,
    cycle_no: int | None,
    heading: str = "# 近24小时国际新闻热点",
) -> str:
    raise RuntimeError("legacy local draft/final markdown shell is disabled; use _render_draft_markdown_via_llm")


def _next_draft_version_no(run_id: str) -> int:
    row = fetch_one("SELECT COALESCE(MAX(version_no), 0) FROM draft_versions WHERE run_id=%s", (run_id,))
    return int(row[0] or 0) + 1


def _latest_draft_version(run_id: str) -> dict:
    row = fetch_one(
        """
        SELECT draft_version_id, version_no, stage, markdown_text, report_json::text, created_at
        FROM draft_versions
        WHERE run_id=%s
        ORDER BY version_no DESC
        LIMIT 1
        """,
        (run_id,),
    )
    if not row:
        return {}
    return {
        "draft_version_id": row[0],
        "version_no": row[1],
        "stage": row[2],
        "markdown_text": row[3],
        "report_json": _load_json(row[4]),
        "created_at": row[5],
    }


def _latest_compose_draft_version_no(run_id: str) -> int:
    row = fetch_one(
        """
        SELECT COALESCE(MAX((result->>'draft_version_no')::int), 0)
        FROM tasks
        WHERE run_id=%s AND phase='draft.compose' AND status='completed'
        """,
        (run_id,),
    )
    return _safe_int(row[0] if row else 0)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _task_payload_json(task_id: str) -> dict:
    row = fetch_one("SELECT payload::text FROM tasks WHERE task_id=%s", (task_id,))
    return _load_json(row[0]) if row and row[0] else {}


def _payload_int(payload: dict | None, key: str, default: int = 0) -> int:
    if not isinstance(payload, dict):
        return default
    return _safe_int(payload.get(key), default)


def _draft_version_mismatch_result(
    *,
    latest_version_no: int,
    expected_version_no: int,
    version_field: str,
) -> dict:
    return {
        "status": "obsolete",
        "message_body": "",
        "generation_mode": "obsolete",
        "generation_error": "",
        version_field: latest_version_no,
        f"expected_{version_field}": expected_version_no,
    }


def _draft_sections_for_prompt(run_id: str, sections: list[str] | None = None) -> dict:
    latest = _latest_draft_version(run_id)
    draft_payload = latest.get("report_json") or (_load_output_bundle(run_id).get("final_json") or {})
    target_sections = sections or (_publication_requirements(run_id).get("section_order") or ["政治经济", "科技", "体育娱乐", "其他"])
    return {
        section: {
            "main": _compact_prompt_object(((draft_payload.get(section) or {}).get("main") or {}), summary_limit=110),
            "secondary": [
                _compact_prompt_object(item, summary_limit=90)
                for item in (((draft_payload.get(section) or {}).get("secondary") or []))
            ],
            "briefs": [
                _compact_prompt_object(item, summary_limit=70)
                for item in (((draft_payload.get(section) or {}).get("briefs") or []))
            ],
        }
        for section in target_sections
    }


def _approved_materials_for_prompt(run_id: str, sections: list[str]) -> dict:
    approved = {}
    for section in sections:
        review = fetch_one(
            """
            SELECT selected_material_ids::text
            FROM reviews
            WHERE run_id=%s AND section=%s AND approved=TRUE
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id, section),
        )
        selected_ids = set(json.loads(review[0])) if review and review[0] else set()
        rows = [item for item in get_materials(run_id, section) if not selected_ids or item["id"] in selected_ids]
        approved[section] = [_compact_prompt_object(item, summary_limit=100) for item in rows]
    return approved


def _proofread_item_ref(slot: str, index: int | None = None) -> str:
    if slot == "main":
        return "main"
    if slot == "secondary":
        return f"secondary_{index or 1}"
    if slot == "brief":
        return f"brief_{index or 1}"
    return "section"


def _compact_material_for_proofread(item: dict) -> dict:
    compact = _compact_prompt_object(item, title_limit=88, summary_limit=88, note_limit=56)
    compact["material_id"] = compact.pop("id", item.get("id"))
    return compact


def _compact_draft_section_for_proofread(section: str, draft_section: dict, approved_materials: list[dict]) -> dict:
    draft_items = []
    main = (draft_section or {}).get("main") or {}
    if main:
        draft_items.append(
            {
                "item_ref": "main",
                **_compact_material_for_proofread(main),
            }
        )
    secondary_items = (draft_section or {}).get("secondary") or []
    brief_items = (draft_section or {}).get("briefs") or []
    for idx, item in enumerate(secondary_items, 1):
        draft_items.append(
            {
                "item_ref": _proofread_item_ref("secondary", idx),
                **_compact_material_for_proofread(item),
            }
        )
    for idx, item in enumerate(brief_items, 1):
        draft_items.append(
            {
                "item_ref": _proofread_item_ref("brief", idx),
                **_compact_material_for_proofread(item),
            }
        )
    return {
        "section": section,
        "structure": {
            "main_count": 1 if main else 0,
            "secondary_count": len(secondary_items),
            "brief_count": len(brief_items),
        },
        "draft_items": draft_items,
        "approved_materials": approved_materials,
    }


def _compact_draft_section_for_revision(section: str, draft_section: dict) -> dict:
    compact_section = _compact_draft_section_for_proofread(section, draft_section, [])
    return {
        "section": section,
        "structure": compact_section.get("structure") or {},
        "draft_items": compact_section.get("draft_items") or [],
    }


def _compact_recheck_evidence(evidence: dict) -> dict:
    payload = evidence or {}
    item_snapshot = payload.get("item_snapshot") or {}
    material_snapshot = payload.get("material_snapshot") or {}
    compact = {
        "item_ref": str(item_snapshot.get("item_ref") or "").strip(),
        "material_id": item_snapshot.get("material_id") or material_snapshot.get("material_id"),
        "item_title": _truncate(item_snapshot.get("title"), 80),
        "material_title": _truncate(material_snapshot.get("title"), 80),
        "patch_instruction": _truncate(payload.get("patch_instruction"), 96),
        "required_actions": _clean_string_list(payload.get("required_actions"), limit=3),
        "review_rationale": _truncate(payload.get("review_rationale"), 120),
    }
    return {key: value for key, value in compact.items() if value not in ("", [], None)}


def _proofread_outline_item(item: dict) -> dict:
    return {
        key: value
        for key, value in {
            "item_ref": item.get("item_ref"),
            "material_id": item.get("material_id"),
            "title": _truncate(item.get("title"), 88),
            "source_media": _truncate(item.get("source_media"), 44),
            "published_at": item.get("published_at", ""),
            "image_count": int(item.get("image_count") or 0),
        }.items()
        if value not in (None, "", [])
    }


def _require_llm_visible_text(
    result: dict,
    *,
    field: str,
    node_type: str,
    aliases: tuple[str, ...] = (),
) -> str:
    mode = str(result.get("generation_mode") or "")
    for key in (field, *aliases):
        text = str(result.get(key) or "").strip()
        if not text:
            continue
        if key != field and not result.get(field):
            result[field] = text
        if mode == "llm":
            return text
        break
    if mode != "llm":
        raise RuntimeError(
            f"{node_type} requires llm-generated visible content; "
            f"mode={mode or 'missing'}; error={result.get('generation_error') or ''}"
        )
    raise RuntimeError(
        f"{node_type} requires llm-generated visible content; "
        f"mode={mode or 'missing'}; error={result.get('generation_error') or ''}"
    )


def _reject_template_shell(text: str, *, node_type: str, markers: list[str]) -> str:
    visible = str(text or "").strip()
    lowered = visible.lower()
    for marker in markers:
        token = str(marker or "").strip()
        if token and token.lower() in lowered:
            raise RuntimeError(f"{node_type} produced templated visible content marker={token}")
    return visible


def _clean_string_list(values, *, limit: int | None = None) -> list[str]:
    if values is None:
        raw_items = []
    elif isinstance(values, str):
        raw_items = [values]
    elif isinstance(values, (list, tuple, set)):
        raw_items = list(values)
    else:
        raw_items = [values]
    cleaned = [str(item).strip() for item in raw_items if str(item).strip()]
    if limit is not None:
        return cleaned[:limit]
    return cleaned


def _product_test_reader_findings(report_json: dict | None, *, limit: int | None = None) -> list[str]:
    payload = report_json or {}
    return _clean_string_list(payload.get("reader_findings") or payload.get("most_obvious_problems"), limit=limit)


def _product_test_reader_improvements(report_json: dict | None, *, limit: int | None = None) -> list[str]:
    payload = report_json or {}
    return _clean_string_list(
        payload.get("reader_improvement_opportunities") or payload.get("priority_improvements"),
        limit=limit,
    )


def _require_llm_string_list(
    result: dict,
    *,
    field: str,
    node_type: str,
    min_items: int = 1,
    limit: int | None = None,
) -> list[str]:
    items = _clean_string_list(result.get(field), limit=limit)
    mode = str(result.get("generation_mode") or "")
    if mode != "llm" or len(items) < min_items:
        raise RuntimeError(
            f"{node_type} requires llm-generated {field}; "
            f"mode={mode or 'missing'}; count={len(items)}; error={result.get('generation_error') or ''}"
    )
    return items


def _require_llm_choice(
    result: dict,
    *,
    field: str,
    node_type: str,
    allowed: tuple[str, ...],
) -> str:
    mode = str(result.get("generation_mode") or "")
    value = str(result.get(field) or "").strip().lower()
    if mode != "llm" or value not in allowed:
        raise RuntimeError(
            f"{node_type} requires llm-generated {field}; "
            f"mode={mode or 'missing'}; value={value or 'missing'}; error={result.get('generation_error') or ''}"
        )
    return value


def _require_llm_bool(
    result: dict,
    *,
    field: str,
    node_type: str,
) -> bool:
    mode = str(result.get("generation_mode") or "")
    raw = result.get(field)
    if mode != "llm":
        raise RuntimeError(
            f"{node_type} requires llm-generated {field}; "
            f"mode={mode or 'missing'}; error={result.get('generation_error') or ''}"
        )
    if isinstance(raw, bool):
        return raw
    value = str(raw or "").strip().lower()
    if value in {"true", "yes", "1"}:
        return True
    if value in {"false", "no", "0"}:
        return False
    raise RuntimeError(
        f"{node_type} requires llm-generated boolean {field}; "
        f"mode={mode or 'missing'}; value={value or 'missing'}; error={result.get('generation_error') or ''}"
    )


def _normalize_retro_plan_product_problems(items) -> list[dict]:
    normalized = []
    for item in items or []:
        if isinstance(item, dict):
            problem = str(item.get("problem") or item.get("title") or item.get("summary") or "").strip()
            if not problem:
                continue
            normalized.append(
                {
                    "priority": str(item.get("priority") or "P1").strip() or "P1",
                    "object": str(item.get("object") or item.get("item_ref") or "final_report").strip() or "final_report",
                    "problem": problem,
                }
            )
            continue
        text = str(item or "").strip()
        if text:
            normalized.append({"priority": "P1", "object": "final_report", "problem": text})
    return normalized


def _normalize_retro_plan_behavior_problems(items) -> list[dict]:
    normalized = []
    for item in items or []:
        if isinstance(item, dict):
            problem = str(item.get("problem") or item.get("title") or item.get("summary") or "").strip()
            if not problem:
                continue
            agent = str(item.get("agent") or item.get("owner") or item.get("to_agent") or "team").strip() or "team"
            normalized.append(
                {
                    "priority": str(item.get("priority") or "P1").strip() or "P1",
                    "agent": agent,
                    "problem": problem,
                }
            )
            continue
        text = str(item or "").strip()
        if text:
            normalized.append({"priority": "P1", "agent": "team", "problem": text})
    return normalized


def _normalize_retro_plan_topics(items) -> list[dict]:
    normalized = []
    for item in items or []:
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("topic") or item.get("problem") or item.get("summary") or "").strip()
            body = str(item.get("body") or item.get("problem") or item.get("details") or title).strip()
            if not body:
                continue
            owner = str(item.get("owner") or item.get("to_agent") or MANAGER_AGENT_ID).strip() or MANAGER_AGENT_ID
            counterpart_agents = [
                agent
                for agent in (
                    item.get("counterpart")
                    if isinstance(item.get("counterpart"), list)
                    else _parse_agents(item.get("counterpart") or item.get("next_agents") or "")
                )
                if agent in {WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID, EDITOR_AGENT_ID, TESTER_AGENT_ID}
            ]
            normalized.append(
                {
                    "title": title or _truncate(body, 36),
                    "body": body,
                    "owner": owner,
                    "counterpart": ",".join(counterpart_agents) or ",".join(RETRO_PARTICIPANTS),
                }
            )
            continue
        text = str(item or "").strip()
        if text:
            normalized.append(
                {
                    "title": _truncate(text, 36),
                    "body": text,
                    "owner": MANAGER_AGENT_ID,
                    "counterpart": ",".join(RETRO_PARTICIPANTS),
                }
            )
    return normalized


def _record_draft_version(
    run_id: str,
    *,
    project_id: str | None,
    cycle_no: int | None,
    stage: str,
    created_by: str,
    markdown_text: str,
    report_json: dict,
    source_task_id: str | None,
) -> dict:
    version_no = _next_draft_version_no(run_id)
    row = execute_returning(
        """
        INSERT INTO draft_versions(
            run_id, project_id, cycle_no, version_no, stage, created_by, markdown_text, report_json, source_task_id
        )
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)
        RETURNING draft_version_id
        """,
        (run_id, project_id, cycle_no, version_no, stage, created_by, markdown_text, jdump(report_json), source_task_id),
    )
    return {"draft_version_id": row[0], "version_no": version_no}


def _proofread_issue_rows(run_id: str, statuses: tuple[str, ...] | None = None) -> list[dict]:
    params = [run_id]
    where = ["run_id=%s"]
    if statuses:
        where.append("status = ANY(%s)")
        params.append(list(statuses))
    rows = fetch_all(
        f"""
        SELECT issue_id, section, item_ref, severity, issue_type, description, evidence::text, reported_by, status, resolution_note, opened_at, closed_at
        FROM proofread_issues
        WHERE {' AND '.join(where)}
        ORDER BY opened_at, issue_id
        """,
        tuple(params),
    )
    return [
        {
            "issue_id": row[0],
            "section": row[1],
            "item_ref": row[2],
            "severity": row[3],
            "issue_type": row[4],
            "description": row[5],
            "evidence": _load_json(row[6]),
            "reported_by": row[7],
            "status": row[8],
            "resolution_note": row[9],
            "opened_at": row[10],
            "closed_at": row[11],
        }
        for row in rows
    ]


def _active_blocker_count(run_id: str) -> int:
    row = fetch_one(
        """
        SELECT COUNT(*) FROM proofread_issues
        WHERE run_id=%s AND severity='blocker' AND status <> 'closed'
        """,
        (run_id,),
    )
    return int(row[0] or 0)


def _pending_proofread_resolution_count(run_id: str) -> int:
    row = fetch_one(
        """
        SELECT COUNT(*) FROM proofread_issues
        WHERE run_id=%s AND status IN ('accepted', 'fixed', 'open')
        """,
        (run_id,),
    )
    return int(row[0] or 0)


def _approved_material_lookup_for_sections(run_id: str, sections: list[str]) -> dict[str, dict[int, dict]]:
    lookup: dict[str, dict[int, dict]] = {}
    for section in sections:
        selected_row = fetch_one(
            """
            SELECT selected_material_ids::text
            FROM reviews
            WHERE run_id=%s AND section=%s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id, section),
        )
        selected_ids = set(_load_json(selected_row[0]) if selected_row and selected_row[0] else [])
        materials = [
            item
            for item in get_materials(run_id, section)
            if not selected_ids or item["id"] in selected_ids
        ]
        lookup[section] = {
            int(item["id"]): item
            for item in materials
            if item.get("id") is not None
        }
    return lookup


def _proofread_round(run_id: str) -> int:
    row = fetch_one(
        """
        SELECT COALESCE(MAX(retry_count), 0)
        FROM tasks
        WHERE run_id=%s AND phase IN ('draft.proofread', 'draft.recheck', 'draft.revise')
        """,
        (run_id,),
    )
    return int(row[0] or 0)


def _current_proofread_gate(run_id: str) -> tuple[str, int]:
    round_no = _proofread_round(run_id)
    recheck_exists = fetch_one(
        """
        SELECT COUNT(*)
        FROM tasks
        WHERE run_id=%s AND phase='draft.recheck' AND retry_count=%s
        """,
        (run_id, round_no),
    )[0]
    return ("draft.recheck" if int(recheck_exists or 0) > 0 else "draft.proofread", round_no)


def _phase_round_status_counts(run_id: str, phase: str, retry_count: int) -> dict[str, int]:
    rows = fetch_all(
        """
        SELECT status, COUNT(*)
        FROM tasks
        WHERE run_id=%s AND phase=%s AND retry_count=%s
        GROUP BY status
        """,
        (run_id, phase, retry_count),
    )
    return {row[0]: int(row[1]) for row in rows}


def _proofread_gate_settled(run_id: str) -> tuple[str, int, bool]:
    phase, round_no = _current_proofread_gate(run_id)
    counts = _phase_round_status_counts(run_id, phase, round_no)
    unsettled = sum(counts.get(status, 0) for status in {"pending", "awaiting_ack", "running", "waiting", "failed"})
    settled = int(counts.get("completed", 0)) > 0 and unsettled == 0
    return phase, round_no, settled


def _completed_draft_audit_attempts_since_compose(run_id: str, compose_version_no: int) -> int:
    if compose_version_no <= 0:
        return 0
    row = fetch_one(
        """
        SELECT COUNT(*)
        FROM tasks
        WHERE run_id=%s
          AND phase IN ('draft.proofread', 'draft.recheck')
          AND status='completed'
          AND COALESCE(
                NULLIF(result->>'draft_version_no','')::int,
                NULLIF(payload->>'draft_version_no','')::int,
                0
              ) >= %s
        """,
        (run_id, compose_version_no),
    )
    return _safe_int(row[0] if row else 0)


def _draft_compose_redo_exists(run_id: str, compose_version_no: int) -> bool:
    if compose_version_no <= 0:
        return False
    row = fetch_one(
        """
        SELECT COUNT(*)
        FROM tasks
        WHERE run_id=%s
          AND phase='draft.compose'
          AND COALESCE(NULLIF(payload->>'redo_from_draft_version_no','')::int, 0)=%s
          AND status IN ('pending', 'awaiting_ack', 'running', 'waiting', 'completed')
        """,
        (run_id, compose_version_no),
    )
    return _safe_int(row[0] if row else 0) > 0


def _dispatch_draft_compose_redo(
    run_id: str,
    *,
    project_id: str,
    cycle_no: int,
    trace_ctx: dict,
    compose_version_no: int,
    failed_audit_attempts: int,
    proofread_round: int,
    pending_resolution_count: int,
) -> str | None:
    if compose_version_no <= 0 or _draft_compose_redo_exists(run_id, compose_version_no):
        return None
    reason = (
        f"同一轮 draft 在最近 {failed_audit_attempts} 次审核中仍未通过，"
        "停止继续 patch revise，改为由 editor 基于批准素材整体重做。"
    )
    next_retry = _safe_int(
        fetch_one(
            "SELECT COALESCE(MAX(retry_count), -1) FROM tasks WHERE run_id=%s AND phase='draft.compose'",
            (run_id,),
        )[0]
    ) + 1
    task_id = dispatch_task(
        run_id,
        None,
        EDITOR_AGENT_ID,
        AGENT_ROLES[EDITOR_AGENT_ID],
        "全局",
        "draft.compose",
        next_retry,
        {
            "redo_reason": reason,
            "redo_from_draft_version_no": compose_version_no,
            "failed_audit_attempts": failed_audit_attempts,
            "proofread_round": proofread_round,
        },
        trace_ctx,
        project_id,
        cycle_no,
    )
    _run_current_phase(run_id, "draft.compose")
    return task_id


def _ensure_draft_review_signal(run_id: str) -> dict | None:
    proofread_phase, proofread_round, proofread_gate_settled = _proofread_gate_settled(run_id)
    if not proofread_gate_settled:
        return None
    proofread_done = fetch_one(
        """
        SELECT COUNT(*)
        FROM tasks
        WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed'
        """,
        (run_id, proofread_phase, proofread_round),
    )[0]
    if int(proofread_done or 0) <= 0:
        return None
    proofread_input_draft_version_no = _safe_int(
        fetch_one(
            """
            SELECT COALESCE(MAX((result->>'draft_version_no')::int), 0)
            FROM tasks
            WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed'
            """,
            (run_id, proofread_phase, proofread_round),
        )[0]
    )
    latest_signal = _latest_manager_signal(run_id, "draft.review")
    if latest_signal:
        payload = latest_signal.get("payload") or {}
        if (
            _safe_int(payload.get("proofread_round")) == proofread_round
            and _safe_int(payload.get("draft_version_no")) == proofread_input_draft_version_no
        ):
            return latest_signal
    pending_resolution_count = _pending_proofread_resolution_count(run_id)
    blocker_count = _active_blocker_count(run_id)
    latest_compose_version_no = _latest_compose_draft_version_no(run_id)
    failed_audit_attempts = _completed_draft_audit_attempts_since_compose(run_id, latest_compose_version_no)
    redo_count = _redo_signal_count_since_progress(run_id, "draft.review")
    forced_progress_issue = None
    if pending_resolution_count == 0 and blocker_count == 0:
        signal = AUDIT_PROCEED
        reason = "当前 draft audit 已收敛，未解决问题和 blocker 均为 0，可进入下一阶段。"
    elif failed_audit_attempts >= MAX_DRAFT_AUDIT_FAILURES_BEFORE_REDO:
        if redo_count >= MAX_AUDIT_REDO_BEFORE_FORCE_PROCEED:
            signal = AUDIT_FORCED_PROCEED
            reason = "draft 已连续两次 redo 后仍未通过第三个大版本验收，manager 强制带风险推进并要求在 retrospective 中强制优化。"
            forced_progress_issue = _record_forced_progress_issue(
                run_id,
                stage_name="draft.review",
                section="全局",
                title="draft 审核连续 redo 后强制推进",
                reason=reason,
                payload={
                    "draft_version_no": proofread_input_draft_version_no,
                    "proofread_phase": proofread_phase,
                    "proofread_round": proofread_round,
                    "pending_resolution_count": pending_resolution_count,
                    "blocker_count": blocker_count,
                    "failed_audit_attempts": failed_audit_attempts,
                },
            )
        else:
            signal = AUDIT_REDO
            reason = (
                f"同一轮 draft 在最近 {failed_audit_attempts} 次审核中仍未通过，"
                "停止继续 patch revise，改为由 editor 基于批准素材整体重做。"
            )
    else:
        signal = AUDIT_PARTIAL_PASS
        reason = f"当前 draft 仍有 {pending_resolution_count} 个待解决问题，需按 proofread 意见继续修订。"
    event = _manager_control_event(
        run_id=run_id,
        stage_name="draft.review",
        signal_type=signal,
        payload={
            "proofread_phase": proofread_phase,
            "proofread_round": proofread_round,
            "draft_version_no": proofread_input_draft_version_no,
            "pending_resolution_count": pending_resolution_count,
            "blocker_count": blocker_count,
            "failed_audit_attempts": failed_audit_attempts,
            "reason": reason,
            "mandatory_retrospective_optimization": bool(forced_progress_issue),
            "forced_progress_issue": forced_progress_issue or {},
        },
    )
    return event


def _mark_task_completed_obsolete(task_id: str, message: str) -> None:
    execute(
        """
        UPDATE tasks
        SET status='completed', finished_at=NOW(), error_message=NULL, result=%s::jsonb
        WHERE task_id=%s
        """,
        (jdump({"status": "obsolete", "message_body": message}), task_id),
    )


def _open_retro_topic(
    run_id: str,
    *,
    project_id: str,
    cycle_no: int,
    title: str,
    opened_by: str,
    evidence_refs: list[dict] | None = None,
) -> str:
    topic_id = f"rtp-{uuid.uuid4().hex[:10]}"
    execute(
        """
        INSERT INTO retro_topics(topic_id, run_id, project_id, cycle_no, title, status, evidence_refs, opened_by)
        VALUES (%s,%s,%s,%s,%s,'open',%s::jsonb,%s)
        """,
        (topic_id, run_id, project_id, cycle_no, title, jdump(evidence_refs or []), opened_by),
    )
    return topic_id


def _current_open_retro_topic(run_id: str) -> dict:
    row = fetch_one(
        """
        SELECT topic_id, title, status, evidence_refs::text, opened_by, opened_at
        FROM retro_topics
        WHERE run_id=%s
          AND status IN ('open', 'debating')
          AND EXISTS (
                SELECT 1
                FROM retrospectives
                WHERE retrospectives.topic_id=retro_topics.topic_id
            )
        ORDER BY opened_at DESC
        LIMIT 1
        """,
        (run_id,),
    )
    if not row:
        return {}
    return {
        "topic_id": row[0],
        "title": row[1],
        "status": row[2],
        "evidence_refs": _load_json(row[3]),
        "opened_by": row[4],
        "opened_at": row[5],
    }


def _close_retro_topic(topic_id: str) -> None:
    execute(
        "UPDATE retro_topics SET status='closed', closed_at=NOW() WHERE topic_id=%s",
        (topic_id,),
    )


def _set_retro_topic_status(topic_id: str, status: str) -> None:
    execute(
        "UPDATE retro_topics SET status=%s WHERE topic_id=%s",
        (status, topic_id),
    )


def _retro_messages_for_topic(run_id: str, topic_id: str) -> list[dict]:
    return [msg for msg in _retro_thread_rows(run_id) if msg.get("topic_id") == topic_id]


def _prepare_retro_decision_job(
    run_id: str,
    *,
    topic_id: str,
    title: str,
    thread: list[dict],
    owner_agent: str = MANAGER_AGENT_ID,
) -> dict:
    evidence = [f"{msg['from_agent']}: {_truncate(msg['body'], 120)}" for msg in thread]
    project_id, cycle_no = get_run_project_context(run_id)
    return {
        "node_type": "retro_decision",
        "project_id": project_id,
        "cycle_no": cycle_no,
        "task_id": None,
        "owner_agent": owner_agent,
        "topic_id": topic_id,
        "title": title,
        "prompt_system": "你是新闻协作项目的 manager。基于一个复盘话题线程，输出真正的 topic-level decision record，不要摘抄拼接原话。",
        "prompt_user": "\n".join(
            [
                f"run_id={run_id}",
                f"topic={title}",
                "线程证据：",
                *[f"- {line}" for line in evidence],
                "请输出 JSON：summary。要求明确这个话题确认了什么问题、决定了什么改法、下一轮谁先承担。summary 用自然中文写 2-3 句。",
            ]
        ),
        "fallback_payload": {"summary": ""},
        "evidence_object_count": len(thread),
        "evidence": evidence,
    }


def _apply_retro_decision_result(
    run_id: str,
    *,
    topic_id: str,
    title: str,
    thread: list[dict],
    decision: dict,
    owner_agent: str = MANAGER_AGENT_ID,
) -> dict:
    decision_id = f"rtd-{uuid.uuid4().hex[:10]}"
    summary = _require_llm_visible_text(decision, field="summary", node_type="retro_decision")
    execute(
        """
        INSERT INTO retro_decisions(decision_id, run_id, topic_id, summary, owner_agent, decision_json)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        """,
        (
            decision_id,
            run_id,
            topic_id,
            summary,
            owner_agent,
            jdump({"title": title, "evidence": [f"{msg['from_agent']}: {_truncate(msg['body'], 120)}" for msg in thread], "message_count": len(thread), **decision}),
        ),
    )
    return {"decision_id": decision_id, "summary": summary}


def _record_retro_decision(
    run_id: str,
    *,
    topic_id: str,
    title: str,
    thread: list[dict],
    owner_agent: str = MANAGER_AGENT_ID,
) -> dict:
    prepared = _prepare_retro_decision_job(run_id, topic_id=topic_id, title=title, thread=thread, owner_agent=owner_agent)
    decision = _run_content_request(prepared)
    return _apply_retro_decision_result(run_id, topic_id=topic_id, title=title, thread=thread, decision=decision, owner_agent=owner_agent)


def _next_retro_topic_candidate(run_id: str) -> dict:
    opened_titles = {row[0] for row in fetch_all("SELECT title FROM retro_topics WHERE run_id=%s", (run_id,))}
    for item in _retrospective_plan_topics(run_id):
        candidate = _retro_topic_seed(item)
        title = candidate.get("title") or candidate.get("topic") or "问题"
        if title in opened_titles:
            continue
        return candidate | {"title": title}
    return {}


def compose_draft(run_id: str, task_id: str | None = None) -> dict:
    run_info = _run_row(run_id)
    project_id, cycle_no = run_info[0], run_info[1]
    manager_optimization = get_effective_optimization_log(project_id, MANAGER_AGENT_ID, cycle_no or 0)
    writer_guidance = _writer_guidance_settings(manager_optimization)
    publication_requirements = _publication_requirements(run_id)
    slot_counts = publication_requirements.get("slot_counts") or {}
    total_slots = max(
        1,
        _safe_int(slot_counts.get("main"))
        + _safe_int(slot_counts.get("secondary"))
        + _safe_int(slot_counts.get("brief")),
    )
    sections = publication_requirements.get("section_order") or ["政治经济", "科技", "体育娱乐", "其他"]
    assembled = {}
    for section in sections:
        review = fetch_one(
            """
            SELECT selected_material_ids::text FROM reviews
            WHERE run_id=%s AND section=%s AND approved=TRUE
            ORDER BY created_at DESC LIMIT 1
            """,
            (run_id, section),
        )
        selected = json.loads(review[0])
        materials = [m for m in get_materials(run_id, section) if m["id"] in selected][:total_slots]
        assembled[section] = generate_section_content(
            section,
            materials,
            guidance=writer_guidance,
            publication_requirements=publication_requirements,
        )
    assembled = _apply_writer_guidance(assembled, manager_optimization)
    next_version_no = _next_draft_version_no(run_id)
    draft_render = _render_draft_markdown_via_llm(
        run_id,
        stage_name="draft.compose",
        draft_version_no=next_version_no,
        sections_payload=assembled,
        writer_guidance=writer_guidance,
    )
    draft_markdown = draft_render["report_markdown"]
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    draft_md_path = run_dir / "draft_report.md"
    draft_json_path = run_dir / "draft_report.json"
    draft_md_path.write_text(draft_markdown)
    draft_json_path.write_text(json.dumps(assembled, ensure_ascii=False, indent=2))
    draft_html_path = write_product_report_html(
        run_id,
        "Draft Report",
        draft_markdown,
        {"run_id": run_id, "draft_json": assembled},
        "draft_report",
    )
    draft_version = _record_draft_version(
        run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        stage="draft",
        created_by="editor",
        markdown_text=draft_markdown,
        report_json=assembled,
        source_task_id=task_id,
    )
    execute(
        """
        INSERT INTO outputs(run_id, project_id, cycle_no, draft_markdown, final_json)
        VALUES (%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (run_id) DO UPDATE
        SET project_id=EXCLUDED.project_id, cycle_no=EXCLUDED.cycle_no,
            draft_markdown=EXCLUDED.draft_markdown, final_json=EXCLUDED.final_json, updated_at=NOW()
        """,
        (run_id, project_id, cycle_no, draft_markdown, jdump(assembled)),
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{draft_report_md}', to_jsonb(%s::text), true),
                    '{draft_report_json}', to_jsonb(%s::text), true
                ),
                '{draft_report_html}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (str(draft_md_path), str(draft_json_path), str(draft_html_path), run_id),
    )
    return {
        "draft_markdown": draft_markdown,
        "sections": assembled,
        "draft_version_no": draft_version["version_no"],
        "markdown_path": str(draft_md_path),
        "json_path": str(draft_json_path),
        "html_path": str(draft_html_path),
        "generation_mode": draft_render["generation_mode"],
        "generation_error": draft_render.get("generation_error", ""),
        "message_body": draft_render["summary"],
    }


def create_discussion_comment(run_id: str, task_id: str, agent_id: str) -> str:
    project_id, _ = get_run_project_context(run_id)
    memory = get_project_memory(project_id, agent_id) if project_id else {}
    sections = AGENT_SECTIONS.get(agent_id) or ["政治经济", "科技", "体育娱乐", "其他"]
    prompt_sections = _draft_sections_for_prompt(run_id, sections)
    approved_materials = _approved_materials_for_prompt(run_id, sections)
    draft_reviews = fetch_all(
        "SELECT agent_id, section_scope, review_text FROM draft_reviews WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    latest = _latest_draft_version(run_id)
    request = content_layer.discussion_comment_request(
        run_id=run_id,
        agent_id=agent_id,
        draft_version_no=latest.get("version_no", 0),
        section_scope=sections,
        memory_summary=_memory_summary(memory) if memory else "",
        draft_sections=prompt_sections,
        approved_materials=approved_materials,
        existing_draft_reviews=[
            {"agent_id": row[0], "section_scope": row[1], "review_text": _truncate(row[2], 100)}
            for row in draft_reviews
        ],
    )
    result = _run_content_request(request)
    comment = _require_llm_visible_text(result, field="comment_text", node_type="discussion.comment")
    execute(
        "INSERT INTO discussions(run_id, task_id, agent_id, comment_text) VALUES (%s,%s,%s,%s)",
        (run_id, task_id, agent_id, comment),
    )
    return comment


def create_draft_review_comment(run_id: str, task_id: str, agent_id: str) -> str:
    sections = AGENT_SECTIONS.get(agent_id) or ["政治经济", "科技", "体育娱乐", "其他"]
    scope = ",".join(sections)
    prompt_sections = _draft_sections_for_prompt(run_id, sections)
    approved_materials = _approved_materials_for_prompt(run_id, sections)
    latest = _latest_draft_version(run_id)
    request = content_layer.draft_review_comment_request(
        run_id=run_id,
        agent_id=agent_id,
        draft_version_no=latest.get("version_no", 0),
        sections=prompt_sections,
        approved_materials=approved_materials,
    )
    result = _run_content_request(request)
    text = _require_llm_visible_text(result, field="review_text", node_type="draft.review.comment")
    execute(
        "INSERT INTO draft_reviews(run_id, task_id, agent_id, section_scope, review_text) VALUES (%s,%s,%s,%s,%s)",
        (run_id, task_id, agent_id, scope, text),
    )
    return text


def start_proofread(run_id: str, task_id: str) -> dict:
    latest = _latest_draft_version(run_id)
    return {
        "status": "started",
        "draft_version_no": latest.get("version_no", 0),
        "message_body": "",
    }


def start_cycle(run_id: str, task_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    optimization = get_effective_optimization_log(project_id, MANAGER_AGENT_ID, cycle_no or 0)
    workflow_objective = _task_initialization_objective(project_id, cycle_no)
    active_rules = optimization.get("compiled_rules") or []
    previous_run = fetch_one(
        "SELECT run_id FROM workflow_runs WHERE project_id=%s AND cycle_no=%s",
        (project_id, (cycle_no or 1) - 1),
    ) if project_id and cycle_no and cycle_no > 1 else None
    previous_run_id = previous_run[0] if previous_run else None
    previous_reject_rows = fetch_all(
        """
        SELECT section, COUNT(*) FILTER (WHERE verdict='rejected')
        FROM material_review_items
        WHERE run_id=%s
        GROUP BY section
        """,
        (previous_run_id,),
    ) if previous_run_id else []
    previous_rejects = {row[0]: int(row[1]) for row in previous_reject_rows}
    prepared = content_layer.cycle_start_request(
        project_id=project_id,
        cycle_no=cycle_no,
        workflow_objective=workflow_objective,
        previous_cycle_context={
            "previous_run_id": previous_run_id,
            "previous_reject_counts": previous_rejects,
            "previous_cycle_summary": workflow_objective.get("previous_cycle_summary", ""),
        },
        active_optimization_context={
            "compiled_rules": [
                {
                    "rule_type": item.get("rule_type"),
                    "target_agent": item.get("target_agent"),
                    "target_section": item.get("target_section"),
                    "rule_payload": item.get("rule_payload") or {},
                    "rationale": _truncate(item.get("rationale"), 180),
                }
                for item in active_rules
            ],
            "human_guidance": [
                {
                    "category": item.get("category"),
                    "body": _truncate(item.get("body"), 220),
                }
                for item in (optimization.get("human_guidance") or [])
                if item.get("body")
            ],
        },
    )
    result = _run_content_request(prepared)
    plan_candidate = _cycle_start_plan_candidate(result)
    last_schema_error = ""
    for repair_round in range(2):
        try:
            _validate_cycle_task_plan_schema(plan_candidate)
            plan_json = _normalize_cycle_task_plan(
                plan_candidate,
                workflow_objective=workflow_objective,
            )
            _validate_cycle_task_plan_schema(plan_json)
            break
        except Exception as exc:
            last_schema_error = f"{exc.__class__.__name__}: {exc}"
            if repair_round >= 1:
                raise
            LOGGER.warning(
                "cycle.start.plan.schema_repair run_id=%s round=%s error=%s",
                run_id,
                repair_round + 1,
                last_schema_error,
            )
            repair_request = content_layer.cycle_start_repair_request(
                project_id=project_id,
                cycle_no=cycle_no,
                workflow_objective=workflow_objective,
                invalid_plan_json=plan_candidate,
                schema_error=last_schema_error,
            )
            result = _run_content_request(repair_request)
            plan_candidate = _cycle_start_plan_candidate(result)
    else:
        raise RuntimeError(
            f"cycle.start.plan failed schema repair after retry: {last_schema_error}"
        )
    summary = str(plan_json.get("summary") or "").strip()
    files = _write_cycle_task_plan_files(run_id, summary, plan_json)
    execute(
        """
        INSERT INTO cycle_task_plans(project_id, cycle_no, run_id, created_by, summary_text, plan_json)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT (project_id, cycle_no) DO UPDATE
        SET run_id=EXCLUDED.run_id, summary_text=EXCLUDED.summary_text, plan_json=EXCLUDED.plan_json
        """,
        (project_id, cycle_no, run_id, MANAGER_AGENT_ID, summary, jdump(plan_json | files)),
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{cycle_task_plan_md}', to_jsonb(%s::text), true),
                    '{cycle_task_plan_json}', to_jsonb(%s::text), true
                ),
                '{cycle_task_plan_html}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (files["markdown_path"], files["json_path"], files["html_path"], run_id),
    )
    return {
        "status": "started",
        "cycle_no": cycle_no,
        "active_rule_count": len(active_rules),
        "cycle_task_plan": plan_json,
        "cycle_task_plan_files": files,
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
        "message_body": "",
    }


def submit_proofread_issues(run_id: str, task_id: str, agent_id: str) -> dict:
    run_info = _run_row(run_id)
    project_id, cycle_no = run_info[0], run_info[1]
    draft = _latest_draft_version(run_id)
    report_json = draft.get("report_json") or {}
    existing_rows = _proofread_issue_rows(run_id)
    sections = AGENT_SECTIONS.get(agent_id, [])
    prompt_sections = []
    section_contexts = {}
    for section in sections:
        selected_row = fetch_one(
            """
            SELECT selected_material_ids::text
            FROM reviews
            WHERE run_id=%s AND section=%s
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id, section),
        )
        selected_ids = set(_load_json(selected_row[0]) if selected_row and selected_row[0] else [])
        compact_materials = [
            _compact_material_for_proofread(item)
            for item in get_materials(run_id, section)
            if not selected_ids or item["id"] in selected_ids
        ]
        compact_section = _compact_draft_section_for_proofread(
            section,
            report_json.get(section) or {},
            compact_materials,
        )
        prompt_sections.append(
            compact_section
        )
        section_contexts[section] = {
            "draft_items": {
                item.get("item_ref"): item
                for item in compact_section.get("draft_items") or []
                if item.get("item_ref")
            },
            "approved_materials": {
                item.get("material_id"): item
                for item in compact_materials
                if item.get("material_id") is not None
            },
        }
    existing_open_issues = [
        {
            "issue_id": row["issue_id"],
            "section": row["section"],
            "item_ref": row["item_ref"],
            "severity": row["severity"],
            "issue_type": row["issue_type"],
            "description": row["description"],
            "status": row["status"],
        }
        for row in existing_rows
        if row["section"] in sections and row["status"] != "closed"
    ]
    total_draft_items = sum(len(section.get("draft_items") or []) for section in prompt_sections)
    LOGGER.info(
        "draft.proofread.start run_id=%s agent_id=%s draft_version_no=%s section_count=%s draft_item_count=%s existing_open_issue_count=%s",
        run_id,
        agent_id,
        draft.get("version_no"),
        len(prompt_sections),
        total_draft_items,
        len(existing_open_issues),
    )
    proofread_result = _run_content_request(
        content_layer.draft_proofread_request(
            run_id=run_id,
            agent_id=agent_id,
            draft_version_no=draft.get("version_no"),
            sections=prompt_sections,
            existing_open_issues=existing_open_issues,
        )
    )
    proofread_issues = [
        {
            **raw_issue,
            "section": (raw_issue.get("section") or "").strip(),
        }
        for raw_issue in (proofread_result.get("issues") or [])
    ]
    LOGGER.info(
        "draft.proofread.result run_id=%s agent_id=%s draft_version_no=%s generation_mode=%s raw_issue_count=%s",
        run_id,
        agent_id,
        draft.get("version_no"),
        proofread_result.get("generation_mode"),
        len(proofread_issues),
    )
    summary_text = _require_llm_visible_text(proofread_result, field="summary", node_type="draft.proofread")
    existing_by_key = {
        (row["section"], row["item_ref"], row["issue_type"]): row
        for row in existing_rows
        if row["section"] in sections and row["status"] != "closed"
    }
    created = []
    closed = []
    reopened = []
    seen_keys = set()
    for raw_issue in proofread_issues:
        section = (raw_issue.get("section") or "").strip()
        if section not in sections:
            continue
        item_ref = (raw_issue.get("item_ref") or "section").strip()
        severity = (raw_issue.get("severity") or "medium").strip().lower()
        if severity not in {"blocker", "high", "medium", "low"}:
            severity = "medium"
        issue_type = re.sub(r"[^a-z0-9_:-]+", "_", (raw_issue.get("issue_type") or "draft_quality").strip().lower()) or "draft_quality"
        description = (raw_issue.get("description") or "").strip()
        if not description:
            continue
        section_ctx = section_contexts.get(section) or {}
        item_snapshot = (section_ctx.get("draft_items") or {}).get(item_ref) or {}
        material_id = raw_issue.get("material_id") or item_snapshot.get("material_id")
        material_snapshot = (section_ctx.get("approved_materials") or {}).get(material_id) or {}
        evidence = raw_issue.get("evidence") if isinstance(raw_issue.get("evidence"), dict) else {}
        evidence = {
            **evidence,
            "draft_version_no": draft.get("version_no"),
            "item_snapshot": item_snapshot,
            "material_id": material_id,
            "material_snapshot": material_snapshot,
            "required_actions": raw_issue.get("required_actions") or evidence.get("required_actions") or [],
            "patch_instruction": raw_issue.get("patch_instruction") or evidence.get("patch_instruction") or "",
            "review_rationale": evidence.get("review_rationale") or description,
        }
        key = (section, item_ref, issue_type)
        seen_keys.add(key)
        existing = existing_by_key.get(key)
        if existing:
            execute(
                """
                UPDATE proofread_issues
                SET severity=%s, description=%s, evidence=%s::jsonb, reported_by=%s, status='open',
                    updated_at=NOW(), closed_at=NULL, resolution_note=''
                WHERE issue_id=%s
                """,
                (severity, description, jdump(evidence), agent_id, existing["issue_id"]),
            )
            if existing["status"] == "fixed":
                reopened.append(existing["issue_id"])
            issue_id = existing["issue_id"]
        else:
            issue_id = f"pfi-{uuid.uuid4().hex[:10]}"
            execute(
                """
                INSERT INTO proofread_issues(
                    issue_id, run_id, project_id, cycle_no, section, item_ref, severity, issue_type,
                    description, evidence, reported_by, status
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,'open')
                """,
                (issue_id, run_id, project_id, cycle_no, section, item_ref, severity, issue_type, description, jdump(evidence), agent_id),
            )
        created.append({"issue_id": issue_id, "section": section, "severity": severity, "description": description})
    for key, row in existing_by_key.items():
        if key in seen_keys:
            continue
        if row["status"] in {"open", "accepted", "fixed"}:
            execute(
                """
                UPDATE proofread_issues
                SET status='closed', updated_at=NOW(), closed_at=NOW(), resolution_note=''
                WHERE issue_id=%s
                """,
                (row["issue_id"],),
            )
            closed.append(row["issue_id"])
    return {
        "status": "submitted",
        "draft_version_no": draft.get("version_no"),
        "issue_count": len(created),
        "closed_issue_count": len(closed),
        "reopened_issue_count": len(reopened),
        "issues": created,
        "summary": summary_text,
        "generation_mode": proofread_result.get("generation_mode", ""),
        "generation_error": proofread_result.get("generation_error", ""),
        "message_body": summary_text,
    }


def _default_patch_instruction_for_issue(issue: dict) -> str:
    evidence = issue.get("evidence") or {}
    explicit = (evidence.get("patch_instruction") or "").strip()
    if explicit:
        return explicit
    required_actions = _clean_string_list(evidence.get("required_actions"))
    if required_actions:
        return f"根据 issue 描述和 required_actions 修正 {issue['section']} 的对应对象。"
    return f"根据当前 issue 证据与 manager 给定的交付要求，修正 {issue['section']} 的对应对象。"


def _proofread_required_actions(issue: dict) -> list[str]:
    evidence = issue.get("evidence") or {}
    explicit = evidence.get("required_actions") or []
    actions = [str(item).strip() for item in explicit if str(item).strip()]
    return list(dict.fromkeys(actions))


def _evaluate_proofread_issue(issue: dict) -> dict:
    evidence = issue.get("evidence") or {}
    suggested_actions = _proofread_required_actions(issue)
    accepted = issue["severity"] in {"blocker", "high", "medium"}
    reasons = []
    if evidence.get("review_rationale"):
        reasons.append(str(evidence.get("review_rationale")))
    if issue["severity"] == "blocker":
        reasons.append("blocker 未清零前禁止 publish")
    if suggested_actions:
        reasons.append("tester 已提供明确修订动作")
    if evidence.get("required_images") and evidence.get("actual_images", evidence.get("image_count", 0)) < evidence.get("required_images"):
        accepted = True
        reasons.append("evidence 显示仍缺图")
    decision_type = "accept" if accepted else "reject"
    blocker_open = bool(accepted and issue["severity"] == "blocker")
    requires_patch = accepted
    return {
        "issue_id": issue["issue_id"],
        "decision_type": decision_type,
        "requires_patch": requires_patch,
        "blocker_open": blocker_open,
        "blocker_closed": False,
        "required_actions": suggested_actions if accepted else [],
        "patch_instruction": _default_patch_instruction_for_issue(issue) if accepted else "",
        "rationale": "；".join(reasons) if reasons else issue["description"],
    }


def _prepare_proofread_decision_explanation_job(run_id: str, task_id: str) -> dict:
    proofread_phase, proofread_round, proofread_gate_settled = _proofread_gate_settled(run_id)
    if not proofread_gate_settled:
        raise RuntimeError(
            f"active proofread gate 尚未收敛（{proofread_phase} round {proofread_round}），禁止生成 decision explanation"
        )
    project_id, cycle_no = get_run_project_context(run_id)
    draft = _latest_draft_version(run_id)
    decisions = fetch_all(
        """
        SELECT i.issue_id, i.section, i.severity, i.issue_type, i.description, d.decision_type, d.rationale, d.decision_json::text
        FROM proofread_decisions d
        JOIN proofread_issues i ON i.issue_id=d.issue_id
        WHERE d.run_id=%s
        ORDER BY d.created_at, i.section
        """,
        (run_id,),
    )
    decision_rows = [
        {
            "issue_id": row[0],
            "section": row[1],
            "severity": row[2],
            "issue_type": row[3],
            "description": row[4],
            "decision_type": row[5],
            "rationale": row[6],
            "decision_json": _load_json(row[7]),
        }
        for row in decisions
    ]
    accepted = [row for row in decision_rows if row["decision_type"] == "accept"]
    rejected = [row for row in decision_rows if row["decision_type"] == "reject"]
    request = content_layer.proofread_explanation_request(
        run_id=run_id,
        draft_version_no=draft.get("version_no"),
        decision_rows=decision_rows,
    )
    request["project_id"] = project_id
    request["cycle_no"] = cycle_no
    request["task_id"] = task_id
    return request


def _apply_proofread_rule_decision(run_id: str, task_id: str, decision_data: dict) -> dict:
    draft = _latest_draft_version(run_id)
    open_issues = _proofread_issue_rows(run_id, ("open",))
    accepted = []
    rejected = []
    patch_instructions = []
    blocker_open = []
    blocker_closed = []
    required_actions = set()
    for issue in open_issues:
        decision_id = f"pfd-{uuid.uuid4().hex[:10]}"
        issue_decision = _evaluate_proofread_issue(issue)
        decision_type = issue_decision["decision_type"]
        rationale = issue_decision["rationale"]
        required_actions.update(issue_decision["required_actions"])
        execute(
            """
            INSERT INTO proofread_decisions(decision_id, run_id, issue_id, decided_by, decision_type, rationale, decision_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb)
            """,
            (
                decision_id,
                run_id,
                issue["issue_id"],
                MANAGER_AGENT_ID,
                decision_type,
                rationale,
                jdump({"draft_version_no": draft.get("version_no"), **issue_decision}),
            ),
        )
        if decision_type == "accept":
            patch_instruction = issue_decision["patch_instruction"]
            patch_id = f"rpp-{uuid.uuid4().hex[:10]}"
            execute(
                """
                INSERT INTO revision_patches(
                    patch_id, run_id, decision_id, issue_id, target_section, patch_instruction, patch_payload, applied_by, source_task_id
                )
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)
                """,
                (
                    patch_id,
                    run_id,
                    decision_id,
                    issue["issue_id"],
                    issue["section"],
                    patch_instruction,
                    jdump({"issue_type": issue["issue_type"], "required_actions": issue_decision["required_actions"]}),
                    MANAGER_AGENT_ID,
                    task_id,
                ),
            )
            execute(
                "UPDATE proofread_issues SET status='accepted', updated_at=NOW(), resolution_note=%s WHERE issue_id=%s",
                (patch_instruction, issue["issue_id"]),
            )
            accepted.append(f"{issue['section']}：{patch_instruction}")
            patch_instructions.append(patch_instruction)
            if issue_decision["blocker_open"]:
                blocker_open.append(issue["issue_id"])
        else:
            execute(
                "UPDATE proofread_issues SET status='rejected', updated_at=NOW(), resolution_note=%s WHERE issue_id=%s",
                (rationale, issue["issue_id"]),
            )
            rejected.append(f"{issue['section']}：{issue['description']}")
            if issue["severity"] == "blocker":
                blocker_closed.append(issue["issue_id"])
    body_md = "\n".join(
        [
            "# Proofread Rule Decision",
            "",
            "## 已采纳 blocker / high issue",
            *( [f"- {item}" for item in accepted] if accepted else ["- 无"] ),
            "",
            "## 暂不采纳",
            *( [f"- {item}" for item in rejected] if rejected else ["- 无"] ),
            "",
            "## Required Actions",
            *( [f"- {item}" for item in sorted(required_actions)] if required_actions else ["- 无"] ),
        ]
    )
    files = _write_aux_report_files(
        run_id,
        "proofread_rule_decision",
        "Proofread Rule Decision",
        body_md,
        {
            "accepted": accepted,
            "rejected": rejected,
            "patch_instructions": patch_instructions,
            "accepted_count": len(accepted),
            "rejected_count": len(rejected),
            "blocker_open": blocker_open,
            "blocker_closed": blocker_closed,
            "required_actions": sorted(required_actions),
            "blocker_count_after_decision": _active_blocker_count(run_id),
            "recheck_required": bool(accepted),
            "generation_mode": "rule",
        },
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{proofread_rule_decision_html}', to_jsonb(%s::text), true),
                    '{proofread_blocker_count_after_decision}', to_jsonb(%s::int), true
                ),
                '{proofread_required_actions}', %s::jsonb, true
            )
        WHERE run_id=%s
        """,
        (files["html_path"], _active_blocker_count(run_id), jdump(sorted(required_actions)), run_id),
    )
    return {
        "status": "decided",
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "patch_instructions": patch_instructions,
        "accepted": accepted,
        "rejected": rejected,
        "requires_patch": bool(accepted),
        "blocker_open": blocker_open,
        "blocker_closed": blocker_closed,
        "required_actions": sorted(required_actions),
        "blocker_count_after_decision": _active_blocker_count(run_id),
        "recheck_required": bool(accepted),
        "html_path": files["html_path"],
        "generation_mode": "rule",
        "message_body": "",
    }


def decide_proofread_issues(run_id: str, task_id: str) -> dict:
    return _apply_proofread_rule_decision(run_id, task_id, {})


def _apply_proofread_explanation_result(run_id: str, task_id: str, explanation_data: dict) -> dict:
    summary = _require_llm_visible_text(
        explanation_data,
        field="summary",
        node_type="proofread.decision.explanation",
    )
    body_md = _reject_template_shell(
        _require_llm_visible_text(
            explanation_data,
            field="explanation_markdown",
            node_type="proofread.decision.explanation",
        ),
        node_type="proofread.decision.explanation",
        markers=[
            "## 已采纳",
            "## 暂不采纳",
            "## Required Actions",
            "已采纳：",
            "暂不采纳：",
            "Required Actions：",
        ],
    )
    accepted = _clean_string_list(explanation_data.get("accepted"), limit=6)
    rejected = _clean_string_list(explanation_data.get("rejected"), limit=6)
    required_actions = _clean_string_list(explanation_data.get("required_actions"), limit=8)
    files = _write_aux_report_files(
        run_id,
        "proofread_decision_explanation",
        "Proofread Decision Explanation",
        body_md,
        {
            "summary": summary,
            "accepted": accepted,
            "rejected": rejected,
            "required_actions": required_actions,
            "explanation_markdown": body_md,
            "generation_mode": explanation_data.get("generation_mode", ""),
            "generation_error": explanation_data.get("generation_error", ""),
            "timeout_ms": explanation_data.get("timeout_ms"),
            "prompt_size": explanation_data.get("prompt_size"),
            "input_size": explanation_data.get("input_size"),
            "evidence_object_count": explanation_data.get("evidence_object_count"),
            "started_at": explanation_data.get("started_at"),
            "finished_at": explanation_data.get("finished_at"),
        },
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(
                        jsonb_set(
                            jsonb_set(COALESCE(notes, '{}'::jsonb), '{proofread_decision_explanation_md}', to_jsonb(%s::text), true),
                            '{proofread_decision_explanation_json}', to_jsonb(%s::text), true
                        ),
                        '{proofread_decision_explanation_html}', to_jsonb(%s::text), true
                    ),
                    '{proofread_decision_html}', to_jsonb(%s::text), true
                ),
                '{proofread_decision_explanation_mode}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (
            files["markdown_path"],
            files["json_path"],
            files["html_path"],
            files["html_path"],
            explanation_data.get("generation_mode", "fallback"),
            run_id,
        ),
    )
    return {
        "status": "explained",
        "summary": summary,
        "markdown_path": files["markdown_path"],
        "html_path": files["html_path"],
        "generation_mode": explanation_data.get("generation_mode", ""),
        "generation_error": explanation_data.get("generation_error", ""),
        "timeout_ms": explanation_data.get("timeout_ms"),
        "prompt_size": explanation_data.get("prompt_size"),
        "input_size": explanation_data.get("input_size"),
        "evidence_object_count": explanation_data.get("evidence_object_count"),
        "started_at": explanation_data.get("started_at"),
        "finished_at": explanation_data.get("finished_at"),
        "message_body": summary,
    }


def summarize_draft_review(run_id: str) -> dict:
    rows = fetch_all(
        "SELECT agent_id, section_scope, review_text FROM draft_reviews WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    request = content_layer.draft_review_summary_request(
        run_id=run_id,
        draft_reviews=[{"agent_id": agent_id, "section_scope": scope, "review_text": text} for agent_id, scope, text in rows],
    )
    result = _run_content_request(request)
    body_md = _reject_template_shell(
        _require_llm_visible_text(result, field="summary_markdown", node_type="discussion.summary"),
        node_type="discussion.summary",
        markers=[
            "主要问题：",
            "修订重点：",
            "统一建议：",
        ],
    )
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "draft_review_summary.md"
    json_path = run_dir / "draft_review_summary.json"
    md_path.write_text(body_md)
    payload = {
        "run_id": run_id,
        "accepted_review_notes": _clean_string_list(result.get("accepted_review_notes"), limit=6),
        "revision_focus": _clean_string_list(result.get("revision_focus"), limit=4),
        "summary": _require_llm_visible_text(result, field="summary", node_type="discussion.summary"),
        "summary_markdown": body_md,
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    html_path = write_product_report_html(run_id, "Draft Review Brief", body_md, payload, "draft_review_summary")
    execute("UPDATE outputs SET revision_plan=%s, updated_at=NOW() WHERE run_id=%s", (body_md, run_id))
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{draft_review_summary_md}', to_jsonb(%s::text), true),
                    '{draft_review_summary_json}', to_jsonb(%s::text), true
                ),
                '{draft_review_summary_html}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (str(md_path), str(json_path), str(html_path), run_id),
    )
    return {
        "summary_text": body_md,
        "revision_plan": body_md,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "message_body": payload["summary"],
    }


def manager_review_materials(run_id: str, task_id: str, section: str) -> dict:
    row = fetch_one(
        """
        SELECT approved, reason, selected_material_ids::text, review_task_id
        FROM reviews
        WHERE run_id=%s AND section=%s
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (run_id, section),
    )
    if not row:
        raise RuntimeError(f"{section} 尚无 tester material review 结果")
    review_task = fetch_one(
        """
        SELECT retry_count, result::text
        FROM tasks
        WHERE task_id=%s
        """,
        (row[3],),
    )
    retry_count = _safe_int(review_task[0] if review_task else 0)
    review_result = _load_json(review_task[1]) if review_task and review_task[1] else {}
    review_decision = str(review_result.get("audit_decision") or review_result.get("gate_decision") or "").strip()
    thresholds_met = bool(review_result.get("thresholds_met"))
    returned_material_issues = review_result.get("returned_material_issues") or []
    gate_deficits = review_result.get("review_gate_deficits") or []
    partial_pass_count = _consecutive_manager_signal_count(
        run_id,
        "material.review",
        section=section,
        signal_types=(AUDIT_PARTIAL_PASS,),
    )
    redo_count = _redo_signal_count_since_progress(run_id, "material.review", section=section)
    forced_progress_issue = None
    if bool(row[0]) and review_decision == AUDIT_PROCEED and thresholds_met:
        signal = AUDIT_PROCEED
        decision_reason = row[1]
    elif redo_count >= MAX_AUDIT_REDO_BEFORE_FORCE_PROCEED:
        signal = AUDIT_FORCED_PROCEED
        decision_reason = "同一板块已连续两次 redo，第三个大版本仍未通过，manager 强制带风险推进并要求在 retrospective 中强制优化。"
        forced_progress_issue = _record_forced_progress_issue(
            run_id,
            stage_name="material.review",
            section=section,
            title=f"{section} 板块素材审核连续 redo 后强制推进",
            reason=decision_reason,
            payload={
                "review_task_id": row[3],
                "retry_count": retry_count,
                "review_decision": review_decision,
                "thresholds_met": thresholds_met,
                "returned_material_issues": returned_material_issues,
                "gate_deficits": gate_deficits,
            },
        )
    elif review_decision == AUDIT_REDO or partial_pass_count >= MAX_AUDIT_PARTIAL_PASS_BEFORE_REDO:
        signal = AUDIT_REDO
        decision_reason = row[1]
    else:
        signal = AUDIT_PARTIAL_PASS
        decision_reason = row[1]
    event = _manager_control_event(
        run_id=run_id,
        stage_name="material.review",
        section=section,
        signal_type=signal,
        payload={
            "approved": bool(row[0]),
            "reason": decision_reason,
            "review_decision": review_decision,
            "thresholds_met": thresholds_met,
            "selected_material_ids": _load_json(row[2]),
            "review_task_id": row[3],
            "retry_count": retry_count,
            "returned_material_issues": returned_material_issues,
            "review_gate_deficits": gate_deficits,
            "mandatory_retrospective_optimization": bool(forced_progress_issue),
            "forced_progress_issue": forced_progress_issue or {},
        },
    )
    return {
        "status": "completed",
        "signal_type": signal,
        "event_id": event["event_id"],
        "reason": decision_reason,
        "message_body": "",
    }


def manager_publish_decision(run_id: str, task_id: str) -> dict:
    proofread_phase, proofread_round, proofread_gate_settled = _proofread_gate_settled(run_id)
    proofread_done = fetch_one(
        """
        SELECT COUNT(*)
        FROM tasks
        WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed'
        """,
        (run_id, proofread_phase, proofread_round),
    )[0]
    blocker_count = _active_blocker_count(run_id)
    draft_review_signal = _latest_manager_signal(run_id, "draft.review")
    forced_progress = bool(draft_review_signal and draft_review_signal["signal_type"] == AUDIT_FORCED_PROCEED)
    approved = bool(
        (proofread_gate_settled and int(proofread_done or 0) > 0 and blocker_count == 0)
        or forced_progress
    )
    if forced_progress:
        reason = "draft.review 已触发 forced_proceed；允许带风险发布，并要求在 retrospective 中强制优化。"
    else:
        reason = (
            "all blockers closed and active proofread gate settled"
            if approved
            else f"active proofread gate not settled or blockers unresolved ({proofread_phase} round {proofread_round})"
        )
    event = _manager_control_event(
        run_id=run_id,
        stage_name="publish.decision",
        signal_type="publish_approved" if approved else "pause",
        payload={
            "blocker_count": blocker_count,
            "proofread_phase": proofread_phase,
            "proofread_round": proofread_round,
            "proofread_gate_settled": proofread_gate_settled,
            "proofread_done": int(proofread_done or 0),
            "draft_review_signal": (draft_review_signal or {}).get("signal_type", ""),
            "forced_progress": forced_progress,
            "reason": reason,
        },
    )
    return {
        "status": "approved" if approved else "rejected",
        "approved": approved,
        "event_id": event["event_id"],
        "reason": reason,
        "message_body": "",
    }


def manager_pre_retro_review(run_id: str, task_id: str) -> dict:
    reports = {row["report_type"]: row for row in _product_report_rows(run_id)}
    missing = [name for name in ["product_test", "benchmark_report", "cross_cycle_compare_report"] if name not in reports]
    partial_pass_count = _consecutive_manager_signal_count(
        run_id,
        "pre-retro.review",
        signal_types=(AUDIT_PARTIAL_PASS,),
    )
    redo_count = _redo_signal_count_since_progress(run_id, "pre-retro.review")
    forced_progress_issue = None
    if not missing:
        signal = AUDIT_PROCEED
        reason = "tester 三份评估报告齐备，可进入 retrospective"
    elif redo_count >= MAX_AUDIT_REDO_BEFORE_FORCE_PROCEED:
        signal = AUDIT_FORCED_PROCEED
        reason = "pre-retro.review 已连续两次 redo，第三次仍缺少评估报告，manager 强制带风险推进并要求在 retrospective 中强制优化。"
        forced_progress_issue = _record_forced_progress_issue(
            run_id,
            stage_name="pre-retro.review",
            section="全局",
            title="评估报告未齐备但被强制推进到 retrospective",
            reason=reason,
            payload={"missing_reports": missing},
        )
    elif partial_pass_count >= MAX_AUDIT_PARTIAL_PASS_BEFORE_REDO:
        signal = AUDIT_REDO
        reason = f"pre-retro.review 连续 partial_pass 达到阈值，升级为 redo；当前缺少报告：{', '.join(missing)}"
    else:
        signal = AUDIT_PARTIAL_PASS
        reason = f"缺少报告：{', '.join(missing)}"
    event = _manager_control_event(
        run_id=run_id,
        stage_name="pre-retro.review",
        signal_type=signal,
        payload={
            "missing_reports": missing,
            "reason": reason,
            "mandatory_retrospective_optimization": bool(forced_progress_issue),
            "forced_progress_issue": forced_progress_issue or {},
        },
    )
    return {
        "status": "completed",
        "signal_type": signal,
        "event_id": event["event_id"],
        "reason": reason,
        "message_body": "",
    }


def summarize_discussion(run_id: str) -> dict:
    comments = fetch_all("SELECT agent_id, comment_text FROM discussions WHERE run_id=%s ORDER BY created_at", (run_id,))
    draft_reviews = fetch_all(
        "SELECT agent_id, section_scope, review_text FROM draft_reviews WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    product_eval = _product_report_by_type(run_id, "product_evaluation_report")
    top_issues = (product_eval.get("report_json", {}) or {}).get("top_product_issues", [])
    output_bundle = _load_output_bundle(run_id)
    latest = _latest_draft_version(run_id)
    request = content_layer.discussion_summary_request(
        run_id=run_id,
        draft_version_no=latest.get("version_no", 0),
        discussion_comments=[
            {"agent_id": agent_id, "comment_text": _truncate(comment_text, 180)}
            for agent_id, comment_text in comments
        ],
        draft_reviews=[
            {
                "agent_id": agent_id,
                "section_scope": section_scope,
                "review_text": _truncate(review_text, 180),
            }
            for agent_id, section_scope, review_text in draft_reviews
        ],
        top_product_issues=top_issues,
        revision_plan=_truncate(output_bundle.get("revision_plan"), 260),
    )
    result = _run_content_request(request)
    plan = _reject_template_shell(
        _require_llm_visible_text(result, field="summary_markdown", node_type="discussion.summary"),
        node_type="discussion.summary",
        markers=[
            "Discussion Summary",
            "Accepted Comments",
            "Rejected Comments",
            "Revision Actions",
        ],
    )
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "discussion_summary.md"
    json_path = run_dir / "discussion_summary.json"
    md_path.write_text(plan)
    summary_text = _require_llm_visible_text(result, field="summary", node_type="discussion.summary")
    payload = {
        "run_id": run_id,
        "top_issues": _clean_string_list(result.get("top_issues"), limit=5),
        "accepted_comments": _clean_string_list(result.get("accepted_comments"), limit=5),
        "rejected_comments": _clean_string_list(result.get("rejected_comments"), limit=5),
        "revision_actions": _clean_string_list(result.get("revision_actions"), limit=6),
        "summary": summary_text,
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
        "markdown_path": str(md_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    html_path = write_product_report_html(run_id, "Draft Discussion Brief", plan, payload, "discussion_summary")
    execute(
        """
        UPDATE outputs
        SET revision_plan=%s, updated_at=NOW()
        WHERE run_id=%s
        """,
        (plan, run_id),
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{discussion_summary_md}', to_jsonb(%s::text), true),
                    '{discussion_summary_json}', to_jsonb(%s::text), true
                ),
                '{discussion_summary_html}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (str(md_path), str(json_path), str(html_path), run_id),
    )
    return {
        "summary_text": plan,
        "revision_plan": plan,
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "message_body": summary_text,
    }


def _prepare_draft_revise_job(run_id: str, task: dict | None = None) -> dict:
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    project_id, cycle_no = row[0], row[1]
    latest = _latest_draft_version(run_id)
    task_payload = (task or {}).get("payload") or {}
    expected_source_draft_version_no = _payload_int(task_payload, "source_draft_version_no")
    latest_version_no = _safe_int(latest.get("version_no"))
    if expected_source_draft_version_no and latest_version_no != expected_source_draft_version_no:
        raise RuntimeError(
            f"draft.revise stale draft version: expected v{expected_source_draft_version_no}, latest is v{latest_version_no}"
        )
    sections_payload = latest.get("report_json") or {}
    active_issues = _proofread_issue_rows(run_id, ("accepted", "open"))
    touched_sections = sorted({issue["section"] for issue in active_issues if issue.get("section")})
    approved_materials = _approved_material_lookup_for_sections(run_id, touched_sections)
    request = content_layer.draft_revise_request(
        run_id=run_id,
        draft_version_no=latest.get("version_no"),
        writer_guidance=_writer_guidance_settings(get_effective_optimization_log(project_id, MANAGER_AGENT_ID, cycle_no or 0)),
        current_draft_sections=[
            _compact_draft_section_for_revision(section, sections_payload.get(section) or {})
            for section in touched_sections
        ],
        revision_patches=[
            {
                "issue_id": issue["issue_id"],
                "section": issue["section"],
                "item_ref": issue.get("item_ref"),
                "severity": issue.get("severity"),
                "issue_type": issue.get("issue_type"),
                "status": issue.get("status"),
                "description": _truncate(issue.get("description"), 96),
                "patch_instruction": _truncate(_default_patch_instruction_for_issue(issue), 96),
                "required_actions": _proofread_required_actions(issue),
                "recheck_note": _truncate(issue.get("resolution_note"), 96),
                "target_material": _compact_prompt_object(
                    approved_materials.get(issue["section"], {}).get(
                        int(
                            (
                                (issue.get("evidence") or {}).get("material_snapshot") or {}
                            ).get("material_id")
                            or (issue.get("evidence") or {}).get("material_id")
                            or 0
                        )
                    )
                    or ((issue.get("evidence") or {}).get("material_snapshot") or {}),
                    title_limit=88,
                    summary_limit=120,
                    note_limit=60,
                ),
                "current_item": _compact_recheck_evidence(issue.get("evidence") or {}),
            }
            for issue in active_issues
        ],
    )
    request["project_id"] = project_id
    request["cycle_no"] = cycle_no
    request["task_id"] = None
    request["source_draft_version_no"] = expected_source_draft_version_no or latest_version_no
    return request


def _apply_draft_revise_result(run_id: str, revise_data: dict, task_id: str | None = None) -> dict:
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    project_id, cycle_no = row[0], row[1]
    latest = _latest_draft_version(run_id)
    source_draft_version_no = _safe_int(latest.get("version_no"))
    sections_payload = latest.get("report_json") or {}
    active_issues = _proofread_issue_rows(run_id, ("accepted", "open"))
    approved_materials = _approved_material_lookup_for_sections(run_id, list(sections_payload.keys()))
    updates_by_section = {item["section"]: item for item in (revise_data.get("section_updates") or []) if item.get("section")}
    applied = []
    for section, section_update in updates_by_section.items():
        section_data = json.loads(json.dumps(sections_payload.get(section) or {}, ensure_ascii=False))
        for item_update in section_update.get("item_updates") or []:
            item_ref = (item_update.get("item_ref") or "").strip()
            material_id_raw = item_update.get("material_id")
            try:
                material_id = int(material_id_raw)
            except Exception:
                material_id = 0
            title = (item_update.get("title") or "").strip()
            summary_zh = (item_update.get("summary_zh") or "").strip()
            source_media = (item_update.get("source_media") or "").strip()
            published_at = (item_update.get("published_at") or "").strip()
            link = (item_update.get("link") or "").strip()

            def _merge_item(existing_item: dict) -> dict:
                target = dict(existing_item or {})
                if material_id:
                    source_item = approved_materials.get(section, {}).get(material_id)
                    if source_item:
                        target = json.loads(json.dumps(source_item, ensure_ascii=False))
                    target["material_id"] = material_id
                if title:
                    target["title"] = title
                if summary_zh:
                    target["summary_zh"] = summary_zh
                if source_media:
                    target["source_media"] = source_media
                if published_at:
                    target["published_at"] = published_at
                if link:
                    target["link"] = link
                return target

            if item_ref == "main":
                main = _merge_item(section_data.get("main") or {})
                if not main:
                    continue
                section_data["main"] = main
                continue
            if item_ref.startswith("secondary:") or item_ref.startswith("secondary_"):
                try:
                    idx = int(re.split(r"[:_]", item_ref, maxsplit=1)[1]) - 1
                except Exception:
                    continue
                items = list(section_data.get("secondary") or [])
                if idx < 0 or idx >= len(items):
                    continue
                items[idx] = _merge_item(items[idx])
                section_data["secondary"] = items
                continue
            if item_ref.startswith("brief:") or item_ref.startswith("brief_"):
                try:
                    idx = int(re.split(r"[:_]", item_ref, maxsplit=1)[1]) - 1
                except Exception:
                    continue
                items = list(section_data.get("briefs") or [])
                if idx < 0 or idx >= len(items):
                    continue
                items[idx] = _merge_item(items[idx])
                section_data["briefs"] = items
        if section_update.get("main_summary") and (section_data.get("main") or {}):
            main = dict(section_data.get("main") or {})
            main["summary_zh"] = section_update.get("main_summary")
            section_data["main"] = main
        sections_payload[section] = section_data
        if section_update.get("reason"):
            applied.append(section_update.get("reason"))
    sections_payload = _apply_writer_guidance(sections_payload, get_effective_optimization_log(project_id, MANAGER_AGENT_ID, cycle_no or 0))
    revision_plan = _require_llm_visible_text(revise_data, field="revision_plan", node_type="draft.revise")
    render_data = _render_draft_markdown_via_llm(
        run_id=run_id,
        stage_name="draft.revise",
        draft_version_no=(latest.get("version_no") or 0) + 1,
        sections_payload=sections_payload,
        writer_guidance=_writer_guidance_settings(get_effective_optimization_log(project_id, MANAGER_AGENT_ID, cycle_no or 0)),
        revision_context=[
            {
                "issue_id": issue["issue_id"],
                "section": issue["section"],
                "issue_type": issue["issue_type"],
                "description": issue["description"],
            }
            for issue in active_issues
        ],
    )
    final_markdown = render_data["report_markdown"]
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "revised_final_report.md"
    json_path = run_dir / "revised_final_report.json"
    md_path.write_text(final_markdown)
    json_path.write_text(json.dumps(sections_payload, ensure_ascii=False, indent=2))
    execute(
        "UPDATE outputs SET final_markdown=%s, project_id=%s, cycle_no=%s, updated_at=NOW() WHERE run_id=%s",
        (final_markdown, project_id, cycle_no, run_id),
    )
    execute(
        "UPDATE outputs SET revision_plan=%s, final_json=%s::jsonb, updated_at=NOW() WHERE run_id=%s",
        (revision_plan, jdump(sections_payload), run_id),
    )
    version = _record_draft_version(
        run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        stage="revised",
        created_by="editor",
        markdown_text=final_markdown,
        report_json=sections_payload,
        source_task_id=task_id,
    )
    html_path = write_product_report_html(
        run_id,
        "Revised Final Report",
        final_markdown,
        {"run_id": run_id, "markdown_path": str(md_path), "json_path": str(json_path)},
        "revised_final_report",
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{revised_report_md}', to_jsonb(%s::text), true),
                    '{revised_report_json}', to_jsonb(%s::text), true
                ),
                '{revised_report_html}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (str(md_path), str(json_path), str(html_path), run_id),
    )
    for issue in active_issues:
        execute(
            "UPDATE proofread_issues SET status='fixed', updated_at=NOW(), closed_at=NULL, resolution_note='' WHERE issue_id=%s",
            (issue["issue_id"],),
        )
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "draft_version_no": version["version_no"],
        "source_draft_version_no": source_draft_version_no,
        "generation_mode": render_data["generation_mode"],
        "generation_error": render_data.get("generation_error", ""),
        "update_generation_mode": revise_data.get("generation_mode", ""),
        "update_generation_error": revise_data.get("generation_error", ""),
        "timeout_ms": revise_data.get("timeout_ms"),
        "prompt_size": revise_data.get("prompt_size"),
        "input_size": revise_data.get("input_size"),
        "evidence_object_count": revise_data.get("evidence_object_count"),
        "started_at": revise_data.get("started_at"),
        "finished_at": render_data.get("finished_at") or revise_data.get("finished_at"),
        "message_body": render_data["summary"],
    }


def revise_draft(run_id: str, task_id: str | None = None) -> dict:
    task = {"task_id": task_id, "payload": _task_payload_json(task_id)} if task_id else None
    prepared = _prepare_draft_revise_job(run_id, task)
    revise_data = _run_content_request(prepared)
    return _apply_draft_revise_result(run_id, revise_data, task_id)


def publish_report(run_id: str) -> dict:
    proofread_phase, proofread_round, proofread_gate_settled = _proofread_gate_settled(run_id)
    proofread_done = fetch_one(
        """
        SELECT COUNT(*) FROM tasks
        WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed'
        """,
        (run_id, proofread_phase, proofread_round),
    )[0]
    blocker_count = _active_blocker_count(run_id)
    if not proofread_gate_settled:
        raise RuntimeError(
            f"active proofread gate 尚未收敛（{proofread_phase} round {proofread_round}），禁止 publish"
        )
    if proofread_done < 1:
        raise RuntimeError(f"{proofread_phase} round {proofread_round} 尚未完成，禁止 publish")
    if blocker_count > 0:
        raise RuntimeError(f"proofread blocker 未清零，当前仍有 {blocker_count} 个 blocker，禁止 publish")
    closed_blockers = fetch_one(
        """
        SELECT COUNT(*) FROM proofread_issues
        WHERE run_id=%s AND severity='blocker' AND status='closed'
        """,
        (run_id,),
    )[0]
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    project_id, cycle_no = row[0], row[1]
    latest = _latest_draft_version(run_id)
    final_markdown = latest.get("markdown_text") or ""
    final_json = latest.get("report_json") or {}
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "final_report.md"
    json_path = run_dir / "final_report.json"
    md_path.write_text(final_markdown or "")
    json_path.write_text(json.dumps(final_json, ensure_ascii=False, indent=2))
    html_path = write_final_report_html(run_id)
    final_artifact_id = f"artifact-{run_id}"
    artifact_manifest = {
        "final_artifact_id": final_artifact_id,
        "rendered_html_path": str(html_path),
        "rendered_md_path": str(md_path),
        "rendered_json_path": str(json_path),
        "publish_status": "completed",
    }
    execute(
        """
        INSERT INTO final_reports(run_id, project_id, cycle_no, source_draft_version_id, markdown_text, report_json, published_by)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb,'editor')
        ON CONFLICT (run_id) DO UPDATE
        SET project_id=EXCLUDED.project_id,
            cycle_no=EXCLUDED.cycle_no,
            source_draft_version_id=EXCLUDED.source_draft_version_id,
            markdown_text=EXCLUDED.markdown_text,
            report_json=EXCLUDED.report_json,
            published_by='editor',
            published_at=NOW()
        """,
        (run_id, project_id, cycle_no, latest.get("draft_version_id"), final_markdown, jdump(final_json)),
    )
    execute(
        "UPDATE outputs SET final_markdown=%s, final_json=%s::jsonb, updated_at=NOW() WHERE run_id=%s",
        (final_markdown, jdump(final_json), run_id),
    )
    if project_id:
        execute(
            """
            UPDATE workflow_runs
            SET status='running', current_phase='report.publish', completed_at=NULL,
                report_markdown_path=%s, report_json_path=%s,
                notes =
                    jsonb_set(
                        jsonb_set(
                            COALESCE(notes, '{}'::jsonb),
                            '{final_report_html}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{publish_gate}',
                        %s::jsonb,
                        true
                    )
            WHERE run_id=%s
            """,
            (
                str(md_path),
                str(json_path),
                str(html_path),
                jdump(
                    {
                        "proofread_round": proofread_round,
                        "recheck_done": int(proofread_done),
                        "blocker_count": int(blocker_count),
                        "closed_blocker_count": int(closed_blockers),
                        "reason": "all blockers closed and recheck passed",
                        "artifact_manifest": artifact_manifest,
                    }
                ),
                run_id,
            ),
        )
    else:
        execute(
            """
            UPDATE workflow_runs
            SET status='completed', current_phase='report.publish', completed_at=NOW(),
                report_markdown_path=%s, report_json_path=%s,
                notes =
                    jsonb_set(
                        jsonb_set(
                            COALESCE(notes, '{}'::jsonb),
                            '{final_report_html}',
                            to_jsonb(%s::text),
                            true
                        ),
                        '{publish_gate}',
                        %s::jsonb,
                        true
                    )
            WHERE run_id=%s
            """,
            (
                str(md_path),
                str(json_path),
                str(html_path),
                jdump(
                    {
                        "proofread_round": proofread_round,
                        "recheck_done": int(proofread_done),
                        "blocker_count": int(blocker_count),
                        "closed_blocker_count": int(closed_blockers),
                        "reason": "all blockers closed and recheck passed",
                        "artifact_manifest": artifact_manifest,
                    }
                ),
                run_id,
            ),
        )
    return {
        "final_artifact_id": final_artifact_id,
        "final_report_path": str(md_path),
        "rendered_html_path": str(html_path),
        "rendered_md_path": str(md_path),
        "rendered_json_path": str(json_path),
        "artifact_manifest": artifact_manifest,
        "publish_timestamp": now_iso(),
        "publish_status": "completed",
        "publish_gate_reason": "all blockers closed and recheck passed",
        "proofread_round": proofread_round,
        "recheck_done": int(proofread_done),
        "blocker_count": int(blocker_count),
        "closed_blocker_count": int(closed_blockers),
        "message_body": "",
    }


def recheck_proofread_issues(run_id: str, task_id: str, agent_id: str) -> dict:
    latest = _latest_draft_version(run_id)
    task_payload = _task_payload_json(task_id)
    expected_draft_version_no = _payload_int(task_payload, "draft_version_no")
    latest_version_no = _safe_int(latest.get("version_no"))
    if expected_draft_version_no and latest_version_no != expected_draft_version_no:
        return _draft_version_mismatch_result(
            latest_version_no=latest_version_no,
            expected_version_no=expected_draft_version_no,
            version_field="draft_version_no",
        )
    report_json = latest.get("report_json") or {}
    sections = AGENT_SECTIONS.get(agent_id, [])
    rows = fetch_all(
        """
        SELECT issue_id, section, item_ref, severity, issue_type, status, description, evidence::text
        FROM proofread_issues
        WHERE run_id=%s AND section = ANY(%s) AND status IN ('accepted', 'fixed', 'open')
        ORDER BY opened_at
        """,
        (run_id, sections),
    )
    draft_sections = {}
    prompt_issues = [
        {
            "issue_id": issue_id,
            "section": section,
            "item_ref": item_ref,
            "severity": severity,
            "issue_type": issue_type,
            "status": status,
            "description": _truncate(description, 160),
            "evidence": _compact_recheck_evidence(_load_json(evidence_text)),
        }
        for issue_id, section, item_ref, severity, issue_type, status, description, evidence_text in rows
    ]
    for _, section, _, _, _, _, _, _ in rows:
        if section not in draft_sections:
            draft_sections[section] = _compact_draft_section_for_revision(section, report_json.get(section) or {})
    result = _run_content_request(
        content_layer.draft_recheck_request(
            run_id=run_id,
            agent_id=agent_id,
            draft_version_no=latest.get("version_no"),
            issues=prompt_issues,
            draft_sections=list(draft_sections.values()),
            reference_time=now_local().isoformat(),
            publication_requirements=_publication_requirements(run_id),
        )
    )
    summary_text = _require_llm_visible_text(result, field="summary", node_type="draft.recheck")
    decisions = {}
    for item in result.get("decisions") or []:
        issue_id = str(item.get("issue_id") or "").strip()
        if issue_id:
            decisions[issue_id] = item
    closed = []
    reopened = []
    for issue_id, section, item_ref, severity, issue_type, status, description, evidence_text in rows:
        decision = decisions.get(issue_id, {})
        if not decision:
            raise RuntimeError(f"draft.recheck missing llm decision for issue_id={issue_id}")
        resolved = _require_llm_bool(
            decision | {"generation_mode": result.get("generation_mode", "")},
            field="resolved",
            node_type="draft.recheck",
        )
        note = str(decision.get("resolution_note") or "").strip()
        if not note:
            raise RuntimeError(f"draft.recheck missing llm resolution_note for issue_id={issue_id}")
        if resolved:
            execute(
                """
                UPDATE proofread_issues
                SET status='closed', updated_at=NOW(), closed_at=NOW(), resolution_note=%s
                WHERE issue_id=%s
                """,
                (note, issue_id),
            )
            closed.append(issue_id)
        else:
            execute(
                """
                UPDATE proofread_issues
                SET status='open', updated_at=NOW(), resolution_note=%s
                WHERE issue_id=%s
                """,
                (note, issue_id),
            )
            reopened.append(issue_id)
    return {
        "status": "rechecked",
        "draft_version_no": latest_version_no,
        "closed_issues": closed,
        "reopened_issues": reopened,
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
        "message_body": summary_text,
    }


def _prepare_product_test_job(run_id: str, task_id: str, agent_id: str) -> dict:
    bundle = _load_output_bundle(run_id)
    final_json = bundle.get("final_json") or {}
    evidence = []
    for section in (_publication_requirements(run_id).get("section_order") or ["政治经济", "科技", "体育娱乐", "其他"]):
        section_data = final_json.get(section) or {}
        slot_items = []
        main = section_data.get("main") or {}
        if main:
            slot_items.append(("main", main))
        slot_items.extend(("secondary", item) for item in (section_data.get("secondary") or []))
        slot_items.extend(("brief", item) for item in (section_data.get("briefs") or []))
        for kind, item in slot_items:
            evidence.append(
                {
                    "section": section,
                    "kind": kind,
                    "title": item.get("title", ""),
                    "summary": item.get("summary_zh", "")[:140],
                    "image_count": len(item.get("images") or []),
                    "source_media": item.get("source_media", ""),
                    "published_at": item.get("published_at", ""),
                }
            )
    request = content_layer.product_test_request(run_id=run_id, agent_id=agent_id, evidence=evidence)
    request["project_id"] = get_run_project_context(run_id)[0]
    request["cycle_no"] = get_run_project_context(run_id)[1]
    request["task_id"] = task_id
    request["evidence"] = evidence
    request["agent_id"] = agent_id
    return request


def _apply_product_test_result(run_id: str, task_id: str, agent_id: str, evidence: list[dict], decision: dict) -> dict:
    focus = _require_llm_visible_text(decision, field="focus", node_type="product.test")
    reader_findings = _require_llm_string_list(
        decision,
        field="reader_findings",
        node_type="product.test",
        min_items=1,
        limit=4,
    )
    reader_improvements = _require_llm_string_list(
        decision,
        field="reader_improvement_opportunities",
        node_type="product.test",
        min_items=1,
        limit=4,
    )
    title = f"{agent_id} 产品测试报告"
    summary = _require_llm_visible_text(decision, field="summary", node_type="product.test")
    body_md = _reject_template_shell(
        _require_llm_visible_text(decision, field="report_markdown", node_type="product.test"),
        node_type="product.test",
        markers=[
            "最明显问题：",
            "优先改进：",
            "执行关联：",
            "责任归因：",
        ],
    )
    payload = {
        "run_id": run_id,
        "agent_id": agent_id,
        "focus": focus,
        "evidence": evidence,
        "reader_findings": reader_findings,
        "reader_improvement_opportunities": reader_improvements,
        "summary": summary,
        "report_markdown": body_md,
        "generation_mode": decision["generation_mode"],
        "generation_error": decision.get("generation_error", ""),
        "timeout_ms": decision.get("timeout_ms"),
        "prompt_size": decision.get("prompt_size"),
        "input_size": decision.get("input_size"),
        "evidence_object_count": decision.get("evidence_object_count"),
        "started_at": decision.get("started_at"),
        "finished_at": decision.get("finished_at"),
    }
    files = _write_aux_report_files(run_id, f"product_test_{agent_id}", title, body_md, payload)
    project_id, cycle_no = get_run_project_context(run_id)
    _insert_product_report(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        agent_id=agent_id,
        report_type="product_test",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def create_product_test_report(run_id: str, task_id: str, agent_id: str) -> dict:
    prepared = _prepare_product_test_job(run_id, task_id, agent_id)
    decision = _run_content_request(prepared)
    return _apply_product_test_result(run_id, task_id, agent_id, prepared["evidence"], decision)


def create_benchmark_report(run_id: str, task_id: str) -> dict:
    bundle = _load_output_bundle(run_id)
    final_json = bundle.get("final_json") or {}
    focus_terms = []
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        main = (final_json.get(section) or {}).get("main") or {}
        if main.get("title"):
            focus_terms.append(main["title"])
    query = "international news roundup world headlines digest layout"
    if focus_terms:
        query = f"{focus_terms[0]} international news roundup page"
    search_results = search_benchmark_samples(query, 4)
    samples = []
    search_mode = "open_search"
    if search_results:
        for item in search_results[:4]:
            samples.append(
                {
                    "name": item["source_media"] or item["title"],
                    "url": item["link"],
                    "page_title": item["title"],
                    "snippet": item.get("snippet", ""),
                    "source_media": item["source_media"],
                }
            )
    else:
        search_mode = "fallback"
        for name, url in BENCHMARK_URLS:
            try:
                resp = HTTP.get(url, timeout=12)
                resp.raise_for_status()
                title = ""
                text = resp.text
                start = text.lower().find("<title>")
                end = text.lower().find("</title>")
                if start >= 0 and end > start:
                    title = text[start + 7 : end].strip()
                samples.append(
                    {
                        "name": name,
                        "url": url,
                        "page_title": title[:140],
                        "snippet": "",
                        "source_media": name,
                    }
                )
            except Exception as exc:
                samples.append(
                    {
                        "name": name,
                        "url": url,
                        "page_title": "",
                        "snippet": f"抓取失败：{exc}",
                        "source_media": name,
                    }
                )
    final_slices = []
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        main = ((final_json.get(section) or {}).get("main") or {})
        if main:
            final_slices.append(
                {
                    "section": section,
                    "title": main.get("title", ""),
                    "summary_zh": main.get("summary_zh", ""),
                    "image_count": len(main.get("images") or []),
                }
            )
    request = content_layer.benchmark_request(
        run_id=run_id,
        benchmark_mode=search_mode,
        search_query=query,
        final_artifact=final_slices,
        samples=samples,
    )
    result = _run_content_request(request)
    if result.get("generation_mode") != "llm":
        raise RuntimeError(
            "product.benchmark requires llm-generated visible content; "
            f"error={result.get('generation_error') or ''}"
        )
    comparisons = result.get("comparisons") or []
    concise_actions = _require_llm_string_list(
        result,
        field="next_cycle_actions",
        node_type="product.benchmark",
        min_items=1,
        limit=5,
    )
    summary = _require_llm_visible_text(result, field="summary", node_type="product.benchmark")
    title = "tester 外部对标报告"
    body_md = _reject_template_shell(
        _require_llm_visible_text(result, field="report_markdown", node_type="product.benchmark"),
        node_type="product.benchmark",
        markers=[
            "对标样本：",
            "最明显差距：",
            "下一轮建议：",
        ],
    )
    payload = {
        "run_id": run_id,
        "benchmark_mode": search_mode,
        "search_query": query,
        "comparisons": comparisons,
        "most_visible_gap": _require_llm_visible_text(result, field="most_visible_gap", node_type="product.benchmark"),
        "next_cycle_actions": concise_actions,
        "summary": summary,
        "report_markdown": body_md,
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
    }
    files = _write_aux_report_files(run_id, "benchmark_report", title, body_md, payload)
    project_id, cycle_no = get_run_project_context(run_id)
    _insert_product_report(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        agent_id="tester",
        report_type="benchmark_report",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def create_cross_cycle_compare_report(run_id: str, task_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    current_final = (_load_output_bundle(run_id).get("final_json") or {})
    prev_row = fetch_one(
        """
        SELECT run_id FROM workflow_runs
        WHERE project_id=%s AND cycle_no=%s
        """,
        (project_id, (cycle_no or 1) - 1),
    ) if project_id and cycle_no and cycle_no > 1 else None
    previous_run_id = prev_row[0] if prev_row else None
    previous_final = (_load_output_bundle(previous_run_id).get("final_json") or {}) if previous_run_id else {}
    previous_summary = ""
    if project_id and cycle_no and cycle_no > 1:
        row = fetch_one(
            "SELECT retrospective_summary FROM project_cycles WHERE project_id=%s AND cycle_no=%s",
            (project_id, cycle_no - 1),
        )
        previous_summary = row[0] if row and row[0] else ""
    sections_payload = []
    current_sections = sorted(current_final.keys())
    for section in current_sections:
        cur_main = ((current_final.get(section) or {}).get("main") or {})
        prev_main = ((previous_final.get(section) or {}).get("main") or {})
        sections_payload.append(
            {
                "section": section,
                "current_main": {
                    "title": cur_main.get("title", ""),
                    "summary_zh": cur_main.get("summary_zh", ""),
                    "image_count": len(cur_main.get("images") or []),
                },
                "previous_main": {
                    "title": prev_main.get("title", ""),
                    "summary_zh": prev_main.get("summary_zh", ""),
                    "image_count": len(prev_main.get("images") or []),
                },
            }
        )
    request = content_layer.cross_cycle_compare_request(
        run_id=run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        previous_run_id=previous_run_id,
        sections=sections_payload,
        previous_retrospective_summary=previous_summary,
    )
    result = _run_content_request(request)
    summary = _require_llm_visible_text(result, field="summary", node_type="product.cross_cycle_compare")
    title = "tester 跨轮对比报告"
    payload = {
        "run_id": run_id,
        "previous_run_id": previous_run_id,
        "improved_issues": _clean_string_list(result.get("improved_issues"), limit=5),
        "unimproved_issues": _clean_string_list(result.get("unimproved_issues"), limit=5),
        "regressed_areas": _clean_string_list(result.get("regressed_areas"), limit=5),
        "unimplemented_previous_optimization_suggestions": _clean_string_list(
            result.get("unimplemented_previous_optimization_suggestions"),
            limit=5,
        ),
        "summary": summary,
        "report_markdown": _reject_template_shell(
            _require_llm_visible_text(result, field="report_markdown", node_type="product.cross_cycle_compare"),
            node_type="product.cross_cycle_compare",
            markers=[
                "改善：",
                "未改善：",
                "退步：",
                "未落实建议：",
            ],
        ),
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
    }
    body_md = payload["report_markdown"]
    files = _write_aux_report_files(run_id, "cross_cycle_compare_report", title, body_md, payload)
    _insert_product_report(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        agent_id="tester",
        report_type="cross_cycle_compare_report",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def create_retrospective_plan(run_id: str, task_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    reports = _product_report_rows(run_id)
    usability = next((item for item in reports if item["report_type"] == "product_test"), {})
    benchmark = next((item for item in reports if item["report_type"] == "benchmark_report"), {})
    cross_cycle = next((item for item in reports if item["report_type"] == "cross_cycle_compare_report"), {})
    review_rows = fetch_all(
        "SELECT section, approved, reason FROM reviews WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    final_json = (_load_output_bundle(run_id).get("final_json") or {})
    final_artifact = []
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        main = ((final_json.get(section) or {}).get("main") or {})
        if not main:
            continue
        final_artifact.append(
            {
                "section": section,
                "title": _truncate(main.get("title"), 120),
                "summary_zh": _truncate(main.get("summary_zh"), 180),
                "image_count": len(main.get("images") or []),
            }
        )
    request = content_layer.retrospective_plan_request(
        run_id=run_id,
        product_test=usability.get("report_json", {}) or {},
        benchmark=benchmark.get("report_json", {}) or {},
        cross_cycle_compare=cross_cycle.get("report_json", {}) or {},
        review_rows=[{"section": section, "approved": approved, "reason": reason} for section, approved, reason in review_rows],
        final_artifact=final_artifact,
        forced_progress_issues=_forced_progress_issues(run_id),
    )
    result = _run_content_request(request)
    product_problems = _normalize_retro_plan_product_problems(result.get("product_problems"))
    behavior_problems = _normalize_retro_plan_behavior_problems(result.get("behavior_problems"))
    topics = _normalize_retro_plan_topics(result.get("topics"))
    if not topics:
        raise RuntimeError("retrospective.plan 必须生成至少 1 个可讨论 topic，禁止空 topics 进入 retrospective.discussion")
    summary = _require_llm_visible_text(result, field="summary", node_type="retrospective.plan")
    title = "manager retrospective plan"
    payload = {
        "run_id": run_id,
        "product_problems": product_problems,
        "behavior_problems": behavior_problems,
        "topics": topics,
        "summary": summary,
        "plan_markdown": _reject_template_shell(
            _require_llm_visible_text(result, field="plan_markdown", node_type="retrospective.plan"),
            node_type="retrospective.plan",
            markers=[
                "开场提醒",
                "第一个讨论点",
                "第二个讨论点",
                "第三个讨论点",
                "收尾",
                "散会",
                "行动项认领",
            ],
        ),
        "generation_mode": result.get("generation_mode", ""),
        "generation_error": result.get("generation_error", ""),
    }
    body_md = payload["plan_markdown"]
    files = _write_aux_report_files(run_id, "retrospective_plan", title, body_md, payload)
    _insert_product_report(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        agent_id=MANAGER_AGENT_ID,
        report_type="retrospective_plan",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def _prepare_product_report_job(run_id: str, task_id: str) -> dict:
    reports = _product_report_rows(run_id)
    product_tests = [item for item in reports if item["report_type"] == "product_test"]
    benchmark = next((item for item in reports if item["report_type"] == "benchmark_report"), None)
    request = content_layer.product_evaluation_request(
        run_id=run_id,
        product_tests=[
            {
                "agent_id": item["agent_id"],
                "focus": (item.get("report_json") or {}).get("focus", ""),
                "summary": item["summary_text"],
                "reader_findings": _product_test_reader_findings(item.get("report_json") or {}),
                "reader_improvement_opportunities": _product_test_reader_improvements(
                    item.get("report_json") or {},
                ),
                "report_markdown": _truncate((item.get("report_json") or {}).get("report_markdown"), 400),
            }
            for item in product_tests
        ],
        benchmark_gap=((benchmark or {}).get("report_json", {}) or {}).get("most_visible_gap", ""),
        benchmark_next=_clean_string_list(((benchmark or {}).get("report_json", {}) or {}).get("next_cycle_actions")),
    )
    request["project_id"] = get_run_project_context(run_id)[0]
    request["cycle_no"] = get_run_project_context(run_id)[1]
    request["task_id"] = task_id
    return request


def _apply_product_report_result(run_id: str, task_id: str, decision: dict) -> dict:
    reports = _product_report_rows(run_id)
    product_tests = [item for item in reports if item["report_type"] == "product_test"]
    benchmark = next((item for item in reports if item["report_type"] == "benchmark_report"), None)
    dedup_problems = _require_llm_string_list(
        decision,
        field="top_product_issues",
        node_type="product.report",
        min_items=1,
        limit=5,
    )
    agent_links = _require_llm_string_list(
        decision,
        field="agent_responsibility_links",
        node_type="product.report",
        min_items=1,
        limit=5,
    )
    dedup_next = _require_llm_string_list(
        decision,
        field="next_cycle_recommendations",
        node_type="product.report",
        min_items=1,
        limit=6,
    )
    summary = _require_llm_visible_text(decision, field="summary", node_type="product.report")
    title = "本轮产品评估总报告"
    body_md = _reject_template_shell(
        _require_llm_visible_text(decision, field="report_markdown", node_type="product.report"),
        node_type="product.report",
        markers=[
            "主要问题：",
            "责任归属：",
            "下一轮建议：",
        ],
    )
    payload = {
        "run_id": run_id,
        "top_product_issues": dedup_problems,
        "agent_responsibility_links": agent_links,
        "next_cycle_recommendations": dedup_next,
        "summary": summary,
        "report_markdown": body_md,
        "generation_mode": decision["generation_mode"],
        "generation_error": decision.get("generation_error", ""),
        "timeout_ms": decision.get("timeout_ms"),
        "prompt_size": decision.get("prompt_size"),
        "input_size": decision.get("input_size"),
        "evidence_object_count": decision.get("evidence_object_count"),
        "started_at": decision.get("started_at"),
        "finished_at": decision.get("finished_at"),
    }
    files = _write_aux_report_files(run_id, "product_evaluation_report", title, body_md, payload)
    project_id, cycle_no = get_run_project_context(run_id)
    _insert_product_report(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        agent_id=MANAGER_AGENT_ID,
        report_type="product_evaluation_report",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def create_product_evaluation_report(run_id: str, task_id: str) -> dict:
    prepared = _prepare_product_report_job(run_id, task_id)
    decision = _run_content_request(prepared)
    return _apply_product_report_result(run_id, task_id, decision)


def start_retrospective_thread(run_id: str, task_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    plan_topics = _retrospective_plan_topics(run_id)
    first_topic = _retro_topic_seed(plan_topics[0])
    topic_title = first_topic.get("title") or first_topic.get("topic") or "问题"
    topic_body = first_topic.get("body") or ""
    topic_id = _open_retro_topic(
        run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        title=topic_title,
        opened_by=MANAGER_AGENT_ID,
        evidence_refs=[{"body": topic_body, "owner": first_topic.get("owner"), "counterpart": first_topic.get("counterpart")}],
    )
    topic_evidence = [
        {
            "topic": item.get("title") or item.get("topic") or "问题",
            "owner": item.get("owner") or "",
            "counterpart": item.get("counterpart") or "",
            "body": _truncate(item.get("body"), 180),
        }
        for item in [_retro_topic_seed(topic) for topic in plan_topics]
    ]
    request = content_layer.retrospective_opening_request(
        run_id=run_id,
        topic_id=topic_id,
        topic_title=topic_title,
        topic_body=_truncate(topic_body, 220),
        topic_evidence=topic_evidence,
        next_agents=first_topic.get("next_agents") or RETRO_PARTICIPANTS[:],
    )
    opening = _run_content_request(request)
    body = _require_llm_visible_text(
        opening,
        field="body",
        node_type="retrospective.discussion",
        aliases=("content", "comment_text", "message_body"),
    )
    raw_next_agents = opening.get("next_agents")
    suggested_next_agents = [
        agent for agent in (raw_next_agents if isinstance(raw_next_agents, list) else _parse_agents(raw_next_agents))
        if agent in RETRO_PARTICIPANTS
    ]
    if not suggested_next_agents:
        suggested_next_agents = first_topic.get("next_agents") or RETRO_PARTICIPANTS[:]
    result = {
        "topic_id": topic_id,
        "message_id": task_id,
        "reply_to_message_id": None,
        "from_agent": MANAGER_AGENT_ID,
        "to_agent": opening.get("to_agent") or request["fallback_payload"]["to_agent"],
        "target_type": opening.get("target_type") or request["fallback_payload"]["target_type"],
        "topic": _topic_label(opening.get("topic") or topic_title),
        "intent": opening.get("intent") or request["fallback_payload"]["intent"],
        "round_no": 0,
        "body": body,
        "next_agents": suggested_next_agents,
        "controversies": topic_evidence,
        "next_topic": _retro_topic_seed(plan_topics[1]) if len(plan_topics) > 1 else {},
        "generation_mode": opening.get("generation_mode", ""),
        "generation_error": opening.get("generation_error", ""),
        "evidence_object_count": opening.get("evidence_object_count"),
        "started_at": opening.get("started_at"),
        "finished_at": opening.get("finished_at"),
    }
    _insert_retrospective_message(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        topic_id=topic_id,
        agent_id=MANAGER_AGENT_ID,
        message_id=task_id,
        reply_to_message_id=None,
        to_agent=result["to_agent"],
        target_type=result["target_type"],
        topic=result["topic"],
        intent=result["intent"],
        round_no=0,
        body=body,
    )
    return result


def create_retrospective_comment(run_id: str, task_id: str, agent_id: str, payload: dict) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    topic_id = payload.get("topic_id") or (_current_open_retro_topic(run_id) or {}).get("topic_id")
    topic_row = _current_open_retro_topic(run_id)
    round_no = int(payload.get("round_no") or 1)
    reply_to_message_id = payload.get("reply_to_message_id")
    sections = AGENT_SECTIONS.get(agent_id, [])
    review_rows = fetch_all(
        """
        SELECT section, approved, reason
        FROM reviews
        WHERE run_id=%s
        ORDER BY created_at DESC
        """,
        (run_id,),
    )
    own_reviews = [
        signal
        for section, approved, reason in review_rows
        if section in sections
        for signal in [_review_signal(section, approved, reason)]
        if signal
    ]
    other_reviews = [
        signal
        for section, approved, reason in review_rows
        if section not in sections
        for signal in [_review_signal(section, approved, reason)]
        if signal
    ]
    thread = _retro_thread_rows(run_id)
    memory = get_project_memory(project_id, agent_id)
    reply_row = None
    if reply_to_message_id:
        for msg in thread:
            if msg["message_id"] == reply_to_message_id:
                reply_row = (
                    msg["from_agent"],
                    msg["to_agent"],
                    msg["topic"],
                    msg["intent"],
                    msg["body"],
                )
                break
    reply_text = ""
    if reply_row:
        reply_text = reply_row[4]
    peer_agent = WORKER_XHS_AGENT_ID if agent_id == WORKER_33_AGENT_ID else WORKER_33_AGENT_ID
    peer_msg = next((msg for msg in reversed(thread) if msg["from_agent"] == peer_agent), None)
    product_reports = _product_report_rows(run_id)
    product_signals = []
    product_tests: dict[str, list[str]] = {}
    for report in product_reports:
        if report["report_type"] == "product_test":
            findings = _product_test_reader_findings(report["report_json"])
            product_signals.extend(findings)
            product_tests.setdefault(report["agent_id"], []).extend(findings)
        elif report["report_type"] == "product_evaluation_report":
            product_signals.extend(_clean_string_list(report["report_json"].get("top_product_issues")))
    benchmark = next((row for row in product_reports if row["report_type"] == "benchmark_report"), None)
    topic_evidence = [
        {
            "owner": item.get("owner") or "",
            "counterpart": item.get("counterpart") or "",
            "body": _truncate(item.get("body"), 180),
        }
        for item in ((topic_row or {}).get("evidence_refs") or [])
    ]
    relevant_context = {
        "own_reviews": own_reviews,
        "other_reviews": other_reviews,
        "memory_summary": memory.get("summary"),
        "reply_text": _truncate(reply_text, 200),
        "peer_message": _truncate(peer_msg["body"], 180) if peer_msg else "",
        "recent_thread": _retro_thread_for_prompt(run_id, body_limit=150),
        "product_signals": list(dict.fromkeys([item for item in product_signals if item])),
        "product_tests": product_tests,
        "benchmark_summary": _truncate(benchmark["summary_text"], 180) if benchmark else "",
        "final_titles": _main_titles(run_id),
        "topic_evidence": topic_evidence,
    }
    fallback = _retro_route_defaults(payload, topic_row)
    request = content_layer.retrospective_comment_request(
        run_id=run_id,
        agent_id=agent_id,
        topic_id=topic_id,
        current_topic=(topic_row or {}).get("title") or payload.get("topic") or "",
        topic_evidence=topic_evidence,
        reply_to_message_id=reply_to_message_id,
        reply={
            "from_agent": reply_row[0] if reply_row else "",
            "to_agent": reply_row[1] if reply_row else "",
            "topic": reply_row[2] if reply_row else "",
            "intent": reply_row[3] if reply_row else "",
            "body": _truncate(reply_text, 220),
        },
        responsibility_scope=sections,
        review_signals={"own": own_reviews, "other": other_reviews},
        product_signals=relevant_context.get("product_signals") or [],
        benchmark_summary=relevant_context.get("benchmark_summary") or "",
        memory_summary=relevant_context.get("memory_summary") or "",
        final_titles=relevant_context.get("final_titles") or {},
        recent_thread=relevant_context.get("recent_thread") or [],
        fallback=fallback,
    )
    discussion = _run_content_request(request)
    body = _require_llm_visible_text(
        discussion,
        field="body",
        node_type="retrospective.discussion",
        aliases=("content", "comment_text", "message_body"),
    )
    to_agent = discussion.get("to_agent") or fallback["to_agent"]
    target_type = discussion.get("target_type") or fallback["target_type"]
    topic = discussion.get("topic") or fallback["topic"]
    intent = discussion.get("intent") or fallback["intent"]
    raw_next_agents = discussion.get("next_agents")
    next_agents = [
        agent for agent in (raw_next_agents if isinstance(raw_next_agents, list) else _parse_agents(raw_next_agents))
        if agent in RETRO_PARTICIPANTS
    ]
    if not next_agents:
        next_agents = fallback["next_agents"]
    result = {
        "topic_id": topic_id,
        "message_id": task_id,
        "reply_to_message_id": reply_to_message_id,
        "from_agent": agent_id,
        "to_agent": to_agent,
        "target_type": target_type,
        "topic": _topic_label(topic),
        "intent": intent,
        "round_no": round_no,
        "body": body.strip(),
        "next_agents": next_agents,
        "generation_mode": discussion.get("generation_mode", ""),
        "generation_error": discussion.get("generation_error", ""),
        "evidence_object_count": discussion.get("evidence_object_count"),
        "started_at": discussion.get("started_at"),
        "finished_at": discussion.get("finished_at"),
    }
    _insert_retrospective_message(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        topic_id=topic_id,
        agent_id=agent_id,
        message_id=task_id,
        reply_to_message_id=reply_to_message_id,
        to_agent=result["to_agent"],
        target_type=result["target_type"],
        topic=result["topic"],
        intent=result["intent"],
        round_no=round_no,
        body=body,
    )
    return result


def _prepare_retrospective_summary_job(run_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    topic_rows = fetch_all(
        """
        SELECT t.topic_id, t.title, d.summary
        FROM retro_topics t
        LEFT JOIN retro_decisions d ON d.topic_id=t.topic_id
        WHERE t.run_id=%s
        ORDER BY t.opened_at
        """,
        (run_id,),
    )
    usability = _product_report_by_type(run_id, "product_test")
    benchmark = _product_report_by_type(run_id, "benchmark_report")
    cross_cycle = _product_report_by_type(run_id, "cross_cycle_compare_report")
    retro_plan = _product_report_by_type(run_id, "retrospective_plan")
    applied_rules = get_effective_optimization_log(project_id, MANAGER_AGENT_ID, (cycle_no or 0) + 1).get("compiled_rules") or []
    thread_rows = _retro_thread_for_prompt(run_id, body_limit=170)
    final_json = (_load_output_bundle(run_id).get("final_json") or {})
    final_artifact = []
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        main = ((final_json.get(section) or {}).get("main") or {})
        if not main:
            continue
        final_artifact.append(
            {
                "section": section,
                "title": _truncate(main.get("title"), 120),
                "summary_zh": _truncate(main.get("summary_zh"), 180),
                "image_count": len(main.get("images") or []),
            }
        )
    request = content_layer.retrospective_summary_request(
        run_id=run_id,
        product_test={
            "summary": usability.get("summary_text"),
            "reader_findings": _product_test_reader_findings(usability.get("report_json", {}) or {}),
            "reader_improvement_opportunities": _product_test_reader_improvements(
                usability.get("report_json", {}) or {},
            ),
        },
        benchmark={
            "summary": benchmark.get("summary_text"),
            "most_visible_gap": (benchmark.get("report_json", {}) or {}).get("most_visible_gap", ""),
            "next_cycle_actions": _clean_string_list((benchmark.get("report_json", {}) or {}).get("next_cycle_actions")),
        },
        cross_cycle_compare={
            "summary": cross_cycle.get("summary_text"),
            "improved_issues": _clean_string_list((cross_cycle.get("report_json", {}) or {}).get("improved_issues")),
            "unimplemented": _clean_string_list(
                (cross_cycle.get("report_json", {}) or {}).get("unimplemented_previous_optimization_suggestions")
            ),
        },
        retrospective_plan={
            "summary": retro_plan.get("summary_text"),
            "product_problems": (retro_plan.get("report_json", {}) or {}).get("product_problems", []),
            "behavior_problems": (retro_plan.get("report_json", {}) or {}).get("behavior_problems", []),
        },
        final_artifact=final_artifact,
        retro_thread=thread_rows,
        retro_decisions=[
            {"topic_id": topic_id, "title": title, "summary": decision_summary or ""}
            for topic_id, title, decision_summary in topic_rows
        ],
        applied_rules=applied_rules,
        forced_progress_issues=_forced_progress_issues(run_id),
    )
    request["project_id"] = project_id
    request["cycle_no"] = cycle_no
    request["task_id"] = None
    return request


def _apply_retrospective_summary_result(run_id: str, decision: dict) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    summary = _require_llm_visible_text(decision, field="summary", node_type="retrospective.summary")
    summary_md = _reject_template_shell(
        _require_llm_visible_text(
            decision,
            field="summary_markdown",
            node_type="retrospective.summary",
            aliases=("summary",),
        ),
        node_type="retrospective.summary",
        markers=[
            "Discussion Summary",
            "Product Problems",
            "Root Causes",
            "Accepted",
            "Next Cycle",
        ],
    )
    files = _write_aux_report_files(
        run_id,
        "retrospective_summary",
        "Retrospective Summary",
        summary_md,
        {
            "run_id": run_id,
            "summary": summary,
            "summary_markdown": summary_md,
            "generation_mode": decision["generation_mode"],
            "generation_error": decision.get("generation_error", ""),
            "timeout_ms": decision.get("timeout_ms"),
            "prompt_size": decision.get("prompt_size"),
            "input_size": decision.get("input_size"),
            "evidence_object_count": decision.get("evidence_object_count"),
            "started_at": decision.get("started_at"),
            "finished_at": decision.get("finished_at"),
        },
    )
    execute(
        """
        UPDATE project_cycles
        SET retrospective_summary=%s, updated_at=NOW(), retrospective_completed_at=NOW()
        WHERE project_id=%s AND cycle_no=%s
        """,
        (summary, project_id, cycle_no),
    )
    execute(
        """
        UPDATE workflow_runs
        SET notes =
            jsonb_set(
                jsonb_set(
                    jsonb_set(COALESCE(notes, '{}'::jsonb), '{retrospective_summary_md}', to_jsonb(%s::text), true),
                    '{retrospective_summary_json}', to_jsonb(%s::text), true
                ),
                '{retrospective_summary_html}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (files["markdown_path"], files["json_path"], files["html_path"], run_id),
    )
    return {
        "summary": summary,
        "summary_markdown": summary_md,
        "markdown_path": files["markdown_path"],
        "json_path": files["json_path"],
        "html_path": files["html_path"],
        "generation_mode": decision["generation_mode"],
        "generation_error": decision.get("generation_error", ""),
        "timeout_ms": decision.get("timeout_ms"),
        "prompt_size": decision.get("prompt_size"),
        "input_size": decision.get("input_size"),
        "evidence_object_count": decision.get("evidence_object_count"),
        "started_at": decision.get("started_at"),
        "finished_at": decision.get("finished_at"),
    }


def summarize_retrospective(run_id: str) -> str:
    prepared = _prepare_retrospective_summary_job(run_id)
    decision = _run_content_request(prepared)
    return _apply_retrospective_summary_result(run_id, decision)


def _agent_memory_blueprint(agent_id: str, cycle_no: int, summary: str, previous: dict) -> dict:
    raise RuntimeError("legacy agent memory blueprint is disabled; use _agent_memory_seed with run-bound evidence")


def self_optimize_agent(run_id: str, agent_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    previous = get_project_memory(project_id, agent_id)
    optimization_log = get_effective_optimization_log(project_id, agent_id, cycle_no or 0)
    summary_row = fetch_one(
        "SELECT retrospective_summary FROM project_cycles WHERE project_id=%s AND cycle_no=%s",
        (project_id, cycle_no),
    )
    summary = summary_row[0] if summary_row and summary_row[0] else ""
    thread = _retro_thread_rows(run_id)
    relevant = []
    for msg in thread:
        to_agent = msg["to_agent"] or ""
        if msg["from_agent"] == agent_id or agent_id in to_agent or to_agent in {"all", "team"}:
            relevant.append(msg)
    memory = _agent_memory_seed(agent_id, cycle_no, summary, previous, optimization_log)
    request = content_layer.agent_self_optimize_request(
        agent_id=agent_id,
        cycle_no=cycle_no,
        evidence=_self_optimize_evidence(run_id, agent_id, summary, previous, relevant, memory, optimization_log),
    )
    data = _run_content_request(request)
    generated_summary = _require_llm_visible_text(data, field="summary", node_type="agent.self_optimize")
    exposed_issues = _require_llm_string_list(
        data,
        field="exposed_issues",
        node_type="agent.self_optimize",
        min_items=1,
        limit=4,
    )
    next_cycle_strategy = _require_llm_string_list(
        data,
        field="next_cycle_strategy",
        node_type="agent.self_optimize",
        min_items=1,
        limit=5,
    )
    next_cycle_quality_checks = _require_llm_string_list(
        data,
        field="next_cycle_quality_checks",
        node_type="agent.self_optimize",
        min_items=1,
        limit=5,
    )
    role_improvement_plan = _require_llm_visible_text(
        data,
        field="role_improvement_plan",
        node_type="agent.self_optimize",
    )
    optimization_markdown = _reject_template_shell(
        _require_llm_visible_text(
            data,
            field="optimization_markdown",
            node_type="agent.self_optimize",
        ),
        node_type="agent.self_optimize",
        markers=[
            "暴露问题：",
            "下一轮策略：",
            "质量检查：",
            "角色改进：",
        ],
    )
    memory.update(
        {
            "summary": generated_summary,
            "exposed_issues": exposed_issues,
            "next_cycle_strategy": next_cycle_strategy,
            "next_cycle_quality_checks": next_cycle_quality_checks,
            "role_improvement_plan": role_improvement_plan,
            "optimization_markdown": optimization_markdown,
            "generation_mode": data.get("generation_mode", ""),
            "generation_error": data.get("generation_error", ""),
            "evidence_object_count": data.get("evidence_object_count"),
            "started_at": data.get("started_at"),
            "finished_at": data.get("finished_at"),
        }
    )
    if memory.get("next_cycle_strategy"):
        memory["execution_strategy"] = memory["next_cycle_strategy"]
    if memory.get("next_cycle_quality_checks"):
        memory["quality_checks"] = memory["next_cycle_quality_checks"]
    optimization_log = get_effective_optimization_log(project_id, agent_id, cycle_no + 1)
    memory["optimization_log"] = optimization_log
    execute(
        """
        INSERT INTO agent_optimizations(project_id, cycle_no, run_id, agent_id, summary_text, optimization_json)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb)
        """,
        (project_id, cycle_no, run_id, agent_id, memory["summary"], jdump(memory)),
    )
    execute(
        """
        INSERT INTO optimization_logs(
            project_id, cycle_no, run_id, agent_id, source_type, source, author, category,
            effective_from_cycle, expires_after_cycle, body, details
        )
        VALUES (%s,%s,%s,%s,'agent_generated','retrospective',%s,'agent_memory',%s,NULL,%s,%s::jsonb)
        """,
        (
            project_id,
            cycle_no,
            run_id,
            agent_id,
            agent_id,
            cycle_no + 1,
            memory["summary"],
            jdump(
                {
                    "exposed_issues": memory.get("exposed_issues", []),
                    "next_cycle_strategy": memory.get("next_cycle_strategy", []),
                    "next_cycle_quality_checks": memory.get("next_cycle_quality_checks", []),
                    "role_improvement_plan": memory.get("role_improvement_plan", ""),
                    "optimization_markdown": memory.get("optimization_markdown", ""),
                    "source_whitelist": memory.get("source_whitelist", []),
                    "source_blacklist": memory.get("source_blacklist", []),
                    "prefer_images": True,
                }
            ),
        ),
    )
    execute(
        """
        INSERT INTO project_agent_memory(project_id, agent_id, current_memory)
        VALUES (%s,%s,%s::jsonb)
        ON CONFLICT (project_id, agent_id) DO UPDATE
        SET current_memory=EXCLUDED.current_memory, updated_at=NOW()
        """,
        (project_id, agent_id, jdump(memory)),
    )
    return memory


def manager_write_agent_optimizations(run_id: str, task_id: str) -> dict:
    results = {}
    for agent_id in ALL_AGENT_IDS:
        results[agent_id] = self_optimize_agent(run_id, agent_id)
    return {
        "status": "optimized",
        "agents": {agent_id: value.get("summary") for agent_id, value in results.items()},
    }


def _cycle_dir(project_id: str, cycle_no: int) -> Path:
    return PROJECT_OUTPUT_DIR / project_id / "cycles" / f"{cycle_no:03d}"


def _sync_project_files(project_id: str, cycle_no: int, run_id: str):
    cycle_dir = _cycle_dir(project_id, cycle_no)
    cycle_dir.mkdir(parents=True, exist_ok=True)
    run_dir = RUN_OUTPUT_DIR / run_id
    for name in [
        "final_report.html",
        "final_report.md",
        "final_report.json",
        "product_test_tester.html",
        "product_test_tester.md",
        "product_test_tester.json",
        "benchmark_report.html",
        "benchmark_report.md",
        "benchmark_report.json",
        "product_evaluation_report.html",
        "product_evaluation_report.md",
        "product_evaluation_report.json",
        "draft_report.html",
        "draft_report.md",
        "draft_report.json",
        "draft_review_summary.html",
        "draft_review_summary.md",
        "draft_review_summary.json",
        "discussion_summary.html",
        "discussion_summary.md",
        "discussion_summary.json",
        "revised_final_report.html",
        "revised_final_report.md",
        "revised_final_report.json",
        "retrospective_summary.html",
        "retrospective_summary.md",
        "retrospective_summary.json",
    ]:
        src = run_dir / name
        if src.exists():
            (cycle_dir / name).write_text(src.read_text())
    (cycle_dir / "conversation.html").write_text(render_conversation_html(run_id))
    (cycle_dir / "draft-review.html").write_text(render_draft_review_html(run_id))
    (cycle_dir / "review-thread.html").write_text(render_review_thread_html(run_id))
    (cycle_dir / "retrospective.html").write_text(render_retrospective_html(run_id))
    (cycle_dir / "product-reports.html").write_text(render_product_report_html(run_id))
    optimization_rows = fetch_all(
        """
        SELECT agent_id, summary_text, optimization_json::text
        FROM agent_optimizations
        WHERE project_id=%s AND cycle_no=%s
        ORDER BY agent_id
        """,
        (project_id, cycle_no),
    )
    optimization_summary = {
        "project_id": project_id,
        "cycle_no": cycle_no,
        "run_id": run_id,
        "agents": {
            agent_id: {"summary": summary_text, "memory": _load_json(opt_json)}
            for agent_id, summary_text, opt_json in optimization_rows
        },
    }
    (cycle_dir / "optimization_summary.json").write_text(
        json.dumps(optimization_summary, ensure_ascii=False, indent=2)
    )
    optimization_log_rows = fetch_all(
        """
        SELECT agent_id, source_type, source, author, category, effective_from_cycle, expires_after_cycle, body, details::text, created_at
        FROM optimization_logs
        WHERE project_id=%s AND effective_from_cycle <= %s AND (expires_after_cycle IS NULL OR expires_after_cycle >= %s)
        ORDER BY created_at
        """,
        (project_id, cycle_no + 1, cycle_no + 1),
    )
    (cycle_dir / "next_cycle_optimization_log.json").write_text(
        json.dumps(
            [
                {
                    "agent_id": row[0],
                    "source_type": row[1],
                    "source": row[2],
                    "author": row[3],
                    "category": row[4],
                    "effective_from_cycle": row[5],
                    "expires_after_cycle": row[6],
                    "body": row[7],
                    "details": _load_json(row[8]),
                    "created_at": row[9].isoformat() if row[9] else None,
                }
                for row in optimization_log_rows
            ],
            ensure_ascii=False,
            indent=2,
        )
    )
    for agent_id in ALL_AGENT_IDS:
        memory = get_project_memory(project_id, agent_id)
        (cycle_dir / f"agent_{agent_id}_memory.json").write_text(
            json.dumps(memory, ensure_ascii=False, indent=2)
        )


def _sync_project_indexes(project_id: str):
    project_dir = PROJECT_OUTPUT_DIR / project_id
    project_dir.mkdir(parents=True, exist_ok=True)
    project = _project_row(project_id)
    cycles = fetch_all(
        """
        SELECT cycle_no, run_id, status, started_at, completed_at, retrospective_summary, optimization_snapshot::text
        FROM project_cycles
        WHERE project_id=%s
        ORDER BY cycle_no
        """,
        (project_id,),
    )
    overview = {
        "project_id": project_id,
        "workflow_id": project[1],
        "status": project[2],
        "current_cycle_no": project[3],
        "max_cycles": project[4],
        "latest_run_id": project[10],
        "next_cycle_at": project[11].isoformat() if project[11] else None,
        "paused_reason": project[12],
        "notes": _load_json(project[13]),
    }
    (project_dir / "project_overview.json").write_text(json.dumps(overview, ensure_ascii=False, indent=2))
    cycle_index = []
    changelog_lines = [f"# Project {project_id} Changelog", ""]
    improvement_lines = [f"# Project {project_id} Improvement History", ""]
    prev_snapshot = {}
    for cycle_no, run_id, status, started_at, completed_at, retro_summary, snapshot_text in cycles:
        snapshot = _load_json(snapshot_text)
        cycle_index.append(
            {
                "cycle_no": cycle_no,
                "run_id": run_id,
                "status": status,
                "started_at": started_at.isoformat() if started_at else None,
                "completed_at": completed_at.isoformat() if completed_at else None,
                "retrospective_summary": retro_summary,
                "product_report_page": f"/newsflow/runs/{run_id}/product.html" if run_id else None,
            }
        )
        changelog_lines.append(f"## Cycle {cycle_no}")
        changelog_lines.append(f"- run_id: {run_id}")
        changelog_lines.append(f"- status: {status}")
        changelog_lines.append(
            f"- retrospective: {(retro_summary or '无').splitlines()[0]}"
        )
        changelog_lines.append(
            f"- product reports: /newsflow/runs/{run_id}/product.html" if run_id else "- product reports: 无"
        )
        changelog_lines.append("")
        improvement_lines.append(f"## Cycle {cycle_no}")
        for agent_id in ALL_AGENT_IDS:
            current_summary = (snapshot.get(agent_id) or {}).get("summary", "无")
            previous_summary = (prev_snapshot.get(agent_id) or {}).get("summary", "无")
            improvement_lines.append(f"- {agent_id}: 当前 `{current_summary}`；上一轮 `{previous_summary}`")
        guidance_rows = fetch_all(
            """
            SELECT agent_id, source_type, body
            FROM optimization_logs
            WHERE project_id=%s AND effective_from_cycle=%s
            ORDER BY created_at
            """,
            (project_id, cycle_no),
        )
        for agent_id, source_type, body in guidance_rows:
            improvement_lines.append(f"- {source_type}/{agent_id or 'project'}: {body}")
        improvement_lines.append("")
        prev_snapshot = snapshot
    (project_dir / "cycle_index.json").write_text(json.dumps(cycle_index, ensure_ascii=False, indent=2))
    (project_dir / "changelog.md").write_text("\n".join(changelog_lines))
    (project_dir / "improvement_history.md").write_text("\n".join(improvement_lines))


def create_project(
    project_id: str | None = None,
    *,
    max_cycles: int | None = None,
    max_consecutive_failures: int | None = None,
    discussion_seconds: int | None = None,
    retrospective_seconds: int | None = None,
    next_cycle_delay_seconds: int | None = None,
    auto_start: bool = True,
) -> dict:
    project_id = project_id or f"newsloop-{uuid.uuid4().hex[:8]}"
    execute(
        """
        INSERT INTO projects(
            project_id, workflow_id, status, current_cycle_no, max_cycles, max_consecutive_failures,
            consecutive_failures, discussion_seconds, retrospective_seconds, next_cycle_delay_seconds, notes
        )
        VALUES (%s,%s,'running',0,%s,%s,0,%s,%s,%s,%s::jsonb)
        ON CONFLICT (project_id) DO NOTHING
        """,
        (
            project_id,
            WORKFLOW_ID,
            max_cycles if max_cycles is not None else SETTINGS.project_max_cycles_default,
            max_consecutive_failures
            if max_consecutive_failures is not None
            else SETTINGS.project_max_consecutive_failures,
            discussion_seconds if discussion_seconds is not None else SETTINGS.discussion_test_seconds,
            retrospective_seconds
            if retrospective_seconds is not None
            else SETTINGS.project_retrospective_default_seconds,
            next_cycle_delay_seconds
            if next_cycle_delay_seconds is not None
            else SETTINGS.project_next_cycle_delay_default_seconds,
            jdump(
                {
                    "defaults": {
                        "discussion_seconds": SETTINGS.discussion_default_seconds,
                        "retrospective_seconds": SETTINGS.project_retrospective_default_seconds,
                        "next_cycle_delay_seconds": SETTINGS.project_next_cycle_delay_default_seconds,
                    },
                    "test_values": {
                        "discussion_seconds": discussion_seconds
                        if discussion_seconds is not None
                        else SETTINGS.discussion_test_seconds,
                        "retrospective_seconds": retrospective_seconds
                        if retrospective_seconds is not None
                        else SETTINGS.project_retrospective_default_seconds,
                        "next_cycle_delay_seconds": next_cycle_delay_seconds
                        if next_cycle_delay_seconds is not None
                        else SETTINGS.project_next_cycle_delay_default_seconds,
                    },
                }
            ),
        ),
    )
    _sync_project_indexes(project_id)
    run_id = start_next_cycle(project_id) if auto_start else None
    return {"project_id": project_id, "run_id": run_id}


def pause_project(project_id: str, reason: str = "manual pause") -> None:
    execute(
        "UPDATE projects SET status='paused', paused_reason=%s, updated_at=NOW() WHERE project_id=%s",
        (reason, project_id),
    )
    execute(
        """
        UPDATE project_cycles
        SET status='paused', updated_at=NOW()
        WHERE project_id=%s AND status IN ('running', 'retrospective_running', 'optimizing')
        """,
        (project_id,),
    )
    execute(
        """
        UPDATE workflow_runs
        SET status='paused'
        WHERE project_id=%s AND status='running'
        """,
        (project_id,),
    )
    _sync_project_indexes(project_id)


def resume_project(project_id: str) -> None:
    execute(
        """
        UPDATE projects
        SET status='running', paused_reason=NULL,
            next_cycle_at=COALESCE(next_cycle_at, NOW()), updated_at=NOW()
        WHERE project_id=%s
        """,
        (project_id,),
    )
    execute(
        """
        UPDATE project_cycles
        SET status=CASE
            WHEN retrospective_started_at IS NOT NULL AND retrospective_completed_at IS NULL THEN 'retrospective_running'
            WHEN retrospective_completed_at IS NOT NULL AND completed_at IS NULL THEN 'optimizing'
            ELSE 'running'
        END,
        updated_at=NOW()
        WHERE project_id=%s AND status='paused'
        """,
        (project_id,),
    )
    execute(
        """
        UPDATE workflow_runs
        SET status='running'
        WHERE project_id=%s AND status='paused'
        """,
        (project_id,),
    )
    _sync_project_indexes(project_id)


def stop_project(project_id: str, reason: str = "manual stop") -> None:
    execute(
        """
        UPDATE projects
        SET status='stopped', paused_reason=%s, next_cycle_at=NULL, updated_at=NOW()
        WHERE project_id=%s
        """,
        (reason, project_id),
    )
    execute(
        """
        UPDATE project_cycles
        SET status='stopped', updated_at=NOW()
        WHERE project_id=%s AND status IN ('running', 'retrospective_running', 'optimizing', 'paused')
        """,
        (project_id,),
    )
    execute(
        """
        UPDATE workflow_runs
        SET status='stopped'
        WHERE project_id=%s AND status IN ('running', 'paused')
        """,
        (project_id,),
    )
    _sync_project_indexes(project_id)


def resume_from_stage(run_id: str, stage_name: str) -> dict:
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    if not row:
        raise KeyError(run_id)
    project_id, cycle_no = row[0], row[1]
    execute(
        """
        UPDATE workflow_runs
        SET status='running', current_phase=%s, completed_at=NULL
        WHERE run_id=%s
        """,
        (stage_name, run_id),
    )
    execute(
        """
        UPDATE tasks
        SET status='pending',
            started_at=NULL,
            finished_at=NULL,
            error_message=NULL,
            result=CASE
                WHEN result ? 'llm_job_id' THEN result - 'llm_job_id'
                ELSE result
            END
        WHERE run_id=%s AND phase=%s AND status IN ('failed','waiting')
        """,
        (run_id, stage_name),
    )
    execute(
        """
        DELETE FROM llm_jobs
        WHERE run_id=%s AND node_type=%s AND status IN ('pending','running','retrying','failed','fallback')
        """,
        (run_id, stage_name),
    )
    execute(
        """
        UPDATE projects
        SET status='running', paused_reason=NULL, updated_at=NOW()
        WHERE project_id=%s
        """,
        (project_id,),
    )
    execute(
        """
        UPDATE project_cycles
        SET status='running', updated_at=NOW()
        WHERE project_id=%s AND cycle_no=%s
        """,
        (project_id, cycle_no),
    )
    return {"project_id": project_id, "run_id": run_id, "stage_name": stage_name, "status": "running"}


def resume_failed_run(run_id: str) -> dict:
    failed_task = fetch_one(
        """
        SELECT phase
        FROM tasks
        WHERE run_id=%s AND status='failed'
        ORDER BY finished_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        (run_id,),
    )
    phase = failed_task[0] if failed_task else (fetch_one("SELECT current_phase FROM workflow_runs WHERE run_id=%s", (run_id,)) or [None])[0]
    if not phase:
        raise RuntimeError("未找到可恢复阶段")
    return resume_from_stage(run_id, phase)


def start_next_cycle(project_id: str) -> str | None:
    project = _project_row(project_id)
    if not project or project[2] != "running":
        return None
    active = fetch_one(
        """
        SELECT COUNT(*)
        FROM project_cycles
        WHERE project_id=%s AND status IN ('running', 'retrospective_running', 'optimizing')
        """,
        (project_id,),
    )[0]
    if active:
        return None
    if project[4] and project[3] >= project[4]:
        stop_project(project_id, "reached max cycles")
        return None
    guard = _resource_guard()
    if not guard["ok"]:
        pause_project(project_id, f"resource guard: {guard['reason']}")
        return None
    cycle_no = project[3] + 1
    attrs = {
        "workflow_id": WORKFLOW_ID,
        "project_id": project_id,
        "cycle_no": cycle_no,
        "run_id": None,
        "task_id": None,
        "parent_task_id": None,
        "agent_id": "orchestrator",
        "agent_role": "orchestrator",
        "section": "全局",
        "phase": "cycle.start_next",
        "retry_count": 0,
        "status": "started",
    }
    with workflow_span("orchestrator", "cycle.start_next", attrs):
        run_id = new_run(
            discussion_seconds=project[7],
            project_id=project_id,
            cycle_no=cycle_no,
        )
    execute(
        """
        INSERT INTO project_cycles(project_id, cycle_no, run_id, status, started_at, updated_at)
        VALUES (%s,%s,%s,'running',NOW(),NOW())
        ON CONFLICT (project_id, cycle_no) DO UPDATE
        SET run_id=EXCLUDED.run_id, status='running', started_at=NOW(), updated_at=NOW()
        """,
        (project_id, cycle_no, run_id),
    )
    execute(
        """
        UPDATE projects
        SET current_cycle_no=%s, latest_run_id=%s, next_cycle_at=NULL, updated_at=NOW()
        WHERE project_id=%s
        """,
        (cycle_no, run_id, project_id),
    )
    _sync_project_indexes(project_id)
    return run_id


def _finalize_cycle(project_id: str, cycle_no: int, run_id: str):
    project = _project_row(project_id)
    optimization_rows = fetch_all(
        """
        SELECT agent_id, optimization_json::text
        FROM agent_optimizations
        WHERE project_id=%s AND cycle_no=%s
        ORDER BY agent_id
        """,
        (project_id, cycle_no),
    )
    snapshot = {agent_id: _load_json(text) for agent_id, text in optimization_rows}
    execute(
        """
        UPDATE project_cycles
        SET status='completed', completed_at=NOW(), updated_at=NOW(), optimization_snapshot=%s::jsonb
        WHERE project_id=%s AND cycle_no=%s
        """,
        (jdump(snapshot), project_id, cycle_no),
    )
    execute(
        """
        UPDATE workflow_runs
        SET status='completed', current_phase='agent.optimization', completed_at=NOW()
        WHERE run_id=%s
        """,
        (run_id,),
    )
    _sync_project_files(project_id, cycle_no, run_id)
    _sync_project_indexes(project_id)
    if project[4] and cycle_no >= project[4]:
        stop_project(project_id, "reached max cycles")
        return
    guard = _resource_guard()
    if not guard["ok"]:
        pause_project(project_id, f"resource guard: {guard['reason']}")
        return
    next_cycle_at = now_local() + timedelta(seconds=project[9])
    attrs = {
        "workflow_id": WORKFLOW_ID,
        "project_id": project_id,
        "cycle_no": cycle_no,
        "run_id": run_id,
        "task_id": None,
        "parent_task_id": None,
        "agent_id": "orchestrator",
        "agent_role": "orchestrator",
        "section": "全局",
        "phase": "cycle.schedule_next",
        "retry_count": 0,
        "status": "scheduled",
    }
    with workflow_span("orchestrator", "cycle.schedule_next", attrs):
        execute(
            """
            UPDATE project_cycles
            SET next_cycle_at=%s, updated_at=NOW()
            WHERE project_id=%s AND cycle_no=%s
            """,
            (next_cycle_at, project_id, cycle_no),
        )
        execute(
            """
            UPDATE projects
            SET status='running', paused_reason=NULL, next_cycle_at=%s,
                consecutive_failures=0, updated_at=NOW()
            WHERE project_id=%s
            """,
            (next_cycle_at, project_id),
        )
    _sync_project_indexes(project_id)


def _dispatch_retro_comment_tasks(
    run_id: str,
    project_id: str,
    cycle_no: int,
    trace_ctx: dict,
    *,
    round_no: int,
    agents: list[str],
    reply_to_message_id: str | None,
    topic_id: str | None,
    topic: str,
    target_type: str,
    to_agent: str,
    intent: str,
):
    for agent_id in agents:
        exists = fetch_one(
            """
            SELECT COUNT(*) FROM tasks
            WHERE run_id=%s AND phase='retrospective.discussion'
              AND agent_id=%s
              AND COALESCE((payload->>'round_no')::int, 0)=%s
            """,
            (run_id, agent_id, round_no),
        )[0]
        if exists:
            continue
        dispatch_task(
            run_id,
            None,
            agent_id,
            AGENT_ROLES[agent_id],
            "全局",
            "retrospective.discussion",
            0,
            {
                "round_no": round_no,
                "reply_to_message_id": reply_to_message_id,
                "topic_id": topic_id,
                "topic": topic,
                "target_type": target_type,
                "to_agent": to_agent,
                "intent": intent,
            },
            trace_ctx,
            project_id,
            cycle_no,
        )


def _retro_messages_by_round(run_id: str, round_no: int) -> list[dict]:
    return [msg for msg in _retro_thread_rows(run_id) if msg["round_no"] == round_no]


def _parse_agents(value: str | None) -> list[str]:
    return [agent.strip() for agent in str(value or "").split(",") if agent.strip() in ALL_AGENT_IDS]


def _retro_has_new_information(messages: list[dict]) -> bool:
    normalized = []
    for msg in messages:
        text = re.sub(r"\s+", " ", (msg.get("body") or "").strip())
        if text:
            normalized.append(text)
    return len(set(normalized)) >= 2


def _maybe_advance_retro_topic(run_id: str, project_id: str, cycle_no: int, trace_ctx: dict, started, payload: dict) -> None:
    current_topic = _current_open_retro_topic(run_id)
    if not current_topic:
        return
    topic_id = current_topic["topic_id"]
    topic_title = current_topic["title"]
    topic_messages = _retro_messages_for_topic(run_id, topic_id)
    pending_comments = fetch_one(
        """
        SELECT COUNT(*) FROM tasks
        WHERE run_id=%s AND phase='retrospective.discussion' AND agent_id!=%s AND status IN ('pending', 'running')
        """,
        (run_id, MANAGER_AGENT_ID),
    )[0]
    if pending_comments:
        return
    non_manager = [msg for msg in topic_messages if msg["from_agent"] in RETRO_PARTICIPANTS]
    manager_msgs = [msg for msg in topic_messages if msg["from_agent"] == MANAGER_AGENT_ID]
    if current_topic["status"] == "open" and manager_msgs and not non_manager:
        opener = manager_msgs[-1]
        target_agents = [agent for agent in _parse_agents(opener.get("to_agent")) if agent in RETRO_PARTICIPANTS] or RETRO_PARTICIPANTS[:]
        _dispatch_retro_comment_tasks(
            run_id,
            project_id,
            cycle_no,
            trace_ctx,
            round_no=max(msg["round_no"] for msg in topic_messages) + 1,
            agents=target_agents,
            reply_to_message_id=opener["message_id"],
            topic_id=topic_id,
            topic=topic_title,
            target_type="agent",
            to_agent=MANAGER_AGENT_ID,
            intent="critique",
        )
        return
    if current_topic["status"] == "open" and non_manager:
        _set_retro_topic_status(topic_id, "debating")
        opener = non_manager[-1]
        target_agents = sorted({msg["from_agent"] for msg in non_manager if msg["from_agent"] in RETRO_PARTICIPANTS}) or RETRO_PARTICIPANTS[:]
        _dispatch_retro_comment_tasks(
            run_id,
            project_id,
            cycle_no,
            trace_ctx,
            round_no=max(msg["round_no"] for msg in topic_messages) + 1,
            agents=[MANAGER_AGENT_ID],
            reply_to_message_id=opener["message_id"],
            topic_id=topic_id,
            topic=topic_title,
            target_type="agent",
            to_agent=",".join(target_agents),
            intent="question",
        )
        execute(
            """
            UPDATE tasks
            SET payload=jsonb_set(payload,'{mode}',to_jsonb(%s::text),true)
            WHERE run_id=%s AND phase='retrospective.discussion' AND agent_id=%s AND status='pending'
              AND COALESCE((payload->>'topic_id')::text, '')=%s
            """,
            ("moderator_followup", run_id, MANAGER_AGENT_ID, topic_id),
        )
        return
    if current_topic["status"] == "debating" and manager_msgs:
        latest_manager = manager_msgs[-1]
        already_replied = {msg["from_agent"] for msg in topic_messages if msg["reply_to_message_id"] == latest_manager["message_id"]}
        target_agents = [agent for agent in _parse_agents(latest_manager.get("to_agent")) if agent not in already_replied]
        if target_agents:
            _dispatch_retro_comment_tasks(
                run_id,
                project_id,
                cycle_no,
                trace_ctx,
                round_no=max(msg["round_no"] for msg in topic_messages) + 1,
                agents=target_agents,
                reply_to_message_id=latest_manager["message_id"],
                topic_id=topic_id,
                topic=topic_title,
                target_type="agent",
                to_agent=MANAGER_AGENT_ID,
                intent="proposal",
            )
            execute(
                """
                UPDATE tasks
                SET payload=jsonb_set(payload,'{mode}',to_jsonb(%s::text),true)
                WHERE run_id=%s AND phase='retrospective.discussion' AND status='pending'
                  AND COALESCE((payload->>'topic_id')::text, '')=%s
                """,
                ("final_position", run_id, topic_id),
            )
            return
    elapsed = (datetime.now(started.tzinfo) - started).total_seconds()
    duration = int(payload.get("retrospective_seconds") or SETTINGS.project_retrospective_default_seconds)
    if len(topic_messages) >= 3 and (elapsed >= duration or not target_agents if current_topic["status"] == "debating" and manager_msgs else False or elapsed < duration):
        _set_retro_topic_status(topic_id, "closing")
        _ensure_retro_decision_job(run_id, topic_id=topic_id, title=topic_title, thread=topic_messages)
        next_topic = _next_retro_topic_candidate(run_id) if elapsed < duration else {}
        if next_topic:
            next_topic_id = _open_retro_topic(
                run_id,
                project_id=project_id,
                cycle_no=cycle_no,
                title=next_topic.get("title") or next_topic.get("topic") or "问题",
                opened_by=MANAGER_AGENT_ID,
                evidence_refs=[next_topic],
            )
            next_agents = sorted(
                {
                    agent
                    for agent in [
                        next_topic.get("owner"),
                        *str(next_topic.get("counterpart") or "").split(","),
                    ]
                    if agent in RETRO_PARTICIPANTS
                }
            ) or RETRO_PARTICIPANTS[:]
            dispatch_task(
                run_id,
                None,
                MANAGER_AGENT_ID,
                AGENT_ROLES[MANAGER_AGENT_ID],
                "全局",
                "retrospective.discussion",
                0,
                {
                    "round_no": max(msg["round_no"] for msg in topic_messages) + 1,
                    "reply_to_message_id": None,
                    "topic_id": next_topic_id,
                    "topic": next_topic.get("topic") or "问题",
                    "target_type": "team",
                    "to_agent": ",".join(next_agents),
                    "intent": "moderate",
                    "mode": "topic_shift",
                    "topic_context": next_topic.get("body") or "",
                    "next_agents": next_agents,
                },
                trace_ctx,
                project_id,
                cycle_no,
            )
            return


def _prepare_task_llm_job(task: dict) -> dict:
    phase = task["phase"]
    if phase == "proofread.decision.explanation":
        return _prepare_proofread_decision_explanation_job(task["run_id"], task["task_id"])
    if phase == "draft.revise":
        prepared = _prepare_draft_revise_job(task["run_id"], task)
        prepared["task_id"] = task["task_id"]
        return prepared
    if phase == "product.test":
        return _prepare_product_test_job(task["run_id"], task["task_id"], task["agent_id"])
    if phase == "product.report":
        return _prepare_product_report_job(task["run_id"], task["task_id"])
    if phase == "retrospective.summary":
        prepared = _prepare_retrospective_summary_job(task["run_id"])
        prepared["task_id"] = task["task_id"]
        return prepared
    raise RuntimeError(f"unsupported llm job phase {phase}")


def _ensure_task_llm_job(task: dict) -> dict:
    existing_job_id = (task.get("result") or {}).get("llm_job_id") if isinstance(task.get("result"), dict) else None
    if not existing_job_id:
        row = fetch_one("SELECT result::text FROM tasks WHERE task_id=%s", (task["task_id"],))
        result_json = _load_json(row[0]) if row and row[0] else {}
        existing_job_id = result_json.get("llm_job_id")
    if existing_job_id:
        return _llm_job_row(existing_job_id)
    prepared = _prepare_task_llm_job(task)
    config = _llm_node_config(task["phase"])
    job = _create_llm_job(
        job_key=_llm_job_key(task["phase"], task["run_id"], task["task_id"]),
        node_type=task["phase"],
        project_id=task["project_id"],
        run_id=task["run_id"],
        cycle_no=task["cycle_no"],
        task_id=task["task_id"],
        prompt_system=prepared["prompt_system"],
        prompt_user=prepared["prompt_user"],
        fallback_payload=prepared["fallback_payload"],
        provider_model=SETTINGS.llm_model,
        timeout_ms=config["timeout_ms"],
        max_attempts=config["max_attempts"],
        backoff_ms=config["backoff_ms"],
        evidence_object_count=prepared["evidence_object_count"],
    )
    _mark_task_waiting_for_job(task["task_id"], job["job_id"])
    return job


def _fail_run_due_to_llm(task: dict, job: dict) -> None:
    fail_task(task["task_id"], job.get("generation_error") or f"llm job failed: {job['node_type']}")
    execute(
        "UPDATE workflow_runs SET status='failed', current_phase=%s WHERE run_id=%s",
        (f"{task['phase']}.failed", task["run_id"]),
    )


def _complete_noncritical_llm_failure(task: dict, job: dict) -> None:
    fail_task(task["task_id"], job.get("generation_error") or f"llm job failed: {job['node_type']}")


def _handle_waiting_llm_apply_failure(task: dict, job: dict, exc: Exception) -> None:
    message = (
        f"{task['phase']} result apply failed: {exc.__class__.__name__}: {exc}"
    )
    LOGGER.exception(
        "waiting llm apply failed run_id=%s task_id=%s phase=%s job_id=%s",
        task["run_id"],
        task["task_id"],
        task["phase"],
        (task.get("result") or {}).get("llm_job_id"),
    )
    failed_job = dict(job or {})
    failed_job["generation_error"] = message
    if _llm_node_config(task["phase"]).get("critical"):
        _fail_run_due_to_llm(task, failed_job)
    else:
        fail_task(task["task_id"], message)


def _progress_waiting_llm_tasks() -> None:
    rows = fetch_all(
        """
        SELECT task_id, run_id, project_id, cycle_no, parent_task_id, agent_id, agent_role, section, phase, retry_count,
               payload::text, result::text
        FROM tasks
        WHERE status='waiting'
        ORDER BY created_at
        """
    )
    for row in rows:
        task = {
            "task_id": row[0],
            "run_id": row[1],
            "project_id": row[2],
            "cycle_no": row[3],
            "parent_task_id": row[4],
            "agent_id": row[5],
            "agent_role": row[6],
            "section": row[7],
            "phase": row[8],
            "retry_count": row[9],
            "payload": _load_json(row[10]),
            "result": _load_json(row[11]),
        }
        job_id = (task["result"] or {}).get("llm_job_id")
        if not job_id:
            continue
        job = _llm_job_row(job_id)
        if task["phase"] == "draft.revise":
            expected_source_version = _payload_int(task.get("payload"), "source_draft_version_no")
            latest_version_no = _safe_int(_latest_draft_version(task["run_id"]).get("version_no"))
            if expected_source_version and latest_version_no != expected_source_version:
                complete_task(
                    task["task_id"],
                    _draft_version_mismatch_result(
                        latest_version_no=latest_version_no,
                        expected_version_no=expected_source_version,
                        version_field="source_draft_version_no",
                    )
                    | {"llm_job_id": job_id},
                )
                continue
        if task["phase"] == "draft.revise" and _active_blocker_count(task["run_id"]) == 0:
            complete_task(
                task["task_id"],
                {
                    "status": "obsolete",
                    "message_body": "",
                    "generation_mode": job.get("generation_mode") or "obsolete",
                    "generation_error": job.get("generation_error", ""),
                    "llm_job_id": job_id,
                },
            )
            continue
        if not job or job["status"] in {"pending", "running", "retrying"}:
            continue
        if job["status"] in {"failed", "fallback"} or str(job.get("generation_mode") or "") != "llm":
            if _llm_node_config(task["phase"]).get("critical"):
                _fail_run_due_to_llm(task, job)
            else:
                _complete_noncritical_llm_failure(task, job)
            continue
        data = job["result_json"] or {}
        try:
            if task["phase"] == "proofread.decision.explanation":
                result = _apply_proofread_explanation_result(task["run_id"], task["task_id"], data)
                complete_task(task["task_id"], result)
            elif task["phase"] == "draft.revise":
                result = _apply_draft_revise_result(task["run_id"], data, task["task_id"])
                complete_task(task["task_id"], result | {"status": "revised"})
            elif task["phase"] == "product.test":
                prepared = _prepare_product_test_job(task["run_id"], task["task_id"], task["agent_id"])
                result = _apply_product_test_result(task["run_id"], task["task_id"], task["agent_id"], prepared["evidence"], data)
                complete_task(task["task_id"], result | {"status": "tested", "message_body": result["summary"]})
            elif task["phase"] == "product.report":
                result = _apply_product_report_result(task["run_id"], task["task_id"], data)
                complete_task(task["task_id"], result | {"status": "reported", "message_body": result["summary"]})
            elif task["phase"] == "retrospective.summary":
                result = _apply_retrospective_summary_result(task["run_id"], data)
                complete_task(task["task_id"], {"status": "summarized", "message_body": result["summary"], **result})
        except Exception as exc:
            _handle_waiting_llm_apply_failure(task, job, exc)


def _ensure_retro_decision_job(run_id: str, *, topic_id: str, title: str, thread: list[dict], owner_agent: str = MANAGER_AGENT_ID) -> dict:
    prepared = _prepare_retro_decision_job(run_id, topic_id=topic_id, title=title, thread=thread, owner_agent=owner_agent)
    config = _llm_node_config("retro_decision")
    return _create_llm_job(
        job_key=_llm_job_key("retro_decision", run_id, extra=topic_id),
        node_type="retro_decision",
        project_id=prepared["project_id"],
        run_id=run_id,
        cycle_no=prepared["cycle_no"],
        task_id=None,
        prompt_system=prepared["prompt_system"],
        prompt_user=prepared["prompt_user"],
        fallback_payload=prepared["fallback_payload"] | {"topic_id": topic_id, "title": title},
        provider_model=SETTINGS.llm_model,
        timeout_ms=config["timeout_ms"],
        max_attempts=config["max_attempts"],
        backoff_ms=config["backoff_ms"],
        evidence_object_count=prepared["evidence_object_count"],
    )


def _progress_retro_decision_jobs() -> None:
    for topic_id, run_id, title in fetch_all(
        """
        SELECT topic_id, run_id, title
        FROM retro_topics
        WHERE status='closing'
        ORDER BY opened_at
        """
    ):
        job = _llm_job_row(fetch_one("SELECT job_id FROM llm_jobs WHERE job_key=%s", (_llm_job_key("retro_decision", run_id, extra=topic_id),))[0]) if fetch_one("SELECT job_id FROM llm_jobs WHERE job_key=%s", (_llm_job_key("retro_decision", run_id, extra=topic_id),)) else {}
        if not job or job["status"] in {"pending", "running", "retrying"}:
            continue
        if job["status"] in {"failed", "fallback"} or str(job.get("generation_mode") or "") != "llm":
            execute("UPDATE workflow_runs SET status='failed', current_phase='retro_decision.failed' WHERE run_id=%s", (run_id,))
            continue
        thread = _retro_messages_for_topic(run_id, topic_id)
        _apply_retro_decision_result(run_id, topic_id=topic_id, title=title, thread=thread, decision=job["result_json"])
        _close_retro_topic(topic_id)


def project_tick():
    for project_id, cycle_no, run_id, status in fetch_all(
        """
        SELECT project_id, cycle_no, run_id, status
        FROM project_cycles
        WHERE status IN ('running', 'retrospective_running', 'optimizing')
        ORDER BY project_id, cycle_no
        """
    ):
        run_state = fetch_one("SELECT status FROM workflow_runs WHERE run_id=%s", (run_id,))
        if run_state and run_state[0] == "failed":
            project = _project_row(project_id)
            failures = project[6] + 1
            new_status = "paused" if failures >= project[5] else "running"
            execute(
                """
                UPDATE projects
                SET consecutive_failures=%s, status=%s, paused_reason=%s, updated_at=NOW(), next_cycle_at=NULL
                WHERE project_id=%s
                """,
                (failures, new_status, "run failed", project_id),
            )
            _sync_project_indexes(project_id)
            continue
        self_opt_done = fetch_one(
            """
            SELECT COUNT(*) FROM tasks
            WHERE project_id=%s AND cycle_no=%s AND phase='agent.optimization' AND status='completed'
            """,
            (project_id, cycle_no),
        )[0]
        if self_opt_done >= 1:
            already_done = fetch_one(
                "SELECT status FROM project_cycles WHERE project_id=%s AND cycle_no=%s",
                (project_id, cycle_no),
            )[0]
            if already_done != "completed":
                _finalize_cycle(project_id, cycle_no, run_id)

    for project in fetch_all(
        """
        SELECT project_id, status, next_cycle_at
        FROM projects
        WHERE status='running' AND next_cycle_at IS NOT NULL
        ORDER BY updated_at
        """
    ):
        project_id, _, next_cycle_at = project
        if next_cycle_at and now_local() >= next_cycle_at.astimezone():
            start_next_cycle(project_id)


def _run_current_phase(run_id: str, phase: str):
    execute("UPDATE workflow_runs SET current_phase=%s WHERE run_id=%s", (phase, run_id))


def orchestrator_tick():
    _progress_waiting_llm_tasks()
    _progress_retro_decision_jobs()
    _recover_stalled_agent_acks()
    _recover_stale_running_tasks()
    runs = fetch_all(
        """
        SELECT run_id, project_id, cycle_no, status, discussion_seconds, current_phase, started_at
        FROM workflow_runs
        WHERE status='running'
        ORDER BY started_at
        """
    )
    for run_id, project_id, cycle_no, _, discussion_seconds, _, _ in runs:
        trace_ctx = get_run_trace_context(run_id)
        cycle_started = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='cycle.start' AND status='completed'", (run_id,))[0]
        if cycle_started <= 0:
            continue
        for section, _default_agent_id in ALL_SECTION_ASSIGNMENTS:
            requirement = _section_requirement(run_id, section)
            agent_id = requirement["owner"]
            collect_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND section=%s AND agent_id=%s AND phase='material.collect'
                """,
                (run_id, section, agent_id),
            )[0]
            if collect_exists == 0:
                collect_task_id = dispatch_task(
                    run_id=run_id,
                    parent_task_id=None,
                    agent_id=agent_id,
                    agent_role=AGENT_ROLES[agent_id],
                    section=section,
                    phase="material.collect",
                    retry_count=0,
                    payload={"target_count": requirement["candidate_target"], "cycle_task_plan": (_cycle_task_plan_row(run_id) or {}).get("plan_json", {})},
                    parent_trace=trace_ctx,
                    project_id=project_id,
                    cycle_no=cycle_no,
                )
                dispatch_task(
                    run_id=run_id,
                    parent_task_id=collect_task_id,
                    agent_id=agent_id,
                    agent_role=AGENT_ROLES[agent_id],
                    section=section,
                    phase="material.submit",
                    retry_count=0,
                    payload={},
                    parent_trace=trace_ctx,
                    project_id=project_id,
                    cycle_no=cycle_no,
                )
        for section, _default_agent_id in ALL_SECTION_ASSIGNMENTS:
            requirement = _section_requirement(run_id, section)
            agent_id = requirement["owner"]
            latest_collect = fetch_one(
                """
                SELECT task_id, retry_count
                FROM tasks
                WHERE run_id=%s AND section=%s AND agent_id=%s AND phase='material.collect' AND status='completed'
                ORDER BY retry_count DESC, finished_at DESC NULLS LAST
                LIMIT 1
                """,
                (run_id, section, agent_id),
            )
            if not latest_collect:
                continue
            review_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND section=%s AND phase='material.review' AND retry_count=%s
                """,
                (run_id, section, latest_collect[1]),
            )[0]
            if review_exists == 0:
                dispatch_task(
                    run_id,
                    latest_collect[0],
                    "tester",
                    "tester",
                    section,
                    "material.review",
                    latest_collect[1],
                    {},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "material.review")

        for section, _agent_id in ALL_SECTION_ASSIGNMENTS:
            latest_review_task = fetch_one(
                """
                SELECT review_task_id
                FROM reviews
                WHERE run_id=%s AND section=%s
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (run_id, section),
            )
            if latest_review_task:
                decision_exists = fetch_one(
                    """
                    SELECT COUNT(*) FROM tasks
                    WHERE run_id=%s AND section=%s AND phase='material.review.decision' AND parent_task_id=%s
                    """,
                    (run_id, section, latest_review_task[0]),
                )[0]
                if decision_exists == 0:
                    dispatch_task(
                        run_id,
                        latest_review_task[0],
                        MANAGER_AGENT_ID,
                        AGENT_ROLES[MANAGER_AGENT_ID],
                        section,
                        "material.review.decision",
                        0,
                        {},
                        trace_ctx,
                        project_id,
                        cycle_no,
                    )

        for section, _default_agent_id in ALL_SECTION_ASSIGNMENTS:
            requirement = _section_requirement(run_id, section)
            agent_id = requirement["owner"]
            signal = _latest_manager_signal(run_id, "material.review", section)
            latest_review = fetch_one(
                """
                SELECT r.approved, r.reason, t.retry_count, r.review_task_id
                FROM reviews r
                JOIN tasks t ON t.task_id = r.review_task_id
                WHERE r.run_id=%s AND r.section=%s
                ORDER BY t.retry_count DESC, r.created_at DESC
                LIMIT 1
                """,
                (run_id, section),
            )
            signal_review_task_id = ((signal or {}).get("payload") or {}).get("review_task_id")
            if (
                latest_review
                and signal
                and signal["signal_type"] in {AUDIT_PARTIAL_PASS, AUDIT_REDO}
                and signal_review_task_id == latest_review[3]
            ):
                newer_collect_exists = fetch_one(
                    """
                    SELECT COUNT(*) FROM tasks
                    WHERE run_id=%s AND section=%s AND phase='material.collect' AND retry_count>%s
                    """,
                    (run_id, section, latest_review[2]),
                )[0]
                if newer_collect_exists == 0:
                    retry = latest_review[2] + 1
                    collect_task_id = dispatch_task(
                        run_id,
                        None,
                        agent_id,
                        AGENT_ROLES[agent_id],
                        section,
                        "material.collect",
                        retry,
                        {"target_count": requirement["candidate_target"], "rework_reason": (signal["payload"] or {}).get("reason", latest_review[1])},
                        trace_ctx,
                        project_id,
                        cycle_no,
                    )
                    dispatch_task(
                        run_id,
                        collect_task_id,
                        agent_id,
                        AGENT_ROLES[agent_id],
                        section,
                        "material.submit",
                        retry,
                        {},
                        trace_ctx,
                        project_id,
                        cycle_no,
                    )
                    _run_current_phase(run_id, "material.rework")

        approved_count = 0
        for section in ["政治经济", "科技", "体育娱乐", "其他"]:
            signal = _latest_manager_signal(run_id, "material.review", section)
            if signal and signal["signal_type"] in {AUDIT_PROCEED, AUDIT_FORCED_PROCEED}:
                approved_count += 1
        compose_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose'", (run_id,))[0]
        if approved_count == 4 and compose_exists == 0:
            dispatch_task(run_id, None, "editor", "editor", "全局", "draft.compose", 0, {}, trace_ctx, project_id, cycle_no)
            _run_current_phase(run_id, "draft.compose")

        if project_id:
            compose_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose' AND status='completed'", (run_id,))[0]
            latest_draft = _latest_draft_version(run_id)
            latest_draft_version_no = _safe_int(latest_draft.get("version_no"))
            latest_draft_stage = str(latest_draft.get("stage") or "")
            proofread_round = _proofread_round(run_id)
            any_proofread_round_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase IN ('draft.proofread','draft.recheck')
                """,
                (run_id,),
            )[0]
            proofread_for_latest_draft_exists = fetch_one(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE run_id=%s
                  AND phase IN ('draft.proofread','draft.recheck')
                  AND COALESCE(
                        NULLIF(result->>'draft_version_no','')::int,
                        NULLIF(payload->>'draft_version_no','')::int,
                        0
                      )=%s
                """,
                (run_id, latest_draft_version_no),
            )[0]
            if (
                compose_done > 0
                and latest_draft_stage == "draft"
                and latest_draft_version_no > 0
                and proofread_for_latest_draft_exists == 0
            ):
                next_proofread_round = 0 if int(any_proofread_round_exists or 0) == 0 else proofread_round + 1
                dispatch_task(
                    run_id,
                    None,
                    "tester",
                    "tester",
                    "全局",
                    "draft.proofread",
                    next_proofread_round,
                    {"draft_version_no": latest_draft_version_no},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "draft.proofread")

            proofread_gate_phase, proofread_gate_round, proofread_gate_settled = _proofread_gate_settled(run_id)
            proofread_done = fetch_one(
                """
                SELECT COUNT(*)
                FROM tasks
                WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed'
                """,
                (run_id, proofread_gate_phase, proofread_gate_round),
            )[0]
            proofread_input_draft_version_no = _safe_int(
                fetch_one(
                    """
                    SELECT COALESCE(MAX((result->>'draft_version_no')::int), 0)
                    FROM tasks
                    WHERE run_id=%s AND phase=%s AND retry_count=%s AND status='completed'
                    """,
                    (run_id, proofread_gate_phase, proofread_gate_round),
                )[0]
            )
            explanation_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.decision.explanation' AND retry_count=%s
                """,
                (run_id, proofread_gate_round),
            )[0]
            revise_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='draft.revise' AND retry_count=%s
                  AND COALESCE(NULLIF(payload->>'source_draft_version_no','')::int, %s)=%s
                """,
                (run_id, proofread_gate_round, proofread_input_draft_version_no, proofread_input_draft_version_no),
            )[0]
            blocker_count = _active_blocker_count(run_id)
            pending_proofread_resolution_count = _pending_proofread_resolution_count(run_id)
            draft_review_signal = _ensure_draft_review_signal(run_id)
            draft_review_signal_type = (draft_review_signal or {}).get("signal_type", "")
            draft_review_payload = (draft_review_signal or {}).get("payload") or {}
            if draft_review_signal_type == AUDIT_REDO:
                _dispatch_draft_compose_redo(
                    run_id,
                    project_id=project_id,
                    cycle_no=cycle_no,
                    trace_ctx=trace_ctx,
                    compose_version_no=_safe_int(draft_review_payload.get("draft_version_no")),
                    failed_audit_attempts=_safe_int(draft_review_payload.get("failed_audit_attempts")),
                    proofread_round=_safe_int(draft_review_payload.get("proofread_round")),
                    pending_resolution_count=_safe_int(draft_review_payload.get("pending_resolution_count")),
                )
            if (
                draft_review_signal_type == AUDIT_PARTIAL_PASS
                and proofread_input_draft_version_no > 0
                and pending_proofread_resolution_count > 0
                and revise_exists == 0
            ):
                dispatch_task(
                    run_id,
                    None,
                    "editor",
                    "editor",
                    "全局",
                    "draft.revise",
                    proofread_gate_round,
                    {"source_draft_version_no": proofread_input_draft_version_no},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "draft.revise")
            if draft_review_signal_type in {AUDIT_PROCEED, AUDIT_PARTIAL_PASS, AUDIT_REDO, AUDIT_FORCED_PROCEED} and explanation_exists == 0:
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "proofread.decision.explanation", proofread_gate_round, {}, trace_ctx, project_id, cycle_no)

            revised_draft_version_no = fetch_one(
                """
                SELECT COALESCE(MAX((result->>'draft_version_no')::int), 0)
                FROM tasks
                WHERE run_id=%s AND phase='draft.revise' AND retry_count=%s AND status='completed'
                  AND COALESCE(NULLIF(payload->>'source_draft_version_no','')::int, %s)=%s
                """,
                (
                    run_id,
                    proofread_gate_round,
                    proofread_input_draft_version_no,
                    proofread_input_draft_version_no,
                ),
            )[0]
            next_recheck_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='draft.recheck' AND retry_count>%s
                  AND COALESCE(NULLIF(payload->>'draft_version_no','')::int, %s)=%s
                """,
                (run_id, proofread_gate_round, _safe_int(revised_draft_version_no), _safe_int(revised_draft_version_no)),
            )[0]
            if (
                _safe_int(revised_draft_version_no) > proofread_input_draft_version_no
                and next_recheck_exists == 0
            ):
                next_round = proofread_gate_round + 1
                dispatch_task(
                    run_id,
                    None,
                    "tester",
                    "tester",
                    "全局",
                    "draft.recheck",
                    next_round,
                    {"draft_version_no": _safe_int(revised_draft_version_no)},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "draft.recheck")

            publish_decision_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='publish.decision'", (run_id,))[0]
            if (
                draft_review_signal_type in {AUDIT_PROCEED, AUDIT_FORCED_PROCEED}
                and publish_decision_exists == 0
            ):
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "publish.decision", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "publish.decision")

            report_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='report.publish'", (run_id,))[0]
            publish_signal = _latest_manager_signal(run_id, "publish.decision")
            if publish_signal and publish_signal["signal_type"] == "publish_approved" and report_exists == 0:
                dispatch_task(run_id, None, "editor", "editor", "全局", "report.publish", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "report.publish")

            report_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='report.publish' AND status='completed'",
                (run_id,),
            )[0]
            product_test_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.test'",
                (run_id,),
            )[0]
            if report_done > 0 and product_test_exists == 0:
                dispatch_task(run_id, None, "tester", "tester", "全局", "product.test", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "product.test")

            product_test_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.test' AND status='completed'",
                (run_id,),
            )[0]
            benchmark_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.benchmark'",
                (run_id,),
            )[0]
            if report_done > 0 and product_test_done >= 3 and benchmark_exists == 0:
                pass
            if report_done > 0 and product_test_done > 0 and benchmark_exists == 0:
                dispatch_task(run_id, None, "tester", "tester", "全局", "product.benchmark", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "product.benchmark")

            benchmark_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.benchmark' AND status='completed'",
                (run_id,),
            )[0]
            cross_cycle_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.cross_cycle_compare'",
                (run_id,),
            )[0]
            if report_done > 0 and product_test_done > 0 and benchmark_done > 0 and cross_cycle_exists == 0:
                dispatch_task(run_id, None, "tester", "tester", "全局", "product.cross_cycle_compare", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "product.cross_cycle_compare")

            cross_cycle_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.cross_cycle_compare' AND status='completed'",
                (run_id,),
            )[0]
            pre_retro_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='pre-retro.review'",
                (run_id,),
            )[0]
            if cross_cycle_done > 0 and pre_retro_exists == 0:
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "pre-retro.review", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "pre-retro.review")

            pre_retro_signal = _latest_manager_signal(run_id, "pre-retro.review")
            if pre_retro_signal and pre_retro_signal["signal_type"] in {AUDIT_PARTIAL_PASS, AUDIT_REDO}:
                missing_reports = ((pre_retro_signal.get("payload") or {}).get("missing_reports") or [])
                source_event_id = pre_retro_signal.get("event_id")
                for report_name, phase_name in {
                    "product_test": "product.test",
                    "benchmark_report": "product.benchmark",
                    "cross_cycle_compare_report": "product.cross_cycle_compare",
                }.items():
                    if report_name not in missing_reports:
                        continue
                    existing_recovery = fetch_one(
                        """
                        SELECT COUNT(*)
                        FROM tasks
                        WHERE run_id=%s AND phase=%s
                          AND COALESCE(payload->>'source_pre_retro_event_id','')=%s
                        """,
                        (run_id, phase_name, source_event_id),
                    )[0]
                    if int(existing_recovery or 0) > 0:
                        continue
                    next_retry = _safe_int(
                        fetch_one(
                            "SELECT COALESCE(MAX(retry_count), -1) FROM tasks WHERE run_id=%s AND phase=%s",
                            (run_id, phase_name),
                        )[0]
                    ) + 1
                    dispatch_task(
                        run_id,
                        None,
                        TESTER_AGENT_ID,
                        AGENT_ROLES[TESTER_AGENT_ID],
                        "全局",
                        phase_name,
                        next_retry,
                        {
                            "source_pre_retro_event_id": source_event_id,
                            "redo_reason": (pre_retro_signal.get("payload") or {}).get("reason", ""),
                            "missing_reports": missing_reports,
                        },
                        trace_ctx,
                        project_id,
                        cycle_no,
                    )
                    _run_current_phase(run_id, phase_name)
            retro_plan_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.plan'",
                (run_id,),
            )[0]
            if pre_retro_signal and pre_retro_signal["signal_type"] in {AUDIT_PROCEED, AUDIT_FORCED_PROCEED} and retro_plan_exists == 0:
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "retrospective.plan", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "retrospective.plan")

            retro_plan_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.plan' AND status='completed'",
                (run_id,),
            )[0]
            retro_start_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.discussion' AND agent_id=%s",
                (run_id, MANAGER_AGENT_ID),
            )[0]
            if retro_plan_done > 0 and retro_start_exists == 0:
                if report_done == 0:
                    continue
                project = _project_row(project_id)
                dispatch_task(
                    run_id,
                    None,
                    MANAGER_AGENT_ID,
                    AGENT_ROLES[MANAGER_AGENT_ID],
                    "全局",
                    "retrospective.discussion",
                    0,
                    {"retrospective_seconds": project[8], "mode": "start"},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                execute(
                    """
                    UPDATE project_cycles
                    SET status='retrospective_running', retrospective_started_at=NOW(), updated_at=NOW()
                    WHERE project_id=%s AND cycle_no=%s
                    """,
                    (project_id, cycle_no),
                )
                _run_current_phase(run_id, "retrospective.discussion")
        else:
            compose_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose' AND status='completed'", (run_id,))[0]
            discussion_start_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.start'", (run_id,))[0]
            if compose_done > 0 and discussion_start_exists == 0:
                dispatch_task(
                    run_id,
                    None,
                    MANAGER_AGENT_ID,
                    AGENT_ROLES[MANAGER_AGENT_ID],
                    "全局",
                    "discussion.start",
                    0,
                    {"discussion_seconds": discussion_seconds},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                dispatch_task(run_id, None, WORKER_33_AGENT_ID, AGENT_ROLES[WORKER_33_AGENT_ID], "全局", "discussion.comment", 0, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, WORKER_XHS_AGENT_ID, AGENT_ROLES[WORKER_XHS_AGENT_ID], "全局", "discussion.comment", 0, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "discussion.comment", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "discussion.start")

            discussion_started = fetch_one(
                """
                SELECT started_at, payload::text
                FROM tasks
                WHERE run_id=%s AND phase='discussion.start' AND status='completed'
                ORDER BY started_at DESC LIMIT 1
                """,
                (run_id,),
            )
            summarize_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.summarize'", (run_id,))[0]
            if discussion_started and summarize_exists == 0:
                started = discussion_started[0]
                payload = json.loads(discussion_started[1])
                comments_done = fetch_one(
                    "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.comment' AND status='completed'",
                    (run_id,),
                )[0]
                if comments_done >= 3 and (datetime.now(started.tzinfo) - started).total_seconds() >= payload["discussion_seconds"]:
                    dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "discussion.summarize", 0, {}, trace_ctx, project_id, cycle_no)
                    _run_current_phase(run_id, "discussion.summarize")

            summarize_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.summarize' AND status='completed'", (run_id,))[0]
            revise_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.revise'", (run_id,))[0]
            if summarize_done > 0 and revise_exists == 0:
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "draft.revise", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "draft.revise")

            revise_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.revise' AND status='completed'", (run_id,))[0]
            report_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='report.publish'", (run_id,))[0]
            if revise_done > 0 and report_exists == 0 and summarize_done > 0:
                dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "report.publish", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "report.publish")

        retro_started = fetch_one(
            """
            SELECT task_id, started_at, payload::text, result::text
            FROM tasks
            WHERE run_id=%s AND phase='retrospective.discussion' AND agent_id=%s AND status='completed'
              AND (
                COALESCE(payload->>'mode','')='start'
                OR COALESCE(result->>'status','')='started'
              )
            ORDER BY started_at DESC LIMIT 1
            """,
            (run_id, MANAGER_AGENT_ID),
        )
        retro_summary_exists = fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.summary'",
            (run_id,),
        )[0]
        if retro_started:
            started = retro_started[1]
            payload = json.loads(retro_started[2])
            _maybe_advance_retro_topic(run_id, project_id, cycle_no, trace_ctx, started, payload)
            pending_comments = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='retrospective.discussion' AND agent_id!=%s AND status IN ('pending', 'running')
                """,
                (run_id, MANAGER_AGENT_ID),
            )[0]
            retro_thread = _retro_thread_rows(run_id)
            open_or_closing_topics = fetch_one(
                """
                SELECT COUNT(*)
                FROM retro_topics
                WHERE run_id=%s
                  AND status IN ('open','debating','closing')
                  AND EXISTS (
                        SELECT 1
                        FROM retrospectives
                        WHERE retrospectives.topic_id=retro_topics.topic_id
                    )
                """,
                (run_id,),
            )[0]
            if retro_summary_exists == 0 and pending_comments == 0 and open_or_closing_topics == 0 and len(retro_thread) > 0:
                if (datetime.now(started.tzinfo) - started).total_seconds() >= payload["retrospective_seconds"]:
                    dispatch_task(run_id, None, MANAGER_AGENT_ID, AGENT_ROLES[MANAGER_AGENT_ID], "全局", "retrospective.summary", 0, {}, trace_ctx, project_id, cycle_no)
                    _run_current_phase(run_id, "retrospective.summary")

        retro_summary_done = fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.summary' AND status='completed'",
            (run_id,),
        )[0]
        self_opt_exists = fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='agent.optimization'",
            (run_id,),
        )[0]
        if retro_summary_done > 0 and self_opt_exists == 0:
            dispatch_task(
                run_id,
                None,
                MANAGER_AGENT_ID,
                AGENT_ROLES[MANAGER_AGENT_ID],
                "全局",
                "agent.optimization",
                0,
                {},
                trace_ctx,
                project_id,
                cycle_no,
            )
            if project_id:
                execute(
                    """
                    UPDATE project_cycles
                    SET status='optimizing', updated_at=NOW()
                    WHERE project_id=%s AND cycle_no=%s
                    """,
                    (project_id, cycle_no),
                )
            _run_current_phase(run_id, "agent.optimization")


def run_worker(agent_id: str):
    service_name = "orchestrator" if agent_id == "orchestrator" else f"agent-{agent_id}"
    while True:
        ack_task = claim_ack_task(agent_id)
        if ack_task:
            ack = _evaluate_agent_ack(ack_task)
            ack_id = _insert_agent_ack(ack_task, ack)
            complete_agent_ack(ack_task["task_id"], ack_id, ack)
            time.sleep(0.2)
            continue
        task = claim_task(agent_id)
        if not task:
            time.sleep(2)
            continue
        LOGGER.info(
            "worker.task.claimed agent_id=%s task_id=%s run_id=%s phase=%s section=%s retry_count=%s",
            agent_id,
            task["task_id"],
            task["run_id"],
            task["phase"],
            task["section"],
            task["retry_count"],
        )
        attrs = {
            "workflow_id": WORKFLOW_ID,
            "project_id": task["project_id"],
            "cycle_no": task["cycle_no"],
            "run_id": task["run_id"],
            "task_id": task["task_id"],
            "parent_task_id": task["parent_task_id"],
            "agent_id": task["agent_id"],
            "agent_role": task["agent_role"],
            "section": task["section"],
            "phase": task["phase"],
            "retry_count": task["retry_count"],
            "status": "running",
        }
        try:
            with workflow_span(service_name, task["phase"], attrs, context=extract_context(task["payload"].get("trace_context"))) as span:
                if task["phase"] == "material.collect":
                    requirement = task["payload"].get("section_target") or _section_requirement(task["run_id"], task["section"])
                    target_count = int(task["payload"].get("target_count") or requirement["candidate_target"])
                    items = collect_news(task["section"], target_count)
                    items = _apply_collection_guidance(items, task["payload"].get("optimization_log_snapshot") or {})
                    memory_note = _memory_summary(task["payload"].get("agent_memory_snapshot"))
                    optimization_log = task["payload"].get("optimization_log_snapshot") or {}
                    items = _enrich_collected_materials(
                        task["section"],
                        items,
                        target_count=target_count,
                        quality_requirements=task["payload"].get("quality_requirements") or {},
                        manager_watchpoints=task["payload"].get("manager_watchpoints") or [],
                        copy_requirements=task["payload"].get("copy_requirements") or {},
                        memory_summary=memory_note,
                        optimization_log=optimization_log,
                    )
                    save_materials(task["run_id"], task["task_id"], task["section"], task["agent_id"], items)
                    span.set_attribute("status", "collected")
                    span.set_attribute("source_count", len(items))
                    complete_task(
                        task["task_id"],
                        {
                            "status": "collected",
                            "source_count": len(items),
                            "memory_summary": memory_note,
                            "target_count": target_count,
                            "message_body": "",
                        },
                    )
                    LOGGER.info(
                        "worker.task.completed agent_id=%s task_id=%s run_id=%s phase=%s section=%s source_count=%s",
                        agent_id,
                        task["task_id"],
                        task["run_id"],
                        task["phase"],
                        task["section"],
                        len(items),
                    )
                elif task["phase"] == "cycle.start":
                    result = start_cycle(task["run_id"], task["task_id"])
                    span.set_attribute("status", "started")
                    complete_task(task["task_id"], result)
                elif task["phase"] == "material.submit":
                    count = fetch_one(
                        "SELECT COUNT(*) FROM materials WHERE run_id=%s AND task_id=%s",
                        (task["run_id"], task["task_id"]),
                    )[0]
                    rows = fetch_all(
                        """
                        SELECT id, title, source_media, published_at, link, images::text, summary_zh, brief_zh, metadata::text
                        FROM materials
                        WHERE run_id=%s AND task_id=%s
                        ORDER BY published_at DESC
                        """,
                        (task["run_id"], task["task_id"]),
                    )
                    full_items = [
                        {
                            "material_id": row[0],
                            "title": row[1],
                            "source_media": row[2],
                            "published_at": row[3].isoformat() if row[3] else "",
                            "link": row[4],
                            "image_count": len(json.loads(row[5] or "[]")),
                            "summary_zh": row[6] or "",
                            "brief_zh": row[7] or "",
                            "relevance_note": (_load_json(row[8]) or {}).get("relevance_note", ""),
                        }
                        for row in rows
                    ]
                    detail_url = f"{SETTINGS.public_base_url}:5555/newsflow/runs/{task['run_id']}/materials.html"
                    span.set_attribute("status", "submitted")
                    span.set_attribute("source_count", count)
                    complete_task(
                        task["task_id"],
                        {
                            "status": "submitted",
                            "source_count": count,
                            "submitted_materials": full_items,
                            "detail_url": detail_url,
                            "message_body": "",
                        },
                    )
                elif task["phase"] == "material.review":
                    result = review_section(task["run_id"], task["section"], task["task_id"])
                    phase = "material.reject" if not result["approved"] else "material.review"
                    with workflow_span(service_name, phase, attrs | {"status": "rejected" if not result["approved"] else "approved"}):
                        pass
                    span.set_attribute("status", "approved" if result["approved"] else "rejected")
                    span.set_attribute("source_count", len(result["selected_material_ids"]))
                    complete_task(
                        task["task_id"],
                        result
                        | {
                            "status": "approved" if result["approved"] else "rejected",
                            "detail_url": f"{SETTINGS.public_base_url}:5555/newsflow/runs/{task['run_id']}/material-review.html",
                        },
                    )
                    LOGGER.info(
                        "worker.task.completed agent_id=%s task_id=%s run_id=%s phase=%s section=%s approved=%s selected_material_count=%s",
                        agent_id,
                        task["task_id"],
                        task["run_id"],
                        task["phase"],
                        task["section"],
                        result["approved"],
                        len(result["selected_material_ids"]),
                    )
                elif task["phase"] == "material.review.decision":
                    result = manager_review_materials(task["run_id"], task["task_id"], task["section"])
                    span.set_attribute("status", result["signal_type"])
                    complete_task(task["task_id"], result)
                    LOGGER.info(
                        "worker.task.completed agent_id=%s task_id=%s run_id=%s phase=%s section=%s signal_type=%s reason=%s",
                        agent_id,
                        task["task_id"],
                        task["run_id"],
                        task["phase"],
                        task["section"],
                        result.get("signal_type", ""),
                        _truncate(result.get("reason", ""), 96),
                    )
                elif task["phase"] == "draft.compose":
                    result = compose_draft(task["run_id"], task["task_id"])
                    span.set_attribute("status", "drafted")
                    complete_task(task["task_id"], result | {"status": "drafted", "draft_len": len(result["draft_markdown"])})
                elif task["phase"] == "draft.proofread":
                    expected_draft_version_no = _payload_int(task.get("payload"), "draft_version_no")
                    latest_draft_version_no = _safe_int(_latest_draft_version(task["run_id"]).get("version_no"))
                    if expected_draft_version_no and latest_draft_version_no != expected_draft_version_no:
                        complete_task(
                            task["task_id"],
                            _draft_version_mismatch_result(
                                latest_version_no=latest_draft_version_no,
                                expected_version_no=expected_draft_version_no,
                                version_field="draft_version_no",
                            ),
                        )
                        continue
                    result = submit_proofread_issues(task["run_id"], task["task_id"], task["agent_id"])
                    decision = decide_proofread_issues(task["run_id"], task["task_id"])
                    result = result | {
                        "decision_summary": decision["message_body"],
                        "accepted_count": decision["accepted_count"],
                        "blocker_count_after_decision": decision["blocker_count_after_decision"],
                        "recheck_required": decision["recheck_required"],
                        "required_actions": decision["required_actions"],
                        "decision_html_path": decision["html_path"],
                        "detail_url": f"{SETTINGS.public_base_url}:5555/newsflow/runs/{task['run_id']}/proofread.html",
                    }
                    span.set_attribute("status", "submitted")
                    span.set_attribute("issue.count", result["issue_count"])
                    span.set_attribute("proofread.blocker_count_after_decision", result["blocker_count_after_decision"])
                    complete_task(task["task_id"], result)
                elif task["phase"] == "proofread.decision.explanation":
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "draft.revise":
                    expected_source_draft_version_no = _payload_int(task.get("payload"), "source_draft_version_no")
                    latest_draft_version_no = _safe_int(_latest_draft_version(task["run_id"]).get("version_no"))
                    if expected_source_draft_version_no and latest_draft_version_no != expected_source_draft_version_no:
                        complete_task(
                            task["task_id"],
                            _draft_version_mismatch_result(
                                latest_version_no=latest_draft_version_no,
                                expected_version_no=expected_source_draft_version_no,
                                version_field="source_draft_version_no",
                            ),
                        )
                        continue
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "draft.recheck":
                    expected_draft_version_no = _payload_int(task.get("payload"), "draft_version_no")
                    latest_draft_version_no = _safe_int(_latest_draft_version(task["run_id"]).get("version_no"))
                    if expected_draft_version_no and latest_draft_version_no != expected_draft_version_no:
                        complete_task(
                            task["task_id"],
                            _draft_version_mismatch_result(
                                latest_version_no=latest_draft_version_no,
                                expected_version_no=expected_draft_version_no,
                                version_field="draft_version_no",
                            ),
                        )
                        continue
                    result = recheck_proofread_issues(task["run_id"], task["task_id"], task["agent_id"])
                    span.set_attribute("status", "rechecked")
                    complete_task(
                        task["task_id"],
                        result | {"detail_url": f"{SETTINGS.public_base_url}:5555/newsflow/runs/{task['run_id']}/recheck.html"},
                    )
                elif task["phase"] == "publish.decision":
                    result = manager_publish_decision(task["run_id"], task["task_id"])
                    span.set_attribute("status", "approved" if result["approved"] else "rejected")
                    complete_task(task["task_id"], result)
                elif task["phase"] == "report.publish":
                    result = publish_report(task["run_id"])
                    span.set_attribute("status", "published")
                    complete_task(task["task_id"], result | {"status": "published"})
                elif task["phase"] == "product.test":
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "product.benchmark":
                    result = create_benchmark_report(task["run_id"], task["task_id"])
                    span.set_attribute("status", "benchmarked")
                    complete_task(task["task_id"], result | {"status": "benchmarked", "message_body": result["summary"]})
                elif task["phase"] == "product.cross_cycle_compare":
                    result = create_cross_cycle_compare_report(task["run_id"], task["task_id"])
                    span.set_attribute("status", "compared")
                    complete_task(task["task_id"], result | {"status": "compared", "message_body": result["summary"]})
                elif task["phase"] == "pre-retro.review":
                    result = manager_pre_retro_review(task["run_id"], task["task_id"])
                    span.set_attribute("status", result["signal_type"])
                    complete_task(task["task_id"], result)
                elif task["phase"] == "retrospective.plan":
                    result = create_retrospective_plan(task["run_id"], task["task_id"])
                    span.set_attribute("status", "planned")
                    complete_task(task["task_id"], result | {"status": "planned", "message_body": result["summary"]})
                elif task["phase"] == "retrospective.discussion":
                    if task["agent_id"] == MANAGER_AGENT_ID and (task["payload"].get("mode") or "start") == "start":
                        seconds = task["payload"].get("retrospective_seconds", SETTINGS.project_retrospective_default_seconds)
                        kickoff = start_retrospective_thread(task["run_id"], task["task_id"])
                        project_id, cycle_no = get_run_project_context(task["run_id"])
                        trace_ctx = get_run_trace_context(task["run_id"])
                        next_agents = [agent for agent in (kickoff.get("next_agents") or RETRO_PARTICIPANTS[:]) if agent in RETRO_PARTICIPANTS]
                        if not next_agents:
                            next_agents = RETRO_PARTICIPANTS[:]
                        _dispatch_retro_comment_tasks(
                            task["run_id"],
                            project_id,
                            cycle_no,
                            trace_ctx,
                            round_no=1,
                            agents=next_agents,
                            reply_to_message_id=kickoff["message_id"],
                            topic_id=kickoff["topic_id"],
                            topic=kickoff["topic"],
                            target_type="agent",
                            to_agent=MANAGER_AGENT_ID,
                            intent="critique",
                        )
                        span.set_attribute("status", "started")
                        complete_task(
                            task["task_id"],
                            {
                                "status": "started",
                                "retrospective_seconds": seconds,
                                "topic_id": kickoff["topic_id"],
                                "message_id": kickoff["message_id"],
                                "next_agents": kickoff["next_agents"],
                                "topic": kickoff["topic"],
                                "message_body": kickoff["body"],
                            },
                        )
                    else:
                        comment = create_retrospective_comment(task["run_id"], task["task_id"], task["agent_id"], task["payload"])
                        span.set_attribute("status", "commented")
                        complete_task(
                            task["task_id"],
                            {
                                "status": "commented",
                                "topic_id": comment["topic_id"],
                                "message_id": comment["message_id"],
                                "reply_to_message_id": comment["reply_to_message_id"],
                                "topic": comment["topic"],
                                "intent": comment["intent"],
                                "to_agent": comment["to_agent"],
                                "comment_text": comment["body"],
                                "message_body": comment["body"],
                            },
                        )
                elif task["phase"] == "retrospective.start":
                    seconds = task["payload"].get("retrospective_seconds", SETTINGS.project_retrospective_default_seconds)
                    kickoff = start_retrospective_thread(task["run_id"], task["task_id"])
                    project_id, cycle_no = get_run_project_context(task["run_id"])
                    trace_ctx = get_run_trace_context(task["run_id"])
                    next_agents = [agent for agent in (kickoff.get("next_agents") or [WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID]) if agent in {WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID}]
                    if not next_agents:
                        next_agents = [WORKER_33_AGENT_ID, WORKER_XHS_AGENT_ID]
                    _dispatch_retro_comment_tasks(
                        task["run_id"],
                        project_id,
                        cycle_no,
                        trace_ctx,
                        round_no=1,
                        agents=next_agents,
                        reply_to_message_id=kickoff["message_id"],
                        topic_id=kickoff["topic_id"],
                        topic=kickoff["topic"],
                        target_type="agent",
                        to_agent=MANAGER_AGENT_ID,
                        intent="critique",
                    )
                    for agent in next_agents:
                        execute(
                            "UPDATE tasks SET payload=jsonb_set(payload,'{mode}',to_jsonb(%s::text),true) WHERE run_id=%s AND phase='retrospective.comment' AND agent_id=%s AND COALESCE((payload->>'round_no')::int,0)=1",
                            ("open", task["run_id"], agent),
                        )
                    span.set_attribute("status", "started")
                    complete_task(
                        task["task_id"],
                        {
                            "status": "started",
                            "retrospective_seconds": seconds,
                            "topic_id": kickoff["topic_id"],
                            "message_id": kickoff["message_id"],
                            "next_agents": kickoff["next_agents"],
                            "topic": kickoff["topic"],
                            "controversies": kickoff.get("controversies", []),
                            "message_body": kickoff["body"],
                        },
                    )
                elif task["phase"] == "retrospective.comment":
                    comment = create_retrospective_comment(task["run_id"], task["task_id"], task["agent_id"], task["payload"])
                    span.set_attribute("status", "commented")
                    complete_task(
                        task["task_id"],
                        {
                            "status": "commented",
                            "topic_id": comment["topic_id"],
                            "message_id": comment["message_id"],
                            "reply_to_message_id": comment["reply_to_message_id"],
                            "topic": comment["topic"],
                            "intent": comment["intent"],
                            "to_agent": comment["to_agent"],
                            "comment_text": comment["body"],
                            "message_body": comment["body"],
                        },
                    )
                elif task["phase"] == "retrospective.summary":
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "agent.optimization":
                    result = manager_write_agent_optimizations(task["run_id"], task["task_id"])
                    span.set_attribute("status", "optimized")
                    complete_task(task["task_id"], result)
                elif task["phase"] == "agent.self_optimize":
                    memory = self_optimize_agent(task["run_id"], task["agent_id"])
                    span.set_attribute("status", "optimized")
                    complete_task(task["task_id"], {"status": "optimized", "message_body": memory["summary"], "memory_version": memory["version_cycle"]})
                else:
                    raise RuntimeError(f"unknown phase {task['phase']}")
        except Exception as exc:
            LOGGER.exception(
                "worker.task.failed agent_id=%s task_id=%s run_id=%s phase=%s section=%s",
                agent_id,
                task["task_id"],
                task["run_id"],
                task["phase"],
                task["section"],
            )
            fail_task(task["task_id"], str(exc))
            time.sleep(1)
