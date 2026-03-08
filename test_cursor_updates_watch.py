import unittest
from datetime import datetime, timezone

from cursor_updates_watch import (
    UpdateItem,
    build_report,
    filter_new_items,
    parse_blog_article,
    parse_blog_sitemap,
    parse_changelog_rss,
    parse_x_timeline,
    should_run_now,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_rss(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <guid>https://cursor.com/changelog/03-05-26</guid>
      <description><![CDATA[<p>Always-on agents with triggers.</p>]]></description>
      <pubDate>Thu, 05 Mar 2026 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""
        items = parse_changelog_rss(xml_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertIn("Always-on agents", items[0].summary)

    def test_parse_blog_sitemap_filters_article_pages(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/topic/product</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/blog/page/2</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        entries = parse_blog_sitemap(xml_text)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0][0], "https://cursor.com/blog/automations")

    def test_parse_blog_article_uses_metadata(self) -> None:
        html_text = """
<html>
  <head>
    <meta property="og:title" content="Build agents that run automatically · Cursor"/>
    <meta property="og:description" content="Cursor now supports automations that run based on triggers and instructions you define."/>
  </head>
  <body>
    <time dateTime="2026-03-05T12:00:00.000Z">Mar 5, 2026</time>
  </body>
</html>
"""
        item = parse_blog_article(
            html_text,
            "https://cursor.com/blog/automations",
            datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(item.title, "Build agents that run automatically")
        self.assertIn("automations", item.summary)
        self.assertEqual(item.url, "https://cursor.com/blog/automations")

    def test_parse_x_timeline_extracts_recent_posts(self) -> None:
        html_text = """
<html><body>
<script id="__NEXT_DATA__" type="application/json">
{
  "props": {
    "pageProps": {
      "timeline": {
        "entries": [
          {
            "type": "tweet",
            "content": {
              "tweet": {
                "id_str": "123",
                "created_at": "Thu, 05 Mar 2026 12:00:00 +0000",
                "full_text": "We are shipping automations today https://t.co/demo",
                "permalink": "/cursor_ai/status/123",
                "entities": {
                  "urls": [
                    {
                      "url": "https://t.co/demo",
                      "expanded_url": "https://cursor.com/blog/automations"
                    }
                  ]
                }
              }
            }
          }
        ]
      }
    }
  }
}
</script>
</body></html>
"""
        items = parse_x_timeline(html_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "123")
        self.assertIn("automations", items[0].summary)
        self.assertEqual(items[0].url, "https://x.com/cursor_ai/status/123")

    def test_filter_new_items_bootstrap_rules(self) -> None:
        bootstrap_cutoff = datetime(2026, 3, 1, tzinfo=timezone.utc)
        items = [
            UpdateItem(
                source="x",
                item_id="a",
                title="one",
                summary="one",
                url="https://x.com/cursor_ai/status/a",
                published_at=datetime(2026, 3, 5, tzinfo=timezone.utc),
            ),
            UpdateItem(
                source="x",
                item_id="b",
                title="two",
                summary="two",
                url="https://x.com/cursor_ai/status/b",
                published_at=datetime(2026, 3, 4, tzinfo=timezone.utc),
            ),
        ]
        filtered = filter_new_items(
            items,
            source="x",
            state={},
            bootstrap_cutoff=bootstrap_cutoff,
            bootstrap_x_posts=1,
        )
        self.assertEqual([item.item_id for item in filtered], ["a"])

    def test_should_run_now_honors_gate_and_last_run(self) -> None:
        now_local = datetime(2026, 3, 8, 9, 5, tzinfo=timezone.utc)
        should_run, _ = should_run_now(force=False, state={}, now_local=now_local, run_hour=9)
        self.assertTrue(should_run)

        should_run, _ = should_run_now(
            force=False,
            state={"last_run_date": "2026-03-08"},
            now_local=now_local,
            run_hour=9,
        )
        self.assertFalse(should_run)

    def test_build_report_contains_source_counts(self) -> None:
        generated_at = datetime(2026, 3, 8, 9, 0, tzinfo=timezone.utc)
        report = build_report(
            generated_at=generated_at,
            timezone_name="UTC",
            new_items={
                "changelog": [],
                "blog": [],
                "x": [],
            },
        )
        self.assertIn("Changelog：0 条", report)
        self.assertIn("Blog：0 条", report)
        self.assertIn("官方 X：0 条", report)


if __name__ == "__main__":
    unittest.main()
