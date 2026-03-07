import argparse
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import cursor_updates as cu


SAMPLE_CHANGELOG_HTML = """
<article><div class="grid-cursor gap-y-0 pb-v5 mb-v5 border-theme-border-02 border-b">
<div class="mb-v2/12 col-span-full xl:col-end-7">
<p class="text-theme-text-sec sticky top-[var(--site-sticky-top)] left-[-1px] inline-flex items-center">
<a class="hover:text-theme-text inline-flex items-center" href="/changelog/03-05-26">
<time dateTime="2026-03-05T00:00:00.000Z" class="type-base">Mar 5, 2026</time></a></p></div>
<div class="col-span-full md:col-start-1 md:col-end-19 lg:col-start-1 lg:col-end-17 xl:col-start-7 xl:col-end-19">
<header class="mb-v2 relative"><h1 class="type-lg text-balance" id="automations">
<a class="active:text-theme-text hover:opacity-90" href="/changelog/03-05-26">Automations</a></h1></header>
<div class="prose prose--block">
<p>Cursor now supports automations for building always-on agents.</p>
<p>Automations run on schedules and event triggers.</p>
</div></div></div></article>
"""

SAMPLE_BLOG_HTML = """
<article class="flex grow-1 flex-col mb-g1">
<a class="card card--text grow-1 grid-cursor-v1 grid-cols-[1fr_auto] @container" href="/blog/automations">
<div class="flex flex-col @sm:col-start-1 @sm:col-end-2"><div class="grow-1">
<p class="type-base text-theme-text text-pretty">Build agents that run automatically</p>
<p class="type-base text-theme-text-sec text-pretty">Cursor now supports automations that run based on triggers and instructions you define.</p>
</div><div class="mt-v1 text-theme-text-sec flex shrink-0 items-center">
<span class="capitalize">product<!-- --> <!-- -->·<!-- --> </span>
<time dateTime="2026-03-05T12:00:00.000Z" class="type-base">Mar 5, 2026</time>
</div></div></a></article>
"""

SAMPLE_X_MARKDOWN = """
Title: Cursor (@cursor_ai) / X

URL Source: http://x.com/cursor_ai

Published Time: Sat, 07 Mar 2026 22:52:36 GMT

Markdown Content:
[![Image 1: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai/photo)

Cursor

@cursor_ai

The best way to code with AI.

Cursor's posts
--------------

Pinned

[![Image 2: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

We're introducing Cursor Automations to build always-on agents.

1:34

[![Image 3: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

GPT 5.4 is now available in Cursor! We've found it to be more natural and assertive than previous models.

[![Image 4: Square profile picture](https://pbs.twimg.com/profile_images/example.jpg)](https://x.com/cursor_ai)

Cursor is now available in JetBrains IDEs through the Agent Client Protocol.
"""


class ParsingTests(unittest.TestCase):
    def test_parse_changelog_html(self) -> None:
        entries = cu.parse_changelog_html(SAMPLE_CHANGELOG_HTML)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Automations")
        self.assertEqual(entries[0].url, "https://cursor.com/changelog/03-05-26")
        self.assertIn("always-on agents", entries[0].summary)

    def test_parse_blog_html(self) -> None:
        entries = cu.parse_blog_html(SAMPLE_BLOG_HTML)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].title, "Build agents that run automatically")
        self.assertEqual(entries[0].category, "product")
        self.assertEqual(entries[0].url, "https://cursor.com/blog/automations")

    def test_parse_x_markdown(self) -> None:
        snapshot_time, entries = cu.parse_x_markdown(SAMPLE_X_MARKDOWN)
        self.assertEqual(snapshot_time, "2026-03-07T22:52:36+00:00")
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[0].title, "We're introducing Cursor Automations to build always-on agents.")
        self.assertIn("JetBrains IDEs", entries[2].title)

    def test_should_run_now(self) -> None:
        dt = datetime(2026, 3, 8, 9, 15, 0)
        self.assertTrue(cu.should_run_now(dt, 9))
        self.assertFalse(cu.should_run_now(dt, 8))

    def test_dedupe_entries(self) -> None:
        entries = [
            cu.UpdateEntry(source="changelog", entry_id="same", title="A", url="https://example.com/a"),
            cu.UpdateEntry(source="changelog", entry_id="same", title="A duplicate", url="https://example.com/a"),
            cu.UpdateEntry(source="changelog", entry_id="other", title="B", url="https://example.com/b"),
        ]
        deduped = cu.dedupe_entries(entries)
        self.assertEqual(len(deduped), 2)
        self.assertEqual(deduped[0].title, "A")


class RunFlowTests(unittest.TestCase):
    def test_force_run_writes_report_and_state(self) -> None:
        responses = {
            cu.CHANGELOG_URL: SAMPLE_CHANGELOG_HTML,
            cu.BLOG_URL: SAMPLE_BLOG_HTML,
            cu.X_SNAPSHOT_URL: SAMPLE_X_MARKDOWN,
        }

        def fake_fetch_text(url: str) -> str:
            return responses[url]

        with tempfile.TemporaryDirectory() as temp_dir:
            base_path = Path(temp_dir)
            args = argparse.Namespace(
                force=True,
                timezone="Asia/Shanghai",
                hour=9,
                max_changelog=5,
                max_blog=5,
                max_x=5,
                state_file=str(base_path / "state.json"),
                output_dir=str(base_path / "reports"),
            )
            fixed_now = datetime(2026, 3, 8, 9, 5, 0)
            with mock.patch.object(cu, "fetch_text", side_effect=fake_fetch_text):
                with mock.patch.object(cu, "current_local_time", return_value=fixed_now):
                    exit_code = cu.run(args)

            self.assertEqual(exit_code, 0)
            report_path = base_path / "reports" / "2026-03-08.md"
            self.assertTrue(report_path.exists())
            self.assertIn("Cursor Daily Updates - 2026-03-08", report_path.read_text(encoding="utf-8"))

            state = cu.load_state(base_path / "state.json")
            self.assertEqual(state["last_successful_local_date"], "2026-03-08")
            self.assertEqual(state["latest_report"]["counts"]["changelog"], 1)


if __name__ == "__main__":
    unittest.main()
