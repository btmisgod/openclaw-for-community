# cycle_task_plan

- run_id: f5499af24248
- completion_definition: 四个板块均完成主推/副推/简讯结构，proofread blocker 清零，manager 放行，editor 交付正式 final artifact，tester 完成三份成品评估后进入复盘。

## top_priorities
- 标题必须自然翻译成中文，不允许直接照搬英文原题。
- 主推/副推摘要必须有信息提炼，不接受只写“据xxx报道”。
- 图片重复、题材归位错误、来源重复要尽量在 material.review 前段就收紧。

## section_material_requirements
- 政治经济: owner=33 candidate_target=13 min_approved=10 min_with_images=3
- 科技: owner=33 candidate_target=13 min_approved=10 min_with_images=3
- 体育娱乐: owner=xhs candidate_target=12 min_approved=10 min_with_images=2
- 其他: owner=xhs candidate_target=12 min_approved=10 min_with_images=2

## phase_assignments
- cycle.start: manager
- material.collect: worker-33 / worker-xhs
- material.review: tester
- draft.compose: editor
- draft.proofread: tester
- draft.revise: editor
- publish.decision: manager
- report.publish: editor
- product.test: tester
- product.benchmark: tester
- product.cross_cycle_compare: tester
- pre-retro.review: manager
- retrospective.plan: manager
- retrospective.discussion: manager+all
- retrospective.summary: manager
- agent.optimization: manager

## phase_acceptance
- material.review: approved_material_pool 足够支撑完整板块，returned_material_issues 有逐条原因。
- draft.proofread: issue 必须能定位到具体 draft slice 或素材对象。
- publish.decision: proofread blocker 清零，recheck 已通过，artifact manifest 可写出。
- pre-retro.review: tester 的三份报告都基于 final artifact，并给出可执行结论。

## manager_watchpoints
- 不要把 tester 审核退化成预览计数。
- 不要让 editor 在 publish approval 前越过 manager gate。
- 对上一轮未落地优化建议做追踪，不要只写新建议。

## risk_notes
- 外部模型延迟仍可能影响长文本节点，但不应影响结构化 gate。
- 若某 section 被退回较多，本轮先提升候选缓冲量再决定是否重采。

摘要：cycle 1 task plan 已生成。