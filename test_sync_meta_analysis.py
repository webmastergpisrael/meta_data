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

    def test_comment_quota_is_water_filled_equally_and_redistributes_unused_share(self):
        comments = [
            {
                "comment_id": "short-1",
                "post_id": "short",
                "comment_created_time": "2026-07-01T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "busy-1",
                "post_id": "busy",
                "comment_created_time": "2026-07-02T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "busy-2",
                "post_id": "busy",
                "comment_created_time": "2026-07-03T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "busy-3",
                "post_id": "busy",
                "comment_created_time": "2026-07-04T00:00:00+00:00",
                "is_brand_comment": False,
            },
        ]

        selected, brand_count, post_count = sync.select_comments_fairly(comments, max_comments=4)

        self.assertEqual(brand_count, 0)
        self.assertEqual(post_count, 2)
        self.assertEqual(
            {comment["comment_id"] for comment in selected},
            {"short-1", "busy-1", "busy-2", "busy-3"},
        )

    def test_brand_replies_are_selected_before_audience_comments(self):
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

        selected, brand_count, post_count = sync.select_comments_fairly(comments, max_comments=1)

        self.assertEqual(brand_count, 1)
        self.assertEqual(post_count, 0)
        self.assertEqual(selected[0]["comment_id"], "brand")

    def test_fractional_comment_slots_go_to_posts_with_newest_comments(self):
        comments = [
            {
                "comment_id": "old-post-comment",
                "post_id": "old-post",
                "comment_created_time": "2026-07-01T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "middle-post-comment",
                "post_id": "middle-post",
                "comment_created_time": "2026-07-02T00:00:00+00:00",
                "is_brand_comment": False,
            },
            {
                "comment_id": "new-post-comment",
                "post_id": "new-post",
                "comment_created_time": "2026-07-03T00:00:00+00:00",
                "is_brand_comment": False,
            },
        ]

        selected, _, _ = sync.select_comments_fairly(comments, max_comments=2)

        self.assertEqual(
            {comment["comment_id"] for comment in selected},
            {"middle-post-comment", "new-post-comment"},
        )

    def test_existing_scan_jobs_reserve_monthly_audits_then_use_oldest_delta(self):
        now = datetime(2026, 7, 31, tzinfo=timezone.utc)
        recent_full = datetime(2026, 7, 15, tzinfo=timezone.utc)
        old_full = datetime(2026, 6, 1, tzinfo=timezone.utc)
        posts = [
            {
                "post_id": f"post-{index}",
                "platform": "facebook",
                "post_created_time": f"2026-01-{index + 1:02d}T00:00:00+00:00",
                "_last_scanned_at": datetime(2026, 7, index + 1, tzinfo=timezone.utc),
                "_last_full_scan_at": old_full if index < 2 else recent_full,
            }
            for index in range(6)
        ]

        jobs, audit_count, backlog = sync.select_existing_scan_jobs(posts, 3, now, 30)

        self.assertEqual(audit_count, 1)
        self.assertEqual(backlog, 2)
        self.assertEqual(jobs[0]["post_id"], "post-0")
        self.assertTrue(jobs[0]["_full_scan"])
        self.assertEqual([job["post_id"] for job in jobs[1:]], ["post-1", "post-2"])
        self.assertTrue(all(not job["_full_scan"] for job in jobs[1:]))

    def test_old_existing_post_is_scanned_directly_but_not_returned_for_post_analysis(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        new_comment = {
            "id": "new-comment",
            "created_time": "2026-07-30T12:00:00+00:00",
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
            patch.object(sync, "fetch_facebook_posts", return_value=[]),
            patch.object(sync, "fetch_facebook_comments_with_replies", return_value=[new_comment]) as fetch_comments,
            patch.object(sync, "fetch_instagram_media", return_value=[]),
            patch.object(sync, "fetch_ads", return_value=[]),
        ):
            posts, comments, scanned_posts = sync.collect_rows(
                {"existing-post"},
                set(),
                existing_post_last_scanned={"existing-post": datetime(2026, 7, 30, tzinfo=timezone.utc)},
                existing_posts=[
                    {
                        "post_id": "existing-post",
                        "platform": "facebook",
                        "post_created_time": "2025-01-01T00:00:00+00:00",
                        "post_url": "https://example.com/post",
                        "_last_scanned_at": datetime(2026, 7, 30, tzinfo=timezone.utc),
                        "_last_full_scan_at": datetime(2026, 7, 15, tzinfo=timezone.utc),
                    }
                ],
            )

        self.assertEqual(posts, [])
        self.assertEqual([comment["comment_id"] for comment in comments], ["new-comment"])
        self.assertEqual([post["post_id"] for post in scanned_posts], ["existing-post"])
        self.assertEqual(
            fetch_comments.call_args.args[1],
            datetime(2026, 7, 29, tzinfo=timezone.utc),
        )

    def test_new_content_is_selected_before_existing_content(self):
        start = datetime(2026, 7, 1, tzinfo=timezone.utc)
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        posts_from_meta = [
            {
                "id": "older-new-post",
                "created_time": "2026-07-02T00:00:00+00:00",
                "message": "Older but not stored",
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
                existing_post_last_scanned={"existing-post": datetime(2026, 7, 1, tzinfo=timezone.utc)},
                existing_posts=[
                    {
                        "post_id": "existing-post",
                        "platform": "facebook",
                        "post_created_time": "2025-01-01T00:00:00+00:00",
                        "_last_scanned_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                        "_last_full_scan_at": datetime(2026, 7, 1, tzinfo=timezone.utc),
                    }
                ],
            )

        self.assertEqual([post["post_id"] for post in posts], ["older-new-post"])
        self.assertEqual(comments, [])
        self.assertEqual([post["post_id"] for post in scanned_posts], ["older-new-post"])
        self.assertEqual(fetch_comments.call_args.args[0], "older-new-post")

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

    def test_full_audit_collects_new_reply_under_old_root(self):
        end = datetime(2026, 7, 31, tzinfo=timezone.utc)
        comments = []
        raw_comments = [
            {
                "id": "old-root",
                "created_time": "2025-01-01T00:00:00+00:00",
                "message": "Old root",
                "from": {"id": "old-user", "name": "Old User"},
                "comments": {
                    "data": [
                        {
                            "id": "new-reply",
                            "created_time": "2026-07-30T00:00:00+00:00",
                            "message": "New reply",
                            "from": {"id": "new-user", "name": "New User"},
                        }
                    ]
                },
            }
        ]

        sync.collect_visible_comments(
            comments,
            {"old-root"},
            "facebook",
            "post",
            raw_comments,
            "https://example.com/post",
            None,
            end,
        )

        self.assertEqual([comment["comment_id"] for comment in comments], ["new-reply"])

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

    def test_scan_checkpoint_waits_until_all_discovered_comments_are_completed(self):
        post_row = [""] * len(sync.POST_HEADERS)
        post_row[sync.POST_HEADERS.index("post_id")] = "existing-post"
        scanned_post = {
            "post_id": "existing-post",
            "collected_window_start": "2026-07-01T00:00:00+00:00",
            "collected_window_end": "2026-07-31T00:00:00+00:00",
            "_new_comment_ids": {"comment-1", "comment-2"},
        }
        service = object()

        with (
            patch.object(sync, "get_values", return_value=[post_row]),
            patch.object(sync, "batch_update_values") as batch_update,
        ):
            advanced, held = sync.update_scanned_post_checkpoints(
                service,
                "spreadsheet",
                [scanned_post],
                [{"comment_id": "comment-1"}],
            )

        self.assertEqual((advanced, held), (0, 1))
        batch_update.assert_called_once_with(service, "spreadsheet", [])

    def test_completed_full_scan_updates_delta_and_full_scan_checkpoints(self):
        post_row = [""] * len(sync.POST_HEADERS)
        post_row[sync.POST_HEADERS.index("post_id")] = "existing-post"
        scanned_post = {
            "post_id": "existing-post",
            "collected_window_start": "2026-07-01T00:00:00+00:00",
            "collected_window_end": "2026-07-31T00:00:00+00:00",
            "_new_comment_ids": {"comment-1"},
            "_full_scan": True,
        }
        service = object()

        with (
            patch.object(sync, "get_values", return_value=[post_row]),
            patch.object(sync, "batch_update_values") as batch_update,
        ):
            advanced, held = sync.update_scanned_post_checkpoints(
                service,
                "spreadsheet",
                [scanned_post],
                [{"comment_id": "comment-1"}],
            )

        self.assertEqual((advanced, held), (1, 0))
        updates = batch_update.call_args.args[2]
        self.assertEqual(len(updates), 2)
        self.assertEqual(updates[1]["values"], [["2026-07-31T00:00:00+00:00"]])

    def test_brand_reply_zeros_root_and_all_siblings_before_and_after(self):
        comments = [
            {
                "comment_id": "thread-root",
                "post_id": "post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T00:00:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.9,
            },
            {
                "comment_id": "before-brand",
                "post_id": "post",
                "parent_comment_id": "thread-root",
                "comment_created_time": "2026-07-02T12:00:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.8,
            },
            {
                "comment_id": "brand-reply",
                "post_id": "post",
                "parent_comment_id": "thread-root",
                "comment_created_time": "2026-07-03T00:00:00+00:00",
                "is_brand_comment": True,
                "response_value_score": "",
            },
            {
                "comment_id": "after-brand",
                "post_id": "post",
                "parent_comment_id": "thread-root",
                "comment_created_time": "2026-07-04T00:00:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.7,
            },
        ]

        sync.zero_response_scores_for_brand_threads(comments)

        self.assertEqual(comments[0]["response_value_score"], 0.0)
        self.assertEqual(comments[1]["response_value_score"], 0.0)
        self.assertEqual(comments[2]["response_value_score"], "")
        self.assertEqual(comments[3]["response_value_score"], 0.0)

    def test_brand_participation_zeros_every_user_in_multi_user_thread(self):
        comments = [
            {
                "comment_id": "thread-root",
                "post_id": "post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T12:00:00+00:00",
                "commenter_name": "Root User",
                "is_brand_comment": False,
                "response_value_score": 0.75,
            },
            {
                "comment_id": "first-user",
                "post_id": "post",
                "parent_comment_id": "thread-root",
                "comment_created_time": "2026-07-02T13:09:00+00:00",
                "commenter_name": "First User",
                "comment_message": "What does Greenpeace say about this?",
                "is_brand_comment": False,
                "response_value_score": 0.85,
            },
            {
                "comment_id": "second-user",
                "post_id": "post",
                "parent_comment_id": "thread-root",
                "comment_created_time": "2026-07-02T13:15:00+00:00",
                "commenter_name": "Second User",
                "comment_message": "I also want an answer.",
                "is_brand_comment": False,
                "response_value_score": 0.8,
            },
            {
                "comment_id": "brand-sibling",
                "post_id": "post",
                "parent_comment_id": "thread-root",
                "comment_created_time": "2026-07-02T13:33:00+00:00",
                "commenter_name": "Greenpeace Israel",
                "comment_message": "Meirav, our position is clear.",
                "is_brand_comment": True,
                "response_value_score": "",
            },
        ]

        sync.zero_response_scores_for_brand_threads(comments)

        self.assertEqual(comments[0]["response_value_score"], 0.0)
        self.assertEqual(comments[1]["response_value_score"], 0.0)
        self.assertEqual(comments[2]["response_value_score"], 0.0)

    def test_brand_reply_to_deep_comment_zeros_entire_connected_chain(self):
        comments = [
            {
                "comment_id": "root",
                "post_id": "post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T13:00:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.8,
            },
            {
                "comment_id": "level-1",
                "post_id": "post",
                "parent_comment_id": "root",
                "comment_created_time": "2026-07-02T13:09:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.85,
            },
            {
                "comment_id": "level-2",
                "post_id": "post",
                "parent_comment_id": "level-1",
                "comment_created_time": "2026-07-02T13:20:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.7,
            },
            {
                "comment_id": "brand-deep-reply",
                "post_id": "post",
                "parent_comment_id": "level-2",
                "comment_created_time": "2026-07-02T13:33:00+00:00",
                "is_brand_comment": True,
                "response_value_score": "",
            },
        ]

        sync.zero_response_scores_for_brand_threads(comments)

        self.assertEqual(comments[0]["response_value_score"], 0.0)
        self.assertEqual(comments[1]["response_value_score"], 0.0)
        self.assertEqual(comments[2]["response_value_score"], 0.0)

    def test_other_threads_and_posts_remain_unchanged(self):
        comments = [
            {
                "comment_id": "answered-root",
                "post_id": "post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T13:00:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.8,
            },
            {
                "comment_id": "brand",
                "post_id": "post",
                "parent_comment_id": "answered-root",
                "comment_created_time": "2026-07-02T13:09:00+00:00",
                "is_brand_comment": True,
                "response_value_score": "",
            },
            {
                "comment_id": "unanswered-root",
                "post_id": "post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T13:33:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.85,
            },
            {
                "comment_id": "answered-root",
                "post_id": "other-post",
                "parent_comment_id": "",
                "comment_created_time": "2026-07-02T13:45:00+00:00",
                "is_brand_comment": False,
                "response_value_score": 0.9,
            },
        ]

        sync.zero_response_scores_for_brand_threads(comments)

        self.assertEqual(comments[0]["response_value_score"], 0.0)
        self.assertEqual(comments[2]["response_value_score"], 0.85)
        self.assertEqual(comments[3]["response_value_score"], 0.9)

    def test_top_level_brand_comment_does_not_mark_thread_answered(self):
        comments = [
            {
                "comment_id": "brand-root",
                "post_id": "post",
                "parent_comment_id": "",
                "is_brand_comment": True,
                "response_value_score": "",
            },
            {
                "comment_id": "audience-reply",
                "post_id": "post",
                "parent_comment_id": "brand-root",
                "is_brand_comment": False,
                "response_value_score": 0.85,
            },
        ]

        sync.zero_response_scores_for_brand_threads(comments)

        self.assertEqual(comments[1]["response_value_score"], 0.85)

    def test_existing_sheet_rows_are_zeroed_for_entire_brand_thread(self):
        headers = [
            "comment_id",
            "post_id",
            "parent_comment_id",
            "is_brand_comment",
            "response_value_score",
        ]
        rows = [
            ["root", "post", "", False, 0.9],
            ["audience-before", "post", "root", False, 0.8],
            ["brand", "post", "root", True, ""],
            ["audience-after", "post", "root", False, 0.7],
            ["other-root", "post", "", False, 0.85],
        ]

        sync.zero_brand_thread_scores_in_rows(rows, headers)

        self.assertEqual([row[4] for row in rows], [0, 0, "", 0, 0.85])

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
