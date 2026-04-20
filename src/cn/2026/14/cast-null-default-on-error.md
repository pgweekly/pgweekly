# 带 NULL 或 DEFAULT ON ERROR 的 CAST：错误安全类型转换的 WIP，以及为何在标准层面搁浅

## 引言

2022 年 12 月，**Corey Huinker** 在 [pgsql-hackers 邮件列表](https://www.postgresql.org/message-id/flat/CADkLM%3Dfv1JfY4Ufa-jcwwNbjQixNViskQ8jZu3Tz_p656i_4hQ%40mail.gmail.com) 上发布了一项**进行中的实现**：在 **`CAST` 上增加“出错时如何处理”**的语法，思路来自 **Vik Fearing** 的提案，并建立在当时正在推进的**错误安全用户函数（error-safe user functions）**基础设施之上。目标是类似 SQL Server 的 **`TRY_CAST`**：类型转换失败时返回 **NULL** 或**调用方给定的默认值**，而不是让整个语句报错退出。随后的讨论在**功能价值**与**在标准未定前扩展 `CAST` 语法的风险**之间形成了清晰分歧。

## 为何重要

在数据导入、ETL、以及宽松文本列场景中，部分行上的类型转换失败很常见。当前 PostgreSQL 用户往往用 PL/pgSQL 异常、`regexp_replace` 与 `CASE` 组合，或在库外预处理。若能在 SQL 层用**一等公民**的方式表达“失败则用回退值”，并与**错误安全函数**共用机制，可减少样板代码，也让优化器与类型系统能完整看到逻辑。该线程也说明：**标准化不确定性**如何**拦住语法扩展**，即便**执行器侧**已有不少可复用工作。

## 技术分析

### 提议的语法形态（Huinker 的 WIP）

补丁系列中设想的行为包括：

| 形式 | 行为 |
|------|------|
| `CAST(expr AS typename)` | 与现状相同（出错即失败）。 |
| `CAST(expr AS typename ERROR ON ERROR)` | 与未修饰的 `CAST` 相同（显式“严格”写法）。 |
| `CAST(expr AS typename NULL ON ERROR)` | 经**错误安全**路径完成转换；失败则 **NULL**。 |
| `CAST(expr AS typename DEFAULT expr2 ON ERROR)` | 对 `expr` 做错误安全转换；失败则求值并返回 **`expr2`**。 |

讨论中还提到可选的 **`FORMAT fmt`**（多用于日期/时间类转换），但**最初 WIP 未实现**。

### SQL 示例（来自线程；并非已发布版本中的功能）

作者在邮件中称下列写法在**标量**场景下可用（常量折叠与运行时）。请仅作**讨论中的示例**——需要包含实验性补丁的构建，**不能**假定在任意正式发行版上可执行。

**常量与简单数组：**

```sql
VALUES (CAST('error' AS integer));
VALUES (CAST('error' AS integer ERROR ON ERROR));
VALUES (CAST('error' AS integer NULL ON ERROR));
VALUES (CAST('error' AS integer DEFAULT 42 ON ERROR));

SELECT CAST('{123,abc,456}' AS integer[] DEFAULT '{-789}' ON ERROR) AS array_test1;
```

**按行将文本转为整数并带默认值：**

```sql
CREATE TEMPORARY TABLE t(t text);
INSERT INTO t VALUES ('a'), ('1'), ('b'), (2);
SELECT CAST(t.t AS integer DEFAULT -1 ON ERROR) AS foo FROM t;
-- 线程示例中的期望：-1, 1, -1, 2
```

### 实现要点

- **`OidInputFunctionCallSafe`** 与 **`stringTypeDatumSafe()`**：在现有 “safe” 输入路径旁提供不抛异常的转换能力。
- 为避免在 **`FuncExpr`**、**`CoerceViaIO`**、**`ArrayCoerce`** 等处重复“内层转换是否失败”的分支，WIP 扩展了 **`CoalesceExpr`**：除原有的**空值测试模式**外，增加**错误测试模式**，分别接错误安全的主表达式与对默认表达式的常规求值。
- 首帖中**未完成**：**数组到数组**转换（涉及 **`ArrayCoerce`**，改动面较大）、**域（domain）**，以及 **JIT**（相关改动在 CI 中触发了 `llvmjit_expr.c` 等问题）。

### 遗留问题与怪异行为

- 仍有一条**回归用例**失败：文本数组到整数数组、且带**默认数组**、元素级转换部分失败的情形（线程中的 `array_test2`）。
- **列名**：未起别名的 `CAST ... DEFAULT ... ON ERROR` 在内部可能显示为 **`coalesce`**，对用户不友好。

## 社区观点

- **Tom Lane**：认可**需求**，但担心**扩展 `CAST`** 与未来 **ISO SQL** 关键字布局**冲突**；若标准委员会方向未明，更倾向用**函数**或其它**非 `CAST` 语法**。
- **Andrew Dunstan**：附议（+1）。
- **Corey Huinker**：引用 **Vik Fearing** 面向 SQL 委员会的早期规范，并表示**文法可单独拆成补丁**；**检测转换失败并替换默认值**的底层能力**大致不变**。
- **Vik Fearing**：说明论文**尚未提交**给委员会，但计划在**二月初工作组会议前**提交；在**委员会定稿前不要在 PostgreSQL 里加该语法**，以免措辞再变。
- **Tom Lane**：同意——**赶不上 PostgreSQL 16**，应等委员会给出明确结论。
- **Gregory Stark（Commitfest 管理员）**：在 Commitfest 中将其标为 **Rejected**。
- **Isaac Morland**：问 **`NULL ON ERROR`** 与 **`DEFAULT NULL ON ERROR`** 是否有区别。**Huinker** 答：**实现上无实质区别**，两者都落到常量 NULL；**`NULL ON ERROR`** 可视为在更复杂的 **`DEFAULT expr`** 之前的简写。

## 技术细节

后续工具链（如 **CFBot**）曾报告 **`llvmjit_expr.c`** 中与错误标志、空值标记相关的 **JIT 编译错误**，说明 **LLVM IR 生成**必须与解释器路径在错误/空值语义上**严格对齐**。

2025 年 7 月，**jian he** 汇总了 **master 上已落地的铺垫工作**：SQL/JSON 对 **`ERROR`** 的保留、**`CoerceViaIo` / `CoerceToDomain`** 的错误安全求值、以及 **`ExprState`** 上的 **`ErrorSaveContext`**（在初始化表达式前为软错误求值准备上下文）。他分享了基于旧补丁的 **POC**，包括**域覆盖复合类型**等例子，并引用 **Oracle** 文档中的 **`CAST(... DEFAULT ... ON CONVERSION ERROR)`**，同时根据 **Peter Eisentraut** 对 **SQL:2023** 的综述，指出该 **`CAST` 形态未必已出现在 SQL:2023 文本中——**标准与厂商实现**仍可能长期并存、各有差异。

## 现状

按 **2023** 年列表上的共识，**语法层面**需等待 **SQL 委员会**产出；Commitfest 状态与此一致。**Corey Huinker** 表示会在**委员会有可跟进的结论**后再继续。**2025** 的跟进说明**核心基础设施**在独立推进；**面向用户的 `CAST ... ON ERROR` 语法**在 PostgreSQL 中**仍未作为该线程所述形态发布**。

## 结语

该线程把问题拆得很清楚：**错误安全求值**对转换、域、JSON、执行器路径都有广泛用途；而 **`CAST` 语法**则涉及 **ISO SQL** 与**项目流程**层面的风险。若读者今天需要 **TRY_CAST** 式行为，仍多依赖**应用层**或 **PL/pgSQL**；本讨论解释了为何看似“加一个 `CAST` 子句”的改动，会**等待标准与治理结论**，而不仅是代码实现。

## 参考

- [线程（flat）](https://www.postgresql.org/message-id/flat/CADkLM%3Dfv1JfY4Ufa-jcwwNbjQixNViskQ8jZu3Tz_p656i_4hQ%40mail.gmail.com)
- [Vik Fearing 的早期规范邮件](https://www.postgresql.org/message-id/f8600a3b-f697-2577-8fea-f40d3e18bea8@postgresfriends.org)（线程中引用）
- [SQL:2023 概览（Peter Eisentraut）](https://peter.eisentraut.org/blog/2023/04/04/sql-2023-is-finished-here-is-whats-new)
- [Oracle CAST 文档](https://docs.oracle.com/en/database/oracle/oracle-database/23/sqlrf/CAST.html)
