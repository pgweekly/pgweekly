# JSONPath 字符串方法：在路径里清洗 JSON，以及一场关于不可变性的长跑讨论

## 引言

处理“脏”JSON 时，常常要在比较之前做去空白、大小写转换、按分隔符拆分等操作。现在你可以在 `jsonb_path_query()` 等函数外围用 SQL 完成这些步骤，但不一定能在 **JSONPath 表达式内部**直接完成。Florents Tselai 在 `pgsql-hackers` 上发布了一系列补丁，为 JSONPath 增加常见的字符串方法：`lower()`、`upper()`、`initcap()`、`ltrim()` / `rtrim()` / `btrim()`、`replace()`、`split_part()`，实现上委托给 PostgreSQL 内置的字符串处理函数。

讨论很快从“能写什么表达式”扩展到更底层的问题：这些方法与 **易变性与不可变性**（依赖区域设置时的行为）、PostgreSQL 的 JSONPath 究竟应对齐 **SQL 标准** 还是互联网 RFC，以及现有带 `*_tz` 后缀的 JSONPath 入口在命名上的历史包袱。该工作登记在 [Commitfest 5270](https://commitfest.postgresql.org/patch/5270/)；[邮件串](https://www.postgresql.org/message-id/flat/CA%2Bv5N40sJF39m0v7h%3DQN86zGp0CUf9F1WKasnZy9nNVj_VhCZQ%40mail.gmail.com) 从 2024 年延续到 2025 年，补丁版本迭代很多轮。

## 为何重要

JSONPath 是 `jsonb_path_*` 系列函数内嵌的领域语言。在路径里直接提供字符串原语，可以减少外层 SQL 嵌套，让意图集中在路径上，也更接近人们熟悉的“管道式”文本处理——只是作用在 JSON 标量上。对数据清洗场景（空白、大小写、分隔符拆分），这类能力在易用性上很有吸引力。

真正的难点在于：**大小写映射和许多字符串操作依赖区域设置（locale）**。PostgreSQL 的规划与优化依赖正确的易变性标注：若把仍可能随区域或系统环境变化的结果标成 `immutable`，会破坏优化器的基本假设。线程里大部分篇幅正是在处理这一矛盾。

## 技术分析

### 补丁在做什么

首版补丁让这些方法转发到已在 `pg_proc` 注册的实现；在 `JsonPathParseItem` 上为需要比传统左右操作数更灵活入参的方法增加了 `method_args`（`arg0`、`arg1`），并增加 `jspGetArgX()` 访问器。作者还加入了 `README.jsonpath`，说明今后如何新增方法，方便后续贡献者。提案中的用法示例如下：

```sql
SELECT jsonb_path_query('" hElLo WorlD "', '$.btrim().lower().upper().lower().replace("hello","bye") starts with "bye"');
SELECT jsonb_path_query('"abc~(at)~def~@~ghi"', '$.split_part("~(at)~", 2)');
```

首帖列出的待决事项包括：若将来 **SQL/JSON 标准**定义了同名方法如何避免冲突（相关讨论里提到过 `pg_` 等前缀）、与现有 JSONPath 代码一致的 **默认排序规则**、尚缺的用户文档，以及类似 `CREATE JSONPATH FUNCTION` 的可扩展性设想。

### 补丁演进（概览）

可下载的系列从 **v1** 到 **v18**；自 **v6** 起每个版本拆成两个补丁：一个重命名 JSONPath 方法实参相关的词法/语法标记，另一个承载字符串方法本体。中间多轮修订涉及测试、`jsonpath_scan.l` 等文件的变基与冲突消解。较新的版本在 `doc/src/sgml/func/func-json.sgml` 中补充了 **SGML 文档**，并为 `jsonpath`、`jsonb_jsonpath` 等增加了 **回归测试**。

## 社区观点

- **Tom Lane** 很早就指出 [不可变性问题](https://www.postgresql.org/message-id/145894.1727298237%40sss.pgh.pa.us)：依赖区域设置的字符串操作无法保证 JSONPath 运算处处不可变；他引用提交 `cb599b9dd`，并对比 JSONPath 中为时区敏感日期时间引入的 `_tz` 分裂——他认为这种为每一种易变来源再复制一套入口的做法难以持续扩展。

- **Alexander Korotkov** 提出 **“灵活的易变性”** 设想：是否可以有辅助逻辑分析 JSONPath 参数是否为常量、路径中方法是否都“安全”，从而在受限情形下把 `jsonb_path_query()` 标为 `immutable`，否则标为 `stable`。他还询问这些新名字是否出现在 **SQL 标准**（或草案）中。

- **Florents Tselai** 提到 2019 年的相关讨论，并勾勒 **启发式**（若路径上各段都安全，则整体可视为不可变）；他引用 [RFC 9535](https://www.rfc-editor.org/rfc/rfc9535.html#name-function-extensions) 中的“函数扩展”，认为厂商扩展在规范上有依据，而易变性属于实现细节。

- **David E. Wheeler** 说明：PostgreSQL 的 JSONPath 跟踪的是 **SQL 标准中的 SQL/JSON**，而不是 RFC 9535；公开 RFC 中的扩展机制与 SQL 标准文本中的规则未必一致。他同时关心 **可扩展钩子**（词法、语法、执行器）；Florents 概括了步骤：新增 `JsonPathItemType`、修改 `jsonpath_scan.l` / `jsonpath_gram.y`、在 `executeItemOptUnwrapTarget` 中分发。

- **Robert Haas** 把问题部分归结为 **通用策略**（依赖操作系统时间或区域设置时，函数往往是 `stable`），并类比现有的 **`json_path_exists` 与 `json_path_exists_tz`**：可以考虑让带后缀的一组函数承担更多“非纯”行为，而在无后缀版本中报错——同时承认 `_tz` 这个名字对“区域设置”并不贴切。

- **David Wheeler** 将 Tom 所说的“难扩展”理解为：不会为每一种易变来源都造一套 `json_path_exists_*`；并讨论是否 **改名** 或泛化。在欧洲 PostgreSQL 社区会议上的讨论之后，Florents 总结了较务实的路线：把新行为放在 **`jsonb_path_*_tz` 家族**下、在非 `_tz` 变体中拒绝使用并 **写清文档**——在命名上与现有 `_tz` 保持一致，尽管语义上已不只在说时区。

- **命名与 API 膨胀**：David 提议是否引入 `_stable` 一类后缀；**Robert** 描述了Deprecation、GUC、多年迁移等重流程，并认为抱怨难以避免。**Tom Lane** 认为不必为迁移投入那么大精力，用户若执意保留旧名可以包一层 **包装函数**，并反问：为何不能 **新增一组更好的名字且永不删除旧名**。**Robert** 表示若能接受多出来的符号也可以。**Florents** 则指出再并行一套五个 `jsonb_path_*` 会显著增加 API 表面积。**David** 还从索引化角度追问：现实中 `_tz` 路径是否适合索引场景；讨论中也提到生成列以及未来虚拟生成列等用法。

## 技术细节

### 不可变性与区域设置

PostgreSQL 区分 `immutable`（在规划器假设下，相同输入应产生相同结果）与 `stable`（在一次语句执行过程中可能变化，例如随会话或环境）。区域数据可能随操作系统或 ICU 更新而改变，因此依赖区域的函数通常不能标为 `immutable`。JSONPath 若深度参与表达式求值与优化，必须与核心 SQL 函数遵守同一套规则。

### 标准立场

讨论中区分了两类文献：

- **RFC 9535**（IETF JSONPath）：公开，描述扩展点。
- **SQL/JSON 与 SQL 标准中的 JSONPath**（PostgreSQL 对齐目标）：文本不公开，扩展规则可能与 RFC 不同。

即便规范允许厂商扩展方法名，未来标准若占用同名仍有风险——首帖提出的命名策略问题仍未消失。

### 实现层面

除易变性外，新增方法会牵动 JSONPath 词法分析器、语法、执行器与测试。将“重命名标记”和“功能本体”拆成两个补丁，有利于在版本号变多之后仍保持评审可控。

## 现状

补丁系列在多轮变基中持续演进（抓取数据中可见至 **v18**）。工作条目见 [Commitfest 5270](https://commitfest.postgresql.org/patch/5270/)，也可在 [GitHub PR](https://github.com/Florents-Tselai/postgres/pull/18) 浏览。2025 年 5 月的讨论在实现策略（`*_tz` 表面、非后缀版本报错、文档）与命名取舍上趋于务实，但本邮件串片段中未见最终合入主线的结论。

## 结语

JSONPath 字符串方法回应的是真实的产品需求：在路径表达式内完成规范化与拆分。社区回应的焦点与其说在“要不要这个功能”，不如说在 **易变性标注是否正确**、**与 SQL/JSON 的关系**，以及 **`jsonb_path_*` 与 `_tz` 后缀** 的长期可维护性。这条线程很好地展示了 PostgreSQL 在扩展内嵌 DSL 时，如何在标准符合性、优化器正确性与 API 卫生之间取舍。
