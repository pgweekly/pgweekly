# Stronger XID and MultiXact Wraparound Warnings: Percent Detail and a 100M Head Start

## Introduction

Transaction ID wraparound is one of the few ways a PostgreSQL cluster can march toward data loss without a single “disk full” event. The server has long emitted `WARNING`s when the oldest normal XIDs in a database get uncomfortably close to the wraparound horizon, but the message was easy to misread: a raw count of transactions left can feel abstract, and operators report that the old default window was too tight for modern churn.

This [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/aRdhSSFb9zZH_0zc%40nathan) follows Nathan Bossart’s patch series to **clarify** those warnings and **widen** the early-warning distance—after review, a third idea (periodic `LOG` lines on a hot path) was dropped in favor of keeping monitoring in DBA tooling.

## Technical Analysis

### Percentage in `DETAIL`

The first change augments the existing wraparound warnings in `varsup.c` and the analogous paths in `multixact.c` with an `errdetail()` line of the form:

```text
DETAIL:  Approximately 1.86% of transaction IDs are available for use.
```

The percentage is computed as remaining headroom divided by half the ID space—matching how `xidWrapLimit` is defined relative to `MaxTransactionId`—so the fraction tracks the same “distance to real trouble” semantics as the core wraparound math. MultiXact warnings get the parallel `MultiXactIds` wording.

Documentation in `maintenance.sgml` was updated so the example `WARNING` / `DETAIL` / `HINT` block matches what users will see.

### Raising the warning threshold (40M → 100M)

Historically the code started “complaining loudly” when roughly **40 million** transactions remained before the stop limit; that constant is bumped to **100 million** for both normal XIDs and MultiXacts. The in-code comment’s gas-gauge analogy was adjusted (roughly “2% of full” → “5% of full”) to stay in spirit with the new numbers.

This is a behavioral change: warnings appear **earlier**, giving more time to run a database-wide `VACUUM`, fix long-lived prepared transactions, or clear stale replication slots before assignment failures.

### Dropped patch: periodic early `LOG` on `GetNewTransactionId()`

The original series included a third patch that would emit a `LOG` to the server log every million transactions once fewer than 500M XIDs remained—intended as an early nudge that would not spam client applications. Reviewer Shinya Kato argued the complexity was not worth it: it added state to `TransamVariablesData` and a modulo check on **`GetNewTransactionId()`**, a hot path, while DBAs who want earlier signal can already watch `age(datfrozenxid)` (and related catalog fields) with thresholds they choose.

Nathan agreed and **removed** that piece; the final submission is **two** patches (v4), not three.

### SQL examples

These queries do not trigger warnings by themselves; they are the standard way to see how close each database is to needing attention—the same class of monitoring Shinya pointed to when early `LOG` was declined.

```sql
-- Per-database transaction age (documented in maintenance chapter)
SELECT datname, age(datfrozenxid) FROM pg_database;
```

```sql
-- Broader freeze / anti-wraparound visibility (typical DBA checks)
SELECT relname, age(relfrozenxid) AS rel_age
FROM pg_class
WHERE relkind = 'r'
ORDER BY age(relfrozenxid) DESC
LIMIT 20;
```

Exact `WARNING` text and thresholds depend on your PostgreSQL version and build; the enhanced messages and 100M margin apply once the merged changes are in your tree.

## Community Insights

**Chao Li** reviewed v2 in depth: suggested using `(MaxTransactionId / 2)` instead of `PG_INT32_MAX` in the percentage denominator for consistency with wraparound math (Nathan adopted the open-coded half-range approach), noted that `%.2f` can show `0.00%` while counts remain large (the `errmsg` still carries the exact transaction count), called out typos (“transaction IDs”, subject “Periodically”), and wondered whether tying the removed early-`LOG` threshold to `autovacuum_freeze_max_age` would be clearer. Nathan preferred keeping wraparound warnings meaningful even under extreme GUC settings.

**Shinya Kato** approved the percent-detail idea, flagged that `maintenance.sgml` had to stay in sync with the new numbers (fixed in a later revision), and gave the decisive push to drop the hot-path logging patch.

**wenhui qiu** LGTM’d the direction and asked Nathan to look at a separate CommitFest item about surfacing *why* freeze age cannot drop—orthogonal to this series but a nice pointer toward richer operability.

## Technical Details

- **Files touched (final series):** `varsup.c`, `multixact.c`, `doc/src/sgml/maintenance.sgml`.
- **Percent formula:** `(wrapLimit - current) / (MaxTransactionId / 2) * 100` (and the MultiXact analogue with `MaxMultiXactId / 2`), aligning with how wrap limits are derived from the oldest potentially visible ID plus half the ring.
- **No new user GUC:** the longstanding comment that this threshold will not be made configurable remains; the change is a conservative default shift plus clearer messaging.

## Current Status

In the thread, Nathan indicated the work was **committed** (March 2026), after trimming the series to the percent-detail plus 100M-threshold changes. Operators should expect clearer `DETAIL` lines and earlier `WARNING`s once they run a release that includes that commit.

## Conclusion

Wraparound mitigation still boils down to vacuum discipline and fixing blockers to freezing, but this thread improves **signal quality** (percent alongside the raw countdown) and **reaction time** (warnings at 100M instead of 40M from the stop zone). Dropping the periodic `LOG` proposal kept the transaction-ID fast path lean while steering “tell me earlier” use cases toward explicit monitoring of `datfrozenxid` age—an intentional trade-off the reviewers endorsed.
