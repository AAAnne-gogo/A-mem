import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

import cursor_updates_watch as cuw


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_now_respects_schedule_and_duplicate_guard(self) -> None:
        tz = ZoneInfo("Asia/Shanghai")

        should_run, reason = cuw.should_run_now(
            datetime(2026, 3, 7, 8, 59, tzinfo=tz),
            {},
            force=False,
        )
        self.assertFalse(should_run)
        self.assertIn("outside the 09:00 gate", reason)

        should_run, reason = cuw.should_run_now(
            datetime(2026, 3, 7, 9, 15, tzinfo=tz),
            {},
            force=False,
        )
        self.assertTrue(should_run)
        self.assertIn("scheduled 09:00 gate", reason)

        should_run, reason = cuw.should_run_now(
            datetime(2026, 3, 7, 9, 30, tzinfo=tz),
            {"last_scheduled_date_local": "2026-03-07"},
            force=False,
        )
        self.assertFalse(should_run)
        self.assertIn("already completed", reason)

        should_run, reason = cuw.should_run_now(
            datetime(2026, 3, 7, 14, 0, tzinfo=tz),
            {"last_scheduled_date_local": "2026-03-07"},
            force=True,
        )
        self.assertTrue(should_run)
        self.assertEqual(reason, "forced run")

    def test_parse_sitemap_entries_filters_localized_paths(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/cn/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T00:00:00.000Z</lastmod>
  </url>
</urlset>
"""

        blog_entries = cuw.parse_sitemap_entries(xml_text, "blog")
        self.assertEqual(
            [entry["url"] for entry in blog_entries],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )

        changelog_entries = cuw.parse_sitemap_entries(xml_text, "changelog")
        self.assertEqual(
            [entry["url"] for entry in changelog_entries],
            ["https://cursor.com/changelog/03-05-26"],
        )

    def test_extract_summary_handles_blog_and_changelog_markdown(self) -> None:
        blog_body = """We're introducing Cursor Automations to build always-on agents.

These agents run on schedules or are triggered by events like a sent Slack message.

> A customer quote that still belongs in the summary.
"""
        self.assertEqual(
            cuw.extract_summary("blog", "Build agents that run automatically", blog_body),
            "We're introducing Cursor Automations to build always-on agents. These agents run on schedules or are triggered by events like a sent Slack message. A customer quote that still belongs in the summary.",
        )

        changelog_body = """Automations · Cursor
===============

[Skip to content](http://cursor.com/changelog/03-05-26#main)

Mar 5, 2026 · [Changelog](http://cursor.com/changelog)

Automations
===========

Cursor now supports [automations](https://cursor.com/docs/cloud-agent/automations) for building always-on agents that run based on triggers and instructions you define.

Automations run on schedules or are triggered by events from Slack, Linear, GitHub, PagerDuty, and webhooks.

Create automations at [cursor.com/automations](https://cursor.com/automations).

[Next post →Cursor in JetBrains IDEs](http://cursor.com/changelog/03-04-26)
"""
        self.assertEqual(
            cuw.extract_summary("changelog", "Automations", changelog_body),
            "Cursor now supports automations for building always-on agents that run based on triggers and instructions you define. Automations run on schedules or are triggered by events from Slack, Linear, GitHub, PagerDuty, and webhooks. Create automations at cursor.com/automations.",
        )

    def test_extract_x_posts_filters_images_duplicates_and_profile_noise(self) -> None:
        x_text = """Title: Cursor (@cursor_ai) / X

Markdown Content:
[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor's posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example2.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

Cursor can now continuously monitor and improve your codebase. Automations run based on triggers and instructions you define.

We're introducing Cursor Automations to build always-on agents.

![Image 3](https://pbs.twimg.com/media/example.jpg)
"""
        self.assertEqual(
            cuw.extract_x_posts(x_text, limit=5),
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "Cursor can now continuously monitor and improve your codebase. Automations run based on triggers and instructions you define.",
            ],
        )


if __name__ == "__main__":
    unittest.main()
