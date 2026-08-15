---
name: pgweekly-blog-generation
description: Generates technical blog posts from PostgreSQL mailing list threads for the pgweekly digest. Use when the user wants to write a blog post from a thread, generate a blog, convert a mailing list discussion to a post, or mentions thread ID/URL with blog/blog post intent.
---

# PostgreSQL Weekly Blog Generation

Generates English and Chinese technical blog posts from PostgreSQL mailing list discussions. Applied when the user provides a thread ID/URL and wants a blog post.

## Quick Workflow

1. **Fetch** thread data (required; do not skip):
   ```bash
   python3 tools/fetch_data.py --thread-id "{THREAD_ID_OR_URL}"
   ```
   - **Wait for the command to finish** (check exit code is 0). Do not proceed if fetch failed.
   - This creates `data/threads/YYYY-MM-DD/<sanitized-thread-id>/` and downloads attachments into `attachments/`.
   - The `YYYY-MM-DD` in the path is the **fetch date** (when you ran the script), NOT the thread date—do not use it for year/week.

2. **Locate** fetched content in `data/threads/YYYY-MM-DD/<thread-id>/`:
   - `thread.html` - Original HTML
   - `thread.md` - Converted Markdown
   - `metadata.txt` - Thread info
   - `attachments/<message-time>_<version>/` - **Downloaded patches grouped by source email and patchset**
   - `attachments.txt` - Message-grouped attachment index with source date, Message-ID, version, and relative paths

   **Keep the attachment layout strict:**
   - Put all attachments from one email in one directory, and put no attachments from another email in that directory. Treat the email as the grouping boundary even when it has only one attachment.
   - Name each directory `<YYYY-MM-DD>_<HH-MM-SS>_<version>`, using the source email's displayed timestamp and an explicit version label, for example `2024-03-29_09-33-30_v8/`.
   - Infer `vN` from that email's attachment filenames first and its subject second. Use `unversioned` when neither contains a version. Use `mixed-v1-v2` if one email genuinely contains multiple version labels.
   - Never flatten attachments from the whole thread into one directory and never group files by patch number alone.
   - Stop and report an attachment-grouping error if the source email or a unique time/version directory cannot be determined; do not guess or mix files.
   - Preserve the server-provided attachment filenames inside the group directory.

3. **Verify** all patch set versions are downloaded — **MANDATORY GATE; do not skip**:
   - Read `thread.md` and `thread.html` to identify **all** patch versions referenced (v1, v2, v3…; or `0001-`, `0002-` in patch series)
   - Read `attachments.txt`, then run `find data/threads/YYYY-MM-DD/<thread-id>/attachments -type f` and compare the message-grouped files with every referenced patchset
   - Confirm that every patchset directory name contains its source email timestamp and version, and that no directory mixes attachments from different emails
   - **If any referenced version is missing:**
     - Run `python3 tools/fetch_data.py --thread-dir "data/threads/YYYY-MM-DD/<thread-id>"` to retry
     - Re-verify; if still missing, **STOP** — report missing versions to the user and do not write the blog
   - **If the thread has no patches**, verification passes (nothing to check).
   - **CRITICAL:** Do not proceed to step 4 (Analyze) until you have explicitly confirmed: "Referenced versions: [list] ✓ All present in attachments/". Only then may you write the blog.

4. **Analyze** content:
   - If multiple patch versions (v1, v2, v3...), run `diff -u` between versions to explain evolution
   - Identify main topic, key decisions, reviewer feedback

5. **Generate** TWO blog posts with this structure:
   - Clear title (topic-based)
   - Introduction (context, why it matters)
   - Technical Analysis (key points, code/patch highlights, evolution)
   - Community Insights (reviewer feedback, issues resolved)
   - Technical Details (implementation, edge cases, performance)
   - Current Status (patch/discussion state)
   - Conclusion (summary, implications)
   - **SQL examples:** follow the [SQL examples convention](#sql-examples-convention) below (both languages).

6. **Save** both versions:
   - English: `src/en/{year}/{week}/{descriptive-filename}.md`
   - Chinese: `src/cn/{year}/{week}/{descriptive-filename}.md`
   - Filename: kebab-case from main topic (e.g. `planner-count-optimization`)

7. **Update** `src/SUMMARY.md` and year READMEs:
   - Add entries under both `# 🇬🇧 English` and `# 🇨🇳 中文`
   - Follow existing hierarchy: year → week → link to article
   - **Put the new week/article at the top** (newest first): insert the new week immediately after the year line, so the latest week appears first in the list.
   - Create `src/en/{year}/{week}/README.md` and `src/cn/{year}/{week}/README.md` if missing
   - In `src/en/{year}/README.md` and `src/cn/{year}/README.md`, also add the new week at the **top** of the Weeks list (newest first).

## Year/Week

**Use the blog writing date (the day you write the blog) as the source of truth.** This determines which week the article is filed under.

**Rules:**
- Compute ISO year and ISO week from **today's date** (the date when the blog is being written).
- Example: if writing on 2026-03-20, use year=2026, week=12 (from `datetime(2026, 3, 20).isocalendar()`).
- **Do NOT use** the thread date, `metadata.txt`, the directory name `YYYY-MM-DD` (fetch date), or "Downloaded:" for year/week.

## Writing Guidelines

| Version | Style |
|---------|-------|
| English | Professional technical writing, clear explanations |
| Chinese | Professional 中文 technical writing, natural terminology (不要直译) |
| Both | Code blocks with syntax highlighting; links to docs/references |

## SQL examples convention

**Default:** If the thread topic can reasonably be illustrated with **SQL** (new/changed functions, syntax, `EXPLAIN`, DDL, GUCs demonstrable in SQL, query patterns, regression-test-style snippets from patches), include a **dedicated subsection** with one or more examples in **both** the English and Chinese posts. Readers expect copy-pasteable snippets.

**What to include**

- Fenced blocks with `sql` syntax highlighting.
- Examples grounded in the thread or attachments (e.g. `strings.sql` / `uuid.sql` from patches, or minimal repros of discussed behavior).
- Short comments on **expected shape of results** or **version/commit requirements** when the feature is not in a released PostgreSQL yet—avoid implying examples run on every existing install without a caveat.

**When to skip**

- The discussion is purely internal (e.g. executor internals with no user-visible SQL), or SQL would be misleading or enormous. In those cases, C snippets or prose are enough; do not force SQL.

**Placement**

- Usually under **Technical Analysis** or **Technical Details** (e.g. `### SQL examples` / `### SQL 示例`); keep EN and CN structurally aligned.

**Parity**

- Chinese and English posts should carry the **same** examples (same statements; comments may be localized).

## Terminology (Chinese)

When writing the Chinese version, use these standard translations:

| English | 中文 |
|---------|------|
| planner | **优化器** (not 规划器) |

Apply consistently in titles, body text, and navigation (e.g. "查询优化器优化", "优化器可以选择...").

## SUMMARY.md Format

```markdown
# 🇬🇧 English
- [2026](./en/2026/README.md)
  - [Week 06](./en/2026/06/README.md)
    - [Article Title](./en/2026/06/article-filename.md)

# 🇨🇳 中文
- [2026](./cn/2026/README.md)
  - [第 06 周](./cn/2026/06/README.md)
    - [中文标题](./cn/2026/06/article-filename.md)
```

## Data directory (threads and patches)

All thread data, including **downloaded patches**, is stored under `data/threads/`:

- **Base path:** `data/threads/` (configurable via `--output-dir`; default is `data/threads`).
- **Per-thread directory:** `data/threads/YYYY-MM-DD/<sanitized-thread-id>/`, where `YYYY-MM-DD` is the **fetch date** (the day the script was run).
- **Patches:** The fetch script downloads every attachment (e.g. `.patch`, `.txt`) into `attachments/<source-email-time>_<version>/`. Each subdirectory is exactly one source email/patchset; use `attachments.txt` to trace its date, Message-ID, version, and files.

Do not generate a blog from a thread without first running `fetch_data.py` so that the thread and its patches are present under `data/threads/`.

## Minimal Trigger

User says: "Generate a blog from this thread: [URL/ID]" → run workflow above.

For advanced options, batch processing, or custom structure, see [BLOG_GENERATION_PROMPT.md](../../BLOG_GENERATION_PROMPT.md).
