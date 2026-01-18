# pg_plan_advice：PostgreSQL 查询计划控制的新方案

## 背景介绍

PostgreSQL 的查询优化器功能强大，通常能生成优秀的执行计划。然而，经验丰富的 DBA 和开发者偶尔会遇到需要影响或稳定优化器决策的场景。来自 EnterpriseDB 的 Robert Haas 正在开发一个重要的 contrib 模块 `pg_plan_advice`，旨在解决这一长期存在的需求。

本文分析了 pgsql-hackers 邮件列表上自 2025 年 10 月以来持续讨论的 [pg_plan_advice 线程](https://www.postgresql.org/message-id/flat/CA%2BTgmoZ-Jh1T6QyWoCODMVQdhTUPYkaZjWztzP1En4%3DZHoKPzw%40mail.gmail.com)。

## pg_plan_advice 是什么？

`pg_plan_advice` 是一个提议中的 contrib 模块，引入了一种专门用于控制关键规划决策的"建议迷你语言"(advice mini-language)。该模块支持：

- 使用 `EXPLAIN (PLAN_ADVICE)` 从现有查询计划**生成建议字符串**
- 通过 `pg_plan_advice.advice` GUC 参数**应用建议字符串**，以复现或约束后续的规划决策

建议语言允许控制：

- **连接顺序**：表的连接顺序
- **连接方法**：Nested Loop、Merge Join、Hash Join
- **扫描类型**：顺序扫描、索引扫描（可指定具体索引）
- **并行执行**：并行执行的位置和方式
- **分区连接**：分区表连接的处理方式

## 核心设计理念

Robert Haas 在 README 中强调，主要使用场景并非让用户"战胜优化器"，而是**复现过去表现良好的执行计划**：

> "我们不需要接受用户能比优化器做出更好规划的观点。我们只需要接受用户比优化器更能分辨好计划和坏计划。这是一个很低的门槛。优化器永远不知道它生成的计划实际执行时会发生什么，但用户知道。"

这将 `pg_plan_advice` 定位为**计划稳定性工具**，而非微观管理优化器的提示系统。

## 技术架构

### 关系标识符系统

`pg_plan_advice` 最创新的方面之一是其关系标识符系统(Relation Identifier System)。该系统提供对查询各部分的**无歧义引用**，能处理复杂场景：

- 同一表使用不同别名的多次引用
- 子查询和 CTE
- 分区表及其分区

标识符语法使用特殊表示法如 `t#2` 来区分查询中表 `t` 的第一次和第二次出现。

### 使用示例

以下是 Jakub Wartak 测试中展示的系统能力：

```sql
-- 为带别名的查询生成建议
EXPLAIN (PLAN_ADVICE, COSTS OFF) 
SELECT * FROM (SELECT * FROM t1 a JOIN t2 b USING (id)) a, t2 b, t3 c 
WHERE a.id = b.id AND b.id = c.id;

-- 输出包含：
-- Generated Plan Advice:
--   JOIN_ORDER(a#2 b#2 c)
--   MERGE_JOIN_PLAIN(b#2 c)
--   SEQ_SCAN(c)
--   INDEX_SCAN(a#2 public.t1_pkey)
--   NO_GATHER(c a#2 b#2)
```

然后可以选择性地应用约束：

```sql
-- 强制使用特定扫描类型
SET pg_plan_advice.advice = 'SEQ_SCAN(b#2)';

-- 重新执行 EXPLAIN 查看新计划
EXPLAIN (PLAN_ADVICE, COSTS OFF) 
SELECT * FROM (SELECT * FROM t1 a JOIN t2 b USING (id)) a, t2 b, t3 c 
WHERE a.id = b.id AND b.id = c.id;

-- 输出显示：
-- Supplied Plan Advice:
--   SEQ_SCAN(b#2) /* matched */
```

## 补丁结构（v10）

实现分为五个补丁：

| 补丁 | 描述 | 大小 |
|------|------|------|
| 0001 | 存储范围表扁平化信息 | 7.8 KB |
| 0002 | 在最终计划中存储省略节点的信息 | 9.8 KB |
| 0003 | 存储 Append 节点合并信息 | 40.4 KB |
| 0004 | 允许插件控制路径生成策略 | 56.1 KB |
| 0005 | WIP：添加 pg_plan_advice contrib 模块 | 399.1 KB |

前四个补丁为优化器添加必要的基础设施，第五个包含实际模块。这种分离设计使基础设施在未来可能惠及其他扩展。

## 社区审查与测试

该线程得到了多位社区成员的积极参与：

### Jakub Wartak（EDB）
进行了大量 TPC-H 基准测试，发现了若干 bug：
- debug/ASAN 构建中的空指针崩溃
- 无统计信息时的半连接唯一性检测问题
- 复杂查询中的连接顺序建议冲突

### Jacob Champion（EDB）
应用模糊测试技术发现边缘情况：
- 畸形建议字符串导致的解析器崩溃
- 对非分区表使用分区相关建议的问题
- 通过语料库模糊测试发现的 AST 工具 bug

### 其他贡献者
- **Alastair Turner**：赞赏测试替代计划的能力
- **Hannu Krosing**（Google）：引用 VLDB 研究，显示 20% 的实际查询有 10+ 个连接
- **Lukas Fittl**：对与 pg_stat_statements 集成的可能性感兴趣

## 发现并修复的问题

协作审查过程发现并修复了多个版本中的若干问题：

1. **编译器警告**（gcc-13、clang-20）- 在早期版本中修复
2. 扩展状态未分配时 `pgpa_join_path_setup()` 中的**空指针崩溃**
3. **连接顺序冲突检测**错误地将连接方法建议视为正向约束
4. 在 EXPLAIN 中未使用 PLAN_ADVICE 时**半连接唯一性追踪**工作不正确
5. 嵌套连接顺序规范中的**部分匹配检测**问题

## 当前状态

截至 v10（2026 年 1 月 15 日发布）：

- 补丁已注册在 [Commitfest](https://commitfest.postgresql.org/patch/6184/)
- 仍标记为 **WIP（进行中）**
- 测试仍在进行，特别是 TPC-H 查询测试
- Robert Haas 正在寻求实质性的代码审查，特别是针对补丁 0001

## 对 PostgreSQL 用户的意义

如果被合并，`pg_plan_advice` 将提供：

1. **计划稳定性**：捕获并复现已知良好的查询计划
2. **调试辅助**：理解优化器为何做出特定选择
3. **测试工具**：在不修改查询的情况下实验替代计划形状
4. **生产安全网**：防止统计信息变化后的意外计划退化

## 与 pg_hint_plan 的比较

与流行的 `pg_hint_plan` 扩展不同，`pg_plan_advice` 专注于**往返安全性(round-trip safety)**：

- 计划可以被可靠地捕获和重新应用
- 关系标识符系统自动处理复杂的别名
- 设计为可与任何查询结构配合使用，无需手动管理标识符

## 总结

`pg_plan_advice` 代表了 PostgreSQL 优化器可扩展性方面的重要进步。它不是要取代优化器的判断，而是提供一种安全机制来保留经过验证的执行策略。活跃的社区审查过程已经大幅改进了代码，持续的测试正在帮助确保其健壮性。

对于管理复杂工作负载的 DBA，特别是那些查询偶尔遭受计划退化的场景，该模块提供了一个有前途的解决方案，它与优化器**协同工作**而非对抗。

---

**邮件列表链接**: [pg_plan_advice - pgsql-hackers](https://www.postgresql.org/message-id/flat/CA%2BTgmoZ-Jh1T6QyWoCODMVQdhTUPYkaZjWztzP1En4%3DZHoKPzw%40mail.gmail.com)

**Commitfest 条目**: [CF 6184](https://commitfest.postgresql.org/patch/6184/)

**作者**: Robert Haas（EnterpriseDB）

**审查者**: Jakub Wartak、Jacob Champion、Alastair Turner、Hannu Krosing、John Naylor 等
