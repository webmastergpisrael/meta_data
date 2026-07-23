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

    def test_existing_posts_receive_comment_coverage_before_global_fill(self):
        comments = [
            {
                "comment_id": "busy-1",
                "post_id": "busy-existing",
                "comment_created_time": "2026-07-01T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "busy-2",
                "post_id": "busy-existing",
                "comment_created_time": "2026-07-02T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "new-post-oldest",
                "post_id": "new-post",
                "comment_created_time": "2026-06-30T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "uncovered-1",
                "post_id": "uncovered-existing",
                "comment_created_time": "2026-07-03T00:00:00+00:00",
                "is_brand_comment": False,
            },
        ]

        selected, coverage_count = sync.select_comments_with_existing_post_coverage(
            comments,
            max_comments=3,
            existing_post_ids={"busy-existing", "uncovered-existing"},
            existing_audience_counts={"busy-existing": 100},
            per_post_coverage=1,
        )

        self.assertEqual(coverage_count, 2)
        self.assertEqual(
            {comment["comment_id"] for comment in selected},
            {"busy-1", "uncovered-1", "new-post-oldest"},
        )

    def test_brand_replies_do_not_consume_existing_post_coverage(self):
        comments = [
            {
                "comment_id": "brand",
                "post_id": "existing",
                "comment_created_time": "2026-07-01T00:00:00+00:00",
                "is_brand_comment": True,
            },
            {
                "comment_id": "audience",
                "post_id": "existing",
                "comment_created_time": "2026-07-02T00:00:00+00:00",
                "is_brand_comment": False,
            },
        ]

        selected, coverage_count = sync.select_comments_with_existing_post_coverage(
            comments,
            max_comments=1,
            existing_post_ids={"existing"},
            existing_audience_counts={},
            per_post_coverage=1,
        )

        self.assertEqual(coverage_count, 1)
        self.assertEqual(selected[0]["comment_id"], "audience")

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

    def test_existing_content_is_selected_before_older_new_content(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        posts_from_meta = [
            {
                "id": "older-new-post",
                "created_time": "2026-07-02T00:00:00+00:00",
                "message": "Older but not stored",
            },
            {
                "id": "existing-post",
                "created_time": "2026-07-03T00:00:00+00:00",
                "message": "Already stored",
            },
        ]

        with (
            patch.dict(sync.CONFIG, {"max_content_items_per_run": 1, "max_comments_per_run": 500}),
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
            patch.object(sync, "fetch_facebook_posts", return_value=posts_from_meta),
            patch.object(sync, "fetch_facebook_comments_with_replies", return_value=[]) as fetch_comments,
            patch.object(sync, "fetch_instagram_media", return_value=[]),
            patch.object(sync, "fetch_ads", return_value=[]),
        ):
            posts, comments, scanned_posts = sync.collect_rows(
                {"existing-post"},
                set(),
                {"existing-post": 0},
            )

        self.assertEqual(posts, [])
        self.assertEqual(comments, [])
        self.assertEqual([post["post_id"] for post in scanned_posts], ["existing-post"])
        self.assertEqual(fetch_comments.call_args.args[0], "existing-post")

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

    def test_reply_context_uses_structural_parent_not_previous_sibling(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        comments = []
        raw_comments = [
            {
                "id": "root",
                "created_time": "2026-07-02T00:00:00+00:00",
                "message": "Root question",
                "from": {"id": "first-user", "name": "First User"},
                "comments": {
                    "data": [
                        {
                            "id": "reply-1",
                            "created_time": "2026-07-03T00:00:00+00:00",
                            "message": "First sibling reply",
                            "from": {"id": "second-user", "name": "Second User"},
                        },
                        {
                            "id": "reply-2",
                            "created_time": "2026-07-04T00:00:00+00:00",
                            "message": "Second sibling reply",
                            "from": {"id": "third-user", "name": "Third User"},
                        },
                    ]
                },
            }
        ]

        sync.collect_visible_comments(
            comments,
            set(),
            "facebook",
            "post",
            raw_comments,
            "https://example.com/post",
            start,
            end,
        )

        second_reply = next(comment for comment in comments if comment["comment_id"] == "reply-2")
        self.assertEqual(second_reply["parent_comment_id"], "root")
        self.assertEqual(second_reply["parent_comment_message"], "Root question")
        self.assertEqual(second_reply["_parent_commenter_name"], "First User")

    def test_user_to_user_reply_score_is_forced_to_zero(self):
        comment = {
            "comment_message": "יש לך הוכחות?",
            "parent_comment_id": "audience-parent",
            "_parent_is_brand_comment": False,
            "comment_intent": "question",
            "response_value_score": 0.85,
        }

        changed = sync.enforce_response_routing(
            comment,
            {"reply_target": "another_user", "criticism_target": "another_user"},
        )

        self.assertTrue(changed)
        self.assertEqual(comment["response_value_score"], 0.0)

    def test_reply_directed_to_greenpeace_keeps_score(self):
        comment = {
            "comment_message": "Greenpeace Israel, תביאו הוכחות",
            "parent_comment_id": "audience-parent",
            "_parent_is_brand_comment": False,
            "comment_intent": "criticism",
            "response_value_score": 0.85,
        }

        changed = sync.enforce_response_routing(
            comment,
            {"reply_target": "greenpeace", "criticism_target": "greenpeace"},
        )

        self.assertFalse(changed)
        self.assertEqual(comment["response_value_score"], 0.85)

    def test_reply_to_brand_comment_keeps_score(self):
        comment = {
            "comment_message": "אז איפה חותמים?",
            "parent_comment_id": "brand-parent",
            "_parent_is_brand_comment": True,
            "comment_intent": "information_request",
            "response_value_score": 0.8,
        }

        changed = sync.enforce_response_routing(
            comment,
            {"reply_target": "greenpeace", "criticism_target": "unclear"},
        )

        self.assertFalse(changed)
        self.assertEqual(comment["response_value_score"], 0.8)

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

    def test_deadline_leaves_unanalyzed_audience_comment_for_next_run(self):
        comment = {
            "comment_id": "new-comment",
            "post_id": "existing-post",
            "comment_message": "Needs analysis",
            "is_brand_comment": False,
        }
        post = {
            "post_id": "existing-post",
            "post_message": "Context",
            "platform": "facebook",
            "source_type": "organic",
        }

        with patch.object(sync, "analysis_time_available", return_value=False):
            completed = sync.analyze_comments([comment], [post])

        self.assertEqual(completed, [])

    def test_brand_comment_can_be_written_without_gemini_at_deadline(self):
        comment = {
            "comment_id": "brand-comment",
            "post_id": "existing-post",
            "comment_message": "Official reply",
            "is_brand_comment": True,
        }
        post = {
            "post_id": "existing-post",
            "post_message": "Context",
            "platform": "facebook",
            "source_type": "organic",
        }

        with patch.object(sync, "analysis_time_available", return_value=False):
            completed = sync.analyze_comments([comment], [post])

        self.assertEqual([item["comment_id"] for item in completed], ["brand-comment"])
        self.assertEqual(completed[0]["response_value_score"], "")


if __name__ == "__main__":
    unittest.main()
