# 2026-04-11 Workflow Layering Cleanup

本轮未运行测试，只收 workflow，并按设计文档重新审阅分层。

## 本轮目标

- 把任务要求从 workflow/content_layer 本地硬编码迁回 manager 初始化与拆解层。
- 清掉 proofread / revise / retrospective / optimization 主路径里的 fixed slice 和 fixed window。
- 保留 server 控制层：
  - phase dispatch
  - hidden prompt / ACK
  - manager control events
  - proceed / redo / pause / publish_approved
  - parser 所需 machine-readable 字段

## 实际修改

1. `cycle.start`
- 新增 manager 专用的 `cycle_start_request()`。
- `start_cycle()` 不再本地拼 `cycle_task_plan`，改为 server 注入任务初始化目标后，由 manager 通过 LLM 生成计划。
- workflow 只负责校验 `section_material_requirements`、`publication_requirements` 等结构字段并落库。

2. `material.collect`
- `title_zh/summary_zh/brief_zh/relevance_note` 的 copy 要求不再写死在 workflow 本地计划里，改为从 manager plan 的 `publication_requirements.copy_requirements` 读取。

3. `draft.compose / draft.recheck`
- 稿件结构和图片约束不再由 workflow 本地常量直接控制，改为统一从 manager plan 的 `publication_requirements` 读取。
- proofread / revise / recheck 不再使用 `[:4] / [:6] / [:2] / [:7]` 这类固定对象切片。

4. `retrospective.* / agent.optimization`
- discussion / summary / self_optimize 不再只喂前几条 review、product signal、thread message、rule。
- workflow 不再本地生成 optimization rule/rationale 文案。

5. `patch instruction`
- 本地默认 patch instruction 不再写死“主推 1 条 / 副推 2 条 / 简讯 7 条 / 主推 3 图”等答案式修法。

## 审阅结论

本轮收掉的是“要求放错层”和“workflow 固定裁剪证据”两类问题。

仍然残留的高优先级问题：

1. `material.review` 仍是二态 gate
- 当前只接受 `proceed / redo`。
- 还没有实现你已经确认的三态设计：
  - `proceed`
  - `partial_pass`
  - `redo`

2. `proofread` 仍有本地 `issue_type -> required_actions` 映射
- 这比之前轻，但本质上仍是 workflow 在补 tester 的动作语义。

3. `product.benchmark / cross_cycle_compare`
- 还残留少量运输层裁剪和字段上限，后续还要继续收。

## 本轮未做

- 未启动 fresh run
- 未同步 live runtime
- 未改动 proofread gate / ACK / orchestrator 等控制层

