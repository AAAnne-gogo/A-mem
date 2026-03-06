import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cursor_updates_watch import (
    OFFICIAL_X_MIRROR_URL,
    SITEMAP_URL,
    build_feed_items,
    is_blog_post,
    is_changelog_post,
    parse_sitemap_entries,
    parse_x_snapshot,
    run_watch,
)


SAMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
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
    <loc>https://cursor.com/blog/topic/research</loc>
    <lastmod>2026-03-01T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/2-6</loc>
    <lastmod>2026-03-03T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/6</loc>
    <lastmod>2026-03-02T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""

SAMPLE_X = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Fri, 06 Mar 2026 22:07:17 GMT

Markdown Content:
[![Image 1: Square profile picture and Opens profile photo](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor's posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:33

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor!

[![Image 5: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor can now continuously monitor and improve your codebase.
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def fake_fetcher(self, url: str) -> str:
        responses = {
            SITEMAP_URL: SAMPLE_SITEMAP,
            "https://cursor.com/blog/automations": "<title>Build agents that run automatically · Cursor</title>",
            "https://cursor.com/blog/jetbrains-acp": '{"headline":"Cursor is now available in JetBrains IDEs"}',
            "https://cursor.com/changelog/03-05-26": "<title>Automations · Cursor</title>",
            "https://cursor.com/changelog/2-6": "<title>MCP Apps and Team Marketplaces for Plugins · Cursor</title>",
            OFFICIAL_X_MIRROR_URL: SAMPLE_X,
        }
        return responses[url]

    def test_parse_x_snapshot_deduplicates_posts(self) -> None:
        snapshot = parse_x_snapshot(SAMPLE_X, limit=5)

        self.assertEqual(snapshot.account_name, "Cursor")
        self.assertEqual(snapshot.handle, "@cursor_ai")
        self.assertEqual(snapshot.mirror_published_time, "Fri, 06 Mar 2026 22:07:17 GMT")
        self.assertEqual(
            snapshot.posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor!",
                "Cursor can now continuously monitor and improve your codebase.",
            ],
        )

    def test_build_feed_items_filters_out_listing_pages(self) -> None:
        entries = parse_sitemap_entries(SAMPLE_SITEMAP)

        blog_items = build_feed_items(entries, entry_filter=is_blog_post, limit=5, fetcher=self.fake_fetcher)
        changelog_items = build_feed_items(entries, entry_filter=is_changelog_post, limit=5, fetcher=self.fake_fetcher)

        self.assertEqual([item.url for item in blog_items], [
            "https://cursor.com/blog/automations",
            "https://cursor.com/blog/jetbrains-acp",
        ])
        self.assertEqual([item.url for item in changelog_items], [
            "https://cursor.com/changelog/03-05-26",
            "https://cursor.com/changelog/2-6",
        ])
        self.assertEqual(blog_items[0].title, "Build agents that run automatically")
        self.assertEqual(changelog_items[0].title, "Automations")

    def test_force_run_updates_state_without_consuming_daily_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir)
            result = run_watch(
                force=True,
                now=datetime(2026, 3, 6, 23, 2, tzinfo=timezone.utc),
                state_path=state_dir / "state.json",
                latest_report_path=state_dir / "latest_report.md",
                history_dir=state_dir / "history",
                fetcher=self.fake_fetcher,
            )

            self.assertEqual(result.status, "success")
            state = (state_dir / "state.json").read_text(encoding="utf-8")
            self.assertIn('"last_run_mode": "forced"', state)
            self.assertNotIn("last_scheduled_success_date", state)

    def test_scheduled_run_only_executes_once_per_local_day(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir)

            first = run_watch(
                force=False,
                now=datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc),
                state_path=state_dir / "state.json",
                latest_report_path=state_dir / "latest_report.md",
                history_dir=state_dir / "history",
                fetcher=self.fake_fetcher,
            )
            second = run_watch(
                force=False,
                now=datetime(2026, 3, 7, 1, 30, tzinfo=timezone.utc),
                state_path=state_dir / "state.json",
                latest_report_path=state_dir / "latest_report.md",
                history_dir=state_dir / "history",
                fetcher=self.fake_fetcher,
            )

            self.assertEqual(first.status, "success")
            self.assertEqual(second.status, "skipped")
            self.assertIn("scheduled run already completed", second.reason)

    def test_scheduled_run_skips_before_nine_am(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_dir = Path(tmp_dir)
            result = run_watch(
                force=False,
                now=datetime(2026, 3, 6, 23, 2, tzinfo=timezone.utc),
                state_path=state_dir / "state.json",
                latest_report_path=state_dir / "latest_report.md",
                history_dir=state_dir / "history",
                fetcher=self.fake_fetcher,
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("waiting for 09:00", result.reason)


if __name__ == "__main__":
    unittest.main()
