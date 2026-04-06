# Newsloop 0d456012 循环异常分析

## 结论

1. 项目确实在循环，cycle 1-6 都完整完成，cycle 7 正在运行。
2. 复盘线程不是空白，但其结构和措辞在多数轮次高度重复。
3. self_optimize 已写入数据库和 memory 文件，但 summary_text 在每个 agent 上跨 cycle 完全不变。
4. 真正的瓶颈不在“有没有复盘”，而在“优化结果没有被转换成会改变采集/审核/成稿行为的变量”。

## 直接证据

- `neko`: 6 次优化记录，唯一 summary 只有 1 条。
- `33`: 6 次优化记录，唯一 summary 只有 1 条。
- `xhs`: 6 次优化记录，唯一 summary 只有 1 条。

## 每轮摘要哈希

- cycle 1: retrospective_summary hash `9721f00baa740146`
- cycle 2: retrospective_summary hash `9721f00baa740146`
- cycle 3: retrospective_summary hash `b1944a636fefaf42`
- cycle 4: retrospective_summary hash `9721f00baa740146`
- cycle 5: retrospective_summary hash `9721f00baa740146`
- cycle 6: retrospective_summary hash `9721f00baa740146`

## 每轮线程哈希

- cycle 1: thread hash `fc9a74eb82ec017c`
- cycle 2: thread hash `d9f78c65027aa106`
- cycle 3: thread hash `bb570ff06ac6045e`
- cycle 4: thread hash `df58f24b6cc53f29`
- cycle 5: thread hash `cf3ac044a99ad068`
- cycle 6: thread hash `bfa313cc1acb57d4`
- cycle 7: thread hash `da9dc10adee93029`

## 为什么没有实质优化

1. `self_optimize` 的输出虽然包含 `exposed_issues` / `next_cycle_strategy` / `next_cycle_quality_checks`，但 `summary` 仍来自固定 blueprint。
2. `material.collect` 实际调用仍是 `collect_news(section, 16)`，没有把 memory 中的白名单、黑名单、质量检查和策略参数真正下推到采集逻辑。
3. 复盘线程生成逻辑虽然现在是多轮，但依旧是本地确定性模板展开，核心问题信号有限，所以讨论很容易回到相同句式。
4. `memory_snapshot` 已进入下一轮 payload，但目前主要起到“展示/记录”作用，而不是改变候选选择、排序、去重和审核阈值。

## 建议的修复方向

1. 让 `collect_news` 接收 agent memory，并真正使用 `source_whitelist/source_blacklist`、图片要求、板块边界规则。
2. 把 `next_cycle_quality_checks` 映射到具体的 reject / rework 条件，而不是只写进 JSON。
3. 给每轮复盘加入更多 run-specific 事实，如本轮缺图条目数、同源重复数、被打回 section、图片失效率。
4. 让 `summary_text` 不再固定取 blueprint，而是从本轮差异中抽取新的策略标签。

## 文件说明

- `db/`: 项目相关表导出 CSV
- `project/`: 项目目录和 cycle 产出页面快照
- `logs/systemd/`: orchestrator 与 3 个 agent 的 journal
