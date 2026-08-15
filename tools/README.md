# Tools for PostgreSQL Weekly Blog Generation

This folder provides tools to download and process PostgreSQL mailing list threads.

## Features

- Fetch thread HTML from `postgresql.org`
- Convert HTML to Markdown (uses `html2text` if available)
- Download attachments (.patch, .txt, .no-cfbot files), grouped by source email and patchset version
- Organize content by thread-id and date
- **Agent Skills integration** for automated blog generation

## Quick Start

### 📥 Step 1: Download a Thread

```bash
# Using full URL (recommended)
python3 tools/fetch_data.py --thread-id "https://www.postgresql.org/message-id/flat/CACJufx..."

# Or just the thread ID
python3 tools/fetch_data.py --thread-id "CACJufxGn+bMNPyrMTe0-W4fLmkFVXSr..."
```

This will:
1. Download the thread HTML
2. Convert to Markdown
3. Download all attachments (.patch, .txt, .no-cfbot)
4. Save everything to `data/threads/<date>/<thread-id>/`

### 🤖 Step 2: Generate Blog with an Agent Skill

**⚡ Quick Method:**
1. **First time setup:** Copy the template
   ```bash
   cp QUICK_PROMPT.template QUICK_PROMPT.txt
   ```
   > Note: `QUICK_PROMPT.txt` is gitignored for your personal use

2. Open `QUICK_PROMPT.txt` in the project root
3. Replace `PASTE_YOUR_THREAD_ID_HERE` with your thread ID/URL (in 2 places)
4. Copy the entire prompt and paste it into your coding agent's chat

**📚 Detailed Method:**
See `BLOG_GENERATION_PROMPT.md` for:
- Multiple prompt templates (basic, advanced, minimal)
- Customization options
- Batch processing instructions
- Example usage and tips

**The agent will:**
- ✅ Fetch thread data automatically
- ✅ Analyze content and patches
- ✅ Compare patch versions using diff (if applicable)
- ✅ Generate technical blogs as a PostgreSQL expert
- ✅ Create TWO versions: English and Chinese (中文)
- ✅ Save to appropriate directories (auto-determined):
  - English: `src/en/{year}/{week}/{filename}.md`
  - Chinese: `src/cn/{year}/{week}/{filename}.md`
- ✅ Update `src/SUMMARY.md` with both language versions in their respective sections

### 💬 Simple Natural Language

Or ask an Agent Skills-compatible coding agent:

```
"Generate a blog from this PostgreSQL thread: [paste thread ID]"
```

## Output Structure

After running `fetch_data.py`, you'll get:

```
data/threads/
  └── 2026-01-18/
      └── CACJufxGn_bMNPyr.../
          ├── thread.html          # Original HTML
          ├── thread.md            # Converted Markdown
          ├── metadata.txt         # Thread metadata
          ├── attachments.txt      # Message-grouped attachment index
          └── attachments/         # One directory per source email/patchset
              ├── 2026-01-17_09-15-30_v1/
              │   ├── 0001-first-change-v1.patch
              │   └── 0002-second-change-v1.patch
              └── 2026-01-18_14-05-22_v2/
                  ├── 0001-first-change-v2.patch
                  └── 0002-second-change-v2.patch
```

Each attachment directory uses the source email's displayed timestamp and the
version inferred from that email's filenames or subject. Attachments from one
email stay together. A message without a version marker uses `unversioned`.

## Dependencies

Optional (recommended):
- `html2text`: For better HTML to Markdown conversion
  ```bash
  pip install html2text
  ```

## Advanced Usage

### Process Multiple Threads

```bash
for id in "thread1" "thread2" "thread3"; do
    python3 tools/fetch_data.py --thread-id "$id"
done
```

### Use Local HTML File

```bash
python3 tools/fetch_data.py --input "path/to/thread.html"
```

### Custom Output Directory

```bash
python3 tools/fetch_data.py --thread-id <id> --output-dir "my-threads"
```

## Next Steps

1. **Download threads** using `fetch_data.py`
2. **Use prompt templates** in `QUICK_PROMPT.txt` or `BLOG_GENERATION_PROMPT.md`
3. **Let a compatible coding agent generate** high-quality technical blogs
4. **Review and publish** to your weekly digest
