# Meta Data Sync

GitHub Actions workflow that scans Facebook/Instagram organic and paid content, analyzes new items with Gemini, and writes the results to Google Sheets.

Each run reads existing `post_id` and `comment_id` values from Google Sheets first. Existing posts and paid items are scanned for new comments but are never analyzed again. Existing comments are neither written nor analyzed again.

The scheduled run scans all accessible content published in the last 30 days and collects at most 500 new comments across Facebook, Instagram, organic content, and paid content. The limit is global, includes official Greenpeace comments, and is applied after existing comment IDs are removed. If more comments are found, the oldest unseen comments are collected first.

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

- `GEMINI_MODEL` - default: `gemini-3.1-flash-lite`. The GitHub Actions workflow pins this default unless you pass a different `gemini_model` value in a manual run.
- `GEMINI_API_MODE` - default: `generateContent`.
- `META_GRAPH_VERSION` - default: `v23.0`.
- `LOOKBACK_DAYS` - default: `30`. Counts backward from the current time and must be at least `1`.
- `MAX_COMMENTS_PER_RUN` - default: `500`. Global limit for new audience and official comments across all sources; must be at least `1`.
- `GEMINI_BATCH_SIZE` - default: `1`.
- `ANALYSIS_TEXT_CHARS` - default: `1200`.
- `GEMINI_MAX_RETRIES` - default: `3`.
- `GEMINI_RETRY_BASE_SECONDS` - default: `20`.
- `GEMINI_FALLBACK_ON_ERROR` - default: `false`; fails the run if Gemini analysis does not return valid data.
- `GEMINI_FALLBACK_AFTER_QUOTA_ERROR` - default: `false`; avoids spending another request after Gemini returns quota/rate-limit errors.

## Comment Analysis Columns

The `Post/Ad Comments` sheet includes the original emotion fields plus:

- `is_brand_comment` - marks official Greenpeace replies so they are not treated as audience sentiment.
- `parent_comment_message` - stores the immediately previous message for replies, and is sent to Gemini as conversation context.
- `comment_sentiment` - `positive`, `negative`, `mixed`, `neutral`, or `unclear`.
- `comment_intent` includes `service_request` for donation, unsubscribe, billing, or account-support comments.
- `response_value_score` - 0.0 to 1.0 estimate of the organizational benefit Greenpeace would get from replying. A score of 0.70 or higher is the recommended response threshold.
- `response_value_score` is forced to `0` when a later official Greenpeace reply was collected in the same thread.
- The comment-level `requires_response` column is no longer used; response recommendations are derived only from the score.
- `requires_response_count` and `requires_response_rate` in `Post/Ad Summary` are calculated using the 0.70 response-score threshold.
- Official Greenpeace comments keep response-analysis fields blank.

The `Posts/Ads` sheet includes:

- `post_sentiment` - `positive`, `negative`, `mixed`, `neutral`, or `unclear`.

## First Run

Run the `Meta data sync` workflow manually from the Actions tab. Manual runs expose only the lookback period, the global new-comment limit, and the Gemini model. The manual comment limit may be set above 500 to clear a backlog; scheduled runs always use 30 days and 500 comments.

To rebuild the Google Sheets tabs from scratch, run:

```bash
python setup_google_sheets_schema.py --reset
```
