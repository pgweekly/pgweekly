# UUID 与 base32hex 编码

## 引言

在 URL、日志、JSON 等场景里，经常需要一种更短、更易口述的 UUID 文本形式。2025 年 10 月起，在 [pgsql-hackers 讨论串](https://www.postgresql.org/message-id/flat/CAJ7c6TOramr1UTLcyB128LWMqita1Y7%3Darq3KHaU%3Dqikf5yKOQ%40mail.gmail.com) 中，有人倡议增加两个内置函数——`uuid_to_base32hex()` 与 `base32hex_to_uuid()`——把 UUID 编成 [RFC 4648 第 7 节](https://datatracker.ietf.org/doc/html/rfc4648#section-7) 的 **base32hex** 字符串（26 个字符）。后续讨论的焦点很快从「格式好不好」转向：**在 PostgreSQL 的 SQL 接口里，这类能力应该放在哪里**——是再增加一对 UUID 专用函数，还是走 `encode()` / `decode()` 与显式类型转换的组合。

## 为何值得关注

PostgreSQL 已有 `uuid` 类型与 `uuidv7()` 等函数，存储层面很高效；但在系统间交换、对外 API 和人工阅读时，**文本形态**仍然重要。**Base32hex** 在字节层面保持 **字典序与二进制一致**（与常见的 base64 不同），解码时 **大小写不敏感**，口述时也不必区分字母大小写。讨论中还把该格式与 DNSSEC 等现实用法、以及 **RFC 9562** 对「规范 hyphen 表示 + 库内二进制存储」的取向联系起来，强调**紧凑文本编码若碎片化**（各家用各家的短编码）会带来互操作噩梦。

## 技术分析

### 最初设想（概念）

线程中保留的原始说明（`hi-hackers.txt`）大致描述：

- **`uuid_to_base32hex(uuid) → text`**：26 个大写 base32hex 字符，无连字符、无填充；在 128 位与 base32 的 5 位对齐需求之间补两位零比特。
- **`base32hex_to_uuid(text) → uuid`**：解码大小写不敏感；非法输入返回 NULL。

作者同时对比了 base36（性能）、Crockford Base32（标准库支持弱）等，倾向 base32hex。

### 社区更偏好的方向：组合而非重复

**Aleksander Alekseev** 的第一回复既包含流程建议（避免大量 `Cc`；用 `git format-patch`；在 [Commitfest](https://commitfest.postgresql.org/) 登记），也包含接口设计：单独的 UUID 编解码函数 **可组合性差**，更稳妥的是：

1. 提供显式的 **`uuid ↔ bytea`** 转换；
2. 在 **`encode(bytea, ...)` / `decode(text, ...)`** 上增加 **base32hex** 格式，例如：

```sql
SELECT encode(uuidv7()::bytea, 'base32hex');
```

（早期邮件里写的是 `'base32'`；后续补丁采用的格式名是 **`base32hex`**，与 RFC 4648 的命名一致。）

**Andrey Borodin** 与 **Jelte Fennema-Nio** 同意扩展 `encode()` 更符合既有习惯；Jelte 以 PostgreSQL 18 中 **base64url** 的引入为例（[提交 e1d917182](https://git.postgresql.org/cgit/postgresql.git/commit/?h=REL_18_0&id=e1d917182c1953b16b32a39ed2fe38e3d0823047)），说明在相同入口上增加编码格式是可行路径。

### 补丁系列实际走向（概要）

下载的补丁系列（讨论串中可见到 **v12** 等版本）逐步收敛为：

- 在 `encode.c` 中实现 **`encode(bytea, 'base32hex')`** 与 **`decode(text, 'base32hex')`**（编码侧按 RFC 使用 `=` 填充；解码侧接受带填充或不带填充；大小写与空白处理以补丁说明为准）。
- 在 `func-binarystring.sgml` 中记录该格式，并给出紧凑 UUID 的推荐写法：

```sql
rtrim(encode(uuid_value::bytea, 'base32hex'), '=')
```

即相对 36 字符的规范 UUID 文本，得到 **26 字符** 的短形式。

- 与之配套：**`uuid` 与 `bytea` 的显式转换**，避免在核心中为每种编码再增加一对 UUID 专用函数。

### SQL 示例（示意）

下面语句对应讨论中形成的用法（`encode` / `decode` 使用 `'base32hex'`，UUID 经 `::bytea` 参与编码）。**需以包含该功能的 PostgreSQL 版本为准**——撰写本文时相关补丁仍在评审流程中。

**1. 带 RFC 填充的原始输出** — `encode()` 会输出 `'='` 填充；对 16 字节的 UUID，在未 `rtrim` 前字符串长度会大于 26：

```sql
SELECT encode('019535d9-3df7-79fb-b466-fa907fa17f9e'::uuid::bytea, 'base32hex') AS padded;
-- 32 个字符（按 RFC 4648 填充到 8 的倍数，末尾为 '='）
```

**2. 26 字符紧凑形式** — 去掉尾部 `=`，适合 URL、日志等（与线程中的示例 UUID 一致）：

```sql
SELECT rtrim(
         encode('019535d9-3df7-79fb-b466-fa907fa17f9e'::uuid::bytea, 'base32hex'),
         '='
       ) AS short_id;
-- 06AJBM9TUTSVND36VA87V8BVJO
```

**3. 往返** — `decode()` 返回 `bytea`，再 cast 回 `uuid`：

```sql
WITH x AS (
  SELECT rtrim(
           encode('019535d9-3df7-79fb-b466-fa907fa17f9e'::uuid::bytea, 'base32hex'),
           '='
         ) AS short_id
)
SELECT short_id,
       decode(short_id, 'base32hex')::uuid AS back_to_uuid
FROM x;
-- back_to_uuid = 019535d9-3df7-79fb-b466-fa907fa17f9e
```

**4. 与 `uuidv7()` 组合**（需要时间有序 ID 且对外用短字符串时）：

```sql
SELECT rtrim(encode(uuidv7()::bytea, 'base32hex'), '=') AS short_new_id;
```

### 补丁演进

早期版本曾出现 `uuid_encode` / `uuid_decode` 一类接口；后续版本将能力并入 **`encode`/`decode`**，并补充回归测试与文档，还涉及 **排序与排序规则（collation）** 的说明与小幅修正（附件列表中有单独的 doc 补丁）。

## 社区观点

- **列表与可见性**：Aleksander 指出 Sergey 若未订阅列表，首发邮件未必被所有人看到；附原文并建议订阅，有助于讨论在归档里自洽。

- **为何选 base32hex**：Sergey 归纳了排序保持、体积、标准库支持、口述友好、JSON 场景下实现简单等理由，并强调**短格式若各自为政**会导致生态分裂。

- **API 膨胀**：**Masahiko Sawada** 认为若再堆一批 `uuid_*` 编码函数，容易与 `encode()` 职责重叠；他支持 **`encode`/`decode` + UUID 与 bytea 转换**，并认为转换开销可忽略。

- **多态 `encode` 与 `decode` 的签名限制**：Sergey 曾问能否让 `encode()` 直接吃 `uuid`，或让 `decode()` 直接产出 `uuid`。Masahiko 说明：在 PostgreSQL 里 **无法用同一组参数类型** 为 `decode(text, text)` 定义两种不同返回类型；**显式 cast + 可内联的 SQL 包装函数** 是务实做法，例如：

```sql
CREATE FUNCTION uuid_to_base32(u uuid) RETURNS text
LANGUAGE SQL IMMUTABLE STRICT
BEGIN ATOMIC
  SELECT encode($1::bytea, 'base32hex');
END;
```

并说明与手写 `encode($1::bytea, ...)` 相比，额外成本主要在类型转换。

- **「只能有一种短格式」吗**：Sergey 担心多种短编码并存；Masahiko 指出在异构系统集成时，开发者仍可能因兼容性选择 **hex** 等。工程上的折中仍是在核心提供 **标准的 base32hex**，并在文档中写清 **26 字符 UUID** 的用法。

## 技术细节

- **填充**：按 RFC，`encode()` 会输出带 **`=`** 的填充；去掉尾部 `=` 即得到提案中的 26 字符形态。
- **排序**：base32hex 保持字节序对应的字典序；若把编码结果当作 **text** 比较，需注意 **排序规则** 与二进制比较的差异。
- **实现位置**：把编解码放在 `encode.c` 与现有 **base64url** 等并列，符合「二进制↔文本编码集中管理」的习惯。

## 结语

Base32hex 本身并不复杂，但这串讨论体现了 PostgreSQL 对 **接口正交性** 的偏好：`uuid` 存数据、`bytea` 表示字节、`encode`/`decode` 负责文本编码。若你需要紧凑且排序友好的 UUID 文本，正在形成的使用习惯是 **`encode(uuid::bytea, 'base32hex')`**，必要时 **`rtrim(..., '=')`**；若业务希望一行 SQL更短，可以用 **可内联的包装函数** 封装，而不必把每一种编码都塞进核心函数名列表。
