#!/usr/bin/env python3
"""Search and download Unsplash images through the official API."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen


API_BASE = "https://api.unsplash.com"
USER_AGENT = "publish-wechat-draft/1.0"


def access_key(parser: argparse.ArgumentParser) -> str:
    key = os.environ.get("UNSPLASH_ACCESS_KEY", "").strip()
    if not key:
        parser.error("UNSPLASH_ACCESS_KEY is required")
    return key


def api_json(url: str, key: str) -> object:
    req = Request(
        url,
        headers={
            "Authorization": f"Client-ID {key}",
            "Accept-Version": "v1",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        message = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Unsplash API returned HTTP {exc.code}: {message}") from exc
    except URLError as exc:
        raise RuntimeError(f"Unsplash API request failed: {exc.reason}") from exc


def with_utm(url: str, source: str) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"utm_source": source, "utm_medium": "referral"})
    return urlunparse(parsed._replace(query=urlencode(query)))


def normalize_photo(photo: dict[str, object], query: str, source: str) -> dict[str, object]:
    user = photo.get("user") if isinstance(photo.get("user"), dict) else {}
    links = photo.get("links") if isinstance(photo.get("links"), dict) else {}
    urls = photo.get("urls") if isinstance(photo.get("urls"), dict) else {}
    photographer = str(user.get("name", "Unknown photographer"))
    photographer_url = with_utm(str(user.get("links", {}).get("html", "https://unsplash.com")), source) if isinstance(user.get("links"), dict) else with_utm("https://unsplash.com", source)
    unsplash_url = with_utm(str(links.get("html", "https://unsplash.com")), source)
    return {
        "id": photo.get("id"),
        "query": query,
        "description": photo.get("description") or photo.get("alt_description") or "",
        "width": photo.get("width"),
        "height": photo.get("height"),
        "color": photo.get("color"),
        "preview_url": urls.get("small"),
        "raw_url": urls.get("raw"),
        "download_location": links.get("download_location"),
        "unsplash_url": unsplash_url,
        "photographer": photographer,
        "photographer_url": photographer_url,
        "attribution_text": f"Photo by {photographer} on Unsplash",
        "attribution_html": (
            f'Photo by <a href="{photographer_url}">{photographer}</a> '
            f'on <a href="{with_utm("https://unsplash.com", source)}">Unsplash</a>'
        ),
    }


def search(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    key = access_key(parser)
    params = urlencode(
        {
            "query": args.query,
            "orientation": args.orientation,
            "content_filter": "high",
            "per_page": args.count,
            "page": 1,
        }
    )
    payload = api_json(f"{API_BASE}/search/photos?{params}", key)
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected Unsplash search response")
    photos = payload.get("results", [])
    if not isinstance(photos, list):
        raise RuntimeError("unexpected Unsplash search results")
    normalized = [normalize_photo(item, args.query, args.utm_source) for item in photos]
    result = {
        "schema_version": 1,
        "query": args.query,
        "orientation": args.orientation,
        "results": normalized,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"success": True, "count": len(normalized), "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


def lookup(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    key = access_key(parser)
    payload = api_json(f"{API_BASE}/photos/{args.photo_id}", key)
    if not isinstance(payload, dict):
        raise RuntimeError("unexpected Unsplash photo response")
    normalized = normalize_photo(payload, args.query, args.utm_source)
    result = {
        "schema_version": 1,
        "query": args.query,
        "orientation": None,
        "results": [normalized],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"success": True, "count": 1, "output": str(args.output.resolve())}, ensure_ascii=False))
    return 0


def resized_url(url: str, width: int, height: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.update({"fm": "jpg", "q": "85", "fit": "crop", "w": str(width)})
    if height > 0:
        query["h"] = str(height)
    return urlunparse(parsed._replace(query=urlencode(query)))


def download(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    key = access_key(parser)
    candidate_data = json.loads(args.candidates.read_text(encoding="utf-8"))
    matches = [item for item in candidate_data.get("results", []) if item.get("id") == args.photo_id]
    if len(matches) != 1:
        parser.error(f"photo ID {args.photo_id!r} was not found exactly once in candidates")
    selected = matches[0]
    location = selected.get("download_location")
    if not location:
        raise RuntimeError("selected photo has no download_location")

    event = api_json(str(location), key)
    if not isinstance(event, dict) or not event.get("url"):
        raise RuntimeError("Unsplash download event did not return an image URL")
    image_url = resized_url(str(event["url"]), args.width, args.height)
    req = Request(image_url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=60) as response:
            data = response.read()
    except HTTPError as exc:
        raise RuntimeError(f"image download returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"image download failed: {exc.reason}") from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(data)
    sidecar = args.output.with_suffix(args.output.suffix + ".json")
    selected = dict(selected)
    selected.update(
        {
            "downloaded_file": str(args.output.resolve()),
            "requested_width": args.width,
            "requested_height": args.height,
            "bytes": len(data),
        }
    )
    sidecar.write_text(json.dumps(selected, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"success": True, "output": str(args.output.resolve()), "metadata": str(sidecar.resolve())}, ensure_ascii=False))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search")
    search_parser.add_argument("--query", required=True)
    search_parser.add_argument("--orientation", choices=["landscape", "portrait", "squarish"], default="landscape")
    search_parser.add_argument("--count", type=int, default=5, choices=range(1, 11))
    search_parser.add_argument("--utm-source", default="pgweekly_wechat_skill")
    search_parser.add_argument("--output", type=Path, required=True)

    lookup_parser = subparsers.add_parser("lookup")
    lookup_parser.add_argument("--photo-id", required=True)
    lookup_parser.add_argument("--query", default="selected by photo ID")
    lookup_parser.add_argument("--utm-source", default="pgweekly_wechat_skill")
    lookup_parser.add_argument("--output", type=Path, required=True)

    download_parser = subparsers.add_parser("download")
    download_parser.add_argument("--candidates", type=Path, required=True)
    download_parser.add_argument("--photo-id", required=True)
    download_parser.add_argument("--output", type=Path, required=True)
    download_parser.add_argument("--width", type=int, default=900)
    download_parser.add_argument("--height", type=int, default=383)

    args = parser.parse_args()
    try:
        if args.command == "search":
            return search(args, parser)
        if args.command == "lookup":
            return lookup(args, parser)
        return download(args, parser)
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
