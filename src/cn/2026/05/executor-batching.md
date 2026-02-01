# 执行器批处理：面向批量的元组处理

## 引言

PostgreSQL 的执行器长期以来都是 **逐元组（tuple-at-a-time）** 的：每个计划节点通常向子节点要一个元组、处理后再向上返回一个结果元组。这种设计简单，在 OLTP 场景下表现良好，但在分析型和批量负载中，每元组的开销——尤其是重复的函数调用和表达式求值——往往会成为主要成本。在 [PGConf.dev 2025](https://wiki.postgresql.org/wiki/PGConf.dev_2025_Developer_Unconference#Can_the_Community_Support_an_Additional_Batch_Executor) 上，社区讨论了 PostgreSQL 是否能够支持一种 **额外的批处理执行器**，在节点之间传递 **成批元组** 而不是一次一个 slot。

在那次讨论以及 Andres Freund 和 David Rowley 的私下交流之后，**Amit Langote** 于 2025 年 9 月在 pgsql-hackers 上发布了题为 [「Batching in executor」](https://www.postgresql.org/message-id/flat/CA%2BHiwqFfAY_ZFqN8wcAEMw71T9hM_kA8UtyHaZZEZtuT3UyogA%40mail.gmail.com) 的补丁系列。该系列引入了 **批处理表访问方法（Table AM）API**，在执行器中增加了 **支持批处理的接口**（`ExecProcNodeBatch`、`TupleBatch`），并原型化了 **面向批量的表达式求值**（包括批量 qual 和聚合转换函数）。目标是降低每元组开销、为聚合函数中的 SIMD 等未来优化铺路，并为受益于批量执行的列存或压缩表 AM 打基础。

## 为何重要

- **执行器开销**：在 CPU 受限、IO 极少的负载下（例如全缓存表），大量时间消耗在执行器内部。批处理减少了进入表 AM 和表达式解释器的调用次数，并可通过一次对多行求值来削减函数调用开销。
- **聚合与分析**：批量转换求值（如 `count(*)`、`sum()`、`avg()`）可以按批而非按行支付 fmgr 成本，并为向量化或 SIMD 友好路径打开空间。
- **未来表 AM**：批量执行器便于列存或压缩表 AM（如 Parquet 风格）以原生批量格式传递数据，而不必过早物化为堆元组。
- **OLTP 安全**：设计上保留现有逐行路径不变；批处理为可选（例如通过 `executor_batching` GUC），因此 OLTP 负载不受影响。

理解本线程的内容有助于把握 PostgreSQL 如何可能增加一条面向批量的执行路径，以及社区在物化、ExprContext、EEOP 设计等方面正在权衡的内容。

## 技术分析

### 补丁结构

系列分为两部分：

1. **0001–0003** — 基础：批处理表 AM API、heapam 批处理实现、与 SeqScan 对接的执行器批处理接口。
2. **0004–0008** — 原型（WIP/PoC）：支持批处理的 Agg 节点、TupleBatch 相关新 EEOP、批量 qual 求值、批量聚合转换（按行循环与「直接」批 fmgr）。

### 核心抽象

**表 AM 批处理 API（0001）**
新增回调允许表 AM 一次返回 **多个元组** 而非一个。对 heap 而言：

- **`HeapBatch`** 保存单页内的元组；大小受 `EXEC_BATCH_ROWS`（当前 64）和「不跨页」限制。
- **`heapgettup_pagemode_batch()`** 从当前页填充 `HeapTupleData` 数组，逻辑与 `heapgettup_pagemode()` 对应，但面向一批。可见性和扫描方向处理方式一致。

通用层在 `tableam.h` 中引入 **batch** 类型与操作，以便其他 AM 提供自己的批量格式与实现。

**执行器批处理路径（0002–0003）**
- **`TupleBatch`** 是批处理模式下在节点间传递的容器，可持有 AM 原生批（如堆元组）或物化后的 slot，视路径而定。
- **`ExecProcNodeBatch()`** 对应 `ExecProcNode()`：返回 `TupleBatch*` 而非 `TupleTableSlot*`。`PlanState` 增加 `ExecProcNodeBatch` 函数指针，沿用与逐行路径相同的「首次调用」与插桩包装。
- **SeqScan** 获得：
  - **批量驱动的 slot 路径**：仍每次返回一个 slot，但从内部批中填充，减少对 AM 的调用。
  - **批路径**：当父节点支持批处理时，SeqScan 的 `ExecProcNodeBatch` 直接返回 `TupleBatch`（如通过 `ExecSeqScanBatch*`）。

因此前三个补丁提供：(1) 能产生批的表 AM；(2) 请求与传递批的执行器 API；(3) SeqScan 作为首个既能消费又能产生批的节点。

### 面向批量的表达式求值（0004–0008）

后续补丁尝试对 **一批** 行做表达式求值：

- **Agg 的批量输入**：Agg 可通过 `ExecProcNodeBatch()` 从子节点拉取 `TupleBatch`，并成批喂入聚合转换函数。
- **新 EEOP**：表达式解释器增加针对 TupleBatch 的步骤——例如将属性取到批量向量、对一批求 qual、以及按行在解释器内循环（ROWLOOP）或按批调用转换函数（DIRECT）执行聚合转换。
- **批量 qual 求值**：一批元组可用单次遍历完成过滤（ExecQualBatch 及相关 EEOP），降低每行解释器和 fmgr 开销。

提供了两种批量聚合原型路径：一是在解释器内按行迭代（每行转换）；二是每批调用一次转换函数（每批 fmgr）。在 Amit 的基准中，当执行器成本占主导时，后者收益更大。

### 设计选择与未决点

- **单页批**：堆批限于一页，因此批大小可能小于 `EXEC_BATCH_ROWS`（例如每页元组少或 qual 选择性高）。线程中提到未来可改进：跨页批或扫描在批未满时继续要元组。
- **TupleBatch 与 ExprContext**：补丁在 `ExprContext` 上扩展了 `scan_batch`、`inner_batch`、`outer_batch`。每批表达式求值仍使用 `ecxt_per_tuple_memory`，Amit 指出这「 arguably 滥用了」每元组契约。**批作用域内存** 的更清晰模型仍待定义。
- **物化**：目前面向批的表达式求值通常作用在已物化到 slot（或堆元组数组）的元组上。长期目标是在 **原生批格式**（如列存或压缩）上做表达式求值而不强制物化；这需要更多基础设施（如 AM 控制的表达式求值或面向批的算子）。

## 社区观点

### Tomas Vondra：批设计与索引预取

Tomas 将本补丁与 **索引预取** 工作（他参与其中）对比，后者也在索引 AM 与执行器之间引入「批」概念。他指出两种设计因目标不同而不同：

- **索引预取**：共享的批结构由索引 AM 填充，之后由 `indexam.c` 管理；批在此之后与 AM 无关。
- **执行器批处理**：每个表 AM 可产生自己的批格式（如 `HeapBatch`），包装在带 AM 特定操作的通用 `TupleBatch` 中。执行器保留 TAM 特定优化，并依赖 TAM 对批内容进行操作。

Amit 同意：执行器批处理旨在保留 TAM 特定行为并尽可能避免过早物化；预取则追求由 indexam 统一的批格式。两种设计都与各自目标一致。

Tomas 还问：(1) 何时必须将 `TupleBatch` 物化为通用格式（如 slot）？(2) 表达式能否直接在「自定义」批（如压缩/列存）上执行？Amit 回复说目前表达式求值仍需物化，但设计上不应阻碍未来在原生批数据上求值（如列存或 Parquet 风格）。给表 AM 更多控制「如何在其批数据上求值」是可能的后续扩展。

### Tomas Vondra：TPC-H Q22 段错误与 v3 修复

Tomas 报告在启用批处理运行 TPC-H 时出现 **段错误**，且 **仅出现在 Q22**，堆栈始终指向同一处：`numeric_avg_accum` 收到 NULL 的 datum（`DatumGetNumeric(X=0)`），从 `ExecAggPlainTransBatch` 到 `agg_retrieve_direct_batch`。因此问题在批量聚合路径：转换函数收到了本不应为 NULL 的 NULL。

Amit 将崩溃追溯到 **表达式解释器**。两个不同的 EEOP（分别对应 ROWLOOP 和 DIRECT 批量聚合路径）都调用了 **同一个辅助函数**。该辅助函数在运行时再次推导 opcode（如通过 `ExecExprEvalOp(op)`）。在某些构建（如 macOS 上的 clang-17）中，这两个 EEOP 分支编译成相同代码，导致 **分发标签地址相同**。解释器按标签地址做反向查找时可能返回错误的 EEOP；初始化路径可能以为在执行 ROWLOOP EEOP，而执行路径却按 DIRECT EEOP 行为，导致状态错误和 NULL/崩溃。

**v3** 中的修复（补丁 0009）是 **将共享辅助拆成两个函数**，每个 EEOP 一个，这样辅助不再重新推导 opcode。修改后 Amit 在 macOS clang-17 上无法再复现崩溃。同一修复也解决了 Tomas 遇到的 TPC-H Q22 段错误。

### Bruce Momjian：POSETTE 与 OLTP

Bruce 引用了 POSETTE 2025 的两场演讲做背景：一场讲数据仓库需求，一场讲 [「Hacking Postgres Executor For Performance」](https://www.youtube.com/watch?v=D3Ye9UlcR5Y)。Amit（第二场演讲者）确认批处理设计上不会给 OLTP 路径增加明显开销；逐行路径仍是默认且未改动。

### 关闭批处理时的回归

Tomas 观察到在 **关闭批处理**（`executor_batching=off`）时，打补丁的树可能比未打补丁的 master 更慢——即新代码路径未启用时存在回归。Amit 复现了该现象：例如单聚合 `SELECT count(*) FROM bar` 和多聚合 `SELECT avg(a), … FROM bar` 在关闭批处理时相比 master 有约 3–18% 的变慢，具体取决于行数和并行度。他承认回归并表示正在排查。确保在关闭批处理时零或极小成本是合入基础补丁的重要前提。

## 技术细节

### 实现要点

- **批大小**：`EXEC_BATCH_ROWS` 为 64。堆批还受单页限制，实际批大小可能更小（如 Amit 的 1000 万行测试表中每页约 43 行）。
- **插桩**：`ExecProcNodeBatch` 使用与逐行路径相同的插桩钩子；批调用的「元组」数记为返回的 `TupleBatch` 的有效行数（`b->nvalid`），便于 EXPLAIN ANALYZE 等统计保持意义。
- **GUC**：在 v4/v5 中 GUC 为 **`executor_batch_rows`**（0 = 关闭批处理；例如 64 = 批大小）。

### 边界与限制

- **稀疏批**：高选择性 qual 下，过滤后批内有效行可能很少。线程建议未来支持跨页批或扫描在批未满时继续填充。
- **ExprContext 与批生命周期**：用 `ecxt_per_tuple_memory` 承担每批工作是目前的设计债；独立的批作用域分配器或上下文会更清晰。
- **并行与嵌套 Agg**：Tomas 崩溃的堆栈涉及并行 worker（Gather/GatherMerge）和嵌套聚合（如子计划上的 Agg）。NULL datum 问题出在该场景下使用的批量转换路径；v3 的 EEOP 辅助拆分从根因上修复，而非针对单条查询。

### 基准摘要（来自 Amit v1 邮件）

均在完全 VACUUM 的表、大 `shared_buffers` 且预热缓存下运行；时间单位为 ms，「off」= 批处理关，「on」= 批处理开；负 %diff 表示「on」更快。

- **单聚合、无 WHERE**（如 `SELECT count(*) FROM bar_N`）：仅批量 SeqScan（0001–0003）约快 8–22%；加上批量 agg（0001–0007）在部分规模下约快 33–49%。
- **单聚合、有 WHERE**：批量 agg + 批量 qual（0001–0008）约快 31–40%。
- **五聚合、无 WHERE**：批量转换（每批 fmgr，0001–0007）约快 22–31%。
- **五聚合、有 WHERE**：批量转换 + 批量 qual（0001–0008）约快 18–32%。

因此在执行器占主导（IO 极少）时，批处理一致降低 CPU 时间，最大收益来自减少每行 fmgr 调用和对整批求 qual。

### 演进：v4 与 v5

后续修订在基础之上增加了可观测性与批量 qual 工作：

- **v4**（2025 年 10 月）：新增 **EXPLAIN (BATCHES)**（补丁 0003）用于展示元组批处理统计，对应此前「插桩」的待办项。Amit 报告在 v4 中 **关闭批处理时的回归**（相对未打补丁的 master）已不再出现——可能与移除 `HeapScanData` 中的多余字段以及避免混用编译器（gcc vs clang）比较有关。新基准使用 `SELECT * FROM t LIMIT 1 OFFSET n`；在 `batch=64` 下，无 WHERE 时约快 22–26%，`WHERE a > 0` 时约快 21–48%；变形开销大的情况（如对最后一列求 qual）收益较小。**Daniil Davydov** 审阅了堆批处理代码（如 `SO_ALLOW_PAGEMODE` 断言、`heapgettup_pagemode_batch` 逻辑与风格），Amit 在 v4 中已回应。

- **v5**（2026 年 1 月）：**0001–0003** 仍为核心（批表 AM API、SeqScan + TupleBatch、EXPLAIN BATCHES）。**0004** 增加 **ExecQualBatch** 用于批量 qual 求值（WIP）；**0005** 将批量 qual 的 opcode 移到 **专用解释器**，使逐行路径（`ExecInterpExpr`）不被修改，从而在 `executor_batch_rows=0` 时避免额外成本。Amit 移除了 **BatchVector** 中间表示（qual 直接读取批内 slot 的 `tts_values`）。仍有两个待解决问题：(1) 在 0% 选择性（所有行不满足 qual）时，即使关闭批处理，打上批量 qual 补丁后逐行路径仍更热；(2) 对靠后列的 qual（变形开销大）批处理几乎无收益。近期补丁中的 GUC 为 **`executor_batch_rows`**（0 = 关闭）。

## 当前状态

- 线程 **仍在进行**；最近消息为 2026 年 1 月。系列仍为 **进行中**。
- **v5** 为当前版本。**0001–0003**（表 AM 批 API、heapam 批、SeqScan + TupleBatch、EXPLAIN BATCHES）是拟先审阅并争取合入的部分。
- v5 的 **0004–0005** 为 **实验性**（ExecQualBatch、批量 qual 专用解释器）。
- **v3** 已包含针对 TPC-H Q22 / 批量 agg 崩溃的 **段错误修复**（拆分 EEOP 辅助）；v4/v5 在此基础上演进。
- **待办**：(1) 当批量 qual（0004–0005）在树中但 `executor_batch_rows=0` 时的逐行路径回归（如 0% 选择性）；(2) 批作用域内存与 ExprContext；(3) 跨页批与在原生/压缩批格式上求值等后续工作。

## 小结

Amit Langote 的「Batching in executor」系列在 PostgreSQL 执行器中引入了一条 **面向批量的路径**：表 AM 可返回成批元组，执行器通过 `TupleBatch` 请求与传递批，SeqScan 是首个接入该路径的节点。v4、v5 增加了 **EXPLAIN (BATCHES)** 用于可观测性，并原型化了 **批量 qual 求值** 与专用解释器，以保持逐行路径不变。基准显示在开启批处理时收益可观（多为 20–50%）；此前「关闭批处理」时的回归在 v4 中已解决，但仍有问题：在打上批量 qual 补丁且关闭批处理时（如 0% 选择性）逐行路径的成本。

审阅者提出了重要问题：与其他「批」类工作（如索引预取）的协调、物化与未来「在批上求值」的设计、TPC-H Q22 段错误（v3 修复）以及 Daniil 对堆批处理的审阅（v4 已回应）。当前审阅与合入重点为基础补丁（0001–0003）与 EXPLAIN BATCHES。

## 参考

- [邮件列表线程：Batching in executor](https://www.postgresql.org/message-id/flat/CA%2BHiwqFfAY_ZFqN8wcAEMw71T9hM_kA8UtyHaZZEZtuT3UyogA%40mail.gmail.com)
- [PGConf.dev 2025：社区能否支持额外的批处理执行器？](https://wiki.postgresql.org/wiki/PGConf.dev_2025_Developer_Unconference#Can_the_Community_Support_an_Additional_Batch_Executor)
- [POSETTE 2025：Hacking Postgres Executor For Performance](https://www.youtube.com/watch?v=D3Ye9UlcR5Y)
- PostgreSQL 文档：[表访问方法接口](https://www.postgresql.org/docs/current/tableam.html)、[执行器](https://www.postgresql.org/docs/current/executor.html)
