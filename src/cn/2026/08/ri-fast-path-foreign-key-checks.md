# 消除 RI 触发器中的 SPI：外键检查的快速路径

## 引言

PostgreSQL 中的引用完整性（Referential Integrity, RI）触发器传统上通过 **SPI**（Server Programming Interface）执行 SQL 查询，以验证引用表（Referencing Table）中新插入或更新的行是否在被引用表（Referenced Table, 主键表）中存在匹配行。对于批量操作（大批量 `INSERT` 或 `UPDATE`），这意味着**每一行**都会启动和销毁一次完整的执行计划，`ExecutorStart()` 和 `ExecutorEnd()` 带来的开销相当可观。

Amit Langote 一直在致力于消除这一开销，通过用**直接索引探测**替代 SQL 计划来完成 RI 检查。这项工作最新迭代“Eliminating SPI / SQL from some RI triggers - take 3”通过绕过 SPI 执行器、在约束语义允许时直接调用索引访问方法，将批量外键检查的速度提升了最高 **57%**。

补丁集历经多版演进，Junwang Zhao 于 2025 年底加入开发。当前方向为**混合快速路径 + 回退**：在简单场景下使用直接索引探测，在正确性依赖执行器复杂行为时回退到现有 SPI 路径。

## 为什么重要

外键约束无处不在。每次向引用表执行 `INSERT` 或 `UPDATE` 都会触发 RI 检查，验证每一行是否在被引用表的主键中存在匹配。传统做法下：

```sql
CREATE TABLE pk (a int PRIMARY KEY);
CREATE TABLE fk (a int REFERENCES pk);

INSERT INTO pk SELECT generate_series(1, 1000000);
INSERT INTO fk SELECT generate_series(1, 1000000);  -- 100 万次 RI 检查
```

每一次插入都会触发 RI 检查，执行：

1. 构建用于扫描主键索引的查询计划
2. 调用 `ExecutorStart()` 和 `ExecutorEnd()`
3. 执行计划查找（或确认不存在）匹配行

每行都要经历一次执行计划的建立与销毁，主导了总耗时。在 Amit 的 v3 补丁下，同样的批量插入从**约 1000 ms** 降至**约 432 ms**（快 57%） —— 通过直接探测主键索引，而不经过执行器。

## 技术背景

### 传统 RI 路径

`ri_triggers.c` 中的 RI 触发器函数（如 `RI_FKey_check`）调用 `ri_PerformCheck()`，其流程为：

1. 构建形如 `SELECT 1 FROM pk WHERE pk.a = $1` 的 SQL 字符串
2. 使用 `SPI_prepare` 和 `SPI_execute_plan` 执行
3. 执行器在主键上执行索引扫描，若被引用值存在则返回一行

这种方式在所有场景下都正确 —— 分区表、时态外键、并发更新 —— 但每行都承担完整的计划执行成本。

### 快速路径思路

对于简单外键（被引用表非分区、无非时态语义），检查本质上是：“用该值探测主键索引；若找到且能加锁，则检查通过”。可通过以下方式实现：

1. 打开主键关系和其唯一索引
2. 根据外键列值构建扫描键
3. 调用 `index_getnext()`（或等效接口）查找元组
4. 在当前快照下用 `LockTupleKeyShare` 加锁

无需 SQL、计划或执行器，只需直接索引探测和元组加锁。

## 补丁演进

### v1：原始方案（2024 年 12 月）

初版补丁集（3 个补丁）引入：

- **0001**：重构 `PartitionDesc` 接口，显式传递 `omit_detached` 可见性（已分离挂起分区）所需的快照。解决了一个 bug：在 `REPEATABLE READ` 下，因 RI 查找会操作 `ActiveSnapshot`，而 `find_inheritance_children()` 对已分离挂起分区的可见性依赖该快照，导致主键查找可能返回错误结果。
- **0002**：在 RI 触发器函数中避免使用 SPI，引入直接索引探测路径。
- **0003**：对部分 RI 检查避免使用 SQL 查询——主要性能优化。

Amit 指出 temporal foreign key 查询仍保留在 SPI 路径，因其计划涉及范围重叠和聚合，无法用简单索引探测处理。他还为快速路径增加了与 `EvalPlanQual()` 等价的逻辑，在 `READ COMMITTED` 下正确处理并发更新。

### v2：Junwang 的混合快速路径（2025 年 12 月）

Junwang Zhao 在此基础上继续推进，采用混合设计：

- **0001**：为外键约束检查添加快速路径。适用条件：被引用表非分区，约束不涉及 temporal semantics 时。
- **0002**：缓存快速路径元数据（操作符哈希条目、操作符 OID、策略号、子类型）。当时该元数据缓存尚未带来性能提升。

基准测试（100 万行，`numeric` 主键 / `bigint` 外键）：

- 主线：INSERT 13.5s，UPDATE 15s
- 补丁版：INSERT 8.2s，UPDATE 10.1s

### v3：Amit 的重构与按语句缓存（2026 年 2 月）

Amit 将 Junwang 的补丁重构成两个补丁：

- **0001**：功能完整的快速路径。包含并发处理、`REPEATABLE READ` 交叉检查、跨类型操作符、安全上下文（RLS/ACL）及元数据缓存。主要逻辑集中在 `ri_FastPathCheck()`；`RI_FKey_check` 仅负责分支判断并在需要时回退到 SPI。

- **0002**：按语句的资源缓存。不共享 `trigger.c` 与 `ri_triggers.c` 的 `EState`，而是引入新的 **AfterTriggerBatchCallback** 机制，在每次触发器执行周期结束时调用。借此，可在单一周期内缓存主键关系、索引、扫描描述符和快照，从而在多次 FK 触发器调用之间复用，而不是每行都打开和关闭。

Amit 的基准测试：

| 场景 | 主线 | 0001 | 0001+0002 |
|------|------|------|-----------|
| 100 万行，numeric/bigint | 2444 ms | 1382 ms（快 43%） | 1202 ms（快 51%） |
| 100 万行，int/int | 1000 ms | 520 ms（快 48%） | 432 ms（快 57%） |

0002 的额外收益（约 13–17%）来自消除每行的关系打开/关闭、扫描开始/结束、槽分配/释放，并将每行的 `GetSnapshotData()` 替换为缓存中的快照副本。

## 设计：何时走快速路径，何时走 SPI

快速路径适用条件：

- 被引用表**非分区**
- 约束**不**涉及 temporal semantics（范围重叠、`range_agg()` 等）
- 多列键、跨类型相等（通过索引操作符族）、排序规则匹配、RLS/ACL 均在快速路径内处理

在以下情况回退到 SPI：

1. **并发更新或删除**：若 `table_tuple_lock()` 报告目标元组已被更新或删除，则委托给 SPI，由 `EvalPlanQual` 和可见性规则按现有逻辑处理。
2. **分区被引用表**：需要通过 `PartitionDirectory` 将探测路由到正确分区，可后续单独补丁支持。
3. **Temporal foreign keys**：使用范围重叠和包含语义，本质上涉及聚合，保留在 SPI 路径。

安全行为与现有 SPI 路径一致：快速路径在探测时临时切换到父表所有者，使用 `SECURITY_LOCAL_USERID_CHANGE | SECURITY_NOFORCE_RLS`，与 `ri_PerformCheck()` 保持一致。

## 后续方向

**David Rowley** 在私下交流中建议，将多个 FK 值批量为单次索引探测可进一步提升性能，利用 PostgreSQL 17 的 `ScalarArrayOp` 对 btree 的改进。思路：在按约束的缓存中跨触发器调用缓冲 FK 值，构建 `SK_SEARCHARRAY` 扫描键，让 btree AM 在一次有序遍历中扫描匹配的叶页，而不是每行一次树下降。加锁和重检查仍按元组进行。可作为独立补丁在现有系列之上探索。

## 当前状态

- 补丁系列位于 PG19-Drafts。Amit 于 2025 年 10 月移入；Junwang Zhao 正在继续推进。
- Amit 的 v3 补丁（2026 年 2 月）已基本成型，等待审查。欢迎反馈，尤其是关于 `ri_LockPKTuple()` 中的并发处理及 0002 中快照生命周期的意见。
- Pavel Stehule 表示愿意协助测试和审查。

## 结论

对简单外键检查消除 SPI 调用，可为批量操作带来可观的性能提升。混合快速路径 + 回退设计回应了审查者对正确性的关切：在正确性依赖执行器复杂行为时回退到 SPI。v3 中的按语句资源缓存进一步优化，将关系/索引的建立成本分摊到单一触发器执行周期内的多行上。

对于具有大量外键的批量插入或更新场景——常见于 ETL、暂存加载、数据迁移 —— 该工作有望显著缩短运行时间。当前限制（分区主键、时态外键）使这些场景仍走现有路径，在保证正确性的同时优化大多数 FK 工作负载。

## 参考资料

- [讨论串：Eliminating SPI / SQL from some RI triggers - take 3](https://www.postgresql.org/message-id/flat/CA%2BHiwqF4C0ws3cO%2Bz5cLkPuvwnAwkSp7sfvgGj3yQ%3DLi6KNMqA%40mail.gmail.com)
- [1] Simplifying foreign key/RI checks（早期讨论串）
- [2] Eliminating SPI from RI triggers - take 2（早期讨论串）
