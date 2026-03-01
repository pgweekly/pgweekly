# More Speedups for Tuple Deformation: Precalculating attcacheoff

## Introduction

**Tuple deformation** is the process of extracting individual attribute values from a PostgreSQL heap tuple's raw byte representation into a `TupleTableSlot`. It happens constantly during query execution—every time a sequential scan, index scan, or join produces a row, the executor must "deform" that tuple to access column values. For workloads that process millions of rows, even small improvements to the deforming hot path can yield significant gains.

David Rowley has been steadily optimizing tuple deformation. In PostgreSQL 18, he landed several patches: `CompactAttribute` (5983a4cff), faster offset aligning (db448ce5a), and inline deforming loops (58a359e58). Building on that work, he proposed **precalculating `attcacheoff`** rather than computing it on every attribute access. The discussion has since evolved through v10 (February 2026), with **Andres Freund** contributing a NULL-bitmap-to-isnull conversion that pushes Apple M2 speedups to **63%** in some scenarios. The patch set remains under active review.

## Why This Matters

When the executor needs a column value from a tuple, it must:

1. **Align** the current offset according to the attribute's alignment
2. **Fetch** the value via `fetch_att()`
3. **Advance** past the attribute to the next one

These steps form a dependency chain: each offset depends on the previous. There is little opportunity for instruction-level parallelism. For fixed-width attributes, PostgreSQL can cache the offset (`attcacheoff`) to avoid recomputing alignment and length—but that caching was previously done *inside* the deforming loop. David's idea: do it *once* when the `TupleDesc` is finalized, not on every tuple.

## Technical Approach

### TupleDescFinalize()

The core change introduces `TupleDescFinalize()`, which must be called after a `TupleDesc` has been created or changed. This function:

1. **Pre-calculates** `attcacheoff` for all fixed-width attributes
2. **Records** `firstNonCachedOffAttr`—the first attribute (by `attnum`) that is varlena or cstring and thus cannot have a cached offset
3. **Enables** a tight loop that deforms all attributes with cached offsets before falling through to attributes that require manual offset calculation

If a tuple has a NULL before the last attribute with a cached offset, the code can only use `attcacheoff` up to that NULL—but for tuples without early NULLs, the fast path handles many attributes in a tight loop without any per-attribute offset arithmetic.

### Dedicated Deforming Loops

The patch adds a dedicated loop that processes all attributes with a precomputed `attcacheoff` before entering the loop that handles varlena/cstring attributes. For tuples with `HEAP_HASNULL` set, the current code calls `att_isnull()` for every attribute. A further optimization: keep deforming without calling `att_isnull()` until we reach the first NULL. Test #5 in the benchmark (first col int not null, last col int null) highlights this—it often shows the largest speedup.

### Optional OPTIMIZE_BYVAL Loop

An optional variant adds a loop for tuples where all deformed attributes are `attbyval == true`. In that case, `fetch_att()` can be inlined without the branch that handles pointer types, reducing branching and yielding a tighter loop. The tradeoff: when the optimization doesn't apply, there is extra overhead to check `attnum < firstByRefAttr`. Benchmark results vary by hardware and compiler as to whether this helps.

## Benchmark Design

To stress tuple deformation, David designed a benchmark that maximizes deforming work relative to other CPU:

```sql
SELECT sum(a) FROM t1;
```

The `a` column is almost last, so all prior attributes must be deformed before `a` can be read. Eight test schemas cover combinations of first column (int/text, null/not null) and last column (int null/not null). For each of the 8 tests, he ran with 0, 10, 20, 30, and 40 extra `INT NOT NULL` columns—40 scenarios per benchmark run. Each scenario used 1 million rows.

## Benchmark Results

Results varied by hardware and compiler:

- **AMD Zen 2 (3990x) with GCC**: Up to **21%** average speedup with `OPTIMIZE_BYVAL`; some tests exceed **44%**; no regressions.
- **AMD Zen 2 with Clang**: Some small regressions in the 0-extra-column tests.
- **Apple M2**: Tests #1 and #5 improve significantly; others less so; a few slight regressions with certain patches.
- **Intel (Azure)**: Benchmarks run on shared, low-core instances; results were noisier due to co-located workloads.

## Patch Evolution

### v1 → v3 (December 2025 – January 2026)

- **v1**: Three patches—0001 (precalculate attcacheoff), 0002 (experimental NULL bitmap look-ahead), 0003 (remove dedicated hasnulls loop)
- **v2**: Rebase, fix Assert for NULL bitmap in 0003, JIT fix (remove `TTS_FLAG_SLOW`), more benchmarks
- **v3**: Rebase, drop 0002 and 0003 (benchmarks showed little advantage), keep only 0001

### v4 (January 2026)

Addressed code review from Chao Li:

- **NULL bitmap mask** (tupmacs.h): Clarified comment—when `natts & 7 == 0`, the mask is zero and the code correctly returns `natts`
- **Uninitialized TupleDesc**: `firstNonCachedOffAttr == 0` means no cached attributes; `-1` means uninitialized. Added Asserts with hints to call `TupleDescFinalize()` if they fail
- **Typo**: "possibily" → "possibly"
- **LLVM**: Fixed compiler warning

### v5–v8 (January–February 2026): Andres Freund's NULL Bitmap Optimization

**Andres Freund** joined the discussion and proposed a key improvement: instead of calling `att_isnull()` for each column, compute the `isnull[]` array directly from the NULL bitmap using a SWAR (SIMD Within A Register) technique. The idea: multiply one byte of the bitmap by a carefully chosen value (e.g. `0x204081`) so each bit spreads into a separate byte, then mask. This avoids a 2KB lookup table and works well on most hardware.

David implemented this in patch 0004 ("Various experimental changes"). Additional changes in 0004:

- **`populate_isnull_array()`**: Converts the NULL bitmap to `tts_isnull` in bulk using the multiplication trick
- **`tts_isnull` sizing**: Rounded up to a multiple of 8 so the loop can write 8 bytes at a time (avoids `memset` inlining issues)
- **`t_hoff`**: For `!hasnulls` tuples, use `MAXALIGN(offsetof(HeapTupleHeaderData, t_bits))` instead of `t_hoff`
- **`fetch_att_noerr()`**: New variant without `elog` for the common `attlen == 8` case

**John Naylor** noted that `__builtin_ctz(~bits[bytenum])` is undefined when the byte is 255; David fixed this with a cast: `pg_rightmost_one_pos32(~((uint32) bits[bytenum]))`.

Results with 0004: Apple M2 averaged **53% faster** than master (or ~63% excluding 0-extra-column tests). Andres suggested `pg_nounroll` and `pg_novector` pragmas to prevent GCC from over-vectorizing `populate_isnull_array()`, which was generating poor code.

### v9 (February 24, 2026)

- **Resequenced patches**: `deform_bench` moved to 0001 for easier master benchmarking
- **0004 (new)**: Sibling-call optimization in `slot_getsomeattrs`—moved `slot_getmissingattrs()` into `getsomeattrs()` so the compiler can apply tail-call optimization. Reduces overhead and improves 0-extra-column tests
- **0005 (new)**: Shrink `CompactAttribute` from 16 to 8 bytes—`attcacheoff` → `int16` (max 2^15), bitflags for booleans. Andres noted the 8-byte size lets the compiler use a single LEA with scale factor 8; 6 bytes would require two LEA instructions

### v10 (February 25, 2026) — Latest Patch Set

Based on the actual v10 patch content:

**0003 (Optimize tuple deformation)**:
- `firstNonCachedOffsetAttr`: index of the first attribute without a cached offset
- `firstNonGuaranteedAttr`: index of the first nullable, missing, or `!attbyval` attribute. When deforming only up to this point, the code need not access `HeapTupleHeaderGetNatts(tup)`—a dependency reduction that helps the CPU pipeline
- `TTS_FLAG_OBEYS_NOT_NULL_CONSTRAINTS`: opt-in flag for the guaranteed-attribute optimization (some code deforms tuples before NOT NULL validation)
- `populate_isnull_array()`: uses `SPREAD_BITS_MULTIPLIER_32` (0x204081) to spread each bit of the inverted NULL bitmap into a separate byte; processes lower 4 and upper 4 bits separately to avoid uint64 overflow
- `fetch_att_noerr()`: variant of `fetch_att()` without `elog` for invalid attlen; safe when attlen comes from `CompactAttribute`
- `first_null_attr()`: finds the first NULL in the bitmap using `pg_rightmost_one_pos32` or `__builtin_ctz`

**0004 (Sibling-call optimization)**:
- `getsomeattrs()` is now responsible for calling `slot_getmissingattrs()`
- `slot_getmissingattrs()`: replaced `memset` with a for-loop (benchmarks showed the loop is faster)
- `slot_deform_heap_tuple()`: calls `slot_getmissingattrs()` at the end when `attnum < reqnatts`; parameter renamed from `natts` to `reqnatts`

**0005 (8-byte CompactAttribute)**:
- `attcacheoff` → `int16`; offsets > `PG_INT16_MAX` are not cached
- Bitflags for `attispackable`, `atthasmissing`, `attisdropped`, `attgenerated`
- Stores `cattrs = tupleDesc->compact_attrs` to help GCC generate better code (avoids repeated `TupleDescCompactAttr()` calls)

**Review fixes**:
- **Amit Langote**: Fixed rebase noise (duplicate `attcacheoff` check)
- **Zsolt Parragi**: Big-endian fix—`pg_bswap64()` before `memcpy` in `populate_isnull_array()`
- **Typos**: "benchmaring" → "benchmarking", "to info" → "into"
- **Andres**: Set `*offp` before `slot_getmissingattrs` to reduce stack spills; use `size_t` for `attnum` to fix GCC `-fwrapv` codegen

### deform_bench and Benchmark Infrastructure

**Andres** and **Álvaro Herrera** discussed where to put `deform_bench`: `src/test/modules/benchmark_tools`, `src/benchmark/tuple_deform`, or a single extension for micro-benchmarks. Andres argued for merging useful tools incrementally rather than waiting for a full suite. David prefers to focus on the deformation patches first; deform_bench may be committed separately.

## Code Review: Chao Li's Feedback

Chao Li reviewed the patch and raised several points:

1. **NULL bitmap mask**: Add a comment to clarify no overflow/OOB risk when `natts & 7 == 0`
2. **Uninitialized TupleDesc**: Initialize `firstNonCachedOffAttr` to `-1` in TupleDesc creation; Assert `>= 0` in `nocachegetattr()`
3. **Semantic consistency**: Use 0 for "no cached attributes," >0 for "some cached"
4. **Typo**: "possibily" → "possibly"

David addressed all in v4.

## Current Status

- **v10** (February 2026) is the latest patch set: 0001 (deform_bench), 0002 (TupleDescFinalize stub), 0003 (main optimization), 0004 (sibling-call + NULL bitmap→isnull), 0005 (8-byte CompactAttribute)
- **Andres Freund** supports merging 0004 as a clear win; 0005's benefit is less certain (helps with LEA addressing when deforming few columns)
- Active review from **Zsolt Parragi** (Percona), **Álvaro Herrera**, **John Naylor**, **Amit Langote**
- deform_bench placement (src/test/modules vs. src/benchmark) still under discussion; David prefers to land the optimization patches first

## Conclusion

Precalculating `attcacheoff` in `TupleDescFinalize()` and using a dedicated tight loop for attributes with cached offsets yields meaningful speedups for tuple deformation on modern CPUs. The optimization is most effective when tuples have many fixed-width columns and few or late NULLs. With Andres Freund's NULL-bitmap-to-isnull conversion (the "0x204081" SWAR trick), Apple M2 sees up to **63%** speedup excluding edge cases. The sibling-call optimization in `slot_getsomeattrs` further reduces overhead. Results depend on hardware and compiler; GCC can over-vectorize some loops, addressed with pragmas or `size_t` for loop indices. The patch set (v10) has been refined through extensive review from Andres, John Naylor, Zsolt Parragi, Álvaro Herrera, and Amit Langote, and is progressing toward integration.

## References

- [Thread: More speedups for tuple deformation](https://www.postgresql.org/message-id/flat/CAApHDvpoFjaj3%2Bw_jD5uPnGazaw41A71tVJokLDJg2zfcigpMQ%40mail.gmail.com)
- Related v18 work: 5983a4cff (CompactAttribute), db448ce5a (faster offset aligning), 58a359e58 (inline deforming loops)
