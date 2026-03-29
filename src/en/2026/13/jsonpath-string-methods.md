# JSONPath String Methods: Cleaning JSON Inside the Path—and a Long Debate About Immutability

## Introduction

Working with messy JSON often means trimming, case-folding, and splitting strings before comparisons. Today you can do that in SQL around `jsonb_path_query()` and friends, but not always *inside* the JSONPath expression itself. Florents Tselai posted a patch series to `pgsql-hackers` that adds familiar string helpers—`lower()`, `upper()`, `initcap()`, `ltrim()` / `rtrim()` / `btrim()`, `replace()`, and `split_part()`—as JSONPath methods, delegating to PostgreSQL’s built-in string functions.

The thread quickly grew into a broader discussion: how these operations interact with **volatility and immutability** (locale-dependent behavior), whether PostgreSQL’s JSONPath should track the **SQL standard** or Internet RFCs, and what to do about the naming wart around the existing `*_tz` JSONPath entry points. The work is registered on [Commitfest 5270](https://commitfest.postgresql.org/patch/5270/); the [mailing list thread](https://www.postgresql.org/message-id/flat/CA%2Bv5N40sJF39m0v7h%3DQN86zGp0CUf9F1WKasnZy9nNVj_VhCZQ%40mail.gmail.com) spans 2024 through 2025 with many patch revisions.

## Why This Matters

JSONPath is the embedded language used by `jsonb_path_*` routines. Adding string primitives in-path reduces nested SQL, keeps intent local to the path, and matches how people already think about Unix-style text pipelines—only applied to JSON scalars. For data cleaning (whitespace, casing, delimiter splitting), the feature is ergonomically strong.

The catch is that **case mapping and many string operations depend on locale**. PostgreSQL’s planner relies on correct volatility labels: marking something `immutable` when it can change with locale or OS updates breaks assumptions. That tension is what drives most of the thread after the initial patch post.

## Technical Analysis

### What the patch adds

The author’s first revision introduces methods that forward to `pg_proc`-registered implementations, extends `JsonPathParseItem` with `method_args` (`arg0`, `arg1`) for methods that need more than the usual left/right operand pattern, and adds `jspGetArgX()` accessors. A `README.jsonpath` file documents how to add new methods for future contributors. Example shapes from the proposal:

```sql
SELECT jsonb_path_query('" hElLo WorlD "', '$.btrim().lower().upper().lower().replace("hello","bye") starts with "bye"');
SELECT jsonb_path_query('"abc~(at)~def~@~ghi"', '$.split_part("~(at)~", 2)');
```

Open items in the first post included: possible **name collisions** if the SQL/JSON standard later defines methods with the same names (prefixes such as `pg_` were mentioned in an earlier related thread), **default collation** behavior (consistent with existing JSONPath code), missing user-facing documentation, and a half-formed idea of `CREATE JSONPATH FUNCTION`-style extensibility.

### Patch evolution (high level)

The fetchable series runs from **v1** through **v18**, with **v6** onward split into two patches per version: one renaming JSONPath method-argument tokens, the other carrying the string-method implementation. Intermediate revisions iterate on tests, grammar, and rebases against `jsonpath_scan.l` and related files. Later revisions add substantial **SGML documentation** under `doc/src/sgml/func/func-json.sgml` and **regression tests** for `jsonpath`, `jsonb_jsonpath`, and related SQL.

## Community Insights

- **Tom Lane** [raised immutability early](https://www.postgresql.org/message-id/145894.1727298237%40sss.pgh.pa.us): string methods tied to locale mean JSONPath operations are not universally immutable, pointing to commit `cb599b9dd` and contrasting with the `_tz` split used for time-zone-sensitive datetime behavior in JSONPath—a pattern he argued does not scale cleanly to many sources of mutability.

- **Alexander Korotkov** floated **“flexible mutability”**: a companion could analyze whether the JSONPath argument is constant and whether all methods used are “safe,” so `jsonb_path_query()` might be labeled `immutable` in restricted cases and `stable` otherwise. He also asked whether the new names appear in the **SQL standard** (or a draft).

- **Florents Tselai** noted prior discussion from 2019, sketched a **heuristic** (if all elements are safe, treat the path as immutable), and cited [RFC 9535](https://www.rfc-editor.org/rfc/rfc9535.html#name-function-extensions) “function extensions” as covering vendor additions—while acknowledging mutability as an implementation concern.

- **David E. Wheeler** clarified that **PostgreSQL’s JSONPath follows the SQL/JSON track in the SQL standard**, not RFC 9535; extension facilities in the public RFC are not automatically the same as SQL’s rules. He remained interested in **extensibility hooks** (lexer, parser, executor) and asked what hooks would look like; Florents outlined steps: new `JsonPathItemType`, lexer/parser changes in `jsonpath_scan.l` / `jsonpath_gram.y`, and a dispatch hook from `executeItemOptUnwrapTarget`.

- **Robert Haas** reframed the issue as partly a **general policy gap** (immutability when depending on OS time or locale is often `stable`), compared to the existing **`json_path_exists` vs `json_path_exists_tz`** split, and suggested extending the “tz” family to accept locale-dependent operations and **erroring in the non-suffixed functions**—while admitting the `_tz` name becomes misleading.

- **David Wheeler** read Tom’s “doesn’t scale” comment as “we will not add `json_path_exists_foo` for every mutable concern,” and wondered about **renaming** or generalizing. After discussion at a **PostgreSQL community event**, Florents summarized a **pragmatic path**: add the new behavior under the **`jsonb_path_*_tz` family**, reject it in non-`_tz` variants, and document clearly—keeping naming aligned with existing `_tz` functions despite the semantic stretch.

- **Naming clutter**: David suggested introducing `_stable`-style names; **Robert** outlined a heavy deprecation/GUC path and predicted complaints regardless. **Tom Lane** saw little value in elaborate migration machinery, noted **wrapper functions** preserve old names for stubborn apps, and asked what is wrong with **new names alongside old ones without removal**. **Robert** accepted extra clutter if consensus prefers it. **Florents** noted a third parallel set of five `jsonb_path_*` functions would add API surface. **David** questioned indexability of `_tz` usage in the wild; the thread touches on generated columns and future virtual generated columns as where timestamps from JSON often land.

## Technical Details

### Immutability and locale

PostgreSQL distinguishes `immutable` (same inputs → same outputs for the life of a query plan’s assumptions) from `stable` (can change within a statement, e.g., with session or environment). Locale definitions can change when the OS or ICU data is updated, so functions that depend on them are typically not `immutable`. JSONPath integration must respect the same rules as core SQL functions if paths are embedded in expression evaluation and optimization.

### Standards positioning

The thread distinguishes:

- **RFC 9535** (JSONPath, IETF): public, documents extension points.
- **SQL/JSON and SQL-standard JSONPath** (what PostgreSQL targets): not fully public; extension and naming rules may differ.

Vendor-specific method names may still need care to avoid future standard collisions—prefixing or naming conventions remain an open design concern from the first post.

### Implementation surface

Beyond volatility, adding methods touches the JSONPath lexer, grammar, executor, and tests. Splitting patches (rename tokens vs feature body) keeps review manageable once the series grows.

## Current Status

The patch series remained under active development through multiple rebases (through **v18** in the downloaded artifacts). It is listed on [Commitfest 5270](https://commitfest.postgresql.org/patch/5270/), with a [GitHub branch/PR](https://github.com/Florents-Tselai/postgres/pull/18) for readers who prefer that view. The mailing list discussion in May 2025 converged on practical next steps (surface under `*_tz`, strict errors elsewhere, documentation) and naming trade-offs, without a final commit message in this thread snapshot.

## Conclusion

JSONPath string methods address a real ergonomics gap: in-path normalization and splitting of JSON string data. The community response centers less on the feature’s utility than on **correct volatility**, **alignment with SQL/JSON**, and **API sprawl** around `jsonb_path_*` and the `_tz` suffix. The thread is a useful snapshot of how PostgreSQL balances standards compliance, planner correctness, and long-term maintainability when extending a DSL that lives inside core.
