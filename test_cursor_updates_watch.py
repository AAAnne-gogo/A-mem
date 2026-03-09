import unittest
from datetime import datetime, timezone

import cursor_updates_watch as watcher


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_respects_schedule_and_dedup(self) -> None:
        now = datetime(2026, 3, 9, 9, 5, tzinfo=watcher.SCHEDULE_TZ)
        state = {"last_run_date": None, "seen": {"changelog": [], "blog": [], "x": []}}
        self.assertEqual(watcher.should_run(now, state, force=False), (True, "scheduled run"))

        state["last_run_date"] = "2026-03-09"
        allowed, reason = watcher.should_run(now, state, force=False)
        self.assertFalse(allowed)
        self.assertIn("already ran", reason)

        not_nine = datetime(2026, 3, 9, 8, 5, tzinfo=watcher.SCHEDULE_TZ)
        allowed, reason = watcher.should_run(not_nine, state, force=True)
        self.assertTrue(allowed)
        self.assertEqual(reason, "forced run")

    def test_parse_changelog_feed(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Automations</title>
              <link>https://cursor.com/changelog/automations</link>
              <guid>auto-1</guid>
              <pubDate>Thu, 05 Mar 2026 12:00:00 GMT</pubDate>
              <description><![CDATA[Build always-on agents.]]></description>
            </item>
          </channel>
        </rss>
        """
        items = watcher.parse_changelog_feed(xml_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["id"], "auto-1")
        self.assertEqual(items[0]["title"], "Automations")
        self.assertEqual(items[0]["summary"], "Build always-on agents.")
        self.assertEqual(items[0]["published_at"], "2026-03-05T12:00:00Z")

    def test_parse_blog_sitemap_and_article(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://cursor.com/blog/automations</loc>
            <lastmod>2026-03-05T12:00:00.000Z</lastmod>
          </url>
          <url>
            <loc>https://cursor.com/blog/topic/product</loc>
            <lastmod>2026-03-05T12:00:00.000Z</lastmod>
          </url>
        </urlset>
        """
        sitemap_items = watcher.parse_blog_sitemap(xml_text)
        self.assertEqual(sitemap_items, [{"url": "https://cursor.com/blog/automations", "lastmod": "2026-03-05T12:00:00.000Z"}])

        html_text = """
        <html>
          <head>
            <meta property="og:title" content="Build agents that run automatically · Cursor" />
            <meta property="og:description" content="Cursor now supports automations that run based on triggers and instructions you define." />
          </head>
          <body>
            <time dateTime="2026-03-05T12:00:00.000Z"></time>
          </body>
        </html>
        """
        article = watcher.parse_blog_article("https://cursor.com/blog/automations", html_text)
        self.assertEqual(article["title"], "Build agents that run automatically")
        self.assertEqual(
            article["summary"],
            "Cursor now supports automations that run based on triggers and instructions you define.",
        )
        self.assertEqual(article["published_at"], "2026-03-05T12:00:00Z")

    def test_parse_x_timeline_page_expands_links_and_drops_media_tco(self) -> None:
        html_text = """
        <html><body>
        <script id="__NEXT_DATA__" type="application/json">
        {
          "props": {
            "pageProps": {
              "timeline": {
                "entries": [
                  {
                    "type": "tweet",
                    "content": {
                      "tweet": {
                        "id_str": "123",
                        "created_at": "Thu Mar 05 17:05:19 +0000 2026",
                        "full_text": "Hello https://t.co/docs https://t.co/media",
                        "permalink": "/cursor_ai/status/123",
                        "entities": {
                          "urls": [
                            {
                              "indices": [6, 23],
                              "expanded_url": "https://cursor.com/docs"
                            }
                          ],
                          "media": [
                            {
                              "indices": [24, 42]
                            }
                          ]
                        },
                        "extended_entities": {
                          "media": [
                            {
                              "indices": [24, 42]
                            }
                          ]
                        },
                        "user": {
                          "screen_name": "cursor_ai"
                        }
                      }
                    }
                  },
                  {
                    "type": "tweet",
                    "content": {
                      "tweet": {
                        "id_str": "124",
                        "created_at": "Thu Mar 05 18:05:19 +0000 2026",
                        "full_text": "RT @someone: shared post",
                        "permalink": "/cursor_ai/status/124",
                        "entities": {},
                        "user": {
                          "screen_name": "cursor_ai"
                        }
                      }
                    }
                  }
                ]
              }
            }
          }
        }
        </script>
        </body></html>
        """
        items = watcher.parse_x_timeline_page(html_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "Hello https://cursor.com/docs")
        self.assertEqual(items[0]["summary"], "Hello https://cursor.com/docs")
        self.assertEqual(items[0]["url"], "https://x.com/cursor_ai/status/123")
        self.assertEqual(items[0]["published_at"], "2026-03-05T17:05:19Z")

    def test_select_items_uses_bootstrap_window_without_seen_state(self) -> None:
        now_utc = datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc)
        items = [
            {"id": "new", "published_at": "2026-03-05T12:00:00Z"},
            {"id": "old", "published_at": "2026-02-20T12:00:00Z"},
        ]
        selected = watcher.select_items(items, [], "id", now_utc)
        self.assertEqual([item["id"] for item in selected], ["new"])

    def test_persist_run_state_keeps_last_run_date_for_forced_updates(self) -> None:
        state = {
            "last_run_date": None,
            "last_checked_at": None,
            "seen": {"changelog": ["old"], "blog": [], "x": []},
        }
        now_utc = datetime(2026, 3, 8, 23, 0, tzinfo=timezone.utc)
        next_state = watcher.persist_run_state(
            state=state,
            changelog_items=[{"id": "new"}],
            blog_items=[],
            x_items=[{"id": "tweet-1"}],
            now_utc=now_utc,
            scheduled_run=False,
        )
        self.assertIsNone(next_state["last_run_date"])
        self.assertEqual(next_state["seen"]["changelog"], ["new", "old"])
        self.assertEqual(next_state["seen"]["x"], ["tweet-1"])


if __name__ == "__main__":
    unittest.main()
