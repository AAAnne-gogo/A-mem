import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from cursor_updates_watch import (
    BLOG_SITEMAP_URL,
    CHANGELOG_URL,
    X_MIRROR_URLS,
    TARGET_TIMEZONE,
    parse_blog_article_html,
    parse_blog_sitemap,
    parse_changelog_html,
    parse_x_feed_markdown,
    run_watch,
    should_run_scheduled,
)


SAMPLE_CHANGELOG_HTML = """
<section>
  <p><a href="/changelog/03-05-26"><time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time></a></p>
  <header><h1 id="automations"><a href="/changelog/03-05-26">Automations</a></h1></header>
  <div class="prose prose--block"><p>Cursor now supports <a href="/docs">automations</a> for building always-on agents.</p></div>
</section>
<section>
  <p><a href="/changelog/2-6"><span class="label">2.6</span><span> </span><time dateTime="2026-03-03T00:00:00.000Z">Mar 3, 2026</time></a></p>
  <header><h1 id="mcp-apps"><a href="/changelog/2-6">MCP Apps and Team Marketplaces for Plugins</a></h1></header>
  <div class="prose prose--block"><p>This release introduces interactive UIs in agent chats.</p></div>
</section>
"""

SAMPLE_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog</loc>
    <lastmod>2026-03-01T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/topic/product</loc>
    <lastmod>2026-03-01T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""

SAMPLE_BLOG_HTML = """
<html>
  <head>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"BlogPosting","headline":"Build agents that run automatically","description":"Cursor now supports automations that run based on triggers and instructions you define.","datePublished":"2026-03-05T12:00:00.000Z","url":"https://cursor.com/blog/automations"}
    </script>
  </head>
</html>
"""

SAMPLE_X_MARKDOWN = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 21:50:43 GMT

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

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor is now available in JetBrains IDEs through the Agent Client Protocol.

![Image 5](https://pbs.twimg.com/amplify_video_thumb/example.jpg)

[![Image 6: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor now supports MCP Apps.
"""


class StaticFetcher:
    def __init__(self, mapping):
        self.mapping = mapping

    def fetch_text(self, url: str) -> str:
        try:
            return self.mapping[url]
        except KeyError as error:
            raise AssertionError(f"Unexpected URL requested: {url}") from error


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_html_extracts_latest_entries(self):
        items = parse_changelog_html(SAMPLE_CHANGELOG_HTML, limit=5)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertIn("always-on agents", items[0].summary)
        self.assertEqual(items[1].title, "MCP Apps and Team Marketplaces for Plugins")

    def test_parse_blog_sitemap_filters_and_sorts_article_urls(self):
        entries = parse_blog_sitemap(SAMPLE_SITEMAP_XML)

        self.assertEqual(
            entries,
            [
                ("https://cursor.com/blog/automations", "2026-03-05T12:00:00.000Z"),
                ("https://cursor.com/blog/jetbrains-acp", "2026-03-04T12:00:00.000Z"),
            ],
        )

    def test_parse_blog_article_html_reads_structured_metadata(self):
        item = parse_blog_article_html(SAMPLE_BLOG_HTML, fallback_url="https://cursor.com/blog/automations")

        self.assertIsNotNone(item)
        assert item is not None
        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.url, "https://cursor.com/blog/automations")
        self.assertEqual(item.published_at, "2026-03-05T12:00:00.000Z")
        self.assertIn("triggers", item.summary)

    def test_parse_x_feed_markdown_deduplicates_and_filters_noise(self):
        posts = parse_x_feed_markdown(SAMPLE_X_MARKDOWN, limit=5)

        self.assertEqual(
            [post.text for post in posts],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "Cursor is now available in JetBrains IDEs through the Agent Client Protocol.",
                "Cursor now supports MCP Apps.",
            ],
        )

    def test_should_run_scheduled_only_in_target_hour_and_once_per_day(self):
        state = {"last_successful_scheduled_date": None, "seen": {"changelog": [], "blog": [], "x": []}}
        outside_window = datetime(2026, 3, 7, 8, 0, tzinfo=TARGET_TIMEZONE)
        inside_window = datetime(2026, 3, 7, 9, 15, tzinfo=TARGET_TIMEZONE)

        should_run, reason = should_run_scheduled(outside_window, state)
        self.assertFalse(should_run)
        self.assertIn("only runs during the 09:00 hour", reason)

        should_run, _ = should_run_scheduled(inside_window, state)
        self.assertTrue(should_run)

        state["last_successful_scheduled_date"] = "2026-03-07"
        should_run, reason = should_run_scheduled(inside_window, state)
        self.assertFalse(should_run)
        self.assertIn("already succeeded", reason)

    def test_run_watch_updates_state_on_scheduled_run_but_not_force(self):
        mapping = {
            CHANGELOG_URL: SAMPLE_CHANGELOG_HTML,
            BLOG_SITEMAP_URL: SAMPLE_SITEMAP_XML,
            "https://cursor.com/blog/automations": SAMPLE_BLOG_HTML,
            "https://cursor.com/blog/jetbrains-acp": SAMPLE_BLOG_HTML.replace(
                "Build agents that run automatically",
                "Cursor is now available in JetBrains IDEs",
            ).replace(
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ).replace(
                "2026-03-05T12:00:00.000Z",
                "2026-03-04T12:00:00.000Z",
            ),
            X_MIRROR_URLS[0]: SAMPLE_X_MARKDOWN,
        }
        fetcher = StaticFetcher(mapping)

        with tempfile.TemporaryDirectory() as temp_dir_name:
            temp_dir = Path(temp_dir_name)
            state_file = temp_dir / "state.json"
            latest_report_file = temp_dir / "latest_report.md"
            public_report_file = temp_dir / "cursor_updates.md"
            history_dir = temp_dir / "history"

            forced_result = run_watch(
                fetcher=fetcher,
                now=datetime(2026, 3, 7, 6, 0, tzinfo=TARGET_TIMEZONE),
                force=True,
                limit=2,
                state_file=state_file,
                latest_report_file=latest_report_file,
                public_report_file=public_report_file,
                history_dir=history_dir,
            )

            self.assertEqual(forced_result.status, "ran")
            self.assertFalse(state_file.exists())
            self.assertFalse(history_dir.exists())
            self.assertTrue(latest_report_file.exists())
            self.assertTrue(public_report_file.exists())

            scheduled_result = run_watch(
                fetcher=fetcher,
                now=datetime(2026, 3, 7, 9, 0, tzinfo=TARGET_TIMEZONE),
                force=False,
                limit=2,
                state_file=state_file,
                latest_report_file=latest_report_file,
                public_report_file=public_report_file,
                history_dir=history_dir,
            )

            self.assertEqual(scheduled_result.status, "ran")
            self.assertTrue(state_file.exists())
            self.assertTrue((history_dir / "2026-03-07.md").exists())

            state = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["last_successful_scheduled_date"], "2026-03-07")
            self.assertEqual(len(state["seen"]["changelog"]), 2)
            self.assertEqual(len(state["seen"]["blog"]), 2)
            self.assertEqual(len(state["seen"]["x"]), 2)


if __name__ == "__main__":
    unittest.main()
