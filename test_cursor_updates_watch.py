import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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

X_MARKDOWN = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Fri, 06 Mar 2026 23:08:15 GMT

Markdown Content:
Cursor's posts
--------------

Pinned

[![Image 1]](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.

[![Image 2]](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.

[![Image 3]](https://x.com/cursor_ai)
Cursor is now available in JetBrains IDEs through the Agent Client Protocol.
We believe Cursor discovered a novel solution to Problem Six.

[![Image 4]](https://x.com/cursor_ai)
Cursor can now automatically fix issues it finds in PRs with Bugbot Autofix.
"""


def fake_fetcher_factory() -> callable:
    pages = {
        watch.SITEMAP_URL: SITEMAP_XML,
        watch.X_MIRROR_URL: X_MARKDOWN,
        "https://cursor.com/blog/automations": "<title>Build agents that run automatically - Cursor</title>",
        "https://cursor.com/blog/jetbrains-acp": "<meta property=\"og:title\" content=\"Cursor is now available in JetBrains IDEs\">",
        "https://cursor.com/changelog/03-05-26": "<title>Automations · Cursor</title>",
        "https://cursor.com/changelog/03-04-26": "<meta property=\"og:title\" content=\"Cursor in JetBrains IDEs\">",
    }

    def fake_fetcher(url: str, **_: object) -> str:
        return pages[url]

    return fake_fetcher


class CursorUpdatesWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.base_dir = Path(self.tempdir.name)
        self.paths = watch.default_paths(self.base_dir)
        self.fetcher = fake_fetcher_factory()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_should_run_before_nine_am_skips(self) -> None:
        state = {"last_successful_scheduled_date": None}
        now = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc)
        allowed, message = watch.should_run(now, state, force=False)
        self.assertFalse(allowed)
        self.assertIn("outside 09:00", message)

    def test_should_run_at_nine_am_allows_and_duplicate_skips(self) -> None:
        now = datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc)
        allowed, _ = watch.should_run(now, {"last_successful_scheduled_date": None}, force=False)
        self.assertTrue(allowed)

        duplicate_state = {"last_successful_scheduled_date": "2026-03-07"}
        allowed, message = watch.should_run(now, duplicate_state, force=False)
        self.assertFalse(allowed)
        self.assertIn("already completed successfully", message)

    def test_parse_title_and_x_posts(self) -> None:
        title = watch.parse_title_from_html("<title>Automations · Cursor</title>")
        self.assertEqual(title, "Automations")

        published_time = watch.parse_x_published_time(X_MARKDOWN)
        posts = watch.parse_x_posts(X_MARKDOWN, limit=5)
        self.assertEqual(published_time, "Fri, 06 Mar 2026 23:08:15 GMT")
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "Cursor is now available in JetBrains IDEs through the Agent Client Protocol. We believe Cursor discovered a novel solution to Problem Six.",
                "Cursor can now automatically fix issues it finds in PRs with Bugbot Autofix.",
            ],
        )

    def test_force_run_writes_report_without_consuming_scheduled_slot(self) -> None:
        now = datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc)
        status_code, output = watch.run(
            force=True,
            now=now,
            paths=self.paths,
            fetcher=self.fetcher,
        )
        self.assertEqual(status_code, 0)
        self.assertIn("# Cursor updates report", output)
        self.assertTrue(self.paths.latest_report_path.exists())

        state = json.loads(self.paths.state_path.read_text(encoding="utf-8"))
        self.assertIsNone(state["last_successful_scheduled_date"])
        self.assertEqual(state["latest"]["changelog"][0]["title"], "Automations")
        self.assertGreaterEqual(len(state["latest"]["x_posts"]), 3)

    def test_scheduled_run_updates_state_and_blocks_second_run_same_day(self) -> None:
        now = datetime(2026, 3, 7, 1, 0, tzinfo=timezone.utc)
        first_status, first_output = watch.run(
            force=False,
            now=now,
            paths=self.paths,
            fetcher=self.fetcher,
        )
        self.assertEqual(first_status, 0)
        self.assertIn("## Latest changelog", first_output)

        state = json.loads(self.paths.state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["last_successful_scheduled_date"], "2026-03-07")
        self.assertTrue((self.paths.history_dir / "2026-03-07.md").exists())

        second_status, second_output = watch.run(
            force=False,
            now=now,
            paths=self.paths,
            fetcher=self.fetcher,
        )
        self.assertEqual(second_status, 0)
        self.assertIn("already completed successfully", second_output)


if __name__ == "__main__":
    unittest.main()
