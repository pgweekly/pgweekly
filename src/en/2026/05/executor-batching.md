# Batching in Executor: Batch-Oriented Tuple Processing

## Introduction

PostgreSQL’s executor has long been **tuple-at-a-time**: each plan node typically requests one tuple from its child, processes it, and passes one result tuple upward. That design is simple and works well for OLTP, but for analytical and bulk workloads the per-tuple overhead—especially repeated function-call and expression-evaluation cost—can dominate. At [PGConf.dev 2025](https://wiki.postgresql.org/wiki/PGConf.dev_2025_Developer_Unconference#Can_the_Community_Support_an_Additional_Batch_Executor), the community discussed whether PostgreSQL could support an **additional batch executor** that moves **batches of tuples** between nodes instead of one slot at a time.

Following that discussion and off-list input from Andres Freund and David Rowley, **Amit Langote** posted a patch series on the pgsql-hackers list in September 2025 titled [“Batching in executor”](https://www.postgresql.org/message-id/flat/CA%2BHiwqFfAY_ZFqN8wcAEMw71T9hM_kA8UtyHaZZEZtuT3UyogA%40mail.gmail.com). The series introduces a **batch table AM API**, extends the executor with **batch-capable interfaces** (`ExecProcNodeBatch`, `TupleBatch`), and prototypes **batch-aware expression evaluation** (including batched quals and aggregate transitions). The goal is to reduce per-tuple overhead, enable future optimizations such as SIMD in aggregate functions, and lay groundwork for columnar or compressed table AMs that benefit from batch-oriented execution.

## Why This Matters

- **Executor overhead**: In CPU-bound, IO-minimal workloads (e.g., fully cached tables), a large share of time goes into the executor. Batching reduces calls into the table AM and expression interpreter, and can cut function-call overhead by evaluating expressions over many rows at once.
- **Aggregates and analytics**: Batched transition evaluation (e.g., `count(*)`, `sum()`, `avg()`) can pay fmgr cost per batch instead of per row and opens the door to vectorized or SIMD-friendly code paths.
- **Future table AMs**: A batch-oriented executor makes it easier for columnar or compressed table AMs (e.g., Parquet-style) to pass native batch formats without forcing early materialization into heap tuples.
- **OLTP safety**: The design keeps the existing row-at-a-time path unchanged; batching is opt-in (e.g., via `executor_batching` GUC) so OLTP workloads are not affected.

Understanding this thread helps you see how PostgreSQL might gain a second, batch-oriented execution path and what trade-offs (materialization, ExprContext, EEOP design) the community is working through.

## Technical Analysis

### Patch Structure

The series is split into two parts:

1. **Patches 0001–0003** — Foundation: batch table AM API, heapam batch implementation, and executor batch interface wired to SeqScan.
2. **Patches 0004–0008** — Prototype: batch-aware Agg node, new EEOPs for TupleBatch processing, batched qual evaluation, and batched aggregate transition (row-loop and “direct” per-batch fmgr).

Patches 0001–0003 are intended as the first candidates for review and eventual commit; 0004–0008 are marked WIP/PoC.

### Key Abstractions

**Table AM batch API (0001)**
New callbacks let a table AM return **multiple tuples per call** instead of one. For heap:

- **`HeapBatch`** holds tuples from a single page; size is limited by `EXEC_BATCH_ROWS` (currently 64) and by not crossing page boundaries.
- **`heapgettup_pagemode_batch()`** fills a `HeapTupleData` array from the current page, mirroring the logic of `heapgettup_pagemode()` but for a batch. Visibility and scan direction are handled the same way.

The generic layer introduces a **batch** type and ops in `tableam.h` so other AMs can supply their own batch format and implementation.

**Executor batch path (0002–0003)**
- **`TupleBatch`** is the container passed between nodes when running in batch mode. It can hold the AM’s native batch (e.g., heap tuples) or materialized slots, depending on the path.
- **`ExecProcNodeBatch()`** is the batch analogue of `ExecProcNode()`: it returns a `TupleBatch*` instead of a `TupleTableSlot*`. `PlanState` gains an `ExecProcNodeBatch` function pointer, with the same “first call” and instrumentation wrappers as the row path.
- **SeqScan** gets:
  - **Batch-driven slot path**: still returns one slot per call, but fills it from an internal batch (fewer AM calls).
  - **Batch path**: when the parent supports batching, SeqScan’s `ExecProcNodeBatch` returns a `TupleBatch` directly (e.g., from `ExecSeqScanBatch*`).

So the first three patches give: (1) table AMs that can produce batches, (2) an executor API to request and pass batches, and (3) SeqScan as the first node that can both consume and produce batches.

### Batch-Aware Expression Evaluation (0004–0008)

The later patches experiment with evaluating expressions over a **batch** of rows:

- **Batch input to Agg**: Agg can pull `TupleBatch` from its child via `ExecProcNodeBatch()` and feed rows into the aggregate transition in bulk.
- **New EEOPs**: Expression interpreter gains steps that operate on TupleBatch data—e.g., fetching attributes into batch vectors, evaluating a qual over a batch, and running aggregate transitions either by looping over rows inside the interpreter (ROWLOOP) or by calling the transition function once per batch with bulk arguments (DIRECT).
- **Batched qual evaluation**: A batch of tuples can be filtered with a single pass over the batch (ExecQualBatch and related EEOPs), reducing per-row interpreter and fmgr overhead.

Two prototype paths for batched aggregation are provided: one that iterates over rows in the interpreter (per-row transition), and one that invokes the transition function once per batch (per-batch fmgr). The latter shows larger gains in Amit’s benchmarks when executor cost dominates.

### Design Choices and Open Points

- **Single-page batches**: Heap batches are limited to one page. So batches may be smaller than `EXEC_BATCH_ROWS` (e.g., with few tuples per page or selective quals). The thread mentions possible future improvements: batches spanning pages or the scan requesting more tuples when the batch is not full.
- **TupleBatch vs ExprContext**: The patches extend `ExprContext` with `scan_batch`, `inner_batch`, and `outer_batch`. Per-batch expression evaluation still uses `ecxt_per_tuple_memory`, which Amit notes is “arguably an abuse” of the per-tuple contract. A clearer model for **batch-scoped memory** is still needed.
- **Materialization**: Today, batch-aware expression evaluation typically works on tuples materialized into slots (or heap tuple arrays). The long-term goal is to allow expression evaluation on **native batch formats** (e.g., columnar or compressed) without forcing materialization; that would require more infrastructure (e.g., AM-controlled expression evaluation or batch-aware operators).

## Community Insights

### Tomas Vondra: Batch Design vs Index Prefetching

Tomas compared the patch to **index prefetching** work (which he is involved in), which also introduces a “batch” concept for passing data between the index AM and the executor. He noted the designs differ on purpose:

- **Index prefetching**: A shared batch struct is filled by the index AM and then managed by `indexam.c`; the batch is AM-agnostic after that.
- **Executor batching**: Each table AM can produce its own batch format (e.g., `HeapBatch`) wrapped in a generic `TupleBatch` with AM-specific ops. The executor retains TAM-specific optimizations and relies on the TAM for operations on batch contents.

Amit agreed: for executor batching the aim is to keep TAM-specific behavior and avoid early materialization where possible; for prefetching the aim is a single, indexam-driven batch format. Both designs are consistent with their goals.

Tomas also asked: (1) When must a `TupleBatch` be materialized into a generic format (e.g., slots)? (2) Can expressions run directly on “custom” batches (e.g., compressed/columnar)? Amit replied that materialization is currently required for expression evaluation but that the design should not block future work to evaluate expressions on native batch data (e.g., columnar or Parquet-style). Giving the table AM more control over how expressions are evaluated on its batch data is a possible future extension.

### Tomas Vondra: TPC-H Q22 Segfault and Fix (v3)

Tomas reported a **segfault** when running TPC-H with batching enabled, only on **Q22**, with backtraces always pointing to the same place: `numeric_avg_accum` with a NULL datum (`DatumGetNumeric(X=0)`), called from `ExecAggPlainTransBatch` and then `agg_retrieve_direct_batch`. So the bug was in the batched aggregate path: a NULL was being passed where the transition function expected a valid value.

Amit tracked the crash to the **expression interpreter**. Two different EEOPs (for the ROWLOOP and DIRECT batched aggregate paths) both called the **same helper function**. That helper re-derived the opcode at execution time (e.g., via `ExecExprEvalOp(op)`). In some builds (e.g., clang-17 on macOS), the two EEOP cases compiled to identical code, so their **dispatch labels had the same address**. The interpreter’s reverse lookup by label address could then return the wrong EEOP; the init path could think it was running the ROWLOOP EEOP while the exec path behaved like the DIRECT EEOP, leading to incorrect state and the NULL/crash.

The fix (in **v3**, patch 0009) was to **split the shared helper into two separate functions**, one per EEOP, so the helper no longer re-derives the opcode. With that change, Amit could not reproduce the crash on macOS with clang-17. The same fix addresses the TPC-H Q22 segfault that Tomas saw.

### Bruce Momjian: POSETTE Talks and OLTP

Bruce pointed to two POSETTE 2025 talks for context: one on data warehouse needs and one on [“Hacking Postgres Executor For Performance”](https://www.youtube.com/watch?v=D3Ye9UlcR5Y). Amit (who gave the second talk) confirmed that batching is designed to avoid adding meaningful overhead to the OLTP path; the row-at-a-time path remains default and unchanged.

### Regression When Batching Is Off

Tomas had observed that with **batching disabled** (`executor_batching=off`), the patched tree could be slower than unpatched master—i.e., a regression when the new code path is not used. Amit reproduced this: for example, single-aggregate `SELECT count(*) FROM bar` and multi-aggregate `SELECT avg(a), … FROM bar` showed roughly 3–18% slowdown with batching off vs master, depending on row count and parallelism. He acknowledged the regression and said he was looking into it. Ensuring zero or minimal cost when batching is disabled is important for committable patches.

## Technical Details

### Implementation Highlights

- **Batch size**: `EXEC_BATCH_ROWS` is 64. Heap batches are further limited to one page, so effective batch size can be smaller (e.g., ~43 rows per page in Amit’s 10M-row test table).
- **Instrumentation**: `ExecProcNodeBatch` uses the same instrumentation hooks as the row path; the “tuple” count for a batch call is recorded as the number of valid rows in the returned `TupleBatch` (`b->nvalid`), so EXPLAIN ANALYZE-style stats remain meaningful.
- **GUC**: In v4/v5 the GUC is **`executor_batch_rows`** (0 = batching off; e.g. 64 = batch size).

### Edge Cases and Limitations

- **Sparse batches**: With selective quals, batches can end up with few valid rows after filtering. The thread suggests future work: cross-page batches or the scan refilling the batch when it is not full.
- **ExprContext and batch lifetime**: Reusing `ecxt_per_tuple_memory` for per-batch work is a known design debt; a dedicated batch-scoped allocator or context would be cleaner.
- **Parallel and nested Agg**: The backtrace from Tomas’s crash showed parallel workers (Gather/GatherMerge) and nested aggregation (e.g., Agg over subplan). The NULL-datum bug was in the batched transition path used in that setting; the v3 fix (split EEOP helpers) addresses the root cause rather than a single query.

### Benchmark Summary (from Amit’s v1 post)

All runs were on fully VACUUMed tables with large `shared_buffers` and prewarmed cache; timings in ms, “off” = batching off, “on” = batching on. Negative %diff means “on” is faster.

- **Single aggregate, no WHERE** (e.g., `SELECT count(*) FROM bar_N`): With only batched SeqScan (0001–0003), ~8–22% faster; with batched agg (0001–0007), ~33–49% faster in several cases.
- **Single aggregate, with WHERE**: With batched agg and batched qual (0001–0008), ~31–40% faster.
- **Five aggregates, no WHERE**: Batched transitions (per-batch fmgr, 0001–0007) ~22–31% faster.
- **Five aggregates, with WHERE**: Batched transitions + batched qual (0001–0008) ~18–32% faster.

So once the executor dominates (minimal IO), batching consistently reduces CPU time, with the largest gains from avoiding per-row fmgr calls and evaluating quals over batches.

### Evolution: v4 and v5

Later revisions refined the foundation and added observability and batch qual work:

- **v4** (Oct 2025): Adds **EXPLAIN (BATCHES)** (patch 0003) to show tuple-batching statistics, addressing the earlier “instrumentation” open point. Amit reported that the **regression when batching is off** (vs unpatched master) was no longer seen in v4—likely due to removing stray fields from `HeapScanData` and avoiding mixed compiler (gcc vs clang) comparisons. New benchmarks use `SELECT * FROM t LIMIT 1 OFFSET n`; with `batch=64`, improvements are ~22–26% for no-WHERE and ~21–48% for `WHERE a > 0`; deform-heavy cases (e.g. qual on last column) show smaller gains. **Daniil Davydov** reviewed the heap batch code (e.g. `SO_ALLOW_PAGEMODE` assertion, `heapgettup_pagemode_batch` logic, style); Amit addressed these in v4.

- **v5** (Jan 2026): Keeps **0001–0003** as core (batch AM API, SeqScan + TupleBatch, EXPLAIN BATCHES). **0004** adds **ExecQualBatch** for batched qual evaluation (WIP); **0005** moves batch qual opcodes into a **dedicated interpreter** so the per-tuple path (`ExecInterpExpr`) is not modified, aiming to avoid any cost when `executor_batch_rows=0`. Amit removed the **BatchVector** intermediate (quals now read batch slots’ `tts_values` directly). Two open issues: (1) With 0% selectivity (all rows fail the qual), the per-tuple path is still hotter with the batch qual patches applied even when batching is off; (2) Quals on late columns (deform-heavy) get little or no benefit from batching. The GUC in recent patches is **`executor_batch_rows`** (0 = off).

## Current Status

- The thread is **active**; the latest messages are from January 2026. The series is still **work in progress**.
- **v5** is the current revision. **Patches 0001–0003** (table AM batch API, heapam batch, SeqScan + TupleBatch, EXPLAIN BATCHES) are the intended first step for review and possible commit.
- **Patches 0004–0005** in v5 are **experimental** (ExecQualBatch, dedicated interpreter for batch qual).
- **v3** had the **segfault fix** (split EEOP helpers) for the TPC-H Q22 / batched-agg crash; the v4/v5 series builds on that.
- **Open items**: (1) Per-tuple path regression when batch qual (0004–0005) is in the tree but `executor_batch_rows=0` (e.g. 0% selectivity); (2) batch-scoped memory and ExprContext; (3) future work on cross-page batches and expression evaluation on native/compressed batch formats.

## Conclusion

Amit Langote’s “Batching in executor” series introduces a **batch-oriented path** in the PostgreSQL executor: table AMs can return batches of tuples, the executor can request and pass them via `TupleBatch`, and SeqScan is the first node wired to this path. Revisions v4 and v5 add **EXPLAIN (BATCHES)** for observability and prototype **batched qual evaluation** with a dedicated interpreter to keep the row-at-a-time path unchanged. Benchmarks show substantial gains (often 20–50%) when batching is on; the earlier “batching off” regression was addressed in v4, but a remaining issue is per-tuple path cost when the batch qual patches are applied and batching is disabled (e.g. 0% selectivity).

Reviewers have raised important points: alignment with other batch-like work (e.g. index prefetching), materialization and future expression-on-batch design, the TPC-H Q22 segfault (fixed in v3), and Daniil’s heap-batch review (addressed in v4). The foundation (0001–0003) plus EXPLAIN BATCHES is the current focus for review and possible commit.

## References

- [Mailing list thread: Batching in executor](https://www.postgresql.org/message-id/flat/CA%2BHiwqFfAY_ZFqN8wcAEMw71T9hM_kA8UtyHaZZEZtuT3UyogA%40mail.gmail.com)
- [PGConf.dev 2025: Can the Community Support an Additional Batch Executor?](https://wiki.postgresql.org/wiki/PGConf.dev_2025_Developer_Unconference#Can_the_Community_Support_an_Additional_Batch_Executor)
- [POSETTE 2025: Hacking Postgres Executor For Performance](https://www.youtube.com/watch?v=D3Ye9UlcR5Y)
- PostgreSQL documentation: [Table Access Method Interface](https://www.postgresql.org/docs/current/tableam.html), [Executor](https://www.postgresql.org/docs/current/executor.html)
