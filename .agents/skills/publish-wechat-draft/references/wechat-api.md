# WeChat draft API boundary

Supply credentials only through `WECHAT_APPID` and `WECHAT_SECRET`. The scripts must not persist them.

## Side-effect sequence

1. Obtain an official-account access token with `GET /cgi-bin/token`.
2. Upload each local body image with `POST /cgi-bin/media/uploadimg` and replace its HTML `src` with the returned WeChat URL.
3. Upload the cover as permanent image material with `POST /cgi-bin/material/add_material?type=image`, unless an existing cover media ID was supplied.
4. Create one draft with `POST /cgi-bin/draft/add`.
5. Return the draft media ID. Stop.

For an explicitly approved update, reuse the draft `media_id` and permanent cover `media_id`, then call `POST /cgi-bin/draft/update` with `index: 0` and one `articles` object. Stop after a successful `errcode: 0`; do not create a replacement draft.

Never implement or call `/cgi-bin/freepublish/submit` in this Skill.

## Preconditions

- The account must expose the required Official Account API permissions.
- The executing machine's public IP must be allowed by the account's API whitelist when WeChat requires it.
- The content HTML must contain only local images or existing WeChat-hosted images before upload.
- The cover must exist locally or be supplied as an existing permanent `media_id`.
- The user must have reviewed the exact `content.html` and explicitly approved draft creation.

## Draft payload

Submit one article with:

- `title`
- `author` when present
- `digest` when present
- `content`
- `content_source_url` when present
- `thumb_media_id`
- `show_cover_pic: 1`

The dry-run writes the same payload with placeholder media IDs and makes no network calls.

The documented `draft/add` and `draft/update` article schemas do not expose an originality declaration field. Do not invent or send an undocumented `original`, `copyright`, or similar flag. Tell the user to declare originality manually in the Official Account backend before publishing when appropriate.

Official documentation entry points:

- https://developers.weixin.qq.com/doc/offiaccount/Basic_Information/Get_access_token.html
- https://developers.weixin.qq.com/doc/offiaccount/Asset_Management/Adding_Permanent_Assets.html
- https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/Add_draft.html
- https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_add
- https://developers.weixin.qq.com/doc/service/api/draftbox/draftmanage/api_draft_update
