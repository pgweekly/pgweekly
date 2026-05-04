# Extensible Storage Manager API: Registration, Chaining, and the Long Road from SMGR Hooks

## Introduction

A sprawling [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CAEze2WgMySu2suO_TLvFyGY3URa4mAx22WeoEicnK%3DPCNWEMrA%40mail.gmail.com) starting in mid-2023 revisits one of the hardest boundaries in PostgreSQL: the storage manager (`smgr`), the layer that turns relation forks into concrete file I/O. Neon engineers proposed moving away from a hook that replaces the whole `smgr` implementation toward a **registration-based** design, so extensions can supply storage managers without hijacking core symbols. The discussion quickly attracted reviews from core hackers, a proof-of-concept `fsync_checker` built on top, Percona’s interest in transparent data encryption (TDE), and several rebased patch trains—most recently into 2026—touching chaining, test placement, and AIO callbacks.

Why it matters: cloud and extension vendors want compression, encryption, quotas, remote object storage, and better observability at the lowest I/O layer. Doing that safely requires clear APIs, sane recovery semantics, and agreement on how configuration reaches backends that cannot always read catalogs.

## Technical Analysis

### From hooks to a registry

Matthias van de Meent’s opening post (June 2023) positions the work against earlier proposals (including Anastasia Lubennikova’s) and against Yura Sokolov’s preference on-list for **registration** and namespace integration rather than a blunt replacement hook. Compared to prior hook-shaped approaches, extensions would no longer swap the `smgr` vtable in place; they would **register** implementations behind a core-managed table.

The first patch set sketched:

- A dynamically grown `smgrsw` array populated when `shared_preload_libraries` load.
- Optional `CREATE TABLESPACE … USING smgrname (…)` syntax so a tablespace could name a storage manager (later recognized as recovery-sensitive).
- Open problems: **catcache availability** in all backends that perform `smgr` work, broken redo paths in the prototype’s second patch, and tooling (`pg_dump`, tests).

### Early review: indirection, initialization, and types

Andres Freund questioned initializing dynamic managers in `PostmasterMain`, suggesting **`BaseInit()`** instead; pushed back on turning the static `smgrsw[]` into another pointer indirection, preferring a **bounded** number of registrable managers; asked about a `pg_compiler_barrier()` in registration; and wondered why per-relation state needed a globally determined `smgrrelation_size` duplicated on every relation object.

Tristan Partin’s review of the first revision noted API cleanups (`md*` helpers could become static), type widening for `SMgrId`, missing `InvalidSmgrId` checks, naming (`smgrname` in structs), a likely **rebase mistake** in `mdexists`, and stub formatting in `mddroptsp`.

### Recovery and catalog access

Kirill Reshke (December 2023) asked how **`smgr_redo`** could call into code that uses `get_tablespace` and catalog lookups—recovery often cannot use normal catalog buffer paths the way a running backend can. Matthias agreed the prototype was wrong to assume catalog access and floated **auxiliary mapping files** (analogy to `pg_filenode.map`) or tablespace-local metadata (e.g. alongside `/pg_tblspc/` symlinks) to record tablespace→`smgr` identity without going through syscache during replay.

On **per-relation** versus **per-tablespace** managers: Matthias argued tablespaces already model storage pools (“drives”), which aligns with where low-level placement policy belongs; per-relation selection is theoretically possible by minting many tablespaces but is operationally heavy.

### fsync_checker as a stress test for the API

Tristan’s January 2024 follow-up rebased Matthias’s work so managers can **inherit/delegate** to others, added a global override hook (explicitly “hacky” without single-winner rules), introduced a **`checkpoint_create` hook** for pre-checkpoint inspection, and shipped **`fsync_checker`** as a preload-only extension inspired by Andres’s earlier idea of an assert-only fd-layer hash tracking dirtied files until `fsync`. Tristan candidly noted the checker is **not fully sound** as-is because WAL-logged paths may skip `fsync` legitimately (for example around `log_smgrcreate()` / `createdb`-like flows).

Aleksander Alekseev (March 2024) flagged **cfbot** unhappiness and asked for an updated revision.

### Scope debate: infrastructure versus bundled demos

Nitin Jadhav (September 2024) argued the series mixes **core infrastructure** with auxiliary features such as `fsync_checker`, which he would split. He also framed the strategic fork: **hook-based** replacement is simpler but global; **registration** enables multiple managers and more flexibility at the cost of invasive core edits—worth it only if PostgreSQL is headed toward multi-manager support in practice.

Xun Gong (December 2024) echoed support for registration-style extensibility, citing analogies like Greenplum append-only storage using a dedicated `smgr` for different file layouts.

Andreas Karlsson (Percona, February 2025) rebased Tristan’s patches for style and TDE-related needs, added **`mdcreate` plumbing** to pass the prior `RelFileLocator` so encryption metadata can survive relfile swaps, and introduced **`smgr_chain`**: a comma-ordered **tail** storage manager (for example `md`) preceded by optional **modifier** managers (TDE, `fsync_checker`, or a hypothetical remote backend). That design lets encryption code wrap either `md` or a future non-local manager. He also mirrored Andres’s question about the **compiler barrier** and raised configuration and **benchmark/overhead** concerns.

### Reviews that shaped v5 and v6

Kirill’s March 2025 review asked whether **prefetch** must be mandatory for every extension manager, whether asserts should cover additional callbacks, whether small patches should merge, whether `fsync_checker` belongs in **`contrib`** versus **`src/test/modules`**, and requested commit-message rationale for the `RelFileLocator` refactor. CI remained unhappy until addressed.

Vignesh C set CommitFest status to **waiting on author** until Kirill’s notes were handled.

Zsolt Parragi (January 2026) posted **v6**: rebased, added missing asserts (including for **`startreadv`**), merged patches per feedback, defended **empty prefetch** implementations as acceptable, **moved `fsync_checker` to test modules**, expanded commit messages, and added a patch making **AIO handle callbacks extensible**—showing how the storage work intersects with newer asynchronous I/O directions.

### SQL examples

These snippets reflect **posted WIP patches** (through v6 in the thread), not any released PostgreSQL. The `smgr_chain` parameter is defined as **`PGC_POSTMASTER`** in the patch: set it in `postgresql.conf` and restart the server; `SHOW` is meaningful only on a build that includes the patch.

```sql
-- After restart with a valid chain configured at the server level, e.g.
-- smgr_chain = 'fsync_checker, md'   -- modifier(s) then tail manager
SHOW smgr_chain;
```

An early prototype in the thread explored **DDL** for tablespace-scoped manager selection. That exact syntax evolved and interacted badly with redo as discussed; treat any `CREATE TABLESPACE … USING …` form strictly as historical proposal material from the mailing list, not a language promise.

## Community Insights

- **Andres Freund**: initialization placement, avoid gratuitous indirection, sharp questions on barriers and structure layout.
- **Tristan Partin**: concrete API polish, compile-warning fix (`smgr.diff` adjusting `MdSMgrRelation` casts in `mdexists` / `mdcreate`), and a readable demonstration (`fsync_checker`) exposing real API pain points.
- **Kirill Reshke**: recovery-time catalog assumptions, CI status, API completeness (prefetch/asserts), and **where** debugging tools should live in the tree.
- **Nitin Jadhav / Xun Gong**: product-level framing—split concerns, but registration buys real multi-manager flexibility if the ecosystem commits to it.
- **Andreas Karlsson / Zsolt Parragi**: rebase maintenance, TDE-driven `RelFileLocator` threading, chaining for composable encryption, and ongoing CI hygiene.

## Technical Details

- **Dynamic registration** trades flexibility against pointer chasing and complexity in hot paths—exactly what Andres warned about.
- **WAL/redo** must resolve which `smgr` to invoke without syscache; file-backed maps or tablespace-local metadata are the natural design space.
- **Chaining** models cross-cutting concerns (encryption, validation) as **modifiers** ahead of a **tail** concrete store, echoing decorator-like stacks in user-space designs.
- **`fsync_checker`** illustrates why **fd-level accounting** is subtle: durability can arrive via WAL, not `fsync`, so naive “not fsynced before checkpoint” alarms false-positive unless the model understands log-driven durability.

## Current Status

As of Zsolt’s **January 2026 v6** post, the branch was rebased with review feedback addressed (`fsync_checker` relocated, asserts completed, patch consolidation). The thread remains a **long-running feature proposal**: core has not shipped this API in a release covered here, and design questions (configuration surface, overhead, exact recovery metadata) are still active research on-list.

## Conclusion

The thread is a case study in how deep storage extensibility touches **initialization, type identity, redo, benchmarks, and tree policy**—not just a vtable export. Registration and chaining answer different parts of that puzzle than a single global hook, but they demand disciplined answers for recovery and performance. Readers following TDE, remote storage, or PostgreSQL-on-disaggregated-storage should treat this archive as the live design document until an upstream commit lands.
