import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cursor_updates_watch as watch


SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T00:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""


CHANGELOG_HTML = """
<html>
  <head><title>Automations · Cursor</title></head>
  <body><time datetime="2026-03-05T00:00:00.000Z"></time></body>
</html>
"""


BLOG_HTML = """
<html>
  <head><title>Build agents that run automatically · Cursor</title></head>
  <body>{"datePublished":"2026-03-05T12:00:00.000Z"}</body>
</html>
"""


X_TEXT = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 08:21:25 GMT

Markdown Content:
[![Image 1: Square profile picture](https://example.com/a.jpg)](https://x.com/cursor_ai)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 2: Square profile picture](https://example.com/b.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 3: Square profile picture](https://example.com/c.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 4: Square profile picture](https://example.com/d.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 5](https://example.com/video.jpg)
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_sitemap_filters_section_and_sorts_latest_first(self) -> None:
        blog_entries = watch.parse_sitemap(SITEMAP_XML, "blog")
        self.assertEqual(
            [item["loc"] for item in blog_entries],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )

    def test_extract_x_posts_filters_images_and_duplicates(self) -> None:
        account, published_time, posts = watch.extract_x_posts(X_TEXT)
        self.assertEqual(account, "Cursor (@cursor_ai)")
        self.assertEqual(published_time, "Sat, 07 Mar 2026 08:21:25 GMT")
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.",
            ],
        )

    def test_should_run_respects_nine_am_window_and_duplicate_guard(self) -> None:
        local_time = watch.datetime(2026, 3, 7, 8, 59, tzinfo=watch.LOCAL_TZ)
        self.assertEqual(
            watch.should_run(False, local_time, {}),
            (False, "outside 09:00 Asia/Shanghai window (current hour: 08)"),
        )

        local_time = watch.datetime(2026, 3, 7, 9, 5, tzinfo=watch.LOCAL_TZ)
        state = {"last_successful_scheduled_date": "2026-03-07"}
        self.assertEqual(
            watch.should_run(False, local_time, state),
            (False, "already completed scheduled run for 2026-03-07"),
        )

    def test_force_run_writes_report_without_consuming_scheduled_guard(self) -> None:
        url_to_body = {
            watch.SITEMAP_URL: SITEMAP_XML,
            "https://cursor.com/changelog/03-05-26": CHANGELOG_HTML,
            "https://cursor.com/blog/automations": BLOG_HTML,
            "https://cursor.com/blog/jetbrains-acp": BLOG_HTML.replace("2026-03-05", "2026-03-04").replace(
                "Build agents that run automatically", "Cursor is now available in JetBrains IDEs"
            ),
            watch.X_MIRROR_URLS[0]: X_TEXT,
        }

        def fake_fetch(url: str) -> str:
            return url_to_body[url]

        fixed_now = watch.datetime(2026, 3, 7, 1, 2, tzinfo=watch.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.object(watch, "utc_now", return_value=fixed_now):
                result = watch.run(force=True, workspace=Path(tmp_dir), fetch_text=fake_fetch)

            self.assertEqual(result["status"], "fetched")
            state_path = Path(tmp_dir) / watch.STATE_DIR_NAME / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_checked_utc"], "2026-03-07T01:02:00Z")
            self.assertNotIn("last_successful_scheduled_date", state)

            report_path = Path(tmp_dir) / watch.STATE_DIR_NAME / "latest_report.md"
            report = report_path.read_text(encoding="utf-8")
            self.assertIn("Build agents that run automatically", report)
            self.assertIn("We're introducing Cursor Automations", report)


if __name__ == "__main__":
    unittest.main()
