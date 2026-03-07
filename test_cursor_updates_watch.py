import datetime as dt
import unittest

from cursor_updates_watch import (
    LOCAL_TIMEZONE,
    ScheduledSkip,
    compute_new_items,
    ensure_scheduled_window,
    parse_blog_article,
    parse_changelog,
    parse_x_posts,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_blog_article_from_json_ld(self) -> None:
        html = """
        <html>
          <head>
            <script type="application/ld+json">
              {"@context":"https://schema.org","@type":"BlogPosting","headline":"Build agents that run automatically","description":"Cursor now supports automations that run based on triggers and instructions you define.","datePublished":"2026-03-05T12:00:00.000Z"}
            </script>
          </head>
        </html>
        """

        item = parse_blog_article(html, "https://cursor.com/blog/automations", fallback_date="2026-03-05")

        self.assertEqual(item.source, "blog")
        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertEqual(item.published, "2026-03-05")
        self.assertIn("automations", item.summary)

    def test_parse_changelog_extracts_recent_items(self) -> None:
        html = """
        <article>
          <time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time>
          <h1><a href="/changelog/03-05-26">Automations</a></h1>
          <div class="prose prose--block"><p>Cursor now supports <a href="#">automations</a> for always-on agents.</p></div>
        </article>
        <article>
          <time dateTime="2026-03-04T00:00:00.000Z">Mar 4, 2026</time>
          <h1><a href="/changelog/03-04-26">Cursor in JetBrains IDEs</a></h1>
          <div class="prose prose--block"><p>Cursor is now available in JetBrains IDEs.</p></div>
        </article>
        """

        items = parse_changelog(html, limit=5)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].published, "2026-03-05")
        self.assertEqual(items[0].summary, "Cursor now supports automations for always-on agents.")
        self.assertEqual(items[1].url, "https://cursor.com/changelog/03-04-26")

    def test_parse_x_posts_deduplicates_and_strips_media(self) -> None:
        markdown = """
        Title: Cursor (@cursor_ai) / X
        Markdown Content:
        Cursor
        @cursor_ai
        Cursor’s posts

        --------------

        Pinned

        [![Image 1](https://pbs.twimg.com/profile_images/1.jpg)](https://x.com/cursor_ai)

        We're introducing Cursor Automations to build always-on agents.

        1:34

        [![Image 2](https://pbs.twimg.com/profile_images/2.jpg)](https://x.com/cursor_ai)

        GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive.

        [![Image 3](https://pbs.twimg.com/profile_images/3.jpg)](https://x.com/cursor_ai)

        We're introducing Cursor Automations to build always-on agents.

        ![Image 5](https://pbs.twimg.com/amplify_video_thumb/1.jpg)
        """

        items = parse_x_posts(markdown, limit=5)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].summary, "We're introducing Cursor Automations to build always-on agents.")
        self.assertIn("GPT 5.4", items[1].summary)

    def test_compute_new_items_filters_seen_ids(self) -> None:
        html = """
        <article>
          <time dateTime="2026-03-05T00:00:00.000Z">Mar 5, 2026</time>
          <h1><a href="/changelog/03-05-26">Automations</a></h1>
          <div class="prose prose--block"><p>Cursor now supports automations.</p></div>
        </article>
        """
        changelog_items = parse_changelog(html, limit=5)
        updates = {"changelog": changelog_items, "blog": [], "x": []}

        new_items = compute_new_items(updates, seen_ids=[changelog_items[0].identifier])

        self.assertEqual(new_items["changelog"], [])

    def test_ensure_scheduled_window_skips_outside_9am(self) -> None:
        state = {"last_successful_scheduled_local_date": ""}
        now = dt.datetime(2026, 3, 7, 0, 30, tzinfo=dt.timezone.utc)

        with self.assertRaises(ScheduledSkip):
            ensure_scheduled_window(state, now=now, force=False)

    def test_ensure_scheduled_window_skips_duplicate_day(self) -> None:
        local_nine_am = dt.datetime(2026, 3, 8, 9, 5, tzinfo=LOCAL_TIMEZONE)
        now = local_nine_am.astimezone(dt.timezone.utc)
        state = {"last_successful_scheduled_local_date": "2026-03-08"}

        with self.assertRaises(ScheduledSkip):
            ensure_scheduled_window(state, now=now, force=False)

    def test_ensure_scheduled_window_allows_force(self) -> None:
        state = {"last_successful_scheduled_local_date": "2026-03-08"}
        now = dt.datetime(2026, 3, 7, 0, 30, tzinfo=dt.timezone.utc)

        local_now = ensure_scheduled_window(state, now=now, force=True)

        self.assertEqual(local_now.tzinfo, LOCAL_TIMEZONE)


if __name__ == "__main__":
    unittest.main()
