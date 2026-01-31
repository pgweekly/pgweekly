# GOO：面向大规模连接问题的贪心连接顺序搜索算法

## 引言

PostgreSQL 根据查询复杂度采用不同的连接顺序策略：在关系数少于 `geqo_threshold`（默认 12）时使用**动态规划**（DP）求最优连接顺序；当连接图更大时则启用 **GEQO**（遗传查询优化器），用遗传算法在连接顺序空间中做启发式搜索。GEQO 存在一些已知问题：在中等规模连接数下规划时间可能比 DP 还慢，且缺少类似可复现种子的调优手段。

2025 年 12 月，Chengpeng Yan 在 pgsql-hackers 邮件列表中提出了 **GOO（Greedy Operator Ordering）**——一种确定性的贪心连接顺序搜索方法，旨在作为 GEQO 的替代方案处理大规模连接。算法基于 Leonidas Fegaras 在 1998 年 DEXA 上的论文《A New Heuristic for Optimizing Large Queries》。

## 为什么值得关注

- **规划时间**：在星型/雪花型及 TPC-DS 类负载上，GOO 的规划时间明显短于 GEQO（例如某次测试中，对 99 条 TPC-DS 查询做 EXPLAIN：GOO 约 5s，GEQO 约 20s），而在连接数未超阈值时 DP 仍然最快。
- **计划质量**：目标是在当前使用 GEQO 的场景下做到「足够好」——减少极端退化、行为更可预期。
- **内存**：GOO 的迭代、逐步合并结构理论上比完整 DP 更省内存，与 GEQO 的特性也不同；作者计划补充测量。

了解这一讨论有助于把握 PostgreSQL 在多表连接场景下的演进方向，以及 GOO 与 DP、GEQO 的定位关系。

## 技术分析

### GOO 算法原理

GOO 以增量方式构建连接顺序：

1. 初始时每个基表对应一个「团块」（clump, geqo 中的概念）。
2. 每一步在所有**合法**连接对（满足查询连接约束的团块对）中评估。
3. 对每一对构造连接关系，用规划器现有代价模型得到总代价。
4. **选择估计总代价最低的一对**，合并为一个团块，重复上述过程。
5. 直到只剩一个团块，其最优路径即为最终计划。

因此「贪心」体现在：在每一步，在所有当前团块中，只做当前看起来代价最小的连接。原论文按估计结果大小排序；补丁中为与现有规划器一致，采用规划器的 `total_cost`。

**复杂度**：时间为 O(n³)，n 为基表数量：共 (n−1) 次合并，每步约 O(k²) 个团块对。

### 与规划器的集成

补丁引入：

- **`enable_goo_join_search`**：GUC，用于开启 GOO（默认关闭）。
- **阈值**：目前复用 `geqo_threshold`；当连接层数 ≥ `geqo_threshold` 且开启 GOO 时，规划器调用 `goo_join_search()` 而非 GEQO。因此 GOO 的定位是**替代 GEQO**，不替代 DP。

`allpaths.c` 中的相关逻辑：

```c
else if (enable_goo_join_search && levels_needed >= geqo_threshold)
    return goo_join_search(root, levels_needed, initial_rels);
else if (enable_geqo && levels_needed >= geqo_threshold)
    return geqo(root, levels_needed, initial_rels);
```

### 实验中的贪心策略

作者用不同指标作为「最便宜」的贪心依据做了对比：

| 策略         | 含义 |
|--------------|------|
| **cost**     | 使用规划器对该连接的 `total_cost`（基线）。 |
| **result_size** | 使用估计输出大小（字节）：`reltarget->width * rows`。 |
| **rows**     | 使用估计输出行数。 |
| **selectivity** | 使用连接选择性（输出行数 / (左表行数 × 右表行数)）。 |
| **combined** | 分别按 cost 和 result_size 各跑一次 GOO，再选最终估计代价更低的计划。 |

结论概括：

- 仅用 **cost** 会出现严重长尾（如 JOB 上最大 431 倍、大量 ≥10 倍退化）。
- **result_size** 平均更好，但仍有差的长尾（如 JOB 上最大 67 倍）。
- **combined**（cost + result_size 各生成一计划再选更便宜的）在鲁棒性上最好：几何平均更优、JOB 上无 ≥10 倍退化、最坏约 8.68 倍。

因此讨论重点从「选哪种单一指标」转向「如何降低长尾风险」——例如通过多种贪心策略并行、再选更优计划。

## 社区讨论要点

### 基准测试说明

Dilip Kumar 最初质疑用 pgbench 测连接顺序是否合适。作者澄清：数据来自 **Tomas Vondra 此前线程中的自定义星型/雪花型负载**，而非默认 pgbench；这些负载包含多表连接且表为空，因此吞吐主要反映 **规划时间**（DP / GEQO / GOO 的差异）。

### 崩溃与「提前聚合」修复（v1 → v2）

Tomas Vondra 在 TPC-DS 查询上跑 EXPLAIN 时遇到崩溃，堆栈显示 `sort_inner_and_outer()` 中 `inner_path = NULL`。根因是：GOO 在某些路径下构造连接关系时未调用 `set_cheapest()`，导致后续代码读到空的 cheapest 路径。Tomas 进一步将问题缩小到 **SELECT 列表中含聚合** 的查询（如 TPC-DS Q7）。

**v2** 的修复是正确处理 **eager aggregation**：规划器会生成需要正确 cheapest 路径的分组/基表关系。修复后，99 条 TPC-DS 查询均可正常完成规划。

### GEQO 在中等连接数下比 DP 更慢

Tomas 给出了 99 条 TPC-DS 查询的 EXPLAIN 耗时（3 种 scale × 0/4 worker）：

- **master (DP)**：8s
- **master/geqo**：20s
- **master/goo**：5s

说明在该负载下 GEQO 比 DP 慢，GOO 最快。John Naylor 和 Pavel Stehule 指出：GEQO 的设计本就是在连接规模足够大时才占优；在连接数较小或中等时，其固定开销可能占主导。因此评估应聚焦在真正会启用 GEQO 的范围（例如关系数超过 `geqo_threshold`）。

### TPC-DS 执行结果（Tomas Vondra）

Tomas 分享了完整 TPC-DS 运行结果（scale 1 和 10，0 和 4 worker）。99 条查询总耗时概括：

- **Scale 1**：GOO 比 master 和 GEQO 都慢（例如约 1124s vs GEQO 399s vs master 816s）。
- **Scale 10**：GOO 更快（例如约 1859s vs GEQO 2325s vs master 2439s）。

说明 GOO 在小数据量下表现更差、在较大数据量下更好，行为与负载和规模相关。Tomas 建议分析变慢的查询以改进启发式，并增加更大数据量和冷缓存测试。

### TPC-H：单一指标贪心的失效模式（v3）

作者在 TPC-H SF=1 上对比了四种策略：rows、selectivity、result_size、cost。主要结论：

- **Q20**：`partsupp` 与聚合后的 `lineitem` 子查询连接；行数估计严重偏低（估计几十行、实际数十万）。面向输出的规则（rows、selectivity、result_size）会因「看起来」极度收缩而非常早地选这个连接；实际却产生巨大中间结果并放大后续代价。**估计错误**会使面向输出的贪心规则表现很差。
- **Q7**：基于 cost 的贪心选了一个局部很便宜的连接，却形成大的多对多中间结果，使后续连接代价激增。说明**局部最优代价**可能**全局很差**。

Tomas 指出：Q20 本质是估计问题（垃圾进垃圾出）；Q7 则是贪心算法固有的——局部最优导致全局次优，换单一指标无法从根本上解决。

### JOB 与组合策略（v4）

在完整 JOB 负载上，**combined** 策略（cost + result_size 各跑一次，选代价更低的计划）表现如下：

- 几何平均最优（相对 DP 为 0.953）。
- 无 ≥10 倍退化，最大约 8.68 倍。
- 比单独 GOO(cost) 或 GOO(result_size) 的尾部好很多。

通过两种贪心策略并行、再选更优计划，可以在几乎不增加规划成本的前提下减少灾难性计划。

### 定位：GOO 作为 GEQO 替代

Tomas 询问目标是替代 DP 还是 GEQO。作者明确：**GOO 的定位是替代 GEQO**，不替代 DP；在连接数低于 `geqo_threshold` 时仍应使用 DP。

### 文献与后续方向

Tomas 提到 CIDR 2021 的 "Simplicity Done Right for Join Ordering"（Hertzschuch 等），该工作强调鲁棒性（如最坏情况/上界连接顺序）和仅信任基表估计，对基数估计过于乐观导致的 nestloop 爆炸可能有参考价值。作者计划先用当前方案打好基线，再逐步吸收这类思路。

## 实现与细节

### 实现方式

- 新增：`src/backend/optimizer/path/goo.c`、`src/include/optimizer/goo.h`。
- GOO 通过反复调用现有规划接口（如 `make_join_rel`、路径生成）构建连接关系，因此与 DP/GEQO 共用同一代价模型和路径类型。
- 使用多个内存上下文以在候选评估阶段控制内存占用。

### 边界与鲁棒性

- **Eager aggregation**：v2 通过保证 GOO 生成的连接关系都正确设置 cheapest 路径，修复了 `sort_inner_and_outer` 遇到 NULL inner_path 的崩溃。
- **基数估计错误**：所有方法在估计严重偏差时都会受影响；GOO 对不同策略的敏感度不同（例如面向输出的规则在行数估计错误时可能更糟）。combined 策略的目标是降低长尾，而非从根本上修正估计。
- **结构局限**：某些图结构（如星型、扇出）会使 cost 和 result_size 都选到类似的差计划，这是「只看一步」的贪心枚举的固有局限。

### 性能相关

- **规划时间**：GOO 为 O(n³)，在已报告的基准中通常快于 GEQO；作者计划补充规划时间和内存的显式测量。
- **执行时间**：高度依赖负载；GOO 相对 GEQO/DP 可能更好或更差（如 TPC-DS 的 scale 1 与 10、JOB 中涉及 GEQO 的子集）。

## 当前状态

- **补丁**：讨论截止时最新为 v4。v4-0001 为核心 GOO 实现（与 v3-0001 一致）；v4-0002 为测试用 GUC 及多策略（如 combined）的实验框架。
- **目标**：将 GOO 确立为可行的 GEQO 替代——相同阈值下，计划质量和规划时间不逊于或优于 GEQO，并减少极端退化。
- **后续**（作者计划）：在更多负载、更大连接图、冷缓存、更大 scale 上评估；考虑在组合策略中加入 selectivity；测量规划时间与内存；探索可调参数与渐进降级（如资源受限时先用 DP 再退化为贪心）。

## 小结

GOO 邮件线程展示了一种用确定性贪心连接顺序算法替代 GEQO 的完整尝试：

- 复用现有代价模型与规划基础设施。
- 在多个基准上缩短了规划时间（相对 GEQO）。
- 通过组合多种贪心策略（如 cost 与 result_size）并选取更优计划，改善了最坏情况下的计划质量。

同时承认局限：贪心本质是局部决策，在估计错误或不利的连接图结构下仍可能产生差计划；当前重点放在**鲁棒性与尾部行为**而非单一指标的极致调优。对 PostgreSQL 用户而言，这是值得关注的补丁：若被采纳，将在复杂多表连接场景下提供除 GEQO 之外的另一种选择，并在可预测性和规划性能上可能带来改进。

### 参考

- [1] **讨论线程**：[Add a greedy join search algorithm to handle large join problems](https://www.postgresql.org/message-id/3FF63E99-AB4F-41A9-BC78-AAB28823FBD0%40Outlook.com)（pgsql-hackers，2025 年 12 月 – 2026 年 1 月）
- [2] Leonidas Fegaras, "A New Heuristic for Optimizing Large Queries", DEXA '98. [ACM](https://dl.acm.org/doi/10.5555/648311.754892) / [PostScript](https://lambda.uta.edu/order.ps)
- [3] 星型/雪花型连接讨论：[pgsql-hackers](https://www.postgresql.org/message-id/a22ec6e0-92ae-43e7-85c1-587df2a65f51%40vondra.me)
- [4] CIDR 2021 — Simplicity Done Right for Join Ordering：[VLDB](https://vldb.org/cidrdb/2021/simplicity-done-right-for-join-ordering.html)
- [5] PostgreSQL 文档：[遗传查询优化器](https://www.postgresql.org/docs/current/geqo.html)
