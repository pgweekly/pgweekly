# PostgreSQL Storage I/O Transformation Hooks: Decoupling Core Storage from Encryption Logic

## Introduction

A [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CAAAe_zCHNVKxETAWBZg3YWPdmXsf-G2OcQtQOYQn4AeZaES4tQ%40mail.gmail.com) proposes a new hook-based protocol for data transformation in PostgreSQL storage paths, with Transparent Data Encryption (TDE) as the reference use case. The core idea is to let extensions handle cryptographic transforms while PostgreSQL core keeps ownership of storage orchestration, checksums, and durability semantics.

## Technical Analysis

### What the patch set introduces

The patch series adds five new hook points in core I/O and WAL paths:

- `mdread_post_hook`
- `mdwrite_pre_hook`
- `mdextend_pre_hook`
- `xlog_insert_pre_hook`
- `xlog_decode_pre_hook`

It also introduces a page-level transform marker in `pd_flags` and a WAL marker (`XLR_BLOCK_ID_TRANSFORMED = 251`) so replay can fail fast when transformed payload appears without the required extension.

### Patch evolution (v1 to v4)

- **v1**: Initial infrastructure (`0001`) and `contrib/test_tde` reference extension (`0002`).
- **v2**: Build-system integration updates for `test_tde` (Meson and contrib wiring).
- **v3**: Compile fix for `mdread_post_hook` call site (`(void **)` cast for pointer-type compatibility).
- **v4**: Added `0003` test portability fix for tablespace regression test behavior across environments.

### SQL examples

These examples come from `contrib/test_tde/sql/basic.sql` in the posted patches. They are representative of the design and require a patched build with `shared_preload_libraries = 'test_tde'`; they are not available in released PostgreSQL versions.

```sql
SHOW test_tde.key;

CREATE TABLE test_encrypt (
  id int,
  secret_data text,
  secret_number int
);

INSERT INTO test_encrypt VALUES
  (1, 'sensitive data 1', 12345),
  (2, 'sensitive data 2', 67890),
  (3, NULL, 11111);

SELECT COUNT(*) FROM test_encrypt;
SELECT secret_data FROM test_encrypt WHERE secret_number = 12345;
```

```sql
CREATE TABLE test_set_tablespace (id int, data text);
INSERT INTO test_set_tablespace
SELECT g, 'data ' || g FROM generate_series(1, 50) g;

ALTER TABLE test_set_tablespace SET TABLESPACE regress_tde_tblspc;
SELECT COUNT(*) FROM test_set_tablespace;
```

## Community Insights

Reviewer feedback focused less on cryptography itself and more on architecture boundaries:

- Whether WAL and buffer-manager hooks should be split into separate discussions.
- Whether these new hooks overlap with ongoing extensible-SMGR work that already enables storage-layer interception.
- Whether benchmark data is sufficient for real hook-enabled workloads, especially inside critical sections.

The author argued that narrow transform hooks reduce maintenance risk versus replacing broader storage implementations, while reviewers emphasized API overlap and long-term maintainability concerns.

## Technical Details

The reference extension (`test_tde`) demonstrates several safety rules:

- Verify checksums on encrypted page images before decrypting.
- Clear transform marker and recompute checksum for plaintext buffers after reverse-transform.
- Pre-allocate and manage encryption contexts in a memory context allowed in critical sections.
- Chain hooks to preserve composability with other extensions.

On the WAL side, the transform marker and decode hook path are intended to prevent accidental interpretation of transformed records when required transform logic is absent.

## Current Status

The thread reached a fourth revision (`v4`) with iterative fixes, but discussion remained in RFC/review territory. Core design questions (relationship to SMGR extensibility and the scope split between WAL and heap/buffer hooks) were still active, so this is best understood as an evolving infrastructure proposal rather than a committed PostgreSQL feature.

## Conclusion

This thread is a useful example of PostgreSQL’s extension philosophy under pressure from security requirements: keep the core generic, expose carefully bounded hooks, and require explicit protocol markers to preserve safety. The technical direction is promising, but integration with broader storage extensibility efforts and performance validation will likely determine whether the approach lands upstream.
