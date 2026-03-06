from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import cursor_updates_watch as watch


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
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
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-04-26</loc>
    <lastmod>2026-03-04T00:00:00.000Z</lastmod>
  </url>
</urlset>
"""


CHANGELOG_1 = """Title: Automations · Cursor

URL Source: http://cursor.com/changelog/03-05-26

Markdown Content:
Automations
===========

Cursor now supports automations for building always-on agents that run based on triggers and instructions you define.
"""


CHANGELOG_2 = """Title: Cursor in JetBrains IDEs · Cursor

URL Source: http://cursor.com/changelog/03-04-26

Markdown Content:
Cursor in JetBrains IDEs
========================

Cursor is now available in JetBrains IDEs through the Agent Client Protocol.
"""


BLOG_1 = """Title: Build agents that run automatically

URL Source: http://cursor.com/blog/automations

Published Time: 2026-03-05T12:00:00.000Z

Markdown Content:
We're introducing Cursor Automations to build always-on agents.

Automations run on schedules or are triggered by events from Slack, Linear, GitHub, PagerDuty, and webhooks.
"""


BLOG_2 = """Title: Cursor is now available in JetBrains IDEs

URL Source: http://cursor.com/blog/jetbrains-acp

Published Time: 2026-03-04T12:00:00.000Z

Markdown Content:
Cursor is now available in JetBrains IDEs through ACP.
"""


X_MIRROR = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Fri, 06 Mar 2026 21:05:06 GMT

Markdown Content:
Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.
"""


def fake_fetcher(url: str) -> str:
    payloads = {
        watch.SITEMAP_URL: SITEMAP_XML,
        watch.X_MIRROR_URL: X_MIRROR,
        watch.to_jina_http_url("https://cursor.com/changelog/03-05-26"): CHANGELOG_1,
        watch.to_jina_http_url("https://cursor.com/changelog/03-04-26"): CHANGELOG_2,
        watch.to_jina_http_url("https://cursor.com/blog/automations"): BLOG_1,
        watch.to_jina_http_url("https://cursor.com/blog/jetbrains-acp"): BLOG_2,
    }
    return payloads[url]


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_fetch_text_retries_transient_http_error(self) -> None:
        class FakeResponse:
            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b"ok"

        transient_error = watch.urllib.error.HTTPError(
            url="https://example.com",
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=None,
        )
        with mock.patch.object(
            watch.urllib.request,
            "urlopen",
            side_effect=[transient_error, FakeResponse()],
        ) as mocked_urlopen:
            result = watch.fetch_text("https://example.com", retries=2)

        self.assertEqual(result, "ok")
        self.assertEqual(mocked_urlopen.call_count, 2)

    def test_parse_sitemap_entries_filters_and_sorts(self) -> None:
        changelog = watch.parse_sitemap_entries(SITEMAP_XML, "changelog")
        blog = watch.parse_sitemap_entries(SITEMAP_XML, "blog")

        self.assertEqual(
            [entry["url"] for entry in changelog],
            [
                "https://cursor.com/changelog/03-05-26",
                "https://cursor.com/changelog/03-04-26",
            ],
        )
        self.assertEqual(
            [entry["url"] for entry in blog],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )

    def test_parse_x_timeline_dedupes_posts(self) -> None:
        timeline = watch.parse_x_timeline(X_MIRROR)
        self.assertEqual(timeline["account"], "Cursor (@cursor_ai)")
        self.assertEqual(
            timeline["posts"],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.",
            ],
        )

    def test_should_run_only_allows_scheduled_nine_am(self) -> None:
        current_utc = datetime(2026, 3, 6, 0, 0, tzinfo=timezone.utc)
        should_execute, reason = watch.should_run({}, current_utc, force=False)
        self.assertFalse(should_execute)
        self.assertIn("09:00", reason)

    def test_should_run_blocks_duplicate_scheduled_date(self) -> None:
        current_utc = datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc)
        state = {"last_successful_scheduled_date": "2026-03-07"}
        should_execute, reason = watch.should_run(state, current_utc, force=False)
        self.assertFalse(should_execute)
        self.assertIn("already completed", reason)

    def test_forced_run_does_not_consume_scheduled_slot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            current_utc = datetime(2026, 3, 6, 22, 0, tzinfo=timezone.utc)
            code, output = watch.run(base_dir=base_dir, force=True, fetcher=fake_fetcher, current_utc=current_utc)
            self.assertEqual(code, 0)
            self.assertIn("# Cursor daily updates", output)

            state = watch.load_state(base_dir / watch.RUNTIME_DIRNAME / "state.json")
            self.assertEqual(state["last_successful_mode"], "forced")
            self.assertNotIn("last_successful_scheduled_date", state)

    def test_scheduled_run_writes_report_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base_dir = Path(temp_dir)
            current_utc = datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc)
            code, output = watch.run(base_dir=base_dir, force=False, fetcher=fake_fetcher, current_utc=current_utc)
            self.assertEqual(code, 0)
            self.assertIn("## Changelog", output)
            self.assertIn("## Blog", output)
            self.assertIn("## Official X (@cursor_ai)", output)

            runtime_dir = base_dir / watch.RUNTIME_DIRNAME
            self.assertTrue((runtime_dir / "latest_report.md").exists())
            self.assertTrue((runtime_dir / "history" / "2026-03-07.md").exists())

            state = watch.load_state(runtime_dir / "state.json")
            self.assertEqual(state["last_successful_mode"], "scheduled")
            self.assertEqual(state["last_successful_scheduled_date"], "2026-03-07")


if __name__ == "__main__":
    unittest.main()
