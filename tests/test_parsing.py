import unittest

from pyxcom.parse import (
    bottom_cursor,
    parse_post,
    parse_profile,
    timeline_posts,
    timeline_primary_posts,
)


class ParsingTests(unittest.TestCase):
    def test_modern_profile_and_long_post(self):
        profile = parse_profile(
            {
                "data": {
                    "user": {
                        "result": {
                            "rest_id": "123",
                            "core": {
                                "screen_name": "sample",
                                "name": "Sample",
                                "created_at": "Mon Sep 29 07:39:00 +0000 2025",
                            },
                            "profile_bio": {"description": "Research"},
                            "relationship_counts": {"followers": 12, "following": 3},
                            "tweet_counts": {"tweets": 15, "media_tweets": 2},
                            "is_blue_verified": False,
                        }
                    }
                }
            }
        )
        self.assertEqual(profile.handle, "sample")
        self.assertEqual(profile.followers_count, 12)
        self.assertEqual(profile.posts_count, 15)
        result = {
            "rest_id": "9876543210",
            "core": {"user_results": {"result": {"core": {"screen_name": "sample"}}}},
            "legacy": {
                "user_id_str": "123",
                "created_at": "Mon Sep 29 07:39:00 +0000 2025",
                "full_text": "short",
                "favorite_count": 7,
                "entities": {"hashtags": [{"text": "AI"}]},
            },
            "note_tweet": {
                "note_tweet_results": {"result": {"text": "full long text"}}
            },
            "views": {"count": "42"},
        }
        post = parse_post(result)
        self.assertEqual(post.text, "full long text")
        self.assertEqual(post.view_count, 42)
        self.assertEqual(post.author_handle, "sample")
        self.assertEqual(post.hashtags, ["AI"])

    def test_missing_verification_is_unknown_not_false(self):
        profile = parse_profile({"rest_id": "123", "core": {"screen_name": "sample"}})
        self.assertIsNone(profile.verified)

    def test_timeline_dedup_and_bottom_cursor(self):
        tweet = {
            "rest_id": "9876543210",
            "core": {"user_results": {"result": {"core": {"screen_name": "sample"}}}},
            "legacy": {
                "user_id_str": "123",
                "created_at": "Mon Sep 29 07:39:00 +0000 2025",
                "full_text": "hello",
            },
        }
        data = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "entries": [
                                            {
                                                "entryId": "tweet-1",
                                                "content": {
                                                    "itemContent": {
                                                        "tweet_results": {
                                                            "result": tweet
                                                        }
                                                    }
                                                },
                                            },
                                            {
                                                "entryId": "tweet-2",
                                                "content": {
                                                    "itemContent": {
                                                        "tweet_results": {
                                                            "result": tweet
                                                        }
                                                    }
                                                },
                                            },
                                            {
                                                "entryId": "cursor-bottom-1",
                                                "content": {"value": "next-cursor"},
                                            },
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(len(timeline_posts(data)), 1)
        self.assertEqual(len(timeline_primary_posts(data)), 1)
        self.assertEqual(bottom_cursor(data), "next-cursor")

    def test_primary_posts_ignore_nested_quote_and_empty_cursor_page(self):
        quote = {
            "rest_id": "1234567891",
            "core": {"user_results": {"result": {"core": {"screen_name": "sample"}}}},
            "legacy": {
                "user_id_str": "123",
                "created_at": "Tue Sep 29 07:39:00 +0000 2026",
                "full_text": "newer quote",
            },
        }
        old_post = {
            "rest_id": "1234567890",
            "core": {"user_results": {"result": {"core": {"screen_name": "sample"}}}},
            "legacy": {
                "user_id_str": "123",
                "created_at": "Wed May 29 07:39:00 +0000 2024",
                "full_text": "older timeline post",
            },
            "quoted_status_result": {"tweet_results": {"result": quote}},
        }
        data = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "entries": [
                                            {
                                                "entryId": "tweet-1234567890",
                                                "content": {
                                                    "itemContent": {
                                                        "tweet_results": {
                                                            "result": old_post
                                                        }
                                                    }
                                                },
                                            },
                                            {
                                                "entryId": "cursor-bottom",
                                                "content": {"value": "next"},
                                            },
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(len(timeline_posts(data)), 2)
        primary = timeline_primary_posts(data)
        self.assertEqual([item.id for item in primary], ["1234567890"])
        self.assertEqual(primary[0].created_at_utc[:10], "2024-05-29")
        empty = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "entries": [
                                            {
                                                "entryId": "cursor-bottom",
                                                "content": {"value": "next"},
                                            }
                                        ]
                                    }
                                ]
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(timeline_primary_posts(empty), [])


if __name__ == "__main__":
    unittest.main()
