---
name: pgweekly-blog-generation
description: Generates technical blog posts from PostgreSQL mailing list threads for the pgweekly digest. Use when the user wants to write a blog post from a thread, generate a blog, convert a mailing list discussion to a post, or mentions thread ID/URL with blog/blog post intent.
---

# PostgreSQL Weekly Blog Generation

Generates English and Chinese technical blog posts from PostgreSQL mailing list discussions. Applied when the user provides a thread ID/URL and wants a blog post.

## Quick Workflow

1. **Fetch** thread data (required; do not skip): run the fetch script so that the thread HTML, Markdown, and **all patch attachments** are downloaded and saved under `data/threads/`:
   ```bash
   python3 tools/fetch_data.py --thread-id "{THREAD_ID_OR_URL}"
   ```
   This creates `data/threads/YYYY-MM-DD/<sanitized-thread-id>/` and downloads every `.patch` (and other allowed attachments) into `data/threads/YYYY-MM-DD/<sanitized-thread-id>/attachments/`. Always run this step before writing the blog.

2. **Locate** fetched content in `data/threads/YYYY-MM-DD/<thread-id>/`:
   - `thread.html` - Original HTML
   - `thread.md` - Converted Markdown
   - `metadata.txt` - Thread info (use for year/week)
   - `attachments/` - **Downloaded patches** (e.g. `.patch` files from the mailing list)
   - `attachments.txt` - List of downloaded attachment filenames

3. **Verify** all patch set versions are downloaded (required before analyze):
   - Read `thread.md` and `thread.html` to identify all patch versions referenced in the thread (e.g. v1, v2, v3, v4, v5…; also patterns like `0001-`, `0002-` in patch series)
   - List files in `attachments/` and compare: every referenced version must have a corresponding downloaded file
   - If any referenced version is missing:
     - Run `python3 tools/fetch_data.py --thread-dir "data/threads/YYYY-MM-DD/<thread-id>"` to retry downloading missing attachments
     - If still missing, do not proceed with analysis; report the missing versions and ask the user to verify the thread or manually add the patches
   - Only proceed to analyze/generate once all referenced patch versions are present in `attachments/`

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

7. **Update** SUMMARY.md and year READMEs:
   - Add entries under both `# 🇬🇧 English` and `# 🇨🇳 中文`
   - Follow existing hierarchy: year → week → link to article
   - **Put the new week/article at the top** (newest first): insert the new week immediately after the year line, so the latest week appears first in the list.
   - Create `src/en/{year}/{week}/README.md` and `src/cn/{year}/{week}/README.md` if missing
   - In `src/en/{year}/README.md` and `src/cn/{year}/README.md`, also add the new week at the **top** of the Weeks list (newest first).

## Year/Week

Determine from `metadata.txt` (thread date) or use current date. Use ISO week number (e.g. 06 for week 6).

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
