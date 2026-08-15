# Table AM Interface Enhancements: Extensibility and Design Boundaries

## Introduction

In November 2023, Alexander Korotkov opened a large [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CAPpHfdurb9ycV8udYqM%3Do0sPS66PJ4RCBM1g-bBpvzUfogY0EA%40mail.gmail.com) with twelve patches intended to make PostgreSQL's **Table Access Method (Table AM)** interface capable of supporting more than heap-like storage. The immediate motivation was OrioleDB, but the proposed APIs targeted a broader class of engines: index-organized tables, alternative MVCC schemes, custom row identifiers, and table AMs that coordinate their own indexes.

The thread became important for a second reason. Several patches were committed shortly before the PostgreSQL 17 feature freeze, then received deeper post-commit review and were almost entirely reverted. It is therefore a useful case study in two kinds of boundary: the technical boundary between the executor, Table AM, Index AM, and relcache; and the project boundary between a promising extensibility idea and an API mature enough to freeze for external users.

## Why the Existing Table AM Boundary Was Not Enough

PostgreSQL's Table AM API lets a relation choose a storage implementation, but much of the surrounding behavior still assumes heap-like concepts. A heap tuple has a block-and-offset `ctid`; heap ANALYZE samples blocks; `INSERT ... ON CONFLICT` uses speculative insertion; and the executor normally inserts secondary-index tuples after calling the table AM.

Those assumptions become awkward for an index-organized or undo-based engine:

- Refinding a concurrently updated row can be much more expensive than following a heap TID.
- A row identifier may be a primary key or another variable-length value, not a 48-bit block/offset pair.
- Sampling physical blocks may be meaningless.
- The primary index may be the table itself, so speculative heap insertion followed by executor-driven index insertion is the wrong decomposition.
- AM-specific metadata and reloptions may need richer lifetimes and layouts than core currently exposes.

The proposal was not one feature but an attempt to move several of these decisions across the API boundary at once.

## The Original Twelve-Patch Design

The v1 series can be grouped into four themes.

### Tuple identity, lifetime, and MVCC

The first group tried to make tuple handling less heap-specific:

- Let `tuple_update()` and `tuple_delete()` lock an already-refound updated tuple, avoiding a second lookup after concurrent modification.
- Add an EvalPlanQual isolation test for `DELETE ... RETURNING`.
- Let an AM explicitly free complex, multi-allocation data stored behind `rd_amcache`.
- Ask whether a tuple belongs to the current transaction without assuming PostgreSQL transaction IDs.
- Let `tuple_insert()` return a native slot whose system attributes are understood by that AM.

Review improved one of these ideas substantially: the current-transaction check moved from `TableAmRoutine` to `TupleTableSlotOps`. That places the operation next to the representation that knows how to interpret the tuple, rather than at the relation-wide AM level.

### ANALYZE and sampling

The existing interface offered `scan_analyze_next_block` and `scan_analyze_next_tuple`. The proposed `relation_analyze` callback instead let an AM choose the tuple-sampling function, making it possible to implement a non-block-oriented strategy.

The abstraction goal was sound, but the patch also moved or duplicated a large part of `acquire_sample_rows()`. That made the compatibility story difficult: a block-oriented external AM could previously reuse PostgreSQL's sampling algorithm by supplying two callbacks, while the new shape pushed it toward copying more of `analyze.c`.

### INSERT, indexes, and reloptions

Another set attempted to give the table AM more control over operations that currently span several subsystems:

- Replace the speculative-insert callbacks with a whole-operation `tuple_insert_with_arbiter()` API for `INSERT ... ON CONFLICT`.
- Let tuple insertion tell the executor not to insert index tuples because the AM handled them itself.
- Let a table AM define table reloptions and influence reloptions for indexes built on its tables.
- Notify the table AM when an index is created.

These patches exposed the hardest architectural question in the series: how much executor or Index AM knowledge may safely cross into Table AM? Passing `EState` would couple extensions to executor internals. Letting a table AM silently replace or reinterpret a requested index would violate the user's explicit choice of Index AM. Handling index insertion inside the table AM also raises when executor index state should be initialized and who owns index removal.

### RowRefType and variable-length RowID

The final pair separated the kind of row reference from row-lock strength and introduced a `bytea`-like `RowID` as an alternative to `ctid`. This would allow an index-organized table to identify rows with something larger or structurally different from a block number plus offset.

Reviewers pointed out that Table AM changes alone could not make such identifiers useful to ordinary indexes: the existing Index AM API still trafficked in heap-style TIDs. The proposal therefore described only one side of a cross-API design.

## How the Patch Set Evolved

The downloaded archive contains every referenced revision: main-series v1 through v8, custom-reloptions v9 through v11, three ANALYZE follow-ups, and the final revert patch.

Across v2–v4, the series was rebased and reordered, the transaction-current test moved into tuple-slot operations, `rd_amcache` cleanup gained an assertion, and compiler warnings found by testing on GCC 11 were fixed. Review also identified slot ownership as a real issue: allowing `tuple_insert()` to return a different slot is not merely a return-type change, because callers must know whether to clear, reuse, or release both the input and returned slots.

By v5, several smaller patches had been committed and the remaining series shrank to eight. The row-reference work was explicitly deferred because removing `ROW_MARK_COPY` and defining FDW behavior needed more design. The `INSERT ... ON CONFLICT` patch was also held back: moving executor logic into the AM while directly exposing `EState` broke encapsulation, and designing an opaque callback context was too large a change for the end of the cycle.

Custom reloptions then evolved separately. The first committed design assumed too much about `StdRdOptions` and triggered a Coverity warning in `RelationParseRelOptions()`. After that commit was reverted, v9–v11 split options into common fixed-shape values visible to core and opaque AM-specific values, eventually adding a `test_tam_options` module. Even this better-tested redesign still left questions about duplicated parsing paths, defaults known by relcache, documentation, and API shape.

## The ANALYZE Compatibility Trap

The most consequential review concerned ANALYZE. Andres Freund noted that at least four production-used external AMs implemented some ANALYZE behavior. After the committed redesign, block-oriented AMs could no longer reuse `acquire_sample_rows()` without substantial duplication.

Several quick repairs followed:

1. Restore the old block callbacks alongside the new relation-level callback.
2. Instead generalize `acquire_sample_rows()` so both heap-like and non-block AMs could reuse it.
3. Adjust the generalized callback's comments without changing the code.

The difficulty was compounded by concurrent work that made ANALYZE use streaming reads and prefetching. Heap-style prefetch assumes that logical block numbers correspond to physical locations, which is precisely the assumption a generic Table AM interface should avoid. Reverting the Table AM change therefore required carefully disentangling later streaming-read work rather than simply undoing one commit.

This episode illustrates a recurring extensibility rule: compatibility is not only whether an extension still compiles. It also includes whether a previously reusable core algorithm has silently become something every extension must copy and maintain.

## Post-Commit Review and Reverts

In late March and early April 2024, multiple pieces were committed: complex `rd_amcache` cleanup, returned insertion slots, current-transaction slot checks, updated-tuple locking, generalized ANALYZE, custom reloptions, and AM-controlled index insertion. Review after those commits raised concrete concerns about corruption risk, object ownership, unprepared executor state, external AM compatibility, unclear documentation, and incomplete cross-API design.

The timing mattered because these were extension APIs close to feature freeze. Once released, an imperfect callback becomes a compatibility promise. The discussion therefore shifted from polishing patches to deciding which commits had enough consensus and which should leave PostgreSQL 17.

On April 11, the questioned `rd_amcache`, returned-slot, updated-tuple-locking, custom-reloptions, and index-insertion-control commits were reverted. The ANALYZE redesign and its follow-up helper were reverted on April 16 after accounting for the intervening streaming-read changes.

## What Survived

Two small changes from the work remain in current PostgreSQL source:

- `TupleTableSlotOps.is_current_xact_tuple()` (commit `0997e0af273`) lets the tuple representation answer whether its tuple was created by the current transaction. This is the proposal that found a cleaner semantic home during review.
- The parameter order of `TableAmRoutine.relation_copy_for_cluster()` was corrected (commit `97ce821e3e1`), a straightforward API consistency fix.

The broader proposals—custom Table AM reloptions, AM-controlled index insertion, a returned native insertion slot, generalized ANALYZE, `RowRefType`, variable-length `RowID`, index-creation notification, and a whole-operation `INSERT ... ON CONFLICT` callback—did not land from this thread. They should not be described as PostgreSQL 17 features.

There is deliberately no runnable SQL example here. The thread is about internal extension callbacks, and the proposed user-visible effects either depended on hypothetical AMs or were reverted; presenting SQL as current behavior would be misleading.

## Community and Design Lessons

The technical feedback converged on several principles:

- **Put behavior beside the object that owns the semantics.** Moving the transaction-current test to tuple slots was more precise than adding another relation-level callback.
- **Define ownership explicitly.** APIs returning replacement slots or retaining complex cache objects need exact allocation, cleanup, invalidation, and reuse contracts.
- **Design both sides of a boundary.** Variable-length row identifiers and index-organized tables require coordinated Table AM and Index AM changes.
- **Keep executor internals opaque.** An AM callback that depends on `EState` can freeze unrelated executor implementation details.
- **Preserve reusable defaults.** A flexible callback is less useful if every heap-like extension must copy a large core algorithm.
- **Treat extension APIs as long-lived commitments.** Documentation, representative test modules, and explicit reviewer consensus are part of the design, especially near feature freeze.

## Conclusion

The thread did not deliver the comprehensive Table AM redesign it initially proposed, but it clarified where such a redesign must be stronger. PostgreSQL needs better support for storage engines that are not “heap with small variations,” yet each new callback must specify ownership, fallback behavior, interaction with indexes and the executor, and compatibility for existing external AMs.

The surviving tuple-slot method is a modest result, but also an instructive one: the most durable extensibility changes are often those that narrow a semantic boundary instead of merely enlarging an interface.

## References

- [Mailing-list thread: Table AM Interface Enhancements](https://www.postgresql.org/message-id/flat/CAPpHfdurb9ycV8udYqM%3Do0sPS66PJ4RCBM1g-bBpvzUfogY0EA%40mail.gmail.com)
- [PostgreSQL documentation: Table Access Method Interface Definition](https://www.postgresql.org/docs/current/tableam.html)
- [PGCon 2023: Future of Table Access Methods](https://www.pgcon.org/events/pgcon_2023/schedule/session/470-future-of-table-access-methods/)
