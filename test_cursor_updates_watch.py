from __future__ import annotations

import unittest
from datetime import datetime, timezone

import cursor_updates_watch as watcher


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_only_during_nine_am_shanghai(self) -> None:
        state = watcher.default_state()

        should_run, reason = watcher.should_run(
            datetime(2026, 3, 9, 8, 59, tzinfo=watcher.SHANGHAI_TZ),
            state,
            force=False,
        )
        self.assertFalse(should_run)
        self.assertIn("outside 09:00", reason)

        should_run, reason = watcher.should_run(
            datetime(2026, 3, 9, 9, 15, tzinfo=watcher.SHANGHAI_TZ),
            state,
            force=False,
        )
        self.assertTrue(should_run)
        self.assertIn("scheduled run", reason)

    def test_should_skip_duplicate_scheduled_run(self) -> None:
        state = watcher.default_state()
        state["last_run_date"] = "2026-03-09"

        should_run, reason = watcher.should_run(
            datetime(2026, 3, 9, 9, 1, tzinfo=watcher.SHANGHAI_TZ),
            state,
            force=False,
        )
        self.assertFalse(should_run)
        self.assertIn("duplicate run", reason)

    def test_parse_rss_items(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>&lt;p&gt;Build always-on agents.&lt;/p&gt;</description>
    </item>
  </channel>
</rss>
"""
        items = watcher.parse_rss_items(xml_text)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].summary, "Build always-on agents.")
        self.assertEqual(items[0].published_at, datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc))

    def test_parse_blog_sitemap_filters_non_posts(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com</loc>
    <lastmod>2026-03-08T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        items = watcher.parse_blog_sitemap(xml_text)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://cursor.com/blog/automations")
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].published_at, datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc))

    def test_select_dated_updates_bootstrap_and_seen_mode(self) -> None:
        now_utc = datetime(2026, 3, 8, 15, 0, tzinfo=timezone.utc)
        items = [
            watcher.UpdateItem(
                title="New",
                url="https://cursor.com/blog/new",
                published_at=datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
                summary="",
            ),
            watcher.UpdateItem(
                title="Old",
                url="https://cursor.com/blog/old",
                published_at=datetime(2026, 2, 20, 12, 0, tzinfo=timezone.utc),
                summary="",
            ),
        ]

        bootstrap = watcher.select_dated_updates(items, [], now_utc)
        self.assertEqual([item.title for item in bootstrap], ["New"])

        incremental = watcher.select_dated_updates(items, ["https://cursor.com/blog/new"], now_utc)
        self.assertEqual([item.title for item in incremental], ["Old"])

    def test_parse_x_posts_deduplicates_pinned_and_media_noise(self) -> None:
        mirrored = """Title: Cursor (@cursor_ai) / X

Markdown Content:
Cursor’s posts
--------------
Pinned

[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
1:34

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)
GPT 5.4 is now available in Cursor!

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
![Image 99](https://pbs.twimg.com/media/example.jpg)
"""
        posts = watcher.parse_x_posts(mirrored)

        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor!",
            ],
        )

    def test_select_x_updates_bootstrap_and_seen_mode(self) -> None:
        posts = [f"post {index}" for index in range(10)]

        bootstrap = watcher.select_x_updates(posts, [])
        self.assertEqual(len(bootstrap), 8)
        self.assertEqual(bootstrap[0], "post 0")

        incremental = watcher.select_x_updates(posts, ["post 0", "post 1"])
        self.assertEqual(incremental[0], "post 2")


if __name__ == "__main__":
    unittest.main()
