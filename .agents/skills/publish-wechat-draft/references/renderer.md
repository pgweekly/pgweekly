# Renderer contract

## Inputs

`render_wechat.py` accepts one UTF-8 Markdown file and optional metadata overrides:

- `--title`: override front matter or the first H1
- `--author`: override front matter `author`
- `--digest`: override `digest`, `summary`, or `description`
- `--source-url`: set the article source URL for the draft payload
- `--footer-markdown-file`: append generated attribution or disclosure text without modifying the source
- `--output-dir`: required destination for review artifacts

The source is never modified. YAML front matter is optional.

## Metadata resolution

Resolve metadata in this order:

1. Explicit command-line override
2. YAML front matter
3. First Markdown H1 for the title
4. Project default `pgweekly` for the author when neither override nor front matter supplies it

Remove the first H1 from the rendered body to avoid duplicating the title shown by WeChat.

## Outputs

- `content.html` contains a single `<section>` with inline CSS and no scripts.
- `preview.html` wraps the same fragment in a standalone mobile-width document.
- `manifest.json` records the source SHA-256, optional footer path and hash, resolved metadata, checks, and absolute output paths.

The renderer uses Python-Markdown when available and falls back to Pandoc. Fail if neither exists.

Flatten Markdown-generated `<ul>`, `<ol>`, and `<li>` elements into ordinary paragraphs with explicit bullet or number markers before applying inline styles. Native list elements can produce empty items after the WeChat editor normalizes API-submitted HTML. Preserve inline links, emphasis, and code inside each item. Record transformed container and item counts in `manifest.json`.

## Review checks

Treat these as blockers before draft creation:

- Empty title or body
- Title longer than 32 Unicode characters
- Author longer than 16 Unicode characters
- Digest longer than 128 Unicode characters
- Rendered content longer than the Skill's conservative 18,000-character safety budget
- Any remote body image that has not been uploaded to WeChat
- Any native `<ul>`, `<ol>`, or `<li>` tag remaining after the compatibility pass

Treat external article links as warnings: WeChat may restrict or rewrite links depending on account capabilities.
