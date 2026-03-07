import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import cursor_updates_watch as watch


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_skip_before_nine(self) -> None:
        now_utc = watch.parse_now("2026-03-07T00:30:00Z")
        should_run, reason = watch.should_run_now(
            now_utc=now_utc,
            timezone=ZoneInfo("Asia/Shanghai"),
            force=False,
            scheduled_date="2026-03-07",
            last_scheduled_date=None,
        )

        self.assertFalse(should_run)
        self.assertIn("09:00 hour", reason)

    def test_should_skip_duplicate_scheduled_date(self) -> None:
        now_utc = watch.parse_now("2026-03-07T01:01:00Z")
        should_run, reason = watch.should_run_now(
            now_utc=now_utc,
            timezone=ZoneInfo("Asia/Shanghai"),
            force=False,
            scheduled_date="2026-03-07",
            last_scheduled_date="2026-03-07",
        )

        self.assertFalse(should_run)
        self.assertIn("already collected", reason)

    def test_force_run_does_not_consume_daily_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            self._run_main(
                output_dir=output_dir,
                now="2026-03-07T01:01:00Z",
                force=True,
            )

            state = json.loads((output_dir / watch.STATE_FILE_NAME).read_text(encoding="utf-8"))
            self.assertIsNone(state["last_successful_scheduled_date"])
            self.assertEqual(state["latest"]["x_published_time"], "Sat, 07 Mar 2026 01:03:09 GMT")

    def test_scheduled_run_records_daily_slot(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            self._run_main(
                output_dir=output_dir,
                now="2026-03-07T01:01:00Z",
                force=False,
            )

            state = json.loads((output_dir / watch.STATE_FILE_NAME).read_text(encoding="utf-8"))
            self.assertEqual(state["last_successful_scheduled_date"], "2026-03-07")
            self.assertTrue((output_dir / watch.LATEST_REPORT_NAME).exists())
            self.assertTrue((output_dir / watch.HISTORY_DIR_NAME / "2026-03-07.md").exists())

    def test_parse_x_posts(self) -> None:
        markdown = """
Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 01:03:09 GMT

Markdown Content:
[![Image 1: Square profile picture](https://example.com/profile.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor's posts
--------------

Pinned

[![Image 2: Square profile picture](https://example.com/pinned.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 3: Square profile picture](https://example.com/post2.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

2h

![Image 4](https://example.com/video.jpg)
"""
        posts = watch.parse_x_posts(markdown)
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.",
            ],
        )

    def test_extract_page_title_prefers_clean_title(self) -> None:
        html = """
<!doctype html>
<html>
  <head>
    <meta property="og:title" content="Build agents that run automatically · Cursor" />
    <title>Ignored fallback | Cursor - The AI Code Editor</title>
  </head>
</html>
"""
        self.assertEqual(watch.extract_page_title(html), "Build agents that run automatically")

    def _run_main(self, *, output_dir: Path, now: str, force: bool) -> None:
        changelog_items = [
            watch.UpdateItem(
                source="changelog",
                title="Automations",
                url="https://cursor.com/changelog/03-05-26",
                date="2026-03-05",
            ),
            watch.UpdateItem(
                source="changelog",
                title="Cursor in JetBrains IDEs",
                url="https://cursor.com/changelog/03-04-26",
                date="2026-03-04",
            ),
        ]
        blog_items = [
            watch.UpdateItem(
                source="blog",
                title="Build agents that run automatically",
                url="https://cursor.com/blog/automations",
                date="2026-03-05",
            )
        ]
        x_snapshot = {
            "profile_url": "https://x.com/cursor_ai",
            "mirror_url": "https://r.jina.ai/http://x.com/cursor_ai",
            "published_time": "Sat, 07 Mar 2026 01:03:09 GMT",
            "posts": [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor!",
            ],
        }

        argv = ["--output-dir", str(output_dir), "--now", now]
        if force:
            argv.append("--force")

        with mock.patch.object(watch, "fetch_cursor_entries", side_effect=[changelog_items, blog_items]):
            with mock.patch.object(watch, "fetch_x_snapshot", return_value=x_snapshot):
                exit_code = watch.main(argv)

        self.assertEqual(exit_code, 0)


if __name__ == "__main__":
    unittest.main()
