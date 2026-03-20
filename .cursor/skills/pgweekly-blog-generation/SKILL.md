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
   - `attachments/` - **Downloaded patches** (e.g. `.patch` files from the mailing list)
   - `attachments.txt` - List of downloaded attachment filenames

3. **Verify** all patch set versions are downloaded — **MANDATORY GATE; do not skip**:
   - Read `thread.md` and `thread.html` to identify **all** patch versions referenced (v1, v2, v3…; or `0001-`, `0002-` in patch series)
   - Run `ls data/threads/YYYY-MM-DD/<thread-id>/attachments/` and compare with the list of referenced versions
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
- **Patches:** The fetch script downloads every attachment (e.g. `.patch`, `.txt`) from the thread into `data/threads/YYYY-MM-DD/<sanitized-thread-id>/attachments/`. Use these files when analyzing patch evolution or citing code changes.

Do not generate a blog from a thread without first running `fetch_data.py` so that the thread and its patches are present under `data/threads/`.

## Minimal Trigger

User says: "Generate a blog from this thread: [URL/ID]" → run workflow above.

For advanced options, batch processing, or custom structure, see [BLOG_GENERATION_PROMPT.md](../../BLOG_GENERATION_PROMPT.md).
