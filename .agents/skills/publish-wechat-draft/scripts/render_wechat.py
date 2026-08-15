#!/usr/bin/env python3
"""Render Markdown to a local, inline-styled WeChat article preview."""

from __future__ import annotations

import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime, timezone
from urllib.parse import urlparse


STYLES = {
    "p": "margin:0 0 1.15em;",
    "h2": "margin:2.2em 0 .9em;padding-left:.65em;border-left:4px solid #2f6f5e;font-size:21px;line-height:1.45;color:#183d35;",
    "h3": "margin:1.8em 0 .75em;font-size:18px;line-height:1.5;color:#244c42;",
    "h4": "margin:1.5em 0 .65em;font-size:17px;line-height:1.5;",
    "strong": "color:#183d35;",
    "em": "color:#59636b;",
    "a": "color:#1f6f8b;text-decoration:none;",
    "blockquote": "margin:1.4em 0;padding:0.9em 1em;border-left:4px solid #b9cec7;background:#f4f8f6;color:#4c5b56;",
    "code": "background:#eef3f1;color:#9b3d32;font-family:monospace;",
    "pre": "margin:1.3em 0;padding:1em;overflow-x:auto;border-radius:8px;background:#18211f;color:#e8f0ed;line-height:1.6;white-space:pre-wrap;",
    "table": "width:100%;margin:1.2em 0;border-collapse:collapse;font-size:14px;line-height:1.55;",
    "thead": "background:#e8f1ee;color:#183d35;",
    "th": "padding:0.65em;border:1px solid #c9d7d2;text-align:left;font-weight:700;",
    "td": "padding:0.65em;border:1px solid #d8e1de;vertical-align:top;",
    "img": "display:block;max-width:100%;height:auto;margin:1.35em auto;",
    "hr": "height:1px;margin:2em 0;border:0;background:#dce5e2;",
}

VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "source", "wbr"}
DROP_TAGS = {"script", "style", "iframe", "object", "embed", "form"}
DEFAULT_AUTHOR = "pgweekly"
LIST_ITEM_STYLE = "margin:0 0 .65em;padding-left:1.65em;text-indent:-1.65em;"
LIST_MARKER_STYLE = "display:inline-block;min-width:1.65em;color:#2f6f5e;font-weight:700;text-indent:0;"


class InlineStyleSanitizer(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.dropped: list[str] = []

    def _is_dropped(self) -> bool:
        return bool(self.dropped)

    def _attrs(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        clean: dict[str, str] = {}
        for key, value in attrs:
            key = key.lower()
            value = value or ""
            if key.startswith("on") or key in {"srcdoc"}:
                continue
            if key in {"href", "src"} and value:
                parsed = urlparse(value)
                if parsed.scheme and parsed.scheme not in {"http", "https", "mailto"}:
                    continue
            clean[key] = value
        if tag in STYLES:
            clean["style"] = STYLES[tag] + clean.get("style", "")
        return "".join(
            f' {html.escape(key, quote=True)}="{html.escape(value, quote=True)}"'
            for key, value in clean.items()
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in DROP_TAGS:
            self.dropped.append(tag)
            return
        if self._is_dropped():
            return
        suffix = " /" if tag in VOID_TAGS else ""
        self.parts.append(f"<{tag}{self._attrs(tag, attrs)}{suffix}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.dropped:
            if tag == self.dropped[-1]:
                self.dropped.pop()
            return
        if tag not in VOID_TAGS:
            self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if not self._is_dropped():
            self.parts.append(html.escape(data, quote=False))

    def handle_comment(self, data: str) -> None:
        return

    def result(self) -> str:
        return "".join(self.parts)


class URLCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.images: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a" and values.get("href"):
            self.links.append(values["href"])
        if tag.lower() == "img" and values.get("src"):
            self.images.append(values["src"])


class WeChatListFlattener(HTMLParser):
    """Replace native HTML lists with paragraphs that survive WeChat editing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.lists: list[dict[str, int | str]] = []
        self.items: list[dict[str, bool | int]] = []
        self.list_count = 0
        self.item_count = 0

    @staticmethod
    def _serialize_attrs(attrs: list[tuple[str, str | None]]) -> str:
        return "".join(
            f' {html.escape(key, quote=True)}="{html.escape(value or "", quote=True)}"'
            for key, value in attrs
        )

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if tag in {"ul", "ol"}:
            start = 1
            if tag == "ol":
                try:
                    start = int(values.get("start", "1"))
                except ValueError:
                    start = 1
            self.lists.append({"tag": tag, "next": start})
            self.list_count += 1
            return
        if tag == "li" and self.lists:
            current = self.lists[-1]
            if current["tag"] == "ol":
                try:
                    number = int(values.get("value", str(current["next"])))
                except ValueError:
                    number = int(current["next"])
                marker = f"{number}."
                current["next"] = number + 1
            else:
                marker = "•"
            is_root = not self.items
            if is_root:
                self.parts.append(f'<p style="{LIST_ITEM_STYLE}">')
            else:
                self.parts.append("<br>")
            nested_margin = max(0, len(self.lists) - 1)
            marker_style = LIST_MARKER_STYLE
            if nested_margin:
                marker_style += f"margin-left:{nested_margin * 1.2:.1f}em;"
            self.parts.append(f'<span style="{marker_style}">{html.escape(marker)}</span>')
            self.items.append({"root": is_root, "paragraphs": 0})
            self.item_count += 1
            return
        if tag == "p" and self.items:
            item = self.items[-1]
            if int(item["paragraphs"]) > 0:
                self.parts.append("<br>")
            item["paragraphs"] = int(item["paragraphs"]) + 1
            return
        self.parts.append(f"<{tag}{self._serialize_attrs(attrs)}>")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in {"ul", "ol", "li"}:
            self.handle_starttag(tag, attrs)
            self.handle_endtag(tag)
            return
        self.parts.append(f"<{tag}{self._serialize_attrs(attrs)} />")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"ul", "ol"} and self.lists:
            self.lists.pop()
            return
        if tag == "li" and self.items:
            item = self.items.pop()
            if item["root"]:
                self.parts.append("</p>")
            return
        if tag == "p" and self.items:
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.lists and not data.strip():
            return
        self.parts.append(html.escape(data, quote=False))

    def handle_entityref(self, name: str) -> None:
        self.parts.append(f"&{name};")

    def handle_charref(self, name: str) -> None:
        self.parts.append(f"&#{name};")

    def handle_comment(self, data: str) -> None:
        return

    def result(self) -> str:
        return "".join(self.parts)


def collect_urls(fragment: str) -> URLCollector:
    collector = URLCollector()
    collector.feed(fragment)
    collector.close()
    return collector


def flatten_wechat_lists(fragment: str) -> tuple[str, dict[str, int]]:
    parser = WeChatListFlattener()
    parser.feed(fragment)
    parser.close()
    return parser.result(), {"containers": parser.list_count, "items": parser.item_count}


def is_unlocalized_remote_image(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    hostname = (parsed.hostname or "").lower()
    return hostname != "mmbiz.qpic.cn" and not hostname.endswith(".mmbiz.qpic.cn")


def parse_front_matter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    try:
        import yaml

        data = yaml.safe_load(raw) or {}
    except Exception as exc:
        raise ValueError(f"invalid YAML front matter: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("YAML front matter must be a mapping")
    return data, text[end + 5 :]


def extract_first_h1(text: str) -> tuple[str, str]:
    match = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    if not match:
        return "", text
    title = match.group(1).strip()
    body = text[: match.start()] + text[match.end() :]
    return title, body.lstrip("\n")


def plain_text(markdown_text: str) -> str:
    text = re.sub(r"```.*?```", " ", markdown_text, flags=re.S)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"!\[[^]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"[#>*_~|-]+", " ", text)
    return " ".join(text.split())


def make_digest(body: str, limit: int = 120) -> str:
    text = plain_text(body)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def markdown_to_html(text: str) -> tuple[str, str]:
    try:
        import markdown

        result = markdown.markdown(
            text,
            extensions=["extra", "sane_lists", "nl2br"],
            output_format="html5",
        )
        return result, f"python-markdown {markdown.__version__}"
    except ImportError:
        proc = subprocess.run(
            ["pandoc", "--from=gfm", "--to=html5"],
            input=text,
            text=True,
            capture_output=True,
            check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError("install Python-Markdown or Pandoc to render Markdown")
        return proc.stdout, "pandoc"


def style_fragment(fragment: str) -> str:
    parser = InlineStyleSanitizer()
    parser.feed(fragment)
    parser.close()
    root_style = (
        "box-sizing:border-box;margin:0 auto;padding:0 4px;max-width:100%;"
        "font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;"
        "font-size:16px;line-height:1.85;letter-spacing:.02em;color:#2f3337;text-align:left;word-break:break-word;"
    )
    return f'<section style="{root_style}">{parser.result()}</section>'


def preview_document(title: str, author: str, content: str) -> str:
    author_html = (
        f'<p class="byline">{html.escape(author)}</p>' if author else ""
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(title)}</title>
  <style>
    body {{ margin:0; background:#edf1ef; color:#2f3337; }}
    main {{ box-sizing:border-box; width:min(100%, 720px); min-height:100vh; margin:0 auto; padding:28px 22px 54px; background:#fff; }}
    h1 {{ margin:0 0 10px; font:700 28px/1.35 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; color:#162d27; }}
    .byline {{ margin:0 0 30px; color:#84908b; font:14px/1.5 -apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif; }}
  </style>
</head>
<body><main><h1>{html.escape(title)}</h1>{author_html}{content}</main></body>
</html>
"""


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--author", default="")
    parser.add_argument("--digest", default="")
    parser.add_argument("--source-url", default="")
    parser.add_argument("--footer-markdown-file", type=Path)
    args = parser.parse_args()

    source = args.source.expanduser().resolve()
    if not source.is_file():
        parser.error(f"source does not exist: {source}")
    raw = source.read_text(encoding="utf-8")
    try:
        front_matter, markdown_body = parse_front_matter(raw)
    except ValueError as exc:
        parser.error(str(exc))
    h1_title, markdown_body = extract_first_h1(markdown_body)
    footer_markdown = ""
    footer_path = None
    if args.footer_markdown_file:
        footer_path = args.footer_markdown_file.expanduser().resolve()
        if not footer_path.is_file():
            parser.error(f"footer Markdown does not exist: {footer_path}")
        footer_markdown = footer_path.read_text(encoding="utf-8").strip()
        if footer_markdown:
            markdown_body = f"{markdown_body.rstrip()}\n\n{footer_markdown}\n"

    title = (args.title or str(front_matter.get("title", "")) or h1_title).strip()
    author = (args.author or str(front_matter.get("author", "")) or DEFAULT_AUTHOR).strip()
    digest = (
        args.digest
        or str(front_matter.get("digest", ""))
        or str(front_matter.get("summary", ""))
        or str(front_matter.get("description", ""))
        or make_digest(markdown_body)
    ).strip()
    source_url = (args.source_url or str(front_matter.get("source_url", ""))).strip()

    raw_html, engine = markdown_to_html(markdown_body)
    compatible_html, list_stats = flatten_wechat_lists(raw_html)
    content = style_fragment(compatible_html)
    urls = collect_urls(content)
    remote_images = [url for url in urls.images if is_unlocalized_remote_image(url)]
    native_list_tags = len(re.findall(r"</?(?:ul|ol|li)\b", content, flags=re.I))
    checks = [
        {"code": "title_present", "ok": bool(title), "blocking": True, "value": len(title)},
        {"code": "title_length", "ok": len(title) <= 32, "blocking": True, "value": len(title), "limit": 32},
        {"code": "author_length", "ok": len(author) <= 16, "blocking": True, "value": len(author), "limit": 16},
        {"code": "digest_length", "ok": len(digest) <= 128, "blocking": True, "value": len(digest), "limit": 128},
        {"code": "body_present", "ok": bool(plain_text(markdown_body)), "blocking": True},
        {"code": "content_length_budget", "ok": len(content) <= 18000, "blocking": True, "value": len(content), "limit": 18000},
        {"code": "remote_body_images", "ok": not remote_images, "blocking": True, "value": len(remote_images)},
        {"code": "wechat_safe_lists", "ok": native_list_tags == 0, "blocking": True, "value": native_list_tags},
        {"code": "external_links", "ok": not urls.links, "blocking": False, "value": len(urls.links)},
    ]
    ready = all(check["ok"] for check in checks if check["blocking"])

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    content_path = output_dir / "content.html"
    preview_path = output_dir / "preview.html"
    manifest_path = output_dir / "manifest.json"
    content_path.write_text(content + "\n", encoding="utf-8")
    preview_path.write_text(preview_document(title, author, content), encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": str(source),
        "source_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "footer_markdown": (
            {
                "path": str(footer_path),
                "sha256": hashlib.sha256(footer_markdown.encode("utf-8")).hexdigest(),
            }
            if footer_path
            else None
        ),
        "renderer": engine,
        "metadata": {
            "title": title,
            "author": author,
            "digest": digest,
            "source_url": source_url,
        },
        "checks": checks,
        "assets": {
            "body_images": urls.images,
            "unlocalized_remote_images": remote_images,
            "external_links": urls.links,
        },
        "list_compatibility": list_stats,
        "ready_for_draft": ready,
        "outputs": {
            "content_html": str(content_path),
            "preview_html": str(preview_path),
        },
    }
    write_json(manifest_path, manifest)
    print(json.dumps({"success": True, "ready_for_draft": ready, "manifest": str(manifest_path)}, ensure_ascii=False))
    return 0 if ready else 2


if __name__ == "__main__":
    sys.exit(main())
