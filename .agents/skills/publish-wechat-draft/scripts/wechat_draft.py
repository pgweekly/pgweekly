#!/usr/bin/env python3
"""Prepare, create, or update one WeChat Official Account draft."""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


WECHAT_API = "https://api.weixin.qq.com"
USER_AGENT = "publish-wechat-draft/1.0"
IMAGE_PATTERN = re.compile(r'(<img\b[^>]*?\bsrc\s*=\s*)(["\'])(.*?)(\2)', re.I | re.S)


class WeChatAPIError(RuntimeError):
    pass


def request_json(method: str, url: str, body: bytes | None = None, headers: dict[str, str] | None = None) -> dict[str, object]:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    req = Request(url, data=body, headers=request_headers, method=method)
    try:
        with urlopen(req, timeout=60) as response:
            payload = json.load(response)
    except HTTPError as exc:
        raise WeChatAPIError(f"WeChat API returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise WeChatAPIError(f"WeChat API request failed: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise WeChatAPIError("WeChat API returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise WeChatAPIError("WeChat API returned an unexpected response")
    errcode = payload.get("errcode", 0)
    if errcode not in (0, None):
        raise WeChatAPIError(f"WeChat API error {errcode}: {payload.get('errmsg', 'unknown error')}")
    return payload


def get_access_token(appid: str, secret: str) -> str:
    query = urlencode({"grant_type": "client_credential", "appid": appid, "secret": secret})
    payload = request_json("GET", f"{WECHAT_API}/cgi-bin/token?{query}")
    token = str(payload.get("access_token", ""))
    if not token:
        raise WeChatAPIError("WeChat token response did not contain access_token")
    return token


def multipart_file(field: str, path: Path) -> tuple[bytes, str]:
    boundary = "----publish-wechat-draft-" + secrets.token_hex(12)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    data = path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="{field}"; filename="{path.name}"\r\n'.encode(),
            f"Content-Type: {content_type}\r\n\r\n".encode(),
            data,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


def upload_body_image(token: str, path: Path) -> str:
    body, content_type = multipart_file("media", path)
    payload = request_json(
        "POST",
        f"{WECHAT_API}/cgi-bin/media/uploadimg?access_token={token}",
        body,
        {"Content-Type": content_type},
    )
    url = str(payload.get("url", ""))
    if not url:
        raise WeChatAPIError(f"body image upload returned no URL for {path.name}")
    return url


def upload_cover(token: str, path: Path) -> str:
    body, content_type = multipart_file("media", path)
    payload = request_json(
        "POST",
        f"{WECHAT_API}/cgi-bin/material/add_material?access_token={token}&type=image",
        body,
        {"Content-Type": content_type},
    )
    media_id = str(payload.get("media_id", ""))
    if not media_id:
        raise WeChatAPIError("cover upload returned no media_id")
    return media_id


def resolve_local_image(src: str, source_dir: Path) -> Path | None:
    parsed = urlparse(src)
    if parsed.scheme in {"http", "https"}:
        hostname = (parsed.hostname or "").lower()
        if hostname == "mmbiz.qpic.cn" or hostname.endswith(".mmbiz.qpic.cn"):
            return None
        raise ValueError(f"remote body image must be downloaded locally before draft creation: {src}")
    if parsed.scheme == "file":
        path = Path(parsed.path)
    elif parsed.scheme:
        raise ValueError(f"unsupported image URL scheme: {src}")
    else:
        path = Path(src)
        if not path.is_absolute():
            path = source_dir / path
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ValueError(f"body image does not exist: {path}")
    return path


def localize_body_images(content: str, source_dir: Path, token: str | None, dry_run: bool) -> tuple[str, list[dict[str, object]]]:
    uploads: list[dict[str, object]] = []

    def replace(match: re.Match[str]) -> str:
        src = match.group(3)
        path = resolve_local_image(src, source_dir)
        if path is None:
            uploads.append({"source": src, "uploaded": False, "reason": "already_wechat_hosted"})
            return match.group(0)
        if dry_run:
            url = f"https://mmbiz.qpic.cn/dry-run/{len(uploads) + 1}/{path.name}"
        else:
            if not token:
                raise RuntimeError("missing access token")
            url = upload_body_image(token, path)
        uploads.append({"source": str(path), "uploaded": not dry_run, "wechat_url": url})
        return f"{match.group(1)}{match.group(2)}{url}{match.group(4)}"

    return IMAGE_PATTERN.sub(replace, content), uploads


def build_article(metadata: dict[str, object], content: str, cover_media_id: str) -> dict[str, object]:
    article: dict[str, object] = {
        "title": str(metadata.get("title", "")),
        "content": content,
        "thumb_media_id": cover_media_id,
        "show_cover_pic": 1,
    }
    for key in ("author", "digest"):
        value = str(metadata.get(key, "")).strip()
        if value:
            article[key] = value
    source_url = str(metadata.get("source_url", "")).strip()
    if source_url:
        article["content_source_url"] = source_url
    return article


def create_draft(token: str, article: dict[str, object]) -> str:
    body = json.dumps({"articles": [article]}, ensure_ascii=False).encode("utf-8")
    payload = request_json(
        "POST",
        f"{WECHAT_API}/cgi-bin/draft/add?access_token={token}",
        body,
        {"Content-Type": "application/json; charset=utf-8"},
    )
    media_id = str(payload.get("media_id", ""))
    if not media_id:
        raise WeChatAPIError("draft creation returned no media_id")
    return media_id


def update_draft(token: str, draft_media_id: str, article: dict[str, object], index: int = 0) -> None:
    body = json.dumps(
        {"media_id": draft_media_id, "index": index, "articles": article},
        ensure_ascii=False,
    ).encode("utf-8")
    request_json(
        "POST",
        f"{WECHAT_API}/cgi-bin/draft/update?access_token={token}",
        body,
        {"Content-Type": "application/json; charset=utf-8"},
    )


def validate_manifest(manifest: dict[str, object], manifest_path: Path) -> tuple[dict[str, object], Path, Path]:
    if not manifest.get("ready_for_draft"):
        raise ValueError("renderer manifest is not ready for draft creation")
    metadata = manifest.get("metadata")
    outputs = manifest.get("outputs")
    if not isinstance(metadata, dict) or not isinstance(outputs, dict):
        raise ValueError("invalid renderer manifest")
    content_path = Path(str(outputs.get("content_html", "")))
    if not content_path.is_absolute():
        content_path = manifest_path.parent / content_path
    if not content_path.is_file():
        raise ValueError(f"content HTML does not exist: {content_path}")
    source = Path(str(manifest.get("source", "")))
    if not source.is_file():
        raise ValueError(f"source Markdown does not exist: {source}")
    return metadata, content_path.resolve(), source.resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    cover = parser.add_mutually_exclusive_group(required=True)
    cover.add_argument("--cover", type=Path)
    cover.add_argument("--cover-media-id")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--create", action="store_true")
    mode.add_argument("--update", action="store_true")
    parser.add_argument("--draft-media-id")
    parser.add_argument("--confirm-create-draft", action="store_true")
    parser.add_argument("--confirm-update-draft", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.create and not args.confirm_create_draft:
        parser.error("--create requires --confirm-create-draft after explicit user approval")
    if args.update and not args.confirm_update_draft:
        parser.error("--update requires --confirm-update-draft after explicit user approval")
    if args.update and not (args.draft_media_id or "").strip():
        parser.error("--update requires --draft-media-id")
    if args.create and args.draft_media_id:
        parser.error("--draft-media-id is invalid with --create")
    if args.dry_run and (args.confirm_create_draft or args.confirm_update_draft):
        parser.error("confirmation flags are invalid with --dry-run")
    if args.create and args.confirm_update_draft:
        parser.error("--confirm-update-draft is invalid with --create")
    if args.update and args.confirm_create_draft:
        parser.error("--confirm-create-draft is invalid with --update")

    try:
        manifest_path = args.manifest.expanduser().resolve()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        metadata, content_path, source_dir = validate_manifest(manifest, manifest_path)
        content = content_path.read_text(encoding="utf-8")

        cover_path: Path | None = None
        if args.cover:
            cover_path = args.cover.expanduser().resolve()
            if not cover_path.is_file():
                raise ValueError(f"cover image does not exist: {cover_path}")

        token: str | None = None
        if args.create or args.update:
            appid = os.environ.get("WECHAT_APPID", "").strip()
            secret = os.environ.get("WECHAT_SECRET", "").strip()
            if not appid or not secret:
                raise ValueError("WECHAT_APPID and WECHAT_SECRET are required for network writes")
            token = get_access_token(appid, secret)

        localized_content, uploads = localize_body_images(content, source_dir, token, args.dry_run)
        if args.cover_media_id:
            cover_media_id = args.cover_media_id.strip()
        elif args.dry_run:
            cover_media_id = "DRY_RUN_COVER_MEDIA_ID"
        else:
            if not token or not cover_path:
                raise RuntimeError("missing token or cover path")
            cover_media_id = upload_cover(token, cover_path)

        article = build_article(metadata, localized_content, cover_media_id)
        is_update_payload = args.update or (args.dry_run and bool((args.draft_media_id or "").strip()))
        if is_update_payload:
            request_payload = {
                "media_id": str(args.draft_media_id).strip(),
                "index": 0,
                "articles": article,
            }
        else:
            request_payload = {"articles": [article]}
        output_path = args.output.expanduser().resolve() if args.output else manifest_path.parent / "draft-request.json"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(request_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        if args.dry_run:
            result_mode = "dry_run_update" if is_update_payload else "dry_run_create"
        elif args.update:
            result_mode = "update"
        else:
            result_mode = "create"
        result: dict[str, object] = {
            "success": True,
            "mode": result_mode,
            "request": str(output_path),
            "body_images": uploads,
            "cover": str(cover_path) if cover_path else "existing_media_id",
        }
        if args.create:
            if not token:
                raise RuntimeError("missing access token")
            result["draft_media_id"] = create_draft(token, article)
        elif args.update:
            if not token:
                raise RuntimeError("missing access token")
            draft_media_id = str(args.draft_media_id).strip()
            update_draft(token, draft_media_id, article)
            result["updated_draft_media_id"] = draft_media_id
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, WeChatAPIError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
