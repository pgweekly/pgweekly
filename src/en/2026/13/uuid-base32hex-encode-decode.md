# UUID and Base32hex Encoding

## Introduction

Compact, copy-paste-friendly representations of UUIDs come up in URLs, logs, JSON payloads, and anything humans have to read aloud. A [pgsql-hackers thread](https://www.postgresql.org/message-id/flat/CAJ7c6TOramr1UTLcyB128LWMqita1Y7%3Darq3KHaU%3Dqikf5yKOQ%40mail.gmail.com) starting in October 2025 began with advocacy for two new builtins—`uuid_to_base32hex()` and `base32hex_to_uuid()`—encoding UUIDs as 26-character [RFC 4648 Section 7](https://datatracker.ietf.org/doc/html/rfc4648#section-7) **base32hex** strings. What followed was less about the numeric format (everyone agreed it is standardized and useful) and more about **where that behavior should live** in PostgreSQL’s SQL surface: separate UUID-specific functions, or the existing `encode()` / `decode()` pipeline plus explicit type conversions.

## Why This Matters

PostgreSQL already stores UUIDs efficiently as a dedicated type and ships `uuidv7()` and friends. Text interchange formats still matter for APIs and cross-system contracts. **Base32hex** preserves **lexicographic sort order** of the underlying bytes (unlike typical base64), stays **case-insensitive for decoding**, and avoids ambiguous oral spelling of mixed-case hex. The thread connects that format to real-world precedent (DNSSEC encoders, JSON-heavy pipelines) and to **RFC 9562** positioning: canonical hyphenated UUID strings for compatibility, binary storage in databases, and a desire—not always fully standardized in the RFC text—for *one* compact text encoding to reduce ecosystem fragmentation.

## Technical Analysis

### Original proposal (conceptual)

The opening pitch (preserved in the thread as `hi-hackers.txt`) described:

- **`uuid_to_base32hex(uuid) → text`**: 26 uppercase base32hex characters, no hyphens, no padding; two zero bits appended so 128 bits map cleanly to base32’s 5-bit alphabet.
- **`base32hex_to_uuid(text) → uuid`**: case-insensitive decode; invalid input yields NULL.

Sergey Prokhorenko also argued against alternatives such as base36 (performance) and Crockford Base32 (weaker presence in standard libraries), positioning base32hex as the practical compact choice.

### Community direction: compose, do not duplicate

**Aleksander Alekseev**’s first reply mixed process advice (avoid mass `Cc:` lists; use `git format-patch`; register on [Commitfest](https://commitfest.postgresql.org/)) with API feedback: standalone UUID helpers are **not composable**. Prefer:

1. Explicit **`uuid ↔ bytea`** casts (or conversions).
2. Extending **`encode(bytea, ...)`** and **`decode(text, ...)`** with a **base32hex** format—so usage looks like:

```sql
SELECT encode(uuidv7()::bytea, 'base32hex');
```

(The early mail said `'base32'`; the implemented format name in later patches is **`base32hex`**, matching RFC 4648’s “base32hex” alphabet.)

**Andrey Borodin** and **Jelte Fennema-Nio** agreed that extending `encode()` matches existing practice; Jelte pointed to **base64url** support added in PostgreSQL 18 ([commit e1d917182](https://git.postgresql.org/cgit/postgresql.git/commit/?h=REL_18_0&id=e1d917182c1953b16b32a39ed2fe38e3d0823047)) as a precedent for adding encodings to the same entry points.

### What landed in the patch series (high level)

The downloadable series (revisions through **v12** in the thread attachments) converges on:

- **`encode(data bytea, 'base32hex')`** and **`decode(text, 'base32hex')`** implemented in `encode.c`, with RFC 4648 padding on encode and tolerant decode (padded or unpadded; case-insensitive; whitespace ignored—see patch headers for exact rules).
- Documentation under `func-binarystring.sgml` describing base32hex and explicitly recommending:

```sql
rtrim(encode(uuid_value::bytea, 'base32hex'), '=')
```

for a **26-character** compact UUID string versus 36 characters in canonical hex-with-hyphens form.

- Companion work: **explicit casting between `uuid` and `bytea`**, so the `encode()` path does not require ad-hoc UUID-only builtins.

### SQL examples (illustrative)

The snippets below match the **API shape** discussed on the list (`encode` / `decode` with `'base32hex'` and `uuid::bytea`). They assume a PostgreSQL build that includes that support—the thread’s patches were still under review when this post was written.

**1. Raw RFC output (with padding)** — `encode()` emits `'='` padding; for a 16-byte UUID the string is longer than 26 characters until trimmed:

```sql
SELECT encode('019535d9-3df7-79fb-b466-fa907fa17f9e'::uuid::bytea, 'base32hex') AS padded;
-- 32 characters (RFC 4648 padding to a multiple of 8; ends with '=')
```

**2. Compact 26-character form** — strip padding for URLs and logs (same example UUID as in the original thread):

```sql
SELECT rtrim(
         encode('019535d9-3df7-79fb-b466-fa907fa17f9e'::uuid::bytea, 'base32hex'),
         '='
       ) AS short_id;
-- 06AJBM9TUTSVND36VA87V8BVJO
```

**3. Round trip** — `decode()` yields `bytea`; cast back to `uuid`:

```sql
WITH x AS (
  SELECT rtrim(
           encode('019535d9-3df7-79fb-b466-fa907fa17f9e'::uuid::bytea, 'base32hex'),
           '='
         ) AS short_id
)
SELECT short_id,
       decode(short_id, 'base32hex')::uuid AS back_to_uuid
FROM x;
-- back_to_uuid = 019535d9-3df7-79fb-b466-fa907fa17f9e
```

**4. New IDs with `uuidv7()`** (when you want time-ordered values and a short external form):

```sql
SELECT rtrim(encode(uuidv7()::bytea, 'base32hex'), '=') AS short_new_id;
```

### Patch evolution

Early revisions explored UUID-specific `uuid_encode` / `uuid_decode` style APIs; later revisions fold behavior into **`encode`/`decode`**, rebase cast support, add regression tests, and iterate on documentation—including notes on **collation** and **sortability** of encoded text (additional small doc patches appear in the attachment list).

## Community Insights

- **Process and visibility**: Aleksander noted Sergey was not subscribed to the list, so the original message did not reach all readers; subscribing and attaching the full proposal helped restore context.

- **Why base32hex**: Sergey summarized advantages—sort preservation, compactness, wide library support, easy dictation without case ambiguity, and simple codecs for JSON-centric systems. He also framed **standardization pressure**: many ad-hoc “short UUID” schemes risk incompatibility; RFC 9562 authors reportedly favored a single compact encoding, even if the full compact-text story did not ship inside that RFC’s timeline.

- **API surface area**: **Masahiko Sawada** argued that naming another `uuid_*` encoding beside `encode()` invites a proliferation of one-off functions; **`+1` for `encode`/`decode` + `uuid`/`bytea` conversion**, with negligible cost for the cast.

- **Design tension—polymorphism vs. casts**: Sergey asked whether `encode()` could take `uuid` directly and whether `decode()` could return `uuid` without going through `bytea`. Masahiko explained PostgreSQL cannot overload `decode(text, text)` with two different result types for the same signature; **casts plus inline SQL wrappers** are the idiomatic compromise. He gave an **inlineable** example:

```sql
CREATE FUNCTION uuid_to_base32(u uuid) RETURNS text
LANGUAGE SQL IMMUTABLE STRICT
BEGIN ATOMIC
  SELECT encode($1::bytea, 'base32hex');
END;
```

and noted the runtime difference versus calling `encode(...)` with an explicit cast is essentially a small conversion cost.

- **Sergey’s “one short format” concern**: He warned against a world where different systems pick Crockford base32, base36, unsorted base64, etc. Masahiko countered that **developers still choose** encodings when integrating heterogeneous stacks—e.g., hex when every component supports it. The thread’s engineering answer is still to expose **one standard base32hex** in core and document the **26-character** UUID recipe.

## Technical Details

- **Padding**: RFC-style `encode()` output includes **`=`** padding to a multiple of 8 characters; trimming yields the compact 26-character UUID form used in the original proposal.
- **Sort order**: Base32hex preserves byte order for lexicographic comparisons; documentation and follow-up patches discuss **collation** effects when comparing encoded **text** (binary UUID comparisons remain the authoritative ordering).
- **Integration**: Placing the codec in `encode.c` keeps binary encoding formats in one place and mirrors how **base64url** was added.

## Conclusion

Base32hex is a small feature on paper, but the thread is a clear example of PostgreSQL’s preference for **orthogonal primitives**: typed storage (`uuid`), explicit binary views (`bytea`), and shared text encodings (`encode`/`decode`). If you need compact, sort-friendly UUID text, the emerging pattern is **`encode(uuid::bytea, 'base32hex')`** with optional **`rtrim(..., '=')`**, not a parallel family of UUID-only functions—unless you wrap that one-liner for your own schema’s ergonomics.
