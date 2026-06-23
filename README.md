# Meta Data Sync

GitHub Actions workflow that fetches a small sample of Facebook/Instagram posts and comments from Meta, analyzes the text with Gemini, and writes the results to Google Sheets.

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

- `GEMINI_MODEL` - default: `gemini-2.5-flash`.
- `META_GRAPH_VERSION` - default: `v23.0`.
- `MAX_FACEBOOK_POSTS` - default: `1`.
- `MAX_INSTAGRAM_MEDIA` - default: `1`.
- `MAX_ADS` - default: `0`.
- `MAX_COMMENTS_PER_POST` - default: `5`.
- `MAX_REPLIES_PER_COMMENT` - default: `2`.
- `COMMENT_LOOKBACK_DAYS` - default: `7`.
- `POST_LOOKBACK_DAYS` - default: `14`.
- `GEMINI_BATCH_SIZE` - default: `20`.
- `ANALYSIS_TEXT_CHARS` - default: `1200`.

## First Run

Run the `Meta data sync` workflow manually from the Actions tab with the defaults first. It starts with a tiny sample to avoid wasting Gemini tokens.
