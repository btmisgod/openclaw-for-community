# Newsflow Agentic Workflow Spec

## 1. 文档角色

这份文档是当前仓库内 Newsflow 工作流的唯一设计基准。

后续所有实现、排查、重构、测试与验收，均以本文档为准。
如果代码与本文档冲突，以本文档为准。

## 2. 核心目标

目标不是继续把旧模板工作流补到“能跑”，而是把系统从：

- 新控制协议 + 旧内容生成器

逐步重构为：

- 新控制协议 + 真实 agent 内容生成

短期目标：

- 保留状态机、hidden prompt、ACK、redo、resume、parser 字段
- 清除所有会让系统退化成“演流程”的本地正文生成逻辑
- 让主要阶段的前台正文由 agent 基于当前阶段对象通过 LLM 生成

## 3. 角色边界

### 3.1 server

server 只负责：

- 流程状态
- 阶段切换
- 硬条件与 gate
- 超时、重试、恢复
- machine-readable parsing
- 可观测性与状态对象持久化

server 不负责：

- 替 agent 写前台正文
- 预设角色观点
- 预设争论脚本
- 用 fallback 模板代替真实工作内容

### 3.2 manager

manager 负责：

- 目标理解
- 阶段任务拆解
- 轻验收与放行
- retrospective 主持与收敛
- 下一轮优化主持

manager 不负责：

- 代替 worker/editor/tester 生成其阶段工作产物
- 用本地脚本制造讨论内容

### 3.3 worker / editor / tester

worker / editor / tester 只对当前阶段任务负责。

每个 agent 在每个阶段都必须：

- 读取当前阶段对象
- 基于该对象通过 LLM 生成工作结果
- 输出可被 server 识别的结构化字段

不允许：

- 只按 agent 身份输出固定套路
- 只按 mode 输出固定台词
- 用默认问题、默认建议、默认总结结构代替真实判断

## 4. 控制层与内容层

### 4.1 控制层允许保留

以下属于控制层，可保留：

- phase dispatch
- hidden manager prompt injection
- hidden agent ACK
- proceed / redo / pause / fail / resume
- manager control events
- parser 所需 machine-readable fields
- publish artifact / llm_jobs / proofread state / optimization rules / cycle_task_plan 等状态对象

判断标准：

- 只描述“当前阶段是什么、交给谁、状态怎么推进、字段怎么识别”
- 不直接生成用户可见正文

### 4.2 内容层必须去模板化

以下属于内容层，必须去模板化：

- 本地直接拼 `body`
- 本地直接拼 `comment`
- 本地直接拼 `summary`
- 本地直接拼 `review_text`
- 本地直接拼 `report markdown`
- 固定栏目 `plan_lines`
- 按 agent_id 预设观点或角色话术
- 按 mode 预设复盘脚本
- fallback 默认问题 / 默认建议 / 默认总结
- 用固定数量、固定短路规则代替真实阶段判断

判断标准：

- 如果代码直接替 agent 生成前台正文，就是问题代码
- 如果代码只是保留结构化字段，不替 agent 写正文，则可保留

## 5. 输出显示边界

前台只展示：

- 真实工作产物
- 真实审核意见
- 真实讨论内容
- 真实总结结果

后台控制面不展示到前台：

- server -> manager prompt
- manager -> agent prompt
- hidden ACK
- manager -> server control signal

## 6. machine-readable fields 原则

允许保留结构化字段。

这些字段只用于：

- parser
- gate
- 状态推进
- 任务路由

不允许：

- 用字段结构反向控制 agent 正文文风
- 用字段缺失时的本地模板替代前台正文

## 7. 阶段链路

一轮完整 cycle 至少包含三条链路。

### 7.1 成稿链路

1. `material.collect`
2. `material.review`
3. `draft.compose`
4. `draft.proofread`
5. `draft.revise`
6. `draft.recheck`
7. `report.publish`

### 7.2 成品评价链路

1. `product.test`
2. `product.benchmark`
3. `product.cross_cycle_compare`
4. `product.report`

### 7.3 下一轮优化链路

1. `retrospective.plan`
2. `retrospective.discussion`
3. `retrospective.summary`
4. `agent.optimization`
5. `agent.self_optimize`
6. `optimization.rule.apply`

## 8. 关键阶段要求

### 8.1 material.collect

- manager 在 `cycle_task_plan` 中决定 candidate target
- worker 基于当前阶段任务和对象经 LLM 生成候选素材说明
- 不允许统一套壳摘要
- 不允许写死候选数量

### 8.2 material.review

- tester 必须读取全量素材对象
- 基于当前阶段目标逐条审核
- 不允许退化成 count / image_count / 固定短路

### 8.3 draft.proofread / draft.recheck

- tester 必须读取 draft 与相关素材
- issue 内容必须针对当前对象
- recheck 不能默认通过

### 8.4 product.test / benchmark / cross_cycle_compare

- tester 必须基于当前 final artifact 和当前阶段对象生成报告
- 不允许固定“最明显问题就是 X、下一轮建议就是 Y”这类默认路径

### 8.5 retrospective.plan / discussion / summary

- 可以保留 topic / intent / target_type / to_agent / next_agents 等控制字段
- discussion 正文必须由 agent 基于当前 topic 和当前证据生成
- summary 必须由 manager 基于当前 discussion 对象收敛
- 不允许本地预设 editor / tester / 33 / xhs 应该说什么
- 不允许本地预设 open / topic_shift / peer_challenge / final_position 台词

### 8.6 self_optimize / agent.optimization

- 保留优化规则写回结构
- 优化总结、策略、检查项、下一轮建议必须基于本轮真实前台证据生成
- 不允许 blueprint 直接接管可见文案

## 9. 硬约束

### 9.1 必须做到

- server 负责流程，不负责写内容
- manager 负责拆解、轻验收、放行、复盘主持
- 执行 agent 只对当前阶段任务负责
- 每个 agent 在每个阶段都通过 LLM 读取当前阶段对象完成工作
- 结构化字段持续可被 server 识别

### 9.2 绝对禁止

- 把旧模板搬到新 helper 里继续使用
- 为了方便解析重新把正文做成字段回填
- 用 probe 或手工最小样本冒充完整链路验证
- 回退到旧的 3-agent 模板工作流
- 通过改状态机来掩盖内容层问题

## 10. 治理方式

后续每轮治理都按以下顺序执行：

1. 先找本地直接生成前台内容的函数和分支
2. 标注控制层保留与内容层重构
3. 每轮只清 1-3 个最高优先级内容层问题
4. 改动后跑真实链路测试
5. 验证 parser 字段和控制层未破坏
6. 输出证据，再进入下一批

## 11. 完成标准

一个问题只有同时满足下列条件，才算真正排查掉：

1. 该阶段前台正文不再由本地模板直接生成
2. 该阶段输出仍能被 server 正确识别
3. 实际测试中，同类型阶段文案不再高度同质化
4. 实际测试中，不再反复默认复读同一类问题
5. 输出明显与当前输入对象相关，而不是只与 agent 身份或 mode 相关
