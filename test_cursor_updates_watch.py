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
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-04-26</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com</loc>
    <lastmod>2026-03-07T10:03:48.669Z</lastmod>
  </url>
</urlset>
"""


BLOG_AUTOMATIONS = """Title: Build agents that run automatically

URL Source: http://cursor.com/blog/automations

Published Time: 2026-03-05T12:00:00.000Z

Markdown Content:
We're introducing Cursor Automations to build always-on agents.

These agents run on schedules or are triggered by events like a sent Slack message, a newly created Linear issue, a merged GitHub PR, or a PagerDuty incident.

![Image 1](https://example.com/image.png)

[](http://cursor.com/blog/automations#chores)Chores
---------------------------------------------------
"""


BLOG_JETBRAINS = """Title: Cursor is now available in JetBrains IDEs

URL Source: http://cursor.com/blog/jetbrains-acp

Published Time: 2026-03-04T12:00:00.000Z

Markdown Content:
Cursor is now available in IntelliJ IDEA, PyCharm, WebStorm, and other JetBrains IDEs through the Agent Client Protocol.

Developers who rely on JetBrains for Java and multilanguage support can now use frontier models with Cursor.
"""


CHANGELOG_AUTOMATIONS = """Title: Automations · Cursor

URL Source: http://cursor.com/changelog/03-05-26

Markdown Content:
Automations · Cursor
===============

[Skip to content](http://cursor.com/changelog/03-05-26#main)

[Cursor](http://cursor.com/home)

[Sign in](https://cursor.com/dashboard)[Download](http://cursor.com/download)

Mar 5, 2026 · [Changelog](http://cursor.com/changelog)

[Changelog](http://cursor.com/changelog)

Automations
===========

Cursor now supports automations for building always-on agents that run based on triggers and instructions you define.

Automations run on schedules or are triggered by events from Slack, Linear, GitHub, PagerDuty, and webhooks.

[Next post →Cursor in JetBrains IDEs](http://cursor.com/changelog/03-04-26)

### Product
"""


CHANGELOG_JETBRAINS = """Title: Cursor in JetBrains IDEs · Cursor

URL Source: http://cursor.com/changelog/03-04-26

Markdown Content:
Cursor in JetBrains IDEs · Cursor
===============

Mar 4, 2026 · [Changelog](http://cursor.com/changelog)

Cursor in JetBrains IDEs
========================

Cursor is now available in IntelliJ IDEA, PyCharm, WebStorm, and other JetBrains IDEs through the Agent Client Protocol.
"""


X_TEXT = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 09:22:52 GMT

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

1:34

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 4](https://pbs.twimg.com/amplify_video_thumb/example.jpg)

[![Image 5: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor is now available in JetBrains IDEs through the Agent Client Protocol.

We believe Cursor discovered a novel solution to Problem Six.
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_sitemap_splits_blog_and_changelog(self) -> None:
        result = watch.parse_sitemap(SITEMAP_XML)

        self.assertEqual(
            [entry.url for entry in result["blog"]],
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )
        self.assertEqual(
            [entry.url for entry in result["changelog"]],
            [
                "https://cursor.com/changelog/03-05-26",
                "https://cursor.com/changelog/03-04-26",
            ],
        )

    def test_parse_blog_update_extracts_summary(self) -> None:
        result = watch.parse_blog_update(
            BLOG_AUTOMATIONS,
            "https://cursor.com/blog/automations",
            "2026-03-05T12:00:00.000Z",
        )

        self.assertEqual(result.title, "Build agents that run automatically")
        self.assertEqual(result.published, "2026-03-05T12:00:00.000Z")
        self.assertIn("always-on agents", result.summary)
        self.assertIn("PagerDuty incident", result.summary)
        self.assertNotIn("Image 1", result.summary)

    def test_parse_changelog_update_skips_navigation(self) -> None:
        result = watch.parse_changelog_update(
            CHANGELOG_AUTOMATIONS,
            "https://cursor.com/changelog/03-05-26",
            "2026-03-05T12:00:00.000Z",
        )

        self.assertEqual(result.title, "Automations")
        self.assertEqual(result.published, "Mar 5, 2026")
        self.assertIn("always-on agents", result.summary)
        self.assertIn("PagerDuty", result.summary)
        self.assertNotIn("Sign in", result.summary)

    def test_parse_x_posts_filters_images_duration_and_duplicates(self) -> None:
        result = watch.parse_x_posts(X_TEXT, limit=10)

        self.assertEqual(len(result), 2)
        self.assertEqual(
            result[0], "We're introducing Cursor Automations to build always-on agents."
        )
        self.assertIn("JetBrains IDEs", result[1])
        self.assertNotIn("1:34", result[0])
        self.assertNotIn("Image 4", result[0])

    def test_fetch_x_timeline_falls_back_when_first_mirror_has_no_posts(self) -> None:
        empty_x = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 09:22:52 GMT

Markdown Content:
Cursor
"""
        url_to_body = {
            watch.X_CANDIDATE_URLS[0]: empty_x,
            watch.X_CANDIDATE_URLS[1]: X_TEXT,
        }

        source_url, published_time, posts = watch.fetch_x_timeline(url_to_body.__getitem__)

        self.assertEqual(source_url, watch.X_CANDIDATE_URLS[1])
        self.assertEqual(published_time, "Sat, 07 Mar 2026 09:22:52 GMT")
        self.assertEqual(len(posts), 2)

    def test_should_run_scheduled_respects_hour_duplicate_and_force(self) -> None:
        tz = watch.ZoneInfo("Asia/Shanghai")
        scheduled_now = watch.datetime(2026, 3, 7, 9, 5, tzinfo=tz)
        wrong_hour_now = watch.datetime(2026, 3, 7, 8, 55, tzinfo=tz)

        self.assertEqual(
            watch.should_run_scheduled(scheduled_now, 9, None, False),
            (True, "scheduled run"),
        )
        self.assertEqual(
            watch.should_run_scheduled(scheduled_now, 9, "2026-03-07", False),
            (False, "scheduled digest for 2026-03-07 already ran"),
        )
        self.assertEqual(
            watch.should_run_scheduled(wrong_hour_now, 9, None, False),
            (
                False,
                "outside 09:00 local window (current local time: 2026-03-07 08:55)",
            ),
        )
        self.assertEqual(
            watch.should_run_scheduled(wrong_hour_now, 9, "2026-03-07", True),
            (True, "forced run"),
        )

    def test_force_run_writes_reports_and_preserves_scheduled_guard(self) -> None:
        url_to_body = {
            watch.SITEMAP_URL: SITEMAP_XML,
            f"{watch.JINA_HTTP_PREFIX}cursor.com/blog/automations": BLOG_AUTOMATIONS,
            f"{watch.JINA_HTTP_PREFIX}cursor.com/blog/jetbrains-acp": BLOG_JETBRAINS,
            f"{watch.JINA_HTTP_PREFIX}cursor.com/changelog/03-05-26": CHANGELOG_AUTOMATIONS,
            f"{watch.JINA_HTTP_PREFIX}cursor.com/changelog/03-04-26": CHANGELOG_JETBRAINS,
            watch.X_CANDIDATE_URLS[0]: X_TEXT,
        }

        def fake_fetch(url: str) -> str:
            return url_to_body[url]

        fixed_now = watch.datetime(
            2026, 3, 7, 1, 2, tzinfo=watch.ZoneInfo("UTC")
        ).astimezone(watch.ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as tmp_dir:
            workspace = Path(tmp_dir)
            state_dir = workspace / ".cursor_updates"
            state_dir.mkdir()
            (state_dir / "state.json").write_text(
                json.dumps({"last_scheduled_local_date": "2026-03-06"}) + "\n",
                encoding="utf-8",
            )
            with patch.object(watch, "local_now", return_value=fixed_now):
                result = watch.run(
                    force=True,
                    workspace=workspace,
                    fetch_text=fake_fetch,
                )

            self.assertEqual(result["status"], "fetched")
            state = json.loads((state_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["last_scheduled_local_date"], "2026-03-06")
            self.assertEqual(state["timezone"], "Asia/Shanghai")

            report = (workspace / "cursor_updates.md").read_text(encoding="utf-8")
            latest_report = (state_dir / "latest_report.md").read_text(encoding="utf-8")
            history_report = (
                state_dir / "history" / f"{fixed_now.date().isoformat()}.md"
            ).read_text(encoding="utf-8")

            self.assertEqual(report, latest_report)
            self.assertEqual(report, history_report)
            self.assertIn("Build agents that run automatically", report)
            self.assertIn("We're introducing Cursor Automations", report)


if __name__ == "__main__":
    unittest.main()
