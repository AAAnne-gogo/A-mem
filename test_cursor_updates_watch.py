from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import cursor_updates_watch as watch


CHANGELOG_HTML = """
<div class="entry">
  <a href="/changelog/03-05-26"><time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time></a>
  <header><h1><a href="/changelog/03-05-26">Automations</a></h1></header>
  <div class="prose prose--block"><p>Cursor now supports <a href="https://cursor.com/docs">automations</a> for building always-on agents.</p></div>
</div>
<div class="entry">
  <a href="/changelog/2-6"><span class="label">2.6</span><span> </span><time dateTime="2026-03-03T00:00:00.000Z">Mar 3, 2026</time></a>
  <header><h1><a href="/changelog/2-6">MCP Apps and Team Marketplaces for Plugins</a></h1></header>
  <div class="prose prose--block"><p>This release introduces interactive UIs in agent chats.</p></div>
</div>
"""

BLOG_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com</loc>
    <lastmod>2026-03-08T01:01:58.044Z</lastmod>
  </url>
</urlset>
"""

BLOG_AUTOMATIONS_HTML = """
<html>
  <head>
    <meta property="og:title" content="Build agents that run automatically · Cursor" />
    <meta name="description" content="Cursor now supports automations that run based on triggers and instructions you define." />
  </head>
  <body>
    <header>
      <h1>Build agents that run automatically</h1>
      <time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time>
    </header>
  </body>
</html>
"""

BLOG_JETBRAINS_HTML = """
<html>
  <head>
    <title>Cursor is now available in JetBrains IDEs · Cursor</title>
    <meta property="og:description" content="Cursor now supports JetBrains IDEs through ACP." />
  </head>
  <body>
    <header>
      <time dateTime="2026-03-04T12:00:00.000Z">Mar 4, 2026</time>
    </header>
  </body>
</html>
"""

X_MARKDOWN = """
Title: Cursor (@cursor_ai) / X
URL Source: http://x.com/cursor_ai?output=1
Published Time: Sun, 08 Mar 2026 00:04:57 GMT
Markdown Content:
[![Image 1](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 2](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

[![Image 3](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 4](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.
"""


@contextmanager
def isolated_runtime() -> Path:
    original_values = {
        "RUNTIME_DIR": watch.RUNTIME_DIR,
        "STATE_PATH": watch.STATE_PATH,
        "LATEST_REPORT_PATH": watch.LATEST_REPORT_PATH,
        "HISTORY_DIR": watch.HISTORY_DIR,
        "PUBLIC_REPORT_PATH": watch.PUBLIC_REPORT_PATH,
    }
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        watch.RUNTIME_DIR = temp_path / ".cursor_updates"
        watch.STATE_PATH = watch.RUNTIME_DIR / "state.json"
        watch.LATEST_REPORT_PATH = watch.RUNTIME_DIR / "latest_report.md"
        watch.HISTORY_DIR = watch.RUNTIME_DIR / "history"
        watch.PUBLIC_REPORT_PATH = temp_path / "cursor_updates.md"
        try:
            yield temp_path
        finally:
            for name, value in original_values.items():
                setattr(watch, name, value)


def fake_fetcher(url: str) -> str:
    payloads = {
        watch.CHANGELOG_URL: CHANGELOG_HTML,
        watch.BLOG_SITEMAP_URL: BLOG_SITEMAP_XML,
        "https://cursor.com/blog/automations": BLOG_AUTOMATIONS_HTML,
        "https://cursor.com/blog/jetbrains": BLOG_JETBRAINS_HTML,
    }
    return payloads[url]


def fake_x_fetcher(urls: tuple[str, ...]) -> tuple[str, str]:
    return urls[-1], X_MARKDOWN


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_entries(self) -> None:
        entries = watch.parse_changelog_entries(CHANGELOG_HTML, limit=5)
        self.assertEqual([entry.title for entry in entries], ["Automations", "MCP Apps and Team Marketplaces for Plugins"])
        self.assertEqual(entries[0].summary, "Cursor now supports automations for building always-on agents.")
        self.assertEqual(entries[1].url, "https://cursor.com/changelog/2-6")

    def test_parse_blog_and_x_sources(self) -> None:
        sitemap_items = watch.parse_blog_sitemap(BLOG_SITEMAP_XML)
        self.assertEqual([item["url"] for item in sitemap_items], ["https://cursor.com/blog/automations", "https://cursor.com/blog/jetbrains"])

        automations = watch.parse_blog_article(BLOG_AUTOMATIONS_HTML, "https://cursor.com/blog/automations")
        self.assertEqual(automations.title, "Build agents that run automatically")
        self.assertEqual(automations.published_at, "2026-03-05T12:00:00.000Z")

        posts = watch.parse_x_posts(X_MARKDOWN, limit=5)
        self.assertEqual(len(posts), 2)
        self.assertIn("Cursor Automations", posts[0].text)
        self.assertIn("GPT 5.4", posts[1].text)

    def test_skip_outside_nine_am_preserves_existing_report(self) -> None:
        with isolated_runtime():
            existing_report = "# Previous report\n"
            watch.ensure_runtime_dirs()
            watch.LATEST_REPORT_PATH.write_text(existing_report, encoding="utf-8")
            watch.PUBLIC_REPORT_PATH.write_text(existing_report, encoding="utf-8")

            result = watch.run_watch(
                now=datetime(2026, 3, 8, 0, 11, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                x_fetcher=fake_x_fetcher,
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("09:00 hour", result.message)
            self.assertEqual(watch.PUBLIC_REPORT_PATH.read_text(encoding="utf-8"), existing_report)
            self.assertEqual(watch.LATEST_REPORT_PATH.read_text(encoding="utf-8"), existing_report)
            self.assertFalse(watch.STATE_PATH.exists())

    def test_force_run_does_not_mutate_state(self) -> None:
        with isolated_runtime():
            watch.ensure_runtime_dirs()
            state = {
                "last_success_local_date": "2026-03-07",
                "last_success_run_at": "2026-03-07T01:00:00+00:00",
                "seen": {"changelog": ["/changelog/old"], "blog": ["https://cursor.com/blog/old"], "x": ["abc123"]},
            }
            watch.write_state(state)
            before = watch.STATE_PATH.read_text(encoding="utf-8")

            result = watch.run_watch(
                force=True,
                now=datetime(2026, 3, 8, 0, 11, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                x_fetcher=fake_x_fetcher,
            )

            self.assertEqual(result.status, "ok")
            self.assertIn("Cursor Daily Updates", watch.PUBLIC_REPORT_PATH.read_text(encoding="utf-8"))
            self.assertEqual(watch.STATE_PATH.read_text(encoding="utf-8"), before)

    def test_scheduled_success_updates_state_and_blocks_second_run(self) -> None:
        with isolated_runtime():
            first = watch.run_watch(
                now=datetime(2026, 3, 8, 1, 5, tzinfo=timezone.utc),
                fetcher=fake_fetcher,
                x_fetcher=fake_x_fetcher,
            )
            self.assertEqual(first.status, "ok")
            self.assertTrue(watch.STATE_PATH.exists())
            self.assertTrue((watch.HISTORY_DIR / "2026-03-08.md").exists())

            state = json.loads(watch.STATE_PATH.read_text(encoding="utf-8"))
            self.assertEqual(state["last_success_local_date"], "2026-03-08")
            self.assertIn("/changelog/03-05-26", state["seen"]["changelog"])
            first_report = watch.PUBLIC_REPORT_PATH.read_text(encoding="utf-8")
            self.assertIn("Build agents that run automatically", first_report)

            second = watch.run_watch(
                now=datetime(2026, 3, 8, 1, 20, tzinfo=timezone.utc),
                fetcher=lambda _url: self.fail("fetcher should not be called on duplicate scheduled run"),
                x_fetcher=lambda _urls: self.fail("x_fetcher should not be called on duplicate scheduled run"),
            )
            self.assertEqual(second.status, "skipped")
            self.assertIn("already succeeded", second.message)
            self.assertEqual(watch.PUBLIC_REPORT_PATH.read_text(encoding="utf-8"), first_report)


if __name__ == "__main__":
    unittest.main()
