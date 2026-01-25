# PostgreSQL Planner Optimization: Automatic COUNT(*) Conversion

## Introduction

In October 2025, PostgreSQL committer David Rowley proposed a significant query planner optimization that automatically converts `COUNT(1)` and `COUNT(not_null_col)` expressions to `COUNT(*)`. This optimization addresses a common performance anti-pattern where developers write `COUNT(1)` thinking it's equivalent to `COUNT(*)`, when in fact `COUNT(*)` is more efficient. The patch was committed in November 2025 and introduces new infrastructure for aggregate function simplification.

## Why This Matters

The performance difference between `COUNT(*)` and `COUNT(column)` can be substantial, especially for large tables. When counting a specific column, PostgreSQL must:

1. **Deform the tuple** to extract the column value
2. **Check for NULL values** (even for NOT NULL columns, the check still occurs)
3. **Process the column data** through the aggregate function

In contrast, `COUNT(*)` can count rows without accessing individual column values, resulting in significantly better performance. David Rowley's benchmarks showed approximately **37% performance improvement** when using `COUNT(*)` instead of `COUNT(not_null_col)` on a table with 1 million rows.

## Technical Analysis

### The Infrastructure: SupportRequestSimplifyAggref

The patch introduces a new infrastructure called `SupportRequestSimplifyAggref`, which is similar to the existing `SupportRequestSimplify` used for regular function expressions (`FuncExpr`). Since aggregates use `Aggref` nodes, a separate mechanism was needed.

The key components include:

1. **New support node type**: `SupportRequestSimplifyAggref` in `supportnodes.h`
2. **Simplification function**: `simplify_aggref()` in `clauses.c` that calls the aggregate's support function during constant folding
3. **Enhanced nullability checking**: Extended `expr_is_nonnullable()` to handle `Const` nodes, not just `Var` nodes

### Implementation Details

The optimization is performed during the constant folding phase of query planning, specifically in `eval_const_expressions_mutator()`. When an `Aggref` node is encountered, the planner:

1. Checks if the aggregate function has a support function registered via `pg_proc.prosupport`
2. Calls the support function with a `SupportRequestSimplifyAggref` request
3. If the support function returns a simplified node, replaces the original `Aggref`

For the `COUNT` aggregate specifically, the support function (`int8_agg_support_simplify()`) checks:

- Whether the argument is non-nullable (using `expr_is_nonnullable()`)
- Whether there are no `ORDER BY` or `DISTINCT` clauses in the aggregate
- If both conditions are met, converts `COUNT(ANY)` to `COUNT(*)`

### Code Example

The core simplification logic in `int8.c`:

```c
static Node *
int8_agg_support_simplify(SupportRequestSimplifyAggref *req)
{
    Aggref    *aggref = req->aggref;

    /* Only handle COUNT */
    if (aggref->aggfnoid != INT8_AGG_COUNT_OID)
        return NULL;

    /* Must have exactly one argument */
    if (list_length(aggref->args) != 1)
        return NULL;

    /* No ORDER BY or DISTINCT */
    if (aggref->aggorder != NIL || aggref->aggdistinct != NIL)
        return NULL;

    /* Check if argument is non-nullable */
    if (!expr_is_nonnullable(req->root,
                             (Expr *) linitial(aggref->args),
                             true))
        return NULL;

    /* Convert to COUNT(*) */
    return make_count_star_aggref(aggref);
}
```

## Patch Evolution

The patch went through four iterations, each refining the implementation:

### Version 1 (Initial Proposal)
- Introduced the basic infrastructure
- Used `SysCache` to fetch `pg_proc` tuples

### Version 2 (Code Cleanup)
- Replaced `SysCache` lookup with `get_func_support()` function
- Cleaner and more efficient approach

### Version 3 (Removed Experimental Code)
- Removed `#ifdef NOT_USED` block that handled `COUNT(NULL)` optimization
- Cleaned up unused includes
- Improved comments

### Version 4 (Final Version)
- Rebased after commit `b140c8d7a`
- Fixed assumption that support function always returns an `Aggref`
- Allows support functions to return other node types (e.g., constants) for more aggressive optimizations
- This flexibility enables future optimizations like converting `COUNT(NULL)` to `'0'::bigint`

## Community Insights

### Reviewer Feedback

**Corey Huinker** provided positive feedback:
- +1 for the automatic query improvement
- Noted that we can't educate everyone that `COUNT(1)` is an anti-pattern, so making it not an anti-pattern is the right approach
- Confirmed the patch applies cleanly and tests pass

**Matheus Alcantara** also reviewed and tested:
- Confirmed ~30% performance improvement in benchmarks
- Validated that the code placement is consistent with existing `SupportRequestSimplify` infrastructure
- +1 for the idea

### Design Decisions

**Timing of Optimization**: The optimization happens during constant folding, which is early in the planning process. David considered whether it should happen later (after `add_base_clause_to_rel()`) to catch cases like:

```sql
SELECT count(nullable_col) FROM t WHERE nullable_col IS NOT NULL;
```

However, it must happen before `preprocess_aggref()`, which groups aggregates with the same transition function. The current placement is consistent with `SupportRequestSimplify` for regular functions.

**Support Function Return Type**: The infrastructure allows support functions to return nodes other than `Aggref`. This design decision enables future optimizations, such as:
- Converting `COUNT(NULL)` to `'0'::bigint`
- More aggressive constant folding for aggregates

## Performance Considerations

The optimization provides significant performance benefits:

1. **Reduced tuple deformation**: `COUNT(*)` doesn't need to extract column values from tuples
2. **Fewer NULL checks**: No need to check individual column values
3. **Better cache utilization**: Less data movement means better CPU cache usage

For tables with many columns, the performance gain can be even more substantial, as `COUNT(column)` might require deforming many columns to reach the target column.

## Edge Cases and Limitations

The optimization only applies when:

1. The column is provably non-nullable (NOT NULL constraint or constant)
2. There are no `ORDER BY` clauses in the aggregate
3. There are no `DISTINCT` clauses in the aggregate

Cases that are **not** optimized (yet):

- `COUNT(nullable_col)` where the column might be NULL (even if filtered by `WHERE nullable_col IS NOT NULL` in the same query)
- `COUNT(col ORDER BY col)` - the ORDER BY prevents optimization
- `COUNT(DISTINCT col)` - DISTINCT prevents optimization

The limitation with `WHERE` clauses is due to the timing of the optimization (during constant folding, before relation information is fully available).

## Current Status

The patch was **committed** by David Rowley on November 26, 2025. It's available in PostgreSQL master branch and will be included in PostgreSQL 18.

## Conclusion

This optimization represents a significant improvement to PostgreSQL's query planner, automatically fixing a common performance anti-pattern without requiring application changes. The new `SupportRequestSimplifyAggref` infrastructure also opens the door for future aggregate optimizations.

For developers and DBAs:
- **No action required**: The optimization happens automatically
- **Performance benefit**: Existing queries using `COUNT(1)` or `COUNT(not_null_col)` will automatically get faster
- **Best practice**: While the planner now optimizes these cases, `COUNT(*)` remains the clearest and most idiomatic way to count rows

This change demonstrates PostgreSQL's commitment to improving query performance automatically, reducing the burden on developers to know every optimization detail while still allowing experts to write optimal queries when needed.

## References

- [Discussion Thread](https://www.postgresql.org/message-id/CAApHDvqGcPTagXpKfH=CrmHBqALpziThJEDs_MrPqjKVeDF9wA@mail.gmail.com)
- Related: `SupportRequestSimplify` for regular function expressions
