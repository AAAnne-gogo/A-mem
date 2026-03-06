from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
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


def fake_fetcher(url: str) -> str:
    responses = {
        watch.MARKETING_SITEMAP_URL: SITEMAP_XML,
        "https://cursor.com/blog/automations": """
            <html><head>
            <title>Build agents that run automatically · Cursor</title>
            <script type="application/ld+json">{"datePublished":"2026-03-05T12:00:00.000Z"}</script>
            </head></html>
        """,
        "https://cursor.com/blog/jetbrains-acp": """
            <html><head>
            <title>Cursor is now available in JetBrains IDEs · Cursor</title>
            <script type="application/ld+json">{"datePublished":"2026-03-04T12:00:00.000Z"}</script>
            </head></html>
        """,
        "https://cursor.com/changelog/03-05-26": """
            <html><head><title>Automations · Cursor</title></head>
            <body><h1>Automations</h1></body></html>
        """,
        "https://cursor.com/changelog/03-04-26": """
            <html><head><title>Cursor in JetBrains IDEs · Cursor</title></head>
            <body><h1>Cursor in JetBrains IDEs</h1></body></html>
        """,
        watch.X_MIRROR_URL: """Title: Cursor (@cursor_ai) / X

Published Time: Fri, 06 Mar 2026 18:22:07 GMT

Markdown Content:
Cursor’s posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:33

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor can now continuously monitor and improve your codebase.
""",
    }
    return responses[url]


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_sitemap_entries_returns_latest_urls(self) -> None:
        entries = watch.parse_sitemap_entries(SITEMAP_XML, section="blog", limit=2)
        self.assertEqual(
            entries,
            [
                ("2026-03-05T12:00:00.000Z", "https://cursor.com/blog/automations"),
                ("2026-03-04T12:00:00.000Z", "https://cursor.com/blog/jetbrains-acp"),
            ],
        )

    def test_extract_x_posts_skips_images_and_duration(self) -> None:
        posts = watch.extract_x_posts(fake_fetcher(watch.X_MIRROR_URL), limit=3)
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.",
                "Cursor can now continuously monitor and improve your codebase.",
            ],
        )

    def test_run_watch_respects_schedule_and_persists_state(self) -> None:
        with tempfile.TemporaryDirectory() as tempdir:
            state_dir = Path(tempdir)

            skipped = watch.run_watch(
                now=datetime(2026, 3, 6, 0, 0, tzinfo=UTC),
                state_dir=state_dir,
                fetcher=fake_fetcher,
            )
            self.assertEqual(skipped["status"], "skipped")
            self.assertEqual(skipped["reason"], "outside_run_window:08")

            executed = watch.run_watch(
                now=datetime(2026, 3, 6, 1, 0, tzinfo=UTC),
                state_dir=state_dir,
                fetcher=fake_fetcher,
                limit=2,
            )
            self.assertEqual(executed["status"], "success")
            self.assertEqual([entry.title for entry in executed["new_changelog"]], ["Automations", "Cursor in JetBrains IDEs"])
            self.assertEqual(
                [entry.title for entry in executed["new_blog"]],
                ["Build agents that run automatically", "Cursor is now available in JetBrains IDEs"],
            )
            self.assertEqual(
                executed["new_x_posts"],
                [
                    "We're introducing Cursor Automations to build always-on agents.",
                    "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.",
                ],
            )

            duplicate = watch.run_watch(
                now=datetime(2026, 3, 6, 1, 30, tzinfo=UTC),
                state_dir=state_dir,
                fetcher=fake_fetcher,
                limit=2,
            )
            self.assertEqual(duplicate["status"], "skipped")
            self.assertEqual(duplicate["reason"], "already_ran_today")
            self.assertTrue((state_dir / "state.json").exists())
            self.assertTrue((state_dir / "latest_report.md").exists())
            self.assertTrue((state_dir / "history" / "2026-03-06.md").exists())


if __name__ == "__main__":
    unittest.main()
