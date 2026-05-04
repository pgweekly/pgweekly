# Extensible SMGR Hooks (2021–2022): Anastasia’s Proposal, Tablespace Semantics, and the Redo Wall

## Introduction

This [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CAP4vRV6JKXyFfEOf%3Dn%2Bv5RGsZywAQ3CTM8ESWvgq%2BS87Tmgx_g%40mail.gmail.com) is an early chapter in the long effort to make PostgreSQL’s storage manager (`smgr`) pluggable. Anastasia Lubennikova (June 2021) posted a compact hook-shaped design: replace the static `smgrsw[]` dispatch with a function that resolves an `f_smgr` vtable, defaulting to the existing `md` stack behind `smgr_standard()`. Yura Sokolov immediately asked the question that would dominate later work—**how a concrete implementation is chosen per relation**—and preferred an **immutable tablespace-level** property over a global hook. After months of silence, Andres Freund closed the loop for PostgreSQL 15 timing. Kirill Reshke (June 2022) then attached a far larger experiment: **catalog-backed storage managers**, `CREATE STORAGE MANAGER`, per-table syntax, **WAL format changes**, and a candid description of **redo** failures. Andrey Borodin tied the theme to **Greenplum** work offloading cold data to object storage.

## Technical Analysis

### Hook-first API (0001-smgr_api.patch)

The initial patch introduces `smgr_hook`: extensions implement `const f_smgr smgr_custom = { … }`, a resolver `const f_smgr *smgr_custom(BackendId backend, RelFileNode rnode)` that can branch on backend and `RelFileNode`, and assignment `smgr_hook = smgr_custom`. Core replaces direct `smgrsw[]` indexing with a resolver path that can return the built-in `md` implementation or a custom vtable.

Motivation in the opening post spans **storage-level compression and encryption**, **disk quotas**, **SLRU storage changes**, and other “swap the file layer” ideas (with references to contemporary threads and lazy restore notes from PGCon 2021).

### Selection semantics: global hook versus tablespace property

Yura’s review agrees `smgr` should support more than `md`, but argues the first patch does not show how **different implementations apply to different relations**. He proposes that the choice be an **immutable property of the tablespace**, carried into `smgropen`, and favors a **less invasive** core change: keep `reln->smgr_which` meaningful while making `smgrsw` a pointer to a growable array, plus something like `char smgr_name[NAMEDATALEN]` and linear lookup in a small registry.

That feedback foreshadows later “registration Redux” threads: **naming and binding** matter as much as exporting function pointers.

### CommitFest reality check

Andres’s March 2022 message notes **no activity for more than six months** and marks the item **returned with feedback**, explicitly ruling out merge for **PostgreSQL 15**. The thread nonetheless became the anchor for Kirill’s follow-up a few months later.

### Kirill’s prototype: `pg_smgr`, DDL, and WAL-carried identity

Kirill’s message documents a **custom table AM–style** pattern for storage managers:

- A catalog relation **`pg_smgr`** holding handler OIDs.
- Extension SQL such as `CREATE FUNCTION … RETURNS table_smgr_handler` and  
  `CREATE STORAGE MANAGER proxy_smgr HANDLER proxy_smgr_handler;`
- `CREATE TABLE … STORAGE MANAGER proxy_smgr` (with trial-and-error around naming the handler versus the manager in the user-facing clause).

He reports the branch **almost passes** `make check`, but fails around the **first checkpoint** or **crash recovery**. The core difficulty is again **redo**: during crash recovery, **syscache is not available** in the way normal backends use it, so resolving “which `smgr` applies to this `RelFileNode`?” from catalogs is unsafe. His attempted fix threads an **SMGR OID into WAL** wherever `RelFileNode` appears so replay can pick the implementation without catalog lookups—at the cost of an invasive WAL change and still-fragile integration.

The posted `v1-0001-Add-create-storage-manager-ddl-and-routines.patch` implements much of that surface area (parser additions for `CREATE STORAGE MANAGER`, `OBJECT_STORAGE_MANAGER`, `CREATE TABLE` optional `STORAGE MANAGER name`, and supporting routines).

### Industry parallel

Andrey Borodin describes related technology already in **Greenplum** (with a GitHub PR reference from 2022): extensions that **offload cold segments** to S3-like storage and pull them back on access—an analytical “cold append-only” pattern. He asks whether a similar extension would be valued in PostgreSQL if core exposed extensible `smgr`, framing the question around **very large, rarely touched append-mostly** datasets.

### SQL examples

The following snippets come from **Kirill’s on-list experiments** and the **`v1-0001-Add-create-storage-manager-ddl-and-routines.patch`** prototype. They are not valid on released PostgreSQL; they illustrate the proposed DDL and catalog shape only.

```sql
CREATE EXTENSION proxy_smgr;

SELECT * FROM pg_smgr;

CREATE TABLE tt(i int) STORAGE MANAGER proxy_smgr;
```

Extension script shape (from the thread and patch):

```sql
CREATE FUNCTION proxy_smgr_handler(internal)
RETURNS table_smgr_handler
AS 'MODULE_PATHNAME'
LANGUAGE C;

CREATE STORAGE MANAGER proxy_smgr HANDLER proxy_smgr_handler;
```

## Community Insights

- **Yura Sokolov**: steer selection toward **durable, immutable configuration** (tablespace) and keep core churn smaller than a wholesale hook rewrite.
- **Andres Freund**: **schedule and process**: dormant threads get returned; sets expectations about release boundaries.
- **Kirill Reshke**: demonstrates that **per-relation managers** ram into **redo and WAL invariants** quickly; catalog-only resolution is insufficient without auxiliary persisted state (a lesson echoed in later “SMGR hook Redux” discussions).
- **Andrey Borodin**: supplies a **real-world workload** (cold analytical data) where pluggable `smgr` is not theoretical.

## Technical Details

- A **global hook** can choose implementations dynamically but does not, by itself, define how that choice is **replayed** identically on all backends.
- **Tablespace-bound** configuration aligns with how file locations are already administered, but still requires a **non-catalog** path for early recovery if tablespaces themselves name custom managers.
- **WAL tagging** of `smgr` identity is powerful and expensive: it touches every record class carrying `RelFileNode`, increases format complexity, and must be justified against alternative metadata strategies.

## Current Status

The thread ends in **August 2022** without a merged upstream feature. Later work (for example Neon-driven registration and Percona/TDE-oriented chaining) revisits the same design space with different trade-offs. Historically, this thread is best read as the **origin document** for hook-based `smgr` extensibility and the first public **`CREATE STORAGE MANAGER`** sketch, together with a crisp statement of the **redo/syscache** problem.

## Conclusion

If the later “SMGR Redux” series is about registration, chains, and tooling, this 2021–2022 thread establishes the baseline: **hooks are easy to describe, hard to scope**, and **any per-relation story collides with recovery**. Yura’s tablespace-centric critique and Kirill’s WAL-heavy prototype are two sides of the same coin—metadata must be available when the catalogs are not yet trustworthy.
