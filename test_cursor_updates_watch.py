import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from cursor_updates_watch import (
    parse_blog_update,
    parse_changelog_update,
    parse_sitemap,
    parse_x_posts,
    should_run_scheduled,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_sitemap_splits_blog_and_changelog(self):
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com</loc>
    <lastmod>2026-03-07T10:03:48.669Z</lastmod>
  </url>
</urlset>
"""
        result = parse_sitemap(xml_text)

        self.assertEqual(
            [entry.url for entry in result["blog"]],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )
        self.assertEqual(
            [entry.url for entry in result["changelog"]],
            ["https://cursor.com/changelog/03-05-26"],
        )

    def test_parse_blog_update_extracts_title_published_and_summary(self):
        page_text = """Title: Build agents that run automatically

URL Source: http://cursor.com/blog/automations

Published Time: 2026-03-05T12:00:00.000Z

Markdown Content:
We're introducing Cursor Automations to build always-on agents.

These agents run on schedules or are triggered by events like a sent Slack message, a newly created Linear issue, a merged GitHub PR, or a PagerDuty incident.

![Image 1](https://example.com/image.png)

[](http://cursor.com/blog/automations#chores)Chores
---------------------------------------------------
"""
        result = parse_blog_update(
            page_text,
            "https://cursor.com/blog/automations",
            "2026-03-05T12:00:00.000Z",
        )

        self.assertEqual(result.title, "Build agents that run automatically")
        self.assertEqual(result.published, "2026-03-05T12:00:00.000Z")
        self.assertIn("always-on agents", result.summary)
        self.assertIn("merged GitHub PR", result.summary)
        self.assertNotIn("Image 1", result.summary)

    def test_parse_changelog_update_skips_navigation(self):
        page_text = """Title: Automations · Cursor

URL Source: http://cursor.com/changelog/03-05-26

Markdown Content:
Automations · Cursor
===============

[Skip to content](http://cursor.com/changelog/03-05-26#main)

[Cursor](http://cursor.com/home)

[Sign in](https://cursor.com/dashboard)[Download](http://cursor.com/download)

Mar 5, 2026 · [Changelog](http://cursor.com/changelog)

[Changelog](http://cursor.com/changelog)

Automations
===========

Cursor now supports automations for building always-on agents that run based on triggers and instructions you define.

Automations run on schedules or are triggered by events from Slack, Linear, GitHub, PagerDuty, and webhooks.

[Next post →Cursor in JetBrains IDEs](http://cursor.com/changelog/03-04-26)

### Product
"""
        result = parse_changelog_update(
            page_text,
            "https://cursor.com/changelog/03-05-26",
            "2026-03-05T12:00:00.000Z",
        )

        self.assertEqual(result.title, "Automations")
        self.assertEqual(result.published, "Mar 5, 2026")
        self.assertIn("always-on agents", result.summary)
        self.assertIn("PagerDuty", result.summary)
        self.assertNotIn("Sign in", result.summary)

    def test_parse_changelog_update_handles_version_prefixed_date(self):
        page_text = """Title: MCP Apps and Team Marketplaces for Plugins · Cursor

URL Source: http://cursor.com/changelog/2-6

Markdown Content:
MCP Apps and Team Marketplaces for Plugins · Cursor
===============

2.6 Mar 3, 2026 · [Changelog](http://cursor.com/changelog)

[Changelog](http://cursor.com/changelog)

MCP Apps and Team Marketplaces for Plugins
==========================================

This release introduces interactive UIs in agent chats, a way for teams to share private plugins, and improvements to core capabilities like Debug mode.

### [#](http://cursor.com/changelog/2-6#mcp-apps)MCP Apps

[MCP Apps](https://cursor.com/docs/context/mcp#mcp-apps) support interactive user interfaces like charts from Amplitude directly inside Cursor.
"""
        result = parse_changelog_update(
            page_text,
            "https://cursor.com/changelog/2-6",
            "2026-03-03T00:00:00.000Z",
        )

        self.assertEqual(result.published, "Mar 3, 2026")
        self.assertIn("interactive UIs", result.summary)
        self.assertNotIn("###", result.summary)

    def test_parse_x_posts_filters_images_duration_and_duplicates(self):
        page_text = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 09:22:52 GMT

Markdown Content:
[![Image 1: Square profile picture and Opens profile photo](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 4](https://pbs.twimg.com/amplify_video_thumb/example.jpg)

[![Image 5: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor is now available in JetBrains IDEs through the Agent Client Protocol.

We believe Cursor discovered a novel solution to Problem Six.
"""
        result = parse_x_posts(page_text, limit=10)

        self.assertEqual(len(result), 2)
        self.assertEqual(
            result[0], "We're introducing Cursor Automations to build always-on agents."
        )
        self.assertIn("JetBrains IDEs", result[1])
        self.assertNotIn("1:34", result[0])
        self.assertNotIn("Image 4", result[0])

    def test_should_run_scheduled_respects_hour_duplicate_and_force(self):
        tz = ZoneInfo("Asia/Shanghai")
        scheduled_now = datetime(2026, 3, 7, 9, 5, tzinfo=tz)
        wrong_hour_now = datetime(2026, 3, 7, 8, 55, tzinfo=tz)

        self.assertEqual(
            should_run_scheduled(scheduled_now, 9, None, False),
            (True, "scheduled"),
        )
        self.assertEqual(
            should_run_scheduled(scheduled_now, 9, "2026-03-07", False),
            (False, "scheduled digest for 2026-03-07 already ran"),
        )
        self.assertEqual(
            should_run_scheduled(wrong_hour_now, 9, None, False),
            (False, "current local time 2026-03-07 08:55 is outside 09:00"),
        )
        self.assertEqual(
            should_run_scheduled(wrong_hour_now, 9, "2026-03-07", True),
            (True, "force"),
        )


if __name__ == "__main__":
    unittest.main()
