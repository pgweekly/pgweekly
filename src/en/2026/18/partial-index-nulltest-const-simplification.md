# Fixing Partial Index Matching Regressions in PostgreSQL 17/18: A Deep Dive into NullTest Const-Simplification

## Introduction

This [pgsql-bugs thread](https://www.postgresql.org/message-id/flat/19007-4cc6e252ed8aa54a%40postgresql.org) started from a real-world regression report: a partial index that was used in PostgreSQL 16 stopped being selected in PostgreSQL 17. The case looked odd at first (`WHERE flag IS NOT NULL` on a column already declared `NOT NULL`), but it exposed a planner consistency issue around expression simplification.

The discussion evolved from bug triage into a planner internals fix focused on how `eval_const_expressions()` handles `NullTest` clauses when expressions are loaded from catalogs and then remapped.

## Technical Analysis

### Root cause

The key issue was not partial indexes per se, but when and how expression trees get const-simplified:

- Expressions from relcache/catalog paths (index expressions, index predicates, stats expressions, constraints) may be simplified before `Var` nodes are rewritten to the query's target `varno`.
- In some code paths, `eval_const_expressions()` was called with `root = NULL`, which disables certain `NullTest` reductions.
- As a result, expressions that should become equivalent did not canonicalize to the same form, so implication/matching logic could miss usable partial indexes.

The thread explicitly calls out that planner behavior must not diverge by context for the same logical expression.

### Patch evolution (exploratory patch -> v1 -> v2 -> v3 -> v4)

- **Exploratory patch** (`further_eval_const_expressions_processing_on_partial_indexes.patch`): a minimal idea to re-run simplification on index predicates.
- **v1**: broad single patch touching constraints, stats, index expressions, and index predicates together.
- **v2**: split into two patches:
  - `0001`: constraints and statistics expressions.
  - `0002`: index expressions and predicates, including `ON CONFLICT` arbiter matching paths.
- **v3**: rebased/consolidated the index-focused part after `0001` had been pushed independently.
- **v4**: added stronger rationale on planning-time overhead and expanded regression coverage, including index-expression and index-predicate cases.

The final merged result was pushed to `master`, while backpatch options for v17/v18 remained unresolved in the thread.

### SQL examples

The following SQL reproduces the class of planner behavior discussed and mirrors the regression tests added in the patch series. These examples require a PostgreSQL build containing the fix (merged to `master`), not stock v17/v18 releases.

```sql
CREATE TABLE pred_tab (a int, b int NOT NULL, c int NOT NULL);
INSERT INTO pred_tab SELECT i, i, i FROM generate_series(1, 1000) i;

-- Predicate index path
CREATE INDEX pred_tab_pred_idx ON pred_tab (a)
WHERE b IS NOT NULL AND c IS NOT NULL;

ANALYZE pred_tab;

EXPLAIN (COSTS OFF)
SELECT * FROM pred_tab
WHERE a < 3 AND b IS NOT NULL AND c IS NOT NULL;
```

```sql
-- Expression index path
CREATE INDEX pred_tab_exprs_idx
ON pred_tab ((a < 5 AND b IS NOT NULL AND c IS NOT NULL));

EXPLAIN (COSTS OFF)
SELECT * FROM pred_tab
WHERE (a < 5 AND b IS NOT NULL AND c IS NOT NULL) IS TRUE;
```

Expected shape after the fix: planner can reduce `NullTest`-related forms consistently and choose the corresponding index scan paths in these patterns.

## Community Insights

The review thread shows a few recurring PostgreSQL design themes:

- **Consistency over ad hoc behavior:** simplification should behave the same across planner contexts.
- **Correctness first, then cost:** reviewers questioned the extra `eval_const_expressions()` pass; the patch justified why overhead is small and localized.
- **Incremental landing:** splitting v2 into two patches made review and commit flow smoother; the non-index half landed first.
- **Release-branch caution:** participants noted an awkward version window (not in older versions, fixed in master, unclear for v17/v18 backpatching).

## Technical Details

The core implementation strategy was:

- Ensure `Var` remapping (`ChangeVarNodes`) is done before the decisive simplification pass where needed.
- Re-run `eval_const_expressions(root, ...)` for index expressions and predicates after varno adjustments.
- Apply the same principle in `infer_arbiter_indexes()` so `ON CONFLICT` index inference sees equivalently simplified trees.
- Expand regression tests to validate both predicate indexes and expression indexes, plus surrounding planner behavior expectations.

This makes catalog-loaded expressions converge toward the same canonical form as query quals, restoring reliable predicate implication and index matching.

## Current Status

The thread progressed through multiple patch revisions (`v1` to `v4`) and was ultimately committed to PostgreSQL `master`. The open question at thread close was policy/feasibility for backpatching to v17 and v18.

## Conclusion

This bug report became a useful case study in planner expression hygiene: small differences in simplification context can have major plan-shape effects. By standardizing when const-simplification runs relative to varno fixing (and by providing a valid planner root), PostgreSQL restores partial-index usability for affected patterns and reduces surprising version-to-version behavior changes.

