# pg_plan_advice: A New Approach to PostgreSQL Query Plan Control

## Introduction

PostgreSQL's query planner is sophisticated and generally produces excellent execution plans. However, experienced DBAs and developers occasionally encounter situations where they wish they could influence or stabilize the planner's decisions. Robert Haas from EnterpriseDB has been working on a significant new contrib module called `pg_plan_advice` that aims to address this long-standing need.

This article examines the [pg_plan_advice thread](https://www.postgresql.org/message-id/flat/CA%2BTgmoZ-Jh1T6QyWoCODMVQdhTUPYkaZjWztzP1En4%3DZHoKPzw%40mail.gmail.com) on the pgsql-hackers mailing list, which has been actively discussed since October 2025.

## What is pg_plan_advice?

`pg_plan_advice` is a proposed contrib module that introduces a special-purpose "advice mini-language" for controlling key planning decisions. The module can:

- **Generate advice strings** from existing query plans using `EXPLAIN (PLAN_ADVICE)`
- **Apply advice strings** via the `pg_plan_advice.advice` GUC parameter to reproduce or constrain future planning decisions

The advice language allows control over:

- **Join order**: Which tables are joined in what sequence
- **Join methods**: Nested loop, merge join, hash join
- **Scan types**: Sequential scan, index scan (with specific index selection)
- **Parallelism**: Where and how parallel execution is used
- **Partitionwise joins**: How partitioned table joins are handled

## Key Design Philosophy

Robert Haas emphasizes in the README that the principal use case is not about users "out-planning the planner" but rather about **reproducing plans that worked well in the past**:

> "We don't need to accept the proposition that users can out-plan the planner. We only need to accept that they can tell good plans from bad plans better than the planner. That is a low bar to clear. The planner never finds out what happens when the plans that it generates are actually executed, but users do."

This positions `pg_plan_advice` as a **plan stability tool** rather than a hint system for micromanaging the optimizer.

## Technical Architecture

### The Relation Identifier System

One of the most innovative aspects of `pg_plan_advice` is its relation identifier system. This system provides **unambiguous references** to parts of a query, handling complex scenarios like:

- Multiple references to the same table with different aliases
- Subqueries and CTEs
- Partitioned tables and their partitions

The identifier syntax uses special notation like `t#2` to distinguish between the first and second occurrence of table `t` in a query.

### Example Usage

Here's an example from Jakub Wartak's testing showing the power of the system:

```sql
-- Generate advice for a query with aliasing
EXPLAIN (PLAN_ADVICE, COSTS OFF) 
SELECT * FROM (SELECT * FROM t1 a JOIN t2 b USING (id)) a, t2 b, t3 c 
WHERE a.id = b.id AND b.id = c.id;

-- Output includes:
-- Generated Plan Advice:
--   JOIN_ORDER(a#2 b#2 c)
--   MERGE_JOIN_PLAIN(b#2 c)
--   SEQ_SCAN(c)
--   INDEX_SCAN(a#2 public.t1_pkey)
--   NO_GATHER(c a#2 b#2)
```

You can then selectively apply constraints:

```sql
-- Force a specific scan type
SET pg_plan_advice.advice = 'SEQ_SCAN(b#2)';

-- Re-explain to see the new plan
EXPLAIN (PLAN_ADVICE, COSTS OFF) 
SELECT * FROM (SELECT * FROM t1 a JOIN t2 b USING (id)) a, t2 b, t3 c 
WHERE a.id = b.id AND b.id = c.id;

-- The output shows:
-- Supplied Plan Advice:
--   SEQ_SCAN(b#2) /* matched */
```

## Patch Structure (v10)

The implementation is split into five patches:

| Patch | Description | Size |
|-------|-------------|------|
| 0001 | Store information about range table flattening | 7.8 KB |
| 0002 | Store information about elided nodes in the final plan | 9.8 KB |
| 0003 | Store information about Append node consolidation | 40.4 KB |
| 0004 | Allow for plugin control over path generation strategies | 56.1 KB |
| 0005 | WIP: Add pg_plan_advice contrib module | 399.1 KB |

The first four patches add necessary infrastructure to the planner, while the fifth contains the actual module. This separation allows the infrastructure to potentially benefit other extensions in the future.

## Community Review and Testing

The thread has seen active participation from several community members:

### Jakub Wartak (EDB)
Conducted extensive TPC-H benchmark testing and found several bugs:
- Crashes in debug/ASAN builds with NULL pointer dereferences
- Issues with semijoin uniqueness detection without statistics
- Join order advice conflicts in complex queries

### Jacob Champion (EDB)
Applied fuzzing techniques to discover edge cases:
- Parser crashes with malformed advice strings
- Issues with partition-related advice on non-partitioned tables
- AST utility bugs revealed through corpus-based fuzzing

### Other Contributors
- **Alastair Turner**: Appreciated the ability to test alternative plans
- **Hannu Krosing** (Google): Referenced VLDB research showing 20% of real-world queries have 10+ joins
- **Lukas Fittl**: Interested in pg_stat_statements integration possibilities

## Issues Discovered and Fixed

The collaborative review process has uncovered and fixed several issues across versions:

1. **Compiler warnings** (gcc-13, clang-20) - Fixed in early versions
2. **NULL pointer crashes** in `pgpa_join_path_setup()` when extension state wasn't allocated
3. **Join order conflict detection** incorrectly treating join method advice as positive constraints
4. **Semijoin uniqueness tracking** not working correctly without PLAN_ADVICE in EXPLAIN
5. **Partial match detection** in nested join order specifications

## Current Status

As of v10 (posted January 15, 2026):

- The patch is registered in [Commitfest](https://commitfest.postgresql.org/patch/6184/)
- Still marked as **WIP (Work In Progress)**
- Active testing continues, particularly with TPC-H queries
- Robert Haas is seeking substantive code review, especially for patch 0001

## Implications for PostgreSQL Users

If committed, `pg_plan_advice` would provide:

1. **Plan Stability**: Capture and reproduce known-good query plans
2. **Debugging Aid**: Understand why the planner makes specific choices
3. **Testing Tool**: Experiment with alternative plan shapes without modifying queries
4. **Production Safety Net**: Guard against unexpected plan regressions after statistics changes

## Comparison with pg_hint_plan

Unlike the popular `pg_hint_plan` extension, `pg_plan_advice` focuses on **round-trip safety**:

- Plans can be captured and reapplied reliably
- The relation identifier system handles complex aliasing automatically
- Designed to work with any query structure without manual identifier management

## Conclusion

`pg_plan_advice` represents a significant step forward in PostgreSQL's planner extensibility story. Rather than replacing the optimizer's judgment, it provides a safety mechanism for preserving proven execution strategies. The active community review process has already improved the code substantially, and continued testing is helping ensure robustness.

For DBAs managing complex workloads, particularly those with queries that occasionally suffer from plan regressions, this module offers a promising solution that works *with* the planner rather than against it.

---

**Thread Link**: [pg_plan_advice - pgsql-hackers](https://www.postgresql.org/message-id/flat/CA%2BTgmoZ-Jh1T6QyWoCODMVQdhTUPYkaZjWztzP1En4%3DZHoKPzw%40mail.gmail.com)

**Commitfest Entry**: [CF 6184](https://commitfest.postgresql.org/patch/6184/)

**Author**: Robert Haas (EnterpriseDB)

**Reviewers**: Jakub Wartak, Jacob Champion, Alastair Turner, Hannu Krosing, John Naylor, and others
