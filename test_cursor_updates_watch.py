from __future__ import annotations

import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from cursor_updates_watch import (
    FeedEntry,
    XSnapshot,
    build_state_payload,
    clean_page_title,
    evaluate_run_gate,
    extract_html_title,
    extract_x_posts,
    parse_sitemap_entries,
)


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_sitemap_entries_filters_and_sorts_supported_urls(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://cursor.com/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/changelog/03-05-26</loc>
    <lastmod>2026-03-05T15:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/cn/blog/automations</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
  <url>
    <loc>https://cursor.com/pricing</loc>
    <lastmod>2026-03-05T12:00:00.000Z</lastmod>
  </url>
</urlset>
"""
        entries = parse_sitemap_entries(xml_text)

        self.assertEqual([entry.kind for entry in entries], ["changelog", "blog"])
        self.assertEqual(entries[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertEqual(entries[0].published_date, "2026-03-05")

    def test_extract_html_title_prefers_clean_cursor_title(self) -> None:
        html = '<html><head><meta property="og:title" content="Build agents that run automatically · Cursor" /></head></html>'
        self.assertEqual(extract_html_title(html, "automations"), "Build agents that run automatically")
        self.assertEqual(clean_page_title("Automations | Cursor", "03-05-26"), "Automations")

    def test_extract_x_posts_returns_deduplicated_visible_posts(self) -> None:
        mirror_text = """Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Fri, 06 Mar 2026 20:03:24 GMT

Markdown Content:
[![Image 1: Square profile picture and Opens profile photo](https://pbs.twimg.com/profile_images/foo.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor’s posts
--------------
Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/foo.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:27

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/foo.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models. It's currently the leader on our internal benchmarks.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/foo.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.
"""
        published_time, posts = extract_x_posts(mirror_text, limit=5)

        self.assertEqual(published_time, "Fri, 06 Mar 2026 20:03:24 GMT")
        self.assertEqual(
            posts,
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models. It's currently the leader on our internal benchmarks.",
            ],
        )

    def test_evaluate_run_gate_respects_timezone_window_and_duplicate_guard(self) -> None:
        now_utc = datetime(2026, 3, 6, 1, 0, tzinfo=timezone.utc)

        should_run, message, local_now = evaluate_run_gate(now_utc, "Asia/Shanghai", None, force=False)
        self.assertTrue(should_run)
        self.assertEqual(local_now.hour, 9)
        self.assertEqual(message, "Scheduled run window matched.")

        should_run, message, _ = evaluate_run_gate(now_utc, "Asia/Shanghai", "2026-03-06", force=False)
        self.assertFalse(should_run)
        self.assertIn("already exists", message)

        should_run, message, _ = evaluate_run_gate(now_utc, "Asia/Shanghai", "2026-03-06", force=True)
        self.assertTrue(should_run)
        self.assertIn("--force", message)

    def test_force_run_does_not_consume_scheduled_daily_slot(self) -> None:
        now_utc = datetime(2026, 3, 6, 20, 0, tzinfo=timezone.utc)
        local_now = now_utc.astimezone(ZoneInfo("Asia/Shanghai"))
        entry = FeedEntry(
            kind="blog",
            title="Build agents that run automatically",
            url="https://cursor.com/blog/automations",
            slug="automations",
            lastmod="2026-03-05T12:00:00.000Z",
            published_date="2026-03-05",
        )
        x_snapshot = XSnapshot(
            account_name="Cursor",
            account_handle="@cursor_ai",
            profile_url="https://x.com/cursor_ai",
            mirror_url="https://r.jina.ai/http://x.com/cursor_ai",
            published_time="Fri, 06 Mar 2026 20:03:24 GMT",
            posts=["We're introducing Cursor Automations to build always-on agents."],
        )

        forced_state = build_state_payload(
            previous_state={"last_success_local_date": "2026-03-06"},
            now_utc=now_utc,
            local_now=local_now,
            timezone_name="Asia/Shanghai",
            changelog_entries=[entry],
            blog_entries=[entry],
            x_snapshot=x_snapshot,
            force=True,
        )
        scheduled_state = build_state_payload(
            previous_state={},
            now_utc=now_utc,
            local_now=local_now,
            timezone_name="Asia/Shanghai",
            changelog_entries=[entry],
            blog_entries=[entry],
            x_snapshot=x_snapshot,
            force=False,
        )

        self.assertEqual(forced_state["last_success_local_date"], "2026-03-06")
        self.assertEqual(forced_state["last_run_mode"], "force")
        self.assertEqual(scheduled_state["last_success_local_date"], "2026-03-07")
        self.assertEqual(scheduled_state["last_run_mode"], "scheduled")


if __name__ == "__main__":
    unittest.main()
