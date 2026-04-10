from __future__ import annotations

import json


MANAGER_AGENT_ID = "33"


def _payload(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def _request(
    node_type: str,
    system: str,
    payload: dict,
    fallback_payload: dict,
    *,
    evidence_object_count: int,
    timeout_ms: int | None = None,
    max_completion_tokens: int | None = None,
    max_attempts: int | None = None,
) -> dict:
    request = {
        "node_type": node_type,
        "prompt_system": system,
        "prompt_user": _payload(payload),
        "fallback_payload": fallback_payload,
        "evidence_object_count": evidence_object_count,
    }
    if timeout_ms is not None:
        request["timeout_ms"] = timeout_ms
    if max_completion_tokens is not None:
        request["max_completion_tokens"] = max_completion_tokens
    if max_attempts is not None:
        request["max_attempts"] = max_attempts
    return request


def material_collect_request(
    *,
    section: str,
    target_count: int,
    quality_requirements: dict,
    manager_watchpoints: list[str],
    memory_summary: str,
    optimization_hints: list[str],
    items: list[dict],
) -> dict:
    return _request(
        "material.collect.enrichment",
        (
            "你是负责当前板块的 worker。读取当前候选对象后，为每条素材生成真正可交接的中文候选说明。"
            "不要套统一摘要壳，不要用固定句式。"
            "返回 JSON：items。每项必须包含 "
            "source_index,title_zh,summary_zh,brief_zh,relevance_note,is_primary_candidate,candidate_rank。"
            "candidate_rank 必须返回整数。"
        ),
        {
            "section": section,
            "target_count": target_count,
            "quality_requirements": quality_requirements or {},
            "manager_watchpoints": manager_watchpoints or [],
            "memory_summary": memory_summary,
            "optimization_hints": optimization_hints,
            "items": items,
        },
        {"items": []},
        evidence_object_count=len(items),
    )


def material_review_request(
    *,
    run_id: str,
    section: str,
    requirements: dict,
    materials: list[dict],
) -> dict:
    return _request(
        "material.review",
        (
            "你是 newsflow 的 tester。基于当前板块的全量素材对象做逐条审核。"
            "不要退化成计数检查，不要用统一短句。"
            "返回 JSON：review_summary,gate_reason,selected_material_ids,items。"
            "items 每项必须包含 material_id,verdict,reason,recommended_slot,selection_priority。"
            "verdict 只能是 approved 或 rejected。"
        ),
        {
            "run_id": run_id,
            "section": section,
            "requirements": requirements,
            "materials": materials,
        },
        {
            "review_summary": "",
            "gate_reason": "",
            "selected_material_ids": [],
            "items": [],
        },
        evidence_object_count=len(materials),
    )


def material_review_batch_request(
    *,
    run_id: str,
    section: str,
    requirements: dict,
    batch_index: int,
    batch_count: int,
    materials: list[dict],
) -> dict:
    return _request(
        "material.review",
        (
            "你是 newsflow 的 tester。基于当前板块的一批素材对象做逐条审核。"
            "不要退化成计数检查，不要用统一短句。"
            "返回 JSON：batch_summary,items。"
            "batch_summary 用 1-2 句中文，控制在 120 字内。"
            "items 每项必须包含 material_id,verdict,reason,recommended_slot,selection_priority。"
            "verdict 只能是 approved 或 rejected。"
            "reason 必须具体，但控制在 60 字内。"
        ),
        {
            "run_id": run_id,
            "section": section,
            "requirements": requirements,
            "batch_index": batch_index,
            "batch_count": batch_count,
            "materials": materials,
        },
        {
            "batch_summary": "",
            "items": [],
        },
        evidence_object_count=len(materials),
        timeout_ms=120000,
        max_completion_tokens=220,
        max_attempts=1,
    )


def material_review_rollup_request(
    *,
    run_id: str,
    section: str,
    requirements: dict,
    batch_summaries: list[dict],
    review_items: list[dict],
    approved_count: int,
    rejected_count: int,
    selected_material_ids: list[int],
) -> dict:
    return _request(
        "material.review",
        (
            "你是 newsflow 的 tester。基于当前板块已经完成的逐条审核结果，"
            "生成本板块最终 review summary 和 gate_reason。"
            "不要固定套话，不要复述 checklist。"
            "review_summary 用 2-3 句中文，控制在 180 字内。"
            "gate_reason 控制在 100 字内。"
            "另返回 machine-readable 字段 gate_decision，值只能是 proceed 或 redo。"
            "返回 JSON：review_summary,gate_reason,gate_decision。"
        ),
        {
            "run_id": run_id,
            "section": section,
            "requirements": requirements,
            "approved_count": approved_count,
            "rejected_count": rejected_count,
            "selected_material_ids": selected_material_ids,
            "batch_summaries": batch_summaries,
            "review_items": review_items,
        },
        {
            "review_summary": "",
            "gate_reason": "",
            "gate_decision": "",
        },
        evidence_object_count=len(batch_summaries) + len(review_items),
        timeout_ms=120000,
        max_completion_tokens=180,
        max_attempts=1,
    )


def compose_translation_request(
    *,
    section: str,
    guidance: dict,
    brief_limit: int,
    items: list[dict],
) -> dict:
    return _request(
        "draft.compose.translation",
        (
            "你是负责整稿的 editor。基于已经通过审核的素材，为不同槽位生成真正可发布的中文标题和摘要。"
            "不要套统一开头，不要复述来源句式。"
            "返回 JSON：items。每项必须包含 source_index,title_zh,summary_zh。"
        ),
        {
            "section": section,
            "guidance": guidance or {},
            "brief_limit": brief_limit,
            "items": items,
        },
        {"items": []},
        evidence_object_count=len(items),
    )


def discussion_comment_request(
    *,
    run_id: str,
    agent_id: str,
    draft_version_no: int,
    section_scope: list[str],
    memory_summary: str,
    draft_sections: dict,
    approved_materials: dict,
    existing_draft_reviews: list[dict],
) -> dict:
    return _request(
        "discussion.comment",
        (
            f"你是 {agent_id}。请基于当前 draft、已提交校稿意见和关联素材对象，给出一条推进修订的真实讨论意见。"
            "不要做角色表演，不要复读流程。"
            "返回 JSON：comment_text。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "draft_version_no": draft_version_no,
            "section_scope": section_scope,
            "memory_summary": memory_summary,
            "draft_sections": draft_sections,
            "approved_materials": approved_materials,
            "existing_draft_reviews": existing_draft_reviews,
        },
        {"comment_text": ""},
        evidence_object_count=len(section_scope) + len(existing_draft_reviews),
    )


def draft_review_comment_request(
    *,
    run_id: str,
    agent_id: str,
    draft_version_no: int,
    sections: dict,
    approved_materials: dict,
) -> dict:
    return _request(
        "draft.review.comment",
        (
            f"你是 {agent_id}，正在做 draft review。"
            "请基于当前 draft 和对应板块的已审核素材，写出真实校稿意见。"
            "返回 JSON：review_text。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "draft_version_no": draft_version_no,
            "sections": sections,
            "approved_materials": approved_materials,
        },
        {"review_text": ""},
        evidence_object_count=len(sections),
    )


def draft_proofread_request(
    *,
    run_id: str,
    agent_id: str,
    draft_version_no: int,
    sections: list[dict],
    existing_open_issues: list[dict],
) -> dict:
    return _request(
        "draft.proofread",
        (
            "你是 newsflow 的 tester，正在做 correctness proofread。"
            "读取当前 draft 和对应素材对象后，提出真正需要修的 issue。"
            "返回 JSON：summary,issues。issues 每项必须包含 "
            "section,item_ref,severity,issue_type,description,required_actions,patch_instruction,evidence。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "draft_version_no": draft_version_no,
            "sections": sections,
            "existing_open_issues": existing_open_issues,
        },
        {"summary": "", "issues": []},
        evidence_object_count=len(sections) + len(existing_open_issues),
    )


def draft_proofread_section_request(
    *,
    run_id: str,
    agent_id: str,
    draft_version_no: int,
    section_payload: dict,
    existing_open_issues: list[dict],
) -> dict:
    return _request(
        "draft.proofread",
        (
            "你是 newsflow 的 tester，正在做 correctness proofread。"
            "基于当前单个 section 的 draft 概览、当前 focus items 与对应素材摘要视图，提出真正需要修的 issue。"
            "不要泛泛而谈，不要用模板问题。"
            "返回 JSON：section_summary,issues。"
            "section_summary 控制在 120 字内。"
            "issues 每项必须包含 "
            "item_ref,severity,issue_type,description,required_actions,patch_instruction。"
            "item_ref 只能使用 section、main、secondary_1、secondary_2、brief_1 到 brief_7。"
            "severity 只能是 blocker、high、medium、low。"
            "description 控制在 80 字内，required_actions 最多 3 项，patch_instruction 控制在 60 字内。"
            "如果当前 section 没有需要修的问题，issues 返回空数组。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "draft_version_no": draft_version_no,
            "section_payload": section_payload,
            "existing_open_issues": existing_open_issues,
        },
        {"section_summary": "", "issues": []},
        evidence_object_count=1 + len(existing_open_issues),
        timeout_ms=240000,
        max_completion_tokens=240,
        max_attempts=2,
    )


def draft_proofread_rollup_request(
    *,
    run_id: str,
    agent_id: str,
    draft_version_no: int,
    section_summaries: list[dict],
    issues: list[dict],
) -> dict:
    return _request(
        "draft.proofread",
        (
            "你是 newsflow 的 tester。基于已经完成的分 section proofread 结果，"
            "收敛出本轮 proofread 总结。"
            "不要写流程回执，不要复读模板。"
            "返回 JSON：summary。summary 控制在 180 字内。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "draft_version_no": draft_version_no,
            "section_summaries": section_summaries,
            "issues": issues,
        },
        {"summary": ""},
        evidence_object_count=len(section_summaries) + len(issues),
        timeout_ms=120000,
        max_completion_tokens=180,
        max_attempts=2,
    )


def proofread_explanation_request(
    *,
    run_id: str,
    draft_version_no: int,
    decision_rows: list[dict],
) -> dict:
    return _request(
        "proofread.decision.explanation",
        (
            "你是 newsflow 的 manager。基于已经完成的结构化 proofread 决策，生成给人看的说明。"
            "不要重做规则决策，不要用固定栏目模板。"
            "explanation_markdown 不要套“已采纳 / 暂不采纳 / Required Actions”固定栏目壳。"
            "返回 JSON：summary,accepted,rejected,required_actions,explanation_markdown。"
        ),
        {
            "run_id": run_id,
            "draft_version_no": draft_version_no,
            "decision_rows": decision_rows,
        },
        {
            "summary": "",
            "accepted": [],
            "rejected": [],
            "required_actions": [],
            "explanation_markdown": "",
        },
        evidence_object_count=len(decision_rows),
    )


def draft_review_summary_request(*, run_id: str, draft_reviews: list[dict]) -> dict:
    return _request(
        "discussion.summary",
        (
            "你是 newsflow 的 manager。基于当前 draft review notes 生成收敛后的 draft review summary。"
            "不要固定写三条通用修订重点，也不要套“主要问题 / 修订重点 / 统一建议”固定栏目壳。"
            "返回 JSON：summary_markdown,accepted_review_notes,revision_focus,summary。"
        ),
        {"run_id": run_id, "draft_reviews": draft_reviews},
        {
            "summary_markdown": "",
            "accepted_review_notes": [],
            "revision_focus": [],
            "summary": "",
        },
        evidence_object_count=len(draft_reviews),
    )


def discussion_summary_request(
    *,
    run_id: str,
    draft_version_no: int,
    discussion_comments: list[dict],
    draft_reviews: list[dict],
    top_product_issues: list[str],
    revision_plan: str,
) -> dict:
    return _request(
        "discussion.summary",
        (
            "你是 newsflow 的 manager。基于真实讨论记录生成 discussion summary。"
            "不要固定栏目拼装，不要堆评论原文，也不要套“Discussion Summary / Accepted Comments / Rejected Comments / Revision Actions”固定栏目壳。"
            "返回 JSON：summary_markdown,top_issues,accepted_comments,rejected_comments,revision_actions,summary。"
        ),
        {
            "run_id": run_id,
            "draft_version_no": draft_version_no,
            "discussion_comments": discussion_comments,
            "draft_reviews": draft_reviews,
            "top_product_issues": top_product_issues,
            "revision_plan": revision_plan,
        },
        {
            "summary_markdown": "",
            "top_issues": [],
            "accepted_comments": [],
            "rejected_comments": [],
            "revision_actions": [],
            "summary": "",
        },
        evidence_object_count=len(discussion_comments) + len(draft_reviews) + len(top_product_issues),
    )


def draft_revise_request(
    *,
    run_id: str,
    draft_version_no: int,
    writer_guidance: dict,
    current_draft_sections: list[dict],
    revision_patches: list[dict],
) -> dict:
    return _request(
        "draft.revise",
        (
            "你是新闻编辑。基于 accepted proofread issues、revision patch 和当前 draft，输出真正要改动的 section/item 级更新。"
            "不要复读问题，不要只改固定句式，也不要生成整篇全文。"
            "返回 JSON：section_updates,revision_plan。"
            "section_updates 每项字段为 section,item_updates,reason；"
            "item_updates 每项字段为 item_ref,title,summary_zh。"
        ),
        {
            "run_id": run_id,
            "draft_version_no": draft_version_no,
            "writer_guidance": writer_guidance,
            "current_draft_sections": current_draft_sections,
            "revision_patches": revision_patches,
        },
        {"section_updates": [], "revision_plan": ""},
        evidence_object_count=len(revision_patches),
    )


def draft_render_request(
    *,
    run_id: str,
    stage_name: str,
    draft_version_no: int | None,
    writer_guidance: dict,
    revision_context: list[dict],
    sections: list[dict],
) -> dict:
    return _request(
        "draft.render",
        (
            "你是 newsflow 的 editor。基于已经完成层级编排的结构化 section 对象，输出前台可见的中文成品 markdown。"
            "不要写 workflow_id/project_id/run_id/时区 这类机器头，"
            "不要套“### 主推 | / ### 副推 / ### 其他 7 条”固定槽位壳。"
            "要忠实使用当前对象里的标题、摘要、来源、时间、链接与配图信息。"
            "返回 JSON：summary,report_markdown。"
        ),
        {
            "run_id": run_id,
            "stage_name": stage_name,
            "draft_version_no": draft_version_no,
            "writer_guidance": writer_guidance,
            "revision_context": revision_context,
            "sections": sections,
        },
        {"summary": "", "report_markdown": ""},
        evidence_object_count=len(sections) + len(revision_context),
    )


def draft_recheck_request(
    *,
    run_id: str,
    agent_id: str,
    draft_version_no: int,
    issues: list[dict],
) -> dict:
    return _request(
        "draft.recheck",
        (
            "你是 newsflow 的 tester。你现在要对修订稿逐项 recheck，判断上一轮 issue 是否真的解决。"
            "不要默认通过，不要拿固定句式回执冒充复查。"
            "decisions 必须覆盖输入里的每个 issue_id，且每项都必须包含非空的 resolution_note，"
            "明确说明当前 draft 为什么算解决或为什么仍未解决。"
            "返回 JSON：summary,decisions。decisions 每项字段必须包含 issue_id,resolved,resolution_note。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "draft_version_no": draft_version_no,
            "issues": issues,
        },
        {"summary": "", "decisions": []},
        evidence_object_count=len(issues),
    )


def product_test_request(
    *,
    run_id: str,
    agent_id: str,
    evidence: list[dict],
) -> dict:
    return _request(
        "product.test",
        (
            f"你是 {agent_id}。请从统一读者/产品体验视角，基于当前 final artifact 的真实对象给出产品测试报告。"
            "不要写流程回执，不要做责任归因问卷，不要在正文里分配谁该背锅。"
            "report_markdown 不要套“最明显问题 / 优先改进 / 执行关联”固定栏目壳。"
            "返回 JSON：focus,reader_findings,reader_improvement_opportunities,summary,report_markdown。"
            "report_markdown 必须是前台可见正文。"
        ),
        {"run_id": run_id, "agent_id": agent_id, "evidence": evidence},
        {
            "focus": "",
            "reader_findings": [],
            "reader_improvement_opportunities": [],
            "summary": "",
            "report_markdown": "",
        },
        evidence_object_count=len(evidence),
    )


def benchmark_request(
    *,
    run_id: str,
    benchmark_mode: str,
    search_query: str,
    final_artifact: list[dict],
    samples: list[dict],
) -> dict:
    return _request(
        "product.benchmark",
        (
            "你是 newsflow 的 tester。基于 final artifact 和外部对标样本输出 benchmark report。"
            "不要套固定差距模板。"
            "report_markdown 不要套“对标样本 / 最明显差距 / 下一轮建议”固定栏目壳。"
            "返回 JSON：comparisons,most_visible_gap,next_cycle_actions,summary,report_markdown。"
            "report_markdown 必须是前台可见正文。"
        ),
        {
            "run_id": run_id,
            "benchmark_mode": benchmark_mode,
            "search_query": search_query,
            "final_artifact": final_artifact,
            "samples": samples,
        },
        {
            "comparisons": [],
            "most_visible_gap": "",
            "next_cycle_actions": [],
            "summary": "",
            "report_markdown": "",
        },
        evidence_object_count=len(final_artifact) + len(samples),
    )


def cross_cycle_compare_request(
    *,
    run_id: str,
    project_id: str | None,
    cycle_no: int | None,
    previous_run_id: str | None,
    sections: list[dict],
    previous_retrospective_summary: str,
) -> dict:
    return _request(
        "product.cross_cycle_compare",
        (
            "你是 newsflow 的 tester。对比本轮与上一轮 final artifact 以及上一轮 retrospective summary。"
            "只基于当前对象说话，不要写问卷式套话。"
            "report_markdown 不要套“改善 / 未改善 / 退步 / 未落实建议”固定栏目壳。"
            "返回 JSON：improved_issues,unimproved_issues,regressed_areas,"
            "unimplemented_previous_optimization_suggestions,summary,report_markdown。"
        ),
        {
            "run_id": run_id,
            "project_id": project_id,
            "cycle_no": cycle_no,
            "previous_run_id": previous_run_id,
            "sections": sections,
            "previous_retrospective_summary": previous_retrospective_summary,
        },
        {
            "improved_issues": [],
            "unimproved_issues": [],
            "regressed_areas": [],
            "unimplemented_previous_optimization_suggestions": [],
            "summary": "",
            "report_markdown": "",
        },
        evidence_object_count=len(sections) + (1 if previous_run_id else 0),
    )


def product_evaluation_request(
    *,
    run_id: str,
    product_tests: list[dict],
    benchmark_gap: str,
    benchmark_next: list[str],
) -> dict:
    return _request(
        "product.report",
        (
            "你是 newsflow 的 manager。基于产品测试和 benchmark 报告生成产品评估总报告。"
            "不要拼接原文，不要做 checklist。"
            "report_markdown 不要套“主要问题 / 责任归属 / 下一轮建议”固定栏目壳。"
            "返回 JSON：top_product_issues,agent_responsibility_links,next_cycle_recommendations,summary,report_markdown。"
        ),
        {
            "run_id": run_id,
            "product_tests": product_tests,
            "benchmark_gap": benchmark_gap,
            "benchmark_next": benchmark_next,
        },
        {
            "top_product_issues": [],
            "agent_responsibility_links": [],
            "next_cycle_recommendations": [],
            "summary": "",
            "report_markdown": "",
        },
        evidence_object_count=len(product_tests) + (1 if benchmark_gap or benchmark_next else 0),
    )


def retrospective_plan_request(
    *,
    run_id: str,
    product_test: dict,
    benchmark: dict,
    cross_cycle_compare: dict,
    review_rows: list[dict],
    final_artifact: list[dict],
) -> dict:
    return _request(
        "retrospective.plan",
        (
            "你是 newsflow 的 manager。基于 tester 的三份报告、已发布成品摘要和执行证据生成 retrospective plan。"
            "保留 topics 的 machine-readable 字段，但不要用固定争论脚本或固定栏目模板。"
            "topics 不能为空；只要证据支持，给出 1 个或多个可讨论 topic 都可以。每个 topic 都要包含 title、body、owner、counterpart。"
            "plan_markdown 必须是一份面向团队的自然中文讨论启动文本，可以自然分段，但不要使用"
            "“开场提醒 / 第一个讨论点 / 第二个讨论点 / 收尾 / 散会 / 行动项认领”这类固定章节名。"
            "返回 JSON：product_problems,behavior_problems,topics,summary,plan_markdown。"
        ),
        {
            "run_id": run_id,
            "product_test": product_test,
            "benchmark": benchmark,
            "cross_cycle_compare": cross_cycle_compare,
            "review_rows": review_rows,
            "final_artifact": final_artifact,
        },
        {
            "product_problems": [],
            "behavior_problems": [],
            "topics": [],
            "summary": "",
            "plan_markdown": "",
        },
        evidence_object_count=3 + len(review_rows) + len(final_artifact),
    )


def retrospective_opening_request(
    *,
    run_id: str,
    topic_id: str,
    topic_title: str,
    topic_body: str,
    topic_evidence: list[dict],
    next_agents: list[str],
) -> dict:
    return _request(
        "retrospective.discussion",
        (
            f"你是 newsflow 项目的 manager {MANAGER_AGENT_ID}。现在要开启 retrospective discussion。"
            "基于当前 topic 主持开场。body 必须直接回应当前 topic body 和 evidence，不要套主持人模板。"
            "保留 topic/to_agent/next_agents 等 machine-readable 字段，但前台正文只能放在 body。"
        ),
        {
            "run_id": run_id,
            "topic_id": topic_id,
            "topic_title": topic_title,
            "topic_body": topic_body,
            "topic_evidence": topic_evidence,
            "next_agents": next_agents,
        },
        {
            "topic": topic_title,
            "intent": "moderate",
            "target_type": "team",
            "to_agent": ",".join(next_agents),
            "body": "",
            "next_agents": next_agents,
        },
        evidence_object_count=len(topic_evidence) + 1,
    )


def retrospective_comment_request(
    *,
    run_id: str,
    agent_id: str,
    topic_id: str | None,
    current_topic: str,
    topic_evidence: list[dict],
    reply_to_message_id: str | None,
    reply: dict,
    responsibility_scope: list[str],
    review_signals: dict,
    product_signals: list[str],
    benchmark_summary: str,
    memory_summary: str,
    final_titles: dict,
    recent_thread: list[dict],
    fallback: dict,
) -> dict:
    return _request(
        "retrospective.discussion",
        (
            f"你是 newsflow retrospective 的参与者 {agent_id}。"
            "基于当前 topic、线程上下文、产品报告和本轮职责给出真实讨论发言。"
            "不要照着角色台词说，不要复读前文，也不要用固定争论脚本。"
            "返回 JSON：topic,intent,target_type,to_agent,body,next_agents。"
        ),
        {
            "run_id": run_id,
            "agent_id": agent_id,
            "topic_id": topic_id,
            "current_topic": current_topic,
            "topic_evidence": topic_evidence,
            "reply_to_message_id": reply_to_message_id,
            "reply": reply,
            "responsibility_scope": responsibility_scope,
            "review_signals": review_signals,
            "product_signals": product_signals,
            "benchmark_summary": benchmark_summary,
            "memory_summary": memory_summary,
            "final_titles": final_titles,
            "recent_thread": recent_thread,
        },
        fallback,
        evidence_object_count=len(topic_evidence) + len(recent_thread) + len(product_signals) + len(responsibility_scope),
    )


def retrospective_summary_request(
    *,
    run_id: str,
    product_test: dict,
    benchmark: dict,
    cross_cycle_compare: dict,
    retrospective_plan: dict,
    final_artifact: list[dict],
    retro_thread: list[dict],
    retro_decisions: list[dict],
    applied_rules: list[dict],
) -> dict:
    return _request(
        "retrospective.summary",
        (
            "你是 newsflow 的 manager。基于 retro topics、retro decisions、产品评估、benchmark、最终成品和已应用规则，输出正式 retrospective summary。"
            "不要固定栏目拼装，也不要拼接线程原文。summary_markdown 必须是一份自然收敛的中文结论备忘。"
            "不要使用“Discussion Summary / Product Problems / Root Causes / Accepted / Next Cycle”这类固定栏目名。"
            "返回 JSON：summary,summary_markdown。"
        ),
        {
            "run_id": run_id,
            "product_test": product_test,
            "benchmark": benchmark,
            "cross_cycle_compare": cross_cycle_compare,
            "retrospective_plan": retrospective_plan,
            "final_artifact": final_artifact,
            "retro_thread": retro_thread,
            "retro_decisions": retro_decisions,
            "applied_rules": applied_rules,
        },
        {"summary": "", "summary_markdown": ""},
        evidence_object_count=len(retro_thread) + len(retro_decisions) + len(applied_rules) + len(final_artifact) + 4,
    )


def agent_self_optimize_request(
    *,
    agent_id: str,
    cycle_no: int,
    evidence: dict,
) -> dict:
    return _request(
        "agent.self_optimize",
        (
            f"你是 {agent_id}。根据真实复盘线程、本轮成品证据和产品评估写自我优化记录。"
            "不要模板化，不要直接照抄角色配置或 blueprint。"
            "optimization_markdown 必须是一份自然中文复盘备忘，不要使用“暴露问题 / 下一轮策略 / 质量检查 / 角色改进”固定栏目壳。"
            "返回 JSON：summary,exposed_issues,next_cycle_strategy,next_cycle_quality_checks,role_improvement_plan,optimization_markdown。"
        ),
        evidence | {"cycle_no": cycle_no},
        {
            "summary": "",
            "exposed_issues": [],
            "next_cycle_strategy": [],
            "next_cycle_quality_checks": [],
            "role_improvement_plan": "",
            "optimization_markdown": "",
        },
        evidence_object_count=len(evidence.get("relevant_retrospective_messages") or []) + len(evidence.get("product_reports") or []),
    )
