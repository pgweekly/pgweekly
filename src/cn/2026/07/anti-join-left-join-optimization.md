# 将 LEFT JOIN 归约为 ANTI JOIN：针对 "WHERE col IS NULL" 的优化器优化

## 引言

2025 年 12 月底，Nicolas Adenis-Lamarre 在 pgsql-hackers 邮件列表中提出了一项优化器优化：当查询使用 `LEFT JOIN ... WHERE right_table.column IS NULL`，且该列在语义上非空（例如 NOT NULL 或主键）时，自动识别为**反连接（anti-join）**。这类查询的语义是“左侧有、右侧无匹配的行”，正是反连接所表达的含义。识别出该模式后，优化器可以选择显式的反连接计划（如 Hash Anti Join），而不是普通的左连接加过滤，往往能获得更好的执行效率。

讨论吸引了 Tom Lane、David Rowley、Tender Wang、Richard Guo 等人参与。补丁历经多版迭代，被提交到 CommitFest，并在代码审查中暴露出与嵌套外连接和继承相关的正确性问题。本文概述该优化的思路、实现方式以及当前状态。

## 为什么重要

很多开发者会这样写“在 A 中找在 B 中没有匹配的行”：

```sql
SELECT a.*
FROM a
LEFT JOIN b ON a.id = b.a_id
WHERE b.some_not_null_col IS NULL;
```

LEFT JOIN 会使来自 `a` 的无匹配行在 `b` 的所有列上为 NULL。当 `b.some_not_null_col` 在表 `b` 上为 NOT NULL 时，用 `WHERE b.some_not_null_col IS NULL` 过滤，留下的就是这些无匹配行。语义上这就是**反连接**：“在 A 中且不存在在 B 中匹配的行”。

若优化器不识别该模式，可能按普通左连接加过滤实现；若识别，则可以使用显式的 **Hash Anti Join** 等，执行更高效，也有利于选择更好的连接顺序。这类优化是“非强制”的——熟练用户可以把查询改写成 `NOT EXISTS` 或 `NOT IN`（并注意 NULL 语义），但自动识别能惠及所有用户，同时保留原有 SQL 的可读性。

## 技术背景

PostgreSQL 已有在部分场景下归约外连接的逻辑，例如：

- **提交 904f6a593 与 e2debb643** 引入了优化器可用于此类归约的基础设施。
- 在 `reduce_outer_joins_pass2` 中，优化器已经会在“连接自身的条件对某些被更高层条件**强制为 NULL** 的变量是严格的”时，尝试将 `JOIN_LEFT` 归约为 `JOIN_ANTI`。

该处原有注释提到，还存在其他识别反连接的方式——例如检查来自右侧的变量是否因**表约束**（NOT NULL 等）而必然非空。Nicolas 的提议与 Tender 的补丁实现的正是这一点：利用 NOT NULL 等信息，在 `WHERE rhs_col IS NULL` 能推出“无匹配”时，将 LEFT JOIN 归约为 ANTI JOIN。

## 补丁演进

### Nicolas 的初版补丁

Nicolas 提交的草稿补丁实现了：

- 在“left join b where x is null”且 `x` 为来自右侧（RTE）的非空变量时，识别该模式。
- 有意采用“快速实现”以验证可行性。

他还列举了其他想法（去掉冗余 DISTINCT/GROUP BY、合并双重 ORDER BY、对 NOT IN 做反连接、以及“查看改写后查询”的方式等），邮件中略有讨论，但非本文重点。

### Tom Lane 与 David Rowley

**Tom Lane** 指出：

- 该优化合理，且应使用新基础设施（904f6a593、e2debb643）。
- 草稿不应让周边注释过时；保持注释准确是必须的。

**David Rowley** 建议：

- 使用 `find_relation_notnullatts()`，并与 `forced_null_vars` 比较，注意 `FirstLowInvalidHeapAttributeNumber`。
- 在邮件列表中检索 **UniqueKeys** 相关历史（用于冗余 DISTINCT 消除）。
- 对“消除双重 ORDER”和“NOT IN 反连接”持谨慎态度，二者此前都有讨论且边界情况复杂。
- “查看改写后查询”含义不清，很多优化无法再表达成单一 SQL。

### Tender Wang 的实现（v2–v4）

Tender Wang 提供的补丁：

- 基于 904f6a593 和 e2debb643 的基础设施实现。
- 更新了 `reduce_outer_joins_pass2` 中的注释，说明通过右侧 NOT NULL 约束识别反连接的新情况。
- 增加了回归测试。

随后 Nicolas：

- 确认 Tender 的补丁是正确的（经重新测试）。
- 建议增加提前退出：仅当 `forced_null_vars != NIL` 时才执行新逻辑，避免在大多数没有“强制为 NULL”变量的左连接上调用 `find_nonnullable_vars` 和 `have_var_is_notnull`。
- 贡献了额外回归测试，使用**新表**（带 NOT NULL 约束），而不是修改 tenk1 等现有测试表。

**Tom Lane** 明确：不应修改通用测试对象（如 `test_setup.sql` 中的表），否则可能改变规划行为并影响其他测试。新测试应使用新表或已有且属性合适的表。

Tender 将 Nicolas 的提前退出与回归测试合并为 **v4** 单补丁，并提交到 [CommitFest](https://commitfest.postgresql.org/patch/6375/)。

## Richard Guo 的审查：正确性问题

Richard Guo 对 v4 的审查发现了两个正确性问题。

### 1. 嵌套外连接

当左连接的右侧本身又包含外连接时，即使某列在其基表上为 NOT NULL，在连接结果中仍可能为 NULL。此时将该外连接归约为反连接会出错。

例如（表 `t1`、`t2`、`t3`，列如 `(a NOT NULL, b, c)`）：

```sql
EXPLAIN (COSTS OFF)
SELECT * FROM t1
LEFT JOIN (t2 LEFT JOIN t3 ON t2.c = t3.c) ON t1.b = t2.b
WHERE t3.a IS NULL;
```

这里 `t3.a` 在 `t3` 上为 NOT NULL，但由于内层 `t2 LEFT JOIN t3`，来自 `t1` 的一行在与子查询连接后仍可能使 `t3.a` 为 NULL（当在 `t3` 中无匹配时）。因此上层连接必须保持为左连接；若错误地转为反连接，会错误地丢弃行。

补丁在判断“非空列”时没有考虑该变量是否会被下层外连接变为 NULL。Richard 指出当前 `forced_null_vars` 中并未记录 `varnullingrels`，一种简单修复是仅当右侧无外连接（`right_state->contains_outer` 为 false）时做此优化，但这会过于保守。

他提出的方向是：在 `reduce_outer_joins_pass1_state` 中记录每个子树下**可为空的基表 relid**；在检查 NOT NULL 约束时，跳过来自这些 rel 的变量。他附上了 **v5** 补丁以说明该思路。

### 2. 继承

对继承父表而言，某些子表可能在某列上有 NOT NULL，而其他子表没有。补丁未考虑这种情况；相比嵌套外连接，这一点相对容易修复。

## 其他讨论

- **Pavel Stehule** 提醒避免在列表中 top-posting；PostgreSQL 维基有[邮件列表风格](https://wiki.postgresql.org/wiki/Mailing_Lists)说明。
- **子查询中的常量**：Nicolas 提到像 `SELECT * FROM a LEFT JOIN (SELECT 1 AS const1 FROM b) x WHERE x.const1 IS NULL` 这类情况未被处理，他认为不值得专门处理。

## 当前状态

- **v4** 补丁（含提前退出与回归测试）已提交至 CommitFest（patch 6375）。
- **Richard Guo 的 v5** 通过记录可为空的基表 rel 并收紧 NOT NULL 的使用条件，针对嵌套外连接与继承问题做了修正。
- 截至该讨论，工作仍在进行；最终是否合入以及以何种形式合入，以邮件列表与 CommitFest 为准。

## 小结

在能够证明右侧某列为非空的前提下，将 `LEFT JOIN ... WHERE rhs_not_null_col IS NULL` 自动归约为反连接，是一项有用的优化器优化，可在不要求用户改写 SQL 的情况下提升性能。补丁从草稿发展到基于现有基础设施的实现，并加入了回归测试与提前退出。审查反馈指出了重要的正确性约束：右侧可能存在嵌套外连接或继承，因此只有在变量不会被下层连接或继承变为 NULL 时，才能基于 NOT NULL 做归约。后续工作集中在 Richard 的方案（记录可为空的基表 rel、限制 NOT NULL 检查）以及对继承的安全处理上。

## 参考

- [讨论串：Planner : anti-join on left joins](https://www.postgresql.org/message-id/flat/CACPGbctKMDP50PpRH09in%2BoWbHtZdahWSroRstLPOoSDKwoFsw%40mail.gmail.com)
- [CommitFest patch 6375](https://commitfest.postgresql.org/patch/6375/)
- David Rowley 提到的：[UniqueKeys](https://www.postgresql.org/search/?m=1&q=UniqueKeys&l=1&d=-1&s=d)、[NOT IN / 反连接](https://www.postgresql.org/message-id/flat/3793.1565689764%40linux-edt6#bf4b983d5744bca153c288904c038020)
