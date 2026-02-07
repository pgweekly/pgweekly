# COPY TO with JSON Format: Native JSON Export from PostgreSQL

## Introduction

In November 2023, **Davin Shearer** asked on the pgsql-general list how to emit JSON from PostgreSQL to a file using `COPY TO`. When he used `COPY TO` with a query that produced a single JSON column (e.g. `json_agg(row_to_json(t))`), the text format applied its own quoting rules: double quotes inside the JSON were escaped again, producing invalid JSON that tools like `jq` could not parse. The community agreed that a proper solution would be a **native JSON format for COPY TO**—so that one column of JSON (or a row rendered as one JSON object) is written as valid JSON without an extra layer of text/CSV escaping.

Base on the idea and, in a long-running thread that also references [Joe Conway’s earlier COPY/JSON discussion](https://www.postgresql.org/message-id/6a04628d-0d53-41d9-9e35-5a8dc302c34c@joeconway.com), posted a series of patches. The design that emerged is: add a **`FORMAT json`** option for `COPY TO` only, and an optional **`FORCE_ARRAY`** option to wrap the output in a JSON array. The thread has seen many revisions (v8 through v23), with feedback from **Tom Lane**, **Joe Conway**, **Alvaro Herrera**, **Joel Jacobson**, **Jian He**, **Junwang Zhao**, and others. This post summarizes the discussion, the implementation, and the current status.

## Why This Matters

- **Correct JSON export**: Today, exporting a query result as JSON from the server usually means using `COPY TO` in text or CSV format. Text format treats the result as a string and escapes quotes and backslashes, which breaks JSON. A dedicated JSON format writes each row as one JSON object (or one JSON value per column) with proper escaping, so the output is valid JSON.
- **Interoperability**: Many pipelines expect JSON (e.g. one JSON object per line, or a single JSON array). Native `COPY TO ... (FORMAT json)` and `FORCE_ARRAY` allow exporting directly from the database without client-side formatting or workarounds (e.g. `psql -t -A` or LO API).
- **Consistency with existing formats**: COPY already supports `text`, `csv`, and `binary`. Adding `json` keeps the same mental model: choose a format and get correctly encoded output.

## Technical Analysis

### Design Decisions

The patch set makes these choices:

1. **JSON format is COPY TO only.** `COPY FROM` with JSON is not supported (parsing arbitrary JSON is a larger feature). The grammar and option validation reject `FORMAT json` for `COPY FROM`.
2. **No HEADER with JSON.** The documentation and code disallow `HEADER` when using JSON format, to avoid mixing a header line with JSON lines/array.
3. **One logical column in protocol.** In JSON mode, the frontend/backend Copy protocol sends a single (non-binary) column; the row is rendered as one JSON value (e.g. one object per row).
4. **FORCE_ARRAY only with JSON.** The `FORCE_ARRAY` option wraps the entire COPY output in `[ ... ]` and inserts commas between rows, so the result is a single JSON array. It is only valid with `FORMAT json`.

### Patch Structure

- **Patch 1 (from v13) — CopyFormat refactor**
  **Joel Jacobson** introduced an enum `CopyFormat` (e.g. `COPY_FORMAT_TEXT`, `COPY_FORMAT_CSV`, `COPY_FORMAT_BINARY`) and replaced the two booleans `csv_mode` and `binary` in `CopyFormatOptions` with a single `format` field. This makes adding new formats (like JSON) cleaner. **jian he** later refactored this patch to address review feedback; **Junwang Zhao** adapted it to the new `CopyToRoutine` structure in the executor.

- **Patch 2 — JSON format for COPY TO**
  - **Grammar** (`gram.y`): Add `JSON` as a format option and allow `FORMAT json` in COPY options.
  - **Options** (`copy.c`, `copy.h`): From v13, format is represented by `CopyFormat`; JSON adds `COPY_FORMAT_JSON` and the same checks: no HEADER/default/null/delimiter with JSON, no JSON with COPY FROM.
  - **Copy protocol** (`copyto.c`): In `SendCopyBegin`, when in JSON mode, send a single column with format 0 (text) instead of per-column formats.
  - **Row output** (`copyto.c`): In `CopyOneRowTo`, when `json_mode` is set, the row is converted to JSON via `composite_to_json()` (from `utils/adt/json.c`) and the resulting string is sent. For query-based COPY (no relation), the patch ensures the slot’s tuple descriptor matches the query’s so that `composite_to_json` sees the correct attribute metadata for key names.
  - **json.c**: `composite_to_json()` is changed from `static` to exported and declared in `utils/json.h` so COPY can call it.

- **Patch 3 — FORCE_ARRAY for COPY TO**
  - **Options** (`copy.c`, `copy.h`): Add `force_array` and parse `force_array` / `force_array true|false`. Validation: FORCE_ARRAY is only allowed with JSON mode (v12+ uses `ERRCODE_INVALID_PARAMETER_VALUE` for the error).
  - **Output** (`copyto.c`): Before the first row, if JSON mode and `force_array`, send `[` and a newline; between rows, send `,` before each JSON object (using a `json_row_delim_needed` flag); after the last row, send `]` and newline. Default output (without FORCE_ARRAY) remains one JSON object per line.

### Evolution: v8 Through v23

- **v8** took a larger approach: extracting COPY TO/FROM format implementations and adding a pluggable mechanism (including a contrib module `pg_copy_json`). Reviewers preferred a smaller, in-core change.
- **v9–v10** simplified to adding only the JSON format for COPY TO (no pluggable API). v10 introduced the `json_mode` flag and the use of `composite_to_json`.
- **v11** added the **FORCE_ARRAY** option and the corresponding tests; it also fixed the error code for “COPY FROM with json” and tightened option validation.
- **v12** (August 2024): only patch 2 (FORCE_ARRAY) was resent; the error when FORCE_ARRAY is used without JSON was changed to `ERRCODE_INVALID_PARAMETER_VALUE`.
- **v13** (October 2024): **Joel Jacobson** contributed patch 0001 — introduce **CopyFormat** enum and replace `csv_mode` and `binary` in `CopyFormatOptions` with a single `format` field. Patches 0002 (json format) and 0003 (force_array) were rebased on top; the docs explicitly state that JSON cannot be used with `header`, `default`, `null`, or `delimiter`.
- **v14–v22**: Mostly **rebase and adaptation** to upstream. v14 dropped the separate CopyFormat patch in some postings (rebase on different bases). **Junwang Zhao** (v15, March 2025) adapted the JSON implementation to the new **CopyToRoutine** struct (commit 2e4127b6d2). Subsequent versions (v16–v22) continued to rebase and address review comments without changing the core design.
- **v23** (January 2026): Current series. **Three patches**: (1) CopyFormat refactor (originally Joel Jacobson; refactored by jian he), (2) json format for COPY TO (Author: Joe Conway; **Reviewed-by** multiple contributors including Andrey M. Borodin, Dean Rasheed, Daniel Verite, Andrew Dunstan, Davin Shearer, Masahiko Sawada, Alvaro Herrera), (3) FORCE_ARRAY for COPY JSON format. The feature set is unchanged; the patch set is rebased and has gathered substantial review.

### Code Highlights

**Row as JSON (patch 2)**
Each row is turned into a single JSON object via the existing `composite_to_json()`:

```c
rowdata = ExecFetchSlotHeapTupleDatum(slot);
result = makeStringInfo();
composite_to_json(rowdata, result, false);
CopySendData(cstate, result->data, result->len);
```

**FORCE_ARRAY framing (patch 3)**
Before the row loop, send `[`; for each row after the first, send `,` then the object; after the loop, send `]`:

```c
if (cstate->opts.json_mode && cstate->opts.force_array)
{
    CopySendChar(cstate, '[');
    CopySendEndOfRow(cstate);
}
// ... row loop: first row no comma, then CopySendChar(cstate, ','); then object ...
if (cstate->opts.json_mode && cstate->opts.force_array)
{
    CopySendChar(cstate, ']');
    CopySendEndOfRow(cstate);
}
```

**Example usage (from regression tests)**

```sql
COPY copytest TO STDOUT (FORMAT json);
-- One JSON object per line.

COPY copytest TO STDOUT (FORMAT json, force_array true);
-- Single JSON array: [ {"col":1,...}, {"col":2,...} ]
```

## Community Insights

### Original Problem and Workarounds

Davin’s initial issue was that COPY’s text format escaped his JSON again, breaking it. **David G. Johnston** and **Adrian Klaver** suggested using `psql` to write query results to a file instead of COPY. **Dominique Devienne** and **David G. Johnston** agreed that a “not formatted” or raw option for COPY would help when dumping a single column (e.g. JSON) as-is. **Tom Lane** concurred that a copy option for unformatted output would address the use case. That consensus led to the idea of a dedicated JSON format rather than overloading text/CSV.

### Reviewer Feedback and Refinements

- **Tom Lane** and others emphasized keeping the change minimal: add JSON output for COPY TO without a large refactor. That pushed the design from the v8-style pluggable formats to the current in-core JSON path.
- **Joe Conway** had previously discussed COPY and JSON in a separate thread; jian he’s patches reference that discussion and align with the idea of first-class JSON output for COPY TO.
- **Alvaro Herrera** and other reviewers have commented on the thread; the evolution from v8 to v9/v10 reflects their preference for a smaller, focused patch set.

### Edge Cases in the Patch

- **Query-based COPY**: When the source is a query (no relation), the tuple descriptor from the slot can differ from the query’s. The patch copies the query’s attribute metadata into the slot’s tuple descriptor so `composite_to_json` generates correct key names.
- **Protocol**: In JSON mode, the Copy protocol sends one column; the backend still produces one JSON “value” per row (one object, or one element inside the array when FORCE_ARRAY is used).

## Technical Details

### Implementation Notes

- **Escaping**: JSON string escaping is handled by `composite_to_json()` and the existing `escape_json`-style helpers in `json.c`, so quotes, backslashes, and control characters in column values are encoded correctly.
- **HEADER**: Explicitly disallowed with JSON to keep the stream either pure JSON lines or a single JSON array.
- **FORCE_ARRAY output**: The regression tests show that with `force_array`, the output is `[`, then newline, then the first object; then for each next row, `,` followed by the object; then `]`. So the result is a single valid JSON array (with optional whitespace/newlines between elements).

### Limitations

- **COPY FROM**: No JSON import; only COPY TO is extended.
- **HEADER**: Not supported in JSON mode.
- **Binary**: JSON format is text-only (no binary JSON in this patch).

## Current Status

- The thread has seen activity from **2023 through early 2026**. The latest series is **v23** (January 2026): three patches (CopyFormat refactor, json format for COPY TO, FORCE_ARRAY).
- As of the thread snapshot, the patches have **not been committed**; they remain under discussion. v23 reflects the current design and review state.
- **Design**: `COPY TO ... (FORMAT json)` and optional `(FORMAT json, force_array true)` for a single JSON array; JSON is incompatible with HEADER, DEFAULT, NULL, DELIMITER, and COPY FROM.

## Conclusion

The “Emitting JSON to file using COPY TO” thread started with a user hitting double-escaping when exporting JSON via COPY. The community agreed that a native **JSON format for COPY TO** was the right fix. **jian he** (and later **Junwang Zhao**) implemented `FORMAT json` (COPY TO only; incompatible with HEADER, DEFAULT, NULL, DELIMITER) and `FORCE_ARRAY`, reusing `composite_to_json()` and the existing JSON escaping. **Joel Jacobson**’s CopyFormat refactor (v13+) replaced the format booleans with an enum, making the codebase ready for JSON and future formats. The series has evolved through v23 with rebases and adaptations (e.g. to CopyToRoutine) and has received Reviewed-by from several committers. 

## References

- [Mailing list thread: Emitting JSON to file using COPY TO](https://www.postgresql.org/message-id/CALvfUkBxTYy5uWPFVwpk_7ii2zgT07t3d-yR_cy4sfrrLU%3Dkcg%40mail.gmail.com)
- [Joe Conway: COPY and JSON discussion](https://www.postgresql.org/message-id/6a04628d-0d53-41d9-9e35-5a8dc302c34c@joeconway.com)
- PostgreSQL documentation: [COPY](https://www.postgresql.org/docs/current/sql-copy.html), [JSON Types](https://www.postgresql.org/docs/current/datatype-json.html)
