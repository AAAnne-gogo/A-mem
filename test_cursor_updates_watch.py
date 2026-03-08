import unittest
from datetime import datetime, timezone

import cursor_updates_watch as cuw


SAMPLE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>Cursor now supports automations.</description>
    </item>
  </channel>
</rss>
"""


SAMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/en/blog</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/scaling-agents</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""


SAMPLE_BLOG_HTML = """
<html>
  <head>
    <meta property="og:title" content="Build agents that run automatically · Cursor" />
    <meta property="og:description" content="Cursor now supports automations that run based on triggers and instructions you define." />
  </head>
  <body>
    <time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time>
  </body>
</html>
"""


SAMPLE_PROFILE_HTML = """
<html>
  <head>
    <link rel="preload" as="script" href="https://abs.twimg.com/responsive-web/client-web/main.a907f5ba.js" />
  </head>
  <body>
    <script>document.cookie="gt=2030766735478124787; Max-Age=9000; Domain=.x.com; Path=/; Secure";</script>
  </body>
</html>
"""


SAMPLE_MAIN_JS = """
e.exports={queryId:"pLsOiyHJ1eFwPJlNmLp4Bg",operationName:"UserByScreenName",operationType:"query"};
e.exports={queryId:"tBNuKtAJqe33sRX5V6Vlbg",operationName:"UserTweets",operationType:"query"};
headers.Authorization="Bearer AAAAAAAAAAAAAAAAAAAAANRILgAAAAAATESTTOKEN";
"""


SAMPLE_X_PAYLOAD = {
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
                                                    "rest_id": "2029604182286856663",
                                                    "legacy": {
                                                        "created_at": "Thu Mar 05 17:05:19 +0000 2026",
                                                        "full_text": "Build agents that run automatically https://t.co/abc https://t.co/media",
                                                        "entities": {
                                                            "urls": [
                                                                {
                                                                    "url": "https://t.co/abc",
                                                                    "expanded_url": "https://cursor.com/blog/automations",
                                                                }
                                                            ],
                                                            "media": [
                                                                {"url": "https://t.co/media"},
                                                            ],
                                                        },
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
                                            "itemContent": {
                                                "tweet_results": {
                                                    "result": {
                                                        "__typename": "Tweet",
                                                        "rest_id": "2029604182286856663",
                                                        "legacy": {
                                                            "created_at": "Thu Mar 05 17:05:19 +0000 2026",
                                                            "full_text": "Duplicate",
                                                            "entities": {},
                                                        },
                                                    }
                                                }
                                            }
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


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_items(self) -> None:
        items = cuw.parse_changelog_items(SAMPLE_RSS)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].item_id, "https://cursor.com/changelog/03-05-26")

    def test_parse_blog_sitemap_filters_index_page(self) -> None:
        items = cuw.parse_blog_sitemap(SAMPLE_SITEMAP)
        self.assertEqual(
            items,
            [
                ("https://cursor.com/blog/automations", "2026-03-05T12:00:00.000Z"),
                ("https://cursor.com/blog/scaling-agents", "2026-03-04T12:00:00.000Z"),
            ],
        )

    def test_parse_blog_article(self) -> None:
        article = cuw.parse_blog_article("https://cursor.com/blog/automations", SAMPLE_BLOG_HTML, None)
        self.assertIsNotNone(article)
        assert article is not None
        self.assertEqual(article.title, "Build agents that run automatically")
        self.assertIn("automations", article.summary)
        self.assertEqual(article.published_at, "2026-03-05T12:00:00Z")

    def test_extract_x_tokens_and_query_ids(self) -> None:
        self.assertEqual(cuw.extract_x_guest_token(SAMPLE_PROFILE_HTML), "2030766735478124787")
        self.assertEqual(
            cuw.extract_x_main_js_url(SAMPLE_PROFILE_HTML),
            "https://abs.twimg.com/responsive-web/client-web/main.a907f5ba.js",
        )
        self.assertEqual(cuw.extract_x_bearer_token(SAMPLE_MAIN_JS), "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAATESTTOKEN")
        self.assertEqual(cuw.extract_x_query_id(SAMPLE_MAIN_JS, "UserByScreenName"), "pLsOiyHJ1eFwPJlNmLp4Bg")
        self.assertEqual(cuw.extract_x_query_id(SAMPLE_MAIN_JS, "UserTweets"), "tBNuKtAJqe33sRX5V6Vlbg")

    def test_parse_x_tweets_expands_urls_and_deduplicates(self) -> None:
        items = cuw.parse_x_tweets(SAMPLE_X_PAYLOAD, "cursor_ai")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "2029604182286856663")
        self.assertNotIn("https://t.co/abc", items[0].summary)
        self.assertNotIn("https://t.co/media", items[0].summary)
        self.assertIn("https://cursor.com/blog/automations", items[0].summary)
        self.assertEqual(items[0].url, "https://x.com/cursor_ai/status/2029604182286856663")

    def test_select_items_bootstrap_modes(self) -> None:
        now_utc = datetime(2026, 3, 8, 12, 0, tzinfo=timezone.utc)
        recent_item = cuw.UpdateItem("blog", "a", "A", "https://cursor.com/blog/a", "2026-03-07T12:00:00Z", "A")
        old_item = cuw.UpdateItem("blog", "b", "B", "https://cursor.com/blog/b", "2026-02-01T12:00:00Z", "B")
        x_items = [cuw.UpdateItem("x", str(i), f"title {i}", f"https://x.com/{i}", None, f"summary {i}") for i in range(10)]

        self.assertEqual(cuw.select_items([recent_item, old_item], [], "recent_days", now_utc), [recent_item])
        self.assertEqual(cuw.select_items([recent_item, old_item], ["a"], "recent_days", now_utc), [old_item])
        self.assertEqual(len(cuw.select_items(x_items, [], "latest_x", now_utc)), cuw.BOOTSTRAP_X_COUNT)

    def test_should_run_now(self) -> None:
        state = {"last_run_date": ""}
        allowed, reason = cuw.should_run_now(
            False,
            "Asia/Shanghai",
            state,
            current_time=datetime(2026, 3, 9, 9, 1, tzinfo=cuw.ZoneInfo("Asia/Shanghai")),
        )
        self.assertTrue(allowed)
        self.assertIn("scheduled", reason)

        blocked, reason = cuw.should_run_now(
            False,
            "Asia/Shanghai",
            {"last_run_date": "2026-03-09"},
            current_time=datetime(2026, 3, 9, 9, 5, tzinfo=cuw.ZoneInfo("Asia/Shanghai")),
        )
        self.assertFalse(blocked)
        self.assertIn("already ran", reason)

    def test_build_report_includes_source_errors(self) -> None:
        generated_at = datetime(2026, 3, 9, 9, 0, tzinfo=cuw.ZoneInfo("Asia/Shanghai"))
        changelog = cuw.SourceResult("changelog", [], [])
        blog = cuw.SourceResult("blog", [], [], error="HTTP Error 500")
        x_posts = cuw.SourceResult("x", [], [])
        report = cuw.build_report(changelog, blog, x_posts, "Asia/Shanghai", generated_at)
        self.assertIn("# Cursor 每日更新", report)
        self.assertIn("## 抓取异常", report)
        self.assertIn("HTTP Error 500", report)


if __name__ == "__main__":
    unittest.main()
