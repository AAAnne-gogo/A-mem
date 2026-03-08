import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import cursor_updates_watch as watcher


class CursorUpdatesWatchTests(unittest.TestCase):
    def test_parse_changelog_rss(self) -> None:
        xml_text = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Automations</title>
              <link>https://cursor.com/changelog/03-05-26</link>
              <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
              <description>Cursor now supports automations.</description>
            </item>
            <item>
              <title>Older item</title>
              <link>https://cursor.com/changelog/02-01-26</link>
              <pubDate>Sat, 01 Feb 2026 00:00:00 GMT</pubDate>
              <description>Older summary.</description>
            </item>
          </channel>
        </rss>
        """
        items = watcher.parse_changelog_rss(xml_text)
        self.assertEqual(items[0].title, "Automations")
        self.assertEqual(items[0].summary, "Cursor now supports automations.")
        self.assertGreater(items[0].published, items[1].published)

    def test_parse_x_markdown_deduplicates_posts(self) -> None:
        markdown = """
        Title: Cursor (@cursor_ai) / X
        Cursor’s posts
        --------------
        Pinned
        [![Image 1]](https://x.com/cursor_ai)
        We're introducing Cursor Automations to build always-on agents.
        [![Image 2]](https://x.com/cursor_ai)
        We're introducing Cursor Automations to build always-on agents.
        [![Image 3]](https://x.com/cursor_ai)
        Cursor is now available in JetBrains IDEs through the Agent Client Protocol.
        """
        items = watcher.parse_x_markdown(markdown)
        self.assertEqual(
            [item.title for item in items],
            [
                "We're introducing Cursor Automations to build always-on agents.",
                "Cursor is now available in JetBrains IDEs through the Agent Client Protocol.",
            ],
        )

    def test_should_run_scheduled(self) -> None:
        now = datetime(2026, 3, 8, 9, 0, tzinfo=watcher.TIME_ZONE)
        should_run, reason = watcher.should_run_scheduled(now, {"last_success_date": "2026-03-07"})
        self.assertTrue(should_run)
        self.assertIn("open", reason)

        should_run, reason = watcher.should_run_scheduled(now, {"last_success_date": "2026-03-08"})
        self.assertFalse(should_run)
        self.assertIn("already completed", reason)

        late = datetime(2026, 3, 8, 10, 0, tzinfo=watcher.TIME_ZONE)
        should_run, reason = watcher.should_run_scheduled(late, {})
        self.assertFalse(should_run)
        self.assertIn("waiting for 09:00", reason)

    def test_select_new_items_bootstrap_respects_lookback_and_limit(self) -> None:
        now_utc = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
        fresh = watcher.UpdateItem(
            source="blog",
            title="Fresh",
            url="https://cursor.com/blog/fresh",
            summary="",
            published=datetime(2026, 3, 7, 0, 0, tzinfo=timezone.utc),
            identity="fresh",
        )
        stale = watcher.UpdateItem(
            source="blog",
            title="Stale",
            url="https://cursor.com/blog/stale",
            summary="",
            published=datetime(2026, 2, 1, 0, 0, tzinfo=timezone.utc),
            identity="stale",
        )
        selected = watcher.select_new_items(
            [fresh, stale],
            seen_ids=set(),
            now_utc=now_utc,
            bootstrap_lookback_days=7,
            bootstrap_limit=1,
        )
        self.assertEqual([item.title for item in selected], ["Fresh"])

    def test_forced_run_writes_report_without_touching_state(self) -> None:
        now_utc = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
        sample_item = watcher.UpdateItem(
            source="changelog",
            title="Automations",
            url="https://cursor.com/changelog/03-05-26",
            summary="Cursor now supports automations.",
            published=datetime(2026, 3, 5, 0, 0, tzinfo=timezone.utc),
            identity="https://cursor.com/changelog/03-05-26",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            runtime_dir = tmp_path / ".cursor_updates"
            history_dir = runtime_dir / "history"
            with mock.patch.object(watcher, "ROOT_DIR", tmp_path), \
                mock.patch.object(watcher, "RUNTIME_DIR", runtime_dir), \
                mock.patch.object(watcher, "STATE_PATH", runtime_dir / "state.json"), \
                mock.patch.object(watcher, "LATEST_REPORT_PATH", runtime_dir / "latest_report.md"), \
                mock.patch.object(watcher, "ROOT_REPORT_PATH", tmp_path / "cursor_updates.md"), \
                mock.patch.object(watcher, "HISTORY_DIR", history_dir), \
                mock.patch.object(
                    watcher,
                    "collect_updates",
                    return_value=(
                        {"changelog": [sample_item], "blog": [], "x": []},
                        {},
                        {"changelog": [sample_item], "blog": [], "x": []},
                    ),
                ), \
                mock.patch.object(watcher, "store_state") as store_state:
                exit_code = watcher.run(force=True, now_utc=now_utc)

            self.assertEqual(exit_code, 0)
            self.assertFalse(store_state.called)
            report_path = tmp_path / "cursor_updates.md"
            self.assertTrue(report_path.exists())
            self.assertIn("Automations", report_path.read_text(encoding="utf-8"))

    def test_scheduled_run_updates_state(self) -> None:
        now_utc = datetime(2026, 3, 8, 1, 0, tzinfo=timezone.utc)
        sample_post = watcher.UpdateItem(
            source="x",
            title="GPT 5.4 is now available in Cursor!",
            url="https://x.com/cursor_ai",
            summary="",
            published=None,
            identity="x-gpt-5.4",
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            runtime_dir = tmp_path / ".cursor_updates"
            history_dir = runtime_dir / "history"
            with mock.patch.object(watcher, "ROOT_DIR", tmp_path), \
                mock.patch.object(watcher, "RUNTIME_DIR", runtime_dir), \
                mock.patch.object(watcher, "STATE_PATH", runtime_dir / "state.json"), \
                mock.patch.object(watcher, "LATEST_REPORT_PATH", runtime_dir / "latest_report.md"), \
                mock.patch.object(watcher, "ROOT_REPORT_PATH", tmp_path / "cursor_updates.md"), \
                mock.patch.object(watcher, "HISTORY_DIR", history_dir), \
                mock.patch.object(
                    watcher,
                    "collect_updates",
                    return_value=(
                        {"changelog": [], "blog": [], "x": [sample_post]},
                        {},
                        {"changelog": [], "blog": [], "x": [sample_post]},
                    ),
                ):
                exit_code = watcher.run(force=False, now_utc=now_utc)

            self.assertEqual(exit_code, 0)
            state = json_load(tmp_path / ".cursor_updates" / "state.json")
            self.assertEqual(state["last_success_date"], "2026-03-08")
            self.assertIn("x-gpt-5.4", state["seen"]["x"])


def json_load(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
