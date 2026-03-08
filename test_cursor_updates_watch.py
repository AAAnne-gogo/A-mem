from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime

from cursor_updates_watch import (
    UpdateItem,
    parse_blog_sitemap,
    parse_changelog_rss,
    parse_x_timeline_html,
    render_report,
    select_new_items,
    should_run,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_rss(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Automations</title>
      <link>https://cursor.com/changelog/03-05-26</link>
      <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
      <description><![CDATA[<p>Always-on agents for your repo.</p>]]></description>
    </item>
  </channel>
</rss>
"""
        items = parse_changelog_rss(xml_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].source, "changelog")
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].item_id, "https://cursor.com/changelog/03-05-26")
        self.assertIn("Always-on agents", items[0].summary)

    def test_parse_blog_sitemap_filters_blog_entries(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/pricing</loc>
    <lastmod>2026-03-07T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        items = parse_blog_sitemap(xml_text)
        self.assertEqual(items, [{"url": "https://cursor.com/blog/automations", "lastmod": "2026-03-05T12:00:00.000Z"}])

    def test_parse_x_timeline_html(self) -> None:
        payload = {
            "props": {
                "pageProps": {
                    "timeline": {
                        "entries": [
                            {
                                "type": "tweet",
                                "content": {
                                    "tweet": {
                                        "rest_id": "2028982677358190740",
                                        "legacy": {
                                            "full_text": "RT @mntruell: Retweeted post",
                                            "created_at": "Tue Mar 03 23:55:00 +0000 2026",
                                            "entities": {"urls": []},
                                        },
                                    }
                                },
                            },
                            {
                                "type": "tweet",
                                "content": {
                                    "tweet": {
                                        "rest_id": "2029604182286856663",
                                        "legacy": {
                                            "full_text": "Cursor Automations are here https://t.co/demo",
                                            "created_at": "Thu Mar 05 17:05:19 +0000 2026",
                                            "entities": {
                                                "urls": [
                                                    {
                                                        "url": "https://t.co/demo",
                                                        "expanded_url": "https://cursor.com/blog/automations",
                                                    }
                                                ]
                                            },
                                        },
                                    }
                                },
                            }
                        ]
                    }
                }
            }
        }
        html_text = (
            '<html><body><script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + "</script></body></html>"
        )
        items = parse_x_timeline_html(html_text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item_id, "2029604182286856663")
        self.assertEqual(items[0].url, "https://x.com/cursor_ai/status/2029604182286856663")
        self.assertIn("https://cursor.com/blog/automations", items[0].summary)

    def test_select_new_items_bootstrap_and_seen(self) -> None:
        now = datetime(2026, 3, 8, 21, 0, tzinfo=UTC)
        items = [
            UpdateItem(
                source="x",
                item_id="new",
                title="New",
                url="https://x.com/cursor_ai/status/new",
                published_at="2026-03-08T18:00:00+00:00",
                summary="new",
            ),
            UpdateItem(
                source="x",
                item_id="old",
                title="Old",
                url="https://x.com/cursor_ai/status/old",
                published_at="2026-02-20T18:00:00+00:00",
                summary="old",
            ),
        ]

        bootstrap = select_new_items(items, [], now=now, bootstrap_days=7)
        self.assertEqual([item.item_id for item in bootstrap], ["new"])

        incremental = select_new_items(items, ["new"], now=now, bootstrap_days=7)
        self.assertEqual([item.item_id for item in incremental], ["old"])

    def test_should_run_uses_shanghai_morning_hour(self) -> None:
        state = {"last_run_date": "", "seen": {"changelog": [], "blog": [], "x": []}}

        should_execute, mode = should_run(datetime(2026, 3, 9, 1, 0, tzinfo=UTC), state, force=False)
        self.assertTrue(should_execute)
        self.assertEqual(mode, "scheduled run")

        should_execute, mode = should_run(datetime(2026, 3, 9, 0, 0, tzinfo=UTC), state, force=False)
        self.assertFalse(should_execute)
        self.assertIn("outside 09:00 hour", mode)

        state["last_run_date"] = "2026-03-09"
        should_execute, mode = should_run(datetime(2026, 3, 9, 1, 15, tzinfo=UTC), state, force=False)
        self.assertFalse(should_execute)
        self.assertIn("already ran", mode)

    def test_render_report_contains_all_sections(self) -> None:
        item = UpdateItem(
            source="changelog",
            item_id="1",
            title="Automations",
            url="https://cursor.com/changelog/03-05-26",
            published_at="2026-03-05T00:00:00+00:00",
            summary="Always-on agents.",
        )
        report = render_report(
            checked_at=datetime(2026, 3, 8, 21, 0, tzinfo=UTC),
            mode="forced run",
            changelog_items=[item],
            blog_items=[],
            x_items=[],
        )
        self.assertIn("# Cursor Daily Updates", report)
        self.assertIn("## Changelog", report)
        self.assertIn("## Blog", report)
        self.assertIn("## Official X", report)
        self.assertIn("Automations", report)


if __name__ == "__main__":
    unittest.main()
