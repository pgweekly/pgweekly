# Unsplash image workflow

Use the official Unsplash API, not HTML scraping. Supply the access key through `UNSPLASH_ACCESS_KEY`.

## Selection

1. Derive one or two concrete English search queries from the article.
2. Request landscape images with `content_filter=high` for covers.
3. Show 3–5 candidates with photographer, description, dimensions, and Unsplash page URL.
4. Prefer images without recognizable people, logos, trademarks, artwork, or private property.
5. Let the user choose. Do not silently pick and upload a photo.

If the user already chose a stable Unsplash photo ID, use the script's `lookup` command instead of relying on search ranking to return it again.

For technical articles, prefer one relevant cover over decorative body photos. Use diagrams or screenshots for technical explanations rather than generic stock photography.

## Download and attribution

When a photo is selected:

1. Call its `download_location` endpoint to register the download.
2. Download from the returned image URL with the requested crop parameters.
3. Keep the photo ID, photographer name, photographer URL, Unsplash URL, and query in the sidecar metadata.
4. Include linked attribution in the final article, for example: `Photo by Name on Unsplash`.

The general Unsplash License does not require attribution, but API use does. API links must include the configured UTM source and `utm_medium=referral`.

Sources:

- https://unsplash.com/documentation
- https://unsplash.com/api-terms
- https://help.unsplash.com/en/articles/2511315-guideline-attribution
- https://help.unsplash.com/en/articles/2612329-releases-and-trademarks
