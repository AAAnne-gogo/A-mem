import unittest
from datetime import datetime, timezone
from unittest import mock

import cursor_updates_watch as watch


SAMPLE_CHANGELOG_XML = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description>Always-on agents triggered by schedules.</description>
    </item>
  </channel>
</rss>
"""


SAMPLE_BLOG_HTML = """
<article>
  <a class="card" href="/blog/automations">
    <div>
      <p>Build agents that run automatically</p>
      <p>Cursor now supports automations that run based on triggers and instructions you define.</p>
    </div>
    <div>
      <span class="capitalize">product<!-- -->&nbsp;<!-- -->·<!-- -->&nbsp;</span>
      <time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time>
    </div>
  </a>
</article>
"""


SAMPLE_BLOG_SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""


SAMPLE_X_MARKDOWN = """
Title: Cursor (@cursor_ai) / X

Markdown Content:
Cursor’s posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
1:33

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)
GPT 5.4 is now available in Cursor!

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_only_during_nine_am_shanghai(self) -> None:
        allowed_now = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
        skipped_now = datetime(2026, 3, 8, 2, 0, tzinfo=timezone.utc)

        allowed, allowed_reason = watch.should_run_scheduled(allowed_now, watch.DEFAULT_STATE)
        skipped, skipped_reason = watch.should_run_scheduled(skipped_now, watch.DEFAULT_STATE)

        self.assertTrue(allowed)
        self.assertIn("running scheduled check", allowed_reason)
        self.assertFalse(skipped)
        self.assertIn("outside the 09:00", skipped_reason)

    def test_should_skip_when_same_local_day_already_completed(self) -> None:
        now = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
        state = {
            "last_successful_local_date": "2026-03-08",
            "seen": {"changelog": [], "blog": [], "x": []},
        }

        should_run, reason = watch.should_run_scheduled(now, state)

        self.assertFalse(should_run)
        self.assertIn("already completed", reason)

    def test_parse_changelog_rss_extracts_expected_fields(self) -> None:
        items = watch.parse_changelog_rss(SAMPLE_CHANGELOG_XML)

        self.assertEqual(1, len(items))
        self.assertEqual("Automations", items[0].title)
        self.assertEqual("https://cursor.com/changelog/03-05-26", items[0].url)
        self.assertEqual("Always-on agents triggered by schedules.", items[0].summary)

    def test_merge_blog_items_uses_index_title_and_sitemap_date(self) -> None:
        index_items = watch.parse_blog_index(SAMPLE_BLOG_HTML)
        sitemap_items = watch.parse_blog_sitemap(SAMPLE_BLOG_SITEMAP)
        merged = watch.merge_blog_items(index_items, sitemap_items)

        self.assertEqual(1, len(merged))
        self.assertEqual("Build agents that run automatically", merged[0].title)
        self.assertEqual("product", merged[0].topic)
        self.assertEqual(
            "Cursor now supports automations that run based on triggers and instructions you define.",
            merged[0].summary,
        )
        self.assertEqual("2026-03-05T12:00:00+00:00", merged[0].published_at)

    def test_parse_x_posts_removes_video_duration_and_duplicate_pinned_text(self) -> None:
        items = watch.parse_x_posts(SAMPLE_X_MARKDOWN)

        self.assertEqual(2, len(items))
        self.assertEqual("We're introducing Cursor Automations to build always-on agents.", items[0].title)
        self.assertEqual("GPT 5.4 is now available in Cursor!", items[1].title)

    def test_force_run_generates_snapshot_without_saving_state(self) -> None:
        responses = {
            watch.CHANGELOG_RSS_URL: SAMPLE_CHANGELOG_XML,
            watch.BLOG_INDEX_URL: SAMPLE_BLOG_HTML,
            watch.BLOG_SITEMAP_URL: SAMPLE_BLOG_SITEMAP,
            watch.X_MIRROR_URLS[0]: SAMPLE_X_MARKDOWN,
        }

        def fake_fetch(url: str) -> str:
            return responses[url]

        now = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
        with mock.patch.object(watch, "load_state", return_value=watch.DEFAULT_STATE), mock.patch.object(
            watch, "write_report"
        ) as write_report, mock.patch.object(watch, "save_state") as save_state:
            exit_code, report = watch.run_watch(force=True, now=now, fetch_text=fake_fetch)

        self.assertEqual(0, exit_code)
        self.assertIn("# Cursor updates", report)
        write_report.assert_called_once()
        save_state.assert_not_called()


if __name__ == "__main__":
    unittest.main()
