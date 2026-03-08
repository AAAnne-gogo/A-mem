import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import cursor_updates_watch as cuw


CHANGELOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>Cursor now supports automations for always-on agents.</description>
    </item>
    <item>
      <title>JetBrains IDEs</title>
      <link>https://cursor.com/changelog/03-04-26</link>
      <pubDate>Wed, 04 Mar 2026 00:00:00 GMT</pubDate>
      <description>Cursor is now available in JetBrains IDEs.</description>
    </item>
  </channel>
</rss>
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
  <url>
    <loc>https://cursor.com/blog/topic/news</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""


AUTOMATIONS_ARTICLE = """Title: Build agents that run automatically

URL Source: http://cursor.com/blog/automations

Published Time: 2026-03-05T12:00:00.000Z

Markdown Content:
We're introducing Cursor Automations to build always-on agents.

These agents run on schedules or event triggers.
"""


JETBRAINS_ARTICLE = """Title: Cursor in JetBrains IDEs

URL Source: http://cursor.com/blog/jetbrains-acp

Published Time: 2026-03-04T12:00:00.000Z

Markdown Content:
Cursor is now available in IntelliJ IDEA, PyCharm, and WebStorm.

### [](http://cursor.com/blog/jetbrains-acp#coding-with-cursor-in-jetbrains-ides)Coding with Cursor in JetBrains IDEs
"""


X_MARKDOWN = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sun, 08 Mar 2026 15:49:19 GMT

Markdown Content:
Cursor

@cursor_ai

Cursor's posts
--------------

Pinned

[![Image 1: Square profile picture](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 2: Square profile picture](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor!

[![Image 3: Square profile picture](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

![Image 4](https://pbs.twimg.com/amplify_video_thumb.jpg)

[![Image 5: Square profile picture](https://pbs.twimg.com/profile.jpg)](https://x.com/cursor_ai)

Cursor can now continuously monitor and improve your codebase.
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_blog_sitemap_filters_non_article_urls(self) -> None:
        entries = cuw.parse_blog_sitemap(BLOG_SITEMAP_XML)
        self.assertEqual(
            entries,
            [
                ("https://cursor.com/blog/automations", datetime(2026, 3, 5, 12, 0, tzinfo=ZoneInfo("UTC"))),
                ("https://cursor.com/blog/jetbrains-acp", datetime(2026, 3, 4, 12, 0, tzinfo=ZoneInfo("UTC"))),
            ],
        )

    def test_parse_jina_article_extracts_title_date_and_summary(self) -> None:
        item = cuw.parse_jina_article(AUTOMATIONS_ARTICLE, "https://cursor.com/blog/automations")
        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.published_at, "2026-03-05")
        self.assertEqual(
            item.summary,
            "We're introducing Cursor Automations to build always-on agents. These agents run on schedules or event triggers.",
        )

    def test_parse_jina_article_stops_before_markdown_heading(self) -> None:
        item = cuw.parse_jina_article(JETBRAINS_ARTICLE, "https://cursor.com/blog/jetbrains-acp")
        self.assertEqual(
            item.summary,
            "Cursor is now available in IntelliJ IDEA, PyCharm, and WebStorm.",
        )

    def test_parse_x_posts_deduplicates_and_filters_noise(self) -> None:
        posts = cuw.parse_x_posts(X_MARKDOWN)
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor!",
                "Cursor can now continuously monitor and improve your codebase.",
            ],
        )

    def test_should_run_blocks_non_target_hour_and_duplicate_date(self) -> None:
        morning = datetime(2026, 3, 9, 9, 15, tzinfo=ZoneInfo("Asia/Shanghai"))
        state = {"last_run_date": "2026-03-09", "seen": {"changelog": [], "blog": [], "x": []}}
        should_run, reason = cuw.should_run(now=morning, state=state, force=False)
        self.assertFalse(should_run)
        self.assertIn("Already checked updates", reason)

        midnight_utc = datetime(2026, 3, 9, 0, 5, tzinfo=ZoneInfo("UTC"))
        state["last_run_date"] = None
        should_run, reason = cuw.should_run(now=midnight_utc, state=state, force=False)
        self.assertFalse(should_run)
        self.assertIn("scheduled hour is 09:00", reason)

    def test_force_run_writes_report_without_persisting_state(self) -> None:
        mapping = {
            cuw.CHANGELOG_RSS_URL: CHANGELOG_XML,
            cuw.BLOG_SITEMAP_URL: BLOG_SITEMAP_XML,
            cuw.to_jina_url("https://cursor.com/blog/automations"): AUTOMATIONS_ARTICLE,
            cuw.to_jina_url("https://cursor.com/blog/jetbrains-acp"): JETBRAINS_ARTICLE,
            cuw.X_MIRROR_URL: X_MARKDOWN,
        }

        def fake_fetcher(url: str) -> str:
            return mapping[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "state.json"
            output_path = temp_path / "cursor_updates.md"
            result = cuw.run(
                now=datetime(2026, 3, 9, 0, 5, tzinfo=ZoneInfo("UTC")),
                state_path=state_path,
                output_path=output_path,
                force=True,
                fetcher=fake_fetcher,
            )

            self.assertTrue(result.executed)
            self.assertEqual(result.counts, {"changelog": 2, "blog": 2, "x": 3})
            self.assertTrue(output_path.exists())
            self.assertFalse(state_path.exists())
            report = output_path.read_text(encoding="utf-8")
            self.assertIn("# Cursor Daily Updates", report)
            self.assertIn("Build agents that run automatically", report)
            self.assertIn("GPT 5.4 is now available in Cursor!", report)

    def test_scheduled_run_persists_state_and_next_run_skips(self) -> None:
        mapping = {
            cuw.CHANGELOG_RSS_URL: CHANGELOG_XML,
            cuw.BLOG_SITEMAP_URL: BLOG_SITEMAP_XML,
            cuw.to_jina_url("https://cursor.com/blog/automations"): AUTOMATIONS_ARTICLE,
            cuw.to_jina_url("https://cursor.com/blog/jetbrains-acp"): JETBRAINS_ARTICLE,
            cuw.X_MIRROR_URL: X_MARKDOWN,
        }

        def fake_fetcher(url: str) -> str:
            return mapping[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            state_path = temp_path / "state.json"
            output_path = temp_path / "cursor_updates.md"
            now = datetime(2026, 3, 9, 1, 0, tzinfo=ZoneInfo("UTC"))
            first = cuw.run(
                now=now,
                state_path=state_path,
                output_path=output_path,
                force=False,
                fetcher=fake_fetcher,
            )

            self.assertTrue(first.executed)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["last_run_date"], "2026-03-09")
            self.assertIn("https://cursor.com/changelog/03-05-26", persisted["seen"]["changelog"])

            second = cuw.run(
                now=now,
                state_path=state_path,
                output_path=output_path,
                force=False,
                fetcher=fake_fetcher,
            )
            self.assertFalse(second.executed)
            self.assertIn("Already checked updates", second.skipped_reason)


if __name__ == "__main__":
    unittest.main()
