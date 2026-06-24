import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

POST_HEADERS = [
    "post_id",
    "platform",
    "post_created_time",
    "post_message",
    "post_url",
    "post_hashtags",
    "canonical_topic",
    "canonical_subtopic",
    "topic_source",
    "topic_confidence",
    "source_type",
    "campaign_name",
    "ad_id",
    "media_type",
    "collected_comment_count",
    "collected_greenpeace_comment_count",
    "collected_like_count",
    "collected_reply_count",
    "collected_window_start",
    "collected_window_end",
    "post_emotions",
]

COMMENT_HEADERS = [
    "comment_id",
    "post_id",
    "parent_comment_id",
    "commenter_id",
    "commenter_name",
    "comment_created_time",
    "comment_message",
    "comment_url",
    "like_count",
    "reply_count",
    "collected_at",
    "comment_emotions",
    "Emotion_Confidence",
    "comment_stance",
    "comment_intent",
    "is_sarcastic",
    "requires_response",
    "response_priority",
]

SUMMARY_HEADERS = [
    "post_id",
    "total_comments",
    "dominant_comment_stance",
    "dominant_comment_intent",
    "supportive_comment_rate",
    "opposed_comment_rate",
    "skeptical_comment_rate",
    "neutral_comment_rate",
    "question_rate",
    "criticism_rate",
    "mockery_rate",
    "requires_response_count",
    "requires_response_rate",
]

FACEBOOK_COMMENT_FIELDS = ",".join(
    [
        "id",
        "message",
        "created_time",
        "from{id,name}",
        "permalink_url",
        "parent{id}",
        "comment_count",
        "like_count",
    ]
)

POST_EMOTIONS = ["Awe", "Urgency", "Anger", "Grief", "Hope", "Sarcasm", "Amusement", "Neutral"]
COMMENT_EMOTIONS = ["Admiration", "Sadness", "Skepticism", "Anger", "Sarcasm", "Anxiety", "Amusement", "Neutral"]
TOPIC_SOURCES = ["hashtags", "campaign_name", "ad_text", "ai_classification", "manual", "unknown"]
COMMENT_STANCES = ["supportive", "opposed", "skeptical", "neutral", "unclear"]
COMMENT_INTENTS = [
    "support",
    "criticism",
    "question",
    "mockery",
    "information_request",
    "personal_story",
    "tag_friend",
    "political_attack",
    "spam",
    "other",
]
RESPONSE_PRIORITIES = ["none", "low", "medium", "high"]


def env_value(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def env_int(name: str, default: int) -> int:
    value = env_value(name, str(default))
    return int(value) if value else default


def env_bool(name: str, default: bool) -> bool:
    value = env_value(name, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "y"}


CONFIG = {
    "spreadsheet_id": env_value("SPREADSHEET_ID"),
    "page_id": "",
    "ig_user_id": "",
    "ad_account_id": "",
    "graph_version": env_value("META_GRAPH_VERSION", "v23.0"),
    "post_lookback_days": env_int("POST_LOOKBACK_DAYS", 14),
    "comment_lookback_days": env_int("COMMENT_LOOKBACK_DAYS", 7),
    "max_facebook_posts": env_int("MAX_FACEBOOK_POSTS", 1),
    "max_instagram_media": env_int("MAX_INSTAGRAM_MEDIA", 1),
    "max_ads": env_int("MAX_ADS", 0),
    "max_comments_per_post": env_int("MAX_COMMENTS_PER_POST", 5),
    "max_replies_per_comment": env_int("MAX_REPLIES_PER_COMMENT", 2),
    "analysis_batch_size": env_int("GEMINI_BATCH_SIZE", 20),
    "analysis_text_chars": env_int("ANALYSIS_TEXT_CHARS", 1200),
    "gemini_max_retries": env_int("GEMINI_MAX_RETRIES", 0),
    "gemini_retry_base_seconds": env_int("GEMINI_RETRY_BASE_SECONDS", 15),
    "gemini_fallback_on_error": env_bool("GEMINI_FALLBACK_ON_ERROR", True),
    "gemini_model": env_value("GEMINI_MODEL", "gemini-2.5-flash-lite"),
    "gemini_api_mode": env_value("GEMINI_API_MODE", "interactions"),
    "greenpeace_facebook_page_id": "",
    "greenpeace_instagram_username": "",
}


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def meta_access_token() -> str:
    value = os.getenv("META_ACCESS_TOKEN")
    if not value:
        raise RuntimeError("Missing required environment variable: META_ACCESS_TOKEN")
    return value


def get_sheets_service():
    credentials_info = json.loads(required_env("GOOGLE_SERVICE_ACCOUNT_JSON"))
    credentials = service_account.Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
    return build("sheets", "v4", credentials=credentials)


def sheet_range(sheet_name: str, a1_range: str) -> str:
    escaped = sheet_name.replace("'", "''")
    return f"'{escaped}'!{a1_range}"


def col_letter(index: int) -> str:
    letters = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def pad_row(row: list[Any], width: int) -> list[Any]:
    return row + [""] * max(0, width - len(row))


def batch_update(service, spreadsheet_id: str, requests_body: list[dict[str, Any]]) -> None:
    if requests_body:
        service.spreadsheets().batchUpdate(spreadsheetId=spreadsheet_id, body={"requests": requests_body}).execute()


def get_spreadsheet(service, spreadsheet_id: str) -> dict[str, Any]:
    return (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title))")
        .execute()
    )


def find_sheet(metadata: dict[str, Any], title: str) -> dict[str, Any] | None:
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == title:
            return properties
    return None


def update_values(service, spreadsheet_id: str, range_name: str, values: list[list[Any]]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def get_values(service, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    response = service.spreadsheets().values().get(spreadsheetId=spreadsheet_id, range=range_name).execute()
    return response.get("values", [])


def append_values(service, spreadsheet_id: str, range_name: str, values: list[list[Any]]) -> None:
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def clear_values(service, spreadsheet_id: str, range_name: str) -> None:
    service.spreadsheets().values().clear(spreadsheetId=spreadsheet_id, range=range_name, body={}).execute()


def ensure_sheet(service, spreadsheet_id: str, title: str, headers: list[str]) -> None:
    metadata = get_spreadsheet(service, spreadsheet_id)
    sheet = find_sheet(metadata, title)
    if not sheet:
        batch_update(service, spreadsheet_id, [{"addSheet": {"properties": {"title": title}}}])
        metadata = get_spreadsheet(service, spreadsheet_id)
        sheet = find_sheet(metadata, title)

    current = get_values(service, spreadsheet_id, sheet_range(title, "1:1"))
    current_headers = [str(value).strip() for value in current[0]] if current else []
    if current_headers[: len(headers)] != headers:
        update_values(service, spreadsheet_id, sheet_range(title, f"A1:{col_letter(len(headers))}1"), [headers])

    if sheet:
        batch_update(
            service,
            spreadsheet_id,
            [
                {
                    "updateSheetProperties": {
                        "properties": {
                            "sheetId": sheet["sheetId"],
                            "gridProperties": {"frozenRowCount": 1},
                        },
                        "fields": "gridProperties.frozenRowCount",
                    }
                }
            ],
        )


def ensure_schema(service, spreadsheet_id: str) -> None:
    ensure_sheet(service, spreadsheet_id, "Posts", POST_HEADERS)
    ensure_sheet(service, spreadsheet_id, "Post Comments", COMMENT_HEADERS)
    ensure_sheet(service, spreadsheet_id, "Post Summary", SUMMARY_HEADERS)


def header_map(service, spreadsheet_id: str, sheet_name: str, headers: list[str]) -> dict[str, int]:
    rows = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A1:{col_letter(len(headers))}1"))
    row = rows[0] if rows else []
    return {str(header).strip(): index + 1 for index, header in enumerate(row) if header}


def existing_ids(service, spreadsheet_id: str, sheet_name: str, headers: list[str], id_field: str) -> set[str]:
    ensure_sheet(service, spreadsheet_id, sheet_name, headers)
    mapping = header_map(service, spreadsheet_id, sheet_name, headers)
    id_column = mapping.get(id_field)
    if not id_column:
        return set()

    rows = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A2:{col_letter(len(headers))}"))
    output = set()
    for row in rows:
        padded = pad_row(row, len(headers))
        value = str(padded[id_column - 1] or "").strip()
        if value:
            output.add(value)
    return output


def upsert_by_key(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    key_fields: list[str],
    rows: list[dict[str, Any]],
) -> None:
    ensure_sheet(service, spreadsheet_id, sheet_name, headers)
    if not rows:
        return

    existing = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A2:{col_letter(len(headers))}"))
    existing_keys: dict[str, int] = {}
    mapping = header_map(service, spreadsheet_id, sheet_name, headers)

    for row_index, row in enumerate(existing, start=2):
        padded = pad_row(row, len(headers))
        key = row_key({header: padded[mapping[header] - 1] for header in key_fields if header in mapping}, key_fields)
        if key.strip(":"):
            existing_keys[key] = row_index

    updates = []
    appends = []
    for row in rows:
        values = [serialize_cell(row.get(header, "")) for header in headers]
        key = row_key(row, key_fields)
        existing_row = existing_keys.get(key)
        if existing_row:
            updates.append(
                {
                    "range": sheet_range(sheet_name, f"A{existing_row}:{col_letter(len(headers))}{existing_row}"),
                    "values": [values],
                }
            )
        else:
            appends.append(values)

    if updates:
        service.spreadsheets().values().batchUpdate(
            spreadsheetId=spreadsheet_id,
            body={"valueInputOption": "RAW", "data": updates},
        ).execute()
    if appends:
        append_values(service, spreadsheet_id, sheet_range(sheet_name, f"A:{col_letter(len(headers))}"), appends)


def serialize_cell(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    return value


def row_key(row: dict[str, Any], fields: list[str]) -> str:
    return ":".join(str(row.get(field, "")) for field in fields)


def graph_url(path: str, params: dict[str, Any], access_token: str) -> str:
    query_params = dict(params)
    query_params["access_token"] = access_token
    return f"https://graph.facebook.com/{CONFIG['graph_version']}{path}?{urlencode(query_params)}"


def fetch_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=60)
    try:
        data = response.json() if response.text else {}
    except ValueError as exc:
        raise RuntimeError(f"Meta API returned non-JSON response: {response.text[:500]}") from exc

    if not response.ok:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(f"Meta API error {response.status_code}: {error.get('message') or response.text[:500]}")
    return data


def get_all_pages(path: str, params: dict[str, Any], access_token: str, max_items: int) -> list[dict[str, Any]]:
    output = []
    url = graph_url(path, params, access_token)
    while url:
        data = fetch_json(url)
        for item in data.get("data", []):
            output.append(item)
            if max_items and len(output) >= max_items:
                return output
        url = data.get("paging", {}).get("next", "")
    return output


def to_unix(date_value: datetime) -> int:
    return int(date_value.timestamp())


def parse_meta_date(value: str) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("+0000", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def is_within(date_value: str, since_date: datetime, until_date: datetime) -> bool:
    parsed = parse_meta_date(date_value)
    return bool(parsed and since_date <= parsed <= until_date)


def clean_text(value: Any) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()


def truncate_text(value: Any) -> str:
    text = clean_text(value)
    limit = CONFIG["analysis_text_chars"]
    return text[:limit]


def extract_hashtags(text: str) -> list[str]:
    return sorted(set(re.findall(r"#[\w\u0590-\u05FF]+", text or "")))


def first_summary_total(data: dict[str, Any], key: str) -> int:
    return int(data.get(key, {}).get("summary", {}).get("total_count") or 0)


def resolve_page_access_token(page_id: str, access_token: str) -> str:
    accounts = fetch_json(graph_url("/me/accounts", {"fields": "id,name,access_token", "limit": 100}, access_token))
    for page in accounts.get("data", []):
        if str(page.get("id")) == str(page_id) and page.get("access_token"):
            return page["access_token"]
    return access_token


def discover_meta_context(access_token: str) -> dict[str, str]:
    context = {
        "page_id": CONFIG["page_id"],
        "page_access_token": access_token,
        "ig_user_id": CONFIG["ig_user_id"],
        "ad_account_id": CONFIG["ad_account_id"],
        "instagram_username": CONFIG["greenpeace_instagram_username"],
    }

    if not context["page_id"] or not context["ig_user_id"] or not context["instagram_username"]:
        try:
            accounts = fetch_json(
                graph_url(
                    "/me/accounts",
                    {
                        "fields": "id,name,access_token,instagram_business_account{id,username}",
                        "limit": 100,
                    },
                    access_token,
                )
            )
            pages = accounts.get("data", [])
        except RuntimeError:
            page = fetch_json(
                graph_url(
                    "/me",
                    {"fields": "id,name,instagram_business_account{id,username}"},
                    access_token,
                )
            )
            pages = [page] if page.get("id") else []
        selected_page = None
        for page in pages:
            if context["page_id"] and str(page.get("id")) == str(context["page_id"]):
                selected_page = page
                break
        if not selected_page and pages:
            selected_page = pages[0]

        if selected_page:
            context["page_id"] = context["page_id"] or str(selected_page.get("id") or "")
            context["page_access_token"] = selected_page.get("access_token") or access_token
            instagram = selected_page.get("instagram_business_account") or {}
            context["ig_user_id"] = context["ig_user_id"] or str(instagram.get("id") or "")
            context["instagram_username"] = context["instagram_username"] or str(instagram.get("username") or "")

    if not context["ad_account_id"] and CONFIG["max_ads"] > 0:
        ad_accounts = fetch_json(
            graph_url(
                "/me/adaccounts",
                {"fields": "id,name,account_id", "limit": 25},
                access_token,
            )
        )
        first_account = (ad_accounts.get("data") or [{}])[0]
        context["ad_account_id"] = str(first_account.get("id") or "")

    CONFIG["greenpeace_facebook_page_id"] = context["page_id"]
    CONFIG["greenpeace_instagram_username"] = context["instagram_username"]
    return context


def facebook_post_fields(comment_since: datetime, until: datetime) -> str:
    return ",".join(
        [
            "id",
            "message",
            "created_time",
            "permalink_url",
            "attachments{media_type,type}",
            "reactions.limit(0).summary(true)",
            "likes.limit(0).summary(true)",
        ]
    )


def fetch_facebook_posts(page_id: str, post_since: datetime, until: datetime, comment_since: datetime, access_token: str):
    if not page_id or CONFIG["max_facebook_posts"] <= 0:
        return []
    return get_all_pages(
        f"/{page_id}/feed",
        {
            "fields": facebook_post_fields(comment_since, until),
            "since": to_unix(post_since),
            "until": to_unix(until),
            "limit": CONFIG["max_facebook_posts"],
        },
        access_token,
        CONFIG["max_facebook_posts"],
    )


def fetch_instagram_media(ig_user_id: str, access_token: str):
    if not ig_user_id or CONFIG["max_instagram_media"] <= 0:
        return []
    fields = ",".join(
        [
            "id",
            "caption",
            "timestamp",
            "permalink",
            "media_type",
            "like_count",
            "comments_count",
        ]
    )
    return get_all_pages(
        f"/{ig_user_id}/media",
        {"fields": fields, "limit": CONFIG["max_instagram_media"]},
        access_token,
        CONFIG["max_instagram_media"],
    )


def fetch_facebook_comments_with_replies(object_id: str, since_date: datetime, until_date: datetime, access_token: str):
    replies = f"comments.limit({CONFIG['max_replies_per_comment']}){{{FACEBOOK_COMMENT_FIELDS}}}"
    fields = f"{FACEBOOK_COMMENT_FIELDS},{replies}"
    comments = get_all_pages(
        f"/{object_id}/comments",
        {
            "fields": fields,
            "filter": "stream",
            "order": "chronological",
            "since": to_unix(since_date),
            "until": to_unix(until_date),
            "limit": CONFIG["max_comments_per_post"],
        },
        access_token,
        CONFIG["max_comments_per_post"],
    )
    return [comment for comment in comments if is_within(comment.get("created_time", ""), since_date, until_date)]


def fetch_instagram_comments(media_id: str, since_date: datetime, until_date: datetime, access_token: str):
    replies = f"replies.limit({CONFIG['max_replies_per_comment']}){{id,text,timestamp,username,like_count}}"
    fields = f"id,text,timestamp,username,like_count,{replies}"
    comments = get_all_pages(
        f"/{media_id}/comments",
        {"fields": fields, "limit": CONFIG["max_comments_per_post"]},
        access_token,
        CONFIG["max_comments_per_post"],
    )
    return [comment for comment in comments if is_within(comment.get("timestamp", ""), since_date, until_date)]


def fetch_ads(ad_account_id: str, access_token: str):
    if not ad_account_id or CONFIG["max_ads"] <= 0:
        return []
    return get_all_pages(
        f"/{ad_account_id}/ads",
        {"fields": "id,name,campaign{name},creative{id,effective_object_story_id,effective_instagram_media_id}", "limit": CONFIG["max_ads"]},
        access_token,
        CONFIG["max_ads"],
    )


def fetch_facebook_object(object_id: str, access_token: str):
    return fetch_json(
        graph_url(
            f"/{object_id}",
            {
                "fields": "id,message,created_time,permalink_url,attachments{media_type,type},reactions.limit(0).summary(true),likes.limit(0).summary(true)"
            },
            access_token,
        )
    )


def fetch_facebook_comments(object_id: str, since_date: datetime, until_date: datetime, access_token: str):
    comments = get_all_pages(
        f"/{object_id}/comments",
        {
            "fields": FACEBOOK_COMMENT_FIELDS,
            "filter": "stream",
            "order": "chronological",
            "since": to_unix(since_date),
            "until": to_unix(until_date),
            "limit": CONFIG["max_comments_per_post"],
        },
        access_token,
        CONFIG["max_comments_per_post"],
    )
    return [comment for comment in comments if is_within(comment.get("created_time", ""), since_date, until_date)]


def fetch_instagram_media_by_id(media_id: str, access_token: str):
    replies = (
        f"replies.limit({CONFIG['max_replies_per_comment']})"
        "{id,text,timestamp,username,like_count}"
    )
    comments = (
        f"comments.limit({CONFIG['max_comments_per_post']})"
        f"{{id,text,timestamp,username,like_count,{replies}}}"
    )
    return fetch_json(
        graph_url(
            f"/{media_id}",
            {
                "fields": f"id,caption,timestamp,permalink,media_type,like_count,comments_count,{comments}"
            },
            access_token,
        )
    )


def media_type_from_facebook(post: dict[str, Any]) -> str:
    attachment = (post.get("attachments", {}).get("data") or [{}])[0]
    raw = str(attachment.get("media_type") or attachment.get("type") or "").lower()
    if "video" in raw:
        return "video"
    if "album" in raw or "carousel" in raw or "multi" in raw:
        return "carousel"
    if "photo" in raw or "image" in raw:
        return "image"
    return "text_only" if not clean_text(post.get("message")) else "text_only"


def media_type_from_instagram(media: dict[str, Any]) -> str:
    raw = str(media.get("media_type") or "").upper()
    if raw == "VIDEO":
        return "reel"
    if raw == "CAROUSEL_ALBUM":
        return "carousel"
    if raw == "IMAGE":
        return "image"
    return "text_only"


def is_greenpeace_comment(comment: dict[str, Any], platform: str) -> bool:
    if platform == "facebook":
        commenter_id = str(comment.get("commenter_id") or "")
        return bool(CONFIG["greenpeace_facebook_page_id"] and commenter_id == str(CONFIG["greenpeace_facebook_page_id"]))
    username = str(comment.get("commenter_name") or "").lower()
    official = CONFIG["greenpeace_instagram_username"].lower()
    return bool(official and username == official)


def add_comment(
    comments: list[dict[str, Any]],
    seen: set[str],
    platform: str,
    post_id: str,
    raw_comment: dict[str, Any],
    parent_comment_id: str,
    post_url: str,
) -> None:
    comment_id = str(raw_comment.get("id") or "")
    if not comment_id or comment_id in seen:
        return
    seen.add(comment_id)

    if platform == "facebook":
        message = raw_comment.get("message", "")
        created_time = raw_comment.get("created_time", "")
        commenter_name = raw_comment.get("from", {}).get("name", "")
        commenter_id = raw_comment.get("from", {}).get("id", "")
        comment_url = raw_comment.get("permalink_url", "")
        reply_count = raw_comment.get("comment_count", 0)
    else:
        message = raw_comment.get("text", "")
        created_time = raw_comment.get("timestamp", "")
        commenter_name = raw_comment.get("username", "")
        commenter_id = ""
        comment_url = post_url
        reply_count = len(raw_comment.get("replies", {}).get("data", []))

    comments.append(
        {
            "comment_id": comment_id,
            "post_id": post_id,
            "parent_comment_id": parent_comment_id or raw_comment.get("parent", {}).get("id", ""),
            "commenter_id": commenter_id,
            "commenter_name": commenter_name,
            "comment_created_time": created_time,
            "comment_message": clean_text(message),
            "comment_url": comment_url,
            "like_count": raw_comment.get("like_count", 0),
            "reply_count": reply_count,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "comment_emotions": ["Neutral"],
            "Emotion_Confidence": 0.0,
            "comment_stance": "unclear",
            "comment_intent": "other",
            "is_sarcastic": False,
            "requires_response": False,
            "response_priority": "none",
        }
    )


def collect_rows(
    existing_post_ids: set[str] | None = None,
    existing_comment_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    existing_post_ids = existing_post_ids or set()
    existing_comment_ids = existing_comment_ids or set()
    access_token = meta_access_token()
    now = datetime.now(timezone.utc)
    post_since = now - timedelta(days=CONFIG["post_lookback_days"])
    comment_since = now - timedelta(days=CONFIG["comment_lookback_days"])
    meta_context = discover_meta_context(access_token)
    page_token = meta_context["page_access_token"]

    posts: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    seen_comments: set[str] = set(existing_comment_ids)

    for post in fetch_facebook_posts(meta_context["page_id"], post_since, now, comment_since, page_token):
        post_id = post.get("id", "")
        if not post_id or post_id in existing_post_ids:
            continue

        post_comments = fetch_facebook_comments_with_replies(post_id, comment_since, now, page_token)
        for comment in post_comments:
            add_comment(comments, seen_comments, "facebook", post_id, comment, "", post.get("permalink_url", ""))
            for reply in comment.get("comments", {}).get("data", []):
                add_comment(comments, seen_comments, "facebook", post_id, reply, comment.get("id", ""), post.get("permalink_url", ""))

        posts.append(
            make_post_row(
                post_id=post_id,
                platform="facebook",
                created_time=post.get("created_time", ""),
                message=post.get("message", ""),
                url=post.get("permalink_url", ""),
                source_type="organic",
                campaign_name="",
                ad_id="",
                media_type=media_type_from_facebook(post),
                like_count=first_summary_total(post, "reactions") or first_summary_total(post, "likes"),
                collected_window_start=comment_since,
                collected_window_end=now,
                comments=[comment for comment in comments if comment["post_id"] == post_id],
            )
        )

    for media in fetch_instagram_media(meta_context["ig_user_id"], access_token):
        post_id = media.get("id", "")
        if not post_id or post_id in existing_post_ids:
            continue

        post_comments = fetch_instagram_comments(post_id, comment_since, now, access_token)
        for comment in post_comments:
            add_comment(comments, seen_comments, "instagram", post_id, comment, "", media.get("permalink", ""))
            for reply in comment.get("replies", {}).get("data", []):
                if is_within(reply.get("timestamp", ""), comment_since, now):
                    add_comment(comments, seen_comments, "instagram", post_id, reply, comment.get("id", ""), media.get("permalink", ""))

        posts.append(
            make_post_row(
                post_id=post_id,
                platform="instagram",
                created_time=media.get("timestamp", ""),
                message=media.get("caption", ""),
                url=media.get("permalink", ""),
                source_type="organic",
                campaign_name="",
                ad_id="",
                media_type=media_type_from_instagram(media),
                like_count=media.get("like_count", 0),
                collected_window_start=comment_since,
                collected_window_end=now,
                comments=[comment for comment in comments if comment["post_id"] == post_id],
            )
        )

    for ad in fetch_ads(meta_context["ad_account_id"], access_token):
        creative = ad.get("creative", {})
        campaign_name = ad.get("campaign", {}).get("name", "")
        ad_id = ad.get("id", "")
        if creative.get("effective_object_story_id"):
            object_id = creative["effective_object_story_id"]
            if object_id not in existing_post_ids:
                ad_post = fetch_facebook_object(object_id, page_token)
                ad_comments = fetch_facebook_comments(object_id, comment_since, now, page_token)
                for comment in ad_comments:
                    add_comment(comments, seen_comments, "facebook", object_id, comment, "", ad_post.get("permalink_url", ""))
                posts.append(
                    make_post_row(
                        post_id=object_id,
                        platform="facebook",
                        created_time=ad_post.get("created_time", ""),
                        message=ad_post.get("message", ""),
                        url=ad_post.get("permalink_url", ""),
                        source_type="paid",
                        campaign_name=campaign_name,
                        ad_id=ad_id,
                        media_type=media_type_from_facebook(ad_post),
                        like_count=first_summary_total(ad_post, "reactions") or first_summary_total(ad_post, "likes"),
                        collected_window_start=comment_since,
                        collected_window_end=now,
                        comments=[comment for comment in comments if comment["post_id"] == object_id],
                    )
                )
        if creative.get("effective_instagram_media_id"):
            media_id = creative["effective_instagram_media_id"]
            if media_id not in existing_post_ids:
                ad_media = fetch_instagram_media_by_id(media_id, access_token)
                for comment in ad_media.get("comments", {}).get("data", []):
                    if is_within(comment.get("timestamp", ""), comment_since, now):
                        add_comment(comments, seen_comments, "instagram", media_id, comment, "", ad_media.get("permalink", ""))
                posts.append(
                    make_post_row(
                        post_id=media_id,
                        platform="instagram",
                        created_time=ad_media.get("timestamp", ""),
                        message=ad_media.get("caption", ""),
                        url=ad_media.get("permalink", ""),
                        source_type="paid",
                        campaign_name=campaign_name,
                        ad_id=ad_id,
                        media_type=media_type_from_instagram(ad_media),
                        like_count=ad_media.get("like_count", 0),
                        collected_window_start=comment_since,
                        collected_window_end=now,
                        comments=[comment for comment in comments if comment["post_id"] == media_id],
                    )
                )

    return posts, comments


def make_post_row(
    post_id: str,
    platform: str,
    created_time: str,
    message: str,
    url: str,
    source_type: str,
    campaign_name: str,
    ad_id: str,
    media_type: str,
    like_count: int,
    collected_window_start: datetime,
    collected_window_end: datetime,
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    reply_count = sum(1 for comment in comments if comment.get("parent_comment_id"))
    return {
        "post_id": post_id,
        "platform": platform,
        "post_created_time": created_time,
        "post_message": clean_text(message),
        "post_url": url,
        "post_hashtags": extract_hashtags(message),
        "canonical_topic": "",
        "canonical_subtopic": "",
        "topic_source": "unknown",
        "topic_confidence": 0.0,
        "source_type": source_type,
        "campaign_name": campaign_name,
        "ad_id": ad_id,
        "media_type": media_type,
        "collected_comment_count": len(comments),
        "collected_greenpeace_comment_count": sum(1 for comment in comments if is_greenpeace_comment(comment, platform)),
        "collected_like_count": like_count,
        "collected_reply_count": reply_count,
        "collected_window_start": collected_window_start.date().isoformat(),
        "collected_window_end": collected_window_end.date().isoformat(),
        "post_emotions": ["Neutral"],
    }


def gemini_generate_json(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    api_key = required_env("GEMINI_API_KEY")
    url, payload, headers = gemini_request(prompt, api_key)
    retryable_statuses = {429, 500, 502, 503, 504}
    max_retries = max(0, CONFIG["gemini_max_retries"])

    for attempt in range(max_retries + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=120)
            data = response.json() if response.text else {}
        except (requests.RequestException, ValueError) as exc:
            if attempt >= max_retries:
                return gemini_fallback_or_raise(f"Gemini request failed: {exc}")
            sleep_before_gemini_retry(attempt)
            continue

        if response.ok:
            try:
                return extract_gemini_json(data)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                return gemini_fallback_or_raise(f"Gemini returned invalid JSON: {exc}")

        message = json.dumps(data, ensure_ascii=False)[:1000]
        if response.status_code not in retryable_statuses or attempt >= max_retries:
            return gemini_fallback_or_raise(f"Gemini API error {response.status_code}: {message}")
        sleep_before_gemini_retry(attempt)

    return gemini_fallback_or_raise("Gemini API failed after retries.")


def gemini_request(prompt: str, api_key: str) -> tuple[str, dict[str, Any], dict[str, str]]:
    if CONFIG["gemini_api_mode"] == "interactions":
        return (
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            {
                "model": CONFIG["gemini_model"],
                "input": prompt,
            },
            {
                "x-goog-api-key": api_key,
                "Content-Type": "application/json",
            },
        )

    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{CONFIG['gemini_model']}:generateContent?key={api_key}",
        {
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "responseMimeType": "application/json",
            },
        },
        {"Content-Type": "application/json"},
    )


def extract_gemini_json(data: dict[str, Any]) -> dict[str, Any]:
    text = ""
    if data.get("candidates"):
        text = data["candidates"][0]["content"]["parts"][0]["text"]
    elif data.get("output"):
        for output in data.get("output", []):
            for content in output.get("content", []):
                if content.get("type") == "text" and content.get("text"):
                    text += content["text"]

    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            text = text[start : end + 1]
    return json.loads(text)


def sleep_before_gemini_retry(attempt: int) -> None:
    delay = CONFIG["gemini_retry_base_seconds"] * (2 ** attempt)
    print(f"Gemini is temporarily unavailable. Retrying in {delay} seconds...")
    time.sleep(delay)


def gemini_fallback_or_raise(message: str) -> dict[str, Any]:
    if CONFIG["gemini_fallback_on_error"]:
        print(f"WARNING: {message}")
        print("Continuing without Gemini analysis for this batch; default analysis values will be used.")
        return {}
    raise RuntimeError(message)


def analyze_posts(posts: list[dict[str, Any]]) -> None:
    if not posts:
        return
    schema = {
        "type": "OBJECT",
        "properties": {
            "posts": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "post_id": {"type": "STRING"},
                        "canonical_topic": {"type": "STRING"},
                        "canonical_subtopic": {"type": "STRING"},
                        "topic_source": {"type": "STRING", "enum": TOPIC_SOURCES},
                        "topic_confidence": {"type": "NUMBER"},
                        "post_emotions": {"type": "ARRAY", "items": {"type": "STRING", "enum": POST_EMOTIONS}},
                    },
                    "required": [
                        "post_id",
                        "canonical_topic",
                        "canonical_subtopic",
                        "topic_source",
                        "topic_confidence",
                        "post_emotions",
                    ],
                },
            }
        },
        "required": ["posts"],
    }
    items = [
        {
            "post_id": post["post_id"],
            "platform": post["platform"],
            "source_type": post["source_type"],
            "campaign_name": post["campaign_name"],
            "hashtags": post["post_hashtags"],
            "message": truncate_text(post["post_message"]),
        }
        for post in posts
    ]
    prompt = (
        "Analyze Greenpeace Israel social posts. Return only valid JSON in this exact shape: "
        '{"posts":[{"post_id":"...","canonical_topic":"...","canonical_subtopic":"...",'
        '"topic_source":"hashtags|campaign_name|ad_text|ai_classification|manual|unknown",'
        '"topic_confidence":0.0,"post_emotions":["Neutral"]}]}. '
        "Use short snake_case English labels for canonical_topic and canonical_subtopic. "
        "topic_source must indicate the strongest source used: hashtags, campaign_name, ad_text, "
        "ai_classification, manual, or unknown. Emotions must come from the allowed list.\n\n"
        f"Posts:\n{json.dumps(items, ensure_ascii=False)}"
    )
    analysis = {item["post_id"]: item for item in gemini_generate_json(prompt, schema).get("posts", [])}
    for post in posts:
        item = analysis.get(post["post_id"], {})
        post["canonical_topic"] = clean_text(item.get("canonical_topic", "")) or "unknown"
        post["canonical_subtopic"] = clean_text(item.get("canonical_subtopic", "")) or "unknown"
        post["topic_source"] = allowed_value(item.get("topic_source"), TOPIC_SOURCES, "unknown")
        post["topic_confidence"] = bounded_float(item.get("topic_confidence"), 0.0, 1.0)
        post["post_emotions"] = allowed_list(item.get("post_emotions"), POST_EMOTIONS, ["Neutral"])


def analyze_comments(comments: list[dict[str, Any]], posts: list[dict[str, Any]]) -> None:
    if not comments:
        return
    post_context = {post["post_id"]: truncate_text(post["post_message"]) for post in posts}
    schema = {
        "type": "OBJECT",
        "properties": {
            "comments": {
                "type": "ARRAY",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "comment_id": {"type": "STRING"},
                        "comment_emotions": {"type": "ARRAY", "items": {"type": "STRING", "enum": COMMENT_EMOTIONS}},
                        "emotion_confidence": {"type": "NUMBER"},
                        "comment_stance": {"type": "STRING", "enum": COMMENT_STANCES},
                        "comment_intent": {"type": "STRING", "enum": COMMENT_INTENTS},
                        "is_sarcastic": {"type": "BOOLEAN"},
                        "requires_response": {"type": "BOOLEAN"},
                        "response_priority": {"type": "STRING", "enum": RESPONSE_PRIORITIES},
                    },
                    "required": [
                        "comment_id",
                        "comment_emotions",
                        "emotion_confidence",
                        "comment_stance",
                        "comment_intent",
                        "is_sarcastic",
                        "requires_response",
                        "response_priority",
                    ],
                },
            }
        },
        "required": ["comments"],
    }
    for batch_start in range(0, len(comments), CONFIG["analysis_batch_size"]):
        batch = comments[batch_start : batch_start + CONFIG["analysis_batch_size"]]
        items = [
            {
                "comment_id": comment["comment_id"],
                "post_id": comment["post_id"],
                "post_context": post_context.get(comment["post_id"], ""),
                "comment_message": truncate_text(comment["comment_message"]),
            }
            for comment in batch
        ]
        prompt = (
            "Analyze Hebrew/English social media comments for Greenpeace Israel. "
            "Return only valid JSON in this exact shape: "
            '{"comments":[{"comment_id":"...","comment_emotions":["Neutral"],"emotion_confidence":0.0,'
            '"comment_stance":"supportive|opposed|skeptical|neutral|unclear",'
            '"comment_intent":"support|criticism|question|mockery|information_request|personal_story|tag_friend|political_attack|spam|other",'
            '"is_sarcastic":false,"requires_response":false,"response_priority":"none|low|medium|high"}]}. '
            "Use the closed lists exactly. "
            "requires_response should be true for good-faith questions, information requests, serious criticism, "
            "or safety/reputation issues. Use high priority only when immediate organizational response is important.\n\n"
            f"Comments:\n{json.dumps(items, ensure_ascii=False)}"
        )
        analysis = {item["comment_id"]: item for item in gemini_generate_json(prompt, schema).get("comments", [])}
        for comment in batch:
            item = analysis.get(comment["comment_id"], {})
            comment["comment_emotions"] = allowed_list(item.get("comment_emotions"), COMMENT_EMOTIONS, ["Neutral"])
            comment["Emotion_Confidence"] = bounded_float(item.get("emotion_confidence"), 0.0, 1.0)
            comment["comment_stance"] = allowed_value(item.get("comment_stance"), COMMENT_STANCES, "unclear")
            comment["comment_intent"] = allowed_value(item.get("comment_intent"), COMMENT_INTENTS, "other")
            comment["is_sarcastic"] = bool(item.get("is_sarcastic", False))
            comment["requires_response"] = bool(item.get("requires_response", False))
            comment["response_priority"] = allowed_value(item.get("response_priority"), RESPONSE_PRIORITIES, "none")


def allowed_value(value: Any, allowed: list[str], default: str) -> str:
    text = str(value or "").strip()
    return text if text in allowed else default


def allowed_list(values: Any, allowed: list[str], default: list[str]) -> list[str]:
    if not isinstance(values, list):
        return default
    filtered = [str(value) for value in values if str(value) in allowed]
    return filtered or default


def bounded_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return minimum
    return min(max(parsed, minimum), maximum)


def summarize_posts(posts: list[dict[str, Any]], comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comments_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comment in comments:
        comments_by_post[comment["post_id"]].append(comment)

    summaries = []
    for post in posts:
        post_id = post["post_id"]
        post_comments = comments_by_post.get(post_id, [])
        total = len(post_comments)
        stance_counter = Counter(comment.get("comment_stance", "unclear") for comment in post_comments)
        intent_counter = Counter(comment.get("comment_intent", "other") for comment in post_comments)
        requires_response_count = sum(1 for comment in post_comments if comment.get("requires_response"))

        summaries.append(
            {
                "post_id": post_id,
                "total_comments": total,
                "dominant_comment_stance": most_common(stance_counter, "unclear"),
                "dominant_comment_intent": most_common(intent_counter, "other"),
                "supportive_comment_rate": rate(stance_counter["supportive"], total),
                "opposed_comment_rate": rate(stance_counter["opposed"], total),
                "skeptical_comment_rate": rate(stance_counter["skeptical"], total),
                "neutral_comment_rate": rate(stance_counter["neutral"], total),
                "question_rate": rate(intent_counter["question"] + intent_counter["information_request"], total),
                "criticism_rate": rate(intent_counter["criticism"] + intent_counter["political_attack"], total),
                "mockery_rate": rate(intent_counter["mockery"], total),
                "requires_response_count": requires_response_count,
                "requires_response_rate": rate(requires_response_count, total),
            }
        )
    return summaries


def most_common(counter: Counter, default: str) -> str:
    return counter.most_common(1)[0][0] if counter else default


def rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


def sync() -> None:
    spreadsheet_id = required_env("SPREADSHEET_ID")
    service = get_sheets_service()
    ensure_schema(service, spreadsheet_id)

    known_post_ids = existing_ids(service, spreadsheet_id, "Posts", POST_HEADERS, "post_id")
    known_comment_ids = existing_ids(service, spreadsheet_id, "Post Comments", COMMENT_HEADERS, "comment_id")
    print(f"Found {len(known_post_ids)} existing posts and {len(known_comment_ids)} existing comments in Google Sheets.")

    posts, comments = collect_rows(known_post_ids, known_comment_ids)
    if env_bool("ANALYZE_WITH_GEMINI", True):
        analyze_posts(posts)
        analyze_comments(comments, posts)

    summaries = summarize_posts(posts, comments)
    upsert_by_key(service, spreadsheet_id, "Posts", POST_HEADERS, ["post_id"], posts)
    upsert_by_key(service, spreadsheet_id, "Post Comments", COMMENT_HEADERS, ["comment_id"], comments)
    upsert_by_key(service, spreadsheet_id, "Post Summary", SUMMARY_HEADERS, ["post_id"], summaries)
    print(f"Synced {len(posts)} posts, {len(comments)} comments/replies, {len(summaries)} summaries.")


if __name__ == "__main__":
    sync()
