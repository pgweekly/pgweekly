# Converting NOT IN Sublinks to Anti-Joins When Safe

## Introduction

In February–March 2026, Richard Guo proposed and iterated on a planner patch to convert

```sql
... WHERE expr NOT IN (SELECT subexpr FROM ...)
```

into a **join-based anti-join** when it is provably safe. The topic has a long history on
`pgsql-hackers`: `NOT IN` has tricky semantics around `NULL`, so naïvely rewriting it to an
anti-join risks changing query results.

The new patch takes advantage of infrastructure that did not exist in older attempts: an
outer-join-aware `Var` representation, a global not-null-attnums hash table, and smarter
non-nullability reasoning. Over six patch versions, reviewers including wenhui qiu, Zhang
Mingli, Japin Li, David Geier and others helped refine both the safety criteria and the
implementation details. The final v6 patch was committed by Richard in March 2026.

This post explains why `NOT IN` is hard, how the planner proves safety, what changed between
v1 and v6, and what this means for everyday queries.

## Why NOT IN Is Hard

At first glance, `NOT IN` looks like a natural anti-join:

```sql
SELECT *
FROM users
WHERE id NOT IN (SELECT user_id FROM banned_users);
```

Intuitively this means “users that are not banned,” and a hash anti-join against
`banned_users` is exactly the plan you would like to see. The problem is **SQL’s `NULL`
semantics**.

Consider a scalar comparison `(A = B)` used inside `NOT IN`:

- If `(A = B)` is `TRUE`, the element is found and the `NOT IN` condition fails.
- If `(A = B)` is `FALSE`, the particular element is not a match; the overall result depends
  on the other elements.
- If `(A = B)` is `NULL`, `NOT (NULL)` is also `NULL` (treated as false in `WHERE`), so the
  row is **discarded**.

In contrast, for an anti-join, if the join condition evaluates to `NULL` for a row pair, the
executor essentially treats that as “no match” and may keep the outer row. As Richard points
out in the thread, whenever the comparison operator can yield `NULL` on any pair of inputs
that the executor considers a valid equality comparison, `NOT IN` and a simple anti-join
disagree.

Historically, this semantic mismatch made the planner avoid converting `NOT IN` sublinks to
anti-joins. Users who wanted a join-based plan needed to rewrite their queries using
`NOT EXISTS` or other patterns.

## Planner Infrastructure: Proving Non-Nullability

Recent releases have accumulated infrastructure that can prove non-nullability of expressions
more reliably and cheaply:

- **Outer-join-aware `Var`s** record which relations can be nulled out by outer joins.
- A **not-null-attnums hash table** tracks columns declared `NOT NULL` (or implied by
  primary keys and certain constraints).
- `expr_is_nonnullable()` reasons about more complex expressions, not just simple `Var` or
  `Const` nodes.
- `find_nonnullable_vars()` can derive non-nullability from qual clauses (e.g.
  `col IS NOT NULL` or strict operators in join conditions).

In the new patch, Richard leverages this infrastructure to answer two questions:

1. **Can either side of the comparison become `NULL`?**
   - Use relation-level information (NOT NULL, PK) and outer-join-aware `Var`s to reject
     Vars that come from the nullable side of an outer join.
   - Incorporate `find_nonnullable_vars()` and safe qual clauses to detect values forced
     non-null by the query’s WHERE/ON conditions.
2. **Can the comparison operator itself return `NULL` on non-null arguments?**
   - Use catalog information to restrict to operators that are members of a **B-tree or
     hash opfamily**, because those must behave like a normal total order or equality
     operator. If such an operator returned `NULL` for non-null inputs, indexes built
     on it would be broken.

Only when both operands are provably non-nullable and the operator is considered safe does
the planner consider rewriting the `NOT IN` sublink as an anti-join.

## From v1 to v6: Tightening the Safety Checks

The patch did not land in its first incarnation. The thread documents a clear evolution:

- **v1**:
  - Implemented the basic conversion logic.
  - Focused on proving both sides of the comparison are non-nullable using existing planner
    helpers.
  - Did not yet check that the operator itself could not return `NULL` for non-null inputs.

- **Discussion on operator safety**:
  - Richard realized that requiring non-nullable operands is not enough; an operator could
    still return `NULL` on non-null inputs.
  - He asked whether it is possible to detect operators that never return `NULL` on
    non-null inputs and suggested using membership in **btree** opclasses as a proxy.
  - David Geier noted that a lot of executor code assumes comparison operators do not return
    `NULL`—`FunctionCall2()` and friends will `ERROR` if they do—so constraining to builtin
    B-tree / hash operators is a reasonable and safe starting point.

- **v2–v4**:
  - Added the check that the operator must be a member of a B-tree or hash operator family.
  - Clarified comments and added more regression tests covering cases where the inner side
    of the subquery comes from the nullable side of an outer join but is forced non-null by
    a `WHERE` clause.
  - Incorporated small review fixes around test comments and edge cases.

- **v5–v6**:
  - Refined internal helpers, including the function that checks non-nullability of the
    test expression (`sublink_testexpr_is_not_nullable`).
  - Improved handling of row comparison expressions (`RowCompareExpr`) so that multi-column
    `NOT IN` patterns can also benefit.
  - Refactored list iteration using `foreach_ptr` / `foreach_node` for better type safety.
  - Fixed a subtle order issue in `query_outputs_are_not_nullable()` around flattening
    grouping Vars versus join alias Vars.
  - Added and polished regression test coverage, then performed a final self-review.

By the time v6 was committed, the patch combined conservative semantics, good test coverage,
and multiple rounds of review feedback.

## What the Patch Actually Does

At a high level, when the planner sees a `NOT IN` sublink of the canonical ANY/ALL form, it:

1. **Recognizes the pattern** in `SubLink` and the associated `testexpr`.
2. **Collects outer expressions** (the left-hand side(s) of the comparison).
3. **Checks operator safety**:
   - Every operator involved in the comparison must be a member of a B-tree or hash
     opfamily.
4. **Proves operand non-nullability**:
   - Use relation-level NOT NULL information, outer-join-aware `Var` metadata, and
     non-nullable-vars analysis from qual clauses to show that neither the outer expression
     nor the subquery output can be `NULL`.
5. **Converts to an anti-join** if and only if all checks pass.

The conversion allows the planner to:

- Pull the subquery up into the global join tree instead of treating it as an opaque
  subplan.
- Reorder the join relative to other joins.
- Choose the best join algorithm (hash anti-join, merge anti-join, etc.) based on cost.

From the user’s perspective, the benefit is that typical exclusion patterns written with
`NOT IN` now receive plans comparable to well-written `NOT EXISTS` and explicit anti-join
forms, without requiring manual rewrites.

## Example: Typical Exclusion Pattern

The patch primarily targets canonical patterns like:

```sql
SELECT *
FROM users
WHERE id NOT IN (SELECT user_id FROM banned_users);
```

and:

```sql
SELECT *
FROM users
WHERE id NOT IN (
  SELECT user_id
  FROM banned_users
  WHERE user_id IS NOT NULL
);
```

In well-modelled schemas, `users.id` and `banned_users.user_id` are usually defined as
`NOT NULL` and use standard equality operators. Under these conditions, the planner can
prove that:

- Neither side of the comparison can be `NULL`.
- The equality operator behaves like a normal B-tree/hash equality operator.

The `NOT IN` sublink is then rewritten to an anti-join, and the plan can look like:

- A **Hash Anti Join** between `users` and `banned_users`, or
- A **Merge Anti Join** if there are supporting indexes and the planner prefers a merge
  strategy.

The thread also includes a large performance test contributed by wenhui qiu, showing how
the new optimization produces efficient anti-join plans on big synthetic data sets once the
relevant columns are marked `NOT NULL`.

## Community Feedback and Scope

The discussion also explored how far this optimization should go:

- David Geier described more aggressive rewrites that add `IS NOT NULL` predicates and extra
  `NOT EXISTS` subqueries to cover cases where either side can be `NULL`. Richard pointed out
  correctness problems with some of those transformations (especially when the subquery is
  empty) and concluded that they were **out of scope** for this patch.
- There was brief speculation about a potential **null-aware anti-join** execution node,
  similar to what Oracle does, to handle more `NOT IN` cases without purely syntactic query
  rewrites. That is left for future work.
- Reviewers repeatedly emphasized a conservative approach: it is better to miss some
  theoretical optimization opportunities than to risk changing query answers.

The final patch deliberately focuses on the **basic, high-ROI form**: both sides provably
non-nullable and standard B-tree/hash operators. This matches the majority of real-world
`NOT IN` exclusion queries while keeping code complexity and risk under control.

## Current Status

As of mid-March 2026:

- The v6 patch “Convert NOT IN sublinks to anti-joins when safe” has been **committed**.
- The optimization is enabled automatically when the planner can prove its safety.

In practice, this means that if you write typical `NOT IN` exclusion queries on NOT NULL
keys using builtin comparison operators, PostgreSQL’s planner can now generate anti-join
plans for you. You get better plans without changing application SQL, as long as your
schemas and constraints correctly reflect non-nullability.

## Takeaways for Users

- **Use proper NOT NULL constraints and primary keys.** The more accurately schemas model
  non-nullability, the more often the planner can safely apply this optimization.
- **`NOT IN` on nullable columns remains tricky.** PostgreSQL will continue to be
  conservative in these cases; consider `NOT EXISTS` patterns if you need predictable
  behavior with `NULL`s.
- **You do not need to rewrite canonical `NOT IN` exclusions** just to get an anti-join;
  the planner can now recognize and optimize them when it is safe to do so.

## References

- [Discussion thread: Convert NOT IN sublinks to anti-joins when safe](https://www.postgresql.org/message-id/CAMbWs495eF=-fSa5CwJS6B-BaEi3ARp0UNb4Lt3EkgUGZJwkAQ@mail.gmail.com)
