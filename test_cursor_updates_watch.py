from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cursor_updates_watch import (
    SourceSnapshot,
    UpdateItem,
    load_state,
    parse_blog_article,
    parse_blog_sitemap,
    parse_changelog_rss,
    parse_x_markdown,
    run_watch,
    should_run,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_rss(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>Always-on agents.</description>
    </item>
    <item>
      <title>JetBrains</title>
      <link>https://cursor.com/changelog/03-04-26</link>
      <pubDate>Wed, 04 Mar 2026 00:00:00 GMT</pubDate>
      <description>ACP support.</description>
    </item>
  </channel>
</rss>
"""
        items = parse_changelog_rss(xml_text)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].item_id, "https://cursor.com/changelog/03-05-26")
        self.assertEqual(items[0].summary, "Always-on agents.")

    def test_parse_blog_sitemap_filters_canonical_blog_urls(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
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
    <loc>https://www.cursor.com/blog/codex-model-harness</loc>
    <lastmod>2026-03-06T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        entries = parse_blog_sitemap(xml_text)

        self.assertEqual(
            entries,
            [
                ("https://cursor.com/blog/codex-model-harness", datetime(2026, 3, 6, 12, 0, tzinfo=timezone.utc)),
                ("https://cursor.com/blog/automations", datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)),
            ],
        )

    def test_parse_blog_article_uses_jina_metadata(self) -> None:
        markdown = """Title: Build agents that run automatically

URL Source: http://cursor.com/blog/automations

Published Time: 2026-03-05T12:00:00.000Z

Markdown Content:
We're introducing Cursor Automations to build always-on agents.

Second paragraph.
"""
        item = parse_blog_article(
            markdown,
            fallback_url="https://cursor.com/blog/automations",
            fallback_published=None,
        )

        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.summary, "We're introducing Cursor Automations to build always-on agents.")
        self.assertEqual(item.link, "https://cursor.com/blog/automations")

    def test_parse_x_markdown_deduplicates_pinned_and_ignores_media_lines(self) -> None:
        markdown = """Title: Cursor (@cursor_ai) / X

Published Time: Sun, 08 Mar 2026 15:02:13 GMT

Markdown Content:
Cursor

@cursor_ai

Cursor’s posts
--------------

Pinned

[![Image 1]](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 2]](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 3](https://pbs.twimg.com/image.jpg)

[![Image 4]](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor!
"""
        fetched_at, items = parse_x_markdown(markdown)

        self.assertEqual(fetched_at, datetime(2026, 3, 8, 15, 2, 13, tzinfo=timezone.utc))
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].summary, "We're introducing Cursor Automations to build always-on agents.")
        self.assertEqual(items[1].summary, "GPT 5.4 is now available in Cursor!")

    def test_should_run_only_once_at_nine_am_shanghai(self) -> None:
        now = datetime.fromisoformat("2026-03-09T09:15:00+08:00")
        fresh_state = {"last_run_date": None, "seen": {"changelog": [], "blog": [], "x": []}}
        seen_state = {"last_run_date": "2026-03-09", "seen": {"changelog": [], "blog": [], "x": []}}

        self.assertTrue(should_run(now, fresh_state, force=False))
        self.assertFalse(should_run(now, seen_state, force=False))
        self.assertTrue(should_run(now, seen_state, force=True))

    def test_force_run_writes_report_without_persisting_state(self) -> None:
        now = datetime.fromisoformat("2026-03-09T03:00:00+08:00")
        changelog_snapshot = SourceSnapshot(
            "changelog",
            [
                UpdateItem(
                    "changelog",
                    "cl-1",
                    "Automations",
                    "https://cursor.com/changelog/03-05-26",
                    "2026-03-05T00:00:00Z",
                    "Always-on agents.",
                )
            ],
            [
                UpdateItem(
                    "changelog",
                    "cl-1",
                    "Automations",
                    "https://cursor.com/changelog/03-05-26",
                    "2026-03-05T00:00:00Z",
                    "Always-on agents.",
                )
            ],
        )
        empty_blog = SourceSnapshot("blog", [], [])
        empty_x = SourceSnapshot("x", [], [])

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            report_path = Path(tmpdir) / "report.md"

            did_run, output = run_watch(
                now=now,
                force=True,
                update_state=False,
                state_path=state_path,
                report_path=report_path,
                changelog_fetcher=lambda _now, _seen: changelog_snapshot,
                blog_fetcher=lambda _now, _seen: empty_blog,
                x_fetcher=lambda _now, _seen: empty_x,
            )

            self.assertTrue(did_run)
            self.assertIn("Automations", output)
            self.assertTrue(report_path.exists())
            self.assertFalse(state_path.exists())

    def test_scheduled_run_persists_state(self) -> None:
        now = datetime.fromisoformat("2026-03-09T09:00:00+08:00")
        empty_changelog = SourceSnapshot("changelog", [], [])
        empty_blog = SourceSnapshot("blog", [], [])
        x_snapshot = SourceSnapshot(
            "x",
            [UpdateItem("x", "x-1", "Post", "https://x.com/cursor_ai", None, "Post body")],
            [UpdateItem("x", "x-1", "Post", "https://x.com/cursor_ai", None, "Post body")],
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            state_path = Path(tmpdir) / "state.json"
            report_path = Path(tmpdir) / "report.md"

            did_run, _ = run_watch(
                now=now,
                force=False,
                update_state=False,
                state_path=state_path,
                report_path=report_path,
                changelog_fetcher=lambda _now, _seen: empty_changelog,
                blog_fetcher=lambda _now, _seen: empty_blog,
                x_fetcher=lambda _now, _seen: x_snapshot,
            )

            self.assertTrue(did_run)
            state = load_state(state_path)
            self.assertEqual(state["last_run_date"], "2026-03-09")
            self.assertEqual(state["seen"]["x"], ["x-1"])


if __name__ == "__main__":
    unittest.main()
