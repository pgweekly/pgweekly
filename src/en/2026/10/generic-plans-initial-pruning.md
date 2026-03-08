# Generic Plans and Initial Pruning: Fewer Locks for Partitioned Tables

## Introduction

In December 2021, Amit Langote proposed a patch to speed up execution of **generic plans** over partitioned tables. Generic plans (e.g. from prepared statements with parameters) cannot prune partitions at plan time, so they contain nodes for every partition. That makes `AcquireExecutorLocks()` a major bottleneck: it locks every relation in the plan, and partition count grows without bound. This post summarizes the idea, the benchmark gains, and the safety and design discussion that followed on pgsql-hackers.

## Why This Matters

When you use a prepared statement like:

```sql
PREPARE q AS SELECT * FROM partitioned_table WHERE key = $1;
EXECUTE q(123);
```

with `plan_cache_mode = force_generic_plan` (or after the planner has chosen a generic plan), the plan is shared across executions and has no plan-time pruning. So the plan tree includes **all** partitions. Before each execution, `CheckCachedPlan()` must ensure the plan is still valid; most of that cost is in `AcquireExecutorLocks()`, which locks every relation in the plan. As partition count grows (hundreds or thousands), lock acquisition dominates and throughput drops sharply.

A previous attempt by David Rowley was to delay locking until after "initial" (pre-execution) pruning in the executor. That was rejected because leaving some partitions unlocked opened race conditions: a concurrent session could alter a partition after the plan was deemed valid but before execution, invalidating part of the plan.

Amit's approach keeps locking at plan-check time but **reduces the set of relations to lock** by reusing the same "initial" pruning logic that the executor will later use. Only partitions that survive initial pruning are locked, so the plan stays safe while lock count scales with the number of partitions that are actually used.

## Technical Analysis

### The Idea: Prune Before Locking

Append and MergeAppend nodes for partitioned tables carry **partition pruning information**: which subplans are discarded by "initial" (pre-execution) steps versus "execution-time" steps. Initial steps depend only on values available before execution (e.g. bound parameters), not on per-row values. The patch teaches `AcquireExecutorLocks()` to:

1. Walk the plan tree as it does today to collect relations to lock.
2. For Append/MergeAppend nodes that have **initial pruning steps** (`contains_init_steps`), run those steps (using the same logic as the executor) to get the set of subplans that survive.
3. Only add relations from those surviving subplans to the lock set.

So the lock set is exactly the set of relations that will be used when the plan runs. No partition that is pruned away by initial steps is locked, and no partition that is used is left unlocked.

### Duplication of Pruning

Initial pruning is therefore performed **twice** for generic plans: once in `AcquireExecutorLocks()` to decide what to lock, and again in `ExecInit[Merge]Append()` to decide which partition subnodes to create. Amit noted he couldn't find a clean way to avoid this duplication without restructuring where locking happens (e.g. moving it into executor startup), which would be a larger change.

### Benchmark

Using pgbench with a partitioned database and `plan_cache_mode = force_generic_plan`:

- **HEAD**: throughput falls as partition count increases (e.g. 32 partitions ≈ 20.5k tps, 2048 partitions ≈ 1.3k tps).
- **Patched**: throughput stays much higher (e.g. 32 partitions ≈ 27.5k tps, 2048 partitions ≈ 16.3k tps).

So the patch removes most of the scaling cost from lock acquisition when generic plans are used with many partitions.

## Community Insights

### When Does This Apply?

Ashutosh Bapat asked when "pre-execution" pruning instructions exist so that this approach helps. Amit clarified:

- The main use case is **prepared statements** that use a **generic plan**, e.g. `PREPARE q AS SELECT * FROM partitioned_table WHERE key = $1;` with `EXECUTE q(...)`.
- Other bottlenecks (e.g. executor startup/shutdown code that walk the full range table) are unchanged by this patch.

### Code Review (Amul Sul)

Amul suggested several cleanups for the v1 patch:

- Move declarations inside the `if (pruneinfo && pruneinfo->contains_init_steps)` block.
- Add a short comment that when the condition is false, `plan_tree_walker()` continues to child nodes, so locking behavior remains correct.
- Prefer `GetLockableRelations_worker()` (or equivalent) over adding a new `get_plan_scanrelids()`.
- Use `plan_walk_members()` for CustomScan like other node types.
- Let the **caller** create/free the temporary `EState` used for pruning in the lock path, instead of doing it inside the lock-collection helper.
- Use `foreach_current_index()` in the relevant loops for clarity.

### Safety (Robert Haas)

Robert raised two important points.

**1. Partly valid plans**
Today we only run plans that are fully valid: we lock every relation, so we accept invalidation messages and detect DDL that might invalidate the plan. If we skip locking some relations, we might never see invalidations for them. For example:

- A partition has an extra index and the plan uses an Index Scan on it.
- That partition is pruned by initial steps, so we don't lock it.
- Another session drops the index.
- We still consider the plan valid. We don't execute the pruned part, but code that walks the whole plan (e.g. EXPLAIN, auto_explain) might touch that node and break (e.g. looking up the index name).

So we'd be in a situation where the plan is "partly valid," which we don't have today. Robert wasn't sure there is a concrete bug in core from that, but it's a new class of risk.

Amit replied that he'd looked for places that inspect the plan tree before executor init and hadn't found one that would touch pruned-off parts; EXPLAIN runs after `ExecutorStart()`, which builds the PlanState tree and thus only the non-pruned portion. He agreed it's not something to assert with certainty.

**2. Lock set vs. init set must match**
Doing initial pruning in two places means two separate computations. If they ever disagree (e.g. due to a function misdeclared as IMMUTABLE but actually VOLATILE), we could lock one set of partitions and then initialize a different set. Robert argued we should ensure that cannot happen rather than rely on the two being always identical.

Amit agreed the patch assumes initial pruning is deterministic (no VOLATILE in the pruning expressions). Misdeclared IMMUTABLE could lead to Assert failures or, in non-assert builds, using an unlocked partition, which would be bad.

## Technical Details

### Applicability

- **Generic plans only**: Custom plans can prune at plan time, so they don't have the "all partitions in the plan" problem to the same degree.
- **Initial pruning only**: Only partitions eliminated by **initial** (pre-execution) pruning are skipped for locking. Partitions pruned at execution time (e.g. by runtime parameter values in a different execution) are still in the plan and would still be locked; the patch doesn't change that.

### Edge Cases

- EXPLAIN / auto_explain: The concern is that they might walk plan nodes for relations we didn't lock. Amit's analysis is that they run after executor init, so they only see the initialized (post-pruning) plan.
- Incorrect VOLATILE/IMMUTABLE: Could make lock set and init set differ; the patch doesn't add extra guards for that.

## Current Status

The thread carried a single patch version (v1) and did not show a follow-up commit in the thread. The discussion highlighted:

- Strong benchmark gains for generic plans on many partitions.
- Need to address code-style and refactor suggestions (EState lifecycle, walker usage, comments).
- Open design points: ensuring lock set and init set cannot diverge, and whether "partly valid" plans are acceptable for code paths that walk the full plan tree.

## Conclusion

Amit's patch reduces the cost of `AcquireExecutorLocks()` for generic plans over partitioned tables by locking only partitions that survive initial pruning, avoiding the race conditions of the earlier "delay locking" approach. Benchmarks show large throughput improvements when many partitions are present. The discussion clarified the intended use case (prepared statements with generic plans), raised valid safety and consistency concerns (partly valid plans, duplicate pruning), and produced concrete code-review suggestions. Implementing similar logic in a way that guarantees a single pruning result for both locking and executor init would strengthen the approach and may require a somewhat larger refactor of where and how locks are acquired.

## References

- [Thread: generic plans and "initial" pruning](https://www.postgresql.org/message-id/flat/CA%2BHiwqFGkMSge6TgC9KQzde0ohpAycLQuV7ooitEEpbKB0O_mg%40mail.gmail.com) (Amit Langote, Dec 2021)
- [1] David Rowley's earlier proposal: [message-id](https://www.postgresql.org/message-id/CAKJS1f_kfRQ3ZpjQyHC7=PK9vrhxiHBQFZ+hc0JCwwnRKkF3hg@mail.gmail.com)
- [2] Race condition with delayed locking: [message-id](https://www.postgresql.org/message-id/CAKJS1f99JNe+sw5E3qWmS+HeLMFaAhehKO67J1Ym3pXv0XBsxw@mail.gmail.com)
