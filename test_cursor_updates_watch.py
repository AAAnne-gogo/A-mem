from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import cursor_updates_watch as watcher


SAMPLE_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T10:30:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T09:30:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T11:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-04-26</loc>
    <lastmod>2026-03-04T08:00:00.000Z</lastmod>
  </url>
</urlset>
"""

SAMPLE_BLOG_HTML = """
<!DOCTYPE html>
<html>
  <body>
    <h1>Build agents that run automatically</h1>
  </body>
</html>
"""

SAMPLE_CHANGELOG_HTML = """
<!DOCTYPE html>
<html>
  <body>
    <h1>Automations</h1>
  </body>
</html>
"""

SAMPLE_X_MARKDOWN = """
Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Fri, 06 Mar 2026 20:03:24 GMT

Markdown Content:
[![Image 1]](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 2]](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:27

[![Image 3]](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 4]](https://x.com/cursor_ai)

Cursor can now continuously monitor and improve your codebase. Automations run based on triggers and instructions you define.
"""


class CursorUpdatesWatchTest(unittest.TestCase):
    def test_parse_sitemap_entries_filters_and_sorts(self) -> None:
        blog_entries = watcher.parse_sitemap_entries(
            SAMPLE_SITEMAP,
            "https://cursor.com/blog/",
        )
        changelog_entries = watcher.parse_sitemap_entries(
            SAMPLE_SITEMAP,
            "https://cursor.com/changelog/",
        )

        self.assertEqual(
            [entry.url for entry in blog_entries],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )
        self.assertEqual(
            [entry.url for entry in changelog_entries],
            [
                "https://cursor.com/changelog/03-05-26",
                "https://cursor.com/changelog/03-04-26",
            ],
        )

    def test_extract_title_prefers_h1(self) -> None:
        self.assertEqual(
            watcher.extract_title_from_html(
                SAMPLE_BLOG_HTML,
                "https://cursor.com/blog/automations",
            ),
            "Build agents that run automatically",
        )
        self.assertEqual(
            watcher.extract_title_from_html(
                SAMPLE_CHANGELOG_HTML,
                "https://cursor.com/changelog/03-05-26",
            ),
            "Automations",
        )

    def test_extract_x_posts_parses_recent_posts(self) -> None:
        parsed = watcher.extract_x_posts(SAMPLE_X_MARKDOWN)

        self.assertEqual(parsed["account"], "Cursor (@cursor_ai)")
        self.assertEqual(parsed["source_url"], "https://x.com/cursor_ai")
        self.assertEqual(parsed["published_time"], "Fri, 06 Mar 2026 20:03:24 GMT")
        self.assertEqual(
            parsed["posts"],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.",
                "Cursor can now continuously monitor and improve your codebase. Automations run based on triggers and instructions you define.",
            ],
        )

    def test_should_run_now_honors_schedule_and_duplicate_guard(self) -> None:
        not_nine_utc = datetime(2026, 3, 6, 0, 30, tzinfo=timezone.utc)
        should_run, reason = watcher.should_run_now(
            not_nine_utc,
            watcher.DEFAULT_TIMEZONE,
            state={},
            force=False,
        )
        self.assertFalse(should_run)
        self.assertIn("09:00", reason)

        nine_utc = datetime(2026, 3, 7, 1, 5, tzinfo=timezone.utc)
        should_run, _ = watcher.should_run_now(
            nine_utc,
            watcher.DEFAULT_TIMEZONE,
            state={},
            force=False,
        )
        self.assertTrue(should_run)

        should_run, reason = watcher.should_run_now(
            nine_utc,
            watcher.DEFAULT_TIMEZONE,
            state={"last_scheduled_date": "2026-03-07"},
            force=False,
        )
        self.assertFalse(should_run)
        self.assertIn("already completed", reason)

        should_run, _ = watcher.should_run_now(
            not_nine_utc,
            watcher.DEFAULT_TIMEZONE,
            state={"last_scheduled_date": "2026-03-06"},
            force=True,
        )
        self.assertTrue(should_run)

    def test_run_watch_force_updates_state_without_consuming_daily_slot(self) -> None:
        now_utc = datetime(2026, 3, 6, 20, 10, tzinfo=timezone.utc)

        def fake_fetch(url: str, timeout: int = 30) -> str:
            del timeout
            mapping = {
                watcher.SITEMAP_URL: SAMPLE_SITEMAP,
                "https://cursor.com/blog/automations": SAMPLE_BLOG_HTML,
                "https://cursor.com/blog/jetbrains-acp": SAMPLE_BLOG_HTML.replace(
                    "Build agents that run automatically",
                    "Cursor is now available in JetBrains IDEs",
                ),
                "https://cursor.com/changelog/03-05-26": SAMPLE_CHANGELOG_HTML,
                "https://cursor.com/changelog/03-04-26": SAMPLE_CHANGELOG_HTML.replace(
                    "Automations",
                    "Cursor in JetBrains IDEs",
                ),
                watcher.X_MIRROR_URL: SAMPLE_X_MARKDOWN,
            }
            return mapping[url]

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir) / ".cursor_updates"
            with mock.patch.object(watcher, "fetch_text", side_effect=fake_fetch):
                ran, report = watcher.run_watch(
                    state_dir=state_dir,
                    timezone_name=watcher.DEFAULT_TIMEZONE,
                    force=True,
                    now_utc=now_utc,
                )

            self.assertTrue(ran)
            self.assertIn("baseline snapshot", report)

            state = watcher.load_state(state_dir / "state.json")
            self.assertEqual(state["last_forced_run_utc"], now_utc.isoformat())
            self.assertNotIn("last_scheduled_date", state)
            self.assertTrue((state_dir / "latest_report.md").exists())
            self.assertFalse((state_dir / "history").exists())


if __name__ == "__main__":
    unittest.main()
