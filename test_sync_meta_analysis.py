import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import sync_meta_analysis as sync


class MetaAnalysisCollectionTests(unittest.TestCase):
    def test_oldest_new_comments_are_selected_globally(self):
        comments = [
            {"comment_id": "newest", "comment_created_time": "2026-07-03T00:00:00+00:00"},
            {"comment_id": "oldest", "comment_created_time": "2026-07-01T00:00:00+00:00"},
            {"comment_id": "middle", "comment_created_time": "2026-07-02T00:00:00+00:00"},
        ]

        selected = sync.select_oldest_comments(comments, 2)

        self.assertEqual([comment["comment_id"] for comment in selected], ["oldest", "middle"])

    def test_invalid_global_comment_limit_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "MAX_COMMENTS_PER_RUN"):
            sync.select_oldest_comments([], 0)

    def test_existing_post_is_scanned_but_not_returned_for_analysis(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        facebook_post = {
            "id": "existing-post",
            "created_time": "2026-07-02T00:00:00+00:00",
            "message": "Existing post",
            "permalink_url": "https://example.com/post",
        }
        new_comment = {
            "id": "new-comment",
            "created_time": "2026-07-03T00:00:00+00:00",
            "message": "New comment",
            "from": {"id": "audience", "name": "Audience"},
        }

        with (
            patch.dict(sync.CONFIG, {"max_comments_per_run": 500}),
            patch.object(sync, "meta_access_token", return_value="token"),
            patch.object(sync, "collection_window", return_value=(start, end)),
            patch.object(
                sync,
                "discover_meta_context",
                return_value={
                    "page_id": "page",
                    "page_access_token": "page-token",
                    "ig_user_id": "",
                    "ad_account_id": "",
                },
            ),
            patch.object(sync, "fetch_facebook_posts", return_value=[facebook_post]),
            patch.object(sync, "fetch_facebook_comments_with_replies", return_value=[new_comment]),
            patch.object(sync, "fetch_instagram_media", return_value=[]),
            patch.object(sync, "fetch_ads", return_value=[]),
        ):
            posts, comments, scanned_posts = sync.collect_rows({"existing-post"}, set())

        self.assertEqual(posts, [])
        self.assertEqual([comment["comment_id"] for comment in comments], ["new-comment"])
        self.assertEqual([post["post_id"] for post in scanned_posts], ["existing-post"])

    def test_existing_comment_is_not_collected_again(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        comments = []
        seen = {"existing-comment"}
        raw_comments = [
            {
                "id": "existing-comment",
                "created_time": "2026-07-03T00:00:00+00:00",
                "message": "Already stored",
                "from": {"id": "audience", "name": "Audience"},
            }
        ]

        sync.collect_visible_comments(
            comments,
            seen,
            "facebook",
            "post",
            raw_comments,
            "https://example.com/post",
            start,
            end,
        )

        self.assertEqual(comments, [])

    def test_later_brand_reply_zeros_existing_audience_score(self):
        comments = [
            {
                "comment_id": "audience-comment",
                "post_id": "post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T00:00:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.9,
            },
            {
                "comment_id": "brand-reply",
                "post_id": "post",
                "parent_comment_id": "audience-comment",
                "comment_created_time": "2026-07-03T00:00:00+00:00",
                "is_brand_comment": True,
                "response_value_score": "",
            },
        ]

        sync.zero_response_scores_for_answered_comments(comments)

        self.assertEqual(comments[0]["response_value_score"], 0.0)

    def test_meta_pagination_continues_until_no_next_page(self):
        pages = [
            {"data": [{"id": "1"}], "paging": {"next": "https://next-page"}},
            {"data": [{"id": "2"}], "paging": {}},
        ]

        with patch.object(sync, "fetch_json", side_effect=pages) as fetch:
            items = sync.get_all_pages("/items", {"limit": 100}, "token", 0)

        self.assertEqual([item["id"] for item in items], ["1", "2"])
        self.assertEqual(fetch.call_count, 2)


if __name__ == "__main__":
    unittest.main()
