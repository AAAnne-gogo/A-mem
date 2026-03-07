import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cursor_updates_watch import (
    UpdateItem,
    extract_bearer_token,
    extract_guest_token,
    extract_main_bundle_url,
    extract_query_id,
    parse_blog_item,
    parse_changelog_item,
    parse_marketing_sitemap,
    parse_x_timeline,
    should_run_now,
    trim_seen_ids,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_marketing_sitemap_filters_article_urls(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog</loc>
    <lastmod>2026-03-05T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        grouped = parse_marketing_sitemap(xml_text)

        self.assertEqual(
            [item.url for item in grouped["blog"]],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )
        self.assertEqual(
            [item.url for item in grouped["changelog"]],
            ["https://cursor.com/changelog/03-05-26"],
        )

    def test_parse_blog_item_prefers_json_ld(self) -> None:
        html_text = """
<html>
  <head>
    <title>Ignored title</title>
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": "Build agents that run automatically",
        "description": "Cursor now supports automations.",
        "datePublished": "2026-03-05T12:00:00.000Z"
      }
    </script>
  </head>
</html>
"""
        item = parse_blog_item("https://cursor.com/blog/automations", html_text)

        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.summary, "Cursor now supports automations.")
        self.assertEqual(item.published_at, "2026-03-05T12:00:00.000Z")

    def test_parse_changelog_item_extracts_title_time_and_summary(self) -> None:
        html_text = """
<html>
  <head><title>Automations · Cursor</title></head>
  <body>
    <header><h1 id="automations">Automations</h1></header>
    <time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time>
    <div class="prose prose--block">
      <p>Cursor now supports <a href="/docs">automations</a> for always-on agents.</p>
    </div>
  </body>
</html>
"""
        item = parse_changelog_item("https://cursor.com/changelog/03-05-26", html_text)

        self.assertEqual(item.title, "Automations")
        self.assertEqual(item.published_at, "2026-03-05T00:00:00.000Z")
        self.assertEqual(item.summary, "Cursor now supports automations for always-on agents.")

    def test_extract_x_bootstrap_values(self) -> None:
        profile_html = """
<html>
  <script>document.cookie="gt=2030284569857917414; Max-Age=9000; Domain=.x.com; Path=/; Secure";</script>
  <script src="https://abs.twimg.com/responsive-web/client-web/main.4438e12a.js"></script>
</html>
"""
        bundle_text = """
something Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAATEST%3Dabc more
queryId:"userQueryId",operationName:"UserByScreenName"
queryId:"tweetQueryId",operationName:"UserTweets"
"""
        self.assertEqual(extract_guest_token(profile_html), "2030284569857917414")
        self.assertEqual(
            extract_main_bundle_url(profile_html),
            "https://abs.twimg.com/responsive-web/client-web/main.4438e12a.js",
        )
        self.assertEqual(extract_bearer_token(bundle_text), "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAATEST=abc")
        self.assertEqual(extract_query_id(bundle_text, "UserByScreenName"), "userQueryId")
        self.assertEqual(extract_query_id(bundle_text, "UserTweets"), "tweetQueryId")

    def test_parse_x_timeline_keeps_visible_cursor_posts(self) -> None:
        payload = {
            "data": {
                "user": {
                    "result": {
                        "timeline": {
                            "timeline": {
                                "instructions": [
                                    {
                                        "entry": {
                                            "content": {
                                                "itemContent": {
                                                    "tweet_results": {
                                                        "result": {
                                                            "__typename": "Tweet",
                                                            "rest_id": "111",
                                                            "core": {
                                                                "user_results": {
                                                                    "result": {
                                                                        "core": {
                                                                            "screen_name": "cursor_ai"
                                                                        }
                                                                    }
                                                                }
                                                            },
                                                            "legacy": {
                                                                "created_at": "Thu Mar 05 12:00:00 +0000 2026",
                                                                "full_text": "Build agents that run automatically",
                                                            },
                                                        }
                                                    }
                                                }
                                            }
                                        }
                                    },
                                    {
                                        "entries": [
                                            {
                                                "content": {
                                                    "items": [
                                                        {
                                                            "item": {
                                                                "itemContent": {
                                                                    "tweet_results": {
                                                                        "result": {
                                                                            "__typename": "TweetWithVisibilityResults",
                                                                            "tweet": {
                                                                                "__typename": "Tweet",
                                                                                "rest_id": "222",
                                                                                "core": {
                                                                                    "user_results": {
                                                                                        "result": {
                                                                                            "core": {
                                                                                                "screen_name": "cursor_ai"
                                                                                            }
                                                                                        }
                                                                                    }
                                                                                },
                                                                                "legacy": {
                                                                                    "created_at": "Fri Mar 06 12:00:00 +0000 2026",
                                                                                    "full_text": "Pinned tweet copy",
                                                                                },
                                                                                "note_tweet": {
                                                                                    "note_tweet_results": {
                                                                                        "result": {
                                                                                            "text": "Pinned tweet full note"
                                                                                        }
                                                                                    }
                                                                                },
                                                                            },
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        },
                                                        {
                                                            "item": {
                                                                "itemContent": {
                                                                    "tweet_results": {
                                                                        "result": {
                                                                            "__typename": "Tweet",
                                                                            "rest_id": "333",
                                                                            "core": {
                                                                                "user_results": {
                                                                                    "result": {
                                                                                        "core": {
                                                                                            "screen_name": "someone_else"
                                                                                        }
                                                                                    }
                                                                                }
                                                                            },
                                                                            "legacy": {
                                                                                "created_at": "Fri Mar 06 13:00:00 +0000 2026",
                                                                                "full_text": "Ignore me",
                                                                            },
                                                                        }
                                                                    }
                                                                }
                                                            }
                                                        },
                                                    ]
                                                }
                                            }
                                        ]
                                    },
                                ]
                            }
                        }
                    }
                }
            }
        }

        items = parse_x_timeline(payload, "cursor_ai")

        self.assertEqual([item.item_id for item in items], ["111", "222"])
        self.assertEqual(items[1].summary, "Pinned tweet full note")
        self.assertEqual(items[0].url, "https://x.com/cursor_ai/status/111")

    def test_should_run_now_allows_first_run_after_scheduled_hour(self) -> None:
        now = datetime(2026, 3, 7, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        should_run, reason = should_run_now(now, None, 9)

        self.assertTrue(should_run)
        self.assertIn("09:00", reason)

    def test_should_run_now_skips_repeat_run_same_day(self) -> None:
        now = datetime(2026, 3, 7, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        should_run, reason = should_run_now(now, "2026-03-07", 9)

        self.assertFalse(should_run)
        self.assertIn("already completed", reason)

    def test_trim_seen_ids_deduplicates_and_limits(self) -> None:
        trimmed = trim_seen_ids(["a", "b", "a", "", "c"])
        self.assertEqual(trimmed, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
