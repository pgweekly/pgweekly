# 修复 PostgreSQL 17/18 的部分索引匹配回归：NullTest 常量化简问题深析

## 引言

这条 [pgsql-bugs 讨论串](https://www.postgresql.org/message-id/flat/19007-4cc6e252ed8aa54a%40postgresql.org) 起源于一个真实回归：在 PostgreSQL 16 能命中的部分索引，升级到 PostgreSQL 17 后不再被选择。示例看似“边角”（在已 `NOT NULL` 的列上写 `IS NOT NULL`），但它揭示了优化器在表达式化简路径上的一致性问题。

讨论随后从 bug 复现，演进到对 `eval_const_expressions()` 的行为修正，核心是：当表达式来自系统目录并在后续重写 `Var` 节点时，`NullTest` 的化简是否还能稳定发生。

## 技术分析

### 根因

问题的核心不在“部分索引”本身，而在表达式何时、以什么上下文做常量化简：

- 来自 relcache/系统目录的表达式（索引表达式、索引谓词、统计信息表达式、约束表达式）可能会在 `Var` 的 `varno` 修正前就被化简。
- 某些路径调用 `eval_const_expressions()` 时传入了 `root = NULL`，会抑制部分 `NullTest` 归约。
- 结果是逻辑上本应等价的表达式没能收敛为同一形态，导致谓词蕴含/匹配判断失败，优化器错过可用的部分索引。

线程里反复强调：同一逻辑表达式不应因上下文不同而出现不同化简行为。

### 补丁演进（探索版 -> v1 -> v2 -> v3 -> v4）

- **探索版补丁**（`further_eval_const_expressions_processing_on_partial_indexes.patch`）：先验证“对索引谓词追加一次化简”这一方向是否可行。
- **v1**：单补丁覆盖约束、统计信息、索引表达式、索引谓词。
- **v2**：拆分成两个补丁：
  - `0001`：处理约束与统计信息表达式。
  - `0002`：处理索引表达式与索引谓词，并覆盖 `ON CONFLICT` 推断索引路径。
- **v3**：在 `0001` 已独立入库后，重整并继续推进索引相关部分。
- **v4**：补充“额外化简开销可接受”的论证，并增强回归测试，覆盖索引表达式与索引谓词两类场景。

最终修复已推送到 `master`；但 v17/v18 是否回补，在讨论结束时仍未定案。

### SQL 示例

下面的 SQL 与补丁新增回归测试对应，可复现线程讨论的规划行为类型。注意：这些示例需要包含修复的 PostgreSQL 版本（已进入 `master`），并不对应未修复的 v17/v18 现网默认行为。

```sql
CREATE TABLE pred_tab (a int, b int NOT NULL, c int NOT NULL);
INSERT INTO pred_tab SELECT i, i, i FROM generate_series(1, 1000) i;

-- 索引谓词路径
CREATE INDEX pred_tab_pred_idx ON pred_tab (a)
WHERE b IS NOT NULL AND c IS NOT NULL;

ANALYZE pred_tab;

EXPLAIN (COSTS OFF)
SELECT * FROM pred_tab
WHERE a < 3 AND b IS NOT NULL AND c IS NOT NULL;
```

```sql
-- 索引表达式路径
CREATE INDEX pred_tab_exprs_idx
ON pred_tab ((a < 5 AND b IS NOT NULL AND c IS NOT NULL));

EXPLAIN (COSTS OFF)
SELECT * FROM pred_tab
WHERE (a < 5 AND b IS NOT NULL AND c IS NOT NULL) IS TRUE;
```

修复后的预期形态是：优化器能更一致地归约与 `NullTest` 相关的表达式，并在这些模式下选择对应的索引扫描路径。

## 社区观察

这条讨论串体现了 PostgreSQL 社区的几个典型工程取向：

- **一致性优先**：表达式化简不能依赖“碰巧走到哪个上下文”。
- **先验证正确性，再评估代价**：评审明确追问额外一次 `eval_const_expressions()` 的规划开销，补丁给出了局部且可控的成本解释。
- **分拆推进降低风险**：v2 的拆分让评审与提交路径更清晰，先落地非索引部分，再推进索引路径。
- **版本策略谨慎**：讨论明确指出版本窗口问题（旧版本无此问题、master 已修复、v17/v18 回补不确定）。

## 技术细节

实现策略可概括为：

- 在关键路径确保 `ChangeVarNodes` 先完成 `Var` 重写，再做决定性的常量化简。
- 对索引表达式和索引谓词在 varno 修正后再次调用 `eval_const_expressions(root, ...)`。
- 在 `infer_arbiter_indexes()` 中应用相同原则，确保 `ON CONFLICT` 推断索引时比较的是一致化简后的表达式树。
- 扩展回归测试，分别验证索引谓词与索引表达式场景的可用性和计划形态。

这样可以让系统目录加载出来的表达式与查询条件更稳定地收敛到同一规范形态，恢复谓词蕴含判断和索引匹配能力。

## 当前状态

该线程经历 `v1` 到 `v4` 多轮演进后，修复已合入 PostgreSQL `master`。截至线程收尾，v17/v18 的回补策略仍是开放问题。

## 结论

这个案例说明：优化器内部“表达式化简时机与上下文”的细小差异，会放大为显著的计划差异。通过统一 varno 修正与 const-simplification 的先后关系，并传入有效的 `root`，PostgreSQL 修复了受影响场景下的部分索引可用性，也降低了版本升级后的行为意外。

