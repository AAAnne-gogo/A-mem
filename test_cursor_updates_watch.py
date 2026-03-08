import unittest
from datetime import UTC, datetime, timedelta

from cursor_updates_watch import (
    UpdateItem,
    parse_blog_sitemap,
    parse_changelog_rss,
    parse_x_mirror_posts,
    select_updates,
    should_run_scheduled,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_should_run_only_during_shanghai_nine(self) -> None:
        allowed, reason = should_run_scheduled(
            datetime(2026, 3, 8, 1, 15, tzinfo=UTC),
            last_success_date=None,
        )
        self.assertTrue(allowed)
        self.assertIn("Scheduled window open", reason)

        allowed, reason = should_run_scheduled(
            datetime(2026, 3, 8, 0, 15, tzinfo=UTC),
            last_success_date=None,
        )
        self.assertFalse(allowed)
        self.assertIn("outside the 09:00 hour", reason)

    def test_should_skip_duplicate_local_date(self) -> None:
        allowed, reason = should_run_scheduled(
            datetime(2026, 3, 8, 1, 30, tzinfo=UTC),
            last_success_date="2026-03-08",
        )
        self.assertFalse(allowed)
        self.assertIn("already completed", reason)

    def test_parse_changelog_rss(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Automations</title>
              <link>https://cursor.com/changelog/03-05-26</link>
              <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
              <description>Always-on agents.</description>
            </item>
          </channel>
        </rss>
        """
        items = parse_changelog_rss(xml_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].summary, "Always-on agents.")
        self.assertEqual(items[0].published_at, datetime(2026, 3, 5, 0, 0, tzinfo=UTC))

    def test_select_updates_bootstrap_uses_recent_window(self) -> None:
        now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
        items = [
            UpdateItem(
                source="blog",
                title="Recent",
                url="https://cursor.com/blog/recent",
                published_at=now - timedelta(days=2),
                summary="recent",
            ),
            UpdateItem(
                source="blog",
                title="Too Old",
                url="https://cursor.com/blog/too-old",
                published_at=now - timedelta(days=9),
                summary="old",
            ),
        ]
        selected = select_updates(items, seen_identities=[], now=now, bootstrap_limit=50)
        self.assertEqual([item.title for item in selected], ["Recent"])

    def test_select_updates_uses_seen_state_after_bootstrap(self) -> None:
        now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
        items = [
            UpdateItem(
                source="changelog",
                title="Automations",
                url="https://cursor.com/changelog/03-05-26",
                published_at=now - timedelta(days=1),
                summary="new",
            ),
            UpdateItem(
                source="changelog",
                title="JetBrains",
                url="https://cursor.com/changelog/03-04-26",
                published_at=now - timedelta(days=2),
                summary="new",
            ),
        ]
        selected = select_updates(
            items,
            seen_identities=["https://cursor.com/changelog/03-05-26"],
            now=now,
            bootstrap_limit=50,
        )
        self.assertEqual([item.title for item in selected], ["JetBrains"])

    def test_select_updates_bootstrap_falls_back_to_undated_items(self) -> None:
        now = datetime(2026, 3, 8, 6, 0, tzinfo=UTC)
        items = [
            UpdateItem(
                source="blog",
                title="Fallback One",
                url="https://cursor.com/blog/fallback-one",
                published_at=None,
                summary="fallback",
            ),
            UpdateItem(
                source="blog",
                title="Fallback Two",
                url="https://cursor.com/blog/fallback-two",
                published_at=None,
                summary="fallback",
            ),
        ]
        selected = select_updates(items, seen_identities=[], now=now, bootstrap_limit=1)
        self.assertEqual([item.title for item in selected], ["Fallback One"])

    def test_parse_blog_sitemap_filters_and_sorts_recent_posts(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
          <url>
            <loc>https://cursor.com/blog/older-post</loc>
            <lastmod>2026-03-01T00:00:00Z</lastmod>
          </url>
          <url>
            <loc>https://cursor.com/pricing</loc>
            <lastmod>2026-03-08T00:00:00Z</lastmod>
          </url>
          <url>
            <loc>https://cursor.com/blog/newer-post</loc>
            <lastmod>2026-03-08T00:00:00Z</lastmod>
          </url>
        </urlset>
        """
        urls = parse_blog_sitemap(xml_text)
        self.assertEqual(
            urls,
            [
                ("https://cursor.com/blog/newer-post", datetime(2026, 3, 8, 0, 0, tzinfo=UTC)),
                ("https://cursor.com/blog/older-post", datetime(2026, 3, 1, 0, 0, tzinfo=UTC)),
            ],
        )

    def test_parse_x_mirror_posts_filters_noise_and_dedupes(self) -> None:
        text = """
Title: Cursor (@cursor_ai) / X

Cursor’s posts
--------------
Pinned
[![Image 2: Square profile picture](https://pbs.twimg.com/foo.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
[![Image 3: Square profile picture](https://pbs.twimg.com/bar.jpg)](https://x.com/cursor_ai)
GPT 5.4 is now available in Cursor!
[![Image 4: Square profile picture](https://pbs.twimg.com/baz.jpg)](https://x.com/cursor_ai)
We're introducing Cursor Automations to build always-on agents.
![Image 5](https://pbs.twimg.com/media.jpg)
"""
        posts = parse_x_mirror_posts(text)
        self.assertEqual(
            [post.title for post in posts],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor!",
            ],
        )


if __name__ == "__main__":
    unittest.main()
