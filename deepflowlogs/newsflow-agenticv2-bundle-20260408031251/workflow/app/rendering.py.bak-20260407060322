from __future__ import annotations

import json
from datetime import datetime
from html import escape
from pathlib import Path

from .config import ROOT, load_settings
from .db import fetch_all, fetch_one


SETTINGS = load_settings()
OUTPUT_ROOT = ROOT / "output"


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


def _dispatch_body(task: dict) -> str:
    payload = task["payload"]
    message = payload.get("message_body")
    if message:
        return message
    phase = task["phase"]
    section = task["section"]
    retry = task["retry_count"]
    if phase == "material.collect":
        if retry > 0:
            return (
                f"请重做【{section}】板块素材采集。要求至少 10 条候选、至少 3 条带图，"
                f"并重点修复上轮问题：{payload.get('rework_reason', '请补强热点性和图片质量')}。"
            )
        return (
            f"请采集【{section}】板块近24小时国际新闻热点候选素材。"
            f"目标 {payload.get('target_count', 12)} 条，优先主流媒体或官方来源，保留标题、来源、发布时间、原文链接和图片。"
        )
    if phase == "material.review":
        return f"请审核【{section}】板块候选素材，核对时效、来源可靠性、图片数量和热点性，并给出通过或打回意见。"
    if phase == "draft.compose":
        return "请把四个板块的已通过素材整合为初稿，按主推 / 副推 / 简讯结构排版。"
    if phase == "draft.review.start":
        return f"进入校稿阶段。请在 {payload.get('discussion_seconds', SETTINGS.discussion_test_seconds)} 秒内围绕初稿逐项校对。"
    if phase == "draft.review.comment":
        return "请按自己负责板块校对初稿，指出事实、标题、来源、链接、图片和主副推归位问题。"
    if phase == "draft.review.summarize":
        return "请汇总 worker 校稿意见，明确采纳项、修订重点和本轮修稿方案。"
    if phase == "draft.revise":
        return "请根据修订方案修改初稿。"
    if phase == "report.publish":
        return "请输出最终成稿并发布产物文件。"
    if phase == "product.test":
        return "请从产品/读者/编辑视角评估本轮成品，指出最明显问题、阅读体验损伤点和下一轮优先改法。"
    if phase == "product.benchmark":
        return "请联网查找相近新闻整理产品，提炼最明显差距并转成少量下一轮可执行建议。"
    if phase == "product.report":
        return "请汇总三份产品测试报告和外部对标报告，形成本轮产品评估总报告。"
    if phase == "retrospective.start":
        return "进入复盘阶段。由 neko 主持，围绕问题、堵点、技能缺失、作品优化与协作优化展开多轮讨论。"
    if phase == "retrospective.comment":
        round_no = payload.get("round_no", 1)
        topic = payload.get("topic", "问题")
        target = payload.get("to_agent", "all")
        return f"复盘回合 {round_no}，请围绕“{topic}”回应 {target}，基于本轮真实执行展开追问、反驳、补充或建议。"
    if phase == "agent.self_optimize":
        return "请基于真实复盘线程更新自己的策略、质量检查和角色改进计划。"
    return f"请执行阶段 {phase}。"


def _task_body(task: dict) -> str:
    result = task["result"]
    if task["phase"] == "material.submit":
        titles = fetch_all(
            """
            SELECT title, source_media
            FROM materials
            WHERE run_id=%s AND task_id=%s
            ORDER BY published_at DESC
            LIMIT 5
            """,
            (task["run_id"], task["parent_task_id"] or task["task_id"]),
        )
        lines = [f"已提交 {result.get('source_count', 0)} 条候选素材。"]
        for title, source_media in titles:
            lines.append(f"- {title} | {source_media}")
        return "\n".join(lines)
    if task["phase"] == "draft.compose":
        summary = result.get("message_body", "初稿已生成。")
        html_path = result.get("html_path")
        if html_path:
            return f"{summary}\n初稿入口：{html_path}"
        return summary
    if task["phase"] == "draft.review.summarize":
        summary = result.get("message_body", "已完成校稿收敛总结。")
        html_path = result.get("html_path")
        if html_path:
            return f"{summary}\n校稿总结入口：{html_path}"
        return summary
    if task["phase"] == "discussion.summarize":
        summary = result.get("revision_plan", "已完成讨论收敛总结。")
        html_path = result.get("html_path")
        if html_path:
            return f"{summary}\n报告入口：{html_path}"
        return summary
    if task["phase"] == "draft.revise":
        summary = result.get("message_body", "已完成本轮修订稿。")
        html_path = result.get("html_path")
        if html_path:
            return f"{summary}\n修订稿入口：{html_path}"
        return summary
    if task["phase"] == "report.publish":
        summary = result.get("message_body", "终稿已发布。")
        html_path = result.get("html_path")
        if html_path:
            return f"{summary}\n成品入口：{html_path}"
        return summary
    if task["phase"] in {"product.test", "product.benchmark", "product.report"}:
        summary = result.get("message_body", "已生成产品报告。")
        html_path = result.get("html_path")
        if html_path:
            return f"{summary}\n报告入口：{html_path}"
        return summary
    if task["phase"] == "retrospective.summary":
        return result.get("message_body", "已完成复盘总结。")
    if task["phase"] == "agent.self_optimize":
        return result.get("message_body", "已完成自我优化。")
    if result.get("message_body"):
        return result["message_body"]
    return ""


def build_conversation_entries(run_id: str) -> list[dict]:
    task_rows = fetch_all(
        """
        SELECT task_id, parent_task_id, agent_id, agent_role, section, phase, retry_count,
               payload::text, result::text, created_at, finished_at
        FROM tasks WHERE run_id=%s ORDER BY created_at
        """,
        (run_id,),
    )
    entries: list[dict] = []
    for row in task_rows:
        task = {
            "task_id": row[0],
            "parent_task_id": row[1],
            "agent_id": row[2],
            "agent_role": row[3],
            "section": row[4],
            "phase": row[5],
            "retry_count": row[6],
            "payload": _load_json(row[7]),
            "result": _load_json(row[8]),
            "created_at": row[9],
            "finished_at": row[10],
            "run_id": run_id,
        }
        if task["phase"] in {"draft.review.start", "retrospective.start"}:
            entries.append(
                {
                    "time": task["created_at"],
                    "speaker": "system",
                    "target": task["agent_id"],
                    "phase": task["phase"],
                    "section": task["section"],
                    "kind": "system",
                    "body": _dispatch_body(task),
                }
            )
        if task["phase"] in {"draft.compose", "material.submit", "draft.review.summarize", "draft.revise", "report.publish", "product.test", "product.benchmark", "product.report", "retrospective.summary", "agent.self_optimize"}:
            body = _task_body(task)
            if body:
                entries.append(
                    {
                        "time": task["finished_at"] or task["created_at"],
                        "speaker": task["agent_id"],
                        "target": "neko" if task["phase"] == "material.submit" else "all",
                        "phase": task["phase"],
                        "section": task["section"],
                        "kind": "message",
                        "body": body,
                    }
                )

    review_rows = fetch_all(
        """
        SELECT r.created_at, r.section, r.approved, r.reason, t.parent_task_id, t.retry_count
        FROM reviews r
        JOIN tasks t ON t.task_id = r.review_task_id
        WHERE r.run_id=%s
        ORDER BY r.created_at
        """,
        (run_id,),
    )
    for created_at, section, approved, reason, parent_task_id, retry_count in review_rows:
        target_row = fetch_one("SELECT agent_id FROM tasks WHERE task_id=%s", (parent_task_id,)) if parent_task_id else None
        entries.append(
            {
                "time": created_at,
                "speaker": "neko",
                "target": target_row[0] if target_row else "collector",
                "phase": "material.review",
                "section": section,
                "kind": "review" if approved else "reject",
                "body": reason + (f"\n当前重试轮次: {retry_count}" if retry_count else ""),
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
                "target": "neko",
                "phase": "draft.review.comment",
                "section": section_scope,
                "kind": "discussion",
                "body": review_text,
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
                "phase": "retrospective.comment",
                "section": f"全局 | {topic}",
                "kind": "discussion",
                "body": comment_text,
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
        body = escape(entry["body"]).replace("\n", "<br>")
        kind = escape(entry["kind"])
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
              </div>
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
        SELECT created_at, agent_id, section_scope, review_text
        FROM draft_reviews
        WHERE run_id=%s
        ORDER BY created_at
        """,
        (run_id,),
    )
    output_row = fetch_one("SELECT revision_plan FROM outputs WHERE run_id=%s", (run_id,))
    cards = []
    for created_at, agent_id, section_scope, review_text in rows:
        cards.append(
            f"""
            <article class="review">
              <header><strong>{escape(agent_id)}</strong><span>{escape(section_scope)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body">{escape(review_text).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    summary = escape((output_row[0] or "暂无校稿收敛总结。")).replace("\n", "<br>") if output_row else "暂无校稿收敛总结。"
    return _page(
        f"Run {meta['run_id']} Draft Review",
        f"<section><h2>Worker 校稿意见</h2>{''.join(cards) or '<p>暂无。</p>'}</section>"
        f"<section class='revision'><h2>neko 校稿收敛总结</h2><div class='body'>{summary}</div></section>",
    )


def render_final_report_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    output_row = fetch_one("SELECT final_json::text, final_markdown FROM outputs WHERE run_id=%s", (run_id,))
    if not output_row or not output_row[0]:
        return _page(f"Run {meta['run_id']} Final Report", "<p>终稿尚未生成。</p>")
    final_json = json.loads(output_row[0])
    sections = []
    ordered_sections = ["政治经济", "科技", "体育娱乐", "其他"]
    for section in ordered_sections:
        data = final_json[section]
        main = data["main"]
        secondary = data["secondary"]
        briefs = data["briefs"]
        main_image_list = list(main.get("images", [])[:3])
        while len(main_image_list) < 3:
            main_image_list.append("")
        main_images = "".join(
            [
                f'<img src="{escape(img)}" alt="{escape(main["title"])}">' if img else '<div class="img-placeholder">主推图片占位</div>'
                for img in main_image_list
            ]
        )
        secondary_cards = []
        for idx, item in enumerate(secondary, 1):
            image = item.get("images", [""])
            img_html = (
                f'<img src="{escape(image[0])}" alt="{escape(item["title"])}">'
                if image and image[0]
                else '<div class="img-placeholder">副推图片占位</div>'
            )
            secondary_cards.append(
                f"""
                <article class="secondary-card">
                  {img_html}
                  <h4>副推{idx} | {escape(item['title'])}</h4>
                  <p class="meta-line">来源：{escape(item['source_media'])} | 发布时间：{escape(item['published_at'])}</p>
                  <p>{escape(item['summary_zh'])}</p>
                  <p><a href="{escape(item['link'])}" target="_blank">原文链接</a></p>
                </article>
                """
            )
        brief_items = "".join(
            [
                f"<li><strong>{escape(item['title'])}</strong><br><span>来源：{escape(item['source_media'])} | 发布时间：{escape(item['published_at'])} | <a href=\"{escape(item['link'])}\" target=\"_blank\">原文链接</a></span><p>{escape(item['summary_zh'])}</p></li>"
                for item in briefs
            ]
        )
        sections.append(
            f"""
            <section class="news-section">
              <h2>{escape(section)}</h2>
              <article class="main-card">
                <div class="main-images">{main_images}</div>
                <div class="main-body">
                  <h3>主推 | {escape(main['title'])}</h3>
                  <p class="meta-line">来源：{escape(main['source_media'])} | 发布时间：{escape(main['published_at'])} | <a href="{escape(main['link'])}" target="_blank">原文链接</a></p>
                  <p>{escape(main['summary_zh'])}</p>
                </div>
              </article>
              <div class="secondary-grid">{''.join(secondary_cards)}</div>
              <div class="briefs">
                <h4>其余 7 条简讯</h4>
                <ul>{brief_items}</ul>
              </div>
            </section>
            """
        )
    return _page(
        f"Run {meta['run_id']} Final Report",
        f"""
        <header class="report-header">
          <h1>近24小时国际新闻热点</h1>
          <p>workflow_id: {escape(meta['workflow_id'])} | project_id: {escape(meta['project_id'] or 'standalone')} | cycle_no: {escape(str(meta['cycle_no'] or 0))} | run_id: {escape(meta['run_id'])} | 时区: {escape(SETTINGS.timezone)}</p>
          <p>时间窗口：近 24 小时国际新闻热点汇总，分为政治经济、科技、体育娱乐、其他四个板块。</p>
        </header>
        {''.join(sections)}
        """,
    )


def render_retrospective_html(run_id: str) -> str:
    meta = get_run_meta(run_id)
    comments = fetch_all(
        """
        SELECT message_id, reply_to_message_id, COALESCE(from_agent, agent_id), COALESCE(to_agent, 'all'),
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
    blocks = ["<section><h2>复盘讨论线程</h2>"]
    for message_id, reply_to_message_id, agent_id, to_agent, target_type, topic, intent, round_no, comment_text, created_at in comments:
        blocks.append(
            f"""
            <article class="review">
              <header><strong>{escape(agent_id)}</strong><span>round {round_no}</span><span>{escape(topic)}</span><span>{escape(intent)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="meta-line">message_id: <code>{escape(message_id or '')}</code> | to: {escape(to_agent)} | target_type: {escape(target_type)}</div>
              <div class="body">{escape(comment_text).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    blocks.append("</section>")
    summary = summary_row[0] if summary_row and summary_row[0] else "暂无复盘总结。"
    blocks.append(
        f"<section class='revision'><h2>neko 收敛总结</h2><div class='body'>{escape(summary).replace(chr(10), '<br>')}</div></section>"
    )
    opt_cards = []
    for agent_id, summary_text, optimization_json in optimization_rows:
        data = _load_json(optimization_json)
        details = []
        if data.get("exposed_issues"):
            details.append("暴露问题：" + "；".join(data["exposed_issues"][:3]))
        if data.get("next_cycle_strategy"):
            details.append("下一轮策略：" + "；".join(data["next_cycle_strategy"][:3]))
        if data.get("next_cycle_quality_checks"):
            details.append("质量检查：" + "；".join(data["next_cycle_quality_checks"][:3]))
        if data.get("role_improvement_plan"):
            details.append("角色改进：" + data["role_improvement_plan"])
        opt_cards.append(
            f"""
            <article class="review approved">
              <header><strong>{escape(agent_id)}</strong><span>agent.self_optimize</span></header>
              <div class="body">{escape(summary_text).replace(chr(10), '<br>')}<br><br>{escape(chr(10).join(details)).replace(chr(10), '<br>')}</div>
            </article>
            """
        )
    blocks.append(f"<section><h2>Agent 自我优化结果</h2>{''.join(opt_cards)}</section>")
    log_cards = []
    for agent_id, source_type, author, category, effective_from_cycle, expires_after_cycle, body_text, details_text, created_at in guidance_rows:
        log_cards.append(
            f"""
            <article class="review">
              <header><strong>{escape(agent_id or 'project')}</strong><span>{escape(source_type)}</span><span>{escape(category)}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="meta-line">author: {escape(author)} | effective_from_cycle: {escape(str(effective_from_cycle))} | expires_after_cycle: {escape(str(expires_after_cycle or ''))}</div>
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
        lines = []
        if report_type == "product_test":
            lines.extend([f"证据：{item}" for item in data.get("evidence", [])[:4]])
            lines.extend([f"优先改进：{item}" for item in data.get("priority_improvements", [])[:4]])
        elif report_type == "benchmark_report":
            lines.extend([f"对标：{item['name']} | {item['url']}" for item in data.get("comparisons", [])[:4]])
            lines.extend([f"下一轮建议：{item}" for item in data.get("next_cycle_actions", [])[:4]])
        elif report_type == "product_evaluation_report":
            lines.extend([f"重要问题：{item}" for item in data.get("top_product_issues", [])[:5]])
            lines.extend([f"下一轮建议：{item}" for item in data.get("next_cycle_recommendations", [])[:5]])
        body = "<br>".join(escape(line) for line in lines)
        sections.append(
            f"""
            <article class="review approved">
              <header><strong>{escape(title)}</strong><span>{escape(report_type)}</span><span>{escape(agent_id or 'neko')}</span><time>{escape(_fmt_ts(created_at))}</time></header>
              <div class="body"><p>{escape(summary_text)}</p><p>{body}</p></div>
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
        pretty = escape(json.dumps(data, ensure_ascii=False, indent=2)).replace("\n", "<br>")
        blocks.append(
            f"""
            <section class="revision">
              <h2>{escape(title)}</h2>
              <p class="meta-line">{escape(agent_id or 'neko')} | {_fmt_ts(created_at)}</p>
              <div class="body"><p>{escape(summary_text)}</p><pre>{pretty}</pre></div>
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
    bullets = []
    for key, value in payload.items():
        if isinstance(value, list):
            bullets.append(f"<h3>{escape(str(key))}</h3><ul>{''.join(f'<li>{escape(str(item))}</li>' for item in value)}</ul>")
        elif isinstance(value, dict):
            bullets.append(f"<h3>{escape(str(key))}</h3><pre>{escape(json.dumps(value, ensure_ascii=False, indent=2))}</pre>")
    path.write_text(
        _page(
            title,
            f"<section class='revision'><h1>{escape(title)}</h1><div class='body'>{escape(markdown_body).replace(chr(10), '<br>')}</div>{''.join(bullets)}</section>",
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
