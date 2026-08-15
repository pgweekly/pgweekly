# PostgreSQL Weekly - A Hacker's Digest

> A technical blog aggregating and analyzing discussions from [pgsql-hackers](https://www.postgresql.org/list/pgsql-hackers/), powered by Agent Skills-compatible coding agents.

[![mdBook](https://img.shields.io/badge/built%20with-mdBook-blue)](https://rust-lang.github.io/mdBook/)
[![Agent Skills](https://img.shields.io/badge/powered%20by-Agent%20Skills-purple)](https://agentskills.io/)

## 🎯 Project Overview

This project automates the process of:
1. **Fetching** PostgreSQL mailing list thread discussions
2. **Converting** HTML content to Markdown format
3. **Downloading** attachments (patches, documentation)
4. **Generating** high-quality technical blog posts with Agent Skills-compatible coding agents
5. **Publishing** organized weekly digests using mdBook

## 📝 Workflow: From Thread to Blog

### Step 1: Find a Thread

Visit [pgsql-hackers](https://www.postgresql.org/list/pgsql-hackers/) and find an interesting discussion. Copy the thread URL or ID.

Example URL:
```
https://www.postgresql.org/message-id/flat/CACJufxGn+bMNPyrMTe0-W4fLmkFVXSr-6cvFos9mGsp-5u-RXw@mail.gmail.com
```

### Step 2: Fetch Thread Data

```bash
python3 tools/fetch_data.py --thread-id "YOUR_THREAD_URL_OR_ID"
```

This command will:
- ✅ Download the thread HTML
- ✅ Convert to Markdown
- ✅ Download attachments (.patch, .txt, .no-cfbot files)
- ✅ Save everything to `data/threads/<date>/<thread-id>/`

### Step 3: Generate Blog with an Agent Skill

This project includes an **agent skill** (`.agents/skills/pgweekly-blog-generation/`) so compatible coding agents automatically know how to generate blogs. Use any of these:

**Option A: Simple (Skill-aware)**

Just say:
```
Generate a blog from this thread: [paste your thread ID/URL]
```
The agent will fetch, analyze, and generate both English and Chinese posts automatically.

**Option B: Using Quick Prompt**

1. Open `QUICK_PROMPT.txt`
2. Replace both instances of `PASTE_YOUR_THREAD_ID_HERE` with your thread ID/URL
3. Copy the entire content and paste it into your coding agent's chat

**Option C: Advanced Control**

See `BLOG_GENERATION_PROMPT.md` for detailed templates with more customization options.

### Step 4: Review and Publish

The agent will:
- Create TWO blog posts (English and Chinese) in separate directories:
  - English version: `src/en/{year}/{week}/{filename}.md`
  - Chinese version: `src/cn/{year}/{week}/{filename}.md`
- Update `src/SUMMARY.md` automatically with both versions in their respective language sections
- Use a descriptive filename based on content

Review the generated blog and make any necessary edits, then:

```bash
mdbook build
mdbook serve  # Preview locally
```

## 📁 Project Structure

```
pgweekly/
├── README.md                          # This file
├── .agents/
│   └── skills/                        # Shared Agent Skills
│       ├── pgweekly-blog-generation/  # Thread-to-blog workflow
│       └── publish-wechat-draft/      # Markdown-to-WeChat draft workflow
├── QUICK_PROMPT.template              # Template for quick blog generation
├── QUICK_PROMPT.txt                   # Your personal prompt (gitignored)
├── BLOG_GENERATION_PROMPT.md          # Detailed prompt templates and docs
├── book.toml                          # mdBook configuration
├── src/                               # Blog content (Markdown)
│   ├── SUMMARY.md                     # Table of contents
│   ├── en/                            # English blog posts
│   │   └── {year}/                    # Organized by year
│   │       └── {week}/                # Organized by ISO week number
│   │           └── *.md               # Individual blog posts
│   └── cn/                            # Chinese blog posts (中文)
│       └── {year}/                    # Organized by year
│           └── {week}/                # Organized by ISO week number
│               └── *.md               # Individual blog posts
├── book/                              # Generated static site (gitignored)
├── data/                              # Downloaded threads (gitignored)
│   └── threads/
│       └── {date}/
│           └── {thread-id}/
│               ├── thread.html        # Original HTML
│               ├── thread.md          # Converted Markdown
│               ├── metadata.txt       # Thread info
│               ├── attachments.txt    # Message-grouped attachment index
│               └── attachments/       # Patches grouped by source email/time and version
└── tools/                             # Automation scripts
    ├── README.md                      # Tools documentation
    └── fetch_data.py                  # Thread downloader
```

## 🛠️ Tools

### `tools/fetch_data.py`

Downloads and processes PostgreSQL mailing list threads.

**Usage:**
```bash
# From URL
python3 tools/fetch_data.py --thread-id "https://www.postgresql.org/..."

# From thread ID only
python3 tools/fetch_data.py --thread-id "CACJufx..."

# From local HTML file
python3 tools/fetch_data.py --input "path/to/thread.html"

# Custom output directory
python3 tools/fetch_data.py --thread-id "..." --output-dir "my-threads"
```

**Output:**
- `data/threads/{date}/{thread-id}/thread.html` - Original HTML
- `data/threads/{date}/{thread-id}/thread.md` - Converted Markdown
- `data/threads/{date}/{thread-id}/metadata.txt` - Thread metadata
- `data/threads/{date}/{thread-id}/attachments/{message-time}_{version}/` - Downloaded patches/files from one source email
- `data/threads/{date}/{thread-id}/attachments.txt` - Message-grouped attachment index

See [tools/README.md](tools/README.md) for more details.

## 🤖 Agent Skills

Compatible coding agents discover the repository's reusable workflows under `.agents/skills/`:

- **`pgweekly-blog-generation`** analyzes mailing list discussions, compares patch versions, generates English and Chinese posts, and updates navigation.
- **`publish-wechat-draft`** renders Markdown as WeChat-compatible HTML and creates a reviewable Official Account draft after explicit approval; it never publishes the article.

### Prompt Templates

1. **QUICK_PROMPT.template** - Copy to `QUICK_PROMPT.txt` for daily use
2. **BLOG_GENERATION_PROMPT.md** - Comprehensive documentation with multiple templates

### Best Practices

- ✅ Let the agent determine the year/week automatically
- ✅ Review generated blogs for technical accuracy
- ✅ Use diff to understand patch evolution
- ✅ Focus on clarity and developer/DBA value
- ✅ Link to original discussions and documentation

## 📚 Content Organization

Blogs are organized by:
- **Year**: ISO year (e.g., 2026)
- **Week**: ISO week number (e.g., 03 for week 3)
- **Filename**: Descriptive, kebab-case (e.g., `pg-get-role-ddl-functions.md`)

Example path: `src/2026/03/pg-get-role-ddl-functions.md`

The `src/SUMMARY.md` file maintains the navigation structure for mdBook.

## 🔄 Typical Workflow

```bash
# 1. Copy the thread URL from postgresql.org
# Example: https://www.postgresql.org/message-id/flat/CACJufx...

# 2. Fetch the thread data
python3 tools/fetch_data.py --thread-id "YOUR_URL_HERE"

# 3. Open QUICK_PROMPT.txt, replace the thread ID (2 places)

# 4. Ask an Agent Skills-compatible coding agent to generate the blog

# 5. Wait for the agent to:
#    - Fetch data
#    - Analyze content
#    - Compare patches
#    - Generate blog
#    - Save and update SUMMARY.md

# 6. Review the generated blogs:
#    - English: src/en/{year}/{week}/{filename}.md
#    - Chinese: src/cn/{year}/{week}/{filename}.md

# 7. Build and preview
mdbook serve

# 8. Commit and push (only blog content, not data/)
git add src/
git commit -m "Add blog: [topic]"
git push
```

## 📄 License

See [LICENSE](LICENSE) file for details.
