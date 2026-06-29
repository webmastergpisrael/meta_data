# Meta Data Sync

GitHub Actions workflow that fetches a small sample of Facebook/Instagram posts and comments from Meta, analyzes the text with Gemini, and writes the results to Google Sheets.

Each run reads existing `post_id` and `comment_id` values from Google Sheets first. Existing posts are skipped, so their comments are not fetched or analyzed again.

## Required GitHub Secrets

Add these in `Settings -> Secrets and variables -> Actions -> New repository secret`:

- `GOOGLE_SERVICE_ACCOUNT_JSON` - full Google service account JSON with Google Sheets API access.
- `SPREADSHEET_ID` - the Google Sheets document ID.
- `META_ACCESS_TOKEN` - the single Meta access token used for all Meta Graph API reads. The script auto-discovers the Facebook Page, connected Instagram business account, and ad account when ads are enabled.
- `GEMINI_API_KEY` - Gemini API key, not a model ID or project ID.

Share the Google Sheet with the `client_email` from the service account JSON as an editor.

Do not commit tokens or API keys into this repository. If a token was pasted into chat, browser history, or a public place, rotate it before using the workflow.

## Optional GitHub Variables

Add these in `Settings -> Secrets and variables -> Actions -> Variables` if you want to override the defaults:

- `GEMINI_MODEL` - default: `gemini-3.5-flash`.
- `GEMINI_API_MODE` - default: `generateContent`.
- `META_GRAPH_VERSION` - default: `v23.0`.
- `MAX_FACEBOOK_POSTS` - default: `1`.
- `MAX_INSTAGRAM_MEDIA` - default: `1`.
- `MAX_PAID_POSTS` - default: `0`. Counts paid Facebook/Instagram posts together.
- `MAX_ADS` - default: `10`. Counts ads to scan while looking for paid posts.
- `MAX_COMMENTS` - default: `10`. Counts top-level comments and replies together across the whole run.
- `MAX_COMMENTS_PER_POST` - default: `5`. Counts top-level comments and replies together per post/media/paid post.
- `START_DATE` - optional `YYYY-MM-DD`; blank means 7 days before `END_DATE` or now.
- `END_DATE` - optional `YYYY-MM-DD`; blank means the current time.
- `GEMINI_BATCH_SIZE` - default: `1`.
- `ANALYSIS_TEXT_CHARS` - default: `1200`.
- `GEMINI_MAX_RETRIES` - default: `0`.
- `GEMINI_RETRY_BASE_SECONDS` - default: `15`.
- `GEMINI_FALLBACK_ON_ERROR` - default: `false`; fails the run if Gemini analysis does not return valid data.
- `GEMINI_FALLBACK_AFTER_QUOTA_ERROR` - default: `false`; avoids spending another request after Gemini returns quota/rate-limit errors.

## Comment Analysis Columns

The `Post Comments` sheet includes the original emotion fields plus:

- `is_brand_comment` - marks official Greenpeace replies so they are not treated as audience sentiment.
- `comment_sentiment` - `positive`, `negative`, `mixed`, `neutral`, or `unclear`.
- `comment_tone` - tone such as `hostile`, `sarcastic`, `worried`, `supportive`, `curious`, or `dismissive`.

## First Run

Run the `Meta data sync` workflow manually from the Actions tab with the defaults first. It starts with a tiny sample to avoid wasting Gemini tokens.
# Meta Data Sync

GitHub Actions workflow that fetches a small sample of Facebook/Instagram posts and comments from Meta, analyzes the text with Gemini, and writes the results to Google Sheets.

Each run reads existing `post_id` and `comment_id` values from Google Sheets first. Existing posts are skipped, so their comments are not fetched or analyzed again.

## Required GitHub Secrets

Add these in `Settings -> Secrets and variables -> Actions -> New repository secret`:

- `GOOGLE_SERVICE_ACCOUNT_JSON` - full Google service account JSON with Google Sheets API access.
- `SPREADSHEET_ID` - the Google Sheets document ID.
- `META_ACCESS_TOKEN` - the single Meta access token used for all Meta Graph API reads. The script auto-discovers the Facebook Page, connected Instagram business account, and ad account when ads are enabled.
- `GEMINI_API_KEY` - Gemini API key, not a model ID or project ID.

Share the Google Sheet with the `client_email` from the service account JSON as an editor.

Do not commit tokens or API keys into this repository. If a token was pasted into chat, browser history, or a public place, rotate it before using the workflow.

## Optional GitHub Variables

Add these in `Settings -> Secrets and variables -> Actions -> Variables` if you want to override the defaults:

- `GEMINI_MODEL` - default: `gemini-3.5-flash`.
- `GEMINI_API_MODE` - default: `generateContent`.
- `META_GRAPH_VERSION` - default: `v23.0`.
- `MAX_FACEBOOK_POSTS` - default: `1`.
- `MAX_INSTAGRAM_MEDIA` - default: `1`.
- `MAX_PAID_POSTS` - default: `0`. Counts paid Facebook/Instagram posts together.
- `MAX_ADS` - default: `10`. Counts ads to scan while looking for paid posts.
- `MAX_COMMENTS` - default: `10`. Counts top-level comments and replies together across the whole run.
- `MAX_COMMENTS_PER_POST` - default: `5`. Counts top-level comments and replies together per post/media/paid post.
- `START_DATE` - optional `YYYY-MM-DD`; blank means 7 days before `END_DATE` or now.
- `END_DATE` - optional `YYYY-MM-DD`; blank means the current time.
- `GEMINI_BATCH_SIZE` - default: `1`.
- `ANALYSIS_TEXT_CHARS` - default: `1200`.
- `GEMINI_MAX_RETRIES` - default: `0`.
- `GEMINI_RETRY_BASE_SECONDS` - default: `15`.
- `GEMINI_FALLBACK_ON_ERROR` - default: `false`; fails the run if Gemini analysis does not return valid data.
- `GEMINI_FALLBACK_AFTER_QUOTA_ERROR` - default: `false`; avoids spending another request after Gemini returns quota/rate-limit errors.

## Comment Analysis Columns

The `Post Comments` sheet includes the original emotion fields plus:

- `is_brand_comment` - marks official Greenpeace replies so they are not treated as audience sentiment.
- `comment_sentiment` - `positive`, `negative`, `mixed`, `neutral`, or `unclear`.
- `comment_tone` - tone such as `hostile`, `sarcastic`, `worried`, `supportive`, `curious`, or `dismissive`.

## First Run

Run the `Meta data sync` workflow manually from the Actions tab with the defaults first. It starts with a tiny sample to avoid wasting Gemini tokens.
