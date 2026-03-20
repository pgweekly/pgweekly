# Reduce Planning Time for Large NOT IN Lists Containing NULL

## Introduction

When a query uses `x NOT IN (NULL, ...)` or `x <> ALL (NULL, ...)`, the result is always empty—no rows can match. In SQL, the presence of a single NULL in a NOT IN list makes the entire predicate evaluate to NULL or false for every row, so the selectivity is 0.0. Yet the PostgreSQL planner was still iterating over every element in the list and invoking the operator's selectivity estimator for each one, wasting planning time on large lists.

Ilia Evdokimov (Tantor Labs) proposed a simple optimization: detect when the array contains NULL and short-circuit the selectivity loop for the `<> ALL` / NOT IN case, returning 0.0 immediately. The patch went through several rounds of review, uncovered a subtle regression involving arrays that once contained NULL but no longer do, and was committed by David Rowley in March 2026.

## Why This Matters

Queries with large `NOT IN` or `<> ALL` lists are common in reporting and ETL workloads. When such a list includes NULL—whether intentionally or from a subquery—the planner was doing unnecessary work:

- For constant arrays: deconstructing the array, iterating over each element, and calling the operator's selectivity function.
- For non-constant expressions: iterating over list elements to check for NULL.

In Ilia's benchmarks, planning time for `WHERE x NOT IN (NULL, ...)` dropped from **5–200 ms** to **~1–2 ms** depending on list size, when the column had detailed statistics. The optimization preserves semantics and avoids regressions in the common case.

## Technical Analysis

### The Semantics

For `x NOT IN (a, b, c, ...)` (or `x <> ALL (array)`):

- If any element is NULL, the predicate yields NULL for every row (in a WHERE clause, that means the row is filtered out).
- The planner models this as selectivity = 0.0: no rows match.

The current code in `scalararraysel()` in `src/backend/utils/adt/selfuncs.c` handles both `= ANY` (IN) and `<> ALL` (NOT IN) via a `useOr` flag. For `<> ALL`, the selectivity is computed by iterating over array elements and combining per-element estimates. When a NULL is present, the final result is always 0.0, so the loop is redundant.

### Patch Evolution

**v1** added an early check after `deconstruct_array()` using `memchr()` on `elem_nulls` to detect any NULL. David Geier raised a concern: `memchr()` adds overhead on every call. He suggested a flag on `ArrayType` instead.

**v2** switched to short-circuiting inside the per-element loop: when a `Const` element is NULL, return 0.0 immediately. This avoids a separate pass but still requires entering the loop.

**v3** moved the check earlier, right after `DatumGetArrayTypeP()`, using `ARR_HASNULL()` to detect NULL before deconstructing the array. This avoids both the deconstruction and the per-element loop when NULL is present. Ilia reported planning time dropping from 5–200 ms to ~1–2 ms.

**v4** addressed a regression found by Zsolt Parragi. The macro `ARR_HASNULL()` only checks for the *existence* of a NULL bitmap—not whether any element is actually NULL. An array that originally had NULL but had all NULLs replaced (e.g., via `array_set_element()`) can still have a NULL bitmap. Using `ARR_HASNULL()` alone caused incorrect selectivity 0.0 for such arrays.

The fix: use `array_contains_nulls()`, which iterates the NULL bitmap and returns true only when an element is actually NULL. v4 also added a regression test that constructs an array from `ARRAY[1, NULL, 3]` with the NULL replaced by 99, ensuring the planner estimates 997 rows (not 0) for `x <> ALL(replace_elem(ARRAY[1,NULL,3], 2, 99))`.

**v5–v9** incorporated feedback from David Rowley: move the test to a new `selectivity_est.sql` file (later renamed to `planner_est.sql`), use `tenk1` from `test_setup.sql` instead of a custom table, add a comment about assuming the operator is strict (like `var_eq_const()`), and simplify tests to assert the invariant (selectivity 0.0 when NULL is present) rather than exact row estimates.

## Community Insights

- **David Geier** questioned the cost of `memchr()` and suggested an `ArrayType` flag; Ilia found that `ARR_HASNULL()` / `array_contains_nulls()` already existed.
- **Zsolt Parragi** found the regression with arrays that had NULLs replaced, and proposed the `replace_elem` test case.
- **David Geier** clarified that `ARR_HASNULL()` checks the bitmap's existence, not actual NULL elements; `array_contains_nulls()` is the correct check.
- **David Rowley** suggested moving tests to a dedicated planner-estimation file, using existing test tables, and documenting the strict-operator assumption. He also pushed the refactoring patch (planner_est.sql) and the main optimization.

## Technical Details

### Implementation

The optimization adds two short-circuit paths in `scalararraysel()`:

1. **Constant array case**: After `DatumGetArrayTypeP()`, if `!useOr` (i.e., `<> ALL` / NOT IN) and `array_contains_nulls(arrayval)`, return 0.0 immediately. This runs before `deconstruct_array()`.

2. **Non-constant list case**: In the per-element loop, if `!useOr` and the element is a `Const` with `constisnull`, return 0.0. This handles `x NOT IN (1, 2, NULL, ...)` when the list comes from a non-constant expression.

The code assumes the operator is strict (like `var_eq_const()`): when the constant is NULL, the operator returns zero selectivity. This is consistent with existing planner behavior.

### Edge Cases

- **Arrays with NULL bitmap but no actual NULLs**: Handled by `array_contains_nulls()` instead of `ARR_HASNULL()`.
- **Non-strict operators**: The comment documents that the short-circuit follows the same assumption as `var_eq_const()`.

### Related Work

David Geier noted that the speedup will be less pronounced once the [hash-based NOT IN code](https://www.postgresql.org/message-id/flat/7db341e0-fbc6-4ec5-922c-11fdafe7be12%40tantorlabs.com) is merged, but the optimization still saves cycles during selectivity estimation.

## Current Status

The patch was committed by David Rowley on March 19, 2026. The refactoring that moved planner row-estimation tests to `planner_est.sql` was committed first; the main optimization followed. It will appear in a future PostgreSQL release.

## Conclusion

A small change—detecting NULL in NOT IN / `<> ALL` lists and returning selectivity 0.0 early—avoids unnecessary per-element work during planning. The fix required careful handling of the NULL bitmap vs. actual NULL elements, and benefited from thorough review and regression tests. The optimization is now part of PostgreSQL and will help workloads that use large NOT IN lists containing NULL.
