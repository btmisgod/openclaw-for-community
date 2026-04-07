from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from deep_translator import GoogleTranslator

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
WORKFLOW_ID = "intl-news-hotspots"
RUN_OUTPUT_DIR = ROOT / "output"
PROJECT_OUTPUT_DIR = ROOT / "projects"
TRANSLATOR = GoogleTranslator(source="auto", target="zh-CN")
AGENT_SECTIONS = {
    "33": ["政治经济", "科技"],
    "xhs": ["体育娱乐", "其他"],
}
AGENT_ROLES = {
    "neko": "manager",
    "33": "collector",
    "xhs": "collector",
}
ALL_SECTION_ASSIGNMENTS = [
    ("政治经济", "33"),
    ("科技", "33"),
    ("体育娱乐", "xhs"),
    ("其他", "xhs"),
]
RETRO_REQUIRED_TOPICS = ["问题", "堵点", "技能缺失", "作品优化", "协作优化"]
RETRO_MIN_THREAD_MESSAGES = 4
BENCHMARK_URLS = [
    ("BBC News", "https://www.bbc.com/news"),
    ("Reuters World", "https://www.reuters.com/world/"),
    ("Google News", "https://news.google.com/topstories?hl=en-US&gl=US&ceid=US:en"),
]

LLM_NODE_CONFIG = {
    "proofread.decision.explanation": {"timeout_ms": 60000, "max_attempts": 2, "backoff_ms": 5000, "critical": False, "max_completion_tokens": 260},
    "draft.revise": {"timeout_ms": 150000, "max_attempts": 2, "backoff_ms": 12000, "critical": True, "max_completion_tokens": 450},
    "product.test": {"timeout_ms": 120000, "max_attempts": 2, "backoff_ms": 10000, "critical": False, "max_completion_tokens": 380},
    "product.report": {"timeout_ms": 140000, "max_attempts": 2, "backoff_ms": 12000, "critical": True, "max_completion_tokens": 450},
    "retro_decision": {"timeout_ms": 110000, "max_attempts": 2, "backoff_ms": 10000, "critical": False, "max_completion_tokens": 320},
    "retrospective.summary": {"timeout_ms": 140000, "max_attempts": 2, "backoff_ms": 12000, "critical": True, "max_completion_tokens": 520},
}

PROOFREAD_BLOCKER_TYPES = {"lead_sentence_rule", "fact_integrity"}


def now_local() -> datetime:
    return datetime.now().astimezone()


def now_iso() -> str:
    return now_local().isoformat()


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
        UPDATE tasks
        SET status='pending', started_at=NULL
        WHERE status='running'
        """
    )
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


def process_llm_jobs() -> None:
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
        model_latency_ms = int((finished - started).total_seconds() * 1000)
        if job["attempt_count"] < job["max_attempts"]:
            _retry_llm_job(job, generation_error=error_text, model_latency_ms=model_latency_ms, finished_at=finished)
            return
        config = _llm_node_config(job["node_type"])
        if config["critical"]:
            _complete_llm_job(
                job["job_id"],
                status="failed",
                generation_mode="failed",
                generation_error=error_text,
                result_json={
                    **job["fallback_payload"],
                    "generation_mode": "failed",
                    "generation_error": error_text,
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
                status="fallback",
                generation_mode="fallback",
                generation_error=error_text,
                result_json={
                    **job["fallback_payload"],
                    "generation_mode": "fallback",
                    "generation_error": error_text,
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
    for topic in RETRO_REQUIRED_TOPICS:
        if topic in value:
            return topic
    return value or "问题"


def _truncate(text: str | None, limit: int = 80) -> str:
    value = (text or "").strip().replace("\n", " ")
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


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
    defaults = {
        "政治经济": "政治经济板块的热点阈值和影响句还不够靠前",
        "科技": "科技板块容易把同源事件堆在一起，影响主副推区分",
        "体育娱乐": "体育娱乐板块的话题边界和主推权重还不够稳",
        "其他": "其他板块的题材边界和图片稳定性需要更早收紧",
    }
    suffix = defaults.get(section, "该板块仍有前置检查不足")
    return f"{section}：{suffix if approved else value or suffix}"


def _product_report_by_type(run_id: str, report_type: str) -> dict:
    return next((row for row in _product_report_rows(run_id) if row["report_type"] == report_type), {})


def _main_titles(run_id: str) -> dict[str, str]:
    final_json = (_load_output_bundle(run_id).get("final_json") or {})
    titles = {}
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        titles[section] = ((final_json.get(section) or {}).get("main") or {}).get("title", "")
    return titles


def _pick_retro_controversies(run_id: str) -> list[dict]:
    titles = _main_titles(run_id)
    reviews = fetch_all(
        """
        SELECT section, approved, reason
        FROM reviews
        WHERE run_id=%s
        ORDER BY created_at
        """,
        (run_id,),
    )
    evaluation = _product_report_by_type(run_id, "product_evaluation_report")
    benchmark = _product_report_by_type(run_id, "benchmark_report")
    issues: list[dict] = []
    for section, approved, reason in reviews:
        if approved:
            continue
        owner = "33" if section in {"政治经济", "科技"} else "xhs"
        counterpart = "xhs" if owner == "33" else "33"
        issues.append(
            {
                "topic": "执行判断前置",
                "owner": owner,
                "counterpart": counterpart,
                "body": f"{section} 板块在《{titles.get(section) or section}》这条主线上，{(reason or '').strip() or '关键判断仍然拖到 review 才暴露'}",
            }
        )
    for issue in (evaluation.get("report_json", {}) or {}).get("top_product_issues", [])[:2]:
        owner = "33" if any(term in issue for term in ["政治经济", "科技", "同源", "信息密度"]) else "xhs"
        issues.append(
            {
                "topic": "成品阅读体验",
                "owner": owner,
                "counterpart": "neko",
                "body": issue,
            }
        )
    if benchmark:
        gap = benchmark["report_json"].get("most_visible_gap", "")
        if gap:
            issues.append(
                {
                    "topic": "外部对标差距",
                    "owner": "neko",
                    "counterpart": "33,xhs",
                    "body": gap,
                }
            )
    dedup = []
    seen = set()
    for item in issues:
        key = (item["topic"], item["owner"], item["body"])
        if key in seen:
            continue
        seen.add(key)
        dedup.append(item)
    return dedup[:2]


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
    controversies = _pick_retro_controversies(run_id)
    lines = ["这轮先不做礼貌性收口，只抓最值得争的点。"]
    next_agents: list[str] = []
    for idx, item in enumerate(controversies, 1):
        lines.append(f"{idx}. {item['body']}")
        if item["owner"] in {"33", "xhs"}:
            next_agents.append(item["owner"])
        counterparts = [agent.strip() for agent in str(item["counterpart"]).split(",") if agent.strip() in {"33", "xhs"}]
        next_agents.extend(counterparts)
    if not next_agents:
        next_agents = ["33", "xhs"]
    next_agents = list(dict.fromkeys(next_agents))
    lines.append("先把其中一处讲透：谁认为它最伤成品或最拖流程，就直接拿工件说话，不要复述流程。")
    return {
        "topic": controversies[0]["topic"] if controversies else "问题",
        "intent": "moderate",
        "target_type": "team",
        "to_agent": ",".join(next_agents),
        "body": " ".join(lines),
        "next_agents": next_agents,
        "controversies": controversies,
        "first_topic": controversies[0] if controversies else {},
        "next_topic": controversies[1] if len(controversies) > 1 else {},
    }


def _local_retro_comment(agent_id: str, payload: dict, relevant_context: dict) -> dict:
    mode = payload.get("mode") or "open"
    reply_text = relevant_context.get("reply_text") or ""
    own_reviews = relevant_context.get("own_reviews") or []
    other_reviews = relevant_context.get("other_reviews") or []
    peer_message = relevant_context.get("peer_message") or ""
    memory_summary = relevant_context.get("memory_summary") or "默认基线策略"
    product_signals = relevant_context.get("product_signals") or []
    product_tests = relevant_context.get("product_tests") or {}
    benchmark_summary = relevant_context.get("benchmark_summary") or ""
    final_titles = relevant_context.get("final_titles") or {}
    peer_agent = "xhs" if agent_id == "33" else "33"
    own_problem = _first_nonempty(own_reviews, "本板块一些关键判断还是拖到了 review 才暴露")
    other_problem = _first_nonempty(other_reviews, "另一侧也有问题和这边是同一类前置不足")
    product_problem = _first_nonempty(product_signals, "终稿第一屏完成度和板块收束感还不够稳")
    own_product_view = _first_nonempty(product_tests.get(agent_id, []), product_problem)
    peer_product_view = _first_nonempty(product_tests.get(peer_agent, []), other_problem)
    focus_title = _first_nonempty(
        [title for section, title in final_titles.items() if title and ((agent_id == "33" and section in {"政治经济", "科技"}) or (agent_id == "xhs" and section in {"体育娱乐", "其他"}))],
        "本轮主推",
    )
    if agent_id == "neko":
        body = "我不准备把讨论放回空泛总结。"
        if reply_text:
            body += f" 刚才最值得继续掰开的，是这句里暴露出来的取舍：{_truncate(reply_text, 120)}。"
        if benchmark_summary:
            body += f" 对标报告已经提醒我们，{_truncate(benchmark_summary, 90)}。"
        if mode == "topic_shift" and payload.get("topic_context"):
            body = f"第一个话题先收口。现在切到第二个更该讲透的问题：{_truncate(payload.get('topic_context'), 120)}。请相关人只围绕这个点继续讲清责任边界和改法。"
        body += " 现在请把责任边界讲清楚：谁应该更早把这个问题拦住，谁来承担下一轮的第一道硬检查。"
        return {
            "topic": "取舍与责任",
            "intent": "question",
            "target_type": "agent",
            "to_agent": payload.get("to_agent") or "33,xhs",
            "body": body,
            "next_agents": [agent.strip() for agent in str(payload.get("to_agent") or "33,xhs").split(",") if agent.strip() in {"33", "xhs"}],
        }
    if mode == "open":
        if agent_id == "33":
            body = (
                f"我先挑《{focus_title}》这类条目说。真正拖后腿的不是素材量，而是判断句太晚出现，"
                f"结果像“{_truncate(own_product_view, 80)}”这种问题一直拖到成稿里才被看见。"
                "这会把政治经济和科技板块做成信息堆，而不是读者一眼能抓住的热点整理。"
            )
        else:
            body = (
                f"我更在意的是成品扫读感。像《{focus_title}》这一类内容，"
                f"如果题材边界和图片稳定性没有先拦住，最后就会落成“{_truncate(own_product_view, 80)}”这种阅读体验问题。"
                "这不是排版能补回来的，而是前置筛选没有硬起来。"
            )
        return {
            "topic": payload.get("topic") or "问题",
            "intent": "critique",
            "target_type": "artifact",
            "to_agent": "neko",
            "body": body,
            "next_agents": [],
        }
    if mode == "peer_challenge":
        if agent_id == "33":
            body = (
                f"xhs 把问题压在图片和边界上，这个判断没错，但我不同意把主因全放在后段。"
                f"政治经济/科技这边先出现的是“{_truncate(own_problem, 70)}”，"
                f"它会直接把后面的主推层级带平。要是这一步不先收紧，后面再怎么补图都只是补救。"
            )
        else:
            body = (
                f"33 把重点放在影响句和同源去重，这一半我同意；另一半我不同意。"
                f"如果“{_truncate(peer_product_view, 70)}”还在，读者先感受到的仍然是成品松，不会先去体会信息密度。"
                "所以我坚持把题材边界和图片可用性提前成硬门槛。"
            )
        return {
            "topic": "分歧点",
            "intent": "critique",
            "target_type": "agent",
            "to_agent": peer_agent,
            "body": body,
            "next_agents": [],
        }
    if mode == "final_position":
        if agent_id == "33":
            body = (
                f"我认领的第一责任是 {own_problem}。"
                "下一轮我会先在采集阶段卡掉同源并排和影响句缺失，再把能不能做主推的判断提前。"
                "这样即使图片问题还存在，也不会先把主线判断拖垮。"
            )
        else:
            body = (
                f"我认领的是 {own_problem}。"
                "下一轮我会先卡题材边界和图片可用性，再把短讯压到更利落。"
                "如果这一步不先做，后面所有关于节奏和首屏的优化都会被稀释。"
            )
        return {
            "topic": "下一轮取舍",
            "intent": "proposal",
            "target_type": "agent",
            "to_agent": "neko",
            "body": body,
            "next_agents": [],
        }
    body = (
        f"我补一条还值得保留的判断：{_truncate(own_problem, 80)}。"
        f"这件事跟“{_truncate(product_problem, 70)}”其实连在一起，所以我会把当前基线“{_truncate(memory_summary, 70)}”里的宽松部分直接删掉。"
    )
    return {
        "topic": "作品优化",
        "intent": "proposal",
        "target_type": "team",
        "to_agent": "neko",
        "body": body,
        "next_agents": [],
    }


def _local_retro_summary(run_id: str, thread: list[dict]) -> dict:
    reviews = fetch_all(
        "SELECT section, approved, reason FROM reviews WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    rejected = [_review_signal(section, approved, reason) for section, approved, reason in reviews if not approved]
    approved = [_review_signal(section, approved, reason) for section, approved, reason in reviews if approved]
    by_agent: dict[str, list[str]] = {}
    for msg in thread:
        by_agent.setdefault(msg["from_agent"], []).append(msg["body"])
    product_eval = next(
        (row for row in _product_report_rows(run_id) if row["report_type"] == "product_evaluation_report"),
        None,
    )
    product_issue = ""
    if product_eval:
        product_issue = _first_nonempty(product_eval["report_json"].get("top_product_issues", []), "")
    problem_line = _unique_join(
        [
            _first_nonempty(rejected, ""),
            product_issue,
            _truncate(by_agent.get("33", [""])[0], 80) if by_agent.get("33") else "",
            _truncate(by_agent.get("xhs", [""])[0], 80) if by_agent.get("xhs") else "",
        ]
    ) or "本轮最明显的问题，是多项质量判断仍然靠后置 review 才暴露。"
    cause_line = (
        "采集阶段的规则前置还不够硬，图片稳定性、热点阈值、去重和交接标准没有在 worker 侧先收紧，"
        "导致问题沿着流程一路传到正文和复盘。"
    )
    fix_line = (
        "下一轮把采集前自检、板块边界、图片可用性和主推影响句前置；"
        "worker 提交时写清交接说明，manager 在初审时只盯最关键的硬约束，不再把模糊建议留到最后。"
    )
    if approved:
        fix_line += f" 本轮已经跑顺的部分可保留：{_truncate('；'.join(approved[:2]), 90)}。"
    assign_line = (
        "33 负责把政治经济/科技的热点阈值、去重和影响句前置；"
        "xhs 负责收紧体育娱乐/其他的题材边界与图片稳定性；"
        "neko 负责在 review 前明确硬标准，并在复盘中继续抓住分歧追问，不让讨论空转。"
    )
    return {
        "summary": "\n".join(
            [
                f"问题：{problem_line}",
                f"原因：{cause_line}",
                f"改法：{fix_line}",
                f"下轮责任分配：{assign_line}",
            ]
        )
    }


def _local_self_optimize(agent_id: str, cycle_no: int, summary: str, previous: dict, relevant: list[dict], blueprint: dict) -> dict:
    directed = []
    self_ack = []
    for msg in relevant:
        if msg["from_agent"] != agent_id:
            directed.append(msg["body"])
        else:
            self_ack.append(msg["body"])
    exposed = []
    if directed:
        exposed.append(f"别人指出：{_truncate(directed[0], 120)}")
    if self_ack:
        exposed.append(f"我认领：{_truncate(self_ack[0], 120)}")
    if not exposed:
        exposed.append("本轮暴露出规则前置不足，容易把质量问题拖到后置 review。")
    next_strategy = list(blueprint.get("execution_strategy", []))
    next_checks = list(blueprint.get("quality_checks", []))
    if directed:
        next_strategy.append(f"针对复盘指出的问题，优先修正：{_truncate(directed[0], 60)}")
    if self_ack:
        next_checks.append(f"新增自检：确认“{_truncate(self_ack[0], 50)}”不再重复出现")
    role_plan = (
        f"{agent_id} 下一轮会把别人点到的问题前置处理，并把复盘总结里的要求拆成可执行检查。"
        f" 当前收敛基线：{_truncate(summary, 120)}"
    )
    return {
        "summary": blueprint["summary"],
        "exposed_issues": exposed,
        "next_cycle_strategy": next_strategy,
        "next_cycle_quality_checks": next_checks,
        "role_improvement_plan": role_plan,
    }


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
        for item in secondary[:2]:
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


def _apply_writer_guidance(sections_payload: dict, optimization_log: dict) -> dict:
    lead_rules = _compiled_rule_payload(optimization_log, "lead_sentence_rule")
    short_rules = _compiled_rule_payload(optimization_log, "short_brief_compression_rule")
    impact_first = any((rule.get("style") or "") == "impact_first" for rule in lead_rules)
    brief_limit = 50
    for rule in short_rules:
        brief_limit = min(brief_limit, int(rule.get("max_chars") or 50))
    if not impact_first and brief_limit == 50:
        return sections_payload
    updated = json.loads(json.dumps(sections_payload, ensure_ascii=False))
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        section_data = updated.get(section) or {}
        main = section_data.get("main") or {}
        if impact_first and main.get("summary_zh"):
            summary = (main.get("summary_zh") or "").strip()
            if summary.startswith("据"):
                main["summary_zh"] = f"最值得关注的是，{summary[1:]}" if len(summary) > 1 else "最值得关注的是，这条新闻直接影响本轮热点判断。"
                section_data["main"] = main
        briefs = section_data.get("briefs") or []
        compressed = []
        for brief in briefs:
            item = dict(brief)
            summary = (item.get("summary_zh") or "").strip()
            if len(summary) > brief_limit:
                item["summary_zh"] = summary[: brief_limit - 1].rstrip("，。；;,. ") + "。"
            compressed.append(item)
        section_data["briefs"] = compressed
        updated[section] = section_data
    return updated


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
) -> str:
    task_id = uuid.uuid4().hex[:16]
    execute(
        """
        INSERT INTO tasks(task_id, run_id, project_id, cycle_no, parent_task_id, agent_id, agent_role, section, phase, retry_count, status, payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)
        """,
        (
            task_id,
            run_id,
            project_id,
            cycle_no,
            parent_task_id,
            agent_id,
            agent_role,
            section,
            phase,
            retry_count,
            jdump(payload),
        ),
    )
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
            if phase == "material.collect" and retry_count == 0:
                enriched_payload["message_body"] = (
                    f"请采集【{section}】板块近24小时国际热点，提交不少于 {payload.get('target_count', 12)} 条候选素材，"
                    "并保留图片、来源、发布时间、原文链接。"
                )
            elif phase == "material.collect" and retry_count > 0:
                enriched_payload["message_body"] = (
                    f"请重做【{section}】板块采集，重点修复：{payload.get('rework_reason', '补强热点与图片质量')}。"
                )
            elif phase == "material.review":
                enriched_payload["message_body"] = (
                    f"请审核【{section}】板块候选素材，核对时效、来源可靠性、图片数量和热点性。"
                )
            elif phase == "draft.review.start":
                enriched_payload["message_body"] = "进入校稿阶段，请各 worker 仅围绕自己负责板块校对初稿。"
            elif phase == "draft.review.comment":
                enriched_payload["message_body"] = "请以责任编辑视角校对自己负责板块的初稿，核对事实、标题、来源、链接、图片和归位。"
            elif phase == "draft.review.summarize":
                enriched_payload["message_body"] = "请汇总校稿意见，明确采纳项和本轮修订重点。"
            elif phase == "proofread.start":
                enriched_payload["message_body"] = "进入 proofread blocker 校稿阶段。只有 blocker 清零后，才允许 publish。"
            elif phase == "proofread.issue.submit":
                enriched_payload["message_body"] = "请只围绕自己负责板块提交 proofread issue，重点检查素材一致性、事实、链接、归位和主推首句规则。"
            elif phase == "proofread.decision":
                enriched_payload["message_body"] = "系统正在基于 proofread issue 执行结构化规则决策，确认 blocker、required actions 与是否进入修订。"
            elif phase == "proofread.decision.explanation":
                enriched_payload["message_body"] = "请输出一份面向 manager 的 proofread 决策解释，说明为什么这些问题需要修或可关闭。"
            elif phase == "proofread.recheck":
                enriched_payload["message_body"] = "请复查已修订稿，确认 blocker 是否真正关闭；未关闭则重新打开。"
            elif phase == "draft.compose":
                enriched_payload["message_body"] = "请把四个板块的已通过素材整合为初稿。"
            elif phase == "report.publish":
                enriched_payload["message_body"] = "请输出最终成稿，并落盘为 Markdown / HTML / JSON。"
            elif phase == "product.test":
                enriched_payload["message_body"] = "请从产品/读者/编辑视角测试本轮成品，指出最明显问题、最影响阅读体验之处和最值得优先改的点。"
            elif phase == "product.benchmark":
                enriched_payload["message_body"] = "请联网找 2-4 个相近新闻整理产品，提炼最明显差距并转成下一轮可执行建议。"
            elif phase == "product.report":
                enriched_payload["message_body"] = "请汇总三份产品测试报告和外部对标报告，形成本轮产品评估总报告。"
            elif phase == "retrospective.start":
                enriched_payload["message_body"] = "进入复盘阶段，由 neko 主持并围绕成品与执行中的争议点展开讨论。"
            elif phase == "retrospective.comment":
                enriched_payload["message_body"] = "内部复盘消息：请围绕指定争议点补充观点、证据或取舍。"
            elif phase == "retrospective.summary":
                enriched_payload["message_body"] = "请收敛复盘意见，输出统一总结。"
            elif phase == "agent.self_optimize":
                enriched_payload["message_body"] = "请基于本轮复盘更新自己的执行策略、规则和记忆。"
        enriched_payload["trace_context"] = inject_current_context()
        enriched_payload["project_id"] = project_id
        enriched_payload["cycle_no"] = cycle_no
        if project_id:
            enriched_payload["agent_memory_snapshot"] = get_project_memory(project_id, agent_id)
            enriched_payload["optimization_log_snapshot"] = get_effective_optimization_log(project_id, agent_id, cycle_no or 0)
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
        for agent_id, sections in AGENT_SECTIONS.items():
            for section in sections:
                collect_task_id = dispatch_task(
                    run_id=run_id,
                    parent_task_id=None,
                    agent_id=agent_id,
                    agent_role=AGENT_ROLES[agent_id],
                    section=section,
                    phase="material.collect",
                    retry_count=0,
                    payload={"target_count": 12},
                    parent_trace=run_trace,
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
                    parent_trace=run_trace,
                    project_id=project_id,
                    cycle_no=cycle_no,
                )
    return run_id


def claim_task(agent_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, run_id, project_id, cycle_no, parent_task_id, agent_id, agent_role, section, phase, retry_count, payload::text
                FROM tasks
                WHERE agent_id=%s AND status='pending'
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
            cur.execute(
                "UPDATE tasks SET status='running', started_at=NOW() WHERE task_id=%s",
                (row[0],),
            )
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


def save_materials(run_id: str, task_id: str, section: str, source_agent: str, items: list[dict]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for item in items:
                cur.execute(
                    """
                    INSERT INTO materials(run_id, task_id, section, source_agent, title, source_media, published_at, link, images, metadata)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb)
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
                        jdump({"summary_en": item.get("summary_en", "")}),
                    ),
                )
        conn.commit()


def get_materials(run_id: str, section: str) -> list[dict]:
    rows = fetch_all(
        """
        SELECT id, title, source_media, published_at, link, images::text, metadata::text
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
                "metadata": json.loads(row[6]),
            }
        )
    return items


def review_section(run_id: str, section: str, task_id: str) -> dict:
    run = fetch_one("SELECT forced_reject_done FROM workflow_runs WHERE run_id=%s", (run_id,))
    materials = get_materials(run_id, section)
    with_images = [m for m in materials if len(m["images"]) >= 1]
    approved = len(materials) >= 10 and len(with_images) >= 3
    forced = False
    reason = ""
    if approved and SETTINGS.force_reject_once and not run[0]:
        approved = False
        forced = True
        reason = "测试覆盖：受控触发一次打回重做，请补充更强热点和更稳定图片来源。"
        execute(
            "UPDATE workflow_runs SET forced_reject_done=TRUE WHERE run_id=%s",
            (run_id,),
        )
    elif not approved:
        reason = f"素材不足：候选 {len(materials)} 条，带图 {len(with_images)} 条，未满足至少 10 条且至少 3 条带图。"
    else:
        reason = "审核通过。"

    selected_ids = [m["id"] for m in materials[:10]]
    execute(
        """
        INSERT INTO reviews(run_id, section, review_task_id, reviewer_agent, approved, reason, selected_material_ids)
        VALUES (%s,%s,%s,'neko',%s,%s,%s::jsonb)
        """,
        (run_id, section, task_id, approved, reason, jdump(selected_ids)),
    )
    return {"approved": approved, "reason": reason, "forced": forced, "selected_material_ids": selected_ids}


def _translate_ranked_items(section: str, items: list[dict]) -> dict[int, dict]:
    translated = {}
    for idx, item in enumerate(items):
        translated[idx] = {
            "title_zh": item["title"],
            "summary_zh": (
                f"据{item['source_media']}报道，这则{section}新闻围绕“{item['title']}”展开，"
                "更多细节与背景请查看原文链接。"
            ),
        }
    return translated


def generate_section_content(section: str, items: list[dict]) -> dict:
    ranked = sorted(items[:10], key=lambda item: (len(item.get("images", [])), item["published_at"]), reverse=True)
    main = ranked[0]
    secondaries = ranked[1:3]
    briefs = ranked[3:10]
    translated = _translate_ranked_items(section, ranked)

    def enrich(item: dict, idx: int, max_len: int, image_limit: int) -> dict:
        translated_item = translated.get(idx, {})
        title_zh = translated_item.get("title_zh") or item["title"]
        summary_zh = (translated_item.get("summary_zh") or item["metadata"].get("summary_en", "") or item["title"]).replace("\n", " ").strip()
        if len(summary_zh) > max_len:
            summary_zh = summary_zh[: max_len - 1].rstrip("，。；;,. ") + "。"
        return item | {"title": title_zh, "summary_zh": summary_zh, "images": item.get("images", [])[:image_limit]}

    return {
        "main": enrich(main, 0, 200, 3),
        "secondary": [enrich(item, idx + 1, 100, 1) for idx, item in enumerate(secondaries)],
        "briefs": [enrich(item, idx + 3, 50, 0) for idx, item in enumerate(briefs)],
    }


def _report_markdown_from_sections(
    sections_payload: dict,
    *,
    run_id: str,
    project_id: str | None,
    cycle_no: int | None,
    heading: str = "# 近24小时国际新闻热点",
) -> str:
    md_lines = [
        heading,
        "",
        f"- workflow_id: {WORKFLOW_ID}",
        f"- project_id: {project_id or 'standalone'}",
        f"- cycle_no: {cycle_no or 0}",
        f"- run_id: {run_id}",
        f"- 时区: {SETTINGS.timezone}",
        "",
    ]
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        data = sections_payload.get(section) or {}
        main = data.get("main") or {}
        secondary = data.get("secondary") or []
        briefs = data.get("briefs") or []
        md_lines.append(f"## {section}")
        if main:
            md_lines.append(f"### 主推 | {main.get('title', '')}")
            md_lines.append(f"- 来源: {main.get('source_media', '')}")
            md_lines.append(f"- 发布时间: {main.get('published_at', '')}")
            md_lines.append(f"- 原文链接: {main.get('link', '')}")
            md_lines.append(f"- 图片: {', '.join(main.get('images', [])[:3])}")
            md_lines.append(main.get("summary_zh", ""))
            md_lines.append("")
        for idx, sec in enumerate(secondary, 1):
            md_lines.append(f"### 副推{idx} | {sec.get('title', '')}")
            md_lines.append(f"- 来源: {sec.get('source_media', '')}")
            md_lines.append(f"- 发布时间: {sec.get('published_at', '')}")
            md_lines.append(f"- 原文链接: {sec.get('link', '')}")
            md_lines.append(f"- 图片: {', '.join(sec.get('images', [])[:1])}")
            md_lines.append(sec.get("summary_zh", ""))
            md_lines.append("")
        md_lines.append("### 其他 7 条")
        for brief in briefs:
            md_lines.append(
                f"- {brief.get('title', '')} | {brief.get('source_media', '')} | {brief.get('published_at', '')} | {brief.get('link', '')} | {brief.get('summary_zh', '')}"
            )
        md_lines.append("")
    return "\n".join(md_lines)


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


def _proofread_round(run_id: str) -> int:
    row = fetch_one(
        """
        SELECT COALESCE(MAX(retry_count), 0)
        FROM tasks
        WHERE run_id=%s AND phase IN ('proofread.issue.submit', 'proofread.decision', 'proofread.recheck', 'draft.revise')
        """,
        (run_id,),
    )
    return int(row[0] or 0)


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
        WHERE run_id=%s AND status IN ('open', 'debating')
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
    owner_agent: str = "neko",
) -> dict:
    evidence = [f"{msg['from_agent']}: {_truncate(msg['body'], 120)}" for msg in thread[:6]]
    fallback = {
        "summary": (
            f"{title}：确认的核心问题是 "
            f"{_truncate(_first_nonempty([msg['body'] for msg in thread if msg['from_agent'] != 'neko'], '该话题需要把前置规则继续收紧。'), 110)}。"
            f"本话题的收敛要求是 "
            f"{_truncate(_first_nonempty([msg['body'] for msg in reversed(thread) if msg['from_agent'] == 'neko'], '由 neko 收敛为下一轮规则变更。'), 110)}。"
        )
    }
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
        "fallback_payload": fallback,
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
    owner_agent: str = "neko",
) -> dict:
    decision_id = f"rtd-{uuid.uuid4().hex[:10]}"
    summary = decision["summary"]
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
            jdump({"title": title, "evidence": [f"{msg['from_agent']}: {_truncate(msg['body'], 120)}" for msg in thread[:6]], "message_count": len(thread), **decision}),
        ),
    )
    return {"decision_id": decision_id, "summary": summary}


def _record_retro_decision(
    run_id: str,
    *,
    topic_id: str,
    title: str,
    thread: list[dict],
    owner_agent: str = "neko",
) -> dict:
    prepared = _prepare_retro_decision_job(run_id, topic_id=topic_id, title=title, thread=thread, owner_agent=owner_agent)
    decision = {
        **prepared["fallback_payload"],
        "generation_mode": "fallback",
        "generation_error": "legacy_inline_path",
        "timeout_ms": _llm_node_config("retro_decision")["timeout_ms"],
        "prompt_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "input_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "evidence_object_count": prepared["evidence_object_count"],
        "started_at": now_iso(),
        "finished_at": now_iso(),
    }
    return _apply_retro_decision_result(run_id, topic_id=topic_id, title=title, thread=thread, decision=decision, owner_agent=owner_agent)


def _next_retro_topic_candidate(run_id: str) -> dict:
    opened_titles = {row[0] for row in fetch_all("SELECT title FROM retro_topics WHERE run_id=%s", (run_id,))}
    for item in _pick_retro_controversies(run_id):
        title = item.get("topic") or "问题"
        if title in opened_titles:
            continue
        return item | {"title": title}
    return {}


def compose_draft(run_id: str) -> dict:
    run_info = _run_row(run_id)
    project_id, cycle_no = run_info[0], run_info[1]
    neko_optimization = get_effective_optimization_log(project_id, "neko", cycle_no or 0)
    sections = ["政治经济", "科技", "体育娱乐", "其他"]
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
        materials = [m for m in get_materials(run_id, section) if m["id"] in selected][:10]
        assembled[section] = generate_section_content(section, materials)
    assembled = _apply_writer_guidance(assembled, neko_optimization)

    draft_markdown = _report_markdown_from_sections(
        assembled,
        run_id=run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        heading="# 近24小时国际新闻热点（初稿）",
    )
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
        created_by="neko",
        markdown_text=draft_markdown,
        report_json=assembled,
        source_task_id=None,
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
        "message_body": f"初稿 v{draft_version['version_no']} 已生成，进入 proofread blocker 校稿阶段。",
    }


def create_discussion_comment(run_id: str, task_id: str, agent_id: str) -> str:
    memory = get_project_memory(get_run_project_context(run_id)[0], agent_id)
    suffix = f" 已应用上一轮优化：{_memory_summary(memory)}。" if memory else ""
    final_json = (_load_output_bundle(run_id).get("final_json") or {})
    if agent_id == "neko":
        main_titles = [((final_json.get(section) or {}).get("main") or {}).get("title", "") for section in ["政治经济", "科技", "体育娱乐", "其他"]]
        comment = f"我先盯主推入口。现在四个板块的开头像四条平行资讯，没有形成一眼能抓住的主次关系。尤其是《{_first_nonempty(main_titles, '主推')}》这类条目，首句还不够直接。{suffix}"
    elif agent_id == "33":
        tech_title = ((final_json.get('科技') or {}).get('main') or {}).get('title', '科技主推')
        comment = f"我建议先改信息密度。《{tech_title}》这一类条目素材够多，但“为什么值得看”说得太晚，副推之间也容易挤在一起，看完像资讯清单。{suffix}"
    else:
        sports_title = ((final_json.get('体育娱乐') or {}).get('main') or {}).get('title', '体育娱乐主推')
        comment = f"我更担心扫读体验。《{sports_title}》这一类条目如果图片不稳、短讯又偏松，读者会先觉得节奏散。下一轮我会把边界和图片稳定性先收紧。{suffix}"
    execute(
        "INSERT INTO discussions(run_id, task_id, agent_id, comment_text) VALUES (%s,%s,%s,%s)",
        (run_id, task_id, agent_id, comment),
    )
    return comment


def create_draft_review_comment(run_id: str, task_id: str, agent_id: str) -> str:
    bundle = _load_output_bundle(run_id)
    final_json = bundle.get("final_json") or {}
    sections = AGENT_SECTIONS.get(agent_id, [])
    lines = []
    for section in sections:
        section_data = final_json.get(section) or {}
        main = section_data.get("main") or {}
        secondary = section_data.get("secondary") or []
        briefs = section_data.get("briefs") or []
        if main:
            lines.append(f"{section} 主推《{main.get('title', '')}》需要核对首句是否准确承接素材，图片是否对应主推。")
        if secondary:
            lines.append(f"{section} 副推共 {len(secondary)} 条，我重点检查标题、来源和归位是否正确。")
        if briefs:
            lines.append(f"{section} 简讯 {len(briefs)} 条，优先核对链接、发布时间和是否有遗漏。")
    if agent_id == "33":
        text = "我先按政治经济和科技两块校稿：" + " ".join(lines[:3]) + " 我建议优先修正主推首句与副推归位，避免素材明明正确却在初稿里显得层级不清。"
        scope = "政治经济,科技"
    else:
        text = "我先按体育娱乐和其他两块校稿：" + " ".join(lines[:3]) + " 我建议优先修正图片显示和短讯归类，避免成稿里出现题材边界松动或图文错配。"
        scope = "体育娱乐,其他"
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
        "message_body": f"proofread 已启动，当前检查对象是 draft v{latest.get('version_no', 0)}。",
    }


def submit_proofread_issues(run_id: str, task_id: str, agent_id: str) -> dict:
    run_info = _run_row(run_id)
    project_id, cycle_no = run_info[0], run_info[1]
    draft = _latest_draft_version(run_id)
    report_json = draft.get("report_json") or {}
    existing_keys = {
        (row["section"], row["item_ref"], row["issue_type"], row["status"])
        for row in _proofread_issue_rows(run_id)
        if row["status"] != "closed"
    }
    created = []
    for section in AGENT_SECTIONS.get(agent_id, []):
        data = report_json.get(section) or {}
        main = data.get("main") or {}
        summary = (main.get("summary_zh") or "").strip()
        if summary and summary.startswith("据"):
            key = (section, "main", "lead_sentence_rule", "open")
            if key not in existing_keys:
                issue_id = f"pfi-{uuid.uuid4().hex[:10]}"
                description = f"{section} 主推首句仍以“据…报道”开头，影响句没有前置，读者第一眼抓不到为什么值得看。"
                evidence = {
                    "draft_version_no": draft.get("version_no"),
                    "title": main.get("title", ""),
                    "current_summary": summary[:180],
                    "link": main.get("link", ""),
                }
                execute(
                    """
                    INSERT INTO proofread_issues(
                        issue_id, run_id, project_id, cycle_no, section, item_ref, severity, issue_type,
                        description, evidence, reported_by, status
                    )
                    VALUES (%s,%s,%s,%s,%s,%s,'blocker',%s,%s,%s::jsonb,%s,'open')
                    """,
                    (issue_id, run_id, project_id, cycle_no, section, "main", "lead_sentence_rule", description, jdump(evidence), agent_id),
                )
                created.append({"issue_id": issue_id, "section": section, "severity": "blocker", "description": description})
        if data.get("secondary") and len(data.get("secondary") or []) != 2:
            issue_id = f"pfi-{uuid.uuid4().hex[:10]}"
            description = f"{section} 副推数量不是 2 条，初稿结构与发布规格不一致。"
            execute(
                """
                INSERT INTO proofread_issues(
                    issue_id, run_id, project_id, cycle_no, section, item_ref, severity, issue_type,
                    description, evidence, reported_by, status
                )
                VALUES (%s,%s,%s,%s,%s,%s,'high',%s,%s,%s::jsonb,%s,'open')
                ON CONFLICT (issue_id) DO NOTHING
                """,
                (issue_id, run_id, project_id, cycle_no, section, "secondary", "structure_count", description, jdump({"draft_version_no": draft.get("version_no")}), agent_id),
            )
            created.append({"issue_id": issue_id, "section": section, "severity": "high", "description": description})
    lines = [f"{agent_id} 已完成 proofread issue 提交，共提出 {len(created)} 个问题。"]
    for item in created[:4]:
        lines.append(f"- [{item['severity']}] {item['section']}：{item['description']}")
    return {
        "status": "submitted",
        "draft_version_no": draft.get("version_no"),
        "issue_count": len(created),
        "issues": created,
        "message_body": "\n".join(lines),
    }


def _default_patch_instruction_for_issue(issue: dict) -> str:
    if issue["issue_type"] == "lead_sentence_rule":
        return f"把 {issue['section']} 主推首句改成先说影响/结果，再说事实；保留原来源、时间、链接。"
    if issue["issue_type"] in {"structure_count", "section_mismatch"}:
        return f"修正 {issue['section']} 的结构归位，确保主推 1 条、副推 2 条、简讯 7 条。"
    if issue["issue_type"] in {"missing_image", "image_missing"}:
        return f"补齐 {issue['section']} 对应条目的图片，主推需 3 张、副推需 1 张。"
    if issue["issue_type"] in {"source_mismatch", "title_mismatch", "link_mismatch", "fact_integrity"}:
        return f"修正 {issue['section']} 的标题、来源、链接与素材事实一致性，并复核发布时间。"
    return f"修正 {issue['section']} 的 {issue['issue_type']} 问题，确保满足发布规格。"


def _proofread_required_actions(issue: dict) -> list[str]:
    actions = []
    if issue["issue_type"] in {"missing_image", "image_missing"}:
        actions.append("补齐图片")
    if issue["issue_type"] in {"source_mismatch", "title_mismatch", "link_mismatch", "fact_integrity"}:
        actions.append("修正素材字段")
    if issue["issue_type"] in {"section_mismatch", "structure_count"}:
        actions.append("修正板块归位")
    if issue["issue_type"] == "lead_sentence_rule":
        actions.append("改写主推首句")
    if issue["severity"] in {"blocker", "high"}:
        actions.append("进入 recheck")
    return actions or ["进入 recheck"]


def _evaluate_proofread_issue(issue: dict) -> dict:
    evidence = issue.get("evidence") or {}
    accepted = issue["severity"] in {"blocker", "high"} or issue["issue_type"] in {
        "lead_sentence_rule",
        "structure_count",
        "missing_image",
        "image_missing",
        "source_mismatch",
        "title_mismatch",
        "link_mismatch",
        "fact_integrity",
        "section_mismatch",
    }
    reasons = []
    if issue["severity"] == "blocker":
        reasons.append("blocker 未清零前禁止 publish")
    if issue["issue_type"] in {"missing_image", "image_missing"}:
        reasons.append("图片数量不满足发布规格")
    if issue["issue_type"] in {"source_mismatch", "title_mismatch", "link_mismatch", "fact_integrity"}:
        reasons.append("素材字段与初稿内容不一致")
    if issue["issue_type"] in {"section_mismatch", "structure_count"}:
        reasons.append("板块归位或结构数量不符合规格")
    if issue["issue_type"] == "lead_sentence_rule":
        reasons.append("主推首句不符合 lead sentence rule")
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
        "required_actions": _proofread_required_actions(issue) if accepted else [],
        "patch_instruction": _default_patch_instruction_for_issue(issue) if accepted else "",
        "rationale": "；".join(reasons) if reasons else "当前证据不足以证明需要改单。",
    }


def _prepare_proofread_decision_explanation_job(run_id: str, task_id: str) -> dict:
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
    issue_fallback = {
        "summary": f"本轮 proofread 共处理 {len(decision_rows)} 个 issue，其中采纳 {len(accepted)} 个，驳回 {len(rejected)} 个。",
        "accepted": [f"{row['section']}：{row['description']}" for row in accepted[:5]],
        "rejected": [f"{row['section']}：{row['description']}" for row in rejected[:5]],
        "required_actions": sorted({action for row in accepted for action in (row.get('decision_json') or {}).get('required_actions', [])}),
    }
    return {
        "node_type": "proofread.decision.explanation",
        "project_id": project_id,
        "cycle_no": cycle_no,
        "task_id": task_id,
        "prompt_system": "你是新闻 workflow 的 manager。基于已完成的结构化 proofread 决策，输出一份给人看的简洁 explanation，说明为什么 blocker 需要修、为什么可放行或需要 recheck。不要重做规则决策，只做解释。",
        "prompt_user": "\n".join(
            [
                f"run_id={run_id}",
                f"draft_version={draft.get('version_no')}",
                "proofread decisions:",
                *[
                    f"- issue_id={row['issue_id']} | section={row['section']} | severity={row['severity']} | issue_type={row['issue_type']} | description={row['description']} | decision={row['decision_type']} | rationale={row['rationale']} | required_actions={json.dumps((row.get('decision_json') or {}).get('required_actions', []), ensure_ascii=False)}"
                    for row in decision_rows
                ],
                "返回 JSON，字段：summary(string)、accepted(array of string)、rejected(array of string)、required_actions(array of string)。",
            ]
        ),
        "fallback_payload": issue_fallback,
        "evidence_object_count": len(decision_rows) + 3,
    }


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
            VALUES (%s,%s,%s,'neko',%s,%s,%s::jsonb)
            """,
            (
                decision_id,
                run_id,
                issue["issue_id"],
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
                VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'neko',%s)
                """,
                (patch_id, run_id, decision_id, issue["issue_id"], issue["section"], patch_instruction, jdump({"issue_type": issue["issue_type"], "required_actions": issue_decision["required_actions"]}), task_id),
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
        "message_body": f"系统已完成 proofread 结构化决策，采纳 {len(accepted)} 项，驳回 {len(rejected)} 项，当前 blocker 余量 {_active_blocker_count(run_id)}。",
    }


def decide_proofread_issues(run_id: str, task_id: str) -> dict:
    return _apply_proofread_rule_decision(run_id, task_id, {})


def _apply_proofread_explanation_result(run_id: str, task_id: str, explanation_data: dict) -> dict:
    fallback = {
        "summary": explanation_data.get("summary") or "proofread explanation 未生成，已保留结构化 decision 与 required_actions 供 manager 查看。",
        "accepted": explanation_data.get("accepted") or [],
        "rejected": explanation_data.get("rejected") or [],
        "required_actions": explanation_data.get("required_actions") or [],
    }
    body_md = "\n".join(
        [
            "# Proofread Decision Explanation",
            "",
            fallback["summary"],
            "",
            "## 需要处理的动作",
            *( [f"- {item}" for item in fallback["required_actions"]] if fallback["required_actions"] else ["- 无"] ),
        ]
    )
    files = _write_aux_report_files(
        run_id,
        "proofread_decision_explanation",
        "Proofread Decision Explanation",
        body_md,
        {
            **fallback,
            "generation_mode": explanation_data.get("generation_mode", "fallback"),
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
                jsonb_set(COALESCE(notes, '{}'::jsonb), '{proofread_decision_explanation_html}', to_jsonb(%s::text), true),
                '{proofread_decision_explanation_mode}', to_jsonb(%s::text), true
            )
        WHERE run_id=%s
        """,
        (files["html_path"], explanation_data.get("generation_mode", "fallback"), run_id),
    )
    return {
        "status": "explained",
        "summary": fallback["summary"],
        "html_path": files["html_path"],
        "generation_mode": explanation_data.get("generation_mode", "fallback"),
        "generation_error": explanation_data.get("generation_error", ""),
        "timeout_ms": explanation_data.get("timeout_ms"),
        "prompt_size": explanation_data.get("prompt_size"),
        "input_size": explanation_data.get("input_size"),
        "evidence_object_count": explanation_data.get("evidence_object_count"),
        "started_at": explanation_data.get("started_at"),
        "finished_at": explanation_data.get("finished_at"),
        "message_body": f"proofread explanation 已生成（mode={explanation_data.get('generation_mode', 'fallback')}）。",
    }


def summarize_draft_review(run_id: str) -> dict:
    rows = fetch_all(
        "SELECT agent_id, section_scope, review_text FROM draft_reviews WHERE run_id=%s ORDER BY created_at",
        (run_id,),
    )
    accepted = [f"{agent_id}（{scope}）：{text}" for agent_id, scope, text in rows]
    body_md = "\n".join(
        [
            "# Draft Review Summary",
            "",
            "## 采纳的校稿意见",
            *[f"- {item}" for item in accepted[:4]],
            "",
            "## 本轮修订重点",
            "- 修正主推首句与素材事实承接。",
            "- 校正副推与短讯归位，避免板块内层级错位。",
            "- 复核图片可用性、来源、链接和发布时间。",
        ]
    )
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "draft_review_summary.md"
    json_path = run_dir / "draft_review_summary.json"
    md_path.write_text(body_md)
    payload = {
        "run_id": run_id,
        "accepted_review_notes": accepted[:4],
        "revision_focus": [
            "修正主推首句与素材事实承接",
            "校正副推与短讯归位",
            "复核图片、来源、链接与发布时间",
        ],
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    html_path = write_product_report_html(run_id, "Draft Review Summary", body_md, payload, "draft_review_summary")
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
        "message_body": "neko 已完成校稿收敛总结，明确了本轮修订重点。",
    }


def summarize_discussion(run_id: str) -> dict:
    comments = fetch_all("SELECT agent_id, comment_text FROM discussions WHERE run_id=%s ORDER BY created_at", (run_id,))
    product_eval = _product_report_by_type(run_id, "product_evaluation_report")
    top_issues = (product_eval.get("report_json", {}) or {}).get("top_product_issues", [])
    accepted = []
    rejected = []
    revision_actions = []
    for agent_id, comment_text in comments:
        accepted.append(f"{agent_id}：{comment_text}")
        if "图片" in comment_text or "首句" in comment_text or "主推" in comment_text:
            revision_actions.append(comment_text)
    if not revision_actions:
        revision_actions = [comment_text for _, comment_text in comments[:2]]
    if top_issues:
        rejected.append(f"不再继续泛化扩写背景，优先先解决：{top_issues[0]}")
    plan_lines = [
        "# Discussion Summary",
        "",
        "## 本轮终稿最需要改的点",
        *[f"- {item}" for item in (top_issues[:3] or ["主推首句不够直接，板块阅读层级不够清晰。"])],
        "",
        "## 决定采纳的意见",
        *[f"- {item}" for item in accepted[:4]],
        "",
        "## 决定暂不采纳的意见",
        *[f"- {item}" for item in (rejected[:2] or ["不额外扩写背景长段，避免继续拉长主推和副推。"])],
        "",
        "## 本轮将如何修改",
        *[f"- {item}" for item in revision_actions[:4]],
    ]
    plan = "\n".join(plan_lines)
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "discussion_summary.md"
    json_path = run_dir / "discussion_summary.json"
    md_path.write_text(plan)
    payload = {
        "run_id": run_id,
        "top_issues": top_issues[:3],
        "accepted_comments": accepted[:4],
        "rejected_comments": rejected[:2] or ["不额外扩写背景长段，避免继续拉长主推和副推。"],
        "revision_actions": revision_actions[:4],
        "markdown_path": str(md_path),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    html_path = write_product_report_html(run_id, "Discussion Summary", plan, payload, "discussion_summary")
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
        "message_body": "manager 已完成正式讨论收敛总结，并明确本轮修稿方案。",
    }


def _prepare_draft_revise_job(run_id: str) -> dict:
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    project_id, cycle_no = row[0], row[1]
    latest = _latest_draft_version(run_id)
    sections_payload = latest.get("report_json") or {}
    revision_patches = fetch_all(
        """
        SELECT p.issue_id, p.target_section, p.patch_instruction, p.patch_payload::text, i.description, i.issue_type
        FROM revision_patches p
        JOIN proofread_issues i ON i.issue_id=p.issue_id
        WHERE p.run_id=%s
        ORDER BY p.created_at, p.patch_id
        """,
        (run_id,),
    )
    revise_fallback = {
        "section_updates": [
            {
                "section": section,
                "main_summary": (
                    "最值得关注的是，这条新闻直接影响本轮热点判断。"
                    if issue_type == "lead_sentence_rule"
                    else ""
                ),
                "reason": description,
            }
            for _, section, _, _, description, issue_type in revision_patches
        ],
        "revision_plan": "先把主推首句改成影响前置，再按 proofread 结论修结构。",
    }
    return {
        "node_type": "draft.revise",
        "project_id": project_id,
        "cycle_no": cycle_no,
        "task_id": None,
        "prompt_system": "你是新闻编辑。基于 proofread 决策与 revision patch，输出本轮修订决策。不要生成整篇稿件，只输出需要改动的 section-level updates。",
        "prompt_user": "\n".join(
            [
                f"run_id={run_id}",
                f"draft_version={latest.get('version_no')}",
                "proofread decisions and revision patches:",
                *[
                    f"- issue_id={issue_id} | section={section} | issue_type={issue_type} | patch_instruction={patch_instruction} | current_main_summary={((sections_payload.get(section) or {}).get('main') or {}).get('summary_zh', '')[:160]}"
                    for issue_id, section, patch_instruction, _, _, issue_type in revision_patches
                ],
                "返回 JSON，字段：section_updates(array of {section, main_summary, reason}), revision_plan。main_summary 只在需要改主推首句时返回。",
            ]
        ),
        "fallback_payload": revise_fallback,
        "evidence_object_count": len(revision_patches),
    }


def _apply_draft_revise_result(run_id: str, revise_data: dict) -> dict:
    row = fetch_one("SELECT project_id, cycle_no FROM workflow_runs WHERE run_id=%s", (run_id,))
    project_id, cycle_no = row[0], row[1]
    latest = _latest_draft_version(run_id)
    sections_payload = latest.get("report_json") or {}
    accepted_issues = _proofread_issue_rows(run_id, ("accepted",))
    updates_by_section = {item["section"]: item for item in (revise_data.get("section_updates") or []) if item.get("section")}
    applied = []
    for issue in accepted_issues:
        section_data = sections_payload.get(issue["section"]) or {}
        section_update = updates_by_section.get(issue["section"], {})
        if issue["issue_type"] == "lead_sentence_rule" and section_data.get("main"):
            main = dict(section_data["main"])
            summary = (main.get("summary_zh") or "").strip()
            new_summary = (section_update.get("main_summary") or "").strip()
            if not new_summary and summary.startswith("据"):
                new_summary = f"最值得关注的是，{summary[1:]}" if len(summary) > 1 else "最值得关注的是，这条新闻直接影响本轮热点判断。"
            if new_summary:
                main["summary_zh"] = new_summary
                section_data["main"] = main
                sections_payload[issue["section"]] = section_data
                applied.append(section_update.get("reason") or f"{issue['section']} 主推首句改成先说影响，再说事实。")
        execute(
            "UPDATE proofread_issues SET status='fixed', updated_at=NOW(), resolution_note=COALESCE(resolution_note,'') || %s WHERE issue_id=%s",
            (" | patch applied", issue["issue_id"]),
        )
    sections_payload = _apply_writer_guidance(sections_payload, get_effective_optimization_log(project_id, "neko", cycle_no or 0))
    revision_plan = revise_data.get("revision_plan") or ("\n".join(f"- {item}" for item in applied) if applied else "- 无新增修订。")
    final_markdown = _report_markdown_from_sections(
        sections_payload,
        run_id=run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        heading="# 近24小时国际新闻热点（修订稿）",
    )
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
        created_by="neko",
        markdown_text=final_markdown,
        report_json=sections_payload,
        source_task_id=None,
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
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "draft_version_no": version["version_no"],
        "generation_mode": revise_data["generation_mode"],
        "generation_error": revise_data.get("generation_error", ""),
        "timeout_ms": revise_data.get("timeout_ms"),
        "prompt_size": revise_data.get("prompt_size"),
        "input_size": revise_data.get("input_size"),
        "evidence_object_count": revise_data.get("evidence_object_count"),
        "started_at": revise_data.get("started_at"),
        "finished_at": revise_data.get("finished_at"),
        "message_body": f"已基于 proofread decision 与 revision patch 完成修订稿 v{version['version_no']}，进入 blocker recheck。生成方式：{revise_data['generation_mode']}",
    }


def revise_draft(run_id: str) -> dict:
    prepared = _prepare_draft_revise_job(run_id)
    revise_data = {
        **prepared["fallback_payload"],
        "generation_mode": "fallback",
        "generation_error": "legacy_inline_path",
        "timeout_ms": _llm_node_config("draft.revise")["timeout_ms"],
        "prompt_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "input_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "evidence_object_count": prepared["evidence_object_count"],
        "started_at": now_iso(),
        "finished_at": now_iso(),
    }
    return _apply_draft_revise_result(run_id, revise_data)


def publish_report(run_id: str) -> dict:
    proofread_round = _proofread_round(run_id)
    recheck_done = fetch_one(
        """
        SELECT COUNT(*) FROM tasks
        WHERE run_id=%s AND phase='proofread.recheck' AND retry_count=%s AND status='completed'
        """,
        (run_id, proofread_round),
    )[0]
    blocker_count = _active_blocker_count(run_id)
    if recheck_done < 2:
        raise RuntimeError("proofread recheck 尚未完成，禁止 publish")
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
    execute(
        """
        INSERT INTO final_reports(run_id, project_id, cycle_no, source_draft_version_id, markdown_text, report_json, published_by)
        VALUES (%s,%s,%s,%s,%s,%s::jsonb,'neko')
        ON CONFLICT (run_id) DO UPDATE
        SET project_id=EXCLUDED.project_id,
            cycle_no=EXCLUDED.cycle_no,
            source_draft_version_id=EXCLUDED.source_draft_version_id,
            markdown_text=EXCLUDED.markdown_text,
            report_json=EXCLUDED.report_json,
            published_by='neko',
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
                        "recheck_done": int(recheck_done),
                        "blocker_count": int(blocker_count),
                        "closed_blocker_count": int(closed_blockers),
                        "reason": "all blockers closed and recheck passed",
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
                        "recheck_done": int(recheck_done),
                        "blocker_count": int(blocker_count),
                        "closed_blocker_count": int(closed_blockers),
                        "reason": "all blockers closed and recheck passed",
                    }
                ),
                run_id,
            ),
        )
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "publish_gate_reason": "all blockers closed and recheck passed",
        "proofread_round": proofread_round,
        "recheck_done": int(recheck_done),
        "blocker_count": int(blocker_count),
        "closed_blocker_count": int(closed_blockers),
        "message_body": "终稿已在修订后正式发布，可直接查看 HTML 成品页、Markdown 和 JSON 结构化文件。",
    }


def recheck_proofread_issues(run_id: str, task_id: str, agent_id: str) -> dict:
    latest = _latest_draft_version(run_id)
    report_json = latest.get("report_json") or {}
    sections = AGENT_SECTIONS.get(agent_id, [])
    rows = fetch_all(
        """
        SELECT issue_id, section, issue_type, status
        FROM proofread_issues
        WHERE run_id=%s AND section = ANY(%s) AND status IN ('accepted', 'fixed', 'open')
        ORDER BY opened_at
        """,
        (run_id, sections),
    )
    closed = []
    reopened = []
    for issue_id, section, issue_type, status in rows:
        main = ((report_json.get(section) or {}).get("main") or {})
        summary = (main.get("summary_zh") or "").strip()
        resolved = True
        if issue_type == "lead_sentence_rule":
            resolved = not summary.startswith("据")
        if resolved:
            execute(
                """
                UPDATE proofread_issues
                SET status='closed', updated_at=NOW(), closed_at=NOW(), resolution_note=COALESCE(resolution_note,'') || ' | rechecked closed'
                WHERE issue_id=%s
                """,
                (issue_id,),
            )
            closed.append(issue_id)
        else:
            execute(
                """
                UPDATE proofread_issues
                SET status='open', updated_at=NOW(), resolution_note='recheck 仍未通过'
                WHERE issue_id=%s
                """,
                (issue_id,),
            )
            reopened.append(issue_id)
    return {
        "status": "rechecked",
        "closed_issues": closed,
        "reopened_issues": reopened,
        "message_body": f"{agent_id} 已完成 proofread recheck，关闭 {len(closed)} 个 issue，重新打开 {len(reopened)} 个 issue。",
    }


def _prepare_product_test_job(run_id: str, task_id: str, agent_id: str) -> dict:
    bundle = _load_output_bundle(run_id)
    final_json = bundle.get("final_json") or {}
    evidence = []
    for section in ["政治经济", "科技", "体育娱乐", "其他"]:
        section_data = final_json.get(section) or {}
        main = section_data.get("main") or {}
        secondary = section_data.get("secondary") or []
        if main:
            evidence.append(
                {
                    "section": section,
                    "kind": "main",
                    "title": main.get("title", ""),
                    "summary": main.get("summary_zh", "")[:90],
                    "image_count": len(main.get("images") or []),
                }
            )
    evidence = evidence[:4]
    fallback = {
        "focus": "成品体验",
        "most_obvious_problems": [
            "主推首屏冲击力和板块收束感还不够统一",
            "部分条目的信息排序仍偏晚，读者抓重点成本偏高",
        ],
        "priority_improvements": [
            "先把主推首句改成影响前置",
            "把图片稳定性和板块边界前置成硬约束",
        ],
        "execution_link": [f"{agent_id} 需要把本轮暴露的问题前置到自己负责的采集/编排阶段"],
        "summary": f"{agent_id} 已基于最终成品生成产品测试报告，输出 2 个优先问题和下一轮建议。",
    }
    return {
        "node_type": "product.test",
        "project_id": get_run_project_context(run_id)[0],
        "cycle_no": get_run_project_context(run_id)[1],
        "task_id": task_id,
        "prompt_system": "你是多 agent 新闻项目的参与者。现在要基于最终成品输出产品体验测试报告。不要写流程回执，不要写模板栏目，不要假装评分表。要像真正看完成品后给出的产品意见。",
        "prompt_user": "\n".join(
            [
                f"run_id={run_id}",
                f"agent_id={agent_id}",
                "final artifact slices:",
                *[
                    f"- {item['section']} | {item['title']} | images={item['image_count']} | summary={item['summary']}"
                    for item in evidence
                ],
                "请返回 JSON：focus, most_obvious_problems(array), priority_improvements(array), execution_link(array), summary。只提 2-3 个真正的问题，优先看首屏完成度、板块收束感、阅读节奏、图片与结构。",
            ]
        ),
        "fallback_payload": fallback,
        "evidence_object_count": len(evidence),
        "evidence": evidence,
        "agent_id": agent_id,
    }


def _apply_product_test_result(run_id: str, task_id: str, agent_id: str, evidence: list[dict], decision: dict) -> dict:
    focus = decision.get("focus") or "成品体验"
    problems = list(dict.fromkeys(decision.get("most_obvious_problems") or []))[:4]
    priorities = list(dict.fromkeys(decision.get("priority_improvements") or []))[:4]
    own_responsibility = list(dict.fromkeys(decision.get("execution_link") or []))[:3]
    title = f"{agent_id} 产品测试报告"
    summary = decision.get("summary") or f"{agent_id} 已基于最终成品生成产品测试报告。"
    body_md = "\n".join(
        [
            f"# {title}",
            "",
            f"- run_id: {run_id}",
            f"- 视角: {focus}",
            "",
            "## 成品证据",
            *[
                f"- {item['section']} | {item['kind']} | {item['title']} | image_count={item['image_count']} | {item['summary']}"
                for item in evidence
            ],
            "",
            "## 最明显的问题",
            *[f"- {line}" for line in problems],
            "",
            "## 最值得优先改的点",
            *[f"- {line}" for line in priorities],
            "",
            "## 与我本轮执行的关系",
            *[f"- {line}" for line in own_responsibility],
        ]
    )
    payload = {
        "run_id": run_id,
        "agent_id": agent_id,
        "focus": focus,
        "evidence": evidence,
        "most_obvious_problems": problems,
        "priority_improvements": priorities,
        "execution_link": own_responsibility,
        "summary": summary,
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
    decision = {
        **prepared["fallback_payload"],
        "generation_mode": "fallback",
        "generation_error": "legacy_inline_path",
        "timeout_ms": _llm_node_config("product.test")["timeout_ms"],
        "prompt_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "input_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "evidence_object_count": prepared["evidence_object_count"],
        "started_at": now_iso(),
        "finished_at": now_iso(),
    }
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
    comparisons = []
    search_mode = "open_search"
    if search_results:
        for item in search_results[:4]:
            comparisons.append(
                {
                    "name": item["source_media"] or item["title"],
                    "url": item["link"],
                    "page_title": item["title"],
                    "selected_reason": "搜索结果与国际热点整理/新闻聚合页面形态接近，适合作为轻量对标样本。",
                    "gap": f"从《{item['title']}》的搜索摘要和入口样式看，外部样本更强调强标题与读者首屏抓重点，我们这轮的主推冲击力和板块入口层级还偏平。",
                    "advice": f"参考《{item['title']}》这类结果的入口表达，下轮优先把主推首句写得更直接，并减少同层信息拥挤。",
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
                comparisons.append(
                    {
                        "name": name,
                        "url": url,
                        "page_title": title[:140],
                        "selected_reason": "开放搜索结果不可用，回退到固定参考样本。",
                        "gap": f"{name} 首页更强调第一屏层级和强标题，我们这轮的主推冲击力与版式完成度还有差距。",
                        "advice": f"参考 {name} 的读者入口设计，下轮优先让主推首句更直接、图片更稳定、板块首屏更有层次。",
                        "source_media": name,
                    }
                )
            except Exception as exc:
                comparisons.append(
                    {
                        "name": name,
                        "url": url,
                        "page_title": "",
                        "selected_reason": "开放搜索失败后的固定样本兜底。",
                        "gap": f"未能稳定抓取 {name} 页面：{exc}",
                        "advice": f"保留 {name} 作为对标对象，但下轮仍按“首屏层级 + 重点更直接”推进。",
                        "source_media": name,
                    }
                )
    concise_actions = [item["advice"] for item in comparisons[:3]]
    summary = "外部对标显示，我们与相近新闻整理页最明显的差距在首屏层级、主推冲击力和板块收束感。"
    title = "neko 外部对标报告"
    body_md = "\n".join(
        [
            f"# {title}",
            "",
            f"- run_id: {run_id}",
            f"- benchmark_mode: {search_mode}",
            f"- search_query: {query}",
            "",
            "## 对标对象",
            *[f"- {item['name']} | {item['url']} | {item['page_title'] or '未抓到标题'} | 被选原因：{item['selected_reason']}" for item in comparisons],
            "",
            "## 最明显差距",
            *[f"- {item['gap']}" for item in comparisons[:3]],
            "",
            "## 可落到下一轮的建议",
            *[f"- {text}" for text in concise_actions],
        ]
    )
    payload = {
        "run_id": run_id,
        "benchmark_mode": search_mode,
        "search_query": query,
        "comparisons": comparisons,
        "most_visible_gap": summary,
        "next_cycle_actions": concise_actions,
        "summary": summary,
    }
    files = _write_aux_report_files(run_id, "benchmark_report", title, body_md, payload)
    project_id, cycle_no = get_run_project_context(run_id)
    _insert_product_report(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        agent_id="neko",
        report_type="benchmark_report",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def _prepare_product_report_job(run_id: str, task_id: str) -> dict:
    reports = _product_report_rows(run_id)
    product_tests = [item for item in reports if item["report_type"] == "product_test"]
    benchmark = next((item for item in reports if item["report_type"] == "benchmark_report"), None)
    fallback = {
        "top_product_issues": [
            "主推第一屏完成度和板块收束感仍不够统一",
            "前置规则没有把同源堆叠、图片不稳和板块边界问题及时拦住",
        ],
        "agent_responsibility_links": [
            f"{item['agent_id']}：{item['report_json'].get('execution_link', [''])[0] if item['report_json'].get('execution_link') else item['summary_text']}"
            for item in product_tests
        ],
        "next_cycle_recommendations": [
            "统一主推首句的影响前置规则",
            "把图片稳定性、同源去重和板块边界前置到采集阶段",
        ],
        "summary": "本轮产品评估确认：最优先要改的是主推第一屏完成度、板块收束感和采集端前置规则。",
    }
    return {
        "node_type": "product.report",
        "project_id": get_run_project_context(run_id)[0],
        "cycle_no": get_run_project_context(run_id)[1],
        "task_id": task_id,
        "prompt_system": "你是 newsflow 项目的 manager。基于三份产品测试报告和一份 benchmark 报告，输出真正的产品评估总报告。不要拼接原文，不要做 checklist。",
        "prompt_user": "\n".join(
            [
                f"run_id={run_id}",
                "product_tests:",
                *[
                    f"- agent={item['agent_id']} | summary={item['summary_text']} | problems={json.dumps(item['report_json'].get('most_obvious_problems', [])[:2], ensure_ascii=False)} | next={json.dumps(item['report_json'].get('priority_improvements', [])[:2], ensure_ascii=False)}"
                    for item in product_tests
                ],
                f"benchmark_gap={((benchmark or {}).get('report_json', {}) or {}).get('most_visible_gap', '')}",
                f"benchmark_next={json.dumps((((benchmark or {}).get('report_json', {}) or {}).get('next_cycle_actions', [])[:3]), ensure_ascii=False)}",
                "返回 JSON：top_product_issues(array), agent_responsibility_links(array), next_cycle_recommendations(array), summary。每个数组控制在 2-4 项。",
            ]
        ),
        "fallback_payload": fallback,
        "evidence_object_count": len(product_tests) + (1 if benchmark else 0),
    }


def _apply_product_report_result(run_id: str, task_id: str, decision: dict) -> dict:
    reports = _product_report_rows(run_id)
    product_tests = [item for item in reports if item["report_type"] == "product_test"]
    benchmark = next((item for item in reports if item["report_type"] == "benchmark_report"), None)
    dedup_problems = list(dict.fromkeys([item for item in decision.get("top_product_issues", []) if item]))[:5]
    agent_links = list(dict.fromkeys([item for item in decision.get("agent_responsibility_links", []) if item]))[:5]
    dedup_next = list(dict.fromkeys([item for item in decision.get("next_cycle_recommendations", []) if item]))[:6]
    summary = decision.get("summary") or fallback["summary"]
    title = "本轮产品评估总报告"
    body_md = "\n".join(
        [
            f"# {title}",
            "",
            f"- run_id: {run_id}",
            "",
            "## 本轮成品最重要的问题",
            *[f"- {item}" for item in dedup_problems],
            "",
            "## 与 agent 执行强相关的问题",
            *[f"- {item}" for item in agent_links],
            "",
            "## 进入下一轮的建议",
            *[f"- {item}" for item in dedup_next],
        ]
    )
    payload = {
        "run_id": run_id,
        "top_product_issues": dedup_problems,
        "agent_responsibility_links": agent_links,
        "next_cycle_recommendations": dedup_next,
        "summary": summary,
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
        agent_id="neko",
        report_type="product_evaluation_report",
        title=title,
        summary_text=summary,
        report_json=payload | files,
    )
    return payload | files


def create_product_evaluation_report(run_id: str, task_id: str) -> dict:
    prepared = _prepare_product_report_job(run_id, task_id)
    decision = {
        **prepared["fallback_payload"],
        "generation_mode": "fallback",
        "generation_error": "legacy_inline_path",
        "timeout_ms": _llm_node_config("product.report")["timeout_ms"],
        "prompt_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "input_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "evidence_object_count": prepared["evidence_object_count"],
        "started_at": now_iso(),
        "finished_at": now_iso(),
    }
    return _apply_product_report_result(run_id, task_id, decision)


def start_retrospective_thread(run_id: str, task_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    data = _local_retro_opening(run_id)
    first_topic = data.get("first_topic") or {}
    topic_title = first_topic.get("topic") or data.get("topic") or "问题"
    topic_body = first_topic.get("body") or data.get("body") or ""
    topic_id = _open_retro_topic(
        run_id,
        project_id=project_id,
        cycle_no=cycle_no,
        title=topic_title,
        opened_by="neko",
        evidence_refs=[{"body": topic_body, "owner": first_topic.get("owner"), "counterpart": first_topic.get("counterpart")}],
    )
    body = (data.get("body") or "").strip()
    result = {
        "topic_id": topic_id,
        "message_id": task_id,
        "reply_to_message_id": None,
        "from_agent": "neko",
        "to_agent": data.get("to_agent") or "33,xhs",
        "target_type": data.get("target_type") or "team",
        "topic": _topic_label(topic_title),
        "intent": data.get("intent") or "moderate",
        "round_no": 0,
        "body": body,
        "next_agents": data.get("next_agents") or ["33", "xhs"],
        "controversies": data.get("controversies", []),
        "next_topic": data.get("next_topic", {}),
    }
    _insert_retrospective_message(
        project_id=project_id,
        cycle_no=cycle_no,
        run_id=run_id,
        task_id=task_id,
        topic_id=topic_id,
        agent_id="neko",
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
    own_reviews = [_review_signal(section, approved, reason) for section, approved, reason in review_rows if section in sections]
    other_reviews = [_review_signal(section, approved, reason) for section, approved, reason in review_rows if section not in sections]
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
    mode = payload.get("mode") or "open"
    peer_agent = "xhs" if agent_id == "33" else "33"
    peer_msg = next((msg for msg in reversed(thread) if msg["from_agent"] == peer_agent), None)
    product_reports = _product_report_rows(run_id)
    product_signals = []
    product_tests: dict[str, list[str]] = {}
    for report in product_reports:
        if report["report_type"] == "product_test":
            product_signals.extend(report["report_json"].get("most_obvious_problems", [])[:2])
            product_tests.setdefault(report["agent_id"], []).extend(report["report_json"].get("most_obvious_problems", [])[:2])
        elif report["report_type"] == "product_evaluation_report":
            product_signals.extend(report["report_json"].get("top_product_issues", [])[:2])
    benchmark = next((row for row in product_reports if row["report_type"] == "benchmark_report"), None)
    relevant_context = {
        "own_reviews": own_reviews[:4],
        "other_reviews": other_reviews[:4],
        "memory_summary": memory.get("summary"),
        "reply_text": reply_text,
        "peer_message": peer_msg["body"] if peer_msg else "",
        "thread": _thread_excerpt(run_id),
        "product_signals": list(dict.fromkeys([item for item in product_signals if item]))[:4],
        "product_tests": product_tests,
        "benchmark_summary": benchmark["summary_text"] if benchmark else "",
        "final_titles": _main_titles(run_id),
    }
    data = _local_retro_comment(agent_id, payload, relevant_context)
    body = (data.get("body") or "").strip()
    to_agent = data.get("to_agent") or "neko"
    target_type = data.get("target_type") or "team"
    topic = data.get("topic") or "复盘讨论"
    intent = data.get("intent") or "comment"
    next_agents = data.get("next_agents") or []
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
    product_eval = _product_report_by_type(run_id, "product_evaluation_report")
    benchmark = _product_report_by_type(run_id, "benchmark_report")
    applied_rules = get_effective_optimization_log(project_id, "neko", (cycle_no or 0) + 1).get("compiled_rules") or []
    fallback_text = _local_retro_summary(run_id, _retro_thread_rows(run_id))["summary"]
    return {
        "node_type": "retrospective.summary",
        "project_id": project_id,
        "cycle_no": cycle_no,
        "task_id": None,
        "prompt_system": "你是 newsflow 项目的 manager。基于 retro topics、retro decisions、产品评估、benchmark 和已应用规则，输出正式的 retrospective summary。不要拼接线程原文。",
        "prompt_user": "\n".join(
            [
                f"run_id={run_id}",
                f"product_report={json.dumps({'summary': product_eval.get('summary_text'), 'top_product_issues': (product_eval.get('report_json', {}) or {}).get('top_product_issues', [])[:3], 'next_cycle_recommendations': (product_eval.get('report_json', {}) or {}).get('next_cycle_recommendations', [])[:4]}, ensure_ascii=False)}",
                f"benchmark={json.dumps({'summary': benchmark.get('summary_text'), 'most_visible_gap': (benchmark.get('report_json', {}) or {}).get('most_visible_gap', ''), 'next_cycle_actions': (benchmark.get('report_json', {}) or {}).get('next_cycle_actions', [])[:3]}, ensure_ascii=False)}",
                "retro decisions:",
                *[f"- {title}: {decision_summary or '无'}" for _, title, decision_summary in topic_rows],
                "applied_rules:",
                *[
                    f"- {rule.get('rule_type')} | target_agent={rule.get('target_agent')} | payload={json.dumps(rule.get('rule_payload') or {}, ensure_ascii=False)}"
                    for rule in applied_rules[:8]
                ],
                "返回 JSON：summary。summary 用自然中文，必须明确：执行问题、产品问题、原因、进入下一轮的改法、每个 agent 的责任。",
            ]
        ),
        "fallback_payload": {"summary": fallback_text},
        "evidence_object_count": len(topic_rows) + len(applied_rules) + 2,
    }


def _apply_retrospective_summary_result(run_id: str, decision: dict) -> dict:
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
    decision_lines = [f"- {title}: {decision_summary or '已收口'}" for _, title, decision_summary in topic_rows]
    summary = decision["summary"]
    if decision_lines:
        summary += "\n话题收口：\n" + "\n".join(decision_lines)
    execute(
        """
        UPDATE project_cycles
        SET retrospective_summary=%s, updated_at=NOW(), retrospective_completed_at=NOW()
        WHERE project_id=%s AND cycle_no=%s
        """,
        (summary, project_id, cycle_no),
    )
    return {
        "summary": summary,
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
    decision = {
        **prepared["fallback_payload"],
        "generation_mode": "fallback",
        "generation_error": "legacy_inline_path",
        "timeout_ms": _llm_node_config("retrospective.summary")["timeout_ms"],
        "prompt_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "input_size": len(prepared["prompt_system"]) + len(prepared["prompt_user"]),
        "evidence_object_count": prepared["evidence_object_count"],
        "started_at": now_iso(),
        "finished_at": now_iso(),
    }
    return _apply_retrospective_summary_result(run_id, decision)


def _agent_memory_blueprint(agent_id: str, cycle_no: int, summary: str, previous: dict) -> dict:
    common = {
        "agent_id": agent_id,
        "version_cycle": cycle_no,
        "updated_at": now_iso(),
        "retrospective_memory": summary,
    }
    if agent_id == "neko":
        return common | {
            "strategy_label": f"neko-cycle-{cycle_no}-review-first",
            "summary": "审核前置、主推影响优先、复盘收敛更结构化。",
            "execution_strategy": [
                "dispatch 时明确给出热点性、图片数、时效三条硬约束",
                "审核优先看图片和时效，再看热点性和去重",
                "终稿前先校对四板块主推首句是否直达影响",
            ],
            "quality_checks": [
                "每板块至少 10 条且主推/副推图片满足结构要求",
                "review/reject 原因必须可执行",
                "复盘总结必须包含问题、堵点、缺失能力、下一轮建议",
            ],
            "review_standards": [
                "拒绝图片不稳定、来源不可靠、发布时间超窗的候选",
                "主推必须具备影响描述和三图资源",
            ],
        }
    if agent_id == "33":
        return common | {
            "strategy_label": f"collector-33-cycle-{cycle_no}-whitelist",
            "summary": "政治经济/科技板块先白名单采集，再做影响句补写和同源去重。",
            "execution_strategy": [
                "优先 Reuters、AP、BBC、FT、Bloomberg、官方公告",
                "科技条目优先公司官方博客/公告和主流科技媒体",
                "每条补一条“为什么值得关注”的中文影响句",
            ],
            "source_whitelist": [
                "Reuters",
                "Associated Press",
                "Bloomberg",
                "Financial Times",
                "The Verge",
                "TechCrunch",
                "官方公告",
            ],
            "source_blacklist": previous.get("source_blacklist", []) + ["低质量聚合站"],
            "quality_checks": [
                "去掉同一事件的重复来源",
                "主推候选必须带至少 3 张可用图片的来源",
            ],
        }
    return common | {
        "strategy_label": f"collector-xhs-cycle-{cycle_no}-tight-briefs",
        "summary": "体育娱乐/其他板块优先官方渠道，短讯更紧凑，图片稳定性优先。",
        "execution_strategy": [
            "优先赛事官方、主流文娱媒体和国际主流媒体",
            "短讯先保留结果、时间、影响范围，再补背景",
            "图片优先首发稿源或官方图床",
        ],
        "source_whitelist": [
            "ESPN",
            "BBC Sport",
            "官方赛事渠道",
            "Variety",
            "Reuters",
            "AP",
        ],
        "source_blacklist": previous.get("source_blacklist", []) + ["无来源转载站"],
        "quality_checks": [
            "减少边缘八卦内容，优先全球影响更大的事件",
            "图片链接必须直接可访问",
        ],
    }


def self_optimize_agent(run_id: str, agent_id: str) -> dict:
    project_id, cycle_no = get_run_project_context(run_id)
    previous = get_project_memory(project_id, agent_id)
    summary_row = fetch_one(
        "SELECT retrospective_summary FROM project_cycles WHERE project_id=%s AND cycle_no=%s",
        (project_id, cycle_no),
    )
    summary = summary_row[0] if summary_row and summary_row[0] else "本轮需要强化规则前置与热点筛选。"
    thread = _retro_thread_rows(run_id)
    relevant = []
    for msg in thread:
        to_agent = msg["to_agent"] or ""
        if msg["from_agent"] == agent_id or agent_id in to_agent or to_agent in {"all", "team"}:
            relevant.append(msg)
    memory = _agent_memory_blueprint(agent_id, cycle_no, summary, previous)
    fallback = {
        "summary": memory.get("summary", "下一轮继续优化"),
        "exposed_issues": memory.get("exposed_issues", []),
        "next_cycle_strategy": list(memory.get("execution_strategy", [])),
        "next_cycle_quality_checks": list(memory.get("quality_checks", [])),
        "role_improvement_plan": memory.get("role_improvement_plan", ""),
    }
    data = _local_self_optimize(agent_id, cycle_no, summary, previous, relevant, memory)
    memory.update(
        {
            "summary": data.get("summary") or fallback["summary"],
            "exposed_issues": data.get("exposed_issues") or fallback["exposed_issues"],
            "next_cycle_strategy": data.get("next_cycle_strategy") or fallback["next_cycle_strategy"],
            "next_cycle_quality_checks": data.get("next_cycle_quality_checks") or fallback["next_cycle_quality_checks"],
            "role_improvement_plan": data.get("role_improvement_plan") or fallback["role_improvement_plan"],
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
    rule_specs = []
    if memory.get("source_whitelist"):
        rule_specs.append(
            (
                "source_whitelist",
                {"sources": memory.get("source_whitelist", [])},
                "根据本轮复盘与产品评估，下一轮优先使用更稳定的来源白名单。",
            )
        )
    if memory.get("source_blacklist"):
        rule_specs.append(
            (
                "source_blacklist",
                {"sources": memory.get("source_blacklist", [])},
                "根据本轮复盘与产品评估，下一轮过滤低质量来源。",
            )
        )
    if agent_id in {"33", "xhs"}:
        rule_specs.append(
            (
                "image_availability_threshold",
                {"min_images": 1},
                "采集阶段优先保留带图候选，降低后续主推图片不足风险。",
            )
        )
        rule_specs.append(
            (
                "short_brief_compression_rule",
                {"max_chars": 44},
                "短讯继续压缩，优先保留结果、时间与影响范围。",
            )
        )
    if agent_id == "neko":
        rule_specs.append(
            (
                "lead_sentence_rule",
                {"style": "impact_first"},
                "主推首句必须先说影响或结果，再补事实来源。",
            )
        )
        rule_specs.append(
            (
                "publish_gate",
                {"require_zero_proofread_blockers": True},
                "proofread blocker 未清零时禁止 publish。",
            )
        )
    for rule_type, rule_payload, rationale in rule_specs:
        execute(
            """
            INSERT INTO optimization_rules(
                rule_id, project_id, run_id, cycle_no, source, owner_scope, target_agent,
                effective_from_cycle, rule_type, rule_payload, rationale, status
            )
            VALUES (%s,%s,%s,%s,'agent.self_optimize','agent',%s,%s,%s,%s::jsonb,%s,'active')
            """,
            (
                f"opr-{uuid.uuid4().hex[:10]}",
                project_id,
                run_id,
                cycle_no,
                agent_id,
                cycle_no + 1,
                rule_type,
                jdump(rule_payload),
                rationale,
            ),
        )
    return memory


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
        "product_test_neko.html",
        "product_test_neko.md",
        "product_test_neko.json",
        "product_test_33.html",
        "product_test_33.md",
        "product_test_33.json",
        "product_test_xhs.html",
        "product_test_xhs.md",
        "product_test_xhs.json",
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
    for agent_id in ["neko", "33", "xhs"]:
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
        for agent_id in ["neko", "33", "xhs"]:
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
        SET status='completed', current_phase='agent.self_optimize', completed_at=NOW()
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
            WHERE run_id=%s AND phase='retrospective.comment'
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
            "retrospective.comment",
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
    return [agent.strip() for agent in str(value or "").split(",") if agent.strip() in {"33", "xhs", "neko"}]


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
        WHERE run_id=%s AND phase='retrospective.comment' AND status IN ('pending', 'running')
        """,
        (run_id,),
    )[0]
    if pending_comments:
        return
    non_manager = [msg for msg in topic_messages if msg["from_agent"] in {"33", "xhs"}]
    manager_msgs = [msg for msg in topic_messages if msg["from_agent"] == "neko"]
    if current_topic["status"] == "open" and non_manager:
        _set_retro_topic_status(topic_id, "debating")
        opener = non_manager[-1]
        target_agents = sorted({msg["from_agent"] for msg in non_manager if msg["from_agent"] in {"33", "xhs"}}) or ["33", "xhs"]
        _dispatch_retro_comment_tasks(
            run_id,
            project_id,
            cycle_no,
            trace_ctx,
            round_no=max(msg["round_no"] for msg in topic_messages) + 1,
            agents=["neko"],
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
            WHERE run_id=%s AND phase='retrospective.comment' AND agent_id='neko' AND status='pending'
              AND COALESCE((payload->>'topic_id')::text, '')=%s
            """,
            ("moderator_followup", run_id, topic_id),
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
                to_agent="neko",
                intent="proposal",
            )
            execute(
                """
                UPDATE tasks
                SET payload=jsonb_set(payload,'{mode}',to_jsonb(%s::text),true)
                WHERE run_id=%s AND phase='retrospective.comment' AND status='pending'
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
                opened_by="neko",
                evidence_refs=[next_topic],
            )
            next_agents = sorted(
                {
                    agent
                    for agent in [
                        next_topic.get("owner"),
                        *str(next_topic.get("counterpart") or "").split(","),
                    ]
                    if agent in {"33", "xhs"}
                }
            ) or ["33", "xhs"]
            kickoff_body = (
                f"上一个话题先收口。现在换到更值得继续争的问题：{next_topic.get('body') or next_topic.get('topic') or '问题'}。"
                " 还是只围绕工件和取舍说话，不要回到流程口号。"
            )
            _insert_retrospective_message(
                project_id=project_id,
                cycle_no=cycle_no,
                run_id=run_id,
                task_id=None,
                topic_id=next_topic_id,
                agent_id="neko",
                message_id=f"rtm-{uuid.uuid4().hex[:10]}",
                reply_to_message_id=None,
                to_agent=",".join(next_agents),
                target_type="team",
                topic=next_topic.get("topic") or "问题",
                intent="moderate",
                round_no=max(msg["round_no"] for msg in topic_messages) + 1,
                body=kickoff_body,
            )
            _dispatch_retro_comment_tasks(
                run_id,
                project_id,
                cycle_no,
                trace_ctx,
                round_no=max(msg["round_no"] for msg in topic_messages) + 2,
                agents=next_agents,
                reply_to_message_id=None,
                topic_id=next_topic_id,
                topic=next_topic.get("topic") or "问题",
                target_type="agent",
                to_agent="neko",
                intent="critique",
            )
            execute(
                """
                UPDATE tasks
                SET payload=jsonb_set(payload,'{mode}',to_jsonb(%s::text),true)
                WHERE run_id=%s AND phase='retrospective.comment' AND status='pending'
                  AND COALESCE((payload->>'topic_id')::text, '')=%s
                """,
                ("open", run_id, next_topic_id),
        )


def _prepare_task_llm_job(task: dict) -> dict:
    phase = task["phase"]
    if phase == "proofread.decision.explanation":
        return _prepare_proofread_decision_explanation_job(task["run_id"], task["task_id"])
    if phase == "draft.revise":
        prepared = _prepare_draft_revise_job(task["run_id"])
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
    if task["phase"] == "proofread.decision.explanation":
        result = _apply_proofread_explanation_result(
            task["run_id"],
            task["task_id"],
            {
                **(job.get("fallback_payload") or {}),
                "generation_mode": "failed",
                "generation_error": job.get("generation_error", ""),
                "timeout_ms": job.get("timeout_ms"),
                "prompt_size": job.get("prompt_size"),
                "input_size": job.get("input_size"),
                "evidence_object_count": job.get("evidence_object_count"),
                "started_at": job.get("started_at"),
                "finished_at": job.get("finished_at"),
            },
        )
        complete_task(task["task_id"], result | {"status": "explained"})
        return
    fail_task(task["task_id"], job.get("generation_error") or f"llm job failed: {job['node_type']}")


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
        if not job or job["status"] in {"pending", "running", "retrying"}:
            continue
        if job["status"] == "failed":
            if _llm_node_config(task["phase"]).get("critical"):
                _fail_run_due_to_llm(task, job)
            else:
                _complete_noncritical_llm_failure(task, job)
            continue
        data = job["result_json"] or {}
        if task["phase"] == "proofread.decision.explanation":
            result = _apply_proofread_explanation_result(task["run_id"], task["task_id"], data)
            complete_task(task["task_id"], result)
        elif task["phase"] == "draft.revise":
            result = _apply_draft_revise_result(task["run_id"], data)
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


def _ensure_retro_decision_job(run_id: str, *, topic_id: str, title: str, thread: list[dict], owner_agent: str = "neko") -> dict:
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
        if job["status"] == "failed":
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
            WHERE project_id=%s AND cycle_no=%s AND phase='agent.self_optimize' AND status='completed'
            """,
            (project_id, cycle_no),
        )[0]
        if self_opt_done >= 3:
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
        for section, agent_id in ALL_SECTION_ASSIGNMENTS:
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
                    "neko",
                    "manager",
                    section,
                    "material.review",
                    latest_collect[1],
                    {},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "material.review")

        for section, agent_id in ALL_SECTION_ASSIGNMENTS:
            latest_review = fetch_one(
                """
                SELECT r.approved, r.reason, t.retry_count
                FROM reviews r
                JOIN tasks t ON t.task_id = r.review_task_id
                WHERE r.run_id=%s AND r.section=%s
                ORDER BY t.retry_count DESC, r.created_at DESC
                LIMIT 1
                """,
                (run_id, section),
            )
            if latest_review and not latest_review[0]:
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
                        "collector",
                        section,
                        "material.collect",
                        retry,
                        {"target_count": 12, "rework_reason": latest_review[1]},
                        trace_ctx,
                        project_id,
                        cycle_no,
                    )
                    dispatch_task(
                        run_id,
                        collect_task_id,
                        agent_id,
                        "collector",
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
            latest_review = fetch_one(
                """
                SELECT r.approved
                FROM reviews r
                JOIN tasks t ON t.task_id = r.review_task_id
                WHERE r.run_id=%s AND r.section=%s
                ORDER BY t.retry_count DESC, r.created_at DESC
                LIMIT 1
                """,
                (run_id, section),
            )
            if latest_review and latest_review[0]:
                approved_count += 1
        compose_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose'", (run_id,))[0]
        if approved_count == 4 and compose_exists == 0:
            dispatch_task(run_id, None, "neko", "manager", "全局", "draft.compose", 0, {}, trace_ctx, project_id, cycle_no)
            _run_current_phase(run_id, "draft.compose")

        if project_id:
            compose_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose' AND status='completed'", (run_id,))[0]
            proofread_start_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='proofread.start'", (run_id,))[0]
            if compose_done > 0 and proofread_start_exists == 0:
                dispatch_task(
                    run_id,
                    None,
                    "neko",
                    "manager",
                    "全局",
                    "proofread.start",
                    0,
                    {},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                dispatch_task(run_id, None, "33", "collector", "政治经济,科技", "proofread.issue.submit", 0, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, "xhs", "collector", "体育娱乐,其他", "proofread.issue.submit", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "proofread.start")

            proofread_round = _proofread_round(run_id)
            issue_submit_done = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.issue.submit' AND retry_count=%s AND status='completed'
                """,
                (run_id, proofread_round),
            )[0]
            decision_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.decision' AND retry_count=%s
                """,
                (run_id, proofread_round),
            )[0]
            if compose_done > 0 and issue_submit_done >= 2 and decision_exists == 0:
                dispatch_task(run_id, None, "neko", "manager", "全局", "proofread.decision", proofread_round, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "proofread.decision")

            decision_done = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.decision' AND retry_count=%s AND status='completed'
                """,
                (run_id, proofread_round),
            )[0]
            explanation_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.decision.explanation' AND retry_count=%s
                """,
                (run_id, proofread_round),
            )[0]
            revise_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='draft.revise' AND retry_count=%s
                """,
                (run_id, proofread_round),
            )[0]
            if decision_done > 0 and revise_exists == 0:
                dispatch_task(run_id, None, "neko", "manager", "全局", "draft.revise", proofread_round, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "draft.revise")
            if decision_done > 0 and explanation_exists == 0:
                dispatch_task(run_id, None, "neko", "manager", "全局", "proofread.decision.explanation", proofread_round, {}, trace_ctx, project_id, cycle_no)

            revise_done = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='draft.revise' AND retry_count=%s AND status='completed'
                """,
                (run_id, proofread_round),
            )[0]
            recheck_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.recheck' AND retry_count=%s
                """,
                (run_id, proofread_round),
            )[0]
            if revise_done > 0 and recheck_exists == 0:
                dispatch_task(run_id, None, "33", "collector", "政治经济,科技", "proofread.recheck", proofread_round, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, "xhs", "collector", "体育娱乐,其他", "proofread.recheck", proofread_round, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "proofread.recheck")

            recheck_done = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.recheck' AND retry_count=%s AND status='completed'
                """,
                (run_id, proofread_round),
            )[0]
            blocker_count = _active_blocker_count(run_id)
            next_submit_exists = fetch_one(
                """
                SELECT COUNT(*) FROM tasks
                WHERE run_id=%s AND phase='proofread.issue.submit' AND retry_count>%s
                """,
                (run_id, proofread_round),
            )[0]
            if recheck_done >= 2 and blocker_count > 0 and next_submit_exists == 0:
                next_round = proofread_round + 1
                dispatch_task(run_id, None, "33", "collector", "政治经济,科技", "proofread.issue.submit", next_round, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, "xhs", "collector", "体育娱乐,其他", "proofread.issue.submit", next_round, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "proofread.issue.submit")

            report_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='report.publish'", (run_id,))[0]
            if recheck_done >= 2 and blocker_count == 0 and report_exists == 0:
                dispatch_task(run_id, None, "neko", "manager", "全局", "report.publish", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "report.publish")

            report_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='report.publish' AND status='completed'",
                (run_id,),
            )[0]
            product_test_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.test'",
                (run_id,),
            )[0]
            if report_done > 0:
                for target_agent in ["neko", "33", "xhs"]:
                    agent_test_exists = fetch_one(
                        """
                        SELECT COUNT(*) FROM tasks
                        WHERE run_id=%s AND phase='product.test' AND agent_id=%s
                        """,
                        (run_id, target_agent),
                    )[0]
                    if agent_test_exists == 0:
                        dispatch_task(
                            run_id,
                            None,
                            target_agent,
                            AGENT_ROLES[target_agent],
                            "全局",
                            "product.test",
                            0,
                            {},
                            trace_ctx,
                            project_id,
                            cycle_no,
                        )
                        _run_current_phase(run_id, "product.test")
                        break

            product_test_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.test' AND status='completed'",
                (run_id,),
            )[0]
            benchmark_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.benchmark'",
                (run_id,),
            )[0]
            if report_done > 0 and product_test_done >= 3 and benchmark_exists == 0:
                dispatch_task(
                    run_id,
                    None,
                    "neko",
                    "manager",
                    "全局",
                    "product.benchmark",
                    0,
                    {},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "product.benchmark")

            benchmark_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.benchmark' AND status='completed'",
                (run_id,),
            )[0]
            product_report_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.report'",
                (run_id,),
            )[0]
            if report_done > 0 and product_test_done >= 3 and benchmark_done > 0 and product_report_exists == 0:
                dispatch_task(
                    run_id,
                    None,
                    "neko",
                    "manager",
                    "全局",
                    "product.report",
                    0,
                    {},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                _run_current_phase(run_id, "product.report")

            product_report_done = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='product.report' AND status='completed'",
                (run_id,),
            )[0]
            retro_start_exists = fetch_one(
                "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.start'",
                (run_id,),
            )[0]
            if product_report_done > 0 and retro_start_exists == 0:
                if report_done == 0:
                    continue
                project = _project_row(project_id)
                dispatch_task(
                    run_id,
                    None,
                    "neko",
                    "manager",
                    "全局",
                    "retrospective.start",
                    0,
                    {"retrospective_seconds": project[8]},
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
                _run_current_phase(run_id, "retrospective.start")
        else:
            compose_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose' AND status='completed'", (run_id,))[0]
            discussion_start_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.start'", (run_id,))[0]
            if compose_done > 0 and discussion_start_exists == 0:
                dispatch_task(
                    run_id,
                    None,
                    "neko",
                    "manager",
                    "全局",
                    "discussion.start",
                    0,
                    {"discussion_seconds": discussion_seconds},
                    trace_ctx,
                    project_id,
                    cycle_no,
                )
                dispatch_task(run_id, None, "33", "collector", "全局", "discussion.comment", 0, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, "xhs", "collector", "全局", "discussion.comment", 0, {}, trace_ctx, project_id, cycle_no)
                dispatch_task(run_id, None, "neko", "manager", "全局", "discussion.comment", 0, {}, trace_ctx, project_id, cycle_no)
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
                    dispatch_task(run_id, None, "neko", "manager", "全局", "discussion.summarize", 0, {}, trace_ctx, project_id, cycle_no)
                    _run_current_phase(run_id, "discussion.summarize")

            summarize_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.summarize' AND status='completed'", (run_id,))[0]
            revise_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.revise'", (run_id,))[0]
            if summarize_done > 0 and revise_exists == 0:
                dispatch_task(run_id, None, "neko", "manager", "全局", "draft.revise", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "draft.revise")

            revise_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.revise' AND status='completed'", (run_id,))[0]
            report_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='report.publish'", (run_id,))[0]
            if revise_done > 0 and report_exists == 0 and summarize_done > 0:
                dispatch_task(run_id, None, "neko", "manager", "全局", "report.publish", 0, {}, trace_ctx, project_id, cycle_no)
                _run_current_phase(run_id, "report.publish")

        retro_started = fetch_one(
            """
            SELECT task_id, started_at, payload::text, result::text
            FROM tasks
            WHERE run_id=%s AND phase='retrospective.start' AND status='completed'
            ORDER BY started_at DESC LIMIT 1
            """,
            (run_id,),
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
                WHERE run_id=%s AND phase='retrospective.comment' AND status IN ('pending', 'running')
                """,
                (run_id,),
            )[0]
            retro_thread = _retro_thread_rows(run_id)
            open_or_closing_topics = fetch_one(
                "SELECT COUNT(*) FROM retro_topics WHERE run_id=%s AND status IN ('open','debating','closing')",
                (run_id,),
            )[0]
            if retro_summary_exists == 0 and pending_comments == 0 and open_or_closing_topics == 0 and len(retro_thread) >= RETRO_MIN_THREAD_MESSAGES:
                if (datetime.now(started.tzinfo) - started).total_seconds() >= payload["retrospective_seconds"]:
                    dispatch_task(run_id, None, "neko", "manager", "全局", "retrospective.summary", 0, {}, trace_ctx, project_id, cycle_no)
                    _run_current_phase(run_id, "retrospective.summary")

        retro_summary_done = fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='retrospective.summary' AND status='completed'",
            (run_id,),
        )[0]
        self_opt_exists = fetch_one(
            "SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='agent.self_optimize'",
            (run_id,),
        )[0]
        if retro_summary_done > 0 and self_opt_exists == 0:
            for agent_id in ["neko", "33", "xhs"]:
                dispatch_task(
                    run_id,
                    None,
                    agent_id,
                    AGENT_ROLES[agent_id],
                    "全局",
                    "agent.self_optimize",
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
            _run_current_phase(run_id, "agent.self_optimize")


def run_worker(agent_id: str):
    service_name = "orchestrator" if agent_id == "orchestrator" else f"agent-{agent_id}"
    while True:
        task = claim_task(agent_id)
        if not task:
            time.sleep(2)
            continue
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
                    items = collect_news(task["section"], 16)
                    items = _apply_collection_guidance(items, task["payload"].get("optimization_log_snapshot") or {})
                    save_materials(task["run_id"], task["task_id"], task["section"], task["agent_id"], items)
                    memory_note = _memory_summary(task["payload"].get("agent_memory_snapshot"))
                    optimization_log = task["payload"].get("optimization_log_snapshot") or {}
                    span.set_attribute("status", "collected")
                    span.set_attribute("source_count", len(items))
                    complete_task(
                        task["task_id"],
                        {
                            "status": "collected",
                            "source_count": len(items),
                            "memory_summary": memory_note,
                            "message_body": f"已完成【{task['section']}】板块采集，收集到 {len(items)} 条候选素材。本轮应用策略：{memory_note}。附加优化日志 {len(optimization_log.get('combined') or [])} 条。",
                        },
                    )
                elif task["phase"] == "material.submit":
                    count = fetch_one(
                        "SELECT COUNT(*) FROM materials WHERE run_id=%s AND section=%s AND source_agent=%s",
                        (task["run_id"], task["section"], task["agent_id"]),
                    )[0]
                    top_rows = fetch_all(
                        """
                        SELECT title, source_media
                        FROM materials
                        WHERE run_id=%s AND section=%s AND source_agent=%s
                        ORDER BY published_at DESC
                        LIMIT 5
                        """,
                        (task["run_id"], task["section"], task["agent_id"]),
                    )
                    message = [f"提交【{task['section']}】板块候选素材，共 {count} 条。"]
                    for title, source_media in top_rows:
                        message.append(f"- {title} | {source_media}")
                    span.set_attribute("status", "submitted")
                    span.set_attribute("source_count", count)
                    complete_task(task["task_id"], {"status": "submitted", "source_count": count, "message_body": "\n".join(message)})
                elif task["phase"] == "material.review":
                    result = review_section(task["run_id"], task["section"], task["task_id"])
                    phase = "material.reject" if not result["approved"] else "material.review"
                    with workflow_span(service_name, phase, attrs | {"status": "rejected" if not result["approved"] else "approved"}):
                        pass
                    span.set_attribute("status", "approved" if result["approved"] else "rejected")
                    span.set_attribute("source_count", len(result["selected_material_ids"]))
                    complete_task(task["task_id"], result | {"status": "approved" if result["approved"] else "rejected"})
                elif task["phase"] == "draft.compose":
                    result = compose_draft(task["run_id"])
                    span.set_attribute("status", "drafted")
                    complete_task(task["task_id"], result | {"status": "drafted", "draft_len": len(result["draft_markdown"])})
                elif task["phase"] == "draft.review.start":
                    seconds = task["payload"].get("discussion_seconds", SETTINGS.discussion_test_seconds)
                    span.set_attribute("status", "started")
                    complete_task(
                        task["task_id"],
                        {
                            "discussion_seconds": seconds,
                            "status": "started",
                            "message_body": f"校稿阶段开始，当前测试轮校稿时长设为 {seconds} 秒。",
                        },
                    )
                elif task["phase"] == "draft.review.comment":
                    comment = create_draft_review_comment(task["run_id"], task["task_id"], task["agent_id"])
                    span.set_attribute("status", "commented")
                    complete_task(task["task_id"], {"status": "commented", "comment_text": comment})
                elif task["phase"] == "draft.review.summarize":
                    result = summarize_draft_review(task["run_id"])
                    span.set_attribute("status", "summarized")
                    complete_task(task["task_id"], result | {"status": "summarized"})
                elif task["phase"] == "proofread.start":
                    result = start_proofread(task["run_id"], task["task_id"])
                    span.set_attribute("status", "started")
                    complete_task(task["task_id"], result)
                elif task["phase"] == "proofread.issue.submit":
                    result = submit_proofread_issues(task["run_id"], task["task_id"], task["agent_id"])
                    span.set_attribute("status", "submitted")
                    span.set_attribute("issue.count", result["issue_count"])
                    complete_task(task["task_id"], result)
                elif task["phase"] == "proofread.decision":
                    result = decide_proofread_issues(task["run_id"], task["task_id"])
                    span.set_attribute("status", "decided")
                    span.set_attribute("proofread.blocker_count_after_decision", result["blocker_count_after_decision"])
                    complete_task(task["task_id"], result)
                elif task["phase"] == "proofread.decision.explanation":
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "draft.revise":
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "proofread.recheck":
                    result = recheck_proofread_issues(task["run_id"], task["task_id"], task["agent_id"])
                    span.set_attribute("status", "rechecked")
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
                elif task["phase"] == "product.report":
                    job = _ensure_task_llm_job(task)
                    span.set_attribute("status", "waiting_llm")
                    span.set_attribute("llm.job_id", job["job_id"])
                elif task["phase"] == "retrospective.start":
                    seconds = task["payload"].get("retrospective_seconds", SETTINGS.project_retrospective_default_seconds)
                    kickoff = start_retrospective_thread(task["run_id"], task["task_id"])
                    project_id, cycle_no = get_run_project_context(task["run_id"])
                    trace_ctx = get_run_trace_context(task["run_id"])
                    next_agents = [agent for agent in (kickoff.get("next_agents") or ["33", "xhs"]) if agent in {"33", "xhs"}]
                    if not next_agents:
                        next_agents = ["33", "xhs"]
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
                        to_agent="neko",
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
                elif task["phase"] == "agent.self_optimize":
                    memory = self_optimize_agent(task["run_id"], task["agent_id"])
                    span.set_attribute("status", "optimized")
                    complete_task(
                        task["task_id"],
                        {
                            "status": "optimized",
                            "message_body": memory["summary"],
                            "memory_version": memory["version_cycle"],
                        },
                    )
                else:
                    raise RuntimeError(f"unknown phase {task['phase']}")
        except Exception as exc:
            fail_task(task["task_id"], str(exc))
            time.sleep(1)
