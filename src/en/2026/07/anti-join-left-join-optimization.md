# Reducing LEFT JOIN to ANTI JOIN: A Planner Optimization for "WHERE col IS NULL"

## Introduction

In late December 2025, Nicolas Adenis-Lamarre raised a planner optimization on the pgsql-hackers list: automatically detect **anti-join** patterns in queries that use `LEFT JOIN ... WHERE right_table.column IS NULL` when that column is known to be non-nullable (e.g. NOT NULL or primary key). Such queries mean "rows from the left side with no matching row on the right," which is exactly what an **anti-join** expresses. Recognizing this lets the planner choose a dedicated anti-join plan instead of a generic left join + filter, often with better performance.

The discussion drew in Tom Lane, David Rowley, Tender Wang, Richard Guo, and others. A patch evolved through several versions, was submitted to CommitFest, and received detailed review that uncovered correctness issues with nested outer joins and inheritance. This post summarizes the idea, the implementation approach, and the current status.

## Why This Matters

Many developers write "find rows in A with no match in B" as:

```sql
SELECT a.*
FROM a
LEFT JOIN b ON a.id = b.a_id
WHERE b.some_not_null_col IS NULL;
```

Because the join is a LEFT JOIN, unmatched rows from `a` have NULL in all columns from `b`. Filtering on `b.some_not_null_col IS NULL` (when that column is NOT NULL in `b`) therefore keeps only those unmatched rows. Semantically this is an **anti-join**: "rows in A that do not have a matching row in B."

If the planner does not recognize this pattern, it may implement it as a normal left join plus a filter. If it *does* recognize it, it can use an explicit **Hash Anti Join** (or similar), which can be more efficient and can unlock better join order choices. The optimization is "non-mandatory" in the sense that a skilled user could rewrite the query as `NOT EXISTS` or `NOT IN` (with due care for NULLs), but automatic detection helps all users and keeps the original SQL readable.

## Technical Background

PostgreSQL already has logic to reduce outer joins in certain cases. In particular:

- **Commits 904f6a593 and e2debb643** added infrastructure that the planner can use for this kind of reduction.
- In `reduce_outer_joins_pass2`, the planner already tries to reduce `JOIN_LEFT` to `JOIN_ANTI` when the join's own quals are strict for some var that was **forced null** by higher qual levels (e.g. by an upper `WHERE`).

The existing comment in that area noted that there are other ways to detect an anti-join—for example, checking whether vars from the right-hand side are non-null because of **table constraints** (NOT NULL, etc.). That was left for later; Nicolas's proposal and Tender's patch implement exactly that: use NOT NULL (and related) information to detect when `WHERE rhs_col IS NULL` implies "no match," and thus when a LEFT JOIN can be reduced to an ANTI JOIN.

## Patch Evolution

### Nicolas's initial patch

Nicolas sent a draft patch that:

- Detected the pattern "left join b where x is null" when `x` is a non-null var from the right-hand side (RTE).
- Was intentionally quick-and-dirty to see if the change was feasible.

He also listed other ideas (removing redundant DISTINCT/GROUP BY, folding double ORDER BY, anti-join on `NOT IN`, and a way to "view the rewritten query"). Those were discussed briefly but are not the focus of this post.

### Tom Lane and David Rowley

**Tom Lane** replied that:

- The optimization is reasonable and the new infrastructure (904f6a593, e2debb643) should be used.
- The draft should not leave nearby comments outdated; keeping comments accurate is mandatory.

**David Rowley** suggested:

- Using `find_relation_notnullatts()` and comparing with `forced_null_vars`, with care for `FirstLowInvalidHeapAttributeNumber`.
- Searching the archives for prior work on **UniqueKeys** (for redundant DISTINCT removal).
- Being cautious about "remove double order" and "NOT IN" anti-join; both have been discussed before and have subtle edge cases.
- Noting that "view the rewritten query" is ambiguous—many optimizations cannot be expressed back as a single SQL statement.

### Tender Wang's implementation (v2–v4)

Tender Wang provided a patch that:

- Used the infrastructure from 904f6a593 and e2debb643.
- Updated the comments in `reduce_outer_joins_pass2` to describe the new case (detecting anti-join via NOT NULL constraints on the RHS).
- Added regression tests.

Nicolas then:

- Confirmed that Tender's patch was correct (after re-testing).
- Suggested an early exit: only run the new logic when `forced_null_vars != NIL`, to avoid calling `find_nonnullable_vars` and `have_var_is_notnull` on every left join when most have no forced-null vars.
- Contributed extra regression tests using **new tables** (with NOT NULL constraints) instead of modifying existing test tables like `tenk1`.

**Tom Lane** clarified that modifying common test objects (e.g. in `test_setup.sql`) is a bad idea, as it can change planner behavior and break or alter other tests. New tests should use new tables or existing ones that already match the needed properties.

Tender incorporated Nicolas's early-exit and regression tests into a single **v4** patch and submitted it to [CommitFest](https://commitfest.postgresql.org/patch/6375/).

## Richard Guo's review: correctness issues

Richard Guo reviewed the v4 patch and found two correctness problems.

### 1. Nested outer joins

When the right-hand side of the left join itself contains an outer join, a column that is NOT NULL in its base table can still become NULL in the join result. Reducing the outer join to an anti-join in that case is wrong.

Example (tables `t1`, `t2`, `t3` with columns e.g. `(a NOT NULL, b, c)`):

```sql
EXPLAIN (COSTS OFF)
SELECT * FROM t1
LEFT JOIN (t2 LEFT JOIN t3 ON t2.c = t3.c) ON t1.b = t2.b
WHERE t3.a IS NULL;
```

Here `t3.a` is NOT NULL in `t3`, but because of the inner `t2 LEFT JOIN t3`, a row from `t1` can be joined to the subquery and still have `t3.a` NULL (when there is no matching row in `t3`). So the upper join must remain a left join; converting it to an anti-join would drop rows incorrectly.

The patch was treating any var from a NOT NULL column as "safe" for the anti-join reduction without considering whether that var could be nulled by a lower-level outer join. Richard noted that we don't currently record `varnullingrels` in `forced_null_vars`, so a simple fix would be to only do this optimization when the RHS has no outer joins (`right_state->contains_outer` false), but that would be too restrictive.

His proposed direction: in `reduce_outer_joins_pass1_state`, record the relids of base rels that are nullable within each subtree. Then, when checking NOT NULL constraints, skip vars that come from those rels. He attached a **v5** patch illustrating this idea.

### 2. Inheritance

For inheritance parent tables, some child tables might have a NOT NULL constraint on a column while others do not. The patch did not account for that; the second issue is more straightforward to fix than the nested-outer-join case.

## Other discussion points

- **Pavel Stehule** asked participants to avoid top-posting on the list; the PostgreSQL wiki has guidelines on [mailing list style](https://wiki.postgresql.org/wiki/Mailing_Lists).
- **Constants from subqueries**: Nicolas noted that a case like `SELECT * FROM a LEFT JOIN (SELECT 1 AS const1 FROM b) x WHERE x.const1 IS NULL` is not handled; he considered it not worth handling.

## Current Status

- The **v4** patch (with early exit and regression tests) was submitted to CommitFest (patch 6375).
- **Richard Guo's v5** patch addresses the nested-outer-join and inheritance issues by tracking nullable base rels and tightening when NOT NULL can be used for the reduction.
- As of the thread, the discussion was ongoing; the final resolution (e.g. commit of a revised patch) would be tracked on the list and in CommitFest.

## Conclusion

Automatically reducing `LEFT JOIN ... WHERE rhs_not_null_col IS NULL` to an anti-join when the column is provably non-nullable is a useful planner optimization that can improve performance without requiring users to rewrite queries. The patch has evolved from a draft to an implementation using the existing planner infrastructure, with regression tests and an early-exit optimization. Reviewer feedback has identified important correctness constraints: the RHS may contain nested outer joins or inheritance, so NOT NULL must be applied only when the var cannot be nulled by lower joins or by inheritance. Follow-up work centers on Richard's approach (recording nullable base rels and restricting the NOT NULL check accordingly) and on handling inheritance safely.

## References

- [Discussion thread: Planner : anti-join on left joins](https://www.postgresql.org/message-id/flat/CACPGbctKMDP50PpRH09in%2BoWbHtZdahWSroRstLPOoSDKwoFsw%40mail.gmail.com)
- [CommitFest patch 6375](https://commitfest.postgresql.org/patch/6375/)
- David Rowley's references: [UniqueKeys](https://www.postgresql.org/search/?m=1&q=UniqueKeys&l=1&d=-1&s=d), [NOT IN / anti-join](https://www.postgresql.org/message-id/flat/3793.1565689764%40linux-edt6#bf4b983d5744bca153c288904c038020)
