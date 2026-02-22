# Eliminating SPI from RI Triggers: A Fast Path for Foreign Key Checks

## Introduction

Referential Integrity (RI) triggers in PostgreSQL traditionally execute SQL queries via **SPI** (Server Programming Interface) to verify that inserted or updated rows in a referencing table have matching rows in the referenced (primary key) table. For bulk operations—large `INSERT` or `UPDATE` statements—this means starting and tearing down a full executor plan for **each row**, with significant overhead from `ExecutorStart()` and `ExecutorEnd()`.

Amit Langote has been working on eliminating this overhead by performing RI checks as **direct index probes** instead of SQL plans. The latest iteration of this work, "Eliminating SPI / SQL from some RI triggers - take 3," achieves up to **57% speedup** for bulk foreign key checks by bypassing the SPI executor and calling the index access method directly when the constraint semantics allow it.

The patch set has evolved through several versions, with Junwang Zhao joining the effort in late 2025. The current direction is a **hybrid fast-path + fallback** design: use a direct index probe for straightforward cases, and fall back to the existing SPI path when correctness requires executor behavior that would be difficult or risky to replicate.

## Why This Matters

Foreign key constraints are ubiquitous. Every `INSERT` or `UPDATE` into a referencing table triggers RI checks that must verify each new or modified row against the referenced table's primary key. With the traditional approach:

```sql
CREATE TABLE pk (a int PRIMARY KEY);
CREATE TABLE fk (a int REFERENCES pk);

INSERT INTO pk SELECT generate_series(1, 1000000);
INSERT INTO fk SELECT generate_series(1, 1000000);  -- 1M RI checks
```

Each of the 1 million inserts triggers an RI check that:

1. Builds a query plan to scan the PK index.
2. Runs `ExecutorStart()` and `ExecutorEnd()`.
3. Executes the plan to find (or not find) the matching row.

This per-row plan setup/teardown dominates the cost. With Amit's v3 patches, the same bulk insert drops from **~1000 ms** to **~432 ms** (57% faster) on his benchmark machine—by probing the PK index directly without going through the executor.

## Technical Background

### The Traditional RI Path

RI trigger functions in `ri_triggers.c` (e.g. `RI_FKey_check`) call `ri_PerformCheck()`, which:

1. Builds an SQL string for a query like `SELECT 1 FROM pk WHERE pk.a = $1`.
2. Uses `SPI_prepare` and `SPI_execute_plan` to run it.
3. The executor performs an index scan on the PK, returning a row if the referenced value exists.

This works correctly for all cases—partitioned tables, temporal foreign keys, concurrent updates—but pays the full plan-execution cost per row.

### The Fast-Path Idea

For simple foreign keys (non-partitioned referenced table, non-temporal semantics), the check is conceptually: "probe the PK index for this value; if found and lockable, the check passes." That can be done by:

1. Opening the PK relation and its unique index.
2. Building a scan key from the FK column values.
3. Calling `index_getnext()` (or equivalent) to find the tuple.
4. Locking it with `LockTupleKeyShare` under the current snapshot.

No SQL, no plan, no executor. Just a direct index probe and tuple lock.

## Patch Evolution

### v1: The Original Approach (December 2024)

The first patch set (3 patches) introduced:

- **0001**: Refactoring of the `PartitionDesc` interface to explicitly pass the snapshot needed for `omit_detached` visibility (detach-pending partitions). This addressed a bug where PK lookups could return incorrect results under `REPEATABLE READ` because `find_inheritance_children()`'s visibility of detach-pending partitions depended on `ActiveSnapshot`, which RI lookups were manipulating.
- **0002**: Avoid using SPI in RI trigger functions by introducing a direct index probe path.
- **0003**: Avoid using an SQL query for some RI checks—the main performance optimization.

Amit noted that temporal foreign key queries would remain on the SPI path, as their plans involve range overlap and aggregation and are not amenable to a simple index probe. He also added an equivalent of `EvalPlanQual()` for the new path to handle concurrent updates correctly under `READ COMMITTED`.

### v2: Junwang's Hybrid Fast Path (December 2025)

Junwang Zhao took the work forward with a hybrid design:

- **0001**: Add fast path for foreign key constraint checks. Applies when the referenced table is not partitioned and the constraint does not involve temporal semantics.
- **0002**: Cache fast-path metadata (operator hash entries, operator OIDs, strategy numbers, subtypes). At that stage, the metadata cache did not yet improve performance.

Benchmarks (1M rows, `numeric` PK / `bigint` FK):

- Head: INSERT 13.5s, UPDATE 15s
- Patched: INSERT 8.2s, UPDATE 10.1s

### v3: Amit's Rework with Per-Statement Caching (February 2026)

Amit reworked Junwang's patches into two patches:

- **0001**: Functionally complete fast path. Includes concurrency handling, `REPEATABLE READ` crosscheck, cross-type operators, security context (RLS/ACL), and metadata caching. Most logic lives in `ri_FastPathCheck()`; `RI_FKey_check` just gates the call and falls back to SPI when needed.
- **0002**: Per-statement resource caching. Instead of sharing `EState` between `trigger.c` and `ri_triggers.c`, a new **AfterTriggerBatchCallback** mechanism fires at the end of each trigger-firing cycle. It allows caching the PK relation, index, scan descriptor, and snapshot across all FK trigger invocations within a single cycle, rather than opening and closing them per row.

Benchmarks on Amit's machine:

| Scenario | Master | 0001 | 0001+0002 |
|----------|--------|------|-----------|
| 1M rows, numeric/bigint | 2444 ms | 1382 ms (43% faster) | 1202 ms (51% faster) |
| 1M rows, int/int | 1000 ms | 520 ms (48% faster) | 432 ms (57% faster) |

The incremental gain from 0002 (~13–17%) comes from eliminating per-row relation open/close, scan begin/end, slot allocation/free, and replacing per-row `GetSnapshotData()` with a snapshot copy in the cache.

## Design: When to Use Fast Path vs. SPI

The fast path applies when:

- The referenced table is **not partitioned**.
- The constraint does **not** involve temporal semantics (range overlap, `range_agg()`, etc.).
- Multi-column keys, cross-type equality (via index opfamily), collation matching, and RLS/ACL are all handled directly in the fast path.

The code falls back to SPI when:

1. **Concurrent updates or deletes**: If `table_tuple_lock()` reports that the target tuple was updated or deleted, the code delegates to SPI so that `EvalPlanQual` and visibility rules apply as today.
2. **Partitioned referenced tables**: Require routing the probe through the correct partition via `PartitionDirectory`. Can be added later as a separate patch.
3. **Temporal foreign keys**: Use range overlap and containment semantics that inherently involve aggregation; they stay on the SPI path.

Security behavior mirrors the existing SPI path: the fast path temporarily switches to the parent table's owner with `SECURITY_LOCAL_USERID_CHANGE | SECURITY_NOFORCE_RLS` around the probe, matching `ri_PerformCheck()`.

## Future Directions

**David Rowley** suggested off-list that batching multiple FK values into a single index probe could further improve performance, leveraging the `ScalarArrayOp` btree improvements from PostgreSQL 17. The idea: buffer FK values across trigger invocations in the per-constraint cache, build a `SK_SEARCHARRAY` scan key, and let the btree AM traverse matching leaf pages in one sorted pass instead of one tree descent per row. Locking and recheck would remain per-tuple. This could be explored as a separate patch on top of the current series.

## Current Status

- The series is in PG19-Drafts. Amit moved it there in October 2025; Junwang Zhao is continuing the work.
- Amit's v3 patches (February 2026) are in reasonable shape and ready for review. He welcomes feedback, especially on concurrency handling in `ri_LockPKTuple()` and the snapshot lifecycle in 0002.
- Pavel Stehule has offered to help with testing and review.

## Conclusion

Eliminating SPI from RI triggers for simple foreign key checks yields substantial performance gains for bulk operations. The hybrid fast-path + fallback design addresses reviewer concerns about correctness by deferring to SPI whenever executor behavior is non-trivial to replicate. The per-statement resource caching in v3 adds a second layer of optimization by amortizing relation/index setup across many rows within a single trigger-firing cycle.

For workloads with large bulk inserts or updates on tables with foreign keys—common in ETL, staging loads, and data migrations—this work could significantly reduce runtimes. The current limitations (partitioned PKs, temporal FKs) leave those cases on the existing path, preserving correctness while optimizing the majority of FK workloads.

## References

- [Thread: Eliminating SPI / SQL from some RI triggers - take 3](https://www.postgresql.org/message-id/flat/CA%2BHiwqF4C0ws3cO%2Bz5cLkPuvwnAwkSp7sfvgGj3yQ%3DLi6KNMqA%40mail.gmail.com)
- [1] Simplifying foreign key/RI checks (earlier thread)
- [2] Eliminating SPI from RI triggers - take 2 (earlier thread)
