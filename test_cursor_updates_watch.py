import unittest
from datetime import datetime, timezone

from cursor_updates_watch import (
    UpdateItem,
    parse_blog_index,
    parse_changelog_rss,
    parse_x_timeline,
    select_new_dated_items,
    should_run,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_only_during_scheduled_hour_once_per_day(self) -> None:
        now = datetime.fromisoformat("2026-03-08T09:00:00+08:00")
        allowed, reason = should_run(now, 9, None)
        self.assertTrue(allowed)
        self.assertIn("scheduled window open", reason)

        allowed, reason = should_run(now, 9, "2026-03-08")
        self.assertFalse(allowed)
        self.assertIn("already ran", reason)

        late = datetime.fromisoformat("2026-03-08T10:00:00+08:00")
        allowed, reason = should_run(late, 9, None)
        self.assertFalse(allowed)
        self.assertIn("waiting for 09:00", reason)

    def test_parse_changelog_rss(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description><![CDATA[<p>Build always-on agents.</p>]]></description>
    </item>
  </channel>
</rss>
"""
        items = parse_changelog_rss(xml_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].summary, "Build always-on agents.")
        self.assertEqual(items[0].published_at, "2026-03-05T00:00:00+00:00")

    def test_parse_blog_index(self) -> None:
        html_text = """
<article>
  <a href="/blog/automations">
    <div>
      <p>Build agents that run automatically</p>
      <p>Cursor now supports automations that run based on triggers.</p>
      <div><time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time></div>
    </div>
  </a>
</article>
<article>
  <a href="/blog/topic/company">
    <div>
      <p>Company</p>
      <div><time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time></div>
    </div>
  </a>
</article>
"""
        items = parse_blog_index(html_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Build agents that run automatically")
        self.assertEqual(items[0].link, "https://cursor.com/blog/automations")
        self.assertEqual(
            items[0].summary,
            "Cursor now supports automations that run based on triggers.",
        )

    def test_parse_x_timeline_dedupes_pinned_and_media_rows(self) -> None:
        markdown_text = """Title: Cursor (@cursor_ai) / X

Markdown Content:
Cursor’s posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:33

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 4](https://pbs.twimg.com/media/example.jpg)

[![Image 5: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor!
"""
        posts = parse_x_timeline(markdown_text)
        self.assertEqual(len(posts), 2)
        self.assertTrue(posts[0].pinned)
        self.assertEqual(
            posts[0].text,
            "We're introducing Cursor Automations to build always-on agents.",
        )
        self.assertEqual(posts[1].text, "GPT 5.4 is now available in Cursor!")

    def test_bootstrap_selection_uses_recent_window(self) -> None:
        now_utc = datetime(2026, 3, 8, 4, 0, 0, tzinfo=timezone.utc)
        items = [
            UpdateItem(
                source="blog",
                title="Recent",
                link="https://cursor.com/blog/recent",
                published_at="2026-03-05T00:00:00+00:00",
                summary="",
            ),
            UpdateItem(
                source="blog",
                title="Old",
                link="https://cursor.com/blog/old",
                published_at="2026-02-20T00:00:00+00:00",
                summary="",
            ),
        ]
        selected = select_new_dated_items(items, set(), now_utc, bootstrap_lookback_days=7)
        self.assertEqual([item.title for item in selected], ["Recent"])


if __name__ == "__main__":
    unittest.main()
