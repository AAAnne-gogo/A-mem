import unittest
from datetime import datetime, timezone

from cursor_updates_watch import (
    parse_blog_html,
    parse_blog_sitemap,
    parse_changelog_html,
    parse_x_markdown,
    should_run_now,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_at_target_hour(self) -> None:
        state = {
            "last_successful_local_date": None,
            "last_successful_run_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
        now_utc = datetime(2026, 3, 8, 1, 5, tzinfo=timezone.utc)
        decision = should_run_now(state, now_utc=now_utc)
        self.assertTrue(decision.should_run)
        self.assertEqual(decision.local_now.strftime("%Y-%m-%d %H:%M"), "2026-03-08 09:05")

    def test_should_skip_outside_target_hour(self) -> None:
        state = {
            "last_successful_local_date": None,
            "last_successful_run_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
        now_utc = datetime(2026, 3, 8, 2, 0, tzinfo=timezone.utc)
        decision = should_run_now(state, now_utc=now_utc)
        self.assertFalse(decision.should_run)
        self.assertIn("waiting for 09:00", decision.reason)

    def test_should_skip_if_already_ran_today(self) -> None:
        state = {
            "last_successful_local_date": "2026-03-08",
            "last_successful_run_at": "2026-03-08T01:00:00+00:00",
            "seen": {"changelog": [], "blog": [], "x": []},
        }
        now_utc = datetime(2026, 3, 8, 1, 30, tzinfo=timezone.utc)
        decision = should_run_now(state, now_utc=now_utc)
        self.assertFalse(decision.should_run)
        self.assertIn("Already ran", decision.reason)

    def test_parse_changelog_html(self) -> None:
        html = """
        <article>
          <div>
            <p><a href="/changelog/03-05-26"><time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time></a></p>
            <header><h1><a href="/changelog/03-05-26">Automations</a></h1></header>
            <div class="prose prose--block">
              <p>Cursor now supports automations for always-on agents.</p>
              <p>Automations run on schedules or events from Slack and GitHub.</p>
            </div>
          </div>
        </article>
        """
        items = parse_changelog_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertIn("always-on agents", items[0].summary)

    def test_parse_blog_html(self) -> None:
        html = """
        <article class="flex grow-1 flex-col mb-g1">
          <a href="/blog/automations">
            <div>
              <p>Build agents that run automatically</p>
              <p>Cursor now supports automations that run based on triggers.</p>
              <div><span>product<!-- -->&nbsp;<!-- -->·<!-- -->&nbsp;</span>
              <time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time></div>
            </div>
          </a>
        </article>
        """
        items = parse_blog_html(html)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Build agents that run automatically")
        self.assertEqual(items[0].url, "https://cursor.com/blog/automations")
        self.assertIn("product", items[0].summary)

    def test_parse_blog_sitemap(self) -> None:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://cursor.com/blog/automations</loc>
            <lastmod>2026-03-05T12:00:00.000Z</lastmod>
          </url>
          <url>
            <loc>https://cursor.com/changelog/03-05-26</loc>
            <lastmod>2026-03-05T00:00:00.000Z</lastmod>
          </url>
        </urlset>
        """
        sitemap = parse_blog_sitemap(xml)
        self.assertEqual(
            sitemap,
            {"https://cursor.com/blog/automations": "2026-03-05T12:00:00.000Z"},
        )

    def test_parse_x_markdown_deduplicates_pinned_post(self) -> None:
        markdown = """
        Title: Cursor (@cursor_ai) / X

        Cursor’s posts
        --------------

        Pinned

        [![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

        We're introducing Cursor Automations to build always-on agents.
        1:33

        [![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

        GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive.

        [![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

        We're introducing Cursor Automations to build always-on agents.
        """
        items = parse_x_markdown(
            markdown_text=markdown,
            fetched_at=datetime(2026, 3, 8, 3, 0, tzinfo=timezone.utc),
            limit=8,
        )
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].title.startswith("[Pinned]"))
        self.assertIn("GPT 5.4 is now available", items[1].summary)


if __name__ == "__main__":
    unittest.main()
