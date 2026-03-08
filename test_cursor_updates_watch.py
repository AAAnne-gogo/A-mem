from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

import cursor_updates_watch as watch


CHANGELOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>Always-on agents for recurring workflows.</description>
    </item>
    <item>
      <title>JetBrains ACP</title>
      <link>https://cursor.com/changelog/03-04-26</link>
      <pubDate>Wed, 04 Mar 2026 00:00:00 GMT</pubDate>
      <description>Cursor is now available in JetBrains IDEs.</description>
    </item>
  </channel>
</rss>
"""

BLOG_INDEX_HTML = """
<html>
  <body>
    <article>
      <a href="/blog/automations">
        <p>Build agents that run automatically</p>
        <p>Cursor now supports automations that run based on triggers.</p>
        <time datetime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time>
      </a>
    </article>
    <article>
      <a href="/blog/jetbrains-acp">
        <p>Cursor is now available in JetBrains IDEs</p>
        <p>Use Cursor agents in IntelliJ IDEA and PyCharm.</p>
        <time datetime="2026-03-04T12:00:00.000Z">Mar 4, 2026</time>
      </a>
    </article>
  </body>
</html>
"""

BLOG_SITEMAP_XML = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/jetbrains-acp</loc>
    <lastmod>2026-03-04T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""

X_MARKDOWN = """Title: Cursor (@cursor_ai) / X

Cursor’s posts
--------------
Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 5: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor is now available in JetBrains IDEs through the Agent Client Protocol.
"""


def fake_fetcher(url: str) -> str:
    mapping = {
        watch.CHANGELOG_RSS_URL: CHANGELOG_XML,
        watch.BLOG_INDEX_URL: BLOG_INDEX_HTML,
        watch.BLOG_SITEMAP_URL: BLOG_SITEMAP_XML,
    }
    if url in watch.X_MIRROR_URLS:
        return X_MARKDOWN
    return mapping[url]


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_items(self) -> None:
        items = watch.parse_changelog_items(CHANGELOG_XML)
        self.assertEqual([item.title for item in items], ["Automations", "JetBrains ACP"])
        self.assertEqual(items[0].published_at, "2026-03-05T00:00:00Z")

    def test_parse_blog_index_items(self) -> None:
        items = watch.parse_blog_index_items(BLOG_INDEX_HTML)
        self.assertEqual(
            [item.title for item in items],
            [
                "Build agents that run automatically",
                "Cursor is now available in JetBrains IDEs",
            ],
        )
        self.assertIn("automations", items[0].summary.lower())

    def test_parse_x_items(self) -> None:
        items = watch.parse_x_items(X_MARKDOWN)
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].summary, "We're introducing Cursor Automations to build always-on agents.")
        self.assertIn("GPT 5.4", items[1].summary)

    def test_run_skips_outside_scheduled_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = watch.run(
                now=datetime(2026, 3, 8, 8, 5, tzinfo=UTC),
                base_dir=Path(tmpdir),
                fetcher=fake_fetcher,
            )
            self.assertEqual(result["status"], "skipped")
            self.assertEqual(result["reason"], "outside_scheduled_hour")

    def test_run_bootstrap_writes_state_and_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            result = watch.run(
                now=datetime(2026, 3, 9, 1, 5, tzinfo=UTC),
                base_dir=base_dir,
                fetcher=fake_fetcher,
            )
            self.assertEqual(result["status"], "ok")
            summary_path = base_dir / "cursor_updates.md"
            state_path = base_dir / ".cursor_updates" / "state.json"
            history_path = base_dir / ".cursor_updates" / "history" / "2026-03-09.md"
            self.assertTrue(summary_path.exists())
            self.assertTrue(state_path.exists())
            self.assertTrue(history_path.exists())
            report_text = summary_path.read_text(encoding="utf-8")
            self.assertIn("Build agents that run automatically", report_text)
            self.assertIn("Official X", report_text)
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_success_local_date"], "2026-03-09")
            self.assertIn("https://cursor.com/changelog/03-05-26", state["seen"]["changelog"])

    def test_run_force_does_not_update_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            result = watch.run(
                now=datetime(2026, 3, 8, 8, 5, tzinfo=UTC),
                force=True,
                base_dir=base_dir,
                fetcher=fake_fetcher,
            )
            self.assertEqual(result["status"], "ok")
            self.assertTrue((base_dir / "cursor_updates.md").exists())
            self.assertFalse((base_dir / ".cursor_updates" / "state.json").exists())

    def test_second_scheduled_run_same_day_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base_dir = Path(tmpdir)
            first = watch.run(
                now=datetime(2026, 3, 9, 1, 5, tzinfo=UTC),
                base_dir=base_dir,
                fetcher=fake_fetcher,
            )
            second = watch.run(
                now=datetime(2026, 3, 9, 1, 25, tzinfo=UTC),
                base_dir=base_dir,
                fetcher=fake_fetcher,
            )
            self.assertEqual(first["status"], "ok")
            self.assertEqual(second["status"], "skipped")
            self.assertEqual(second["reason"], "already_ran_today")


if __name__ == "__main__":
    unittest.main()
