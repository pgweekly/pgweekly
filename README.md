# PostgreSQL Weekly - A Hacker's Digest

> A technical blog aggregating and analyzing discussions from PostgreSQL mailing lists, powered by Cursor Agent.

[![mdBook](https://img.shields.io/badge/built%20with-mdBook-blue)](https://rust-lang.github.io/mdBook/)
[![Cursor AI](https://img.shields.io/badge/powered%20by-Cursor%20AI-purple)](https://cursor.sh/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Mailing%20Lists-336791)](https://www.postgresql.org/list/)

## 🎯 Project Overview

This project automates the process of:
1. **Fetching** PostgreSQL mailing list thread discussions
2. **Converting** HTML content to Markdown format
3. **Downloading** attachments (patches, documentation)
4. **Generating** high-quality technical blog posts using AI (Cursor Agent)
5. **Publishing** organized weekly digests using mdBook

## 🚀 Quick Start

### Prerequisites

- Python 3.7+
- mdBook (for building the site)
- Optional: `html2text` for better Markdown conversion
  ```bash
  pip install html2text
  ```

### Setup

1. **Clone the repository**
   ```bash
   git clone <your-repo-url>
   cd pgweekly
   ```

2. **Create your prompt file** (first time only)
   ```bash
   cp QUICK_PROMPT.template QUICK_PROMPT.txt
   ```
   > **Important:**
   > - `QUICK_PROMPT.template` is the version-controlled template
   > - `QUICK_PROMPT.txt` is your personal working copy (gitignored)
   > - Always copy from the template when starting fresh
   > - You can customize `QUICK_PROMPT.txt` without affecting the repository

3. **Build the book** (optional, to view existing content)
   ```bash
   mdbook build
   mdbook serve  # View at http://localhost:3000
   ```

## 📝 Workflow: From Thread to Blog

### Step 1: Find a Thread

Visit [PostgreSQL Mailing Lists](https://www.postgresql.org/list/) and find an interesting discussion. Copy the thread URL or ID.

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

### Step 3: Generate Blog with Cursor Agent

This project includes an **agent skill** (`.cursor/skills/pgweekly-blog-generation/`) so Cursor automatically knows how to generate blogs. Use any of these:

**Option A: Simple (Skill-aware)**

Just say:
```
Generate a blog from this thread: [paste your thread ID/URL]
```
The agent will fetch, analyze, and generate both English and Chinese posts automatically.

**Option B: Using Quick Prompt**

1. Open `QUICK_PROMPT.txt`
2. Replace both instances of `PASTE_YOUR_THREAD_ID_HERE` with your thread ID/URL
3. Copy the entire content and paste into Cursor Agent chat

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
├── QUICK_PROMPT.template              # Template for quick blog generation
├── QUICK_PROMPT.txt                   # Your personal prompt (gitignored)
├── BLOG_GENERATION_PROMPT.md          # Detailed prompt templates and docs
├── book.toml                          # mdBook configuration
├── src/                               # Blog content (Markdown)
│   ├── README.md                      # Landing page with language selection
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
│               └── attachments/       # Patches and files
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
- `data/threads/{date}/{thread-id}/attachments/` - Downloaded patches/files
- `data/threads/{date}/{thread-id}/attachments.txt` - Attachment list

See [tools/README.md](tools/README.md) for more details.

## 🤖 Using Cursor Agent

Cursor Agent acts as a PostgreSQL expert to:
- Analyze mailing list discussions
- Compare patch versions using diff
- Identify key technical points
- Generate well-structured blog posts
- Organize content by date

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

# 4. Copy the entire prompt and paste to Cursor Agent

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

## 📖 Advanced Topics

### Batch Processing

Process multiple threads at once:

```bash
# Fetch multiple threads
for thread_id in "id1" "id2" "id3"; do
    python3 tools/fetch_data.py --thread-id "$thread_id"
done
```

Then use the batch processing prompt in `BLOG_GENERATION_PROMPT.md`.

### Custom Prompts

Customize `QUICK_PROMPT.txt` for your needs:
- Adjust writing style (formal, conversational)
- Focus on specific aspects (performance, security)
- Target specific audiences (DBAs, developers, beginners)
- Change blog length or depth

### Comparing Patches

When multiple patch versions exist (v1, v2, v3), the agent will automatically:
```bash
diff -u attachments/v1-*.patch attachments/v2-*.patch
```

This helps explain how the solution evolved based on community feedback.

## 🎓 Learning Resources

- [PostgreSQL Mailing Lists](https://www.postgresql.org/list/)
- [mdBook Documentation](https://rust-lang.github.io/mdBook/)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)

## 🤝 Contributing

1. Fork the repository
2. Create your feature branch
3. Follow the existing content structure
4. Submit a pull request

## 📄 License

See [LICENSE](LICENSE) file for details.

## 🙏 Acknowledgments

- PostgreSQL community for the valuable mailing list discussions
- Cursor AI for powering the blog generation
- mdBook for the excellent static site generator

---

**Pro Tip:** Keep `QUICK_PROMPT.txt` customized for your workflow, but always refer back to `QUICK_PROMPT.template` for the latest structure and improvements.
