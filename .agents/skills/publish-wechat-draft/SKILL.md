---
name: publish-wechat-draft
description: Render Markdown articles into WeChat-compatible inline-styled HTML, source optional Unsplash images with attribution, prepare a local preview, upload article assets, and create a WeChat Official Account draft for human review. Use when asked to turn a Markdown file into a WeChat article, preview WeChat formatting, select a cover image, upload content to a WeChat draft box, or prepare—but never mass-publish—a WeChat Official Account article.
---

# Publish WeChat Draft

Turn a Markdown source into a reviewable WeChat article. Keep rendering local, preserve the source file, and stop after creating a draft.

## Safety boundaries

- Never call `freepublish/submit`, mass-send, schedule, or publish an article.
- Never create a draft until the user has reviewed the HTML preview and explicitly approved draft creation.
- Treat image upload and draft creation as external side effects. Run the local dry-run first.
- Never write `WECHAT_APPID`, `WECHAT_SECRET`, or `UNSPLASH_ACCESS_KEY` into the repository, generated HTML, manifests, logs, or command arguments.
- Never overwrite the source Markdown. Create any image-enhanced Markdown in a temporary directory unless the user explicitly asks to save it.
- Create at most one draft per explicit approval. If the response is ambiguous or times out, report it instead of retrying.

## Workflow

1. Read the source Markdown and identify the intended title, author, digest, source URL, code blocks, tables, links, and images.
2. Create a local preview with `scripts/render_wechat.py`. Default the author to `pgweekly` unless the user or front matter supplies another author. Use a title of at most 32 Unicode characters, an author of at most 16, and a digest of at most 128.
3. Inspect the generated manifest and preview. Fix blockers in a temporary Markdown copy or with explicit metadata flags; do not edit the source implicitly.
4. When photography would improve the article, follow [references/unsplash.md](references/unsplash.md). Show 3–5 candidates and let the user choose before downloading.
5. Re-render after inserting selected local image paths and attribution into the temporary Markdown.
6. Present the final preview, chosen cover, title, digest, attribution, and any warnings. Ask for explicit approval to create the WeChat draft.
7. After approval, read [references/wechat-api.md](references/wechat-api.md), run `scripts/wechat_draft.py --dry-run`, and inspect its payload. Create a new draft with `--create --confirm-create-draft`, or update a reviewed existing draft with `--update --draft-media-id MEDIA_ID --confirm-update-draft`.
8. Return the draft `media_id`, uploaded asset results, and a manual review checklist. State that the documented draft API does not declare originality; remind the user to mark the article as original manually when appropriate. Direct the user to publish manually in the WeChat backend.

## Local rendering

Run:

```bash
python3 <skill-dir>/scripts/render_wechat.py article.md \
  --output-dir /tmp/wechat-preview \
  --title "Short WeChat title" \
  --digest "Article digest"
```

The command writes:

- `content.html`: the fragment submitted to WeChat
- `preview.html`: a standalone mobile-width browser preview
- `manifest.json`: resolved metadata, hashes, checks, and output paths

The renderer flattens native HTML lists into WeChat-safe paragraphs with explicit markers. Treat any remaining `<ul>`, `<ol>`, or `<li>` tag as a blocking compatibility error.

Read [references/renderer.md](references/renderer.md) when diagnosing rendering or metadata behavior.

## Unsplash images

Search only through the official API:

```bash
python3 <skill-dir>/scripts/unsplash_images.py search \
  --query "data infrastructure" \
  --orientation landscape \
  --output /tmp/unsplash-candidates.json
```

If the user already selected a specific Unsplash photo, retrieve it deterministically by photo ID:

```bash
python3 <skill-dir>/scripts/unsplash_images.py lookup \
  --photo-id PHOTO_ID \
  --output /tmp/unsplash-candidates.json
```

After the user selects a candidate, trigger the required download event and download an optimized image:

```bash
python3 <skill-dir>/scripts/unsplash_images.py download \
  --candidates /tmp/unsplash-candidates.json \
  --photo-id PHOTO_ID \
  --output /tmp/wechat-cover.jpg \
  --width 900 \
  --height 383
```

Always retain the returned attribution text and links in the article or image caption.

## Draft creation

Prepare and inspect without network side effects:

```bash
python3 <skill-dir>/scripts/wechat_draft.py \
  --manifest /tmp/wechat-preview/manifest.json \
  --cover /tmp/wechat-cover.jpg \
  --dry-run \
  --output /tmp/wechat-draft-request.json
```

Only after explicit approval, supply credentials through the environment and create one draft:

```bash
python3 <skill-dir>/scripts/wechat_draft.py \
  --manifest /tmp/wechat-preview/manifest.json \
  --cover /tmp/wechat-cover.jpg \
  --create \
  --confirm-create-draft
```

Do not add a publishing step.

## Draft update

Reuse an existing permanent cover media ID and prepare the exact update payload without network side effects:

```bash
python3 <skill-dir>/scripts/wechat_draft.py \
  --manifest /tmp/wechat-preview/manifest.json \
  --cover-media-id COVER_MEDIA_ID \
  --dry-run \
  --draft-media-id DRAFT_MEDIA_ID \
  --output /tmp/wechat-draft-update-request.json
```

Only after the user reviews the corrected preview and explicitly approves updating that draft, run:

```bash
python3 <skill-dir>/scripts/wechat_draft.py \
  --manifest /tmp/wechat-preview/manifest.json \
  --cover-media-id COVER_MEDIA_ID \
  --update \
  --draft-media-id DRAFT_MEDIA_ID \
  --confirm-update-draft
```

Update article index `0`, reuse the cover when possible, and do not create a replacement draft.
