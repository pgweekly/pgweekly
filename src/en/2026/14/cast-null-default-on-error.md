# CAST with NULL or DEFAULT ON ERROR: A WIP on Error-Safe Casts—and Why It Stalled on Standards

## Introduction

A [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CADkLM%3Dfv1JfY4Ufa-jcwwNbjQixNViskQ8jZu3Tz_p656i_4hQ%40mail.gmail.com) opened in December 2022 with **Corey Huinker**’s work-in-progress implementation of **error-tolerant `CAST` syntax**, aligned with a proposal by **Vik Fearing** and layered on the **error-safe user functions** infrastructure then under development. The idea is familiar from products like SQL Server’s `TRY_CAST`: turn a failing cast into a **NULL** or a **caller-supplied default** instead of aborting the statement. What followed was a split between **enthusiasm for the capability** and **caution about baking syntax into `CAST` before the SQL standard committee settles the shape of the feature**.

## Why This Matters

Data ingestion, ETL, and loosely typed text columns routinely hit casts that fail for a subset of rows. Today, PostgreSQL users often wrap casts in PL/pgSQL exception blocks, use `regexp_replace` + `CASE`, or preprocess outside the database. A **first-class, SQL-visible** way to supply a fallback—especially if it can share machinery with **error-safe functions**—reduces boilerplate and keeps logic in the query where optimizers and type systems can see it. The thread is also a case study in how **standardization risk** gates **grammar** changes even when **executor plumbing** is largely ready.

## Technical Analysis

### Proposed surface syntax (Huinker’s WIP)

In the patch series, the intended behavior was:

| Form | Behavior |
|------|----------|
| `CAST(expr AS typename)` | Unchanged (fail on error). |
| `CAST(expr AS typename ERROR ON ERROR)` | Same as unadorned `CAST` (explicit “strict” spelling). |
| `CAST(expr AS typename NULL ON ERROR)` | Cast via **error-safe** primitives; **NULL** if the cast fails. |
| `CAST(expr AS typename DEFAULT expr2 ON ERROR)` | Error-safe cast of `expr`; if it fails, evaluate and return **`expr2`**. |

An optional **`FORMAT fmt`** clause (for date/time-style conversions) was discussed but **not implemented** in the initial WIP.

### SQL examples (from the thread; not in a released PostgreSQL as-is)

The author reported these styles working for **scalar** cases (constant folding and runtime). Treat these as **illustrative of the discussion**—they require a build containing the experimental patches, not stock PostgreSQL.

**Constants and simple arrays:**

```sql
VALUES (CAST('error' AS integer));
VALUES (CAST('error' AS integer ERROR ON ERROR));
VALUES (CAST('error' AS integer NULL ON ERROR));
VALUES (CAST('error' AS integer DEFAULT 42 ON ERROR));

SELECT CAST('{123,abc,456}' AS integer[] DEFAULT '{-789}' ON ERROR) AS array_test1;
```

**Per-row text to integer with a default:**

```sql
CREATE TEMPORARY TABLE t(t text);
INSERT INTO t VALUES ('a'), ('1'), ('b'), (2);
SELECT CAST(t.t AS integer DEFAULT -1 ON ERROR) AS foo FROM t;
-- Expected: -1, 1, -1, 2 (per the thread’s example)
```

### Implementation sketch

- **`OidInputFunctionCallSafe`** and **`stringTypeDatumSafe()`** (parallel to existing “safe” input paths) to perform casts without throwing in the success path analysis.
- To avoid duplicating “did the inner cast fail?” logic across **`FuncExpr`**, **`CoerceViaIO`**, and **`ArrayCoerce`**, the WIP extended **`CoalesceExpr`** with an **error-test mode** (alongside the usual null-test mode), wiring the error-safe cast of the main expression and a non-error-safe evaluation of the default expression.
- **Not done** in the first post: **array-to-array** casts (deemed invasive around **`ArrayCoerce`**), **domains**, and **JIT** (`llvmjit_expr.c` issues showed up in CI for related work).

### Quirks and open tests

- One **regression query** still failed: casting a text array to an integer array with a **default array** when the per-element cast fails in the middle (`array_test2` in the thread).
- **Column names**: an unaliased `CAST ... DEFAULT ... ON ERROR` could surface as **`coalesce`** internally—confusing for users.

## Community Insights

- **Tom Lane** supported the **goal** but warned that **extending `CAST`** risks conflicting with a future **ISO SQL** keyword layout. He preferred a **function or non-`CAST` syntax** unless the committee’s direction is known and aligned.
- **Andrew Dunstan** concurred (+1).
- **Corey Huinker** pointed to **Vik Fearing**’s earlier spec aimed at the SQL committee, and noted the **grammar could be split into its own patch**; the **underlying machinery** (detecting cast failure and substituting a default) would remain similar.
- **Vik Fearing** clarified he had **not yet posted** the paper to the committee but planned to before an early-February working-group meeting, and **asked not to add the syntax in PostgreSQL until the committee had spoken**, in case wording changes.
- **Tom Lane** agreed: **not for PostgreSQL 16**; wait for something solid from the committee.
- **Gregory Stark (Commitfest Manager)** marked the entry **Rejected** in Commitfest given that timeline.
- **Isaac Morland** asked whether **`NULL ON ERROR`** differs from **`DEFAULT NULL ON ERROR`**. **Huinker** answered: **no practical difference** in his implementation—both become a constant null default; **`NULL ON ERROR`** is shorthand before richer **`DEFAULT expr`** expressions.

## Technical Details

Later discussion and tooling (**CFBot**) surfaced **JIT compilation errors** in `llvmjit_expr.c` when touching error-safe expression paths—typical of how **LLVM IR generation** must track null and error flags consistently with the interpreter.

In **July 2025**, **jian he** summarized **preparatory commits** already on `master`: SQL/JSON work reserving **`ERROR`**, **`CoerceViaIO` / `CoerceToDomain`** error-safe evaluation, and **`ExprState`** gaining an **`ErrorSaveContext`** so expression init can respect soft-error evaluation. He shared a **proof-of-concept** extending the older thread patches, including **domain over composite** examples, and noted **Oracle** documents **`CAST(... DEFAULT ... ON CONVERSION ERROR)`**, while observing that **SQL:2023** material surveyed by Peter Eisentraut does **not** obviously include this exact `CAST` form—so standardization and implementation may continue to diverge across vendors for a while.

## Current Status

As of the **2023** consensus on the list, **syntax was deferred** pending **SQL committee** output; **Commitfest** reflected that as **Rejected**. **Corey Huinker** indicated he would **resume when the committee had something concrete**. The **2025** follow-up shows **core infrastructure** moving forward independently; **user-facing `CAST ... ON ERROR` syntax** in PostgreSQL remains **a future decision**, not a shipped feature in the thread’s terms.

## Conclusion

The thread captures a **clean separation of concerns**: **error-safe evaluation** is broadly useful (casts, domains, JSON, executor details), while **`CAST` grammar** is politically and standards-sensitive. Readers who want **TRY_CAST-style behavior** today still rely on **application-side** or **PL/pgSQL** patterns; this discussion explains **why** a seemingly small SQL extension waited on **ISO SQL** and **project governance**, not only on code.

## References

- [Thread (flat)](https://www.postgresql.org/message-id/flat/CADkLM%3Dfv1JfY4Ufa-jcwwNbjQixNViskQ8jZu3Tz_p656i_4hQ%40mail.gmail.com)
- [Vik Fearing’s earlier spec message](https://www.postgresql.org/message-id/f8600a3b-f697-2577-8fea-f40d3e18bea8@postgresfriends.org) (linked from the thread)
- [SQL:2023 overview (Peter Eisentraut)](https://peter.eisentraut.org/blog/2023/04/04/sql-2023-is-finished-here-is-whats-new) — cited in-thread regarding what SQL:2023 includes
- [Oracle CAST documentation](https://docs.oracle.com/en/database/oracle/oracle-database/23/sqlrf/CAST.html) — vendor comparison in jian he’s follow-up
