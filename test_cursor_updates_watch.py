import datetime as dt
import unittest

from cursor_updates_watch import (
    TIMEZONE,
    extract_x_posts,
    parse_blog,
    parse_changelog,
    should_run,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_only_at_nine_am_shanghai(self) -> None:
        self.assertTrue(should_run(dt.datetime(2026, 3, 6, 9, 0, tzinfo=TIMEZONE)))
        self.assertFalse(should_run(dt.datetime(2026, 3, 6, 8, 59, tzinfo=TIMEZONE)))
        self.assertFalse(should_run(dt.datetime(2026, 3, 6, 10, 0, tzinfo=TIMEZONE)))

    def test_should_not_rerun_same_local_date(self) -> None:
        current_time = dt.datetime(2026, 3, 6, 9, 0, tzinfo=TIMEZONE)
        previous_state = {"checked_at_local": "2026-03-06T09:00:00+08:00"}
        self.assertFalse(should_run(current_time, previous_state))

    def test_parse_changelog_extracts_latest_entries(self) -> None:
        html = """
        <article>
          <a class="hover:text-theme-text inline-flex items-center" href="/changelog/03-05-26">
            <time dateTime="2026-03-05T00:00:00.000Z" class="type-base">Mar 5, 2026</time>
          </a>
          <h1><a href="/changelog/03-05-26">Automations</a></h1>
          <div class="prose prose--block"><p>Build always-on agents.</p></div>
        </article>
        <article>
          <a class="hover:text-theme-text inline-flex items-center" href="/changelog/03-04-26">
            <time dateTime="2026-03-04T00:00:00.000Z" class="type-base">Mar 4, 2026</time>
          </a>
          <h1><a href="/changelog/03-04-26">JetBrains IDEs</a></h1>
          <div class="prose prose--block"><p>Use Cursor in JetBrains.</p></div>
        </article>
        """
        items = parse_changelog(html, max_items=5)

        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].date, "2026-03-05")
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertEqual(items[0].summary, "Build always-on agents.")

    def test_parse_blog_extracts_cards(self) -> None:
        html = """
        <article class="flex grow-1 flex-col mb-g1">
          <a class="card card--text grow-1 grid-cursor-v1" href="/blog/automations">
            <p class="type-base text-theme-text text-pretty">Build agents that run automatically</p>
            <p class="type-base text-theme-text-sec text-pretty">Run on triggers and instructions.</p>
            <time dateTime="2026-03-05T12:00:00.000Z" class="type-base">Mar 5, 2026</time>
          </a>
        </article>
        """
        items = parse_blog(html, max_items=5)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].date, "2026-03-05")
        self.assertEqual(items[0].title, "Build agents that run automatically")
        self.assertEqual(items[0].summary, "Run on triggers and instructions.")
        self.assertEqual(items[0].url, "https://cursor.com/blog/automations")

    def test_extract_x_posts_deduplicates_visible_posts(self) -> None:
        markdown = """
        Title: Cursor (@cursor_ai) / X

        Markdown Content:
        Cursor’s posts
        --------------

        Pinned

        [![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/x.jpg)](https://x.com/cursor_ai)

        We're introducing Cursor Automations to build always-on agents.

        1:34

        [![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/x.jpg)](https://x.com/cursor_ai)

        GPT 5.4 is now available in Cursor!

        [![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/x.jpg)](https://x.com/cursor_ai)

        We're introducing Cursor Automations to build always-on agents.
        """
        posts = extract_x_posts(markdown, max_items=5)

        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor!",
            ],
        )


if __name__ == "__main__":
    unittest.main()
