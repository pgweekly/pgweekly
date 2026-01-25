# PostgreSQL 查询规划器优化：自动 COUNT(*) 转换

## 引言

2025 年 10 月，PostgreSQL 提交者 David Rowley 提出了一个重要的查询规划器优化，能够自动将 `COUNT(1)` 和 `COUNT(not_null_col)` 表达式转换为 `COUNT(*)`。这个优化解决了一个常见的性能反模式：开发者认为 `COUNT(1)` 等同于 `COUNT(*)`，但实际上 `COUNT(*)` 更高效。该补丁于 2025 年 11 月提交，并引入了用于聚合函数简化的新基础设施。

## 为什么这很重要

`COUNT(*)` 和 `COUNT(column)` 之间的性能差异可能非常显著，特别是对于大表。当计算特定列时，PostgreSQL 必须：

1. **解构元组**以提取列值
2. **检查 NULL 值**（即使对于 NOT NULL 列，检查仍然会发生）
3. **通过聚合函数处理列数据**

相比之下，`COUNT(*)` 可以在不访问单个列值的情况下计算行数，从而获得显著更好的性能。David Rowley 的基准测试显示，在包含 100 万行的表上，使用 `COUNT(*)` 而不是 `COUNT(not_null_col)` 可以获得约 **37% 的性能提升**。

## 技术分析

### 基础设施：SupportRequestSimplifyAggref

该补丁引入了一个名为 `SupportRequestSimplifyAggref` 的新基础设施，类似于现有的用于常规函数表达式（`FuncExpr`）的 `SupportRequestSimplify`。由于聚合使用 `Aggref` 节点，因此需要一个单独的机制。

关键组件包括：

1. **新的支持节点类型**：`supportnodes.h` 中的 `SupportRequestSimplifyAggref`
2. **简化函数**：`clauses.c` 中的 `simplify_aggref()`，在常量折叠期间调用聚合的支持函数
3. **增强的可空性检查**：扩展 `expr_is_nonnullable()` 以处理 `Const` 节点，而不仅仅是 `Var` 节点

### 实现细节

优化在查询规划的常量折叠阶段执行，具体在 `eval_const_expressions_mutator()` 中。当遇到 `Aggref` 节点时，规划器会：

1. 检查聚合函数是否通过 `pg_proc.prosupport` 注册了支持函数
2. 使用 `SupportRequestSimplifyAggref` 请求调用支持函数
3. 如果支持函数返回简化的节点，则替换原始的 `Aggref`

对于 `COUNT` 聚合，支持函数（`int8_agg_support_simplify()`）会检查：

- 参数是否不可为空（使用 `expr_is_nonnullable()`）
- 聚合中是否没有 `ORDER BY` 或 `DISTINCT` 子句
- 如果两个条件都满足，则将 `COUNT(ANY)` 转换为 `COUNT(*)`

### 代码示例

`int8.c` 中的核心简化逻辑：

```c
static Node *
int8_agg_support_simplify(SupportRequestSimplifyAggref *req)
{
    Aggref    *aggref = req->aggref;

    /* 只处理 COUNT */
    if (aggref->aggfnoid != INT8_AGG_COUNT_OID)
        return NULL;

    /* 必须恰好有一个参数 */
    if (list_length(aggref->args) != 1)
        return NULL;

    /* 没有 ORDER BY 或 DISTINCT */
    if (aggref->aggorder != NIL || aggref->aggdistinct != NIL)
        return NULL;

    /* 检查参数是否不可为空 */
    if (!expr_is_nonnullable(req->root,
                             (Expr *) linitial(aggref->args),
                             true))
        return NULL;

    /* 转换为 COUNT(*) */
    return make_count_star_aggref(aggref);
}
```

## 补丁演进

该补丁经历了四次迭代，每次都在改进实现：

### 版本 1（初始提案）
- 引入基本基础设施
- 使用 `SysCache` 获取 `pg_proc` 元组

### 版本 2（代码清理）
- 用 `get_func_support()` 函数替换 `SysCache` 查找
- 更清晰、更高效的方法

### 版本 3（移除实验性代码）
- 移除了处理 `COUNT(NULL)` 优化的 `#ifdef NOT_USED` 块
- 清理了未使用的包含文件
- 改进了注释

### 版本 4（最终版本）
- 在提交 `b140c8d7a` 后重新基于
- 修复了支持函数总是返回 `Aggref` 的假设
- 允许支持函数返回其他节点类型（例如常量），以实现更激进的优化
- 这种灵活性使得未来的优化成为可能，例如将 `COUNT(NULL)` 转换为 `'0'::bigint`

## 社区见解

### 审查者反馈

**Corey Huinker** 提供了积极的反馈：
- +1 支持自动查询改进
- 指出我们无法教育所有人 `COUNT(1)` 是反模式，所以让它不再是反模式是正确的做法
- 确认补丁可以干净地应用且测试通过

**Matheus Alcantara** 也进行了审查和测试：
- 确认基准测试中约 30% 的性能提升
- 验证了代码放置与现有的 `SupportRequestSimplify` 基础设施一致
- +1 支持这个想法

### 设计决策

**优化的时机**：优化在常量折叠期间发生，这是规划过程的早期阶段。David 考虑过是否应该在稍后（在 `add_base_clause_to_rel()` 之后）进行，以捕获如下情况：

```sql
SELECT count(nullable_col) FROM t WHERE nullable_col IS NOT NULL;
```

但是，它必须在 `preprocess_aggref()` 之前发生，该函数将具有相同转换函数的聚合分组。当前的位置与常规函数的 `SupportRequestSimplify` 一致。

**支持函数返回类型**：基础设施允许支持函数返回 `Aggref` 以外的节点。这个设计决策使得未来的优化成为可能，例如：
- 将 `COUNT(NULL)` 转换为 `'0'::bigint`
- 对聚合进行更激进的常量折叠

## 性能考虑

该优化提供了显著的性能优势：

1. **减少元组解构**：`COUNT(*)` 不需要从元组中提取列值
2. **更少的 NULL 检查**：不需要检查单个列值
3. **更好的缓存利用率**：更少的数据移动意味着更好的 CPU 缓存使用

对于具有多列的表，性能提升可能更加显著，因为 `COUNT(column)` 可能需要解构许多列才能到达目标列。

## 边界情况和限制

优化仅在以下情况下应用：

1. 列被证明不可为空（NOT NULL 约束或常量）
2. 聚合中没有 `ORDER BY` 子句
3. 聚合中没有 `DISTINCT` 子句

**尚未**优化的情况：

- `COUNT(nullable_col)`，其中列可能为 NULL（即使在同一查询中通过 `WHERE nullable_col IS NOT NULL` 过滤）
- `COUNT(col ORDER BY col)` - ORDER BY 阻止优化
- `COUNT(DISTINCT col)` - DISTINCT 阻止优化

`WHERE` 子句的限制是由于优化的时机（在常量折叠期间，在关系信息完全可用之前）。

## 当前状态

该补丁由 David Rowley 于 2025 年 11 月 26 日**提交**。它可在 PostgreSQL master 分支中使用，并将包含在 PostgreSQL 18 中。

## 结论

这个优化代表了 PostgreSQL 查询规划器的重大改进，自动修复了常见的性能反模式，而无需更改应用程序。新的 `SupportRequestSimplifyAggref` 基础设施也为未来的聚合优化打开了大门。

对于开发者和 DBA：
- **无需操作**：优化会自动发生
- **性能优势**：使用 `COUNT(1)` 或 `COUNT(not_null_col)` 的现有查询将自动变得更快
- **最佳实践**：虽然规划器现在会优化这些情况，但 `COUNT(*)` 仍然是计算行数最清晰、最符合习惯的方式

这一变化体现了 PostgreSQL 对自动改进查询性能的承诺，减少了开发者了解每个优化细节的负担，同时仍然允许专家在需要时编写最优查询。

## 参考资料

- [讨论线程](https://www.postgresql.org/message-id/CAApHDvqGcPTagXpKfH=CrmHBqALpziThJEDs_MrPqjKVeDF9wA@mail.gmail.com)
- 相关：用于常规函数表达式的 `SupportRequestSimplify`
