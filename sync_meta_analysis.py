import json
import os
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-lite"
RECOMMENDED_RESPONSE_SCORE_THRESHOLD = 0.70
DEFAULT_MAX_RUNTIME_SECONDS = 15 * 60
WRITE_RESERVE_SECONDS = 90
ANALYSIS_RESERVE_SECONDS = 4 * 60
SHEETS_API_RETRIES = 3


class DeadlineReached(RuntimeError):
    pass
LEGACY_GEMINI_MODEL_UPGRADES = {
    "gemini-2.5-flash-lite": DEFAULT_GEMINI_MODEL,
}
POSTS_SHEET_NAME = "Posts/Ads"
POST_COMMENTS_SHEET_NAME = "Post/Ad Comments"
POST_SUMMARY_SHEET_NAME = "Post/Ad Summary"
LEGACY_POSTS_SHEET_NAMES = ["Posts"]
LEGACY_POST_COMMENTS_SHEET_NAMES = ["Post Comments", "Meta Comments", "Facebook Comments"]
LEGACY_POST_SUMMARY_SHEET_NAMES = ["Post Summary"]

POST_HEADERS = [
    "post_id",
    "platform",
    "source_type",
    "campaign_name",
    "ad_id",
    "post_created_time",
    "post_url",
    "media_type",
    "post_message",
    "post_hashtags",
    "canonical_topic",
    "canonical_subtopic",
    "topic_source",
    "topic_confidence",
    "post_emotions",
    "post_sentiment",
    "collected_like_count",
    "collected_comment_count",
    "collected_greenpeace_comment_count",
    "collected_reply_count",
    "collected_window_start",
    "collected_window_end",
]

COMMENT_HEADERS = [
    "comment_id",
    "post_id",
    "parent_comment_id",
    "parent_comment_message",
    "comment_created_time",
    "collected_at",
    "commenter_id",
    "commenter_name",
    "is_brand_comment",
    "comment_message",
    "comment_url",
    "like_count",
    "reply_count",
    "comment_sentiment",
    "comment_emotions",
    "emotion_confidence",
    "comment_stance",
    "comment_intent",
    "is_sarcastic",
    "response_value_score",
]

SUMMARY_HEADERS = [
    "post_id",
    "ad_id",
    "campaign_name",
    "post_url",
    "canonical_topic",
    "canonical_subtopic",
    "collected_comment_count",
    "collected_greenpeace_comment_count",
    "collected_like_count",
    "total_comments",
    "requires_response_count",
    "requires_response_rate",
    "dominant_comment_stance",
    "supportive_comment_rate",
    "opposed_comment_rate",
    "skeptical_comment_rate",
    "neutral_comment_rate",
    "dominant_comment_intent",
    "service_request_count",
    "service_request_rate",
    "question_rate",
    "criticism_rate",
    "mockery_rate",
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

POST_EMOTIONS = [
    "Urgency",
    "Concern",
    "Outrage",
    "Anger",
    "Grief",
    "Hope",
    "Empathy",
    "Alarm",
    "Determination",
    "Awe",
    "Sarcasm",
    "Amusement",
    "Neutral",
]
POST_SENTIMENTS = ["positive", "negative", "mixed", "neutral", "unclear"]
COMMENT_EMOTIONS = [
    "Admiration",
    "Sadness",
    "Skepticism",
    "Anger",
    "Anxiety",
    "Amusement",
    "Contempt",
    "Disgust",
    "Hostility",
    "Dismissiveness",
    "Confusion",
    "Concern",
    "Agreement",
    "Neutral",
]
TOPIC_SOURCES = ["hashtags", "campaign_name", "ad_text", "ai_classification", "manual", "unknown"]
COMMENT_SENTIMENTS = ["positive", "negative", "mixed", "neutral", "unclear"]
COMMENT_STANCES = ["supportive", "opposed", "skeptical", "neutral", "unclear"]
COMMENT_INTENTS = [
    "support",
    "criticism",
    "question",
    "mockery",
    "information_request",
    "answer",
    "service_request",
    "personal_story",
    "tag_friend",
    "political_attack",
    "spam",
    "other",
]
def env_value(name: str, default: str = "") -> str:
    return os.getenv(name) or default


def env_int(name: str, default: int) -> int:
    value = env_value(name, str(default))
    if not value:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a whole number") from exc


def env_bool(name: str, default: bool) -> bool:
    value = env_value(name, "true" if default else "false").lower()
    return value in {"1", "true", "yes", "y"}


def gemini_model_from_env() -> str:
    model = env_value("GEMINI_MODEL", DEFAULT_GEMINI_MODEL)
    upgraded_model = LEGACY_GEMINI_MODEL_UPGRADES.get(model)
    if upgraded_model:
        print(f"Upgrading legacy GEMINI_MODEL '{model}' to '{upgraded_model}'.")
        return upgraded_model
    return model


CONFIG = {
    "spreadsheet_id": env_value("SPREADSHEET_ID"),
    "page_id": "",
    "ig_user_id": "",
    "ad_account_id": "",
    "graph_version": env_value("META_GRAPH_VERSION", "v23.0"),
    "lookback_days": env_int("LOOKBACK_DAYS", 30),
    "timezone": env_value("TIMEZONE", "UTC"),
    "max_comments_per_run": env_int("MAX_COMMENTS_PER_RUN", 500),
    "max_content_items_per_run": env_int("MAX_CONTENT_ITEMS_PER_RUN", 100),
    "comments_per_existing_post_coverage": env_int("COMMENTS_PER_EXISTING_POST_COVERAGE", 5),
    "max_runtime_seconds": env_int("MAX_RUNTIME_SECONDS", DEFAULT_MAX_RUNTIME_SECONDS),
    "run_started_monotonic": 0.0,
    "run_deadline_monotonic": 0.0,
    "analysis_batch_size": env_int("GEMINI_BATCH_SIZE", 10),
    "analysis_text_chars": env_int("ANALYSIS_TEXT_CHARS", 1200),
    "gemini_max_retries": env_int("GEMINI_MAX_RETRIES", 3),
    "gemini_retry_base_seconds": env_int("GEMINI_RETRY_BASE_SECONDS", 20),
    "gemini_fallback_on_error": env_bool("GEMINI_FALLBACK_ON_ERROR", False),
    "gemini_model": gemini_model_from_env(),
    "gemini_api_mode": env_value("GEMINI_API_MODE", "generateContent"),
    "gemini_fallback_after_quota_error": env_bool("GEMINI_FALLBACK_AFTER_QUOTA_ERROR", False),
    "gemini_quota_exhausted": False,
    "gemini_quota_notice_printed": False,
    "greenpeace_facebook_page_id": "",
    "greenpeace_instagram_username": "",
}


def start_runtime_budget() -> None:
    max_runtime_seconds = CONFIG["max_runtime_seconds"]
    if max_runtime_seconds < WRITE_RESERVE_SECONDS + 60:
        raise RuntimeError(
            f"MAX_RUNTIME_SECONDS must be at least {WRITE_RESERVE_SECONDS + 60}"
        )
    started = time.monotonic()
    CONFIG["run_started_monotonic"] = started
    CONFIG["run_deadline_monotonic"] = started + max_runtime_seconds


def elapsed_seconds() -> int:
    started = float(CONFIG.get("run_started_monotonic") or 0)
    return int(time.monotonic() - started) if started else 0


def remaining_seconds() -> float:
    deadline = float(CONFIG.get("run_deadline_monotonic") or 0)
    return deadline - time.monotonic() if deadline else float("inf")


def collection_time_available() -> bool:
    return remaining_seconds() > ANALYSIS_RESERVE_SECONDS


def analysis_time_available() -> bool:
    return remaining_seconds() > WRITE_RESERVE_SECONDS + 5


def log_progress(stage: str, message: str) -> None:
    remaining = remaining_seconds()
    remaining_label = "unlimited" if remaining == float("inf") else f"{max(0, int(remaining))}s"
    print(
        f"[{elapsed_seconds():04d}s][{stage}] {message} "
        f"(remaining={remaining_label})",
        flush=True,
    )


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
        .execute(num_retries=SHEETS_API_RETRIES)
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
    ).execute(num_retries=SHEETS_API_RETRIES)


def batch_update_values(service, spreadsheet_id: str, updates: list[dict[str, Any]]) -> None:
    if not updates:
        return
    service.spreadsheets().values().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"valueInputOption": "RAW", "data": updates},
    ).execute(num_retries=SHEETS_API_RETRIES)


def get_values(service, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute(num_retries=SHEETS_API_RETRIES)
    )
    return response.get("values", [])


def clear_managed_rows(service, spreadsheet_id: str, sheet_name: str, headers: list[str], start_row: int = 2) -> None:
    clear_width = len(headers) * 2
    clear_values(service, spreadsheet_id, sheet_range(sheet_name, f"A{start_row}:{col_letter(clear_width)}"))


def clear_values(service, spreadsheet_id: str, range_name: str) -> None:
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        body={},
    ).execute(num_retries=SHEETS_API_RETRIES)


def delete_legacy_sheets(service, spreadsheet_id: str, legacy_titles: list[str]) -> None:
    metadata = get_spreadsheet(service, spreadsheet_id)
    requests = []
    for legacy_title in legacy_titles:
        legacy_sheet = find_sheet(metadata, legacy_title)
        if legacy_sheet:
            requests.append({"deleteSheet": {"sheetId": legacy_sheet["sheetId"]}})
    batch_update(service, spreadsheet_id, requests)


def ensure_sheet(
    service,
    spreadsheet_id: str,
    title: str,
    headers: list[str],
    legacy_titles: list[str] | None = None,
) -> None:
    metadata = get_spreadsheet(service, spreadsheet_id)
    sheet = find_sheet(metadata, title)
    if not sheet:
        for legacy_title in legacy_titles or []:
            legacy_sheet = find_sheet(metadata, legacy_title)
            if legacy_sheet:
                batch_update(
                    service,
                    spreadsheet_id,
                    [
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": legacy_sheet["sheetId"],
                                    "title": title,
                                },
                                "fields": "title",
                            }
                        }
                    ],
                )
                metadata = get_spreadsheet(service, spreadsheet_id)
                sheet = find_sheet(metadata, title)
                break

    if not sheet:
        batch_update(service, spreadsheet_id, [{"addSheet": {"properties": {"title": title}}}])
        metadata = get_spreadsheet(service, spreadsheet_id)
        sheet = find_sheet(metadata, title)

    delete_legacy_sheets(service, spreadsheet_id, legacy_titles or [])

    current = get_values(service, spreadsheet_id, sheet_range(title, "1:1"))
    current_headers = [str(value).strip() for value in current[0]] if current else []
    if current_headers[: len(headers)] != headers:
        migrate_sheet_headers(service, spreadsheet_id, title, current_headers, headers)

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


def migrate_sheet_headers(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    current_headers: list[str],
    desired_headers: list[str],
) -> None:
    if not any(current_headers):
        update_values(service, spreadsheet_id, sheet_range(sheet_name, f"A1:{col_letter(len(desired_headers))}1"), [desired_headers])
        return

    width = max(len(current_headers), len(desired_headers))
    header_index = {header: index for index, header in enumerate(current_headers) if header}
    if "Emotion_Confidence" in header_index and "emotion_confidence" not in header_index:
        header_index["emotion_confidence"] = header_index["Emotion_Confidence"]

    existing = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A2:{col_letter(width)}"))
    migrated = []
    for row in existing:
        padded = pad_row(row, width)
        migrated.append(
            [
                padded[header_index[header]]
                if header in header_index
                else default_cell_value(header)
                for header in desired_headers
            ]
        )
    zero_answered_scores_in_rows(migrated, desired_headers)

    update_values(service, spreadsheet_id, sheet_range(sheet_name, f"A1:{col_letter(len(desired_headers))}1"), [desired_headers])
    if migrated:
        update_values(
            service,
            spreadsheet_id,
            sheet_range(sheet_name, f"A2:{col_letter(len(desired_headers))}{len(migrated) + 1}"),
            migrated,
        )
    if width > len(desired_headers):
        clear_values(service, spreadsheet_id, sheet_range(sheet_name, f"{col_letter(len(desired_headers) + 1)}:{col_letter(width)}"))


def zero_answered_scores_in_rows(rows: list[list[Any]], headers: list[str]) -> None:
    required = {
        "comment_id",
        "post_id",
        "parent_comment_id",
        "comment_created_time",
        "is_brand_comment",
        "response_value_score",
    }
    if not required.issubset(headers):
        return

    index = {header: headers.index(header) for header in required}
    brand_reply_times_by_thread: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for row in rows:
        is_brand = str(row[index["is_brand_comment"]] or "").strip().lower() == "true"
        parent_id = str(row[index["parent_comment_id"]] or "")
        created_at = parse_meta_date(str(row[index["comment_created_time"]] or ""))
        if is_brand and parent_id and created_at:
            key = (str(row[index["post_id"]] or ""), parent_id)
            brand_reply_times_by_thread[key].append(created_at)

    for row in rows:
        is_brand = str(row[index["is_brand_comment"]] or "").strip().lower() == "true"
        created_at = parse_meta_date(str(row[index["comment_created_time"]] or ""))
        if is_brand or not created_at:
            continue
        comment_id = str(row[index["comment_id"]] or "")
        key = (str(row[index["post_id"]] or ""), comment_id)
        if any(reply_time > created_at for reply_time in brand_reply_times_by_thread.get(key, [])):
            row[index["response_value_score"]] = 0


def default_cell_value(header: str) -> str:
    if header in {"post_hashtags", "post_emotions", "comment_emotions"}:
        return "[]"
    if header == "topic_source":
        return "unknown"
    if header == "source_type":
        return "organic"
    if header in {"post_sentiment", "is_brand_comment", "comment_sentiment"}:
        return ""
    if header == "comment_stance":
        return "unclear"
    if header == "comment_intent":
        return "other"
    if header == "emotion_confidence":
        return "0"
    if header == "response_value_score":
        return "0"
    if header == "is_sarcastic":
        return "FALSE"
    return ""


def ensure_schema(service, spreadsheet_id: str) -> None:
    ensure_sheet(service, spreadsheet_id, POSTS_SHEET_NAME, POST_HEADERS, LEGACY_POSTS_SHEET_NAMES)
    ensure_sheet(service, spreadsheet_id, POST_COMMENTS_SHEET_NAME, COMMENT_HEADERS, LEGACY_POST_COMMENTS_SHEET_NAMES)
    ensure_sheet(service, spreadsheet_id, POST_SUMMARY_SHEET_NAME, SUMMARY_HEADERS, LEGACY_POST_SUMMARY_SHEET_NAMES)


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


def existing_comment_state(
    service,
    spreadsheet_id: str,
) -> tuple[set[str], dict[str, int]]:
    """Load existing comment IDs and audience counts per post in one Sheets read."""
    rows = get_values(
        service,
        spreadsheet_id,
        sheet_range(POST_COMMENTS_SHEET_NAME, f"A2:{col_letter(len(COMMENT_HEADERS))}"),
    )
    comment_id_index = COMMENT_HEADERS.index("comment_id")
    post_id_index = COMMENT_HEADERS.index("post_id")
    brand_index = COMMENT_HEADERS.index("is_brand_comment")
    comment_ids: set[str] = set()
    audience_counts: dict[str, int] = defaultdict(int)
    for raw_row in rows:
        row = pad_row(raw_row, len(COMMENT_HEADERS))
        comment_id = str(row[comment_id_index] or "").strip()
        post_id = str(row[post_id_index] or "").strip()
        is_brand = str(row[brand_index] or "").strip().lower() == "true"
        if comment_id:
            comment_ids.add(comment_id)
        if post_id and comment_id and not is_brand:
            audience_counts[post_id] += 1
    return comment_ids, dict(audience_counts)


def existing_post_state(
    service,
    spreadsheet_id: str,
) -> tuple[set[str], dict[str, datetime]]:
    """Load existing post IDs and their last completed comment-scan timestamps."""
    rows = get_values(
        service,
        spreadsheet_id,
        sheet_range(POSTS_SHEET_NAME, f"A2:{col_letter(len(POST_HEADERS))}"),
    )
    post_id_index = POST_HEADERS.index("post_id")
    window_end_index = POST_HEADERS.index("collected_window_end")
    post_ids: set[str] = set()
    last_scanned: dict[str, datetime] = {}
    for raw_row in rows:
        row = pad_row(raw_row, len(POST_HEADERS))
        post_id = str(row[post_id_index] or "").strip()
        if not post_id:
            continue
        post_ids.add(post_id)
        parsed = parse_meta_date(str(row[window_end_index] or ""))
        if parsed:
            last_scanned[post_id] = parsed
    return post_ids, last_scanned


def upsert_by_key(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    key_fields: list[str],
    rows: list[dict[str, Any]],
) -> tuple[int, int, int]:
    ensure_sheet(service, spreadsheet_id, sheet_name, headers)

    existing = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A2:{col_letter(len(headers) * 2)}"))
    existing_keys: dict[str, int] = {}
    merged_values: list[list[Any]] = []

    for existing_row in existing:
        values = normalize_existing_row(existing_row, headers, key_fields)
        if not values:
            continue
        key = row_key_from_values(values, headers, key_fields)
        if not key.strip(":"):
            continue
        if key in existing_keys:
            merged_values[existing_keys[key]] = values
            continue
        existing_keys[key] = len(merged_values)
        merged_values.append(values)

    updated = 0
    appended = 0
    for row in rows:
        values = [serialize_cell(row.get(header, "")) for header in headers]
        key = row_key(row, key_fields)
        if not key.strip(":"):
            continue
        existing_index = existing_keys.get(key)
        if existing_index is not None:
            merged_values[existing_index] = values
            updated += 1
        else:
            existing_keys[key] = len(merged_values)
            merged_values.append(values)
            appended += 1

    if merged_values:
        update_values(
            service,
            spreadsheet_id,
            sheet_range(sheet_name, f"A2:{col_letter(len(headers))}{len(merged_values) + 1}"),
            merged_values,
        )
        clear_managed_rows(service, spreadsheet_id, sheet_name, headers, len(merged_values) + 2)
    else:
        clear_managed_rows(service, spreadsheet_id, sheet_name, headers)
    return updated, appended, len(merged_values)


def append_new_rows(
    service,
    spreadsheet_id: str,
    sheet_name: str,
    headers: list[str],
    key_fields: list[str],
    rows: list[dict[str, Any]],
) -> tuple[int, int, int]:
    ensure_sheet(service, spreadsheet_id, sheet_name, headers)
    existing = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A2:{col_letter(len(headers))}"))
    existing_keys = {
        row_key_from_values(pad_row(row, len(headers)), headers, key_fields)
        for row in existing
        if row_key_from_values(pad_row(row, len(headers)), headers, key_fields).strip(":")
    }
    additions: list[list[Any]] = []
    for row in rows:
        key = row_key(row, key_fields)
        if not key.strip(":") or key in existing_keys:
            continue
        existing_keys.add(key)
        additions.append([serialize_cell(row.get(header, "")) for header in headers])

    if additions:
        start_row = len(existing) + 2
        update_values(
            service,
            spreadsheet_id,
            sheet_range(
                sheet_name,
                f"A{start_row}:{col_letter(len(headers))}{start_row + len(additions) - 1}",
            ),
            additions,
        )
    return 0, len(additions), len(existing_keys)


def normalize_existing_row(row: list[Any], headers: list[str], key_fields: list[str]) -> list[Any] | None:
    if not any(str(value).strip() for value in row):
        return None

    width = len(headers)
    values = pad_row(row, width)[:width]
    if row_key_from_values(values, headers, key_fields).strip(":"):
        return values

    max_shift = max(0, len(row) - 1)
    for shift in range(1, max_shift + 1):
        shifted = pad_row(row[shift : shift + width], width)
        if row_key_from_values(shifted, headers, key_fields).strip(":"):
            return shifted

    return None


def row_key_from_values(values: list[Any], headers: list[str], fields: list[str]) -> str:
    header_index = {header: index for index, header in enumerate(headers)}
    return ":".join(str(values[header_index[field]]) for field in fields if field in header_index)


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
    remaining = remaining_seconds()
    timeout_seconds = 60 if remaining == float("inf") else max(1, min(60, int(remaining - ANALYSIS_RESERVE_SECONDS)))
    response = requests.get(url, timeout=timeout_seconds)
    try:
        data = response.json() if response.text else {}
    except ValueError as exc:
        raise RuntimeError(f"Meta API returned non-JSON response: {response.text[:500]}") from exc

    if not response.ok:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        raise RuntimeError(f"Meta API error {response.status_code}: {error.get('message') or response.text[:500]}")
    return data


def meta_page_limit(value: Any, default: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, 100))


def get_all_pages(path: str, params: dict[str, Any], access_token: str, max_items: int) -> list[dict[str, Any]]:
    output = []
    page_params = dict(params)
    if "limit" in page_params:
        page_params["limit"] = meta_page_limit(page_params["limit"])
    url = graph_url(path, page_params, access_token)
    page_number = 0
    while url:
        if not collection_time_available():
            log_progress("meta", f"Stopping pagination for {path}: collection deadline reached after {len(output)} items")
            break
        page_number += 1
        try:
            data = fetch_json(url)
        except requests.RequestException:
            if not collection_time_available():
                log_progress("deadline", f"Meta request for {path} timed out at the collection deadline")
                break
            raise
        page_items = data.get("data", [])
        log_progress("meta", f"{path}: page={page_number}, page_items={len(page_items)}, total_items={len(output) + len(page_items)}")
        for item in page_items:
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


def meta_date_sort_key(item: dict[str, Any], date_field: str) -> datetime:
    return parse_meta_date(str(item.get(date_field) or "")) or datetime.min.replace(tzinfo=timezone.utc)


def configured_timezone():
    try:
        return ZoneInfo(CONFIG["timezone"])
    except ZoneInfoNotFoundError:
        return timezone.utc


def collection_window() -> tuple[datetime, datetime]:
    local_tz = configured_timezone()
    lookback_days = CONFIG["lookback_days"]
    if lookback_days < 1:
        raise RuntimeError("LOOKBACK_DAYS must be at least 1")
    end_date = datetime.now(local_tz)
    start_date = end_date - timedelta(days=lookback_days)
    return start_date.astimezone(timezone.utc), end_date.astimezone(timezone.utc)


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

    if not context["ad_account_id"]:
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
    if not page_id:
        return []
    return get_all_pages(
        f"/{page_id}/feed",
        {
            "fields": facebook_post_fields(comment_since, until),
            "since": to_unix(post_since),
            "until": to_unix(until),
            "limit": 100,
        },
        access_token,
        0,
    )


def fetch_instagram_media(ig_user_id: str, since_date: datetime, until_date: datetime, access_token: str):
    if not ig_user_id:
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
    media = get_all_pages(
        f"/{ig_user_id}/media",
        {"fields": fields, "since": to_unix(since_date), "until": to_unix(until_date), "limit": 100},
        access_token,
        0,
    )
    return [item for item in media if is_within(item.get("timestamp", ""), since_date, until_date)]


def fetch_facebook_comments_with_replies(
    object_id: str,
    since_date: datetime,
    until_date: datetime,
    access_token: str,
):
    comments = get_all_pages(
        f"/{object_id}/comments",
        {
            "fields": FACEBOOK_COMMENT_FIELDS,
            "filter": "toplevel",
            "order": "chronological",
            "since": to_unix(since_date),
            "until": to_unix(until_date),
            "limit": 100,
        },
        access_token,
        0,
    )
    comments = [comment for comment in comments if is_within(comment.get("created_time", ""), since_date, until_date)]
    for comment in comments:
        if not collection_time_available():
            break
        replies = get_all_pages(
            f"/{comment.get('id', '')}/comments",
            {
                "fields": FACEBOOK_COMMENT_FIELDS,
                "order": "chronological",
                "since": to_unix(since_date),
                "until": to_unix(until_date),
                "limit": 100,
            },
            access_token,
            0,
        )
        comment["comments"] = {
            "data": [reply for reply in replies if is_within(reply.get("created_time", ""), since_date, until_date)]
        }
    return comments


def fetch_instagram_comments(
    media_id: str,
    since_date: datetime,
    until_date: datetime,
    access_token: str,
):
    comments = get_all_pages(
        f"/{media_id}/comments",
        {"fields": "id,text,timestamp,username,like_count", "limit": 100},
        access_token,
        0,
    )
    comments = [comment for comment in comments if is_within(comment.get("timestamp", ""), since_date, until_date)]
    for comment in comments:
        if not collection_time_available():
            break
        replies = get_all_pages(
            f"/{comment.get('id', '')}/replies",
            {"fields": "id,text,timestamp,username,like_count", "limit": 100},
            access_token,
            0,
        )
        comment["replies"] = {
            "data": [reply for reply in replies if is_within(reply.get("timestamp", ""), since_date, until_date)]
        }
    return comments


def fetch_ads(ad_account_id: str, since_date: datetime, until_date: datetime, access_token: str):
    if not ad_account_id:
        return []
    ads = get_all_pages(
        f"/{ad_account_id}/ads",
        {
            "fields": "id,name,created_time,campaign{name},creative{id,effective_object_story_id,effective_instagram_media_id}",
            "limit": 100,
        },
        access_token,
        0,
    )
    return [ad for ad in ads if is_within(ad.get("created_time", ""), since_date, until_date)]


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


def fetch_instagram_media_by_id(media_id: str, access_token: str):
    return fetch_json(
        graph_url(
            f"/{media_id}",
            {
                "fields": "id,caption,timestamp,permalink,media_type,like_count,comments_count"
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


def comment_text(comment: dict[str, Any], platform: str) -> str:
    return str(comment.get("message" if platform == "facebook" else "text") or "")


def raw_commenter_name(comment: dict[str, Any], platform: str) -> str:
    if platform == "facebook":
        return str(comment.get("from", {}).get("name") or "")
    return str(comment.get("username") or "")


def is_greenpeace_raw_comment(comment: dict[str, Any], platform: str) -> bool:
    if platform == "facebook":
        commenter_id = str(comment.get("from", {}).get("id") or "")
        return bool(
            CONFIG["greenpeace_facebook_page_id"]
            and commenter_id == str(CONFIG["greenpeace_facebook_page_id"])
        )
    username = str(comment.get("username") or "").lower()
    official = CONFIG["greenpeace_instagram_username"].lower()
    return bool(official and username == official)


def add_comment(
    comments: list[dict[str, Any]],
    seen: set[str],
    platform: str,
    post_id: str,
    raw_comment: dict[str, Any],
    parent_comment_id: str,
    parent_comment_message: str,
    post_url: str,
    parent_commenter_name: str = "",
    parent_is_brand_comment: bool = False,
) -> dict[str, Any] | None:
    comment_id = str(raw_comment.get("id") or "")
    if not comment_id or comment_id in seen:
        return None
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

    row = {
        "comment_id": comment_id,
        "post_id": post_id,
        "parent_comment_id": parent_comment_id or raw_comment.get("parent", {}).get("id", ""),
        "parent_comment_message": clean_text(parent_comment_message),
        "commenter_id": commenter_id,
        "commenter_name": commenter_name,
        "comment_created_time": created_time,
        "comment_message": clean_text(message),
        "comment_url": comment_url,
        "like_count": raw_comment.get("like_count", 0),
        "reply_count": reply_count,
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "is_brand_comment": False,
        "comment_emotions": ["Neutral"],
        "emotion_confidence": 0.0,
        "comment_sentiment": "neutral",
        "comment_stance": "unclear",
        "comment_intent": "other",
        "is_sarcastic": False,
        "response_value_score": 0.0,
        # Ephemeral routing context. These keys are intentionally not part of
        # COMMENT_HEADERS and are therefore never written to Google Sheets.
        "_parent_commenter_name": clean_text(parent_commenter_name),
        "_parent_is_brand_comment": bool(parent_is_brand_comment),
    }
    row["is_brand_comment"] = is_greenpeace_comment(row, platform)
    comments.append(row)
    return row


def clear_brand_comment_analysis(comment: dict[str, Any]) -> None:
    comment["comment_emotions"] = ""
    comment["emotion_confidence"] = ""
    comment["comment_sentiment"] = ""
    comment["comment_stance"] = ""
    comment["comment_intent"] = ""
    comment["is_sarcastic"] = ""
    comment["response_value_score"] = ""


def zero_response_scores_for_answered_comments(comments: list[dict[str, Any]]) -> None:
    """An already answered comment has no remaining response value."""
    brand_reply_times_by_thread: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    for comment in comments:
        parent_id = str(comment.get("parent_comment_id") or "")
        if not comment.get("is_brand_comment") or not parent_id:
            continue
        created_at = parse_meta_date(str(comment.get("comment_created_time") or ""))
        if created_at:
            brand_reply_times_by_thread[(str(comment.get("post_id") or ""), parent_id)].append(created_at)

    for comment in comments:
        if comment.get("is_brand_comment"):
            continue
        created_at = parse_meta_date(str(comment.get("comment_created_time") or ""))
        if not created_at:
            continue
        comment_id = str(comment.get("comment_id") or "")
        reply_times = brand_reply_times_by_thread.get((str(comment.get("post_id") or ""), comment_id), [])
        if any(reply_time > created_at for reply_time in reply_times):
            comment["response_value_score"] = 0.0


def collect_visible_comments(
    comments: list[dict[str, Any]],
    seen: set[str],
    platform: str,
    post_id: str,
    raw_comments: list[dict[str, Any]],
    post_url: str,
    since_date: datetime,
    until_date: datetime,
) -> None:
    date_field = "created_time" if platform == "facebook" else "timestamp"
    replies_field = "comments" if platform == "facebook" else "replies"

    def keep_added_comment(row: dict[str, Any] | None) -> None:
        if row and row.get("is_brand_comment"):
            clear_brand_comment_analysis(row)

    for comment in raw_comments:
        if not collection_time_available():
            break
        if is_within(comment.get(date_field, ""), since_date, until_date):
            keep_added_comment(add_comment(comments, seen, platform, post_id, comment, "", "", post_url))

        replies = sorted(
            comment.get(replies_field, {}).get("data") or [],
            key=lambda item: meta_date_sort_key(item, date_field),
        )
        thread_comments_by_id = {
            str(item.get("id") or ""): item
            for item in [comment, *replies]
            if item.get("id")
        }
        for reply in replies:
            if not collection_time_available():
                break
            if not is_within(reply.get(date_field, ""), since_date, until_date):
                continue
            structural_parent_id = str(reply.get("parent", {}).get("id") or comment.get("id") or "")
            structural_parent = thread_comments_by_id.get(structural_parent_id, comment)
            keep_added_comment(
                add_comment(
                    comments,
                    seen,
                    platform,
                    post_id,
                    reply,
                    structural_parent_id,
                    comment_text(structural_parent, platform),
                    post_url,
                    raw_commenter_name(structural_parent, platform),
                    is_greenpeace_raw_comment(structural_parent, platform),
                )
            )


def select_oldest_comments(comments: list[dict[str, Any]], max_comments: int) -> list[dict[str, Any]]:
    if max_comments < 1:
        raise RuntimeError("MAX_COMMENTS_PER_RUN must be at least 1")
    comments.sort(
        key=lambda comment: (
            meta_date_sort_key(comment, "comment_created_time"),
            str(comment.get("comment_id") or ""),
        )
    )
    return comments[:max_comments]


def select_comments_with_existing_post_coverage(
    comments: list[dict[str, Any]],
    max_comments: int,
    existing_post_ids: set[str],
    existing_audience_counts: dict[str, int],
    per_post_coverage: int,
) -> tuple[list[dict[str, Any]], int]:
    """Cover existing posts first, then fill the remaining quota chronologically."""
    if max_comments < 1:
        raise RuntimeError("MAX_COMMENTS_PER_RUN must be at least 1")
    if per_post_coverage < 1:
        raise RuntimeError("COMMENTS_PER_EXISTING_POST_COVERAGE must be at least 1")

    chronological = sorted(
        comments,
        key=lambda comment: (
            meta_date_sort_key(comment, "comment_created_time"),
            str(comment.get("comment_id") or ""),
        ),
    )
    audience_by_existing_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comment in chronological:
        post_id = str(comment.get("post_id") or "")
        if post_id in existing_post_ids and not comment.get("is_brand_comment"):
            audience_by_existing_post[post_id].append(comment)

    prioritized_post_ids = sorted(
        audience_by_existing_post,
        key=lambda post_id: (
            existing_audience_counts.get(post_id, 0),
            meta_date_sort_key(audience_by_existing_post[post_id][0], "comment_created_time"),
            post_id,
        ),
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()

    # Round-robin prevents one busy post from consuming the coverage reserve.
    for coverage_round in range(per_post_coverage):
        for post_id in prioritized_post_ids:
            post_comments = audience_by_existing_post[post_id]
            if coverage_round >= len(post_comments) or len(selected) >= max_comments:
                continue
            comment = post_comments[coverage_round]
            selected.append(comment)
            selected_ids.add(str(comment.get("comment_id") or ""))
        if len(selected) >= max_comments:
            break

    coverage_count = len(selected)

    # Strict phase ordering: exhaust unseen comments from existing sheet posts
    # before any comment belonging to newly discovered content can use capacity.
    for existing_phase in (True, False):
        for comment in chronological:
            if len(selected) >= max_comments:
                break
            post_id = str(comment.get("post_id") or "")
            belongs_to_existing_post = post_id in existing_post_ids
            if belongs_to_existing_post != existing_phase:
                continue
            comment_id = str(comment.get("comment_id") or "")
            if comment_id in selected_ids:
                continue
            selected.append(comment)
            selected_ids.add(comment_id)
        if len(selected) >= max_comments:
            break

    selected.sort(
        key=lambda comment: (
            meta_date_sort_key(comment, "comment_created_time"),
            str(comment.get("comment_id") or ""),
        )
    )
    return selected, coverage_count


def content_target_ids(source: str, item: dict[str, Any]) -> set[str]:
    if source != "ad":
        item_id = str(item.get("id") or "")
        return {item_id} if item_id else set()
    creative = item.get("creative", {})
    targets = {
        str(target_id)
        for target_id in (
            creative.get("effective_object_story_id"),
            creative.get("effective_instagram_media_id"),
        )
        if target_id
    }
    if targets:
        return targets
    item_id = str(item.get("id") or "")
    return {item_id} if item_id else set()


def collect_rows(
    existing_post_ids: set[str] | None = None,
    existing_comment_ids: set[str] | None = None,
    existing_audience_counts: dict[str, int] | None = None,
    existing_post_last_scanned: dict[str, datetime] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    existing_post_ids = existing_post_ids or set()
    existing_comment_ids = existing_comment_ids or set()
    existing_audience_counts = existing_audience_counts or {}
    existing_post_last_scanned = existing_post_last_scanned or {}
    access_token = meta_access_token()
    window_start, window_end = collection_window()
    log_progress(
        "meta",
        f"Collection window: {window_start.isoformat()} to {window_end.isoformat()}; discovering accounts",
    )
    meta_context = discover_meta_context(access_token)
    log_progress(
        "meta",
        f"Discovered page={meta_context['page_id'] or 'none'}, instagram={meta_context['ig_user_id'] or 'none'}, "
        f"ad_account={meta_context['ad_account_id'] or 'none'}",
    )
    page_token = meta_context["page_access_token"]

    posts: list[dict[str, Any]] = []
    comments: list[dict[str, Any]] = []
    scanned_posts: dict[str, dict[str, Any]] = {}
    scanned_content_ids: set[str] = set()
    new_post_ids: set[str] = set()
    seen_comments: set[str] = set(existing_comment_ids)
    max_comments = CONFIG["max_comments_per_run"]
    max_content_items = CONFIG["max_content_items_per_run"]
    per_post_coverage = CONFIG["comments_per_existing_post_coverage"]
    if max_content_items < 1:
        raise RuntimeError("MAX_CONTENT_ITEMS_PER_RUN must be at least 1")
    processed_content_items = 0

    def can_process_content() -> bool:
        return processed_content_items < max_content_items and collection_time_available()

    def register_post(post_row: dict[str, Any]) -> None:
        post_id = str(post_row.get("post_id") or "")
        if not post_id:
            return
        scanned_posts[post_id] = post_row
        if post_id not in existing_post_ids and post_id not in new_post_ids:
            posts.append(post_row)
            new_post_ids.add(post_id)

    log_progress("collect", f"Loading content lists; global content limit={max_content_items}")
    facebook_posts = fetch_facebook_posts(meta_context["page_id"], window_start, window_end, window_start, page_token)
    instagram_media = (
        fetch_instagram_media(meta_context["ig_user_id"], window_start, window_end, access_token)
        if collection_time_available()
        else []
    )
    ads = (
        fetch_ads(meta_context["ad_account_id"], window_start, window_end, access_token)
        if collection_time_available()
        else []
    )
    content_candidates: list[tuple[str, dict[str, Any], str]] = []
    content_candidates.extend(
        ("facebook", post, "created_time")
        for post in facebook_posts
        if post.get("id")
    )
    content_candidates.extend(
        ("instagram", media, "timestamp")
        for media in instagram_media
        if media.get("id")
    )
    content_candidates.extend(
        ("ad", ad, "created_time")
        for ad in ads
        if ad.get("id")
    )

    never_scanned = datetime.min.replace(tzinfo=timezone.utc)

    def candidate_existing_targets(candidate: tuple[str, dict[str, Any], str]) -> set[str]:
        return content_target_ids(candidate[0], candidate[1]) & existing_post_ids

    def candidate_last_scanned(candidate: tuple[str, dict[str, Any], str]) -> datetime:
        targets = candidate_existing_targets(candidate)
        return min(
            (existing_post_last_scanned.get(post_id, never_scanned) for post_id in targets),
            default=never_scanned,
        )

    def existing_content_priority(candidate: tuple[str, dict[str, Any], str]) -> tuple[Any, ...]:
        source, item, date_field = candidate
        existing_targets = candidate_existing_targets(candidate)
        stored_audience_count = (
            min(existing_audience_counts.get(post_id, 0) for post_id in existing_targets)
            if existing_targets
            else 0
        )
        return (
            candidate_last_scanned(candidate),
            stored_audience_count,
            meta_date_sort_key(item, date_field),
            source,
            str(item.get("id") or ""),
        )

    existing_candidates = [
        candidate for candidate in content_candidates if candidate_existing_targets(candidate)
    ]
    new_candidates = [
        candidate for candidate in content_candidates if not candidate_existing_targets(candidate)
    ]
    oldest_existing_scan = min(
        (candidate_last_scanned(candidate) for candidate in existing_candidates),
        default=None,
    )
    pending_existing = [
        candidate
        for candidate in existing_candidates
        if candidate_last_scanned(candidate) == oldest_existing_scan
    ]
    pending_existing.sort(key=existing_content_priority)
    new_candidates.sort(
        key=lambda candidate: (
            meta_date_sort_key(candidate[1], candidate[2]),
            candidate[0],
            str(candidate[1].get("id") or ""),
        )
    )

    # Strict queue: only the oldest not-yet-completed existing scan cohort is
    # considered first. New content can use capacity only after that cohort fits.
    content_queue = [*pending_existing, *new_candidates]
    selected_content: list[tuple[str, dict[str, Any], str]] = []
    selected_target_ids: set[str] = set()
    for candidate in content_queue:
        targets = content_target_ids(candidate[0], candidate[1])
        if targets and targets.issubset(selected_target_ids):
            continue
        selected_content.append(candidate)
        selected_target_ids.update(targets)
        if len(selected_content) >= max_content_items:
            break
    selected_facebook_ids = {
        str(item.get("id") or "")
        for source, item, _ in selected_content
        if source == "facebook"
    }
    selected_instagram_ids = {
        str(item.get("id") or "")
        for source, item, _ in selected_content
        if source == "instagram"
    }
    selected_ad_ids = {
        str(item.get("id") or "")
        for source, item, _ in selected_content
        if source == "ad"
    }
    selected_existing_count = sum(
        1
        for source, item, _ in selected_content
        if content_target_ids(source, item) & existing_post_ids
    )
    log_progress(
        "collect",
        f"Content candidates={len(content_candidates)}, selected={len(selected_content)}, "
        f"existing_candidates={len(existing_candidates)}, pending_existing_cohort={len(pending_existing)}, "
        f"existing_first={selected_existing_count}, new_after_existing={len(selected_content) - selected_existing_count} "
        f"(facebook={len(selected_facebook_ids)}, instagram={len(selected_instagram_ids)}, ads={len(selected_ad_ids)})",
    )

    for post in facebook_posts:
        if str(post.get("id") or "") not in selected_facebook_ids:
            continue
        if not can_process_content():
            break
        post_id = post.get("id", "")
        if not post_id:
            continue
        processed_content_items += 1
        comments_before = len(comments)
        scanned_content_ids.add(post_id)
        post_comments = fetch_facebook_comments_with_replies(post_id, window_start, window_end, page_token)
        collect_visible_comments(
            comments,
            seen_comments,
            "facebook",
            post_id,
            post_comments,
            post.get("permalink_url", ""),
            window_start,
            window_end,
        )
        register_post(
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
                collected_window_start=window_start,
                collected_window_end=window_end,
                comments=[comment for comment in comments if comment["post_id"] == post_id],
            )
        )
        log_progress(
            "collect",
            f"Facebook item {processed_content_items}/{max_content_items}: id={post_id}, "
            f"existing={post_id in existing_post_ids}, new_comments={len(comments) - comments_before}",
        )

    for media in instagram_media:
        if str(media.get("id") or "") not in selected_instagram_ids:
            continue
        if not can_process_content():
            break
        post_id = media.get("id", "")
        if not post_id:
            continue
        processed_content_items += 1
        comments_before = len(comments)
        scanned_content_ids.add(post_id)
        post_comments = fetch_instagram_comments(post_id, window_start, window_end, access_token)
        collect_visible_comments(
            comments,
            seen_comments,
            "instagram",
            post_id,
            post_comments,
            media.get("permalink", ""),
            window_start,
            window_end,
        )
        register_post(
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
                collected_window_start=window_start,
                collected_window_end=window_end,
                comments=[comment for comment in comments if comment["post_id"] == post_id],
            )
        )
        log_progress(
            "collect",
            f"Instagram item {processed_content_items}/{max_content_items}: id={post_id}, "
            f"existing={post_id in existing_post_ids}, new_comments={len(comments) - comments_before}",
        )

    for ad in ads:
        if str(ad.get("id") or "") not in selected_ad_ids:
            continue
        if not can_process_content():
            break
        processed_content_items += 1
        comments_before = len(comments)
        creative = ad.get("creative", {})
        campaign_name = ad.get("campaign", {}).get("name", "")
        ad_id = ad.get("id", "")
        if creative.get("effective_object_story_id"):
            object_id = creative["effective_object_story_id"]
            if object_id not in scanned_content_ids:
                scanned_content_ids.add(object_id)
                ad_post = fetch_facebook_object(object_id, page_token)
                if is_within(ad_post.get("created_time", ""), window_start, window_end):
                    ad_comments = fetch_facebook_comments_with_replies(
                        object_id,
                        window_start,
                        window_end,
                        page_token,
                    )
                    collect_visible_comments(
                        comments,
                        seen_comments,
                        "facebook",
                        object_id,
                        ad_comments,
                        ad_post.get("permalink_url", ""),
                        window_start,
                        window_end,
                    )
                    register_post(
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
                            collected_window_start=window_start,
                            collected_window_end=window_end,
                            comments=[comment for comment in comments if comment["post_id"] == object_id],
                        )
                    )
        if creative.get("effective_instagram_media_id"):
            media_id = creative["effective_instagram_media_id"]
            if media_id not in scanned_content_ids:
                scanned_content_ids.add(media_id)
                ad_media = fetch_instagram_media_by_id(media_id, access_token)
                if is_within(ad_media.get("timestamp", ""), window_start, window_end):
                    ad_comments = fetch_instagram_comments(media_id, window_start, window_end, access_token)
                    collect_visible_comments(
                        comments,
                        seen_comments,
                        "instagram",
                        media_id,
                        ad_comments,
                        ad_media.get("permalink", ""),
                        window_start,
                        window_end,
                    )
                    register_post(
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
                            collected_window_start=window_start,
                            collected_window_end=window_end,
                            comments=[comment for comment in comments if comment["post_id"] == media_id],
                        )
                    )
        log_progress(
            "collect",
            f"Ad item {processed_content_items}/{max_content_items}: ad_id={ad_id}, "
            f"new_comments={len(comments) - comments_before}",
        )
    candidate_count = len(comments)
    selected_comments, coverage_count = select_comments_with_existing_post_coverage(
        comments,
        max_comments,
        existing_post_ids,
        existing_audience_counts,
        per_post_coverage,
    )
    deferred_count = candidate_count - len(selected_comments)
    print(
        "New comment candidates: "
        f"found={candidate_count}, selected={len(selected_comments)}, deferred={deferred_count}, "
        f"existing_post_coverage_selected={coverage_count}, coverage_per_post={per_post_coverage}, "
        f"limit={max_comments}."
    )
    if not collection_time_available():
        log_progress("deadline", "Collection stopped early to reserve time for Gemini analysis and Sheets writes")
    elif processed_content_items >= max_content_items:
        log_progress("collect", f"Reached global content-item limit of {max_content_items}")
    return posts, selected_comments, list(scanned_posts.values())


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
        "collected_greenpeace_comment_count": sum(1 for comment in comments if comment.get("is_brand_comment")),
        "collected_like_count": like_count,
        "collected_reply_count": reply_count,
        "collected_window_start": collected_window_start.isoformat(),
        "collected_window_end": collected_window_end.isoformat(),
        "post_emotions": ["Neutral"],
        "post_sentiment": "neutral",
        # Ephemeral checkpoint data; not part of POST_HEADERS.
        "_new_comment_ids": {
            str(comment.get("comment_id") or "")
            for comment in comments
            if comment.get("comment_id")
        },
    }


def gemini_generate_json(prompt: str, schema: dict[str, Any]) -> dict[str, Any]:
    if not analysis_time_available():
        raise DeadlineReached("Not enough time remains for another Gemini request")
    if CONFIG["gemini_quota_exhausted"]:
        if not CONFIG["gemini_quota_notice_printed"]:
            print("Skipping Gemini analysis for the rest of this run because quota is exhausted.")
            CONFIG["gemini_quota_notice_printed"] = True
        return {}

    api_key = required_env("GEMINI_API_KEY")
    primary_mode = CONFIG["gemini_api_mode"]
    fallback_mode = "interactions" if primary_mode == "generateContent" else "generateContent"

    result = gemini_generate_json_with_mode(prompt, api_key, primary_mode)
    if result == "__quota_exhausted__":
        return gemini_fallback_or_raise("Gemini quota exhausted.")
    if isinstance(result, dict):
        return result

    print(f"Gemini {primary_mode} did not return valid JSON. Trying {fallback_mode} fallback once...")
    result = gemini_generate_json_with_mode(prompt, api_key, fallback_mode)
    if result == "__quota_exhausted__":
        return gemini_fallback_or_raise("Gemini quota exhausted.")
    if isinstance(result, dict):
        return result

    return gemini_fallback_or_raise("Gemini did not return valid JSON.")


def gemini_generate_json_with_mode(prompt: str, api_key: str, mode: str) -> dict[str, Any] | str | None:
    if not analysis_time_available():
        raise DeadlineReached("Gemini analysis deadline reached")
    url, payload, headers = gemini_request(prompt, api_key, mode)
    print(
        "Gemini request: "
        f"mode={mode}, "
        f"model={CONFIG['gemini_model']}, "
        f"prompt_chars={len(prompt)}"
    )
    retryable_statuses = {429, 500, 502, 503, 504}
    max_retries = max(0, CONFIG["gemini_max_retries"])

    for attempt in range(max_retries + 1):
        if not analysis_time_available():
            raise DeadlineReached("Gemini analysis deadline reached before request")
        request_timeout = max(1, min(120, int(remaining_seconds() - WRITE_RESERVE_SECONDS)))
        log_progress(
            "gemini",
            f"Request attempt={attempt + 1}/{max_retries + 1}, mode={mode}, timeout={request_timeout}s",
        )
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=request_timeout)
            data = response.json() if response.text else {}
        except (requests.RequestException, ValueError) as exc:
            if attempt >= max_retries:
                print(f"WARNING: Gemini {mode} request failed: {exc}")
                return None
            sleep_before_gemini_retry(attempt)
            continue

        if response.ok:
            try:
                return extract_gemini_json(data)
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                print(f"WARNING: Gemini {mode} returned invalid JSON: {exc}")
                print(f"Gemini {mode} response shape: {gemini_response_shape(data)}")
                return None

        message = json.dumps(data, ensure_ascii=False)[:1000]
        if response.status_code == 429 and not CONFIG["gemini_fallback_after_quota_error"]:
            CONFIG["gemini_quota_exhausted"] = True
            print(f"WARNING: Gemini {mode} quota/rate limit error 429: {message}")
            return "__quota_exhausted__"
        if response.status_code not in retryable_statuses or attempt >= max_retries:
            print(f"WARNING: Gemini {mode} API error {response.status_code}: {message}")
            return None
        sleep_before_gemini_retry(attempt)

    return None


def gemini_request(prompt: str, api_key: str, mode: str) -> tuple[str, dict[str, Any], dict[str, str]]:
    if mode == "interactions":
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
    elif data.get("steps"):
        for step in data.get("steps", []):
            if not isinstance(step, dict):
                continue
            for content in step.get("content", []):
                if isinstance(content, dict) and content.get("type") == "text" and content.get("text"):
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
    return parse_first_json_object(text)


def parse_first_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    stripped = text.strip()
    try:
        value, _ = decoder.raw_decode(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start == -1:
            raise
        value, _ = decoder.raw_decode(stripped[start:])

    if not isinstance(value, dict):
        raise json.JSONDecodeError("Gemini JSON response is not an object", stripped, 0)
    return value


def gemini_response_shape(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        return type(data).__name__
    shape: dict[str, Any] = {key: type(value).__name__ for key, value in data.items()}
    if data.get("output") and isinstance(data["output"], list):
        shape["output_types"] = [
            output.get("type", type(output).__name__)
            for output in data["output"][:5]
            if isinstance(output, dict)
        ]
    if data.get("steps") and isinstance(data["steps"], list):
        shape["step_types"] = [
            step.get("type", type(step).__name__)
            for step in data["steps"][:5]
            if isinstance(step, dict)
        ]
    if data.get("candidates") and isinstance(data["candidates"], list):
        shape["candidates_count"] = len(data["candidates"])
    return json.dumps(shape, ensure_ascii=False)


def sleep_before_gemini_retry(attempt: int) -> None:
    delay = CONFIG["gemini_retry_base_seconds"] * (2 ** attempt)
    if remaining_seconds() <= WRITE_RESERVE_SECONDS + delay + 5:
        raise DeadlineReached("Skipping Gemini retry to preserve Sheets write time")
    print(f"Gemini is temporarily unavailable. Retrying in {delay} seconds...")
    time.sleep(delay)


def gemini_fallback_or_raise(message: str) -> dict[str, Any]:
    if CONFIG["gemini_fallback_on_error"]:
        print(f"WARNING: {message}")
        print("Continuing without Gemini analysis for this batch; default analysis values will be used.")
        return {}
    raise RuntimeError(message)


def analyze_posts(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not posts:
        return []
    analyzed_posts: list[dict[str, Any]] = []
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
                        "post_sentiment": {"type": "STRING", "enum": POST_SENTIMENTS},
                    },
                    "required": [
                        "post_id",
                        "canonical_topic",
                        "canonical_subtopic",
                        "topic_source",
                        "topic_confidence",
                        "post_emotions",
                        "post_sentiment",
                    ],
                },
            }
        },
        "required": ["posts"],
    }
    for post_index, post in enumerate(posts, start=1):
        if not analysis_time_available():
            log_progress("deadline", f"Stopping post analysis at {post_index - 1}/{len(posts)}")
            break
        item_for_prompt = {
            "post_id": post["post_id"],
            "platform": post["platform"],
            "source_type": post["source_type"],
            "campaign_name": post["campaign_name"],
            "hashtags": post["post_hashtags"],
            "message": truncate_text(post["post_message"]),
        }
        prompt = (
            "Analyze this Greenpeace Israel social post. Return only valid JSON in this exact shape: "
            '{"posts":[{"post_id":"...","canonical_topic":"...","canonical_subtopic":"...",'
            '"topic_source":"hashtags|campaign_name|ad_text|ai_classification|manual|unknown",'
            '"topic_confidence":0.0,"post_emotions":["Neutral"],'
            '"post_sentiment":"positive|negative|mixed|neutral|unclear"}]}. '
            "Use short snake_case English labels for canonical_topic and canonical_subtopic. "
            "topic_source must indicate the strongest source used: hashtags, campaign_name, ad_text, "
            "ai_classification, manual, or unknown. Emotions and sentiment must come from the allowed lists. "
            "Calibrate topic_confidence: use 0.95 only when the topic/subtopic is explicit in hashtags, campaign name, "
            "or repeated direct post language; use 0.85-0.90 when the topic is strongly supported but requires some interpretation; "
            "use 0.65-0.80 when the topic is inferred from context, broad framing, or ambiguous wording; "
            "use 0.40-0.60 when multiple topics could reasonably fit; use below 0.40 when the text is too minimal or unclear. "
            "Do not assign 0.95 by default. "
            "Classify the emotional framing Greenpeace is using, not whether the facts are accurate. "
            "Do not classify as Neutral when the post describes harm, death, extinction, pollution, corruption, "
            "corporate responsibility, climate disasters, public health risks, injustice, or urgent calls to action. "
            "If the post asks readers to sign, donate, act, stop something, or join a campaign, include an action-oriented "
            "emotion such as Urgency, Determination, Hope, or Concern when appropriate. If the post describes danger, endangered species, "
            "heat waves, pollution, or health risks, post_emotions should usually include Concern, Alarm, Urgency, "
            "Grief, or Outrage. Use Neutral only for truly flat administrative or descriptive posts.\n\n"
            "Examples:\n"
            '- "הם ידעו מה הנזק ובחרו ברווחים 😡" => emotions ["Outrage","Urgency"], sentiment negative.\n'
            '- "גלי החום באירופה כבר שוברים שיאים... גבה את חייהם" => emotions ["Alarm","Concern","Urgency"], sentiment negative.\n'
            '- "חתמו על העצומה... ליצור שמורות ימיות" => emotions ["Concern","Hope","Urgency"], sentiment mixed.\n'
            '- "דוח חדש של FAO חושף..." => emotions ["Concern"], sentiment negative.\n'
            '- "הצטרפו אלינו לאירוע קהילתי בגינה" => emotions ["Hope"], sentiment positive.\n\n'
            f"Post:\n{json.dumps(item_for_prompt, ensure_ascii=False)}"
        )
        log_progress("gemini", f"Analyzing new post {post_index}/{len(posts)}: id={post['post_id']}")
        try:
            generated = gemini_generate_json(prompt, schema)
        except DeadlineReached:
            log_progress("deadline", f"Post analysis deadline reached at {post_index - 1}/{len(posts)}")
            break
        analysis = {item["post_id"]: item for item in generated.get("posts", [])}
        item = analysis.get(post["post_id"], {})
        post["canonical_topic"] = clean_text(item.get("canonical_topic", "")) or "unknown"
        post["canonical_subtopic"] = clean_text(item.get("canonical_subtopic", "")) or "unknown"
        post["topic_source"] = allowed_value(item.get("topic_source"), TOPIC_SOURCES, "unknown")
        post["topic_confidence"] = bounded_float(item.get("topic_confidence"), 0.0, 1.0)
        post["post_emotions"] = allowed_list(item.get("post_emotions"), POST_EMOTIONS, ["Neutral"])
        post["post_sentiment"] = allowed_value(item.get("post_sentiment"), POST_SENTIMENTS, "unclear")
        analyzed_posts.append(post)
    return analyzed_posts


def explicitly_mentions_greenpeace(comment: dict[str, Any]) -> bool:
    message = str(comment.get("comment_message") or "").casefold()
    official_instagram = CONFIG["greenpeace_instagram_username"].casefold().lstrip("@")
    if "גרינפיס" in message or "greenpeace" in message:
        return True
    return bool(official_instagram and re.search(rf"(?<![\w])@?{re.escape(official_instagram)}(?![\w])", message))


def enforce_response_routing(comment: dict[str, Any], analysis: dict[str, Any]) -> bool:
    """Apply deterministic organization-response rules without adding sheet columns."""
    intent = str(comment.get("comment_intent") or "")
    if intent in {"tag_friend", "spam"}:
        comment["response_value_score"] = 0.0
        return True

    if not comment.get("parent_comment_id"):
        return False

    if comment.get("_parent_is_brand_comment"):
        return False

    reply_target = str(analysis.get("reply_target") or "unclear")
    criticism_target = str(analysis.get("criticism_target") or "unclear")
    is_org_directed = (
        explicitly_mentions_greenpeace(comment)
        or reply_target == "greenpeace"
        or criticism_target in {"greenpeace", "post_claim"}
    )
    if is_org_directed:
        return False

    # Replies inside audience threads default to no organizational response.
    # This gate protects the final score even when Gemini overvalues the reply.
    comment["response_value_score"] = 0.0
    return True


def analyze_comments(comments: list[dict[str, Any]], posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not comments:
        return []
    for comment in comments:
        if comment.get("is_brand_comment"):
            clear_brand_comment_analysis(comment)
    completed_comments = [comment for comment in comments if comment.get("is_brand_comment")]
    audience_comments = [comment for comment in comments if not comment.get("is_brand_comment")]
    post_context = {
        post["post_id"]: {
            "post_message": truncate_text(post["post_message"]),
            "platform": post.get("platform", ""),
            "source_type": post.get("source_type", ""),
            "canonical_topic": post.get("canonical_topic", ""),
            "canonical_subtopic": post.get("canonical_subtopic", ""),
        }
        for post in posts
    }
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
                        "comment_sentiment": {"type": "STRING", "enum": COMMENT_SENTIMENTS},
                        "comment_stance": {"type": "STRING", "enum": COMMENT_STANCES},
                        "comment_intent": {"type": "STRING", "enum": COMMENT_INTENTS},
                        "is_sarcastic": {"type": "BOOLEAN"},
                        "response_value_score": {"type": "NUMBER"},
                        "reply_target": {
                            "type": "STRING",
                            "enum": ["greenpeace", "another_user", "general_discussion", "unclear"],
                        },
                        "criticism_target": {
                            "type": "STRING",
                            "enum": ["greenpeace", "post_claim", "another_user", "external_actor", "general", "unclear"],
                        },
                    },
                    "required": [
                        "comment_id",
                        "comment_emotions",
                        "emotion_confidence",
                        "comment_sentiment",
                        "comment_stance",
                        "comment_intent",
                        "is_sarcastic",
                        "response_value_score",
                        "reply_target",
                        "criticism_target",
                    ],
                },
            }
        },
        "required": ["comments"],
    }
    batch_size = max(1, CONFIG["analysis_batch_size"])
    total_batches = (len(audience_comments) + batch_size - 1) // batch_size
    for batch_number, batch_start in enumerate(range(0, len(audience_comments), batch_size), start=1):
        if not analysis_time_available():
            log_progress(
                "deadline",
                f"Stopping comment analysis after {len(completed_comments) - sum(1 for item in completed_comments if item.get('is_brand_comment'))} "
                f"audience comments; {len(audience_comments) - batch_start} deferred",
            )
            break
        batch = audience_comments[batch_start : batch_start + batch_size]

        items = [
            {
                "comment_id": comment["comment_id"],
                "post_id": comment["post_id"],
                "post_context": post_context.get(comment["post_id"], ""),
                "commenter_name": comment.get("commenter_name", ""),
                "parent_comment_id": comment.get("parent_comment_id", ""),
                "parent_comment_message": truncate_text(comment.get("parent_comment_message", "")),
                "parent_commenter_name": comment.get("_parent_commenter_name", ""),
                "parent_is_brand_comment": bool(comment.get("_parent_is_brand_comment")),
                "comment_message": truncate_text(comment["comment_message"]),
                "is_brand_comment": bool(comment.get("is_brand_comment")),
                "like_count": comment.get("like_count", 0),
                "reply_count": comment.get("reply_count", 0),
            }
            for comment in batch
        ]
        prompt = (
            "Analyze Hebrew/English social media comments for Greenpeace Israel. "
            "Classify audience reaction to Greenpeace content, not the emotion of the post itself. "
            "For replies, parent_comment_message and parent_commenter_name describe the structural parent comment, "
            "not merely the previous chronological reply. Use them only as conversational context and classify comment_message. "
            "Determine reply_target: greenpeace only when the reply addresses Greenpeace/the organization; another_user when it "
            "addresses or answers a participant; general_discussion when it is not directed to a specific participant; otherwise unclear. "
            "Determine criticism_target separately. Criticism of another user, politician, company, or outside actor is not criticism "
            "of Greenpeace. post_claim means the comment challenges a claim made by Greenpeace in the post. "
            "Return only valid JSON in this exact shape: "
            '{"comments":[{"comment_id":"...","comment_emotions":["Neutral"],"emotion_confidence":0.0,'
            '"comment_sentiment":"positive|negative|mixed|neutral|unclear",'
            '"comment_stance":"supportive|opposed|skeptical|neutral|unclear",'
            '"comment_intent":"support|criticism|question|mockery|information_request|answer|service_request|personal_story|tag_friend|political_attack|spam|other",'
            '"is_sarcastic":false,"response_value_score":0.0,"reply_target":"greenpeace|another_user|general_discussion|unclear",'
            '"criticism_target":"greenpeace|post_claim|another_user|external_actor|general|unclear"}]}. '
            "Use the closed lists exactly. "
            "Do not classify a comment as Neutral when it contains insults, accusations, mockery, sarcasm, "
            "conspiracy claims, hostile language, disgust, anxiety, sadness, or clear support. "
            "Emoji-only comments must be interpreted from emoji meaning plus post context. "
            "If comment_stance is opposed or skeptical and comment_intent is criticism or mockery, "
            "comment_emotions should usually not be only Neutral unless the text is purely factual and calm. "
            "For sarcastic comments, set is_sarcastic=true and use the underlying emotion such as Contempt, "
            "Dismissiveness, Anger, Amusement, Skepticism, or Hostility. Do not use Sarcasm as a comment emotion. "
            "Use Contempt/Dismissiveness for belittling or sneering comments, Hostility for personal attacks, "
            "Disgust for revulsion, Concern or Anxiety for worried reactions, Agreement for factual agreement, "
            "and Neutral only for emotionally flat logistics, tags, simple facts, or unclear minimal text. "
            "Crying or sad emoji-only comments should use Sadness or Concern, not Neutral. "
            "Use service_request for donation, account, unsubscribe, billing, or operational support requests. "
            "Use answer when a user answers another user's question, supplies requested information, or corrects another user; "
            "do not label such replies information_request or service_request. "
            "Set response_value_score from 0.0 to 1.0 to estimate how much Greenpeace would benefit by replying publicly. "
            "Use 0.90-1.00 for comments where a reply can prevent reputational harm, correct serious misinformation, "
            "answer a high-value public question, de-escalate a visible conflict, or convert strong engagement into action. "
            "Use 0.70-0.89 for good-faith criticism, substantive skepticism, useful questions, or comments where a clear answer "
            "could educate other readers. Use 0.40-0.69 for moderate engagement or ambiguous criticism where a reply may help but is not essential. "
            "Use 0.10-0.39 for low-value agreement, brief reactions, repetitive criticism, or comments unlikely to change audience perception. "
            "Use 0.00 for spam, tags, answers between users, brand comments, or comments where replying has no clear organizational benefit. "
            "A reply within an audience discussion must receive 0.00 unless it directly addresses Greenpeace, replies to an official "
            "Greenpeace comment, or substantively challenges Greenpeace/the post's claim. Do not reward Greenpeace for intervening "
            "in questions, corrections, criticism, insults, or debates directed at another user. "
            "Calibrate emotion_confidence: use 0.95 only when the emotion/stance is explicit and unambiguous, "
            "such as clear insults, strong praise, direct support, or direct hostility; use 0.85-0.90 when the classification is strong "
            "but depends on context or interpretation; use 0.65-0.80 for questions, short comments, sarcasm, mixed signals, "
            "or implied rather than explicit emotion; use 0.40-0.60 when several classifications could reasonably fit; "
            "use below 0.40 when the text is too minimal, unclear, or mostly context-free. "
            "Do not assign high confidence when fields conflict, for example positive/supportive with only Neutral emotion. "
            "Do not assign 0.90 or 0.95 by default.\n\n"
            "Examples:\n"
            '- "יאללה שקרנים הביתה" => emotions ["Anger","Hostility"], sentiment negative, stance opposed, intent criticism.\n'
            '- "בבקשה תחליטו.. או אבולה או חייזרים" => emotions ["Contempt"], sentiment negative, stance skeptical, intent mockery, is_sarcastic true.\n'
            '- "איפה חותמים?" => emotions ["Agreement"], sentiment positive, stance supportive, intent information_request, response_value_score 0.70-0.89.\n'
            '- "🔥🔥🥵😣" on a climate disaster post => emotions ["Concern","Anxiety"], sentiment mixed, stance supportive.\n'
            '- "@friend" or only tagging a friend => emotions ["Neutral"], sentiment neutral, intent tag_friend.\n\n'
            '- "יעל, תוכיחי שיש ריסוסים?" as a reply to Yael => intent question, reply_target another_user, response_value_score 0.00.\n'
            '- "דרך חברת האשראי או הבנק" answering another user => intent answer, reply_target another_user, response_value_score 0.00.\n'
            '- "Greenpeace Israel, תביאו הוכחות" => reply_target greenpeace, criticism_target greenpeace, eligible for a high score.\n\n'
            f"Comments:\n{json.dumps(items, ensure_ascii=False)}"
        )
        log_progress(
            "gemini",
            f"Analyzing comment batch {batch_number}/{total_batches}: batch_size={len(batch)}, "
            f"completed_audience={batch_start}/{len(audience_comments)}",
        )
        try:
            generated = gemini_generate_json(prompt, schema)
        except DeadlineReached:
            log_progress("deadline", f"Comment analysis deadline reached before batch {batch_number}/{total_batches}")
            break
        analysis = {item["comment_id"]: item for item in generated.get("comments", [])}
        routing_zeroed = 0
        for comment in batch:
            item = analysis.get(comment["comment_id"], {})
            comment["comment_emotions"] = allowed_list(item.get("comment_emotions"), COMMENT_EMOTIONS, ["Neutral"])
            comment["emotion_confidence"] = bounded_float(item.get("emotion_confidence"), 0.0, 1.0)
            comment["comment_sentiment"] = allowed_value(item.get("comment_sentiment"), COMMENT_SENTIMENTS, "unclear")
            comment["comment_stance"] = allowed_value(item.get("comment_stance"), COMMENT_STANCES, "unclear")
            comment["comment_intent"] = allowed_value(item.get("comment_intent"), COMMENT_INTENTS, "other")
            comment["is_sarcastic"] = bool(item.get("is_sarcastic", False))
            comment["response_value_score"] = bounded_float(item.get("response_value_score"), 0.0, 1.0)
            if enforce_response_routing(comment, item):
                routing_zeroed += 1
        log_progress(
            "analysis",
            f"Comment batch {batch_number}/{total_batches}: response scores forced to zero by routing rules={routing_zeroed}",
        )
        completed_comments.extend(batch)

    zero_response_scores_for_answered_comments(completed_comments)
    return completed_comments


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


def response_score(comment: dict[str, Any]) -> float:
    try:
        return float(comment.get("response_value_score") or 0)
    except (TypeError, ValueError):
        return 0.0


def summarize_posts(posts: list[dict[str, Any]], comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    comments_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for comment in comments:
        if comment.get("is_brand_comment"):
            continue
        comments_by_post[comment["post_id"]].append(comment)

    summaries = []
    for post in posts:
        post_id = post["post_id"]
        post_comments = comments_by_post.get(post_id, [])
        total = len(post_comments)
        stance_counter = Counter(comment.get("comment_stance", "unclear") for comment in post_comments)
        intent_counter = Counter(comment.get("comment_intent", "other") for comment in post_comments)
        requires_response_count = sum(
            1
            for comment in post_comments
            if response_score(comment) >= RECOMMENDED_RESPONSE_SCORE_THRESHOLD
        )

        summaries.append(
            {
                "post_id": post_id,
                "ad_id": post.get("ad_id", ""),
                "campaign_name": post.get("campaign_name", ""),
                "post_url": post.get("post_url", ""),
                "canonical_topic": post.get("canonical_topic", ""),
                "canonical_subtopic": post.get("canonical_subtopic", ""),
                "collected_comment_count": post.get("collected_comment_count", 0),
                "collected_greenpeace_comment_count": post.get("collected_greenpeace_comment_count", 0),
                "collected_like_count": post.get("collected_like_count", 0),
                "total_comments": total,
                "dominant_comment_stance": most_common(stance_counter, "unclear"),
                "dominant_comment_intent": most_common(intent_counter, "other"),
                "service_request_count": intent_counter["service_request"],
                "service_request_rate": rate(intent_counter["service_request"], total),
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


def reconcile_answered_response_scores(service, spreadsheet_id: str) -> int:
    raw_rows = get_values(
        service,
        spreadsheet_id,
        sheet_range(POST_COMMENTS_SHEET_NAME, f"A2:{col_letter(len(COMMENT_HEADERS))}"),
    )
    if not raw_rows:
        return 0
    rows = [pad_row(row, len(COMMENT_HEADERS)) for row in raw_rows]
    score_index = COMMENT_HEADERS.index("response_value_score")
    previous_scores = [row[score_index] for row in rows]
    zero_answered_scores_in_rows(rows, COMMENT_HEADERS)
    changed = 0
    updates: list[dict[str, Any]] = []
    score_column = score_index + 1
    for row_offset, (previous, row) in enumerate(zip(previous_scores, rows), start=2):
        try:
            previous_score = float(previous or 0)
        except (TypeError, ValueError):
            previous_score = 0.0
        if previous_score != 0.0 and row[score_index] == 0:
            changed += 1
            cell = f"{col_letter(score_column)}{row_offset}"
            updates.append(
                {
                    "range": sheet_range(POST_COMMENTS_SHEET_NAME, cell),
                    "values": [[0]],
                }
            )
    batch_update_values(service, spreadsheet_id, updates)
    return changed


def refresh_derived_metrics(service, spreadsheet_id: str) -> tuple[int, int]:
    raw_comment_rows = get_values(
        service,
        spreadsheet_id,
        sheet_range(POST_COMMENTS_SHEET_NAME, f"A2:{col_letter(len(COMMENT_HEADERS))}"),
    )
    comments: list[dict[str, Any]] = []
    comments_by_post: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for raw_row in raw_comment_rows:
        row = pad_row(raw_row, len(COMMENT_HEADERS))
        comment = {header: row[index] for index, header in enumerate(COMMENT_HEADERS)}
        comment["is_brand_comment"] = str(comment.get("is_brand_comment") or "").strip().lower() == "true"
        comments.append(comment)
        comments_by_post[str(comment.get("post_id") or "")].append(comment)

    raw_post_rows = get_values(
        service,
        spreadsheet_id,
        sheet_range(POSTS_SHEET_NAME, f"A2:{col_letter(len(POST_HEADERS))}"),
    )
    if not raw_post_rows:
        return 0, 0
    posts: list[dict[str, Any]] = []
    count_values: list[list[int]] = []
    for raw_row in raw_post_rows:
        row = pad_row(raw_row, len(POST_HEADERS))
        post = {header: row[index] for index, header in enumerate(POST_HEADERS)}
        post_comments = comments_by_post.get(str(post.get("post_id") or ""), [])
        total_count = len(post_comments)
        brand_count = sum(1 for comment in post_comments if comment.get("is_brand_comment"))
        reply_count = sum(1 for comment in post_comments if comment.get("parent_comment_id"))
        post["collected_comment_count"] = total_count
        post["collected_greenpeace_comment_count"] = brand_count
        post["collected_reply_count"] = reply_count
        posts.append(post)
        count_values.append([total_count, brand_count, reply_count])

    first_count_column = POST_HEADERS.index("collected_comment_count") + 1
    last_count_column = POST_HEADERS.index("collected_reply_count") + 1
    update_values(
        service,
        spreadsheet_id,
        sheet_range(
            POSTS_SHEET_NAME,
            f"{col_letter(first_count_column)}2:{col_letter(last_count_column)}{len(count_values) + 1}",
        ),
        count_values,
    )
    summaries = summarize_posts(posts, comments)
    _, _, summary_total = upsert_by_key(
        service,
        spreadsheet_id,
        POST_SUMMARY_SHEET_NAME,
        SUMMARY_HEADERS,
        ["post_id"],
        summaries,
    )
    return len(posts), summary_total


def update_scanned_post_checkpoints(
    service,
    spreadsheet_id: str,
    scanned_posts: list[dict[str, Any]],
    completed_comments: list[dict[str, Any]],
) -> tuple[int, int]:
    """Advance existing-post queue checkpoints only after all discovered comments were written."""
    if not scanned_posts:
        return 0, 0

