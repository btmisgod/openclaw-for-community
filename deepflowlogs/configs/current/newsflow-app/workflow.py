from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from deep_translator import GoogleTranslator

from .config import ROOT, load_settings
from .db import execute, execute_returning, fetch_all, fetch_one, get_conn, init_schema, jdump
from .llm import chat_json, chat_text
from .news import collect_news
from .rendering import parse_trace_id, write_final_report_html
from .telemetry import extract_context, inject_current_context, workflow_span


SETTINGS = load_settings()
WORKFLOW_ID = "intl-news-hotspots"
RUN_OUTPUT_DIR = ROOT / "output"
TRANSLATOR = GoogleTranslator(source="auto", target="zh-CN")


def now_iso():
    return datetime.now().isoformat()


def create_db_if_needed():
    # database itself is created externally; schema is created here
    init_schema()
    execute(
        """
        UPDATE tasks
        SET status='pending', started_at=NULL
        WHERE status='running'
        """
    )


def new_run(discussion_seconds: int | None = None) -> str:
    run_id = uuid.uuid4().hex[:12]
    discussion_seconds = discussion_seconds or SETTINGS.discussion_test_seconds
    root_attrs = {
        "workflow_id": WORKFLOW_ID,
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
            INSERT INTO workflow_runs(workflow_id, run_id, status, discussion_seconds, current_phase, notes)
            VALUES (%s,%s,'running',%s,'created',%s::jsonb)
            """,
            (WORKFLOW_ID, run_id, discussion_seconds, jdump({"trace_context": run_trace, "trace_id": trace_id})),
        )
        for agent_id, role, sections in [
            ("33", "collector", ["政治经济", "科技"]),
            ("xhs", "collector", ["体育娱乐", "其他"]),
        ]:
            for section in sections:
                collect_task_id = dispatch_task(
                    run_id=run_id,
                    parent_task_id=None,
                    agent_id=agent_id,
                    agent_role=role,
                    section=section,
                    phase="material.collect",
                    retry_count=0,
                    payload={"target_count": 12},
                    parent_trace=run_trace,
                )
                dispatch_task(
                    run_id=run_id,
                    parent_task_id=collect_task_id,
                    agent_id=agent_id,
                    agent_role=role,
                    section=section,
                    phase="material.submit",
                    retry_count=0,
                    payload={},
                    parent_trace=run_trace,
                )
    return run_id


def create_task(
    run_id: str,
    parent_task_id: str | None,
    agent_id: str,
    agent_role: str,
    section: str,
    phase: str,
    retry_count: int,
    payload: dict,
) -> str:
    task_id = uuid.uuid4().hex[:16]
    execute(
        """
        INSERT INTO tasks(task_id, run_id, parent_task_id, agent_id, agent_role, section, phase, retry_count, status, payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)
        """,
        (task_id, run_id, parent_task_id, agent_id, agent_role, section, phase, retry_count, jdump(payload)),
    )
    return task_id


def get_run_trace_context(run_id: str) -> dict:
    row = fetch_one("SELECT notes::text FROM workflow_runs WHERE run_id=%s", (run_id,))
    if not row or not row[0]:
        return {}
    notes = json.loads(row[0])
    return notes.get("trace_context", {})


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
) -> str:
    attrs = {
        "workflow_id": WORKFLOW_ID,
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
                enriched_payload["message_body"] = f"请采集【{section}】板块近24小时国际热点，提交不少于 {payload.get('target_count', 12)} 条候选素材，并保留图片、来源、发布时间、原文链接。"
            elif phase == "material.collect" and retry_count > 0:
                enriched_payload["message_body"] = f"请重做【{section}】板块采集，重点修复：{payload.get('rework_reason', '补强热点与图片质量')}。"
            elif phase == "material.review":
                enriched_payload["message_body"] = f"请审核【{section}】板块候选素材，核对时效、来源可靠性、图片数量和热点性。"
            elif phase == "discussion.comment":
                enriched_payload["message_body"] = "请对初稿提出明确修改意见，避免空泛表述。"
            elif phase == "discussion.summarize":
                enriched_payload["message_body"] = "请汇总讨论意见并形成统一修订方案。"
            elif phase == "draft.compose":
                enriched_payload["message_body"] = "请把四个板块的已通过素材整合为初稿。"
            elif phase == "report.publish":
                enriched_payload["message_body"] = "请输出最终成稿，并落盘为 Markdown / HTML / JSON。"
        enriched_payload["trace_context"] = inject_current_context()
        return create_task(run_id, parent_task_id, agent_id, agent_role, section, phase, retry_count, enriched_payload)


def claim_task(agent_id: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT task_id, run_id, parent_task_id, agent_id, agent_role, section, phase, retry_count, payload::text
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


def generate_section_content(section: str, items: list[dict]) -> dict:
    ranked = sorted(items[:10], key=lambda item: (len(item.get("images", [])), item["published_at"]), reverse=True)
    main = ranked[0]
    secondaries = ranked[1:3]
    briefs = ranked[3:10]

    def translate_text(text: str) -> str:
        if not text:
            return ""
        try:
            return TRANSLATOR.translate(text)
        except Exception:
            return text

    def enrich(item: dict, max_len: int, image_limit: int) -> dict:
        title_zh = translate_text(item["title"])
        summary_src = item["metadata"].get("summary_en", "") or item["title"]
        summary_zh = translate_text(summary_src).replace("\n", " ").strip()
        if len(summary_zh) > max_len:
            summary_zh = summary_zh[: max_len - 1].rstrip("，。；;,. ") + "。"
        return item | {"title": title_zh, "summary_zh": summary_zh, "images": item.get("images", [])[:image_limit]}

    return {
        "main": enrich(main, 200, 3),
        "secondary": [
            enrich(item, 100, 1)
            for item in secondaries
        ],
        "briefs": [
            enrich(item, 50, 0)
            for item in briefs
        ],
    }


def compose_draft(run_id: str) -> dict:
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

    md_lines = [f"# 近24小时国际新闻热点\n", f"- workflow_id: {WORKFLOW_ID}", f"- run_id: {run_id}", f"- 时区: {SETTINGS.timezone}", ""]
    for section in sections:
        md_lines.append(f"## {section}")
        data = assembled[section]
        main = data["main"]
        md_lines.append(f"### 主推 | {main['title']}")
        md_lines.append(f"- 来源: {main['source_media']}")
        md_lines.append(f"- 发布时间: {main['published_at']}")
        md_lines.append(f"- 原文链接: {main['link']}")
        md_lines.append(f"- 图片: {', '.join(main.get('images', [])[:3])}")
        md_lines.append(main["summary_zh"])
        md_lines.append("")
        for idx, sec in enumerate(data["secondary"], 1):
            md_lines.append(f"### 副推{idx} | {sec['title']}")
            md_lines.append(f"- 来源: {sec['source_media']}")
            md_lines.append(f"- 发布时间: {sec['published_at']}")
            md_lines.append(f"- 原文链接: {sec['link']}")
            md_lines.append(f"- 图片: {', '.join(sec.get('images', [])[:1])}")
            md_lines.append(sec["summary_zh"])
            md_lines.append("")
        md_lines.append("### 其他 7 条")
        for brief in data["briefs"]:
            md_lines.append(
                f"- {brief['title']} | {brief['source_media']} | {brief['published_at']} | {brief['link']} | {brief['summary_zh']}"
            )
        md_lines.append("")
    draft_markdown = "\n".join(md_lines)
    execute(
        """
        INSERT INTO outputs(run_id, draft_markdown, final_json)
        VALUES (%s,%s,%s::jsonb)
        ON CONFLICT (run_id) DO UPDATE SET draft_markdown=EXCLUDED.draft_markdown, final_json=EXCLUDED.final_json, updated_at=NOW()
        """,
        (run_id, draft_markdown, jdump(assembled)),
    )
    return {"draft_markdown": draft_markdown, "sections": assembled}


def create_discussion_comment(run_id: str, task_id: str, agent_id: str) -> str:
    role_comments = {
        "neko": "建议统一四个板块的主推语气，主推首句直接点明事件影响；检查主推图片是否足够稳定。",
        "33": "政治经济和科技板块可再强化“为什么值得关注”的一句话，避免只罗列进展；副推之间尽量减少同源事件重复。",
        "xhs": "体育娱乐和其他板块的短讯可以更紧凑，优先保留结果、时间和影响范围；图片条目要确保链接可直接访问。",
    }
    comment = role_comments.get(agent_id, "建议统一措辞并检查链接可访问性。")
    execute(
        "INSERT INTO discussions(run_id, task_id, agent_id, comment_text) VALUES (%s,%s,%s,%s)",
        (run_id, task_id, agent_id, comment),
    )
    return comment


def summarize_discussion(run_id: str) -> str:
    comments = fetch_all("SELECT agent_id, comment_text FROM discussions WHERE run_id=%s ORDER BY created_at", (run_id,))
    plan = "\n".join([f"- {agent_id}: {comment_text}" for agent_id, comment_text in comments])
    execute("UPDATE outputs SET revision_plan=%s, updated_at=NOW() WHERE run_id=%s", (plan, run_id))
    return plan


def revise_and_publish(run_id: str) -> dict:
    row = fetch_one("SELECT draft_markdown, revision_plan, final_json::text FROM outputs WHERE run_id=%s", (run_id,))
    draft_markdown, revision_plan, final_json = row[0], row[1], json.loads(row[2])
    final_markdown = "\n".join(
        [
            "# 近24小时国际新闻热点（终稿）",
            "",
            "## 修改说明",
            revision_plan or "- 无",
            "",
            draft_markdown,
        ]
    )
    run_dir = RUN_OUTPUT_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    md_path = run_dir / "final_report.md"
    json_path = run_dir / "final_report.json"
    md_path.write_text(final_markdown)
    json_path.write_text(json.dumps(final_json, ensure_ascii=False, indent=2))
    html_path = write_final_report_html(run_id)
    execute(
        "UPDATE outputs SET final_markdown=%s, updated_at=NOW() WHERE run_id=%s",
        (final_markdown, run_id),
    )
    execute(
        """
        UPDATE workflow_runs
        SET status='completed', current_phase='report.publish', completed_at=NOW(),
            report_markdown_path=%s, report_json_path=%s, notes = jsonb_set(COALESCE(notes, '{}'::jsonb), '{final_report_html}', to_jsonb(%s::text), true)
        WHERE run_id=%s
        """,
        (str(md_path), str(json_path), str(html_path), run_id),
    )
    return {
        "markdown_path": str(md_path),
        "json_path": str(json_path),
        "html_path": str(html_path),
        "message_body": f"终稿已发布，可直接查看 HTML 成品页、Markdown 和 JSON 结构化文件。HTML: /newsflow-output/{run_id}/final_report.html",
    }


def orchestrator_tick():
    runs = fetch_all(
        "SELECT run_id, status, discussion_seconds, current_phase, started_at FROM workflow_runs WHERE status='running' ORDER BY started_at"
    )
    for run_id, status, discussion_seconds, current_phase, started_at in runs:
        # initial collect complete?
        sections = ["政治经济", "科技", "体育娱乐", "其他"]
        for section, agent_id in [("政治经济", "33"), ("科技", "33"), ("体育娱乐", "xhs"), ("其他", "xhs")]:
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
                dispatch_task(run_id, latest_collect[0], "neko", "manager", section, "material.review", latest_collect[1], {}, get_run_trace_context(run_id))
                execute("UPDATE workflow_runs SET current_phase='material.review' WHERE run_id=%s", (run_id,))

        for section, agent_id in [("政治经济", "33"), ("科技", "33"), ("体育娱乐", "xhs"), ("其他", "xhs")]:
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
                    trace_ctx = get_run_trace_context(run_id)
                    collect_task_id = dispatch_task(run_id, None, agent_id, "collector", section, "material.collect", retry, {"target_count": 12, "rework_reason": latest_review[1]}, trace_ctx)
                    dispatch_task(run_id, collect_task_id, agent_id, "collector", section, "material.submit", retry, {}, trace_ctx)
                    execute("UPDATE workflow_runs SET current_phase='material.rework' WHERE run_id=%s", (run_id,))

        approved_count = 0
        for section in sections:
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
            dispatch_task(run_id, None, "neko", "manager", "全局", "draft.compose", 0, {}, get_run_trace_context(run_id))
            execute("UPDATE workflow_runs SET current_phase='draft.compose' WHERE run_id=%s", (run_id,))

        compose_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.compose' AND status='completed'", (run_id,))[0]
        discussion_start_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.start'", (run_id,))[0]
        if compose_done > 0 and discussion_start_exists == 0:
            trace_ctx = get_run_trace_context(run_id)
            dispatch_task(run_id, None, "neko", "manager", "全局", "discussion.start", 0, {"discussion_seconds": discussion_seconds}, trace_ctx)
            dispatch_task(run_id, None, "33", "collector", "全局", "discussion.comment", 0, {}, trace_ctx)
            dispatch_task(run_id, None, "xhs", "collector", "全局", "discussion.comment", 0, {}, trace_ctx)
            dispatch_task(run_id, None, "neko", "manager", "全局", "discussion.comment", 0, {}, trace_ctx)
            execute("UPDATE workflow_runs SET current_phase='discussion.start' WHERE run_id=%s", (run_id,))

        discussion_started = fetch_one(
            "SELECT started_at, payload::text FROM tasks WHERE run_id=%s AND phase='discussion.start' AND status='completed' ORDER BY started_at DESC LIMIT 1",
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
                dispatch_task(run_id, None, "neko", "manager", "全局", "discussion.summarize", 0, {}, get_run_trace_context(run_id))
                execute("UPDATE workflow_runs SET current_phase='discussion.summarize' WHERE run_id=%s", (run_id,))

        summarize_done = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='discussion.summarize' AND status='completed'", (run_id,))[0]
        revise_exists = fetch_one("SELECT COUNT(*) FROM tasks WHERE run_id=%s AND phase='draft.revise'", (run_id,))[0]
        if summarize_done > 0 and revise_exists == 0:
            trace_ctx = get_run_trace_context(run_id)
            dispatch_task(run_id, None, "neko", "manager", "全局", "draft.revise", 0, {}, trace_ctx)
            dispatch_task(run_id, None, "neko", "manager", "全局", "report.publish", 0, {}, trace_ctx)
            execute("UPDATE workflow_runs SET current_phase='draft.revise' WHERE run_id=%s", (run_id,))


def run_worker(agent_id: str):
    service_name = "orchestrator" if agent_id == "orchestrator" else f"agent-{agent_id}"
    while True:
        task = claim_task(agent_id)
        if not task:
            time.sleep(2)
            continue
        attrs = {
            "workflow_id": WORKFLOW_ID,
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
                    save_materials(task["run_id"], task["task_id"], task["section"], task["agent_id"], items)
                    span.set_attribute("status", "collected")
                    span.set_attribute("source_count", len(items))
                    complete_task(
                        task["task_id"],
                        {
                            "status": "collected",
                            "source_count": len(items),
                            "message_body": f"已完成【{task['section']}】板块采集，收集到 {len(items)} 条候选素材。",
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
                    complete_task(task["task_id"], {"status": "drafted", "draft_len": len(result["draft_markdown"])})
                elif task["phase"] == "discussion.start":
                    seconds = task["payload"].get("discussion_seconds", SETTINGS.discussion_test_seconds)
                    span.set_attribute("status", "started")
                    complete_task(
                        task["task_id"],
                        {
                            "discussion_seconds": seconds,
                            "status": "started",
                            "message_body": f"讨论阶段开始，当前测试轮讨论时长设为 {seconds} 秒。",
                        },
                    )
                elif task["phase"] == "discussion.comment":
                    comment = create_discussion_comment(task["run_id"], task["task_id"], task["agent_id"])
                    span.set_attribute("status", "commented")
                    complete_task(task["task_id"], {"status": "commented", "comment_text": comment})
                elif task["phase"] == "discussion.summarize":
                    plan = summarize_discussion(task["run_id"])
                    span.set_attribute("status", "summarized")
                    complete_task(task["task_id"], {"status": "summarized", "revision_plan": plan, "message_body": plan})
                elif task["phase"] == "draft.revise":
                    span.set_attribute("status", "revised")
                    complete_task(task["task_id"], {"status": "revised"})
                elif task["phase"] == "report.publish":
                    result = revise_and_publish(task["run_id"])
                    span.set_attribute("status", "published")
                    complete_task(task["task_id"], result | {"status": "published"})
                else:
                    raise RuntimeError(f"unknown phase {task['phase']}")
        except Exception as exc:
            fail_task(task["task_id"], str(exc))
            time.sleep(1)
