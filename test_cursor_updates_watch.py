import json
import tempfile
import unittest
from pathlib import Path
from zoneinfo import ZoneInfo
import datetime as dt

import cursor_updates_watch as watch


SITEMAP_TEXT = """<?xml version="1.0" encoding="UTF-8"?>
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


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_fetch_official_articles_uses_sitemap_order_and_date_fallback(self) -> None:
        fetch_map = {
            watch.SITEMAP_URL: SITEMAP_TEXT,
            "https://cursor.com/blog/automations": (
                '<html><head><title>Build agents that run automatically · Cursor</title>'
                '<meta property="og:title" content="Build agents that run automatically · Cursor" />'
                '<script>{"datePublished":"2026-03-05T12:00:00.000Z"}</script></head></html>'
            ),
            "https://cursor.com/blog/jetbrains-acp": (
                '<html><head><title>Cursor is now available in JetBrains IDEs · Cursor</title>'
                '<script>{"datePublished":"2026-03-04T12:00:00.000Z"}</script></head></html>'
            ),
            "https://cursor.com/changelog/03-05-26": (
                '<html><head><title>Automations · Cursor</title>'
                '<meta property="og:title" content="Automations · Cursor" /></head></html>'
            ),
            "https://cursor.com/changelog/03-04-26": (
                '<html><head><title>Cursor in JetBrains IDEs · Cursor</title></head></html>'
            ),
        }

        def fake_fetch(url: str) -> str:
            return fetch_map[url]

        blog_items = watch.fetch_official_articles(
            section="blog",
            sitemap_text=SITEMAP_TEXT,
            fetch_text_fn=fake_fetch,
            limit=2,
        )
        changelog_items = watch.fetch_official_articles(
            section="changelog",
            sitemap_text=SITEMAP_TEXT,
            fetch_text_fn=fake_fetch,
            limit=2,
        )

        self.assertEqual(
            [item.title for item in blog_items],
            [
                "Build agents that run automatically",
                "Cursor is now available in JetBrains IDEs",
            ],
        )
        self.assertEqual(blog_items[0].published_at, "2026-03-05T12:00:00.000Z")
        self.assertEqual(
            [item.title for item in changelog_items],
            ["Automations", "Cursor in JetBrains IDEs"],
        )
        self.assertEqual(changelog_items[0].published_at, "2026-03-05T00:00:00.000Z")

    def test_parse_x_posts_strips_images_and_deduplicates(self) -> None:
        sample = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Markdown Content:
[![Image 1: profile](https://example.com/avatar.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------

Pinned

[![Image 2: profile](https://example.com/avatar.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:33

[![Image 3: profile](https://example.com/avatar.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural.

[![Image 4: profile](https://example.com/avatar.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 5](https://example.com/video.jpg)
"""

        posts = watch.parse_x_posts(sample, limit=5)

        self.assertEqual(
            [post.title for post in posts],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural.",
            ],
        )

    def test_run_watch_updates_state_only_for_scheduled_runs(self) -> None:
        fetch_map = {
            watch.SITEMAP_URL: SITEMAP_TEXT,
            "https://cursor.com/blog/automations": (
                '<html><head><title>Build agents that run automatically · Cursor</title>'
                '<script>{"datePublished":"2026-03-05T12:00:00.000Z"}</script></head></html>'
            ),
            "https://cursor.com/blog/jetbrains-acp": (
                '<html><head><title>Cursor is now available in JetBrains IDEs · Cursor</title>'
                '<script>{"datePublished":"2026-03-04T12:00:00.000Z"}</script></head></html>'
            ),
            "https://cursor.com/changelog/03-05-26": '<html><head><title>Automations · Cursor</title></head></html>',
            "https://cursor.com/changelog/03-04-26": '<html><head><title>Cursor in JetBrains IDEs · Cursor</title></head></html>',
            watch.X_MIRROR_URLS[0]: """Title: Cursor (@cursor_ai) / X

Markdown Content:
Cursor’s posts
--------------
[![Image 1](https://example.com/a.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.
""",
        }

        def fake_fetch(url: str) -> str:
            return fetch_map[url]

        now = dt.datetime(2026, 3, 8, 9, 5, tzinfo=ZoneInfo("Asia/Shanghai"))
        with tempfile.TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir)
            first_result = watch.run_watch(
                force=False,
                now=now,
                root_dir=root_dir,
                fetch_text_fn=fake_fetch,
            )
            self.assertEqual(first_result.status, "ran")
            state_path = root_dir / ".cursor_updates" / "state.json"
            first_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(first_state["last_successful_run_local_date"], "2026-03-08")
            self.assertEqual(first_result.new_counts["blog"], 2)

            forced_result = watch.run_watch(
                force=True,
                now=now + dt.timedelta(hours=1),
                root_dir=root_dir,
                fetch_text_fn=fake_fetch,
            )
            self.assertEqual(forced_result.status, "ran")
            second_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(second_state, first_state)

            skipped_result = watch.run_watch(
                force=False,
                now=now + dt.timedelta(minutes=10),
                root_dir=root_dir,
                fetch_text_fn=fake_fetch,
            )
            self.assertEqual(skipped_result.status, "skipped")
            self.assertIn("already completed", skipped_result.message)


if __name__ == "__main__":
    unittest.main()
