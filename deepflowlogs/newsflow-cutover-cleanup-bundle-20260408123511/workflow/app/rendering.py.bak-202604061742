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
        SELECT workflow_id, run_id, status, current_phase, started_at, completed_at, notes::text,
               report_markdown_path, report_json_path
        FROM workflow_runs WHERE run_id=%s
        """,
        (run_id,),
    )
    if not row:
        raise KeyError(run_id)
    notes = json.loads(row[6] or "{}")
    traceparent = notes.get("trace_context", {}).get("traceparent", "")
    return {
        "workflow_id": row[0],
        "run_id": row[1],
        "status": row[2],
        "current_phase": row[3],
        "started_at": row[4],
        "completed_at": row[5],
        "notes": notes,
        "trace_id": notes.get("trace_id") or parse_trace_id(traceparent),
        "report_markdown_path": row[7],
        "report_json_path": row[8],
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
    if phase == "discussion.start":
        return f"进入讨论阶段。请在 {payload.get('discussion_seconds', SETTINGS.discussion_test_seconds)} 秒内完成讨论意见提交。"
    if phase == "discussion.comment":
        return "请针对初稿提出明确可执行的修改意见。"
    if phase == "discussion.summarize":
        return "请汇总讨论意见，形成统一修订方案。"
    if phase == "draft.revise":
        return "请根据修订方案修改初稿。"
    if phase == "report.publish":
        return "请输出最终成稿并发布产物文件。"
    return f"请执行阶段 {phase}。"


def _task_body(task: dict) -> str:
    result = task["result"]
    if result.get("message_body"):
        return result["message_body"]
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
    if task["phase"] == "discussion.summarize":
        return result.get("revision_plan", "已完成修订方案汇总。")
    if task["phase"] == "report.publish":
        return "终稿已发布。"
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
        entries.append(
            {
                "time": task["created_at"],
                "speaker": "neko" if task["agent_id"] != "neko" else "orchestrator",
                "target": task["agent_id"],
                "phase": "task.dispatch",
                "section": task["section"],
                "kind": "dispatch",
                "body": _dispatch_body(task),
            }
        )
        if task["phase"] in {"material.submit", "discussion.summarize", "report.publish"}:
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

    discussion_rows = fetch_all(
        """
        SELECT created_at, agent_id, comment_text
        FROM discussions WHERE run_id=%s ORDER BY created_at
        """,
        (run_id,),
    )
    for created_at, agent_id, comment_text in discussion_rows:
        entries.append(
            {
                "time": created_at,
                "speaker": agent_id,
                "target": "all",
                "phase": "discussion.comment",
                "section": "全局",
                "kind": "discussion",
                "body": comment_text,
            }
        )

    entries.sort(key=lambda item: item["time"] or datetime.min)
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
          <p>workflow_id: {escape(meta['workflow_id'])} | run_id: {escape(meta['run_id'])} | 时区: {escape(SETTINGS.timezone)}</p>
          <p>时间窗口：近 24 小时国际新闻热点汇总，分为政治经济、科技、体育娱乐、其他四个板块。</p>
        </header>
        {''.join(sections)}
        """,
    )


def write_final_report_html(run_id: str) -> str:
    run_dir = OUTPUT_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    path = run_dir / "final_report.html"
    path.write_text(render_final_report_html(run_id))
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
