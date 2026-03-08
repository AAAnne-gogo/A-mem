import unittest
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import cursor_updates_watch as watch


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_only_during_nine_am_hour(self):
        now_local = datetime(2026, 3, 9, 8, 30, tzinfo=ZoneInfo(watch.TIMEZONE_NAME))
        allowed, message = watch.should_run(now_local, watch.default_state(), force=False)
        self.assertFalse(allowed)
        self.assertIn("outside the 09:00 window", message)

    def test_should_run_only_once_per_day(self):
        now_local = datetime(2026, 3, 9, 9, 5, tzinfo=ZoneInfo(watch.TIMEZONE_NAME))
        state = watch.default_state()
        state["last_run_date"] = "2026-03-09"
        allowed, message = watch.should_run(now_local, state, force=False)
        self.assertFalse(allowed)
        self.assertIn("already checked today", message)

    def test_should_run_force_bypasses_gate(self):
        now_local = datetime(2026, 3, 9, 1, 0, tzinfo=ZoneInfo(watch.TIMEZONE_NAME))
        allowed, _ = watch.should_run(now_local, watch.default_state(), force=True)
        self.assertTrue(allowed)

    def test_parse_changelog_feed(self):
        xml_text = """\
<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>Cursor now supports <b>automations</b>.</description>
    </item>
    <item>
      <title>JetBrains</title>
      <link>https://cursor.com/changelog/03-04-26</link>
      <pubDate>Wed, 04 Mar 2026 00:00:00 GMT</pubDate>
      <description>Cursor is now available in JetBrains IDEs.</description>
    </item>
  </channel>
</rss>
"""
        items = watch.parse_changelog_feed(xml_text)
        self.assertEqual([item.title for item in items], ["Automations", "JetBrains"])
        self.assertEqual(items[0].summary, "Cursor now supports automations.")
        self.assertEqual(items[0].published_at, datetime(2026, 3, 5, 0, 0, tzinfo=UTC))

    def test_parse_blog_sitemap_uses_only_blog_urls(self):
        xml_text = """\
<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/docs/cloud-agent/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        entries = watch.parse_blog_sitemap(xml_text)
        self.assertEqual([entry.url for entry in entries], [
            "https://cursor.com/blog/automations",
            "https://cursor.com/blog/jetbrains-acp",
        ])

    def test_extract_blog_summary_stops_before_heading_noise(self):
        markdown_text = """\
Title: Build agents that run automatically

Markdown Content:
We're introducing Cursor Automations to build always-on agents.

These agents run on schedules or are triggered by events like a sent Slack message.

> A quote from a user.

[](http://cursor.com/blog/automations#section)Section title
"""
        summary = watch.extract_blog_summary(markdown_text)
        self.assertEqual(
            summary,
            (
                "We're introducing Cursor Automations to build always-on agents. "
                "These agents run on schedules or are triggered by events like a sent Slack message."
            ),
        )

    def test_parse_x_mirror_deduplicates_pinned_content_and_ignores_media_noise(self):
        markdown_text = """\
Title: Cursor (@cursor_ai) / X

Cursor's posts
--------------

Pinned

[![Image 1: Square profile picture](https://example.com/a.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
1:34
![Image 5](https://example.com/thumb.jpg)

[![Image 2: Square profile picture](https://example.com/b.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.

[![Image 3: Square profile picture](https://example.com/c.jpg)](https://x.com/cursor_ai)
Cursor is now available in JetBrains IDEs through the Agent Client Protocol.
"""
        items = watch.parse_x_mirror(markdown_text.replace("Cursor's posts", "Cursor\u2019s posts"))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].summary, "We're introducing Cursor Automations to build always-on agents.")
        self.assertNotIn("1:34", items[0].summary)
        self.assertEqual(
            items[1].summary,
            "Cursor is now available in JetBrains IDEs through the Agent Client Protocol.",
        )

    def test_select_dated_updates_bootstraps_recent_items_only(self):
        now_utc = datetime(2026, 3, 9, 0, 0, tzinfo=UTC)
        items = [
            watch.UpdateItem(
                item_id="recent",
                title="Recent",
                link="https://example.com/recent",
                summary="recent",
                published_at=datetime(2026, 3, 5, 0, 0, tzinfo=UTC),
            ),
            watch.UpdateItem(
                item_id="old",
                title="Old",
                link="https://example.com/old",
                summary="old",
                published_at=datetime(2026, 2, 20, 0, 0, tzinfo=UTC),
            ),
        ]
        selected = watch.select_dated_updates(items, set(), now_utc, bootstrap_days=7)
        self.assertEqual([item.item_id for item in selected], ["recent"])

    def test_select_x_updates_bootstraps_fixed_count(self):
        items = [
            watch.UpdateItem(item_id=str(index), title=str(index), link="x", summary="x")
            for index in range(10)
        ]
        selected = watch.select_x_updates(items, set(), bootstrap_count=3)
        self.assertEqual([item.item_id for item in selected], ["0", "1", "2"])


if __name__ == "__main__":
    unittest.main()
