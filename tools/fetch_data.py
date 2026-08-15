from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from email.utils import parsedate_to_datetime
import urllib.request
import urllib.parse
from html.parser import HTMLParser

try:
    import html2text
    HAS_HTML2TEXT = True
except ImportError:
    HAS_HTML2TEXT = False


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
THREAD_URL = "https://www.postgresql.org/message-id/flat/{thread_id}"
ALLOWED_ATTACHMENT_EXTS = {".patch", ".txt", ".no-cfbot"}


@dataclass
class AttachmentGroup:
    """Attachments posted together in one mailing-list message."""

    message_date: str = ""
    message_id: str = ""
    subject: str = ""
    urls: list[str] = field(default_factory=list)


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


def extract_thread_date(html: str) -> str | None:
    """Extract the first/original message date from thread HTML for year/week determination.
    Returns YYYY-MM-DD or None if not found.
    """
    # RFC 2822 style: "Mon, 20 Jan 2026 12:00:00 +0000" or "Date: Mon, 20 Jan 2026..."
    rfc2822 = re.findall(
        r'(?:Date:\s*)?([A-Za-z]{3},\s*\d{1,2}\s+[A-Za-z]{3}\s+\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?\s*[+-]\d{4})',
        html
    )
    for s in rfc2822:
        try:
            dt = parsedate_to_datetime(s.strip())
            return dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            continue
    # "On Mon, Jan 20, 2026 at 12:00 PM" style
    on_wrote = re.findall(
        r'On\s+([A-Za-z]{3}),\s*([A-Za-z]{3})\s+(\d{1,2}),?\s+(\d{4})',
        html
    )
    if on_wrote:
        try:
            # Use first (original) message date
            _, month_str, day, year = on_wrote[0]
            dt = datetime.strptime(f"{month_str} {day} {year}", "%b %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


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
    """Parse attachment links together with their containing message headers."""

    def __init__(self):
        super().__init__()
        self.groups: list[AttachmentGroup] = []
        self._current_headers: dict[str, str] = {}
        self._building_headers: dict[str, str] = {}
        self._in_message_header = False
        self._in_attachment_section = False
        self._cell_tag = ""
        self._cell_text: list[str] = []
        self._header_name = ""
        self._active_group: AttachmentGroup | None = None

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)
        classes = set(attrs_dict.get("class", "").split())

        if tag == "table" and "message-header" in classes:
            self._in_message_header = True
            self._building_headers = {}
            return

        if tag == "table" and "message-attachments" in classes:
            self._in_attachment_section = True
            self._active_group = AttachmentGroup(
                message_date=self._current_headers.get("date", ""),
                message_id=self._current_headers.get("message-id", ""),
                subject=self._current_headers.get("subject", ""),
            )
            return

        if self._in_message_header and tag in {"th", "td"}:
            self._cell_tag = tag
            self._cell_text = []
            return

        if tag == "a":
            href = attrs_dict.get("href", "")
            if self._in_attachment_section and self._active_group and is_attachment_url(href):
                if href.startswith("/"):
                    href = f"https://www.postgresql.org{href}"
                if href not in self._active_group.urls:
                    self._active_group.urls.append(href)

    def handle_data(self, data):
        if self._in_message_header and self._cell_tag:
            self._cell_text.append(data)

    def handle_endtag(self, tag):
        if self._in_message_header and tag in {"th", "td"} and tag == self._cell_tag:
            value = " ".join("".join(self._cell_text).split())
            if tag == "th":
                self._header_name = value.rstrip(":").lower()
            elif self._header_name:
                self._building_headers[self._header_name] = value
            self._cell_tag = ""
            self._cell_text = []
            return

        if tag == "table" and self._in_message_header:
            self._in_message_header = False
            if self._building_headers:
                self._current_headers = self._building_headers
            return

        if tag == "table" and self._in_attachment_section:
            self._in_attachment_section = False
            if self._active_group and self._active_group.urls:
                self.groups.append(self._active_group)
            self._active_group = None


def is_attachment_url(url: str) -> bool:
    path = urllib.parse.urlsplit(url).path.lower()
    return any(path.endswith(ext) or f"{ext}/" in path for ext in ALLOWED_ATTACHMENT_EXTS)


def attachment_filename(url: str) -> str:
    """Return a safe local filename for an attachment URL."""
    filename = urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
    if not filename:
        filename = "attachment"
    return re.sub(r'[^\w\-_\.]', '_', filename)


def infer_patch_version(group: AttachmentGroup) -> str:
    """Infer one explicit version label for a message's attachment set."""
    versions: set[int] = set()
    for url in group.urls:
        filename = attachment_filename(url)
        for pattern in (r"^v(\d+)-", r"-v(\d+)(?=\.[^.]+$)"):
            match = re.search(pattern, filename, re.IGNORECASE)
            if match:
                versions.add(int(match.group(1)))
                break

    if not versions:
        match = re.search(r"\bv(\d+)\b", group.subject, re.IGNORECASE)
        if match:
            versions.add(int(match.group(1)))

    if len(versions) == 1:
        return f"v{next(iter(versions))}"
    if versions:
        return "mixed-" + "-".join(f"v{version}" for version in sorted(versions))
    return "unversioned"


def attachment_group_dirname(group: AttachmentGroup) -> str:
    """Build a sortable directory name that states message time and patch version."""
    match = re.match(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}):(\d{2}):(\d{2})", group.message_date)
    if match:
        date, hour, minute, second = match.groups()
        timestamp = f"{date}_{hour}-{minute}-{second}"
    else:
        timestamp = "unknown-date"
    return f"{timestamp}_{infer_patch_version(group)}"


def extract_attachment_groups(html: str) -> list[AttachmentGroup]:
    """Extract attachments grouped by the mailing-list message that contains them."""
    parser = AttachmentParser()
    parser.feed(html)

    merged_groups: list[AttachmentGroup] = []
    groups_by_message_id: dict[str, AttachmentGroup] = {}
    for group in parser.groups:
        if group.message_id and group.message_id in groups_by_message_id:
            groups_by_message_id[group.message_id].urls.extend(group.urls)
        else:
            merged_groups.append(group)
            if group.message_id:
                groups_by_message_id[group.message_id] = group

    seen: set[str] = set()
    groups: list[AttachmentGroup] = []
    for group in merged_groups:
        unique_urls = []
        for url in group.urls:
            if url not in seen:
                seen.add(url)
                unique_urls.append(url)
        group.urls = unique_urls
        if group.urls:
            groups.append(group)

    # Detect unexpected page markup instead of silently flattening unrelated emails.
    pattern = r'href="([^"]+\.(?:patch|txt|no-cfbot)[^"]*)"'
    unmatched = []
    for url in re.findall(pattern, html, re.IGNORECASE):
        if url.startswith("/"):
            url = f"https://www.postgresql.org{url}"
        if url not in seen:
            seen.add(url)
            unmatched.append(url)
    if unmatched:
        raise RuntimeError(
            f"Could not associate {len(unmatched)} attachment(s) with a source email; "
            "refusing to flatten them"
        )

    dirnames = [attachment_group_dirname(group) for group in groups]
    if len(dirnames) != len(set(dirnames)):
        raise RuntimeError(
            "Multiple source emails resolve to the same attachment directory name; "
            "refusing to mix them"
        )

    return groups


def extract_attachments(html: str) -> list[str]:
    """Extract attachment URLs from HTML."""
    return [url for group in extract_attachment_groups(html) for url in group.urls]


def download_attachment(url: str, output_dir: Path) -> Path | None:
    """Download an attachment file."""
    try:
        filename = attachment_filename(url)
        output_dir.mkdir(parents=True, exist_ok=True)
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


def organize_existing_attachments(thread_dir: Path, groups: list[AttachmentGroup]) -> list[str]:
    """Move legacy flat attachment files into their message/patchset directories."""
    attachments_dir = thread_dir / "attachments"
    moved: list[str] = []
    if not attachments_dir.is_dir():
        return moved

    targets_by_name: dict[str, list[Path]] = {}
    for group in groups:
        group_dir = attachments_dir / attachment_group_dirname(group)
        for url in group.urls:
            targets_by_name.setdefault(attachment_filename(url), []).append(group_dir)

    for source in attachments_dir.iterdir():
        if not source.is_file():
            continue
        target_dirs = targets_by_name.get(source.name, [])
        if len(target_dirs) != 1:
            print(f"  Warning: Cannot safely place legacy attachment: {source.name}")
            continue
        target_dir = target_dirs[0]
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source.name
        if target.exists():
            print(f"  Warning: Target already exists, leaving legacy file: {source.name}")
            continue
        source.rename(target)
        moved.append(str(target.relative_to(thread_dir)))
    return moved


def write_attachment_index(
    thread_dir: Path,
    title: str,
    thread_id: str,
    groups: list[AttachmentGroup],
) -> None:
    """Write a message-grouped index, including explicit missing-file markers."""
    lines = [
        f"# Attachments for: {title}",
        f"# Thread ID: {thread_id}",
        f"# Updated: {datetime.now().isoformat()}",
        "",
    ]
    for group in groups:
        dirname = attachment_group_dirname(group)
        lines.extend([
            f"## {dirname}",
            f"# Message date: {group.message_date or 'unknown'}",
            f"# Message-ID: {group.message_id or 'unknown'}",
            f"# Version: {infer_patch_version(group)}",
        ])
        for url in group.urls:
            relative_path = Path("attachments") / dirname / attachment_filename(url)
            marker = "" if (thread_dir / relative_path).exists() else "MISSING: "
            lines.append(f"- {marker}{relative_path}")
        lines.append("")

    (thread_dir / "attachments.txt").write_text("\n".join(lines), encoding="utf-8")


def download_missing_attachments(
    thread_dir: Path,
    groups: list[AttachmentGroup] | None = None,
) -> list[str]:
    """Read thread.html in thread_dir, extract attachment URLs, download any not already in attachments/."""
    html_path = thread_dir / "thread.html"
    if not html_path.exists():
        print(f"  ✗ No thread.html in {thread_dir}")
        return []
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    if groups is None:
        groups = extract_attachment_groups(html)
    attachments_dir = thread_dir / "attachments"
    attachments_dir.mkdir(exist_ok=True)
    organize_existing_attachments(thread_dir, groups)
    downloaded = []
    attachment_count = sum(len(group.urls) for group in groups)
    current = 0
    for group in groups:
        group_dir = attachments_dir / attachment_group_dirname(group)
        for url in group.urls:
            current += 1
            filename = attachment_filename(url)
            output_path = group_dir / filename
            if output_path.exists():
                continue
            print(f"  [{current}/{attachment_count}] Downloading: {filename[:60]}")
            result = download_attachment(url, group_dir)
            if result:
                relative_path = str(result.relative_to(thread_dir))
                downloaded.append(relative_path)
                print(f"      ✓ Saved: {relative_path}")
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
        html_path = thread_dir / "thread.html"
        if not html_path.is_file():
            print(f"✗ No thread.html in {thread_dir}")
            return
        html = html_path.read_text(encoding="utf-8", errors="ignore")
        try:
            attachment_groups = extract_attachment_groups(html)
        except RuntimeError as e:
            print(f"✗ Attachment grouping failed: {e}")
            return
        print(f"📧 Downloading missing attachments for: {thread_dir.name}")
        downloaded = download_missing_attachments(thread_dir, attachment_groups)
        write_attachment_index(
            thread_dir,
            extract_title(html),
            thread_dir.name,
            attachment_groups,
        )
        print(f"\n✅ Done. Downloaded {len(downloaded)} missing attachment(s).")
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
    try:
        attachment_groups = extract_attachment_groups(html)
    except RuntimeError as e:
        print(f"  ✗ Attachment grouping failed: {e}")
        return
    attachments = [url for group in attachment_groups for url in group.urls]

    if attachments:
        print(f"  Found {len(attachments)} attachment(s)")
        attachments_dir = thread_dir / "attachments"
        attachments_dir.mkdir(exist_ok=True)

        downloaded = []
        current = 0
        for group in attachment_groups:
            group_dir = attachments_dir / attachment_group_dirname(group)
            for url in group.urls:
                current += 1
                filename = attachment_filename(url)
                print(f"  [{current}/{len(attachments)}] Downloading: {filename[:50]}")
                result = download_attachment(url, group_dir)
                if result:
                    relative_path = str(result.relative_to(thread_dir))
                    downloaded.append(relative_path)
                    print(f"    ✓ Saved: {relative_path}")

        write_attachment_index(thread_dir, title, thread_id, attachment_groups)
        print("  ✓ Attachment index: attachments.txt")
    else:
        print("  No attachments found")

    # Step 6: Create metadata file
    thread_date_str = extract_thread_date(html)
    iso_year, iso_week = "", ""
    if thread_date_str:
        try:
            dt = datetime.strptime(thread_date_str, "%Y-%m-%d")
            iso_year = str(dt.isocalendar()[0])
            iso_week = f"{dt.isocalendar()[1]:02d}"
        except ValueError:
            pass

    metadata_lines = [
        f"Thread ID: {thread_id}",
        f"Title: {title}",
        f"Downloaded: {datetime.now().isoformat()}",
        f"HTML Size: {len(html)} bytes",
        f"Markdown Size: {len(markdown_content)} chars",
        f"Attachments: {len(attachments) if attachments else 0}",
    ]
    if thread_date_str:
        metadata_lines.insert(2, f"Thread date: {thread_date_str}")
        if iso_year and iso_week:
            metadata_lines.insert(3, f"ISO year: {iso_year}, ISO week: {iso_week}")

    metadata_path = thread_dir / "metadata.txt"
    metadata_path.write_text("\n".join(metadata_lines), encoding="utf-8")

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
