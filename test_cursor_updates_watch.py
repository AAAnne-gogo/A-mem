from __future__ import annotations

import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import cursor_updates_watch as watcher


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cursor.com/blog/automations</loc><lastmod>2026-03-05T12:00:00.000Z</lastmod></url>
  <url><loc>https://cursor.com/changelog/03-05-26</loc><lastmod>2026-03-05T00:00:00.000Z</lastmod></url>
  <url><loc>https://cursor.com/blog/jetbrains-acp</loc><lastmod>2026-03-04T12:00:00.000Z</lastmod></url>
  <url><loc>https://cursor.com/changelog/03-04-26</loc><lastmod>2026-03-04T00:00:00.000Z</lastmod></url>
  <url><loc>https://cursor.com/blog/automations</loc><lastmod>2026-03-05T12:00:00.000Z</lastmod></url>
</urlset>
"""

PAGE_HTML = """
<html>
  <head>
    <meta property="og:title" content="Build agents that run automatically · Cursor" />
    <meta property="og:description" content="Automations let agents run on schedules and triggers." />
    <script type="application/ld+json">
      {"datePublished":"2026-03-05T12:00:00.000Z"}
    </script>
  </head>
</html>
"""

X_MIRROR = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 14:22:05 GMT

Markdown Content:
Cursor's posts
--------------

Pinned

[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example_normal.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example_normal.jpg)](https://x.com/cursor_ai)

Cursor can now continuously monitor and improve your codebase.

![Image 3](https://pbs.twimg.com/media/example.jpg)

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example_normal.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_sitemap_entries_include_lastmod(self) -> None:
        self.assertEqual(
            watcher.parse_sitemap_entries(SITEMAP_XML, "changelog"),
            [
                ("https://cursor.com/changelog/03-05-26", "2026-03-05T00:00:00.000Z"),
                ("https://cursor.com/changelog/03-04-26", "2026-03-04T00:00:00.000Z"),
            ],
        )

    def test_parse_sitemap_urls_filters_and_deduplicates(self) -> None:
        self.assertEqual(
            watcher.parse_sitemap_urls(SITEMAP_XML, "blog"),
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )
        self.assertEqual(
            watcher.parse_sitemap_urls(SITEMAP_XML, "changelog"),
            [
                "https://cursor.com/changelog/03-05-26",
                "https://cursor.com/changelog/03-04-26",
            ],
        )

    def test_parse_page_metadata_extracts_title_date_and_summary(self) -> None:
        title, published_at, summary = watcher.parse_page_metadata(PAGE_HTML)
        self.assertEqual(title, "Build agents that run automatically")
        self.assertEqual(published_at, "2026-03-05T12:00:00.000Z")
        self.assertEqual(summary, "Automations let agents run on schedules and triggers.")

    def test_parse_x_mirror_strips_images_and_deduplicates_posts(self) -> None:
        posts, published_time = watcher.parse_x_mirror(X_MIRROR)
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "Cursor can now continuously monitor and improve your codebase.",
            ],
        )
        self.assertEqual(published_time, "Sat, 07 Mar 2026 14:22:05 GMT")

    def test_should_run_only_inside_nine_oclock_window_once_per_day(self) -> None:
        tz = ZoneInfo(watcher.TIMEZONE_NAME)
        at_nine = datetime(2026, 3, 7, 9, 0, tzinfo=tz)
        at_ten = datetime(2026, 3, 7, 10, 0, tzinfo=tz)

        self.assertEqual(
            watcher.should_run(at_nine, {}, force=False),
            (True, "scheduled window is open"),
        )
        allowed, reason = watcher.should_run(at_ten, {}, force=False)
        self.assertFalse(allowed)
        self.assertIn("outside the 09:00 window", reason)
        self.assertEqual(
            watcher.should_run(at_ten, {"last_success_date": "2026-03-07"}, force=True),
            (True, "forced run"),
        )

    def test_mark_new_entries_marks_only_unseen_values(self) -> None:
        items = [
            watcher.UpdateItem(title="Automations", url="https://cursor.com/changelog/03-05-26"),
            watcher.UpdateItem(title="JetBrains", url="https://cursor.com/changelog/03-04-26"),
        ]
        marked = watcher.mark_new_entries(items, {"https://cursor.com/changelog/03-04-26"}, "url")
        self.assertEqual([item.is_new for item in marked], [True, False])


if __name__ == "__main__":
    unittest.main()
