# COPY TO 的 JSON 格式：PostgreSQL 原生 JSON 导出

## 引言

2023 年 11 月，**Davin Shearer** 在 pgsql-general 上询问如何用 `COPY TO` 将 JSON 从 PostgreSQL 导出到文件。当他用 `COPY TO` 导出只产生一列 JSON 的查询（例如 `json_agg(row_to_json(t))`）时，文本格式会再次对内容做转义：JSON 内部的双引号被多转义一层，得到的不是合法 JSON，用 `jq` 等工具无法解析。社区共识是：更合理的做法是为 **COPY TO 提供原生 JSON 格式**——这样单列 JSON（或整行渲染成一个 JSON 对象）会按合法 JSON 写出，不再叠加一层 text/CSV 转义。

在此基础上，结合 [Joe Conway 早前关于 COPY/JSON 的讨论](https://www.postgresql.org/message-id/6a04628d-0d53-41d9-9e35-5a8dc302c34c@joeconway.com)，提交了一系列补丁。最终设计是：为 `COPY TO` 增加 **`FORMAT json`** 选项，以及可选的 **`FORCE_ARRAY`**，用于将整段输出包在一个 JSON 数组中。该线程历经多个版本（v8 到 v23），**Tom Lane**、**Joe Conway**、**Alvaro Herrera**、**Joel Jacobson**、**Jian He**、**Junwang Zhao** 等人都参与过讨论。本文总结讨论要点、实现方式和当前状态。

## 为何重要

- **正确的 JSON 导出**：目前从库内把查询结果当 JSON 导出，通常只能用 `COPY TO` 的 text 或 csv 格式。文本格式会把结果当普通字符串再转义引号和反斜杠，导致 JSON 被破坏。专门的 JSON 格式会按行输出一个 JSON 对象（或每列一个 JSON 值），并做正确转义，输出即为合法 JSON。
- **与上下游对接**：很多流水线期望 JSON（例如每行一个 JSON 对象，或一个 JSON 数组）。原生 `COPY TO ... (FORMAT json)` 和 `FORCE_ARRAY` 可以直接在库内完成导出，无需在客户端再格式化或借助 `psql -t -A`、LO API 等变通手段。
- **与现有格式一致**：COPY 已有 `text`、`csv`、`binary`，增加 `json` 后，仍是「选一种格式、得到正确编码」的同一套思路。

## 技术分析

### 设计取舍

补丁做了这些约定：

1. **JSON 格式仅用于 COPY TO**。不支持 `COPY FROM` 的 JSON（解析任意 JSON 属于更大功能）。语法和选项校验会拒绝在 `COPY FROM` 时使用 `FORMAT json`。
2. **JSON 下不使用 HEADER**。文档和代码都禁止在 JSON 格式下使用 HEADER，避免在 JSON 行/数组前多出一行表头。
3. **协议上视为一列**。JSON 模式下，前后端 Copy 协议只发送一列（非二进制）；每一行被渲染成一个 JSON 值（例如每行一个对象）。
4. **FORCE_ARRAY 仅在与 JSON 一起时有效**。`FORCE_ARRAY` 会在整段 COPY 输出外包上 `[ ... ]` 并在行间插入逗号，得到单个 JSON 数组；仅在与 `FORMAT json` 同时使用时合法。

### 补丁结构

- **补丁 1（自 v13）— CopyFormat 重构**
  **Joel Jacobson** 引入枚举 `CopyFormat`（如 `COPY_FORMAT_TEXT`、`COPY_FORMAT_CSV`、`COPY_FORMAT_BINARY`），用 `CopyFormatOptions` 中的单一 `format` 字段替代原来的 `csv_mode` 和 `binary` 两个布尔，便于后续增加新格式（如 JSON）。**jian he** 随后根据评审意见做了重构；**Junwang Zhao** 又针对执行器中新的 `CopyToRoutine` 做了适配。

- **补丁 2 — COPY TO 的 JSON 格式**
  - **语法**（`gram.y`）：增加 `JSON` 为格式选项，并在 COPY 选项中允许 `FORMAT json`。
  - **选项**（`copy.c`、`copy.h`）：自 v13 起格式由 `CopyFormat` 表示，JSON 对应 `COPY_FORMAT_JSON`；校验不变：JSON 下不能有 HEADER/default/null/delimiter，COPY FROM 不能使用 JSON。
  - **Copy 协议**（`copyto.c`）：在 `SendCopyBegin` 中，JSON 模式下只发送一列、格式 0（text），不再按列送格式。
  - **行输出**（`copyto.c`）：在 `CopyOneRowTo` 中，若开启 `json_mode`，则通过 `composite_to_json()`（来自 `utils/adt/json.c`）把整行转成 JSON 字符串并发送。对基于查询的 COPY（无关系），补丁会保证 slot 的 tuple descriptor 与查询一致，以便 `composite_to_json` 用正确的属性元数据生成键名。
  - **json.c**：将 `composite_to_json()` 从 `static` 改为导出，并在 `utils/json.h` 中声明，供 COPY 调用。

- **补丁 3 — COPY TO 的 FORCE_ARRAY**
  - **选项**（`copy.c`、`copy.h`）：增加 `force_array`，解析 `force_array` / `force_array true|false`。校验：FORCE_ARRAY 仅允许在 JSON 模式下使用（v12 起使用 `ERRCODE_INVALID_PARAMETER_VALUE`）。
  - **输出**（`copyto.c`）：在首行前，若 JSON 模式且 `force_array`，先发送 `[` 和换行；行与行之间在每行 JSON 对象前发送 `,`（用 `json_row_delim_needed` 标记）；最后一行之后发送 `]` 和换行。默认（不加 FORCE_ARRAY）仍是每行一个 JSON 对象。

### 版本演进：v8 到 v23

- **v8** 曾采用更大改动：抽出 COPY TO/FROM 的格式实现并做可插拔机制（含 contrib 模块 `pg_copy_json`）。评审意见倾向于在核心内做更小、直接的改动。
- **v9–v10** 收窄为只增加 COPY TO 的 JSON 格式（不做可插拔 API）。v10 引入 `json_mode` 并采用 `composite_to_json`。
- **v11** 增加 **FORCE_ARRAY** 选项及相应测试，并修正「COPY FROM 与 json」的错误码，加强选项校验。
- **v12**（2024 年 8 月）：只重发补丁 2（FORCE_ARRAY）；在非 JSON 模式下使用 FORCE_ARRAY 时错误码改为 `ERRCODE_INVALID_PARAMETER_VALUE`。
- **v13**（2024 年 10 月）：**Joel Jacobson** 贡献 0001 — 引入 **CopyFormat** 枚举，用 `CopyFormatOptions` 中的单一 `format` 字段替代 `csv_mode` 和 `binary`。0002（json 格式）、0003（force_array）在其上 rebase；文档明确 JSON 不能与 `header`、`default`、`null`、`delimiter` 同用。
- **v14–v22**：主要为 **rebase 与上游适配**。v14 在部分投稿中不再单独发 CopyFormat 片（因 rebase 基准不同）。**Junwang Zhao**（v15，2025 年 3 月）针对新的 **CopyToRoutine** 结构（commit 2e4127b6d2）做了适配。v16–v22 继续 rebase 并回应评审，核心设计未变。
- **v23**（2026 年 1 月）：当前系列。**三片**：(1) CopyFormat 重构（原创 Joel Jacobson，jian he 重构），(2) json format for COPY TO（Author: Joe Conway；**Reviewed-by** 包括 Andrey M. Borodin、Dean Rasheed、Daniel Verite、Andrew Dunstan、Davin Shearer、Masahiko Sawada、Alvaro Herrera 等），(3) FORCE_ARRAY。功能集不变，补丁已 rebase 并获较多评审。

### 代码要点

**行转 JSON（补丁 2）**
每行通过已有的 `composite_to_json()` 转成一个 JSON 对象：

```c
rowdata = ExecFetchSlotHeapTupleDatum(slot);
result = makeStringInfo();
composite_to_json(rowdata, result, false);
CopySendData(cstate, result->data, result->len);
```

**FORCE_ARRAY 的框定（补丁 3）**
在行循环前发 `[`；从第二行起每行先发 `,` 再发对象；循环结束后发 `]`：

```c
if (cstate->opts.json_mode && cstate->opts.force_array)
{
    CopySendChar(cstate, '[');
    CopySendEndOfRow(cstate);
}
// ... 行循环：首行前不发逗号，之后 CopySendChar(cstate, ','); 再发对象 ...
if (cstate->opts.json_mode && cstate->opts.force_array)
{
    CopySendChar(cstate, ']');
    CopySendEndOfRow(cstate);
}
```

**用法示例（来自回归测试）**

```sql
COPY copytest TO STDOUT (FORMAT json);
-- 每行一个 JSON 对象。

COPY copytest TO STDOUT (FORMAT json, force_array true);
-- 单个 JSON 数组：[ {"col":1,...}, {"col":2,...} ]
```

## 社区讨论要点

### 原始问题与权宜方案

Davin 最初遇到的是 COPY 文本格式对 JSON 的二次转义。**David G. Johnston** 和 **Adrian Klaver** 建议用 `psql` 把查询结果写到文件而不是 COPY。**Dominique Devienne** 与 **David G. Johnston** 都认为，若 COPY 能提供「不格式化」或原始输出选项，在只导出一列（如 JSON）时即可按原样写出。**Tom Lane** 也同意，增加这样的选项可以满足该需求。这一共识促成了「专门做 COPY TO 的 JSON 格式」而不是在 text/CSV 上打补丁。

### 评审意见与调整

- **Tom Lane** 等强调改动要尽量小：只增加 COPY TO 的 JSON 输出，不做大重构。这推动了从 v8 的可插拔格式方案收缩到当前的内核 JSON 路径。
- **Joe Conway** 曾在另一线程讨论 COPY 与 JSON；jian he 的补丁引用了该讨论，并与「为 COPY TO 提供一等 JSON 输出」的思路一致。
- **Alvaro Herrera** 等也在线程中提出意见；从 v8 到 v9/v10 的收缩即体现了「更小、更聚焦的补丁集」的偏好。

### 补丁中的边界情况

- **基于查询的 COPY**：数据源是查询（无表）时，slot 的 tuple descriptor 可能与查询的不一致。补丁会把查询的属性元数据同步到 slot 的 tuple descriptor，保证 `composite_to_json` 生成正确的键名。
- **协议**：JSON 模式下 Copy 协议只发送一列；后端仍按行产生一个 JSON「值」（一个对象，或在 FORCE_ARRAY 时数组里的一个元素）。

## 技术细节

### 实现要点

- **转义**：JSON 字符串转义由 `composite_to_json()` 及 `json.c` 中已有的 `escape_json` 等完成，列值中的引号、反斜杠和控制字符会被正确编码。
- **HEADER**：与 JSON 明确不兼容，保证输出要么是纯 JSON 行，要么是单个 JSON 数组。
- **FORCE_ARRAY 输出**：回归测试显示，开启 `force_array` 时输出为 `[`、换行、第一个对象；之后每行先 `,` 再对象；最后 `]`，即一个合法 JSON 数组（元素间可有换行等空白）。

### 当前限制

- **COPY FROM**：不提供 JSON 导入，仅扩展 COPY TO。
- **HEADER**：JSON 模式下不支持。
- **二进制**：JSON 格式仅为文本，本补丁不涉及二进制 JSON。

## 当前状态

- 该线程从 **2023 年延续到 2026 年初**。当前最新系列为 **v23**（2026 年 1 月）：三片（CopyFormat 重构、json format for COPY TO、FORCE_ARRAY）。
- 截至该线程快照，补丁**尚未提交**，仍处于讨论阶段；v23 代表当前设计与评审状态。
- **设计**：`COPY TO ... (FORMAT json)` 及可选的 `(FORMAT json, force_array true)` 得到单个 JSON 数组；JSON 与 HEADER、DEFAULT、NULL、DELIMITER 及 COPY FROM 不兼容。

## 小结

「Emitting JSON to file using COPY TO」线程始于用户在使用 COPY 导出 JSON 时遇到的二次转义问题。社区认同应为 **COPY TO 提供原生 JSON 格式**。**jian he**（及后来的 **Junwang Zhao**）实现了 `FORMAT json`（仅 COPY TO；与 HEADER、DEFAULT、NULL、DELIMITER 不兼容）和 `FORCE_ARRAY`，复用 `composite_to_json()` 与现有 JSON 转义。**Joel Jacobson** 的 CopyFormat 重构（v13 起）用枚举替代格式布尔，为 JSON 及未来格式打下基础。

## 参考

- [邮件列表线程：Emitting JSON to file using COPY TO](https://www.postgresql.org/message-id/CALvfUkBxTYy5uWPFVwpk_7ii2zgT07t3d-yR_cy4sfrrLU%3Dkcg%40mail.gmail.com)
- [Joe Conway：COPY 与 JSON 讨论](https://www.postgresql.org/message-id/6a04628d-0d53-41d9-9e35-5a8dc302c34c@joeconway.com)
- PostgreSQL 文档：[COPY](https://www.postgresql.org/docs/current/sql-copy.html)、[JSON 类型](https://www.postgresql.org/docs/current/datatype-json.html)
