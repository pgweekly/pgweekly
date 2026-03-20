# 缩短含 NULL 的大规模 NOT IN 列表的规划时间

## 引言

当查询使用 `x NOT IN (NULL, ...)` 或 `x <> ALL (NULL, ...)` 时，结果恒为空——不会有任何行匹配。在 SQL 中，NOT IN 列表中只要存在一个 NULL，整个谓词对每一行都会得到 NULL 或 false，因此选择性为 0.0。然而 PostgreSQL 优化器此前仍会遍历列表中的每个元素，并为每个元素调用算子的选择性估计函数，在大列表上浪费规划时间。

Ilia Evdokimov（Tantor Labs）提出了一项简单优化：在 `<> ALL` / NOT IN 情形下，检测数组是否包含 NULL，并提前短路选择性计算循环，直接返回 0.0。该补丁经过多轮评审，发现并修复了与「曾含 NULL 但现已不含」的数组相关的回归问题，最终由 David Rowley 于 2026 年 3 月提交。

## 为何重要

在报表和 ETL 场景中，带有大规模 `NOT IN` 或 `<> ALL` 列表的查询很常见。当这类列表包含 NULL 时（无论是有意还是来自子查询），优化器此前会做大量无用工作：

- 对于常量数组：解构数组，遍历每个元素，并调用算子的选择性函数。
- 对于非常量表达式：遍历列表元素以检查 NULL。

在 Ilia 的测试中，当列具有详细统计信息时，`WHERE x NOT IN (NULL, ...)` 的规划时间从 **5–200 ms** 降至 **约 1–2 ms**，具体取决于列表大小。该优化保持语义不变，且不会影响常见场景。

## 技术分析

### 语义

对于 `x NOT IN (a, b, c, ...)`（或 `x <> ALL (array)`）：

- 若任一元素为 NULL，谓词对每一行都会得到 NULL（在 WHERE 子句中意味着该行被过滤掉）。
- 优化器将其建模为选择性 = 0.0：无行匹配。

`src/backend/utils/adt/selfuncs.c` 中的 `scalararraysel()` 通过 `useOr` 标志同时处理 `= ANY`（IN）和 `<> ALL`（NOT IN）。对于 `<> ALL`，选择性通过遍历数组元素并组合各元素估计值来计算。当存在 NULL 时，最终结果恒为 0.0，因此该循环是多余的。

### 补丁演进

**v1** 在 `deconstruct_array()` 之后增加早期检查，使用 `memchr()` 在 `elem_nulls` 上检测任意 NULL。David Geier 提出疑问：`memchr()` 在每次调用时都会带来开销。他建议在 `ArrayType` 上增加标志位。

**v2** 改为在逐元素循环内部短路：当元素为 `Const` 且为 NULL 时，立即返回 0.0。这避免了单独一轮遍历，但仍需进入循环。

**v3** 将检查提前到 `DatumGetArrayTypeP()` 之后，使用 `ARR_HASNULL()` 在解构数组之前检测 NULL。这样在存在 NULL 时，既不需要解构数组，也不需要逐元素循环。Ilia 报告规划时间从 5–200 ms 降至约 1–2 ms。

**v4** 修复了 Zsolt Parragi 发现的回归。宏 `ARR_HASNULL()` 仅检查 NULL 位图是否存在，而非是否有元素实际为 NULL。一个最初含有 NULL、但后来通过 `array_set_element()` 等将所有 NULL 替换掉的数组，仍可能保留 NULL 位图。仅使用 `ARR_HASNULL()` 会导致对此类数组错误地返回选择性 0.0。

修复方案：使用 `array_contains_nulls()`，该函数会遍历 NULL 位图，仅在实际存在 NULL 元素时返回 true。v4 还增加了回归测试，通过 `replace_elem(ARRAY[1,NULL,3], 2, 99)` 构造数组，确保优化器对 `x <> ALL(replace_elem(ARRAY[1,NULL,3], 2, 99))` 估计 997 行（而非 0）。

**v5–v9** 采纳 David Rowley 的反馈：将测试移至新的 `selectivity_est.sql` 文件（后更名为 `planner_est.sql`），使用 `test_setup.sql` 中的 `tenk1` 而非自定义表，增加关于假定算子为 strict 的注释（与 `var_eq_const()` 一致），并简化测试以断言不变量（存在 NULL 时选择性为 0.0），而非精确行估计。

## 社区反馈

- **David Geier** 质疑 `memchr()` 的代价，并建议在 `ArrayType` 上增加标志；Ilia 发现已有 `ARR_HASNULL()` / `array_contains_nulls()`。
- **Zsolt Parragi** 发现了与「已将 NULL 替换掉」的数组相关的回归，并提出了 `replace_elem` 测试用例。
- **David Geier** 澄清：`ARR_HASNULL()` 检查的是位图是否存在，而非实际 NULL 元素；`array_contains_nulls()` 才是正确的检查。
- **David Rowley** 建议将测试移至专门的 planner 估计相关文件，使用现有测试表，并注明 strict 算子假设。他还提交了重构补丁（planner_est.sql）和主优化补丁。

## 技术细节

### 实现

该优化在 `scalararraysel()` 中增加两条短路路径：

1. **常量数组情形**：在 `DatumGetArrayTypeP()` 之后，若 `!useOr`（即 `<> ALL` / NOT IN）且 `array_contains_nulls(arrayval)`，则立即返回 0.0。这发生在 `deconstruct_array()` 之前。

2. **非常量列表情形**：在逐元素循环中，若 `!useOr` 且元素为 `constisnull` 的 `Const`，则返回 0.0。这处理了当列表来自非常量表达式时的 `x NOT IN (1, 2, NULL, ...)`。

代码假定算子为 strict（与 `var_eq_const()` 一致）：当常量为 NULL 时，算子返回零选择性。这与现有优化器行为一致。

### 边界情况

- **有 NULL 位图但无实际 NULL 的数组**：通过使用 `array_contains_nulls()` 而非 `ARR_HASNULL()` 处理。
- **非 strict 算子**：注释中说明，该短路与 `var_eq_const()` 采用相同假设。

### 相关工作

David Geier 指出，一旦[基于哈希的 NOT IN 代码](https://www.postgresql.org/message-id/flat/7db341e0-fbc6-4ec5-922c-11fdafe7be12%40tantorlabs.com)合并后，加速效果会有所减弱，但仍能在选择性估计阶段节省大量计算。

## 当前状态

该补丁由 David Rowley 于 2026 年 3 月 19 日提交。将优化器行估计测试迁移至 `planner_est.sql` 的重构先被提交，主优化随后跟进。该优化将出现在后续 PostgreSQL 版本中。

## 结论

一项小改动——在 NOT IN / `<> ALL` 列表中检测 NULL 并提前返回选择性 0.0——避免了规划阶段不必要的逐元素计算。修复需要正确处理 NULL 位图与实际 NULL 元素的区别，并得益于细致的评审和回归测试。该优化现已成为 PostgreSQL 的一部分，将惠及使用含 NULL 的大规模 NOT IN 列表的负载。
