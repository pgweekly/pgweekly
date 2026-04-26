# Post-special Page Storage for TDE: How a TDE Proposal Evolved into Broader Page-Space Refactoring

## Introduction

This [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CAOxo6XKWHHUr1agOZxEHuL-UW8Me3YndUsJ%3D09tcDiw%2BLd8YEw%40mail.gmail.com) started as a Transparent Data Encryption (TDE) infrastructure proposal, but quickly expanded into a broader discussion about page layout ownership, storage manager boundaries, and long-term maintainability. The core idea was to reserve optional space in page "special" regions so storage-level features (checksums, encryption metadata, and future page features) could be integrated consistently.

## Technical Analysis

### What the patch sets attempted

Across multiple revisions, the series introduced:

- `reserved_page_size` and related plumbing for "post-special" page space.
- A `PageUsableSpace` abstraction to replace hard-coded assumptions around `BLCKSZ - SizeOfPageHeaderData`.
- Optional page-feature flags, including proposals around extended checksums.
- Follow-on changes to recalculate page-dependent limits dynamically (`MaxHeapTupleSize`, `MaxHeapTuplesPerPage`, toast-related limits, etc.).
- Cluster/bootstrap/control-file integration, including `initdb`, `pg_resetwal`, and later SQL-visible metadata exposure.

### Patch evolution (v1/unprefixed to v4)

- **Early patches (unprefixed, v1/v2 style):** focused on reserved page space and experimental page-feature flags.
- **v2:** began splitting infrastructure and performance-oriented changes (fast div/mod support and visibility map updates), while iterating on page-feature mechanics.
- **v3 (28-patch series):** moved toward a larger refactor, introducing clearer layering (`PageUsableSpace`, dynamic limit calculations, reloptions hooks, and integration updates for toast and control-file paths).
- **v4:** continued decomposition and cleanup, added more explicit control-file/GUC/bootstrap wiring, and included SQL-facing visibility via `pg_control_system()`.

### SQL examples

These snippets reflect SQL-facing behavior discussed in later revisions (especially around SQL-visible control data). They require a patched build from this thread and are not expected to work on released PostgreSQL branches without those patches.

```sql
-- Inspect control-file settings through SQL (proposed in v4)
SELECT *
FROM pg_control_system();
```

```sql
-- Observe toast-related settings that become more dynamic
-- once page-usable-space calculations are no longer constant.
CREATE TABLE t_reserved (
  id bigint PRIMARY KEY,
  payload text
) WITH (toast_tuple_target = 2048);

INSERT INTO t_reserved
SELECT g, repeat('x', 5000)
FROM generate_series(1, 1000) AS g;

SELECT relname, reloptions
FROM pg_class
WHERE relname = 't_reserved';
```

## Community Insights

Review discussion repeatedly returned to architecture boundaries:

- Should this logic live in generic storage-manager infrastructure, or remain closer to access-method-specific code?
- Is consuming "prime" page space for storage metadata acceptable when access methods also need that space?
- Are the runtime costs and complexity justified without stronger benchmark evidence?
- Can TDE adoption in core be made realistic only if API surfaces reduce invasive changes?

Later thread messages also framed the strategic tension explicitly: TDE has high policy-compliance demand, but upstream inclusion depends on whether the design can deliver sufficient technical value at acceptable complexity cost.

## Technical Details

A notable technical trajectory in this thread is the shift from a narrowly scoped TDE hook-up toward a more general page-space model:

- Replace fixed page-capacity macros with calculated forms.
- Teach multiple subsystems (heap/index/visibility/TOAST-related limits) to consume a unified "usable space" abstraction.
- Keep cluster-wide settings discoverable and consistent through initialization and metadata tooling.
- Isolate optimization pieces (like fast division paths) so they can be reviewed independently from core storage semantics.

This decomposition made the series easier to reason about, but also revealed how many PostgreSQL internals assume stable page geometry.

## Current Status

The thread spans a long time window and multiple substantial rewrites. It demonstrates active design work and serious review engagement, but remains an in-progress proposal rather than a committed PostgreSQL feature. Key design questions about ownership boundaries, complexity, and performance trade-offs were still open.

## Conclusion

This discussion is valuable well beyond TDE itself. It highlights how a security-driven feature request can expose deep assumptions in PostgreSQL storage internals, and how getting a feature upstream may require reframing from "add one capability" to "improve the underlying abstraction." Whether this specific series lands or not, the `PageUsableSpace` direction and boundary analysis are likely to influence future storage and encryption work.
