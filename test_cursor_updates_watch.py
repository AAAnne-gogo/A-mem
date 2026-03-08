import json
import tempfile
import textwrap
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from cursor_updates_watch import (
    BLOG_SITEMAP_URL,
    CHANGELOG_RSS_URL,
    OFFICIAL_X_URL,
    X_TIMELINE_MIRROR_URL,
    hash_text,
    load_state,
    run_watcher,
)


def make_fetcher(payloads: dict[str, str]):
    def fetch(url: str) -> str:
        if url not in payloads:
            raise AssertionError(f"Unexpected URL requested: {url}")
        return payloads[url]

    return fetch


def build_blog_page(title: str, description: str) -> str:
    return textwrap.dedent(
        f"""
        <html>
          <head>
            <title>{title} · Cursor</title>
            <meta name="description" content="{description}" />
            <meta property="og:title" content="{title} · Cursor" />
            <meta property="og:description" content="{description}" />
          </head>
          <body></body>
        </html>
        """
    ).strip()


class CursorUpdatesWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.maxDiff = None
        self.timezone = "Asia/Shanghai"
        self.zone = ZoneInfo(self.timezone)

    def test_skip_when_not_in_scheduled_hour(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "cursor_updates.md"
            state_path = Path(tmpdir) / ".cursor_updates" / "state.json"

            result = run_watcher(
                now=datetime(2026, 3, 8, 8, 0, tzinfo=self.zone),
                fetcher=make_fetcher({}),
                timezone_name=self.timezone,
                scheduled_hour=9,
                report_path=report_path,
                state_path=state_path,
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("waiting for 09:00", result.message)
            self.assertFalse(report_path.exists())
            self.assertFalse(state_path.exists())

    def test_force_run_bootstraps_recent_items_and_writes_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "cursor_updates.md"
            state_path = Path(tmpdir) / ".cursor_updates" / "state.json"

            blog_automations = "https://cursor.com/blog/automations"
            blog_jetbrains = "https://cursor.com/blog/jetbrains-acp"
            blog_old = "https://cursor.com/blog/old-news"

            fetcher = make_fetcher(
                {
                    CHANGELOG_RSS_URL: textwrap.dedent(
                        """
                        <rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
                          <channel>
                            <item>
                              <title>Automations</title>
                              <link>https://cursor.com/changelog/03-05-26</link>
                              <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
                              <description>Automations summary.</description>
                              <content:encoded><![CDATA[<p>Automations now run based on triggers.</p>]]></content:encoded>
                            </item>
                            <item>
                              <title>Cursor in JetBrains IDEs</title>
                              <link>https://cursor.com/changelog/03-04-26</link>
                              <pubDate>Wed, 04 Mar 2026 00:00:00 GMT</pubDate>
                              <description>JetBrains summary.</description>
                            </item>
                            <item>
                              <title>Old Update</title>
                              <link>https://cursor.com/changelog/02-20-26</link>
                              <pubDate>Fri, 20 Feb 2026 00:00:00 GMT</pubDate>
                              <description>Old summary.</description>
                            </item>
                          </channel>
                        </rss>
                        """
                    ).strip(),
                    BLOG_SITEMAP_URL: textwrap.dedent(
                        f"""
                        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                          <url>
                            <loc>{blog_automations}</loc>
                            <lastmod>2026-03-05T12:00:00.000Z</lastmod>
                          </url>
                          <url>
                            <loc>{blog_jetbrains}</loc>
                            <lastmod>2026-03-04T12:00:00.000Z</lastmod>
                          </url>
                          <url>
                            <loc>{blog_old}</loc>
                            <lastmod>2026-02-01T12:00:00.000Z</lastmod>
                          </url>
                        </urlset>
                        """
                    ).strip(),
                    blog_automations: build_blog_page(
                        "Build agents that run automatically",
                        "Cursor now supports automations that run on triggers and schedules.",
                    ),
                    blog_jetbrains: build_blog_page(
                        "Cursor in JetBrains IDEs",
                        "Cursor is now available in JetBrains IDEs through ACP.",
                    ),
                    X_TIMELINE_MIRROR_URL: textwrap.dedent(
                        """
                        Title: Cursor (@cursor_ai) / X

                        URL Source: http://x.com/cursor_ai

                        Markdown Content:
                        Cursor’s posts
                        --------------

                        Pinned

                        [![Image 1](https://example.com/a.jpg)](https://x.com/cursor_ai)

                        We're introducing Cursor Automations to build always-on agents.

                        1:34

                        [![Image 2](https://example.com/b.jpg)](https://x.com/cursor_ai)

                        We're introducing Cursor Automations to build always-on agents.

                        [![Image 3](https://example.com/c.jpg)](https://x.com/cursor_ai)

                        Cursor can now continuously monitor and improve your codebase.

                        [![Image 4](https://example.com/d.jpg)](https://x.com/cursor_ai)

                        GPT 5.4 is now available in Cursor!
                        """
                    ).strip(),
                }
            )

            result = run_watcher(
                now=datetime(2026, 3, 8, 22, 0, tzinfo=self.zone),
                fetcher=fetcher,
                timezone_name=self.timezone,
                scheduled_hour=9,
                report_path=report_path,
                state_path=state_path,
                force=True,
            )

            self.assertEqual(result.status, "success")
            self.assertIn("2 changelog item(s)", result.message)
            self.assertIn("2 blog post(s)", result.message)
            self.assertIn("3 X post(s)", result.message)

            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Automations", report_text)
            self.assertIn("Cursor in JetBrains IDEs", report_text)
            self.assertIn("Build agents that run automatically", report_text)
            self.assertIn("Cursor can now continuously monitor and improve your codebase.", report_text)
            self.assertNotIn("Old Update", report_text)
            self.assertNotIn("old-news", report_text)
            self.assertEqual(
                report_text.count("We're introducing Cursor Automations to build always-on agents."),
                1,
            )

            state = load_state(state_path)
            self.assertEqual(state["last_run_date"], "2026-03-08")
            self.assertEqual(state["timezone"], self.timezone)
            self.assertEqual(len(state["seen"]["changelog"]), 3)
            self.assertEqual(len(state["seen"]["blog"]), 3)
            self.assertEqual(len(state["seen"]["x"]), 3)

    def test_only_new_items_are_reported_after_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "cursor_updates.md"
            state_path = Path(tmpdir) / ".cursor_updates" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "last_run_date": "2026-03-08",
                        "last_run_at": "2026-03-08T09:00:00+08:00",
                        "timezone": self.timezone,
                        "seen": {
                            "changelog": ["https://cursor.com/changelog/03-05-26"],
                            "blog": ["https://cursor.com/blog/automations"],
                            "x": [hash_text("We're introducing Cursor Automations to build always-on agents.")],
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )

            blog_automations = "https://cursor.com/blog/automations"
            blog_new = "https://cursor.com/blog/bugbot-autofix"

            fetcher = make_fetcher(
                {
                    CHANGELOG_RSS_URL: textwrap.dedent(
                        """
                        <rss version="2.0">
                          <channel>
                            <item>
                              <title>Bugbot Autofix</title>
                              <link>https://cursor.com/changelog/02-26-26</link>
                              <pubDate>Thu, 26 Feb 2026 00:00:00 GMT</pubDate>
                              <description>Bugbot can now fix issues automatically.</description>
                            </item>
                            <item>
                              <title>Automations</title>
                              <link>https://cursor.com/changelog/03-05-26</link>
                              <pubDate>Thu, 05 Mar 2026 00:00:00 GMT</pubDate>
                              <description>Automations summary.</description>
                            </item>
                          </channel>
                        </rss>
                        """
                    ).strip(),
                    BLOG_SITEMAP_URL: textwrap.dedent(
                        f"""
                        <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
                          <url>
                            <loc>{blog_new}</loc>
                            <lastmod>2026-03-09T12:00:00.000Z</lastmod>
                          </url>
                          <url>
                            <loc>{blog_automations}</loc>
                            <lastmod>2026-03-05T12:00:00.000Z</lastmod>
                          </url>
                        </urlset>
                        """
                    ).strip(),
                    blog_new: build_blog_page(
                        "Bugbot Autofix",
                        "Bugbot can now fix issues it finds in pull requests.",
                    ),
                    X_TIMELINE_MIRROR_URL: textwrap.dedent(
                        """
                        Title: Cursor (@cursor_ai) / X

                        Markdown Content:
                        Cursor’s posts
                        --------------

                        [![Image 1](https://example.com/a.jpg)](https://x.com/cursor_ai)

                        We're introducing Cursor Automations to build always-on agents.

                        [![Image 2](https://example.com/b.jpg)](https://x.com/cursor_ai)

                        Cursor can now automatically fix issues it finds in PRs with Bugbot Autofix.
                        """
                    ).strip(),
                }
            )

            result = run_watcher(
                now=datetime(2026, 3, 9, 9, 0, tzinfo=self.zone),
                fetcher=fetcher,
                timezone_name=self.timezone,
                scheduled_hour=9,
                report_path=report_path,
                state_path=state_path,
            )

            self.assertEqual(result.status, "success")
            self.assertIn("1 changelog item(s)", result.message)
            self.assertIn("1 blog post(s)", result.message)
            self.assertIn("1 X post(s)", result.message)

            report_text = report_path.read_text(encoding="utf-8")
            self.assertIn("Bugbot Autofix", report_text)
            self.assertIn("fix issues it finds in PRs", report_text)
            self.assertNotIn("Build agents that run automatically", report_text)
            self.assertNotIn("Automations summary", report_text)

            state = load_state(state_path)
            self.assertEqual(state["last_run_date"], "2026-03-09")
            self.assertIn("https://cursor.com/changelog/02-26-26", state["seen"]["changelog"])
            self.assertIn(blog_new, state["seen"]["blog"])
            self.assertEqual(result.selected_items["x"][0].url, OFFICIAL_X_URL)

    def test_skip_if_already_ran_today(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            report_path = Path(tmpdir) / "cursor_updates.md"
            state_path = Path(tmpdir) / ".cursor_updates" / "state.json"
            state_path.parent.mkdir(parents=True, exist_ok=True)
            state_path.write_text(
                json.dumps(
                    {
                        "last_run_date": "2026-03-08",
                        "last_run_at": "2026-03-08T09:00:00+08:00",
                        "timezone": self.timezone,
                        "seen": {"changelog": [], "blog": [], "x": []},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

            result = run_watcher(
                now=datetime(2026, 3, 8, 9, 30, tzinfo=self.zone),
                fetcher=make_fetcher({}),
                timezone_name=self.timezone,
                scheduled_hour=9,
                report_path=report_path,
                state_path=state_path,
            )

            self.assertEqual(result.status, "skipped")
            self.assertIn("already collected", result.message)
            self.assertFalse(report_path.exists())


if __name__ == "__main__":
    unittest.main()
