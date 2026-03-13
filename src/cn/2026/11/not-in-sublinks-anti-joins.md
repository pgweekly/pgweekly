# 将 NOT IN 子链接安全地转换为 ANTI JOIN

## 引言

2026 年 2–3 月，Richard Guo 在 pgsql-hackers 邮件列表上提出并迭代了一个优化器补丁，用于在**语义安全**的前提下，将

```sql
... WHERE expr NOT IN (SELECT subexpr FROM ...)
```

自动转换为基于连接的 **ANTI JOIN**。`NOT IN` 与 `NULL` 相关的语义长期以来都非常棘手，稍有不慎就会改变查询结果，因此过去的尝试大多被搁置。

这一次，补丁充分利用了近年来在优化器中新增的基础设施：支持外连接可空信息的 `Var` 表达、全局的 not-null-attnums 哈希表，以及更智能的非空性推理。补丁从 v1 演进到 v6，期间 wenhui qiu、Zhang Mingli、Japin Li、David Geier 等多位开发者参与了审查和讨论。最终版本在 2026 年 3 月由 Richard 提交。

本文将解释为什么 `NOT IN` 难以优化、优化器如何证明“安全”、补丁从 v1 走到 v6 的关键变化，以及这对日常查询意味着什么。

## 为什么 NOT IN 很难

表面看起来，`NOT IN` 好像就是一个反连接：

```sql
SELECT *
FROM users
WHERE id NOT IN (SELECT user_id FROM banned_users);
```

直觉上这代表“没有被封禁的用户”，理想的执行计划自然是对 `banned_users` 做一个哈希 ANTI JOIN。问题在于 **SQL 对 `NULL` 的定义**。

在 `NOT IN` 中，标量比较 `(A = B)` 的行为是：

- `(A = B)` 为 `TRUE`：说明命中，`NOT IN` 条件失败。
- `(A = B)` 为 `FALSE`：当前元素不是匹配项，整体结果取决于其他元素。
- `(A = B)` 为 `NULL`：`NOT (NULL)` 仍然是 `NULL`，在 `WHERE` 中等价于 false，该行会被**丢弃**。

而在 ANTI JOIN 中，如果连接条件对某一行对返回 `NULL`，执行器往往会把它当作“没有匹配”来处理，从而可能保留外侧行。正如 Richard 在邮件中指出的，只要比较算子在某些“被视为合法等值比较”的输入上可能返回 `NULL`，`NOT IN` 和简单 ANTI JOIN 的语义就不一致。

历史上，正是这种语义差异让优化器一直避免把 `NOT IN` 子链接转换为 ANTI JOIN。想要连接计划的用户通常需要自己改写为 `NOT EXISTS` 等形式。

## 优化器基础设施：证明“不会为 NULL”

近几个版本中，PostgreSQL 引入了一些可以更可靠、低成本地证明“表达式不会为 NULL”的基础设施：

- **支持外连接可空信息的 `Var`**：记录某个变量是否可能被外连接置为 NULL。
- **not-null-attnums 哈希表**：跟踪表定义中的 `NOT NULL` 列（包括主键等隐含非空约束）。
- `expr_is_nonnullable()`：可以对复杂表达式（不仅是简单 `Var`/`Const`）进行非空性推理。
- `find_nonnullable_vars()`：可以从条件（例如 `col IS NOT NULL`，或严格算子的连接条件）中推导出被强制非空的变量。

在新补丁中，Richard 利用这些能力回答两个关键问题：

1. **比较两边的表达式是否可能为 `NULL`？**
   - 利用表级 NOT NULL 信息和外连接可空的 `Var` 元数据，排除来自外连接“可空侧”的 `Var`。
   - 结合 `find_nonnullable_vars()` 与安全的条件表达式，识别被 `WHERE`/`ON` 条件强制为非空的值。
2. **比较算子本身在非空输入上是否可能返回 `NULL`？**
   - 查询系统目录，限制只允许属于 **btree 或 hash 操作符族** 的算子，因为这些算子的行为必须满足“正常的全序或等值语义”。如果此类算子在非空输入上返回 `NULL`，依赖它的索引本身就会被破坏。

只有在**两侧表达式都被证明不会为 `NULL`，且比较算子被认为“不会在非空输入上返回 NULL”** 时，规划器才会考虑将 `NOT IN` 子链接改写为 ANTI JOIN。

## 从 v1 到 v6：不断收紧安全边界

补丁并不是一蹴而就的，邮件线程很详尽地记录了它的演进过程：

- **v1**：
  - 实现了基本的转换逻辑。
  - 重点使用已有工具证明比较两侧都是非空的。
  - 但尚未检查“算子本身是否可能在非空输入上返回 `NULL`”。

- **围绕算子安全性的讨论**：
  - Richard 意识到仅要求操作数非空还不够，算子本身也可能返回 `NULL`。
  - 他提出能否识别“在非空输入上永不返回 `NULL`”的算子，并建议把“属于 btree 操作符类”作为一个近似条件。
  - David Geier 指出，执行器中大量代码假定比较算子不会返回 `NULL`——例如 `FunctionCall2()` 一旦拿到 `NULL` 返回值就会抛错。因此，把范围限定在内置的 B-tree / hash 算子是合理且安全的。

- **v2–v4**：
  - 增加了“算子必须是 B-tree 或 hash 操作符族成员”的检查。
  - 明确和完善了注释，并补充更多回归测试，覆盖子查询输出来自外连接“可空侧”但又被 `WHERE` 条件强制非空的情况。
  - 针对测试用例中的注释和一些边界情况做了小幅修正。

- **v5–v6**：
  - 进一步打磨内部辅助函数，包括用于检查 `SubLink` 测试表达式非空性的 `sublink_testexpr_is_not_nullable`。
  - 改进对行比较表达式 (`RowCompareExpr`) 的支持，让多列 `NOT IN` 模式同样可以受益。
  - 将多处 `foreach` 改写成 `foreach_ptr` / `foreach_node`，在开发构建中获得更强的类型检查。
  - 修复了 `query_outputs_are_not_nullable()` 中一个细微但重要的问题：在对分组表达式和连接别名 Var 做“展开”时，先后顺序必须与解析器处理 FROM/JOIN 与 GROUP BY 的顺序一致。
  - 补充和整理了回归测试后，进行了一轮自审，最后宣布准备提交。

到了 v6，被提交的版本已经在语义上足够保守、测试覆盖充分，并吸收了多轮审查反馈。

## 补丁实现了什么？

在高层上，当优化器看到一个标准 ANY/ALL 形式的 `NOT IN` 子链接时，会：

1. **识别模式**：在 `SubLink` 及其 `testexpr` 中识别出 `expr NOT IN (SELECT ...)`。
2. **收集外层表达式**：即比较左侧（外查询）的表达式列表。
3. **检查算子安全性**：所有参与比较的算子都必须属于某个 B-tree 或 hash 操作符族。
4. **证明操作数非空**：借助表级 NOT NULL 信息、外连接可空 `Var` 元数据，以及从条件中推导出的“非空变量”集合，证明外层表达式与子查询输出都不会为 `NULL`。
5. **在且仅在上述条件全部满足时，将 NOT IN 子链接改写为 ANTI JOIN**。

完成改写之后，优化器就可以：

- 把原本“像黑盒子一样的子计划”拉进全局连接树。
- 在整个连接顺序中自由移动该子查询。
- 根据代价选择最合适的连接算法（哈希 ANTI JOIN、归并 ANTI JOIN 等）。

对使用者而言，收益是：很多用 `NOT IN` 写出来的排除模式，现在可以自动得到与精心写成 `NOT EXISTS` 或显式 ANTI JOIN 类似的执行计划，而不需要手工改写 SQL。

## 示例：典型的排除查询

补丁主要面向如下“教科书式”的写法：

```sql
SELECT *
FROM users
WHERE id NOT IN (SELECT user_id FROM banned_users);
```

以及：

```sql
SELECT *
FROM users
WHERE id NOT IN (
  SELECT user_id
  FROM banned_users
  WHERE user_id IS NOT NULL
);
```

在设计良好的模式中，`users.id` 和 `banned_users.user_id` 通常都是 `NOT NULL`，并且使用标准的等号比较。在这种场景下，规划器可以证明：

- 比较两侧都不可能为 `NULL`。
- 所使用的等号算子是标准 B-tree/hash 等值算子。

此时 `NOT IN` 子链接会被改写为 ANTI JOIN，执行计划就可以是：

- 针对 `banned_users` 的 **Hash Anti Join**，或者
- 在有合适索引且代价模型更倾向合并策略时，使用 **Merge Anti Join**。

线程中还包含了 wenhui qiu 提供的大规模压测脚本，展示了在相关列被标记为 `NOT NULL` 之后，新优化如何在合成数据上自动产生高效的 ANTI JOIN 计划。

## 社区讨论与作用范围

邮件中也讨论了“优化应当走多远”的问题：

- David Geier 描述了一些更激进的改写方式：在外层添加 `IS NOT NULL` 谓词，再加额外的 `NOT EXISTS` 子查询，从而覆盖“任一侧可为 NULL”更多情况。Richard 逐一给出反例，指出其中某些改写在子查询为空时会改变结果，并明确这些都**超出了本补丁的范围**。
- 邮件也简要提到，未来也许可以增加类似 Oracle 的**“感知 NULL 的 ANTI JOIN 执行节点”**，以执行层面的新算子来支持更多 `NOT IN` 场景，而不是完全依赖语法层改写。这被认为是后续可以探索的方向。
- 审查者们多次强调：必须采取**保守策略**——宁可错过一些理论上的优化机会，也不能冒着改变查询结果的风险。

最终版本刻意聚焦在**“高收益的基本形态”**：比较两边都可被证明非空，且使用标准 B-tree/hash 比较算子。这覆盖了绝大多数现实中的 `NOT IN` 排除查询，同时在代码复杂度和风险之间取得平衡。

## 当前状态

截至 2026 年 3 月中旬：

- “Convert NOT IN sublinks to anti-joins when safe” v6 补丁已经被**提交**。
- 当优化器能够证明安全时，该优化会自动启用。

在实践中，这意味着：如果你在 `NOT NULL` 键上使用内置比较算子写出常见的 `NOT IN` 排除查询，PostgreSQL 现在可以自动为你生成 ANTI JOIN 计划。只要模式和约束准确反映了非空性，应用端 SQL 无需做任何修改。

## 对用户的启示

- **尽量正确声明 NOT NULL 和主键约束。** 模式越准确地表达“哪些列不允许为 NULL”，优化器就越有机会安全地应用此类优化。
- **在可为 NULL 的列上使用 `NOT IN` 仍然很危险。** PostgreSQL 在这些场景下仍会保守行事；如果你需要在包含 `NULL` 的数据上获得可预期的行为，`NOT EXISTS` 往往更适合。
- **对于典型的排除查询，不必再为了“拿到 ANTI JOIN 计划”而主动改写为 `NOT EXISTS`。** 在满足安全条件时，优化器会自动完成改写，你可以继续使用语义上更直观的 `NOT IN` 写法。

## 参考

- [讨论串：Convert NOT IN sublinks to anti-joins when safe](https://www.postgresql.org/message-id/CAMbWs495eF=-fSa5CwJS6B-BaEi3ARp0UNb4Lt3EkgUGZJwkAQ@mail.gmail.com)
