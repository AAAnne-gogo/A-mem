import datetime as dt
import tempfile
import unittest
from pathlib import Path

import cursor_updates_watch as watch


CHANGELOG_SAMPLE = """
<main>
  <article>
    <div>
      <a href="/changelog/03-05-26"><time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time></a>
    </div>
    <header><h1><a href="/changelog/03-05-26">Automations</a></h1></header>
    <div class="prose prose--block"><p>Build always-on agents that run on schedules and triggers.</p></div>
  </article>
  <article>
    <div>
      <a href="/changelog/03-04-26"><time dateTime="2026-03-04T00:00:00.000Z">Mar 4, 2026</time></a>
    </div>
    <header><h1><a href="/changelog/03-04-26">Cursor in JetBrains IDEs</a></h1></header>
    <div class="prose prose--block"><p>Use Cursor through ACP in JetBrains editors.</p></div>
  </article>
</main>
"""

BLOG_SITEMAP_SAMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://cursor.com/blog/automations</loc></url>
  <url><loc>https://cursor.com/cn/blog/automations</loc></url>
  <url><loc>https://cursor.com/blog/jetbrains-acp</loc></url>
</urlset>
"""

BLOG_ARTICLE_SAMPLE = """
<html>
  <head><title>Build agents that run automatically · Cursor</title></head>
  <body>
    <script type="application/ld+json">
      {"@context":"https://schema.org","@type":"BlogPosting","headline":"Build agents that run automatically","description":"Cursor now supports automations that run based on triggers and instructions you define.","datePublished":"2026-03-05T12:00:00.000Z"}
    </script>
  </body>
</html>
"""

X_MARKDOWN_SAMPLE = """
Title: Cursor (@cursor_ai) / X

Markdown Content:
Cursor’s posts
--------------

Pinned

[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Learn more:
"""


class ParseTests(unittest.TestCase):
    def test_parse_changelog(self) -> None:
        items = watch.parse_changelog(CHANGELOG_SAMPLE, limit=5)
        self.assertEqual([item.title for item in items], ["Automations", "Cursor in JetBrains IDEs"])
        self.assertEqual(items[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertEqual(items[0].summary, "Build always-on agents that run on schedules and triggers.")

    def test_parse_changelog_dedupes_repeated_entries(self) -> None:
        items = watch.parse_changelog(CHANGELOG_SAMPLE + CHANGELOG_SAMPLE, limit=5)
        self.assertEqual([item.title for item in items], ["Automations", "Cursor in JetBrains IDEs"])

    def test_parse_blog_sitemap_filters_non_blog_paths(self) -> None:
        urls = watch.parse_blog_sitemap(BLOG_SITEMAP_SAMPLE)
        self.assertEqual(
            urls,
            [
                "https://cursor.com/blog/automations",
                "https://cursor.com/blog/jetbrains-acp",
            ],
        )

    def test_parse_blog_article_from_ld_json(self) -> None:
        item = watch.parse_blog_article(BLOG_ARTICLE_SAMPLE, "https://cursor.com/blog/automations")
        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.published_at, "2026-03-05T12:00:00.000Z")
        self.assertIn("automations", item.summary)

    def test_parse_x_posts_dedupes_and_ignores_noise(self) -> None:
        items = watch.parse_x_posts(X_MARKDOWN_SAMPLE, limit=5)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "We're introducing Cursor Automations to build always-on agents.")
        self.assertIn("GPT 5.4 is now available in Cursor!", items[1].title)


class ScheduleTests(unittest.TestCase):
    def test_should_run_only_during_target_hour(self) -> None:
        state = {"last_success_local_date": None, "seen": {}}
        local_now = dt.datetime(2026, 3, 8, 8, 59, tzinfo=watch.TARGET_TZ)
        should_execute, reason = watch.should_run(local_now, state, force=False)
        self.assertFalse(should_execute)
        self.assertIn("09:00", reason)

    def test_should_skip_second_scheduled_run_same_day(self) -> None:
        state = {"last_success_local_date": "2026-03-08", "seen": {}}
        local_now = dt.datetime(2026, 3, 8, 9, 15, tzinfo=watch.TARGET_TZ)
        should_execute, reason = watch.should_run(local_now, state, force=False)
        self.assertFalse(should_execute)
        self.assertIn("already completed", reason)

    def test_force_bypasses_schedule_gate(self) -> None:
        state = {"last_success_local_date": "2026-03-08", "seen": {}}
        local_now = dt.datetime(2026, 3, 8, 3, 0, tzinfo=watch.TARGET_TZ)
        should_execute, reason = watch.should_run(local_now, state, force=True)
        self.assertTrue(should_execute)
        self.assertEqual(reason, "forced run")


class StateAndRunTests(unittest.TestCase):
    def test_update_state_appends_recent_item_ids(self) -> None:
        state = {"last_success_local_date": None, "seen": {"changelog": ["old"], "blog": [], "x": []}}
        items = {
            "changelog": [
                watch.UpdateItem("changelog", "Automations", "https://cursor.com/changelog/03-05-26", "2026-03-05T00:00:00.000Z", None, "new-1")
            ],
            "blog": [],
            "x": [],
        }
        next_state = watch.update_state_with_items(
            state,
            items,
            dt.datetime(2026, 3, 8, 9, 0, tzinfo=watch.TARGET_TZ),
        )
        self.assertEqual(next_state["last_success_local_date"], "2026-03-08")
        self.assertEqual(next_state["seen"]["changelog"], ["old", "new-1"])

    def test_forced_run_writes_report_without_state_update(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = watch.default_paths(root)
            original_collect_updates = watch.collect_updates
            try:
                sample_items = {
                    "changelog": [
                        watch.UpdateItem(
                            "changelog",
                            "Automations",
                            "https://cursor.com/changelog/03-05-26",
                            "2026-03-05T00:00:00.000Z",
                            "Build always-on agents.",
                            "c1",
                        )
                    ],
                    "blog": [],
                    "x": [],
                }

                def fake_collect_updates(limit: int = 5):
                    self.assertEqual(limit, 5)
                    return sample_items, []

                watch.collect_updates = fake_collect_updates
                result = watch.run(
                    force=True,
                    now=dt.datetime(2026, 3, 8, 0, 5, tzinfo=dt.timezone.utc),
                    paths=paths,
                )
            finally:
                watch.collect_updates = original_collect_updates

            self.assertEqual(result.status, "success")
            self.assertTrue(paths.latest_report_path.exists())
            self.assertFalse(paths.state_path.exists())
            self.assertIn("Automations", paths.public_report_path.read_text(encoding="utf-8"))

    def test_skipped_run_preserves_existing_success_report(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            paths = watch.default_paths(root)
            paths.state_dir.mkdir(parents=True, exist_ok=True)
            existing_report = "# Previous successful report\n"
            paths.latest_report_path.write_text(existing_report, encoding="utf-8")
            paths.public_report_path.write_text(existing_report, encoding="utf-8")

            result = watch.run(
                force=False,
                now=dt.datetime(2026, 3, 8, 0, 5, tzinfo=dt.timezone.utc),
                paths=paths,
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("only runs during 09:00 hour", result.message)
            self.assertEqual(paths.latest_report_path.read_text(encoding="utf-8"), existing_report)
            self.assertEqual(paths.public_report_path.read_text(encoding="utf-8"), existing_report)


if __name__ == "__main__":
    unittest.main()
