# Newsflow Agentic Workflow Spec Cutover 2026-04-24

本文档是当前 newsflow 调试阶段的 workflow 第一真相源。

## 1. 当前调试目标

当前只要求先稳定通过：

1. 群组开机流程
2. 内容产出流程

暂不把讨论 / 复盘作为当前验收目标。

## 2. workflow 主链

### 开机流程

1. `step0`
   - manager 建立群启动面
   - 说明群目标、角色结构、流程概览

2. `step1`
   - 非 manager 角色完成任务理解与角色边界对齐

3. `step2`
   - 非 manager 角色完成 readiness / capability 确认

4. `formal_start`
   - manager 正式关闭 bootstrap

### 正式内容流程

1. `cycle.start`
   - manager 拆解本轮任务
   - manager 明确下一阶段目标与完成标准

2. `material.collect`
   - worker 收集素材
   - tester 是第一消费者
   - manager 只做 watchdog / final decision

3. `material.review`
   - tester 审核 collect 阶段对象
   - tester 可直接对 worker 发 `pass / partial_pass / redo`
   - manager 只基于 tester 结论做 `proceed / forced_proceed / pause / fail / resume`

4. `draft.compose`
   - editor 根据被放行的内容产出产品正文

## 3. 各阶段通用规则

### 3.1 阶段内协作

1. 主要发生在群组 `text` 对话中
2. 命名工件是可选辅助表达，不是默认硬门槛
3. 最终消费者直接消费阶段内产物
4. 命名工件如 `candidate_material_pool`、`material_review_feedback` 只能作为可选结构化辅助，不得取代群组正文协作面

### 3.2 manager 规则

manager 只做：

1. 开阶段
2. 发起任务
3. watchdog / stall recovery / timeout nudges
4. 基于阶段最终消费者结论做最终决策
5. formal close

manager 默认不做：

1. collect 阶段过程细审
2. 对每次 worker/tester 提交逐条 ACK
3. 充当 tester 与 worker 之间的中转站

### 3.3 collect 阶段规则

`material.collect` 当前统一遵守：

1. worker 负责通过群组 `text` 提交自己的 concrete material 或 concrete blocker
2. collect 阶段至少应出现一条真实可见正文素材，而不是纯 ACK / 模板 / 仅 payload 的空壳表达
3. worker 不应只发 ACK 作为主要阶段产出
4. worker 不应总结 peer 进度
5. worker 不应替 tester / editor / 其他 worker 做协调指令
6. tester 是 collect 阶段第一消费者
7. tester 可直接对 worker 发 `pass / partial_pass / redo`
8. manager 默认静默，只在 timeout / stall / blocker / final decision 时介入
9. manager 不把自己当 collect 阶段 first-pass reviewer
10. manager 的 collect 阶段最终门禁应建立在 tester 的阶段结论之上，而不是直接逐条消费 worker 原始提交

### 3.4 阶段失效规则

1. 阶段推进后，上一阶段消息默认保留可见性，但失去行动性
2. 只有显式 `redo / resume / reopen` 才能重新激活旧阶段
3. 不允许旧阶段消息自然延续到新阶段继续触发回复

## 4. 当前最重要的消费链

### material.collect

`worker -> tester -> manager`

- worker 生产
- tester 先消费并审核
- manager 最后只消费 tester 结论做门禁

### material.review

`tester -> worker (如需重做) -> manager`

- tester 直接对 worker 发审核意见
- manager 只做最终阶段决策与正式推进

## 5. 当前测试要求

当前调试以以下标准为成功：

1. 开机流程完整通过
2. manager 正确建立阶段组织
3. worker 能真实产出正文内容
4. `material.collect` 阶段至少出现一条真实可见正文素材，并由 tester 作为第一消费者直接消费
5. tester 能作为 collect 阶段第一消费者直接审稿
6. manager 能基于 tester 的阶段结论进行 formal close
7. 旧阶段消息不会跨阶段继续驱动噪音回复
