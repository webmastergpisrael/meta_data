import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build


HEADERS = [
    "comment_id",
    "parent_comment_id",
    "post_id",
    "post_created_time",
    "post_message",
    "post_url",
    "comment_created_time",
    "commenter_name",
    "commenter_id",
    "comment_message",
    "comment_url",
    "like_count",
    "reply_count",
    "collected_window_start",
    "collected_window_end",
    "collected_at",
    "platform",
    "source_type",
    "ad_id",
    "ad_name",
]

STATUS_HEADERS = ["time", "status", "message"]

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

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


def env_value(name: str, default: str) -> str:
    return os.getenv(name) or default


def env_int(name: str, default: int) -> int:
    return int(env_value(name, str(default)))


CONFIG = {
    "sheet_name": "Meta Comments",
    "legacy_sheet_name": "Facebook Comments",
    "status_sheet_name": "Meta Sync Status",
    "legacy_status_sheet_name": "Facebook Sync Status",
    "page_id": env_value("FB_PAGE_ID", "44385426513"),
    "ig_user_id": env_value("IG_USER_ID", "17841400351753663"),
    "ad_account_id": env_value("META_AD_ACCOUNT_ID", "act_670121410174015"),
    "graph_version": env_value("META_GRAPH_VERSION", "v23.0"),
    "comment_lookback_days": env_int("COMMENT_LOOKBACK_DAYS", 7),
    "post_lookback_days": env_int("POST_LOOKBACK_DAYS", 14),
    "max_posts_per_run": env_int("MAX_POSTS_PER_RUN", 25),
    "max_instagram_media_per_run": env_int("MAX_INSTAGRAM_MEDIA_PER_RUN", 25),
    "max_ads_per_run": env_int("MAX_ADS_PER_RUN", 25),
    "timezone": env_value("TIMEZONE", "Asia/Jerusalem"),
}


def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def get_sheets_service():
    raw_json = required_env("GOOGLE_SERVICE_ACCOUNT_JSON")
    credentials_info = json.loads(raw_json)
    credentials = service_account.Credentials.from_service_account_info(
        credentials_info,
        scopes=SCOPES,
    )
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


def get_spreadsheet(service, spreadsheet_id: str) -> dict[str, Any]:
    return (
        service.spreadsheets()
        .get(spreadsheetId=spreadsheet_id, fields="sheets(properties(sheetId,title,gridProperties(rowCount,columnCount)))")
        .execute()
    )


def find_sheet(metadata: dict[str, Any], title: str) -> dict[str, Any] | None:
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {})
        if properties.get("title") == title:
            return properties
    return None


def batch_update(service, spreadsheet_id: str, requests_body: list[dict[str, Any]]) -> None:
    if not requests_body:
        return
    service.spreadsheets().batchUpdate(
        spreadsheetId=spreadsheet_id,
        body={"requests": requests_body},
    ).execute()


def update_values(service, spreadsheet_id: str, range_name: str, values: list[list[Any]]) -> None:
    service.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        body={"values": values},
    ).execute()


def get_values(service, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
    response = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=spreadsheet_id, range=range_name)
        .execute()
    )
    return response.get("values", [])


def clear_values(service, spreadsheet_id: str, range_name: str) -> None:
    service.spreadsheets().values().clear(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        body={},
    ).execute()


def append_values(service, spreadsheet_id: str, range_name: str, values: list[list[Any]]) -> None:
    service.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id,
        range=range_name,
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def ensure_sheet(service, spreadsheet_id: str) -> str:
    metadata = get_spreadsheet(service, spreadsheet_id)
    sheet = find_sheet(metadata, CONFIG["sheet_name"])

    if not sheet:
        legacy_sheet = find_sheet(metadata, CONFIG["legacy_sheet_name"])
        if legacy_sheet:
            batch_update(
                service,
                spreadsheet_id,
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": legacy_sheet["sheetId"],
                                "title": CONFIG["sheet_name"],
                            },
                            "fields": "title",
                        }
                    }
                ],
            )
        else:
            batch_update(
                service,
                spreadsheet_id,
                [{"addSheet": {"properties": {"title": CONFIG["sheet_name"]}}}],
            )

    migrate_headers(service, spreadsheet_id, CONFIG["sheet_name"], HEADERS)
    return CONFIG["sheet_name"]


def ensure_status_sheet(service, spreadsheet_id: str) -> str:
    metadata = get_spreadsheet(service, spreadsheet_id)
    sheet = find_sheet(metadata, CONFIG["status_sheet_name"])

    if not sheet:
        legacy_sheet = find_sheet(metadata, CONFIG["legacy_status_sheet_name"])
        if legacy_sheet:
            batch_update(
                service,
                spreadsheet_id,
                [
                    {
                        "updateSheetProperties": {
                            "properties": {
                                "sheetId": legacy_sheet["sheetId"],
                                "title": CONFIG["status_sheet_name"],
                            },
                            "fields": "title",
                        }
                    }
                ],
            )
        else:
            batch_update(
                service,
                spreadsheet_id,
                [{"addSheet": {"properties": {"title": CONFIG["status_sheet_name"]}}}],
            )

    migrate_headers(service, spreadsheet_id, CONFIG["status_sheet_name"], STATUS_HEADERS)
    return CONFIG["status_sheet_name"]


def freeze_first_row(service, spreadsheet_id: str, sheet_name: str) -> None:
    metadata = get_spreadsheet(service, spreadsheet_id)
    sheet = find_sheet(metadata, sheet_name)
    if not sheet:
        return
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


def migrate_headers(service, spreadsheet_id: str, sheet_name: str, desired_headers: list[str]) -> None:
    width = len(desired_headers)
    current = get_values(service, spreadsheet_id, sheet_range(sheet_name, "1:1"))
    current_headers = [str(value).strip() for value in current[0]] if current else []
    has_any_header = any(current_headers)

    if not has_any_header:
        update_values(service, spreadsheet_id, sheet_range(sheet_name, f"A1:{col_letter(width)}1"), [desired_headers])
        freeze_first_row(service, spreadsheet_id, sheet_name)
        return

    if current_headers[:width] == desired_headers:
        freeze_first_row(service, spreadsheet_id, sheet_name)
        return

    source_width = max(width, len(current_headers))
    header_index = {header: index for index, header in enumerate(current_headers) if header}
    old_data = get_values(
        service,
        spreadsheet_id,
        sheet_range(sheet_name, f"A2:{col_letter(source_width)}"),
    )
    new_data = []

    for row in old_data:
        padded = pad_row(row, source_width)
        migrated_row = []
        for header in desired_headers:
            if header == "platform" and header not in header_index:
                migrated_row.append("facebook")
            elif header == "source_type" and header not in header_index:
                migrated_row.append("organic")
            elif header in header_index:
                migrated_row.append(padded[header_index[header]])
            else:
                migrated_row.append("")
        new_data.append(migrated_row)

    update_values(service, spreadsheet_id, sheet_range(sheet_name, f"A1:{col_letter(width)}1"), [desired_headers])
    if new_data:
        update_values(
            service,
            spreadsheet_id,
            sheet_range(sheet_name, f"A2:{col_letter(width)}{len(new_data) + 1}"),
            new_data,
        )
    if source_width > width:
        clear_values(service, spreadsheet_id, sheet_range(sheet_name, f"{col_letter(width + 1)}:{col_letter(source_width)}"))
    freeze_first_row(service, spreadsheet_id, sheet_name)


def write_status(service, spreadsheet_id: str, status: str, message: str) -> None:
    sheet_name = ensure_status_sheet(service, spreadsheet_id)
    append_values(
        service,
        spreadsheet_id,
        sheet_range(sheet_name, "A:C"),
        [[datetime.now(timezone.utc).isoformat(), status, message]],
    )


def get_header_map(service, spreadsheet_id: str, sheet_name: str) -> dict[str, int]:
    rows = get_values(service, spreadsheet_id, sheet_range(sheet_name, f"A1:{col_letter(len(HEADERS))}1"))
    headers = rows[0] if rows else []
    return {str(header).strip(): index + 1 for index, header in enumerate(headers) if header}


def make_row_key(platform: str, source_type: str, ad_id: str, comment_id: str) -> str:
    source = source_type or "organic"
    source_id = str(ad_id or "") if source == "ad" else ""
    return f"{platform or 'facebook'}:{source}:{source_id}:{comment_id or ''}"


def upsert_rows(service, spreadsheet_id: str, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return

    sheet_name = ensure_sheet(service, spreadsheet_id)
    header_map = get_header_map(service, spreadsheet_id, sheet_name)
    id_column = header_map.get("comment_id")
    platform_column = header_map.get("platform")
    source_type_column = header_map.get("source_type")
    ad_id_column = header_map.get("ad_id")

    if not id_column:
        raise RuntimeError("The sheet must contain a comment_id column.")

    existing_values = get_values(
        service,
        spreadsheet_id,
        sheet_range(sheet_name, f"A2:{col_letter(len(HEADERS))}"),
    )
    existing_ids = {}

    for index, existing_row in enumerate(existing_values, start=2):
        padded = pad_row(existing_row, len(HEADERS))
        comment_id = padded[id_column - 1]
        platform = padded[platform_column - 1] if platform_column else "facebook"
        source_type = padded[source_type_column - 1] if source_type_column else "organic"
        ad_id = padded[ad_id_column - 1] if ad_id_column else ""
        if comment_id:
            existing_ids[make_row_key(platform, source_type, ad_id, comment_id)] = index

    updates = []
    appends = []

    for row in rows:
        values = [row.get(header, "") for header in HEADERS]
        existing_row_number = existing_ids.get(
            make_row_key(
                str(row.get("platform", "")),
                str(row.get("source_type", "")),
                str(row.get("ad_id", "")),
                str(row.get("comment_id", "")),
            )
        )
        if existing_row_number:
            updates.append(
                {
                    "range": sheet_range(sheet_name, f"A{existing_row_number}:{col_letter(len(HEADERS))}{existing_row_number}"),
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
        append_values(service, spreadsheet_id, sheet_range(sheet_name, f"A:{col_letter(len(HEADERS))}"), appends)


def graph_url(path: str, params: dict[str, Any], access_token: str) -> str:
    query_params = dict(params)
    query_params["access_token"] = access_token
    query = urlencode(query_params)
    return f"https://graph.facebook.com/{CONFIG['graph_version']}{path}?{query}"


def fetch_json(url: str) -> dict[str, Any]:
    response = requests.get(url, timeout=60)
    try:
        data = response.json() if response.text else {}
    except ValueError as exc:
        raise RuntimeError(f"Meta API returned non-JSON response: {response.text[:500]}") from exc

    if not response.ok:
        error = data.get("error", {}) if isinstance(data, dict) else {}
        message = error.get("message") or response.text
        raise RuntimeError(f"Meta API error {response.status_code}: {message}")

    return data


def get_all_pages(path: str, params: dict[str, Any], access_token: str, max_items: int | None = None) -> list[dict[str, Any]]:
    output = []
    url = graph_url(path, params, access_token)

    while url:
        response = fetch_json(url)
        for item in response.get("data", []):
            output.append(item)
            if max_items and len(output) >= max_items:
                return output
        url = response.get("paging", {}).get("next", "")

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
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def is_within(date_value: str, since_date: datetime, until_date: datetime) -> bool:
    parsed = parse_meta_date(date_value)
    return bool(parsed and since_date <= parsed <= until_date)


def format_date(date_value: datetime) -> str:
    return date_value.astimezone(ZoneInfo(CONFIG["timezone"])).strftime("%Y-%m-%d")


def clean_text(value: Any) -> str:
    return re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()


def resolve_page_access_token(page_id: str, access_token: str) -> str:
    accounts = fetch_json(graph_url("/me/accounts", {"fields": "id,name,access_token", "limit": 100}, access_token))
    for page in accounts.get("data", []):
        if str(page.get("id")) == str(page_id) and page.get("access_token"):
            return page["access_token"]
    return access_token


def fetch_posts_with_comments(page_id: str, post_since_date: datetime, until_date: datetime, comment_since_date: datetime, access_token: str):
    comment_since = to_unix(comment_since_date)
    until = to_unix(until_date)
    comment_fields = ",".join(
        [
            FACEBOOK_COMMENT_FIELDS,
            f"comments.since({comment_since}).until({until}).filter(stream).order(chronological).limit(100){{{FACEBOOK_COMMENT_FIELDS}}}",
        ]
    )
    fields = ",".join(
        [
            "id",
            "message",
            "created_time",
            "permalink_url",
            f"comments.since({comment_since}).until({until}).filter(stream).order(chronological).limit(100){{{comment_fields}}}",
        ]
    )

    return get_all_pages(
        f"/{page_id}/feed",
        {
            "fields": fields,
            "since": to_unix(post_since_date),
            "until": until,
            "limit": CONFIG["max_posts_per_run"],
        },
        access_token,
        CONFIG["max_posts_per_run"],
    )


def fetch_comments(object_id: str, since_date: datetime, until_date: datetime, access_token: str):
    comments = get_all_pages(
        f"/{object_id}/comments",
        {
            "fields": FACEBOOK_COMMENT_FIELDS,
            "filter": "stream",
            "order": "chronological",
            "since": to_unix(since_date),
            "until": to_unix(until_date),
            "limit": 100,
        },
        access_token,
    )
    return [comment for comment in comments if is_within(comment.get("created_time", ""), since_date, until_date)]


def fetch_instagram_media_with_comments(ig_user_id: str, access_token: str):
    fields = ",".join(
        [
            "id",
            "caption",
            "timestamp",
            "permalink",
            "comments_count",
            "comments.limit(100){id,text,timestamp,username,replies.limit(100){id,text,timestamp,username}}",
        ]
    )
    return get_all_pages(
        f"/{ig_user_id}/media",
        {"fields": fields, "limit": CONFIG["max_instagram_media_per_run"]},
        access_token,
        CONFIG["max_instagram_media_per_run"],
    )


def fetch_instagram_media_by_id_with_comments(media_id: str, access_token: str):
    return fetch_json(
        graph_url(
            f"/{media_id}",
            {
                "fields": "id,caption,timestamp,permalink,comments_count,comments.limit(100){id,text,timestamp,username,replies.limit(100){id,text,timestamp,username}}"
            },
            access_token,
        )
    )


def fetch_ads(ad_account_id: str, access_token: str):
    return get_all_pages(
        f"/{ad_account_id}/ads",
        {"fields": "id,name,creative{id}", "limit": CONFIG["max_ads_per_run"]},
        access_token,
        CONFIG["max_ads_per_run"],
    )


def fetch_ad_creative(creative_id: str, access_token: str):
    return fetch_json(
        graph_url(
            f"/{creative_id}",
            {"fields": "id,effective_object_story_id,effective_instagram_media_id"},
            access_token,
        )
    )


def fetch_facebook_object(object_id: str, access_token: str):
    return fetch_json(
        graph_url(
            f"/{object_id}",
            {"fields": "id,message,created_time,permalink_url"},
            access_token,
        )
    )


def add_facebook_comment_row(rows, seen, post, comment, parent_id, since_date, until_date, source_type, ad_id, ad_name):
    row_key = make_row_key("facebook", source_type or "organic", ad_id or "", comment.get("id", ""))
    if not comment.get("id") or row_key in seen:
        return
    seen.add(row_key)

    rows.append(
        {
            "platform": "facebook",
            "comment_id": comment.get("id", ""),
            "parent_comment_id": parent_id or comment.get("parent", {}).get("id", ""),
            "post_id": post.get("id", ""),
            "post_created_time": post.get("created_time", ""),
            "post_message": clean_text(post.get("message", "")),
            "post_url": post.get("permalink_url", ""),
            "comment_created_time": comment.get("created_time", ""),
            "commenter_name": comment.get("from", {}).get("name", ""),
            "commenter_id": comment.get("from", {}).get("id", ""),
            "comment_message": clean_text(comment.get("message", "")),
            "comment_url": comment.get("permalink_url", ""),
            "like_count": comment.get("like_count", 0),
            "reply_count": comment.get("comment_count", 0),
            "collected_window_start": format_date(since_date),
            "collected_window_end": format_date(until_date),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_type": source_type or "organic",
            "ad_id": ad_id or "",
            "ad_name": ad_name or "",
        }
    )


def add_instagram_comment_row(rows, seen, media, comment, parent_id, since_date, until_date, source_type, ad_id, ad_name):
    row_key = make_row_key("instagram", source_type or "organic", ad_id or "", comment.get("id", ""))
    if not comment.get("id") or row_key in seen:
        return
    seen.add(row_key)

    rows.append(
        {
            "platform": "instagram",
            "comment_id": comment.get("id", ""),
            "parent_comment_id": parent_id or "",
            "post_id": media.get("id", ""),
            "post_created_time": media.get("timestamp", ""),
            "post_message": clean_text(media.get("caption", "")),
            "post_url": media.get("permalink", ""),
            "comment_created_time": comment.get("timestamp", ""),
            "commenter_name": comment.get("username", ""),
            "commenter_id": "",
            "comment_message": clean_text(comment.get("text", "")),
            "comment_url": media.get("permalink", ""),
            "like_count": "",
            "reply_count": len(comment.get("replies", {}).get("data", [])),
            "collected_window_start": format_date(since_date),
            "collected_window_end": format_date(until_date),
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "source_type": source_type or "organic",
            "ad_id": ad_id or "",
            "ad_name": ad_name or "",
        }
    )


def sync_meta_comments_daily() -> None:
    spreadsheet_id = required_env("SPREADSHEET_ID")
    access_token = required_env("META_PAGE_ACCESS_TOKEN")
    service = get_sheets_service()

    ensure_sheet(service, spreadsheet_id)
    now = datetime.now(timezone.utc)
    comment_since = now - timedelta(days=CONFIG["comment_lookback_days"])
    post_since = now - timedelta(days=CONFIG["post_lookback_days"])
    page_access_token = resolve_page_access_token(CONFIG["page_id"], access_token)

    write_status(service, spreadsheet_id, "RUNNING", "Fetching Facebook posts/comments and Instagram media/comments...")

    rows = []
    seen = set()
    facebook_comments = 0
    facebook_replies = 0
    instagram_comments = 0
    instagram_replies = 0
    facebook_ad_comments = 0
    facebook_ad_replies = 0
    instagram_ad_comments = 0
    instagram_ad_replies = 0

    posts = fetch_posts_with_comments(CONFIG["page_id"], post_since, now, comment_since, page_access_token)
    write_status(service, spreadsheet_id, "RUNNING", f"Fetched {len(posts)} recent Facebook posts. Preparing rows...")

    for post in posts:
        comments = post.get("comments", {}).get("data", [])
        facebook_comments += len(comments)
        for comment in comments:
            add_facebook_comment_row(rows, seen, post, comment, "", comment_since, now, "organic", "", "")

            replies = comment.get("comments", {}).get("data", [])
            facebook_replies += len(replies)
            for reply in replies:
                add_facebook_comment_row(rows, seen, post, reply, comment.get("id", ""), comment_since, now, "organic", "", "")

    media = fetch_instagram_media_with_comments(CONFIG["ig_user_id"], access_token)
    write_status(service, spreadsheet_id, "RUNNING", f"Fetched {len(media)} recent Instagram media items. Preparing rows...")

    for item in media:
        comments = [
            comment
            for comment in item.get("comments", {}).get("data", [])
            if is_within(comment.get("timestamp", ""), comment_since, now)
        ]
        instagram_comments += len(comments)

        for comment in comments:
            add_instagram_comment_row(rows, seen, item, comment, "", comment_since, now, "organic", "", "")

            replies = [
                reply
                for reply in comment.get("replies", {}).get("data", [])
                if is_within(reply.get("timestamp", ""), comment_since, now)
            ]
            instagram_replies += len(replies)
            for reply in replies:
                add_instagram_comment_row(rows, seen, item, reply, comment.get("id", ""), comment_since, now, "organic", "", "")

    ads = fetch_ads(CONFIG["ad_account_id"], access_token)
    write_status(service, spreadsheet_id, "RUNNING", f"Fetched {len(ads)} ads. Looking for ad post/media comments...")

    for ad in ads:
        creative_id = ad.get("creative", {}).get("id")
        if not creative_id:
            continue

        creative = fetch_ad_creative(creative_id, access_token)

        if creative.get("effective_object_story_id"):
            object_story_id = creative["effective_object_story_id"]
            ad_post = fetch_facebook_object(object_story_id, page_access_token)
            comments = fetch_comments(object_story_id, comment_since, now, page_access_token)
            facebook_ad_comments += len(comments)

            for comment in comments:
                add_facebook_comment_row(
                    rows,
                    seen,
                    ad_post,
                    comment,
                    "",
                    comment_since,
                    now,
                    "ad",
                    ad.get("id", ""),
                    ad.get("name", ""),
                )

                replies = fetch_comments(comment.get("id", ""), comment_since, now, page_access_token)
                facebook_ad_replies += len(replies)
                for reply in replies:
                    add_facebook_comment_row(
                        rows,
                        seen,
                        ad_post,
                        reply,
                        comment.get("id", ""),
                        comment_since,
                        now,
                        "ad",
                        ad.get("id", ""),
                        ad.get("name", ""),
                    )

        if creative.get("effective_instagram_media_id"):
            ad_media = fetch_instagram_media_by_id_with_comments(creative["effective_instagram_media_id"], access_token)
            comments = [
                comment
                for comment in ad_media.get("comments", {}).get("data", [])
                if is_within(comment.get("timestamp", ""), comment_since, now)
            ]
            instagram_ad_comments += len(comments)

            for comment in comments:
                add_instagram_comment_row(
                    rows,
                    seen,
                    ad_media,
                    comment,
                    "",
                    comment_since,
                    now,
                    "ad",
                    ad.get("id", ""),
                    ad.get("name", ""),
                )

                replies = [
                    reply
                    for reply in comment.get("replies", {}).get("data", [])
                    if is_within(reply.get("timestamp", ""), comment_since, now)
                ]
                instagram_ad_replies += len(replies)
                for reply in replies:
                    add_instagram_comment_row(
                        rows,
                        seen,
                        ad_media,
                        reply,
                        comment.get("id", ""),
                        comment_since,
                        now,
                        "ad",
                        ad.get("id", ""),
                        ad.get("name", ""),
                    )

    upsert_rows(service, spreadsheet_id, rows)
    write_status(
        service,
        spreadsheet_id,
        "OK",
        "Facebook: "
        f"{len(posts)} posts, {facebook_comments} comments, {facebook_replies} replies. "
        f"Instagram: {len(media)} media, {instagram_comments} comments, {instagram_replies} replies. "
        f"Ads: {facebook_ad_comments} FB comments, {facebook_ad_replies} FB replies, "
        f"{instagram_ad_comments} IG comments, {instagram_ad_replies} IG replies. "
        f"Wrote or updated {len(rows)} rows.",
    )


def main() -> None:
    spreadsheet_id = os.getenv("SPREADSHEET_ID")
    service = None
    try:
        sync_meta_comments_daily()
    except Exception as exc:
        if spreadsheet_id:
            try:
                service = service or get_sheets_service()
                write_status(service, spreadsheet_id, "ERROR", str(exc))
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
