from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cursor_updates_watch import (
    BLOG_URL,
    CHANGELOG_URL,
    X_TIMELINE_URL,
    compute_new_items,
    parse_blog,
    parse_changelog,
    parse_x_timeline,
    run_watch,
    should_run,
)


SAMPLE_CHANGELOG_HTML = """
<main>
  <article>
    <div class="grid-cursor gap-y-0 pb-v5 mb-v5 border-theme-border-02 border-b">
      <div>
        <a class="hover:text-theme-text inline-flex items-center" href="/changelog/03-05-26">
          <time dateTime="2026-03-05T00:00:00.000Z" class="type-base">Mar 5, 2026</time>
        </a>
      </div>
      <div>
        <header class="mb-v2 relative">
          <h1 class="type-lg text-balance" id="automations">
            <a class="active:text-theme-text hover:opacity-90" href="/changelog/03-05-26">Automations</a>
          </h1>
        </header>
        <div class="prose prose--block">
          <p>Cursor now supports automations for building always-on agents.</p>
          <p>More details below.</p>
        </div>
      </div>
    </div>
    <div class="grid-cursor gap-y-0 pb-v5 mb-v5 border-theme-border-02 border-b">
      <div>
        <a class="hover:text-theme-text inline-flex items-center" href="/changelog/03-04-26">
          <time dateTime="2026-03-04T00:00:00.000Z" class="type-base">Mar 4, 2026</time>
        </a>
      </div>
      <div>
        <header class="mb-v2 relative">
          <h1 class="type-lg text-balance" id="jetbrains">
            <a class="active:text-theme-text hover:opacity-90" href="/changelog/03-04-26">JetBrains IDE Support</a>
          </h1>
        </header>
        <div class="prose prose--block">
          <p>Cursor is now available in JetBrains IDEs.</p>
        </div>
      </div>
    </div>
  </article>
</main>
"""

SAMPLE_BLOG_HTML = """
<main>
  <article class="flex grow-1 flex-col mb-g1">
    <a class="card card--text grow-1 grid-cursor-v1 grid-cols-[1fr_auto] @container" href="/blog/automations">
      <div class="flex flex-col @sm:col-start-1 @sm:col-end-2">
        <div class="grow-1">
          <p class="type-base text-theme-text text-pretty">Build agents that run automatically</p>
          <p class="type-base text-theme-text-sec text-pretty">Cursor now supports automations that run based on triggers.</p>
        </div>
        <div class="mt-v1 text-theme-text-sec flex shrink-0 items-center">
          <span class="capitalize">product<!-- -->&nbsp;<!-- -->·<!-- -->&nbsp;</span>
          <time dateTime="2026-03-05T12:00:00.000Z" class="type-base">Mar 5, 2026</time>
        </div>
      </div>
    </a>
  </article>
  <article class="flex grow-1 flex-col mb-g1">
    <a class="card card--text grow-1 grid-cursor-v1 grid-cols-[1fr_auto] @container" href="/blog/jetbrains-acp">
      <div class="flex flex-col @sm:col-start-1 @sm:col-end-2">
        <div class="grow-1">
          <p class="type-base text-theme-text text-pretty">Cursor is now available in JetBrains IDEs</p>
          <p class="type-base text-theme-text-sec text-pretty">Use Cursor agents in IntelliJ IDEA and more.</p>
        </div>
        <div class="mt-v1 text-theme-text-sec flex shrink-0 items-center">
          <span class="capitalize">product<!-- -->&nbsp;<!-- -->·<!-- -->&nbsp;</span>
          <time dateTime="2026-03-04T12:00:00.000Z" class="type-base">Mar 4, 2026</time>
        </div>
      </div>
    </a>
  </article>
</main>
"""

SAMPLE_X_HTML = """
<!DOCTYPE html>
<html>
  <body>
    <script id="__NEXT_DATA__" type="application/json">{
      "props": {
        "pageProps": {
          "timeline": {
            "entries": [
              {
                "type": "tweet",
                "content": {
                  "tweet": {
                    "id_str": "100",
                    "created_at": "Thu Mar 05 17:05:19 +0000 2026",
                    "full_text": "We're introducing Cursor Automations to build always-on agents. https://t.co/example",
                    "permalink": "/cursor_ai/status/100",
                    "user": {"screen_name": "cursor_ai"}
                  }
                }
              },
              {
                "type": "tweet",
                "content": {
                  "tweet": {
                    "id_str": "101",
                    "created_at": "Thu Mar 05 17:10:19 +0000 2026",
                    "full_text": "RT @someone: external retweet",
                    "permalink": "/cursor_ai/status/101",
                    "retweeted_status": {"id_str": "42"},
                    "user": {"screen_name": "cursor_ai"}
                  }
                }
              },
              {
                "type": "tweet",
                "content": {
                  "tweet": {
                    "id_str": "102",
                    "created_at": "Thu Mar 05 17:20:19 +0000 2026",
                    "full_text": "A thread reply with more details",
                    "permalink": "/cursor_ai/status/102",
                    "in_reply_to_status_id_str": "100",
                    "user": {"screen_name": "cursor_ai"}
                  }
                }
              },
              {
                "type": "tweet",
                "content": {
                  "tweet": {
                    "id_str": "103",
                    "created_at": "Thu Mar 05 17:30:19 +0000 2026",
                    "full_text": "Cursor is now available in JetBrains IDEs.",
                    "permalink": "/cursor_ai/status/103",
                    "user": {"screen_name": "cursor_ai"}
                  }
                }
              }
            ]
          }
        }
      }
    }</script>
  </body>
</html>
"""


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_extracts_latest_entries(self) -> None:
        items = parse_changelog(SAMPLE_CHANGELOG_HTML, max_items=2)
        self.assertEqual([item.item_id for item in items], ["03-05-26", "03-04-26"])
        self.assertEqual(items[0].title, "Automations")
        self.assertIn("always-on agents", items[0].summary)

    def test_parse_blog_extracts_title_summary_and_topic(self) -> None:
        items = parse_blog(SAMPLE_BLOG_HTML, max_items=2)
        self.assertEqual([item.item_id for item in items], ["automations", "jetbrains-acp"])
        self.assertEqual(items[0].title, "Build agents that run automatically")
        self.assertTrue(items[0].summary.startswith("[product]"))

    def test_parse_x_timeline_filters_retweets_and_replies(self) -> None:
        items = parse_x_timeline(SAMPLE_X_HTML, max_items=5)
        self.assertEqual([item.item_id for item in items], ["100", "103"])
        self.assertNotIn("https://t.co", items[0].title)
        self.assertEqual(items[0].url, "https://x.com/cursor_ai/status/100")

    def test_should_run_respects_run_hour_and_same_day_guard(self) -> None:
        state = {"last_run_local_date": None, "seen_ids": {}}
        early = datetime(2026, 3, 6, 0, 30, tzinfo=timezone.utc)
        due = datetime(2026, 3, 6, 2, 0, tzinfo=timezone.utc)

        should_execute, _, local_now = should_run(
            now=early,
            timezone_name="Asia/Shanghai",
            run_hour=9,
            state=state,
        )
        self.assertFalse(should_execute)
        self.assertEqual(local_now.hour, 8)

        should_execute, _, local_now = should_run(
            now=due,
            timezone_name="Asia/Shanghai",
            run_hour=9,
            state=state,
        )
        self.assertTrue(should_execute)
        self.assertEqual(local_now.hour, 10)

        repeated_state = {"last_run_local_date": "2026-03-06", "seen_ids": {}}
        should_execute, _, _ = should_run(
            now=due,
            timezone_name="Asia/Shanghai",
            run_hour=9,
            state=repeated_state,
        )
        self.assertFalse(should_execute)

    def test_compute_new_items_uses_seen_ids(self) -> None:
        items = {
            "changelog": parse_changelog(SAMPLE_CHANGELOG_HTML, max_items=2),
            "blog": parse_blog(SAMPLE_BLOG_HTML, max_items=2),
            "x": parse_x_timeline(SAMPLE_X_HTML, max_items=5),
        }
        state = {
            "last_run_local_date": "2026-03-05",
            "seen_ids": {
                "changelog": ["03-04-26"],
                "blog": ["automations"],
                "x": ["103"],
            },
        }
        new_items = compute_new_items(items, state)
        self.assertEqual([item.item_id for item in new_items["changelog"]], ["03-05-26"])
        self.assertEqual([item.item_id for item in new_items["blog"]], ["jetbrains-acp"])
        self.assertEqual([item.item_id for item in new_items["x"]], ["100"])

    def test_run_watch_writes_report_and_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            state_path = tmp_path / "state" / "state.json"
            report_path = tmp_path / "cursor_updates.md"
            fixtures = {
                CHANGELOG_URL: SAMPLE_CHANGELOG_HTML,
                BLOG_URL: SAMPLE_BLOG_HTML,
                X_TIMELINE_URL: SAMPLE_X_HTML,
            }

            def fake_fetcher(url: str) -> str:
                return fixtures[url]

            now = datetime(2026, 3, 6, 2, 0, tzinfo=timezone.utc)
            result = run_watch(
                now=now,
                force=False,
                state_path=state_path,
                report_path=report_path,
                fetcher=fake_fetcher,
            )

            self.assertEqual(result.status, "updated")
            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Initial baseline created", report_text)
            self.assertIn("Build agents that run automatically", report_text)
            self.assertIn("Cursor is now available in JetBrains IDEs.", report_text)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["last_run_local_date"], "2026-03-06")
            self.assertEqual(state["seen_ids"]["x"], ["100", "103"])


if __name__ == "__main__":
    unittest.main()
