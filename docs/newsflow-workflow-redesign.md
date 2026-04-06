# Newsflow 工作流重构设计稿

## 1. 目标

这份设计稿定义 `newsflow-mvp` 的长期重构方向。目标不是继续给现有流程打补丁，而是把当前“模板驱动 + 自动推进”的伪协作系统，重构为“工件驱动 + 决策驱动 + 可观测”的多 agent 协作系统。

本设计稿同时作为：

- 服务器端 Codex 的长期工程约束
- 工作流阶段重排和数据模型升级的依据
- DeepFlow / Grafana 埋点与看板设计的依据
- 后续验收标准的来源

## 2. 当前问题归因

根据当前仓库快照与运行日志，当前系统的主要瓶颈不在 DeepFlow，也不在 OpenClaw 本身，而在 workflow engine。

### 2.1 当前实现的根本问题

1. 关键阶段不是模型驱动，而是模板驱动。
   - `deepflowlogs/configs/current/newsflow-app/workflow.py` 中 `create_discussion_comment()` 按 agent 固定返回模板文本。
   - `summarize_discussion()` 只是将讨论消息拼成列表。
   - `revise_and_publish()` 不是基于讨论重写稿件，而是把 `revision_plan` 和 `draft_markdown` 拼接成终稿。

2. 状态机按“时间/数量”推进，而不是按“问题是否解决”推进。
   - 当前推进逻辑依赖 `comments_done >= N`、计时到期等条件。
   - 并没有 blocker、decision、recheck、quality gate 等真实协作闸门。

3. 三类讨论被混在一起。
   - 校稿讨论：应该只为本轮初稿修订服务。
   - 产品体验测试：应该只评价本轮最终成品。
   - 复盘讨论：应该只为下一轮优化服务。
   当前系统把三者都放进类似的 comment / summarize 框架里，导致每一步都像自动回复。

4. 优化结果没有编译成执行规则。
   - 当前 `news.py` 的 `collect_news(section, limit)` 仍然主要依赖静态 feed 列表。
   - memory / optimization log 没有真正改变采集、审核、写作逻辑。

5. 数据模型不支持真正的协作闭环。
   - 当前 `db.py` 只有 `tasks / materials / reviews / discussions / outputs`。
   - 缺少一等对象来表达：问题、决策、修订补丁、复查结果、复盘议题、优化规则。

### 2.2 DeepFlow 在这里的正确角色

DeepFlow 是可观测性数据平台，不是语义工作流引擎。

它适合做的是：

- 记录阶段切换、耗时、重试、错误、topic 开闭、rule 应用
- 把 workflow_id / project_id / cycle_no / run_id 等上下文打进 trace / log / metric
- 为排障和优化提供证据

它不负责：

- 决定 agent 如何校稿
- 决定 discussion 如何收敛
- 决定 revision 如何落地

因此，重构重点必须放在 `newsflow-app` 的 workflow engine 和数据模型，不是 DeepFlow 本体。

## 3. 新的设计哲学

### 3.1 工件驱动，不是消息驱动

系统推进应围绕“工件”的状态，而不是围绕“发了几条消息”。

一等工件至少包括：

- `draft_version`
- `proofread_issue`
- `proofread_decision`
- `revision_patch`
- `final_report`
- `product_test_report`
- `benchmark_report`
- `product_evaluation_report`
- `retro_topic`
- `retro_decision`
- `optimization_rule`

### 3.2 决策驱动，不是模板驱动

以下产物必须由模型基于证据包生成，而不是字符串拼接：

- 校稿收敛总结
- 修订补丁
- 产品测试报告
- benchmark 报告
- 产品评估总报告
- retrospective 正式总结
- self optimize / optimization rule

### 3.3 问题闭环，不是单轮评论

每个重要问题都应经历：

1. 提出
2. 定性
3. 决策
4. 修订
5. 复查
6. 关闭

如果问题没有关闭，就不允许进入下一闸门。

### 3.4 系统元信息留在系统层

以下内容保留，但只作为元信息：

- reply_to_message_id
- round_no
- topic_id
- intent
- target_type
- phase

这些字段用于调度和可观测性，不应污染 agent 正文表达。

### 3.5 优化必须编译成规则

所有复盘结论最终都要尽量编译成下一轮可执行规则，例如：

- source whitelist / blacklist
- 图片最少数量和稳定性阈值
- 同源去重规则
- 主推首句写法
- 板块边界规则
- 简讯压缩规则
- publish gate

## 4. 目标工作流

## 4.1 阶段总览

一轮完整 cycle 应拆成三条不同目的的链路：

1. 本轮成稿链路
2. 本轮成品评价链路
3. 下一轮优化链路

### A. 本轮成稿链路

1. `material.collect`
2. `material.review`
3. `draft.compose`
4. `proofread.start`
5. `proofread.issue.submit`
6. `proofread.decision`
7. `draft.revise`
8. `proofread.recheck`
9. 若 blocker 未清零，回到 `proofread.issue.submit`
10. 若 blocker 清零，进入 `report.publish`

### B. 本轮成品评价链路

1. `product.test`
2. `product.benchmark`
3. `product.report`

### C. 下一轮优化链路

1. `retrospective.start`
2. `retro.topic.open`
3. `retrospective.comment`
4. `retro.topic.close`
5. 重复 topic 直到时间结束
6. `retrospective.summary`
7. `agent.self_optimize`
8. `optimization.rule.apply`

## 4.2 校稿阶段（proofread）

校稿阶段发生在 `draft.compose` 之后，`report.publish` 之前。

### 目的

只服务于“修正本轮初稿”。

### 参与者

- `33`：只看 `政治经济 / 科技`
- `xhs`：只看 `体育娱乐 / 其他`
- `neko`：做 proofread decision 和 revision patch

### 每个 worker 在校稿时必须检查

- 初稿内容是否与自己提交的素材一致
- 标题 / 来源 / 链接 / 时间是否准确
- 图片是否对应、可用、位置正确
- 主推 / 副推 / 短讯是否归位正确
- 是否有遗漏、误配、需补充内容

### 校稿的关键要求

- worker 在这一步不是做产品体验评价
- worker 只做责任编辑式校稿
- 每条校稿意见必须绑定到具体 section 和具体条目
- 校稿意见要形成 `proofread_issue`

### 发布门槛

只有当 blocker 级 proofread issue 全部关闭后，才允许 publish。

## 4.3 产品体验测试（product.test）

### 目的

评价本轮最终成品，而不是初稿。

### 时间点

必须发生在 `report.publish` 之后。

### 参与者视角

三位 agent 都统一站在“读者 / 产品体验”视角。

注意：

- 不再把 33 固定成“信息密度视角”
- 不再把 xhs 固定成“图片稳定性视角”
- 不再把 neko 固定成“编辑完成度视角”

三人都看完整成品，只允许关注点自然不同，不允许预设死模板。

### 每份产品测试报告至少回答

- 读者第一眼最不舒服的点是什么
- 最影响继续读下去的点是什么
- 如果只能改两件事，改什么
- 这些问题为什么会在本轮产物中出现

## 4.4 外部对标（product.benchmark）

### 目的

避免闭门造车。

### 要求

- `neko` 联网搜索 2-4 个相近新闻整理产品或页面
- 输出：
  - 选中的样本
  - 选中原因
  - 我们与它最明显的差距
  - 可直接转成下一轮规则的建议

benchmark 不是大而全测评，而是差距定位器。

## 4.5 产品评估总报告（product.report）

`neko` 汇总：

- 3 份 `product_test_report`
- 1 份 `benchmark_report`

输出一份 `product_evaluation_report`，明确：

- 本轮成品的共识问题
- 最严重问题
- 可延后问题
- 建议转入下一轮的规则

## 4.6 复盘讨论（retrospective）

### 目的

为下一轮总结优化，而不是修改本轮终稿。

### 输入

- 本轮执行事实
- proofread issue / decision / recheck 结果
- final report
- product test reports
- benchmark report
- product evaluation report

### 讨论机制

复盘必须是“议题驱动”，不是“回合配额驱动”。

#### 正确机制

1. `neko` 打开第一个 `retro_topic`
2. 相关 agent 围绕该 topic 讨论
3. 若 topic 说透，则 `retro_topic.close`
4. `neko` 切到下一个 topic
5. 一直循环到时间结束
6. 时间结束后，才允许 `retrospective.summary`

### retrospective.summary 的要求

它必须是 `neko` 基于整轮 topic 讨论做出的正式决策报告，明确：

- 本轮确认的执行问题
- 本轮确认的产品问题
- 哪些是来自 product.test / benchmark / product.report 的发现
- 哪些优化进入下一轮
- 每个 agent 下一轮负责什么

不能只是把原发言摘抄拼接成固定模板。

## 5. 数据模型重构

建议新增以下表或等价模型。

### 5.1 校稿与修订

- `proofread_issues`
  - issue_id
  - run_id
  - section
  - item_ref
  - severity
  - issue_type
  - description
  - reported_by
  - status(open/accepted/rejected/fixed/rechecked/closed)

- `proofread_decisions`
  - decision_id
  - run_id
  - issue_id
  - decided_by
  - decision_type(accept/reject/defer)
  - rationale

- `revision_patches`
  - patch_id
  - run_id
  - decision_id
  - target_section
  - patch_instruction
  - applied_by
  - applied_at

### 5.2 产品评价

- `product_reports`
  - report_id
  - run_id
  - report_type(test/benchmark/evaluation)
  - author_agent
  - report_json
  - report_markdown

### 5.3 复盘议题与决策

- `retro_topics`
  - topic_id
  - run_id
  - title
  - opened_by
  - status(open/debating/closed)
  - evidence_refs

- `retro_messages`
  - message_id
  - run_id
  - topic_id
  - from_agent
  - to_agent nullable
  - intent
  - body

- `retro_decisions`
  - decision_id
  - run_id
  - topic_id
  - summary
  - owner_agent
  - action_rule_ref

### 5.4 优化规则

- `optimization_rules`
  - rule_id
  - project_id
  - source(agent_generated/human_guidance)
  - owner_scope(global/agent/section)
  - target_agent nullable
  - effective_from_cycle
  - expires_after_cycle nullable
  - rule_type
  - rule_payload
  - status(active/retired)

## 6. LLM 使用原则

以下阶段必须真正调用 LLM，而不是模板函数：

- proofread decision
- revision patch generation
- product test report generation
- benchmark synthesis
- product evaluation report
- retrospective summary
- self optimize

LLM 的输入应当是证据包，而不是简单 prompt 占位句。

### 证据包最少包含

- 相关工件正文
- 问题列表 / topic 列表
- 关键信息差异
- 结构化元数据
- 上一轮优化规则
- 人工 guidance（如果有）

## 7. DeepFlow / OTel 埋点方案

### 7.1 新增 span / event

建议至少新增：

- `proofread.issue.open`
- `proofread.decision.complete`
- `draft.revise.complete`
- `proofread.recheck.complete`
- `product.test.submit`
- `product.benchmark.complete`
- `product.report.complete`
- `retro.topic.open`
- `retro.topic.close`
- `retrospective.summary.complete`
- `optimization.rule.applied`

### 7.2 公共属性

- `project_id`
- `cycle_no`
- `run_id`
- `workflow_id`
- `task_id`
- `agent_id`
- `agent_role`
- `section`
- `phase`
- `topic_id`
- `issue_id`
- `rule_id`
- `status`

### 7.3 观测目标

在 DeepFlow / Grafana 中应该能够看见：

- 一个 run 有多少 proofread blocker
- draft revise 跑了几轮
- retrospective 开了几个 topic
- 哪些 optimization rule 被应用
- 哪些规则带来了 reject 数下降或图片稳定性上升

## 8. 前台展示原则

不要再把“系统事件流”直接等价成“协作内容”。

### 8.1 主 feed 只展示三类内容

- 工件摘要
- 有效发言
- 决策结果

### 8.2 系统元信息下沉

- dispatch
- retry
- round
- reply_to
- phase gate

这些下沉到 debug / trace / system stream，不占主 feed。

### 8.3 页面分层

每个 run 至少有：

- `draft.html`
- `draft-review.html`
- `revised.html`
- `final.html`
- `product.html`
- `benchmark.html`
- `evaluation.html`
- `retrospective.html`
- `retrospective-summary.html`

## 9. 迁移顺序

## P0：先修数据与状态机骨架

目标：先停止“模板+自动推进”的伪协作。

- 引入 issue / decision / patch / topic / rule 模型
- proofread / product test / retrospective 三段彻底拆开
- publish gate 改成 blocker gate

## P1：把 summary / revise 改成 LLM 驱动

目标：停止摘抄拼接。

- 重写 proofread summary
- 重写 retrospective summary
- 重写 revise patch generation

## P2：把优化日志编译进执行规则

目标：让下一轮真的改。

- collect_news 接 optimization rules
- material.review 接 reject / rework rules
- 写作规则进入 compose / revise

## P3：补齐 DeepFlow 埋点和看板

目标：让优化真正可证明。

- 新增 spans / events
- 看 proofread loop / retro topics / rule application
- 增加 rule effectiveness dashboard

## 10. 长期工程验收标准

以下条件同时满足，才算真正跑通：

1. proofread issue 有 blocker 闭环，未关闭前不能 publish
2. product.test 都基于 final report，且三份报告不是模板镜像
3. benchmark 产出能转成规则
4. retrospective 可以在多个 topic 之间切换，并持续到时间结束
5. retrospective.summary 是 manager 决策报告，不是摘抄模板
6. self optimize 产出的规则会被下一轮实际应用
7. 下一轮成品能从数据上证明变化，而不是只是日志变多
8. DeepFlow 中能观测到 proofread loop / retro topics / optimization rule application

## 11. 非目标

以下内容不是当前首要目标：

- 先做更花的 Grafana UI
- 先追求更复杂的 benchmark 搜索
- 先做更多角色
- 先扩更多 agent

当前阶段最重要的是：

- 真正闭环
- 真正决策
- 真正修订
- 真正把优化变成规则

## 12. 服务器端 Codex 的执行原则

后续服务器端 Codex 执行改造时，必须遵守：

1. 不要继续给现有模板函数打小补丁冒充重构。
2. 每次改造必须明确作用在：状态机 / 工件模型 / LLM 决策 / 执行规则 / 埋点 五者中的哪一层。
3. 任何新增阶段都要有明确工件，不允许只有 feed 事件没有独立产物。
4. 没有 blocker 闭环，不允许声称“流程已跑通”。
5. 没有把规则真正应用到下一轮执行，不允许声称“优化已生效”。
6. 不跑通不停下来，直到达到本设计稿第 10 节的验收标准。
