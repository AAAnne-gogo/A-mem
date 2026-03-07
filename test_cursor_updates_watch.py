from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, Mock, patch

import cursor_updates_watch as watch


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/older-post</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/new-post</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-04-26</loc>
    <lastmod>2026-03-04T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/about</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""


BLOG_HTML = """
<html>
  <head>
    <title>Build agents that run automatically · Cursor</title>
    <meta name="description" content="Cursor is the best way to build software with AI.">
    <script type="application/ld+json">
      {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "headline": "Build agents that run automatically",
        "datePublished": "2026-03-05T12:00:00.000Z",
        "description": "Announcing Cursor Automations."
      }
    </script>
  </head>
  <body>
    <article><p>Automations run on schedules and event triggers.</p></article>
  </body>
</html>
"""


CHANGELOG_HTML = """
<html>
  <head><title>Automations · Cursor</title></head>
  <body>
    <header><h1 id="automations">Automations</h1></header>
    <time>Mar 5, 2026</time>
    <div class="prose prose--block">
      <p>Cursor now supports automations for building always-on agents.</p>
    </div>
  </body>
</html>
"""


X_MARKDOWN = """
Title: Cursor (@cursor_ai) / X

Markdown Content:
[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/1.jpg)](https://x.com/cursor_ai)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/2.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/3.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/4.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    @patch.object(watch.time, "sleep", return_value=None)
    @patch.object(watch, "urlopen")
    def test_fetch_text_retries_transient_http_error(self, urlopen_mock: Mock, _: Mock) -> None:
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers.get_content_charset.return_value = "utf-8"
        response.read.return_value = b"ok"

        urlopen_mock.side_effect = [
            HTTPError("https://example.com", 429, "rate limited", hdrs=None, fp=None),
            response,
        ]

        self.assertEqual(watch.fetch_text("https://example.com"), "ok")
        self.assertEqual(urlopen_mock.call_count, 2)

    def test_parse_sitemap_entries_sorts_latest_first(self) -> None:
        blog_entries = watch.parse_sitemap_entries(SITEMAP_XML, "blog")
        changelog_entries = watch.parse_sitemap_entries(SITEMAP_XML, "changelog")

        self.assertEqual(
            [url for url, _ in blog_entries],
            [
                "https://cursor.com/blog/new-post",
                "https://cursor.com/blog/older-post",
            ],
        )
        self.assertEqual(
            [url for url, _ in changelog_entries],
            [
                "https://cursor.com/changelog/03-05-26",
                "https://cursor.com/changelog/03-04-26",
            ],
        )

    @patch.object(watch, "fetch_text", return_value=BLOG_HTML)
    def test_build_blog_item_extracts_json_ld(self, _: Mock) -> None:
        item = watch.build_blog_item("https://cursor.com/blog/automations", fallback_published=None)

        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.summary, "Announcing Cursor Automations.")
        self.assertEqual(item.published, datetime(2026, 3, 5, 12, 0, tzinfo=UTC))

    @patch.object(watch, "fetch_text", return_value=CHANGELOG_HTML)
    def test_build_changelog_item_extracts_heading_time_and_summary(self, _: Mock) -> None:
        item = watch.build_changelog_item("https://cursor.com/changelog/03-05-26", fallback_published=None)

        self.assertEqual(item.title, "Automations")
        self.assertEqual(
            item.summary,
            "Cursor now supports automations for building always-on agents.",
        )
        self.assertEqual(item.published, datetime(2026, 3, 5, 0, 0, tzinfo=watch.LOCAL_TIMEZONE))

    def test_build_x_items_deduplicates_posts(self) -> None:
        items = watch.build_x_items(X_MARKDOWN)

        self.assertEqual(len(items), 2)
        self.assertEqual(
            [item.summary for item in items],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                (
                    "GPT 5.4 is now available in Cursor! We've found it to be more natural "
                    "and assertive than previous models."
                ),
            ],
        )

    def test_should_run_now_respects_schedule_and_daily_guard(self) -> None:
        off_hour = datetime(2026, 3, 7, 19, 1, tzinfo=UTC)
        can_run, reason = watch.should_run_now(off_hour, {"last_scheduled_run_date": None}, force=False)
        self.assertFalse(can_run)
        self.assertIn("scheduled hour is 09:00", reason)

        scheduled_hour = datetime(2026, 3, 8, 1, 1, tzinfo=UTC)
        can_run, reason = watch.should_run_now(scheduled_hour, {"last_scheduled_run_date": None}, force=False)
        self.assertTrue(can_run)
        self.assertIn("scheduled run for 2026-03-08", reason)

        can_run, reason = watch.should_run_now(
            scheduled_hour,
            {"last_scheduled_run_date": "2026-03-08"},
            force=False,
        )
        self.assertFalse(can_run)
        self.assertIn("already completed scheduled run", reason)

    def test_load_state_supports_legacy_state_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            state_path = temp_root / "state.json"
            legacy_state_path = temp_root / "seen_state.json"
            legacy_state = {"last_scheduled_run_date": "2026-03-08", "seen": {"blog": ["b"], "changelog": [], "x": []}}
            legacy_state_path.write_text(json.dumps(legacy_state), encoding="utf-8")

            with patch.multiple(
                watch,
                STATE_PATH=state_path,
                LEGACY_STATE_PATH=legacy_state_path,
            ):
                self.assertEqual(watch.load_state(), legacy_state)

    def test_write_report_persists_history_for_scheduled_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_root = Path(temp_dir)
            report_dir = temp_root / ".cursor_updates"
            history_dir = report_dir / "history"
            latest_report_path = report_dir / "latest_report.md"
            friendly_report_path = temp_root / "cursor_updates.md"
            state_path = report_dir / "state.json"
            legacy_state_path = report_dir / "seen_state.json"
            now_utc = datetime(2026, 3, 8, 1, 1, tzinfo=UTC)

            with patch.multiple(
                watch,
                REPORT_DIR=report_dir,
                HISTORY_DIR=history_dir,
                LATEST_REPORT_PATH=latest_report_path,
                FRIENDLY_REPORT_PATH=friendly_report_path,
                STATE_PATH=state_path,
                LEGACY_STATE_PATH=legacy_state_path,
            ):
                watch.write_report(now_utc, "hello\n", persist_history=True)

            self.assertEqual(latest_report_path.read_text(encoding="utf-8"), "hello\n")
            self.assertEqual(friendly_report_path.read_text(encoding="utf-8"), "hello\n")
            history_report_path = history_dir / "2026-03-08.md"
            self.assertEqual(history_report_path.read_text(encoding="utf-8"), "hello\n")

    @patch.object(watch, "write_report")
    @patch.object(watch, "write_state")
    @patch.object(watch, "fetch_first_success", return_value=X_MARKDOWN)
    @patch.object(watch, "load_state", return_value={"last_scheduled_run_date": None, "seen": {"blog": [], "changelog": [], "x": []}})
    @patch.object(watch, "fetch_text")
    def test_run_force_generates_report_without_updating_state(
        self,
        fetch_text_mock: Mock,
        _: Mock,
        __: Mock,
        write_state_mock: Mock,
        write_report_mock: Mock,
    ) -> None:
        def fake_fetch(url: str) -> str:
            if url == watch.CURSOR_SITEMAP_URL:
                return SITEMAP_XML
            if "/blog/" in url:
                return BLOG_HTML
            if "/changelog/" in url:
                return CHANGELOG_HTML
            raise AssertionError(f"unexpected url: {url}")

        fetch_text_mock.side_effect = fake_fetch

        result = watch.run(force=True, now_utc=datetime(2026, 3, 7, 19, 1, tzinfo=UTC))

        self.assertFalse(result.skipped)
        self.assertIn("# Cursor Daily Updates", result.report)
        self.assertIn("Run mode: forced run", result.report)
        write_report_mock.assert_called_once()
        write_state_mock.assert_not_called()

    @patch.object(watch, "write_report")
    @patch.object(watch, "write_state")
    @patch.object(watch, "fetch_first_success", return_value=X_MARKDOWN)
    @patch.object(watch, "load_state", return_value={"last_scheduled_run_date": None, "seen": {"blog": [], "changelog": [], "x": []}})
    @patch.object(watch, "fetch_text")
    def test_run_scheduled_updates_state(
        self,
        fetch_text_mock: Mock,
        _: Mock,
        __: Mock,
        write_state_mock: Mock,
        ___: Mock,
    ) -> None:
        def fake_fetch(url: str) -> str:
            if url == watch.CURSOR_SITEMAP_URL:
                return SITEMAP_XML
            if "/blog/" in url:
                return BLOG_HTML
            if "/changelog/" in url:
                return CHANGELOG_HTML
            raise AssertionError(f"unexpected url: {url}")

        fetch_text_mock.side_effect = fake_fetch

        result = watch.run(force=False, now_utc=datetime(2026, 3, 8, 1, 1, tzinfo=UTC))

        self.assertFalse(result.skipped)
        write_state_mock.assert_called_once()
        written_state = write_state_mock.call_args.args[0]
        self.assertEqual(written_state["last_scheduled_run_date"], "2026-03-08")
        self.assertIn("https://cursor.com/blog/new-post", written_state["seen"]["blog"])
        self.assertIn("https://cursor.com/changelog/03-05-26", written_state["seen"]["changelog"])
        self.assertEqual(len(written_state["seen"]["x"]), 2)


if __name__ == "__main__":
    unittest.main()
