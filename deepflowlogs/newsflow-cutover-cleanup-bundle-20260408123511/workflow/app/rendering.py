from __future__ import annotations

import json
import re
from datetime import datetime
from html import escape
from pathlib import Path

from .config import ROOT, load_settings
from .db import fetch_all, fetch_one


SETTINGS = load_settings()
OUTPUT_ROOT = ROOT / "output"
DEFAULT_MANAGER_AGENT_ID = "neko"
PUBLIC_CONVERSATION_TASK_PHASES = {
    "cycle.start",
    "material.collect",
    "material.submit",
    "material.review.decision",
    "draft.compose",
    "draft.proofread",
    "proofread.decision.explanation",
    "draft.revise",
    "draft.recheck",
    "publish.decision",
    "report.publish",
    "product.test",
    "product.benchmark",
    "product.cross_cycle_compare",
    "pre-retro.review",
    "retrospective.plan",
    "retrospective.summary",
    "agent.optimization",
}
MANAGER_AGENT_ID = DEFAULT_MANAGER_AGENT_ID


def parse_trace_id(traceparent: str | None) -> str:
    if not traceparent:
        return ""
    parts = traceparent.split("-")
    if len(parts) >= 4:
        return parts[1]
    return ""


def get_run_meta(run_id: str) -> dict:
    row = fetch_one(
        """
        SELECT workflow_id, run_id, project_id, cycle_no, status, current_phase, started_at, completed_at, notes::text,
               report_markdown_path, report_json_path
        FROM workflow_runs WHERE run_id=%s
        """,
        (run_id,),
    )
    if not row:
        raise KeyError(run_id)
    notes = json.loads(row[8] or "{}")
    traceparent = notes.get("trace_context", {}).get("traceparent", "")
    return {
        "workflow_id": row[0],
        "run_id": row[1],
        "project_id": row[2],
        "cycle_no": row[3],
        "status": row[4],
        "current_phase": row[5],
        "started_at": row[6],
        "completed_at": row[7],
        "notes": notes,
        "trace_id": notes.get("trace_id") or parse_trace_id(traceparent),
        "report_markdown_path": row[9],
        "report_json_path": row[10],
    }


def _fmt_ts(value) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    return str(value)


def _load_json(text: str | None) -> dict:
    return json.loads(text) if text else {}


def _read_text(path_text: str | None) -> str:
    path = Path(str(path_text or "").strip())
    if not path_text or not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text()
    except Exception:
        return ""


def _dispatch_body(task: dict) -> str:
    # Control-layer dispatch prompts must never be rendered in frontstage views.
    return ""


def _task_body(task: dict) -> str:
    result = task["result"]
    if str(result.get("status") or "").strip().lower() == "obsolete":
        return ""
    if task["phase"] == "cycle.start":
        return ""
    for key in ("message_body", "reason", "summary", "decision_summary"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    if task["status"] == "failed":
        return str(task.get("error_message") or "").strip()
    return ""


def _task_fact_pairs(task: dict) -> list[tuple[str, str]]:
    result = task["result"]
    phase = task["phase"]
    if phase == "cycle.start":
        return []
    facts: list[tuple[str, str]] = []
    for key in {
        "cycle.start": ("status", "cycle_no", "active_rule_count"),
        "material.collect": ("status", "source_count", "target_count"),
        "material.submit": ("status",),
        "material.review.decision": ("status",),
        "draft.compose": ("status", "draft_len"),
        "draft.proofread": ("status", "issue_count", "accepted_count", "blocker_count_after_decision", "recheck_required"),
        "draft.recheck": ("status", "issue_count", "resolved_count", "remaining_count", "blocker_count"),
        "publish.decision": ("status", "approved"),
        "report.publish": ("status",),
        "product.test": ("status",),
        "product.benchmark": ("status",),
        "product.cross_cycle_compare": ("status",),
        "pre-retro.review": ("status", "approved"),
        "retrospective.plan": ("status",),
        "retrospective.summary": ("status",),
        "agent.optimization": ("status",),
    }.get(phase, ()):
        value = result.get(key)
        if value in (None, "", [], {}):
            continue
        facts.append((key, str(value)))
    return facts


def _task_target(task: dict, manager_agent_id: str, section_owners: dict[str, str]) -> str:
    phase = task["phase"]
    if phase == "cycle.start":
        return "all"
    if phase == "material.collect":
        return manager_agent_id
    if phase == "material.submit":
        return "tester"
    if phase == "material.review.decision":
        return section_owners.get(task["section"], "all")
    if phase == "draft.review.comment":
        return manager_agent_id
    return "all"


def _task_artifact_url(task: dict) -> str:
    result = task["result"]
    phase = task["phase"]
    if phase == "cycle.start":
        files = result.get("cycle_task_plan_files") or {}
        return str(files.get("html_path") or "").strip()
    if phase in {"material.collect", "material.submit"}:
        return str(result.get("detail_url") or f"/newsflow/runs/{task['run_id']}/materials.html").strip()
    if phase == "material.review.decision":
        return f"/newsflow/runs/{task['run_id']}/material-review.html"
    if phase in {"draft.compose", "proofread.decision.explanation", "draft.revise", "report.publish", "product.test", "product.benchmark", "product.cross_cycle_compare", "retrospective.plan", "retrospective.summary", "agent.optimization"}:
        return str(result.get("html_path") or "").strip()
    if phase in {"draft.proofread", "draft.recheck"}:
        return str(result.get("detail_url") or result.get("html_path") or "").strip()
    if phase == "pre-retro.review":
        return f"/newsflow/runs/{task['run_id']}/product.html"
    return ""


def _cycle_start_public_entry(task: dict) -> dict:
    return {
        "time": task["finished_at"] or task["created_at"],
        "speaker": "system",
        "target": "all",
        "phase": task["phase"],
        "section": task["section"],
        "kind": "message" if task["status"] == "completed" else "reject",
        "body": "",
        "facts": [],
        "artifact_url": _task_artifact_url(task),
    }


def build_conversation_entries(run_id: str) -> list[dict]:
    task_rows = fetch_all(
        """
        SELECT task_id, parent_task_id, agent_id, agent_role, section, phase, retry_count,
               status, payload::text, result::text, error_message, created_at, finished_at
        FROM tasks WHERE run_id=%s ORDER BY created_at
        """,
        (run_id,),
    )
    entries: list[dict] = []
    tasks: list[dict] = []
    manager_agent_id = DEFAULT_MANAGER_AGENT_ID
    section_owners: dict[str, str] = {}
    for row in task_rows:
        task = {
            "task_id": row[0],
            "parent_task_id": row[1],
            "agent_id": row[2],
            "agent_role": row[3],
            "section": row[4],
            "phase": row[5],
            "retry_count": row[6],
            "status": row[7],
            "payload": _load_json(row[8]),
            "result": _load_json(row[9]),
            "error_message": row[10],
            "created_at": row[11],
            "finished_at": row[12],
            "run_id": run_id,
        }
        tasks.append(task)
        if task["phase"] == "cycle.start" and task["agent_id"]:
            manager_agent_id = task["agent_id"]
        if task["phase"] == "material.collect" and task["section"] and task["section"] != "全局" and task["agent_id"]:
            section_owners[task["section"]] = task["agent_id"]

    for task in tasks:
        if task["status"] not in {"completed", "failed"}:
            continue
        if task["phase"] not in PUBLIC_CONVERSATION_TASK_PHASES:
            continue
        if task["phase"] == "cycle.start":
            entry = _cycle_start_public_entry(task)
            if entry["body"] or entry["facts"] or entry["artifact_url"]:
                entries.append(entry)
            continue
        body = _task_body(task)
        facts = _task_fact_pairs(task)
        artifact_url = _task_artifact_url(task)
        if not body and not facts and not artifact_url:
            continue
        entries.append(
            {
                "time": task["finished_at"] or task["created_at"],
                "speaker": task["agent_id"],
                "target": _task_target(task, manager_agent_id, section_owners),
                "phase": task["phase"],
                "section": task["section"],
                "kind": "reject" if task["status"] == "failed" else "message",
                "body": body,
                "facts": facts,
                "artifact_url": artifact_url,
            }
        )

    review_rows = fetch_all(
        """
        SELECT r.created_at, r.section, r.approved, r.reason, t.parent_task_id
        FROM reviews r
        JOIN tasks t ON t.task_id = r.review_task_id
        WHERE r.run_id=%s
        ORDER BY r.created_at
        """,
        (run_id,),
    )
    for created_at, section, approved, reason, parent_task_id in review_rows:
        target_row = fetch_one("SELECT agent_id FROM tasks WHERE task_id=%s", (parent_task_id,)) if parent_task_id else None
        entries.append(
            {
                "time": created_at,
                "speaker": "tester",
                "target": target_row[0] if target_row else "collector",
                "phase": "material.review",
                "section": section,
                "kind": "review" if approved else "reject",
                "body": reason,
                "facts": [],
                "artifact_url": "",
            }
        )

    draft_review_rows = fetch_all(
        """
        SELECT created_at, agent_id, section_scope, review_text
        FROM draft_reviews WHERE run_id=%s ORDER BY created_at
        """,
        (run_id,),
    )
    for created_at, agent_id, section_scope, review_text in draft_review_rows:
        entries.append(
            {
                "time": created_at,
                "speaker": agent_id,
                "target": manager_agent_id,
                "phase": "draft.review.comment",
                "section": section_scope,
                "kind": "discussion",
                "body": review_text,
                "facts": [],
                "artifact_url": "",
            }
        )

    retrospective_rows = fetch_all(
        """
        SELECT created_at, COALESCE(from_agent, agent_id), COALESCE(to_agent, 'all'),
               COALESCE(topic, '问题'), COALESCE(intent, 'comment'), round_no,
               message_id, reply_to_message_id, COALESCE(body, comment_text)
        FROM retrospectives WHERE run_id=%s ORDER BY created_at
        """,
        (run_id,),
    )
    for created_at, agent_id, to_agent, topic, intent, round_no, message_id, reply_to_message_id, comment_text in retrospective_rows:
        entries.append(
            {
                "time": created_at,
                "speaker": agent_id,
                "target": to_agent,
                "phase": "retrospective.discussion",
                "section": f"全局 | {topic}",
                "kind": "discussion",
                "body": comment_text,
                "facts": [],
                "artifact_url": "",
            }
        )

    entries.sort(key=lambda item: item["time"] or datetime.min, reverse=True)
    return entries


def _render_feed(entries: list[dict], title: str) -> str:
    chunks = []
    for entry in entries:
        speaker = escape(entry["speaker"])
        target = escape(entry["target"])
        phase = escape(entry["phase"])
        section = escape(entry["section"])
        body = _render_body_html(entry["body"])
        facts = entry.get("facts") or []
        kind = escape(entry["kind"])
        artifact_url = escape(entry.get("artifact_url") or "")
        artifact_link = (
            f'<a class="artifact-link" href="{artifact_url}" target="_blank">查看产物</a>'
            if artifact_url
            else ""
        )
        facts_html = (
            "<div class=\"facts\">"
            + "".join(
                f'<span class="fact"><strong>{escape(label)}:</strong> {escape(value)}</span>'
                for label, value in facts
            )
            + "</div>"
            if facts
            else ""
        )
        chunks.append(
            f"""
            <article class="msg {kind}">
              <div class="meta">
                <span class="speaker">{speaker}</span>
                <span class="arrow">→</span>
                <span class="target">{target}</span>
                <span class="phase">{phase}</span>
                <span class="section">{section}</span>
                <time>{escape(_fmt_ts(entry["time"]))}</time>
                {artifact_link}
              </div>
              {facts_html}
              <div class="body">{body}</div>
            </article>
            """
        )
    return _page(
        title,
        """
        <section class="feed">
        """
        + "".join(chunks)
        + """
        </section>
        """,
    )


def _render_body_html(body_text: str) -> str:
    text = escape(body_text or "").replace("\n", "<br>")
    return re.sub(r"(https?://[^\s<]+)", r'<a href="\1" target="_blank">\1</a>', text)


def render_conversation_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    entries = build_conversation_entries(run_id)
    return _render_feed(entries, f"Run {meta['run_id']} Conversation")


def render_review_thread_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    review_rows = fetch_all(
        """
        SELECT r.created_at, r.section, r.approved, r.reason
        FROM reviews r
        WHERE r.run_id=%s
        ORDER BY r.created_at
        """,
        (run_id,),
    )
    output_row = fetch_one("SELECT revision_plan FROM outputs WHERE run_id=%s", (run_id,))
    cards = []
    for created_at, section, approved, reason in review_rows:
        status = "通过" if approved else "打回"
        css = "approved" if approved else "rejected"
        cards.append(
            f"""
            <article class="review {css}">
              <header><strong>{escape(section)}</strong><span>{status}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body">{escape(reason).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    revision_plan = escape((output_row[0] or "暂无修订方案")).replace("\n", "<br>") if output_row else "暂无修订方案"
    body = (
        f"<section><h2>审稿与打回记录</h2>{''.join(cards)}</section>"
        f"<section class='revision'><h2>Revision Plan</h2><div class='body'>{revision_plan}</div></section>"
    )
    return _page(f"Run {meta['run_id']} Review Thread", body)


def render_draft_review_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT opened_at, reported_by, section, severity, issue_type, description, status
        FROM proofread_issues
        WHERE run_id=%s
        ORDER BY opened_at
        """,
        (run_id,),
    )
    notes = meta["notes"] or {}
    decision_md = _read_text(notes.get("proofread_decision_explanation_md"))
    decision_html = str(notes.get("proofread_decision_explanation_html") or notes.get("proofread_decision_html") or "").strip()
    cards = []
    for created_at, agent_id, section_scope, severity, issue_type, review_text, status in rows:
        cards.append(
            f"""
            <article class="review">
              <header><strong>{escape(agent_id)}</strong><span>{escape(section_scope)}</span><span>{escape(severity)}</span><span>{escape(issue_type)}</span><span>{escape(status)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body">{escape(review_text).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    return _page(
        f"Run {meta['run_id']} Draft Review",
        f"<section><h2>Proofread Issues</h2>{''.join(cards) or '<p>暂无。</p>'}</section>"
        + (
            f"<section class='revision'><h2>Proofread Decision</h2><div class='body'>{_render_body_html(decision_md)}</div>"
            + (f"<p><a href=\"{escape(decision_html)}\" target=\"_blank\">查看完整 explanation 页面</a></p>" if decision_html else "")
            + "</section>"
            if decision_md or decision_html
            else "<section class='revision'><h2>Proofread Decision</h2><div class='body'>暂无 proofread explanation 产物。</div></section>"
        ),
    )


def render_materials_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT section, id, task_id, title, source_media, published_at, link, jsonb_array_length(images),
               COALESCE(summary_zh, ''), COALESCE(brief_zh, ''), COALESCE(metadata->>'relevance_note', ''), created_at
        FROM materials
        WHERE run_id=%s
        ORDER BY section, published_at DESC, id DESC
        """,
        (run_id,),
    )
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row[0], []).append(row)
    sections = []
    for section, items in groups.items():
        cards = []
        for _, material_id, task_id, title, source_media, published_at, link, image_count, summary_zh, brief_zh, relevance_note, created_at in items:
            summary_html = f"<p>{escape(summary_zh).replace(chr(10), '<br>')}</p>" if summary_zh else ""
            brief_html = f"<p class='meta-line'>{escape(brief_zh).replace(chr(10), '<br>')}</p>" if brief_zh else ""
            note_html = f"<p class='meta-line'>{escape(relevance_note).replace(chr(10), '<br>')}</p>" if relevance_note else ""
            link_html = f"<p class='meta-line'><a href=\"{escape(link)}\" target=\"_blank\">{escape(link)}</a></p>" if link else ""
            cards.append(
                f"""
                <article class="review">
                  <header><strong>{escape(title)}</strong><span>{escape(source_media)}</span><span>images={image_count}</span><time>{escape(_fmt_ts(published_at))}</time></header>
                  <div class="body">{summary_html}{brief_html}{note_html}{link_html}</div>
                </article>
                """
            )
        sections.append(f"<section id='section-{escape(section)}'><h2>{escape(section)}（{len(items)}）</h2>{''.join(cards)}</section>")
    return _page(f"Run {meta['run_id']} Materials", "".join(sections) or "<p>暂无素材。</p>")


def render_material_review_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT i.section, i.review_task_id, i.material_id, m.title, m.source_media, m.link,
               COALESCE(m.summary_zh, ''), COALESCE(m.metadata->>'relevance_note', ''), i.verdict, i.reason, i.created_at
        FROM material_review_items i
        JOIN materials m ON m.id=i.material_id
        WHERE i.run_id=%s
        ORDER BY i.section, i.review_task_id, i.created_at
        """,
        (run_id,),
    )
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row[0], []).append(row)
    sections = []
    for section, items in groups.items():
        cards = []
        for _, review_task_id, material_id, title, source_media, link, summary_zh, relevance_note, verdict, reason, created_at in items:
            css = "approved" if verdict == "approved" else "rejected"
            summary_html = f"<p>{escape(summary_zh).replace(chr(10), '<br>')}</p>" if summary_zh else ""
            note_html = f"<p class='meta-line'>{escape(relevance_note).replace(chr(10), '<br>')}</p>" if relevance_note else ""
            reason_html = f"<p>{escape(reason).replace(chr(10), '<br>')}</p>" if reason else ""
            link_html = f"<p class='meta-line'><a href=\"{escape(link)}\" target=\"_blank\">{escape(link)}</a></p>" if link else ""
            cards.append(
                f"""
                <article class="review {css}">
                  <header><strong>{escape(title)}</strong><span>{escape(verdict)}</span><span>{escape(source_media)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
                  <div class="body">{summary_html}{note_html}{reason_html}{link_html}</div>
                </article>
                """
            )
        sections.append(f"<section id='section-{escape(section)}'><h2>{escape(section)} 逐条审核</h2>{''.join(cards)}</section>")
    return _page(f"Run {meta['run_id']} Material Review", "".join(sections) or "<p>暂无审核结果。</p>")


def render_proofread_detail_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT i.issue_id, i.opened_at, i.reported_by, i.section, i.severity, i.issue_type, i.description,
               i.item_ref, i.status, COALESCE(d.decision_type,''), COALESCE(d.rationale,'')
        FROM proofread_issues i
        LEFT JOIN LATERAL (
          SELECT decision_type, rationale
          FROM proofread_decisions d
          WHERE d.run_id=i.run_id AND d.issue_id=i.issue_id
          ORDER BY created_at DESC
          LIMIT 1
        ) d ON TRUE
        WHERE i.run_id=%s
        ORDER BY i.opened_at
        """,
        (run_id,),
    )
    cards = []
    for issue_id, opened_at, reported_by, section, severity, issue_type, description, item_ref, status, decision_type, rationale in rows:
        meta_bits = [reported_by, item_ref, issue_type, decision_type]
        meta_line = " · ".join(escape(bit) for bit in meta_bits if bit)
        description_html = f"<p>{escape(description).replace(chr(10), '<br>')}</p>" if description else ""
        rationale_html = f"<p class='meta-line'>{escape(rationale).replace(chr(10), '<br>')}</p>" if rationale else ""
        cards.append(
            f"""
            <article class="review">
              <header><strong>{escape(issue_id)}</strong><span>{escape(section)}</span><span>{escape(severity)}</span><span>{escape(status)}</span><time>{escape(_fmt_ts(opened_at))}</time></header>
              <div class="body">{f'<p class=\"meta-line\">{meta_line}</p>' if meta_line else ''}{description_html}{rationale_html}</div>
            </article>
            """
        )
    return _page(f"Run {meta['run_id']} Proofread Detail", "".join(cards) or "<p>暂无 proofread issue。</p>")


def render_recheck_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT issue_id, section, severity, issue_type, status, resolution_note, updated_at, closed_at
        FROM proofread_issues
        WHERE run_id=%s
        ORDER BY updated_at DESC
        """,
        (run_id,),
    )
    cards = []
    for issue_id, section, severity, issue_type, status, resolution_note, updated_at, closed_at in rows:
        meta_bits = [severity, issue_type, _fmt_ts(closed_at)]
        meta_line = " · ".join(escape(bit) for bit in meta_bits if bit)
        resolution_html = f"<p>{escape(resolution_note or '').replace(chr(10), '<br>')}</p>" if resolution_note else ""
        cards.append(
            f"""
            <article class="review">
              <header><strong>{escape(issue_id)}</strong><span>{escape(section)}</span><span>{escape(issue_type)}</span><span>{escape(status)}</span><time>{escape(_fmt_ts(updated_at))}</time></header>
              <div class="body">{f'<p class="meta-line">{meta_line}</p>' if meta_line else ''}{resolution_html}</div>
            </article>
            """
        )
    return _page(f"Run {meta['run_id']} Recheck Detail", "".join(cards) or "<p>暂无 recheck 结果。</p>")


def render_final_report_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    output_row = fetch_one("SELECT final_markdown FROM outputs WHERE run_id=%s", (run_id,))
    if not output_row or not output_row[0]:
        return _page(f"Run {meta['run_id']} Final Report", "<p>终稿尚未生成。</p>")
    final_markdown = output_row[0] or ""
    return _page(
        f"Run {meta['run_id']} Final Report",
        f"""
        <section class="revision">
          <h1>Published Final Artifact</h1>
          <div class="body">{_render_body_html(final_markdown)}</div>
        </section>
        """,
    )


def render_retrospective_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    comments = fetch_all(
        """
        SELECT topic_id, message_id, reply_to_message_id, COALESCE(from_agent, agent_id), COALESCE(to_agent, 'all'),
               COALESCE(target_type, 'team'), COALESCE(topic, '问题'), COALESCE(intent, 'comment'),
               round_no, COALESCE(body, comment_text), created_at
        FROM retrospectives
        WHERE run_id=%s
        ORDER BY created_at
        """,
        (run_id,),
    )
    summary_row = fetch_one(
        """
        SELECT retrospective_summary
        FROM project_cycles
        WHERE project_id=%s AND cycle_no=%s
        """,
        (meta["project_id"], meta["cycle_no"]),
    )
    optimization_rows = fetch_all(
        """
        SELECT agent_id, summary_text, optimization_json::text
        FROM agent_optimizations
        WHERE run_id=%s
        ORDER BY agent_id
        """,
        (run_id,),
    )
    guidance_rows = fetch_all(
        """
        SELECT agent_id, source_type, author, category, effective_from_cycle, expires_after_cycle, body, details::text, created_at
        FROM optimization_logs
        WHERE project_id=%s AND effective_from_cycle <= %s + 1 AND (expires_after_cycle IS NULL OR expires_after_cycle >= %s + 1)
        ORDER BY created_at
        """,
        (meta["project_id"], meta["cycle_no"], meta["cycle_no"]),
    )
    topic_rows = fetch_all(
        """
        SELECT topic_id, title, status, opened_by, opened_at, closed_at, evidence_refs::text
        FROM retro_topics
        WHERE run_id=%s
        ORDER BY opened_at
        """,
        (run_id,),
    )
    decision_rows = fetch_all(
        """
        SELECT topic_id, summary, owner_agent, decision_json::text, created_at
        FROM retro_decisions
        WHERE run_id=%s
        ORDER BY created_at
        """,
        (run_id,),
    )
    topic_by_id = {row[0]: row for row in topic_rows}
    visible_topic_ids = {topic_id for topic_id, *_ in comments if topic_id} | {topic_id for topic_id, *_ in decision_rows if topic_id}
    blocks = ["<section><h2>复盘讨论线程</h2>"]
    for topic_id, message_id, reply_to_message_id, agent_id, to_agent, target_type, topic, intent, round_no, comment_text, created_at in comments:
        blocks.append(
            f"""
            <article class="review">
              <header><strong>{escape(agent_id)}</strong><span>round {round_no}</span><span>{escape(topic)}</span><span>{escape(intent)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body">{escape(comment_text).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    blocks.append("</section>")
    decision_cards = []
    for topic_id, summary_text, owner_agent, decision_text, created_at in decision_rows:
        title = topic_by_id.get(topic_id, [None, topic_id])[1] or topic_id
        decision_cards.append(
            f"""
            <article class="review approved">
              <header><strong>{escape(title)}</strong><span>{escape(owner_agent)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body">{escape(summary_text).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    blocks.append(f"<section><h2>Retro Decisions</h2>{''.join(decision_cards) or '<p>暂无。</p>'}</section>")
    summary = summary_row[0] if summary_row and summary_row[0] else ""
    if summary:
        blocks.append(
            f"<section class='revision'><h2>{escape(MANAGER_AGENT_ID)} 收敛总结</h2><div class='body'>{escape(summary).replace(chr(10), '<br>')}</div></section>"
        )
    opt_cards = []
    for agent_id, summary_text, optimization_json in optimization_rows:
        data = _load_json(optimization_json)
        visible_body = data.get("optimization_markdown") or summary_text or ""
        opt_cards.append(
            f"""
            <article class="review approved">
              <header><strong>{escape(agent_id)}</strong><span>agent.self_optimize</span></header>
              <div class="body">{_render_body_html(visible_body) if visible_body else ''}</div>
            </article>
            """
        )
    blocks.append(f"<section><h2>Agent 自我优化结果</h2>{''.join(opt_cards)}</section>")
    log_cards = []
    for agent_id, source_type, author, category, effective_from_cycle, expires_after_cycle, body_text, details_text, created_at in guidance_rows:
        log_cards.append(
            f"""
            <article class="review">
              <header><strong>{escape(agent_id or 'project')}</strong><span>{escape(category)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body">{escape(body_text).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    blocks.append(f"<section><h2>下一轮优化日志</h2>{''.join(log_cards) or '<p>暂无。</p>'}</section>")
    return _page(f"Run {meta['run_id']} Retrospective", "".join(blocks))


def render_product_report_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT agent_id, report_type, title, summary_text, report_json::text, created_at
        FROM product_reports
        WHERE run_id=%s
        ORDER BY created_at, id
        """,
        (run_id,),
    )
    if not rows:
        return _page(f"Run {meta['run_id']} Product Reports", "<p>尚未生成产品测试/对标/总报告。</p>")
    sections = ["<section><h2>产品测试与评估</h2>"]
    for agent_id, report_type, title, summary_text, report_json, created_at in rows:
        data = _load_json(report_json)
        visible_body = (
            data.get("report_markdown")
            or data.get("plan_markdown")
            or data.get("summary_markdown")
            or data.get("explanation_markdown")
            or ""
        )
        body = _render_body_html(visible_body) if visible_body else ""
        sections.append(
            f"""
            <article class="review approved">
              <header><strong>{escape(title)}</strong><span>{escape(report_type)}</span><span>{escape(agent_id or MANAGER_AGENT_ID)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body"><p>{escape(summary_text)}</p>{body}</div>
            </article>
            """
        )
    sections.append("</section>")
    return _page(f"Run {meta['run_id']} Product Reports", "".join(sections))


def render_single_product_report_html(run_id: str, report_type: str) -> str:
    meta = get_run_meta(run_id)
    rows = fetch_all(
        """
        SELECT agent_id, report_type, title, summary_text, report_json::text, created_at
        FROM product_reports
        WHERE run_id=%s AND report_type=%s
        ORDER BY created_at, id
        """,
        (run_id, report_type),
    )
    if not rows:
        return _page(f"Run {meta['run_id']} {report_type}", "<p>暂无报告。</p>")
    blocks = []
    for agent_id, _, title, summary_text, report_json, created_at in rows:
        data = _load_json(report_json)
        visible_body = (
            data.get("report_markdown")
            or data.get("plan_markdown")
            or data.get("summary_markdown")
            or data.get("explanation_markdown")
            or ""
        )
        blocks.append(
            f"""
            <section class="revision">
              <h2>{escape(title)}</h2>
              <p class="meta-line">{escape(agent_id or MANAGER_AGENT_ID)} | {_fmt_ts(created_at)}</p>
              <div class="body"><p>{escape(summary_text)}</p>{_render_body_html(visible_body) if visible_body else ''}</div>
            </section>
            """
        )
    return _page(f"Run {meta['run_id']} {report_type}", "".join(blocks))


def render_project_overview_html(project_id: str) -> str:
    project = fetch_one(
        """
        SELECT project_id, status, current_cycle_no, max_cycles, latest_run_id, next_cycle_at, paused_reason, notes::text
        FROM projects WHERE project_id=%s
        """,
        (project_id,),
    )
    if not project:
        raise KeyError(project_id)
    cycles = fetch_all(
        """
        SELECT cycle_no, run_id, status, started_at, completed_at, retrospective_summary
        FROM project_cycles
        WHERE project_id=%s
        ORDER BY cycle_no DESC
        """,
        (project_id,),
    )
    optimization_rows = fetch_all(
        """
        SELECT cycle_no, agent_id, summary_text
        FROM agent_optimizations
        WHERE project_id=%s
        ORDER BY cycle_no DESC, agent_id
        """,
        (project_id,),
    )
    grouped_opt: dict[int, list[str]] = {}
    for cycle_no, agent_id, summary_text in optimization_rows:
        grouped_opt.setdefault(cycle_no, []).append(f"<li><strong>{escape(agent_id)}</strong>: {escape(summary_text)}</li>")
    notes = _load_json(project[7])
    body = [
        f"""
        <section class="revision">
          <h2>Project Overview</h2>
          <div class="body">
            <p>project_id: <code>{escape(project[0])}</code></p>
            <p>status: {escape(project[1])}</p>
            <p>current_cycle_no: {escape(str(project[2]))} / max_cycles: {escape(str(project[3]))}</p>
            <p>latest_run_id: <code>{escape(project[4] or '')}</code></p>
            <p>next_cycle_at: {escape(_fmt_ts(project[5]))}</p>
            <p>paused_reason: {escape(project[6] or '')}</p>
            <p>defaults: {escape(json.dumps(notes.get('defaults', {}), ensure_ascii=False))}</p>
            <p>test_values: {escape(json.dumps(notes.get('test_values', {}), ensure_ascii=False))}</p>
          </div>
        </section>
        """
    ]
    for cycle_no, run_id, status, started_at, completed_at, retrospective_summary in cycles:
        body.append(
            f"""
            <article class="review">
              <header><strong>Cycle {cycle_no}</strong><span>{escape(status)}</span><time>{escape(_fmt_ts(started_at))}</time></header>
              <div class="body">
                <p>run_id: <code>{escape(run_id or '')}</code></p>
                <p>completed_at: {escape(_fmt_ts(completed_at))}</p>
                <p>retrospective: {escape((retrospective_summary or '暂无').splitlines()[0])}</p>
                <p><a href="/newsflow/runs/{escape(run_id or '')}/conversation.html" target="_blank">Conversation</a> |
                   <a href="/newsflow/runs/{escape(run_id or '')}/review-thread.html" target="_blank">Review</a> |
                   <a href="/newsflow/runs/{escape(run_id or '')}/product.html" target="_blank">Product</a> |
                   <a href="/newsflow/runs/{escape(run_id or '')}/retrospective.html" target="_blank">Retrospective</a> |
                   <a href="/newsflow/runs/{escape(run_id or '')}/final.html" target="_blank">Final</a></p>
                <h4>本轮优化</h4>
                <ul>{''.join(grouped_opt.get(cycle_no, ['<li>暂无</li>']))}</ul>
              </div>
            </article>
            """
        )
    return _page(f"Project {project_id} Overview", "".join(body))


def write_final_report_html(run_id: str) -> str:
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "final_report.html"
    path.write_text(render_final_report_html(run_id))
    return str(path)


def write_product_report_html(run_id: str, title: str, markdown_body: str, payload: dict, stem: str) -> str:
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / f"{stem}.html"
    path.write_text(
        _page(
            title,
            f"<section class='revision'><h1>{escape(title)}</h1><div class='body'>{_render_body_html(markdown_body)}</div></section>",
        )
    )
    return str(path)


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="zh-Hans">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(title)}</title>
  <style>
    :root {{
      --bg: #f6f4ee;
      --card: #ffffff;
      --ink: #16202a;
      --muted: #5d6975;
      --line: #d9d4c8;
      --accent: #1f6f5f;
      --warm: #9a5a32;
      --danger: #a33e35;
    }}
    body {{ margin: 0; padding: 20px; font: 15px/1.6 "Noto Sans CJK SC","PingFang SC","Microsoft YaHei",sans-serif; color: var(--ink); background: linear-gradient(180deg, #f6f4ee 0%, #ebe5d9 100%); }}
    h1,h2,h3,h4,p {{ margin: 0 0 10px; }}
    a {{ color: #0d5f9b; text-decoration: none; }}
    .feed, .news-section, .revision, .review {{ display: block; }}
    .msg, .review, .main-card, .secondary-card, .revision {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 14px 16px; margin: 0 0 12px; box-shadow: 0 10px 24px rgba(25,35,45,.06); }}
    .msg.dispatch {{ border-left: 6px solid var(--accent); }}
    .msg.message {{ border-left: 6px solid #3f6db3; }}
    .msg.review {{ border-left: 6px solid var(--warm); }}
    .msg.reject {{ border-left: 6px solid var(--danger); }}
    .msg.discussion {{ border-left: 6px solid #7a4ea3; }}
    .meta {{ color: var(--muted); font-size: 13px; display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }}
    .facts {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 8px; }}
    .fact {{ color: var(--muted); font-size: 13px; background: #f4f0e6; border: 1px solid var(--line); border-radius: 999px; padding: 2px 8px; }}
    .speaker {{ font-weight: 700; color: var(--ink); }}
    .phase, .section {{ background: #eef3f7; border-radius: 999px; padding: 2px 8px; }}
    .body {{ white-space: normal; }}
    .report-header {{ margin-bottom: 20px; }}
    .main-card {{ display: grid; grid-template-columns: 1.1fr 1fr; gap: 18px; }}
    .main-images {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }}
    .main-images img, .secondary-card img, .img-placeholder {{ width: 100%; height: 180px; object-fit: cover; border-radius: 10px; background: #ddd; }}
    .img-placeholder {{ display: flex; align-items: center; justify-content: center; color: #69707a; font-size: 13px; border: 1px dashed #b8bfc7; }}
    .secondary-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 14px 0; }}
    .briefs ul {{ padding-left: 18px; }}
    .briefs li {{ margin-bottom: 10px; }}
    .meta-line {{ color: var(--muted); font-size: 13px; }}
    .review.approved header span {{ color: var(--accent); font-weight: 700; }}
    .review.rejected header span {{ color: var(--danger); font-weight: 700; }}
    .review header {{ display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }}
    @media (max-width: 960px) {{
      body {{ padding: 12px; }}
      .main-card {{ grid-template-columns: 1fr; }}
      .main-images {{ grid-template-columns: 1fr; }}
      .secondary-grid {{ grid-template-columns: 1fr; }}
    }}
  </style>
</head>
<body>{body}</body>
</html>"""
