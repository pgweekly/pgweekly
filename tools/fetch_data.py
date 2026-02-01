from __future__ import annotations

import argparse
import os
import re
from datetime import datetime
from pathlib import Path
import urllib.request
from html.parser import HTMLParser

try:
    import html2text
    HAS_HTML2TEXT = True
except ImportError:
    HAS_HTML2TEXT = False


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
THREAD_URL = "https://www.postgresql.org/message-id/flat/{thread_id}"
ALLOWED_ATTACHMENT_EXTS = {".patch", ".txt", ".no-cfbot"}


def extract_thread_id_from_url(url: str) -> str:
    """Extract thread_id from URL (content after last slash)."""
    if url.startswith("http://") or url.startswith("https://"):
        # Extract content after the last slash
        return url.rstrip('/').split('/')[-1]
    return url


def to_url(thread_id: str) -> str:
    if thread_id.startswith("http://") or thread_id.startswith("https://"):
        thread_id = extract_thread_id_from_url(thread_id)
    return THREAD_URL.format(thread_id=thread_id)


def fetch_thread_html(thread_id: str) -> str:
    url = to_url(thread_id)
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Failed to fetch thread. Status code: {response.status}")
        return response.read().decode("utf-8")


def extract_title(html: str) -> str:
    """Extract title from HTML."""
    match = re.search(r"<title>([^<]+)</title>", html, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return "PostgreSQL Thread Summary"


def html_to_markdown(html: str) -> str:
    """Convert HTML to Markdown using html2text if available."""
    if HAS_HTML2TEXT:
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0
        return h.handle(html)
    else:
        # Fallback: simple text extraction
        text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text)
        return text.strip()


class AttachmentParser(HTMLParser):
    """Parse HTML to find attachment links."""
    def __init__(self):
        super().__init__()
        self.attachments = []
        self._in_attachment_section = False

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_dict = dict(attrs)
            href = attrs_dict.get("href", "")
            # Look for attachment links (typically have /message-id/ in path)
            if href and ("/message-id/" in href or href.startswith("/")):
                # Check if it's an attachment file
                for ext in ALLOWED_ATTACHMENT_EXTS:
                    if href.endswith(ext) or f"{ext}/" in href or href.endswith(ext.replace(".", "")):
                        # Make absolute URL if needed
                        if href.startswith("/"):
                            href = f"https://www.postgresql.org{href}"
                        self.attachments.append(href)
                        break


def extract_attachments(html: str) -> list[str]:
    """Extract attachment URLs from HTML."""
    parser = AttachmentParser()
    parser.feed(html)

    # Also use regex to find attachment links in the HTML
    # Pattern: links ending with .patch, .txt, or containing attachment indicators
    pattern = r'href="([^"]+\.(?:patch|txt|no-cfbot)[^"]*)"'
    regex_matches = re.findall(pattern, html, re.IGNORECASE)

    all_attachments = parser.attachments + regex_matches

    # Deduplicate and normalize URLs
    unique_attachments = []
    seen = set()
    for url in all_attachments:
        if url.startswith("/"):
            url = f"https://www.postgresql.org{url}"
        if url not in seen:
            seen.add(url)
            unique_attachments.append(url)

    return unique_attachments


def download_attachment(url: str, output_dir: Path) -> Path | None:
    """Download an attachment file."""
    try:
        # Extract filename from URL
        filename = url.split("/")[-1]
        if not filename or filename == "":
            filename = "attachment"

        # Sanitize filename
        filename = re.sub(r'[^\w\-_\.]', '_', filename)

        output_path = output_dir / filename

        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as response:
            if response.status == 200:
                content = response.read()
                output_path.write_bytes(content)
                return output_path
    except Exception as e:
        print(f"  Warning: Failed to download {url}: {e}")
        return None

    return None


def sanitize_thread_id(thread_id: str) -> str:
    """Sanitize thread_id to create a safe directory name."""
    # Remove URL encoding and special characters
    thread_id = urllib.request.unquote(thread_id)
    # Replace special characters with underscores
    thread_id = re.sub(r'[<>:"/\\|?*@]', '_', thread_id)
    # Replace multiple underscores with single one
    thread_id = re.sub(r'_+', '_', thread_id)
    # Remove leading/trailing underscores
    thread_id = thread_id.strip('_')
    # Limit length
    if len(thread_id) > 100:
        thread_id = thread_id[:100]
    return thread_id


def download_missing_attachments(thread_dir: Path) -> list[str]:
    """Read thread.html in thread_dir, extract attachment URLs, download any not already in attachments/."""
    html_path = thread_dir / "thread.html"
    if not html_path.exists():
        print(f"  ✗ No thread.html in {thread_dir}")
        return []
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    attachments = extract_attachments(html)
    attachments_dir = thread_dir / "attachments"
    attachments_dir.mkdir(exist_ok=True)
    existing = set(attachments_dir.iterdir()) if attachments_dir.exists() else set()
    existing_names = {p.name for p in existing}
    downloaded = []
    for i, url in enumerate(attachments, 1):
        filename = url.split("/")[-1].split("?")[0]
        if not filename:
            continue
        if filename in existing_names:
            continue
        print(f"  [{i}] Downloading: {filename[:60]}")
        result = download_attachment(url, attachments_dir)
        if result:
            downloaded.append(result.name)
            existing_names.add(result.name)
            print(f"      ✓ Saved: {result.name}")
    return downloaded


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download PostgreSQL mailing list threads and convert to Markdown with attachments."
    )
    parser.add_argument("--thread-id", help="Thread ID or full URL to fetch.")
    parser.add_argument("--input", help="Path to local HTML file (alternative to --thread-id).")
    parser.add_argument("--thread-dir", help="Path to existing thread directory; only download missing attachments from thread.html.")
    parser.add_argument("--output-dir", default="data/threads",
                        help="Base output directory for threads (default: data/threads).")
    args = parser.parse_args()

    if args.thread_dir:
        thread_dir = Path(args.thread_dir)
        if not thread_dir.is_dir():
            print(f"✗ Not a directory: {thread_dir}")
            return
        print(f"📧 Downloading missing attachments for: {thread_dir.name}")
        downloaded = download_missing_attachments(thread_dir)
        print(f"\n✅ Done. Downloaded {len(downloaded)} missing attachment(s).")
        if downloaded:
            index_path = thread_dir / "attachments.txt"
            existing_index = index_path.read_text(encoding="utf-8", errors="ignore") if index_path.exists() else ""
            new_lines = "\n".join(f"- {name}" for name in downloaded)
            if new_lines and "- " + downloaded[0] not in existing_index:
                index_path.write_text(existing_index.rstrip() + "\n" + new_lines + "\n", encoding="utf-8")
        return

    if not args.thread_id and not args.input:
        parser.error("Either --thread-id, --input, or --thread-dir is required")

    # Determine thread_id and HTML source
    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            print(f"✗ Error: Input file not found: {input_path}")
            return
        thread_id = input_path.stem  # Use filename as thread_id
        print(f"📧 Processing local file: {input_path.name}")
        html = input_path.read_text(encoding="utf-8", errors="ignore")
        print(f"  ✓ Loaded {len(html)} bytes")
    else:
        thread_id_or_url = args.thread_id
        print(f"📧 Processing thread: {thread_id_or_url[:80]}...")

        # Step 1: Fetch HTML
        print("\n[1/4] Fetching thread HTML...")
        try:
            html = fetch_thread_html(thread_id_or_url)
            print(f"  ✓ Downloaded {len(html)} bytes")
        except Exception as e:
            print(f"  ✗ Failed to fetch thread: {e}")
            return

        # Extract actual thread_id from URL if needed
        thread_id = extract_thread_id_from_url(thread_id_or_url)

    title = extract_title(html)
    print(f"  Thread title: {title}")

    # Step 2: Create thread directory
    safe_thread_id = sanitize_thread_id(thread_id)
    timestamp = datetime.now().strftime("%Y-%m-%d")
    thread_dir = Path(args.output_dir) / timestamp / safe_thread_id
    thread_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n[2/4] Created directory: {thread_dir}")

    # Step 3: Save original HTML
    html_path = thread_dir / "thread.html"
    html_path.write_text(html, encoding="utf-8")
    print(f"  ✓ Saved HTML: {html_path.name}")

    # Step 4: Convert to Markdown
    print("\n[3/4] Converting to Markdown...")
    markdown_content = html_to_markdown(html)
    md_path = thread_dir / "thread.md"
    md_path.write_text(markdown_content, encoding="utf-8")
    print(f"  ✓ Saved Markdown: {md_path.name} ({len(markdown_content)} chars)")

    # Step 5: Extract and download attachments
    print("\n[4/4] Checking for attachments...")
    attachments = extract_attachments(html)

    if attachments:
        print(f"  Found {len(attachments)} attachment(s)")
        attachments_dir = thread_dir / "attachments"
        attachments_dir.mkdir(exist_ok=True)

        downloaded = []
        for i, url in enumerate(attachments, 1):
            print(f"  [{i}/{len(attachments)}] Downloading: {url.split('/')[-1][:50]}")
            result = download_attachment(url, attachments_dir)
            if result:
                downloaded.append(result.name)
                print(f"    ✓ Saved: {result.name}")

        if downloaded:
            # Create attachment index
            index_path = thread_dir / "attachments.txt"
            index_content = "\n".join([
                f"# Attachments for: {title}",
                f"# Thread ID: {thread_id}",
                f"# Downloaded: {datetime.now().isoformat()}",
                "",
                *[f"- {name}" for name in downloaded]
            ])
            index_path.write_text(index_content, encoding="utf-8")
            print(f"  ✓ Attachment index: {index_path.name}")
    else:
        print("  No attachments found")

    # Step 6: Create metadata file
    metadata_path = thread_dir / "metadata.txt"
    metadata_content = "\n".join([
        f"Thread ID: {thread_id}",
        f"Title: {title}",
        f"Downloaded: {datetime.now().isoformat()}",
        f"HTML Size: {len(html)} bytes",
        f"Markdown Size: {len(markdown_content)} chars",
        f"Attachments: {len(attachments) if attachments else 0}",
    ])
    metadata_path.write_text(metadata_content, encoding="utf-8")

    print(f"\n✅ Done! All files saved to: {thread_dir.resolve()}")
    print(f"\nContents:")
    print(f"  - thread.html      (original HTML)")
    print(f"  - thread.md        (converted Markdown)")
    print(f"  - metadata.txt     (thread information)")
    if attachments:
        print(f"  - attachments/     ({len(attachments)} files)")
        print(f"  - attachments.txt  (attachment list)")


if __name__ == "__main__":
    main()
