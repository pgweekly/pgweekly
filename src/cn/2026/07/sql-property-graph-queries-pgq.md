# SQL/PGQ：为 PostgreSQL 引入图查询能力

## 引言

2024 年 2 月，Peter Eisentraut 在 pgsql-hackers 邮件列表中宣布了 **SQL 属性图查询（SQL/PGQ）** 的原型实现——一种在 PostgreSQL 中直接执行图风格查询的新方式，遵循 SQL:2023 标准（ISO 9075-16）。该提议曾在 FOSDEM 开发者会议上被简要讨论，社区的兴趣促使 Peter 将进行中的工作整理并分享出来。

近两年后，补丁已演进为体量可观的实现：**118 个文件变更**，约 **14,800 行**新增代码。Peter 和 Ashutosh Bapat 是主要作者，Junwang Zhao 参与审阅，Ajay Pal 和 Henson Choi 参与测试。最新版本（v20260113）整合了从 v0 到 v14 及之后的全部功能——包括循环路径模式、访问权限、RLS 支持、图元素函数（`LABELS()`、`PROPERTY_NAMES()`）、多模式路径匹配、ECPG 支持、属性排序规则以及 `pg_overexplain` 集成。

SQL/PGQ 让你可以在现有关系表上定义属性图，并用路径模式（由边连接的顶点）进行查询，类似 Cypher 或 GQL。与专用图数据库不同，该方案将图映射到关系模型上：图是表的**视图**，图查询被改写为连接与并集。讨论中提出了重要的架构问题：这种转换应在 PostgreSQL 的**何时**、**以何种方式**完成——以及改写器是否是合适的实现位置。

## 背景与意义

许多应用的数据天然具有图结构：社交网络、供应链、推荐系统、反欺诈等。目前开发者通常需要：

- 使用独立的图数据库（如 Neo4j），维护两套系统，或
- 在 PostgreSQL 中用递归 CTE 和复杂连接来编码图遍历。

SQL/PGQ 旨在为 PostgreSQL 用户提供一种标准的、声明式的方式来表达图查询，而无需脱离 SQL 或复制数据。该标准已被 Oracle 23c 等采用；将其引入 PostgreSQL 将提升互操作性，并使图能力惠及更广泛的用户。

## 技术分析

### SQL/PGQ 模型

SQL/PGQ 中的**属性图**是在已有表上定义的虚拟结构：

- **顶点**：来自一个或多个表的行（可带可选标签）。
- **边**：关系，通常由外键推断或显式指定。
- **属性**：来自这些表的列。
- **标签**：可在多个元素表间共享（例如 `person` 同时用于 `customers` 和 `employees`），每个标签暴露自己的属性集合。

使用 `CREATE PROPERTY GRAPH` 创建图，用 `GRAPH_TABLE(... MATCH ... COLUMNS ...)` 查询。示例：

```sql
CREATE PROPERTY GRAPH myshop
    VERTEX TABLES (
        products LABEL product,
        customers LABEL customer,
        orders LABEL "order"
    )
    EDGE TABLES (
        order_items SOURCE orders DESTINATION products LABEL contains,
        customer_orders SOURCE customers DESTINATION orders LABEL has_placed
    );

SELECT customer_name FROM GRAPH_TABLE (myshop
  MATCH (c IS customer)-[IS has_placed]->(o IS "order" WHERE o.ordered_when = current_date)
  COLUMNS (c.name AS customer_name));
```

`MATCH` 子句描述路径模式；实现会将其改写为 PostgreSQL 可以规划和执行连接与过滤。

### 实现方式：改写系统

实现采用**改写系统**完成图到关系的转换——与视图展开处于同一阶段。Peter 解释，这与 SQL/PGQ 规范一致：图映射到关系，查询像视图定义一样被展开。当优化器看到查询时，图结构已被展开为标准关系形式，从而与视图安全（权限、安全屏障）保持一致。

## 补丁演进：从 v0 到 v20260113

### v0：脆弱原型（2024 年 2 月）

初始补丁约 332 KB。Peter 称其「相当脆弱」。它引入了 `CREATE PROPERTY GRAPH`、`GRAPH_TABLE`、基本路径模式，以及 `ddl.sgml` 和 `queries.sgml` 中的文档。

### v1 与早期完善（2024 年 6–8 月）

Peter 与 Ashutosh 发布了 v1，具备「相当完整的最小功能集」。Ashutosh 贡献了：

- **图模式中的 WHERE 子句**——例如 `MATCH (a)->(b)->(c) WHERE a.vname = c.vname`
- **「列未找到」假阳性修复**：属性名原本引用自 `pg_attribute` 的堆元组；在 `RELCACHE_FORCE_RELEASE` 后可能指向已释放内存。修复方式是将属性名复制出来。
- 编译修复、pgperltidy 合规、隐式属性/标签的错误位置报告。

Imran Zaheer 增加了**标签和属性 EXCEPT 列表**的支持。

### v14：循环路径、访问权限、RLS（2024 年 8–10 月）

Ashutosh 贡献了主要功能：

- **循环路径模式**：元素变量在路径中重复出现的模式（例如同一顶点既在起点又在终点）。共享变量的元素必须具有相同类型和标签表达式；重复边模式不受支持。
- **属性图的访问权限**：属性图行为类似**安全调用者视图**——当前用户必须对底层表有权限。仅对用户可访问的元素，查询才能成功。安全定义者属性图未实现。
- **行级安全（RLS）**：`graph_table_rls.sql` 中的回归测试验证 RLS 与属性图的配合。
- **属性排序规则与边-顶点链接**：同名字的属性在各元素间必须具有相同排序规则。边的键列与引用顶点键的排序规则必须兼容。边-顶点链接使用等值运算符构建，并建立依赖，使这些运算符无法在未删除边的情况下被删除。
- **`\d` 与 `\dG` 变体**：对属性图执行 `\d` 显示元素、表、类型和终点顶点别名；`\d+` 增加标签与属性。`\dG` 列出属性图；`\dG+` 增加所有者与描述。

### Henson Choi：LABELS()、PROPERTY_NAMES()、多模式（2025 年 12 月）

Henson Choi 贡献了三个补丁：

- **LABELS() 图元素函数**：返回图元素的所有标签为 `text[]`。通过在子查询中包装元素表、添加虚拟 `__labels__` 列实现，使优化器在按标签过滤时能剪枝 Append 分支（例如 `WHERE 'Person' = ANY(LABELS(v))`）。
- **PROPERTY_NAMES() 图元素函数**：返回所有属性名为 `text[]`，同样支持基于属性的过滤剪枝。
- **多模式路径匹配**：支持 `MATCH` 中逗号分隔的路径模式，如 `MATCH (a)->(b), (b)->(c)`。共享变量的模式合并为一个连接；不相连的模式产生笛卡尔积（与 SQL/PGQ 和 Neo4j Cypher 一致）。

### v20260113：整合实现（2026 年 1 月）

最新补丁（v20260113）将此前工作合并为单一 WIP 补丁：

- **ECPG 支持**：ECPG 中的 SQL/PGQ——基本查询、预处理语句、游标、动态查询。ECPG 中的标签析取需要对 ecpg 词法分析器做改动。
- **pg_overexplain 集成**：属性图 RTE 与 `RELKIND_PROPGRAPH` 在 `EXPLAIN (RANGE_TABLE, ...)` 中得到识别。
- **扩展测试覆盖**：`create_property_graph.sql`（365 行）、`graph_table.sql`（561 行）、`graph_table_rls.sql`（363 行）、`privileges.sql`（58 行）。
- **rewriteGraphTable.c**：从约 420 行增至约 1,330 行；`propgraphcmds.c` 从约 1,000 行增至约 1,860 行。

## 社区讨论

### Andres Freund：对改写器的担忧

Andres Freund 提出结构层面的顾虑：通过改写系统转换会妨碍优化器利用图语义，并增加对改写系统的依赖。Peter 回应称，PGQ 在设计上以关系为核心（类似视图展开），标准和其他实现都遵循这一模型。Tomas Vondra 质疑是否应更长时间保留图结构以支持图专属索引或执行器节点；Ashutosh Bapat 指出，许多优化本身会改善底层连接，与视图展开保持一致对安全也有意义。

### Florents Tselai：文档

Florents Tselai 建议调整文档顺序：先回答「我的数据建模为图 G=(V, E)，Postgres 能帮上忙吗？」再深入实现细节，并使用 `graph_table.sql` 中的可运行示例。他将该方案与 Apache Age 的 jsonpath 风格做法对比，但同意标准的关系映射适合 PostgreSQL 核心。

## 技术细节

### 架构

- **解析器**：`CREATE PROPERTY GRAPH`、`ALTER PROPERTY GRAPH`、`DROP PROPERTY GRAPH` 以及 `GRAPH_TABLE(... MATCH ... COLUMNS ...)` 的语法。
- **系统表**：`pg_propgraph_element`、`pg_propgraph_element_label`、`pg_propgraph_label`、`pg_propgraph_label_property`、`pg_propgraph_property`。
- **改写**：`rewriteGraphTable.c` 将图模式转换为连接与并集。
- **工具**：`pg_dump`、`psql` 的 `\d`/`\dG`、补全；`pg_get_propgraphdef()` 用于自省。
- **ECPG**：嵌入式 SQL 中的完整支持。

### 访问控制

1. 用户需要对属性图有 **SELECT** 权限。
2. 属性图采用**安全调用者**：当前用户必须对底层表有权限。仅对用户可访问的元素，查询才能成功。
3. 在安全定义者视图中，属性图访问使用视图所有者的权限；底层关系的访问使用执行者的权限。
4. 安全定义者属性图未实现（标准未提及）。

### 属性排序规则与边-顶点链接

- 同名属性在各元素间必须具有相同排序规则。
- 显式指定键时，边的键与顶点键的排序规则必须匹配（外键推导出的链接依赖约束本身）。
- 边-顶点链接使用等值运算符；边依赖这些运算符，因此无法独立删除。

## 当前状态

**v20260113** 补丁是一个整合后的 WIP。它包含：

- 完整的 `CREATE PROPERTY GRAPH` / `ALTER` / `DROP`，支持标签、属性、KEY 子句、SOURCE/DESTINATION REFERENCES
- `GRAPH_TABLE`，支持路径模式、模式内 WHERE、循环路径、多模式
- `LABELS()` 与 `PROPERTY_NAMES()` 图元素函数
- 访问权限、RLS、权限测试
- ECPG 支持、pg_overexplain 集成
- 文档与回归测试

补丁**尚未提交**。Peter 和 Ashutosh 持续改进；基于改写器的设计仍是当前选择，审阅与测试仍在进行中。

## 小结

SQL/PGQ 将为 PostgreSQL 带来标准化的图查询语法。实现已从脆弱原型发展为功能较全的补丁，包含循环路径、图元素函数、多模式匹配、访问控制、RLS、ECPG 支持及较完整的测试。主要架构选择——在改写系统中将图查询改写为关系形式——与标准和视图语义一致。若被提交，PostgreSQL 用户将能在不离开 SQL、不维护独立图数据库的情况下，在关系数据上表达路径模式。

## 参考资料

- [讨论串：SQL Property Graph Queries (SQL/PGQ)](https://www.postgresql.org/message-id/flat/a855795d-e697-4fa5-8698-d20122126567%40eisentraut.org)
- [Oracle 23c：SQL Property Graphs and SQL/PGQ](https://oracle-base.com/articles/23c/sql-property-graphs-and-sql-pgq-23c)
- [CIDR 2023：DuckPGQ](https://www.cidrdb.org/cidr2023/papers/p66-wolde.pdf)
- [PGCon 2019：PostgreSQL 中的图数据库](https://www.pgcon.org/2019/schedule/events/1300.en.html)
