# GOO: A Greedy Join Search Algorithm for Large Join Problems

## Introduction

PostgreSQL uses different strategies for join ordering depending on query complexity. For queries with fewer than `geqo_threshold` relations (default 12), the planner uses **dynamic programming (DP)** to find the optimal join order. For larger join graphs, it falls back to **GEQO (Genetic Query Optimizer)**, which uses a genetic algorithm to search the space of possible join orders in a more scalable but heuristic way. GEQO has well-known drawbacks: it can be slower than DP for moderate join counts due to overhead, and it lacks tuning knobs like a reproducible seed for debugging.

In December 2025, Chengpeng Yan proposed **GOO (Greedy Operator Ordering)** on the pgsql-hackers mailing list—a deterministic greedy join-order search method intended as an alternative to GEQO for large join problems. The algorithm is based on the 1998 DEXA paper by Leonidas Fegaras, "A New Heuristic for Optimizing Large Queries."

## Why This Matters

- **Planning time**: On star/snowflake and TPC-DS style workloads, GOO can plan much faster than GEQO (e.g., ~5s vs ~20s for EXPLAIN on 99 TPC-DS queries in one test), while DP remains fastest when the join count is below the threshold.
- **Plan quality**: The goal is to be "good enough" where GEQO is used today—reducing tail regressions and offering more predictable behavior than a genetic search.
- **Memory**: GOO’s structure (iterative, commit-to-one-join-per-step) suggests lower memory use than full DP and different characteristics than GEQO; the author plans to measure this.

Understanding this thread helps you see how PostgreSQL might evolve for queries with many joins and where GOO fits next to DP and GEQO.

## Technical Analysis

### The GOO Algorithm

GOO builds the join order incrementally:

1. Start with one "clump" per base relation.
2. At each step, consider all **legal** join pairs (clumps that can be joined under the query’s join constraints).
3. For each pair, build the join relation and use the planner’s existing cost model to get a total cost.
4. **Choose the pair with the lowest estimated total cost**, merge them into one clump, and repeat.
5. Stop when a single clump remains; that clump’s best path is the chosen plan.

So the "greedy" choice is: among all current clumps, always do the join that looks cheapest right now. The paper uses estimated result size; the patch uses the planner’s existing `total_cost` for consistency with the rest of PostgreSQL.

**Complexity**: Time is O(n³) in the number of base relations n: (n−1) merges, and at each step O(k²) pairs for k clumps.

### Integration with the Planner

The patch adds:

- **`enable_goo_join_search`** — GUC to turn GOO on (default off).
- **Threshold**: For now, GOO reuses `geqo_threshold`: when the number of join levels is ≥ `geqo_threshold`, and GOO is enabled, the planner calls `goo_join_search()` instead of GEQO. So GOO is positioned as a **GEQO replacement**, not a replacement for DP.

Relevant code in `allpaths.c`:

```c
else if (enable_goo_join_search && levels_needed >= geqo_threshold)
    return goo_join_search(root, levels_needed, initial_rels);
else if (enable_geqo && levels_needed >= geqo_threshold)
    return geqo(root, levels_needed, initial_rels);
```

### Greedy Strategies Under Experiment

The author experimented with different signals for "cheapest" in the greedy step:

| Strategy      | Description |
|---------------|-------------|
| **Cost**      | Use the planner’s `total_cost` for the join (baseline). |
| **Result size** | Use estimated output size in bytes (`reltarget->width * rows`). |
| **Rows**      | Use estimated output row count. |
| **Selectivity** | Use join selectivity (output rows / (left_rows × right_rows)). |
| **Combined**  | Run GOO twice (cost and result_size), then pick the plan with lower final estimated cost. |

Findings:

- **Cost** alone can produce very bad tail cases (e.g., JOB: max 431×, many ≥10× regressions).
- **Result size** is better on average but still has bad tails (e.g., 67× max on JOB).
- **Combined** (cost + result_size, pick cheaper) improves robustness: best geometric mean, no ≥10× regressions in the JOB subset, worst case 8.68×.

So the discussion shifted from "which single metric?" to "how to reduce tail risk?"—e.g., by combining multiple greedy strategies and choosing the better plan.

## Community Insights

### Benchmark Clarification

Dilip Kumar initially questioned the use of pgbench, since default pgbench queries do not exercise join search. The author clarified: the numbers came from **custom star-join and snowflake workloads** from Tomas Vondra’s earlier thread, not from default pgbench. Those workloads use multi-table joins and empty tables, so the reported throughput mainly reflects **planning time** of DP vs GEQO vs GOO.

### Crashes and the Eager-Aggregation Fix (v1 → v2)

Tomas Vondra reported crashes when running EXPLAIN on TPC-DS queries. The backtrace showed `sort_inner_and_outer()` with `inner_path = NULL`. Root cause: in some paths, the GOO code built join relations without calling `set_cheapest()`, so later code saw missing cheapest path. Tomas also narrowed it to queries with **aggregates in the select list** (e.g., TPC-DS Q7).

The fix in **v2** was to correctly handle **eager aggregation**: the planner can create grouped/base relations that require a proper cheapest path. After the v2 fix, all 99 TPC-DS queries could be planned without crashes.

### GEQO Slower Than DP for Moderate Join Counts

Tomas reported EXPLAIN times for 99 TPC-DS queries (3 scales × 0/4 workers):

- **master (DP)**: 8s
- **master/geqo**: 20s
- **master/goo**: 5s

So for that workload, GEQO was slower than DP, and GOO was fastest. John Naylor and Pavel Stehule noted that GEQO is designed to win only when the join problem is large enough; for smaller or moderate join counts, its overhead can dominate. So the comparison should focus on the range where GEQO is actually used (e.g., relation count above `geqo_threshold`).

### TPC-DS Execution Results (Tomas Vondra)

Tomas shared full TPC-DS run results (scale 1 and 10, 0 and 4 workers). Summary of total duration (all 99 queries):

- **Scale 1**: GOO was slower than both master and GEQO (e.g., ~1124s vs 399s geqo vs 816s master).
- **Scale 10**: GOO was faster (e.g., ~1859s vs 2325s geqo vs 2439s master).

So GOO behaved worse at small scale and better at larger scale—suggesting workload- and scale-dependent behavior. Tomas suggested inspecting queries that got worse to refine heuristics and to test with larger data sets and cold cache.

### TPC-H: Failure Modes of Single-Metric Greedy (v3)

The author ran TPC-H SF=1 with four strategies: rows, selectivity, result size, and cost. Main lessons:

- **Q20**: Join between `partsupp` and an aggregated `lineitem` subquery. Row count was misestimated by orders of magnitude (tens vs hundreds of thousands). Output-oriented rules (rows, selectivity, result size) favored this join very early because it "looked" very shrinking; in reality it produced a huge intermediate and blew up downstream cost. So **bad estimates** can make output-oriented greedy rules fail badly.
- **Q7**: Cost-based greedy chose a locally cheap join that created a large many-to-many intermediate, which made later joins much more expensive. So **locally optimal cost** can be **globally bad**.

Tomas pointed out: Q20 is largely an estimation problem (garbage in, garbage out); Q7 is inherent to greediness—locally good choices can be globally poor, and that’s not fixable by picking a different single metric.

### JOB and Combined Strategy (v4)

On the full JOB workload, the **combined** strategy (cost + result_size, pick cheaper) gave:

- Best geometric mean (0.953 vs DP).
- No regressions ≥10×; max 8.68×.
- Fewer bad tail cases than GOO(cost) or GOO(result_size) alone.

So increasing plan diversity by running two greedy criteria and selecting the cheaper plan helps avoid catastrophic plans without much extra planning cost.

### Scope: GOO as GEQO Replacement

Tomas asked whether the goal was to replace DP or GEQO. The author confirmed: **GOO is intended as a GEQO replacement**, not to replace DP. When the join count is below `geqo_threshold`, DP should remain in use.

### Literature and Next Steps

Tomas pointed to the CIDR 2021 paper "Simplicity Done Right for Join Ordering" (Hertzschuch et al.), which focuses on robustness (e.g., upper bound / worst-case join orders) and trusting base relation estimates—potentially relevant to nestloop blowups when cardinality is over-optimistic. The author plans to establish a solid baseline with the current approach, then incorporate ideas from such work incrementally.

## Technical Details

### Implementation Approach

- New files: `src/backend/optimizer/path/goo.c`, `src/include/optimizer/goo.h`.
- GOO builds join relations by repeatedly calling the existing planner routines (e.g., `make_join_rel`, path creation), so it reuses the same cost model and path types as DP/GEQO.
- Memory is managed with multiple memory contexts to limit usage during candidate evaluation.

### Edge Cases and Robustness

- **Eager aggregation**: v2 fixed crashes by ensuring join relations created during GOO have proper cheapest paths set (so code like `sort_inner_and_outer` never sees NULL inner_path).
- **Cardinality misestimation**: All approaches suffer when estimates are wrong; GOO’s sensitivity differs by strategy (e.g., output-oriented rules can be worse when row estimates are off). The combined strategy is aimed at reducing tail risk rather than fixing estimation.
- **Structural limits**: Some shapes (e.g., star with fan-out) cause both cost and result_size to pick similarly bad plans; that’s a limitation of myopic greedy enumeration.

### Performance Considerations

- **Planning time**: GOO is O(n³) and in practice was faster than GEQO in the reported benchmarks; the author plans to add explicit planning-time and memory measurements.
- **Execution time**: Highly workload-dependent; GOO can be better or worse than GEQO/DP depending on scale and query mix (e.g., TPC-DS scale 1 vs 10, JOB GEQO-relevant subset).

## Current Status

- **Patch**: v4 was the latest at thread closure. v4-0001 is the core GOO implementation (unchanged from v3-0001); v4-0002 adds test-only GUCs and machinery for trying different greedy strategies (e.g., combined).
- **Goal**: Establish GOO as a viable GEQO replacement—same threshold, better or comparable plan quality and planning time, with reduced tail regressions.
- **Next steps** (from the author): Broader evaluation (more workloads, larger join graphs, cold cache, larger scale factors); consider adding selectivity to the combined strategy; measure planning time and memory; look at tunability and graceful degradation (e.g., DP up to a resource limit then greedy).

## Conclusion

The GOO thread shows a serious effort to replace GEQO with a deterministic, greedy join ordering algorithm that:

- Reuses the existing cost model and planner infrastructure.
- Improves planning time in several benchmarks compared to GEQO.
- Reduces worst-case plan quality by combining multiple greedy strategies (e.g., cost and result_size) and picking the cheaper plan.

Limitations are acknowledged: greedy methods are inherently local and can produce bad plans when estimates are wrong or when the join graph has unfavorable structure. The focus has shifted to **robustness and tail behavior** rather than perfect single-metric tuning. For PostgreSQL users, this is a patch to watch: if committed, it would offer an alternative to GEQO for complex queries, with different trade-offs and potentially better predictability and planning performance.

### References

- [1] **Discussion thread**: [Add a greedy join search algorithm to handle large join problems](https://www.postgresql.org/message-id/3FF63E99-AB4F-41A9-BC78-AAB28823FBD0%40Outlook.com) (pgsql-hackers, Dec 2025 – Jan 2026)
- [2] Leonidas Fegaras, "A New Heuristic for Optimizing Large Queries", DEXA '98. [ACM](https://dl.acm.org/doi/10.5555/648311.754892) / [PostScript](https://lambda.uta.edu/order.ps)
- [3] Star/snowflake join thread: [pgsql-hackers](https://www.postgresql.org/message-id/a22ec6e0-92ae-43e7-85c1-587df2a65f51%40vondra.me)
- [4] CIDR 2021 — Simplicity Done Right for Join Ordering: [VLDB](https://vldb.org/cidrdb/2021/simplicity-done-right-for-join-ordering.html)
- [5] PostgreSQL documentation: [Genetic Query Optimizer](https://www.postgresql.org/docs/current/geqo.html)
