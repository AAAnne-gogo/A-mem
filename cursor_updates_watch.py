#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_CHANGELOG_LIMIT = 5
DEFAULT_BLOG_LIMIT = 5
DEFAULT_X_LIMIT = 8
STATE_FILE_NAME = "state.json"
LATEST_REPORT_NAME = "latest_report.md"
HISTORY_DIR_NAME = "history"
SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) CursorUpdatesWatch/1.0"
TRANSIENT_HTTP_CODES = {403, 408, 409, 425, 429, 500, 502, 503, 504}
TITLE_SUFFIX_RE = re.compile(r"\s*(?:\||\u00b7|-)\s*Cursor(?:\s*-\s*The AI Code Editor)?(?:\s*\|\s*Cursor(?:\s*-\s*The AI Code Editor)?)?\s*$", re.I)
PAGE_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']|'
    r'<title>(.*?)</title>',
    re.I | re.S,
)
PUBLISHED_TIME_RE = re.compile(r"^Published Time:\s*(.+?)\s*$", re.M)
TIME_ONLY_RE = re.compile(r"^(?:\d+:\d+|\d+[smhdwy])$", re.I)
SITEMAP_NAMESPACE = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str
    date: str


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check official Cursor changelog, blog, and X updates."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately and do not consume the daily scheduled slot.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone used for the daily gate. Default: {DEFAULT_TIMEZONE}",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory used for state and markdown reports.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Override the current time with an ISO8601 timestamp for testing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    timezone = ZoneInfo(args.timezone)
    output_dir = (
        Path(args.output_dir).expanduser().resolve()
        if args.output_dir
        else Path(__file__).resolve().parent / ".cursor_updates"
    )
    output_dir.mkdir(parents=True, exist_ok=True)

    now_utc = parse_now(args.now)
    state = load_state(output_dir / STATE_FILE_NAME)
    scheduled_date = now_utc.astimezone(timezone).date().isoformat()

    should_run, reason = should_run_now(
        now_utc=now_utc,
        timezone=timezone,
        force=args.force,
        scheduled_date=scheduled_date,
        last_scheduled_date=state.get("last_successful_scheduled_date"),
    )
    if not should_run:
        report = build_skip_report(
            now_utc=now_utc,
            timezone=args.timezone,
            reason=reason,
        )
        write_report(output_dir / LATEST_REPORT_NAME, report)
        print(report)
        return 0

    changelog_items = fetch_cursor_entries("changelog", DEFAULT_CHANGELOG_LIMIT)
    blog_items = fetch_cursor_entries("blog", DEFAULT_BLOG_LIMIT)
    x_snapshot = fetch_x_snapshot(DEFAULT_X_LIMIT)

    new_changelog = diff_new_items(
        current=changelog_items,
        seen_urls=state.get("seen_urls", {}).get("changelog", []),
    )
    new_blog = diff_new_items(
        current=blog_items,
        seen_urls=state.get("seen_urls", {}).get("blog", []),
    )
    new_x_posts = diff_new_posts(
        current=x_snapshot["posts"],
        seen_posts=state.get("seen_x_posts", []),
    )

    report = build_report(
        now_utc=now_utc,
        timezone=args.timezone,
        force=args.force,
        changelog_items=changelog_items,
        blog_items=blog_items,
        x_snapshot=x_snapshot,
        new_changelog=new_changelog,
        new_blog=new_blog,
        new_x_posts=new_x_posts,
    )
    write_report(output_dir / LATEST_REPORT_NAME, report)
    history_dir = output_dir / HISTORY_DIR_NAME
    history_dir.mkdir(parents=True, exist_ok=True)
    write_report(history_dir / f"{scheduled_date}.md", report)

    state["seen_urls"] = {
        "changelog": merge_seen_urls(
            state.get("seen_urls", {}).get("changelog", []),
            changelog_items,
        ),
        "blog": merge_seen_urls(
            state.get("seen_urls", {}).get("blog", []),
            blog_items,
        ),
    }
    state["seen_x_posts"] = merge_seen_posts(
        state.get("seen_x_posts", []),
        x_snapshot["posts"],
    )
    state["latest"] = {
        "changelog": [asdict(item) for item in changelog_items],
        "blog": [asdict(item) for item in blog_items],
        "x_posts": x_snapshot["posts"],
        "x_published_time": x_snapshot["published_time"],
    }
    state["last_run_utc"] = now_utc.isoformat()
    if not args.force:
        state["last_successful_scheduled_date"] = scheduled_date
    save_state(output_dir / STATE_FILE_NAME, state)

    print(report)
    return 0


def parse_now(now_value: str | None) -> dt.datetime:
    if now_value is None:
        return dt.datetime.now(dt.timezone.utc)

    normalized = now_value.strip().replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def should_run_now(
    *,
    now_utc: dt.datetime,
    timezone: ZoneInfo,
    force: bool,
    scheduled_date: str,
    last_scheduled_date: str | None,
) -> tuple[bool, str]:
    if force:
        return True, "Forced run."

    local_now = now_utc.astimezone(timezone)
    if local_now.hour != 9:
        return False, (
            f"Skipped scheduled check: current local time is {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
            "and checks only run during the 09:00 hour."
        )
    if last_scheduled_date == scheduled_date:
        return False, (
            f"Skipped scheduled check: updates were already collected for {scheduled_date} "
            f"in timezone {timezone.key}."
        )
    return True, "Scheduled run."


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "last_successful_scheduled_date": None,
            "last_run_utc": None,
            "seen_urls": {"changelog": [], "blog": []},
            "seen_x_posts": [],
            "latest": {},
        }
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_state(path: Path, state: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True, ensure_ascii=True)
        handle.write("\n")


def fetch_cursor_entries(section: str, limit: int) -> list[UpdateItem]:
    sitemap = fetch_text(SITEMAP_URL)
    root = ElementTree.fromstring(sitemap)
    entries: list[tuple[str, str]] = []
    prefix = f"https://cursor.com/{section}/"

    for node in root.findall("sm:url", SITEMAP_NAMESPACE):
        loc = node.findtext("sm:loc", default="", namespaces=SITEMAP_NAMESPACE).strip()
        if not loc.startswith(prefix):
            continue
        remainder = loc[len(prefix) :]
        if not remainder or "/" in remainder:
            continue
        lastmod = node.findtext("sm:lastmod", default="", namespaces=SITEMAP_NAMESPACE).strip()
        entries.append((loc, normalize_date(lastmod)))

    entries.sort(key=lambda item: (item[1], item[0]), reverse=True)

    results: list[UpdateItem] = []
    for url, item_date in entries[:limit]:
        page_html = fetch_text(url)
        title = extract_page_title(page_html)
        results.append(
            UpdateItem(
                source=section,
                title=title,
                url=url,
                date=item_date,
            )
        )
    return results


def normalize_date(value: str) -> str:
    if not value:
        return ""
    normalized = value.replace("Z", "+00:00")
    try:
        return dt.datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        return value[:10]


def extract_page_title(page_html: str) -> str:
    match = PAGE_TITLE_RE.search(page_html)
    if not match:
        return "Untitled"
    raw_title = match.group(1) or match.group(2) or "Untitled"
    title = html.unescape(raw_title)
    title = re.sub(r"\s+", " ", title).strip()
    title = TITLE_SUFFIX_RE.sub("", title).strip()
    return title or "Untitled"


def fetch_x_snapshot(limit: int) -> dict[str, object]:
    markdown = fetch_text(X_MIRROR_URL)
    match = PUBLISHED_TIME_RE.search(markdown)
    published_time = match.group(1).strip() if match else None
    posts = parse_x_posts(markdown)[:limit]
    return {
        "profile_url": "https://x.com/cursor_ai",
        "mirror_url": X_MIRROR_URL,
        "published_time": published_time,
        "posts": posts,
    }


def parse_x_posts(markdown: str) -> list[str]:
    lines = markdown.splitlines()
    in_posts = False
    current: list[str] = []
    posts: list[str] = []

    for raw_line in lines:
        line = raw_line.strip()
        if not in_posts:
            if line in {"Cursor's posts", "Cursor\u2019s posts"}:
                in_posts = True
            continue

        if line in {"--------------", "Pinned", "Posts", "Replies", "Media", "Likes"}:
            continue

        if line.startswith("[![Image") or (line.startswith("![") and "](" in line):
            if current:
                posts.append(" ".join(current).strip())
                current = []
            continue

        if not line:
            if current:
                posts.append(" ".join(current).strip())
                current = []
            continue

        if TIME_ONLY_RE.match(line):
            if current:
                posts.append(" ".join(current).strip())
                current = []
            continue

        if line.startswith("Title:") or line.startswith("URL Source:") or line.startswith("Markdown Content:"):
            continue

        if line.startswith("@cursor_ai") or line == "Cursor":
            continue

        if line == "The best way to code with AI.":
            continue

        if line.startswith("[") and "](" in line:
            continue

        current.append(re.sub(r"\s+", " ", line))

    if current:
        posts.append(" ".join(current).strip())

    return dedupe_preserve_order(post for post in posts if post)


def diff_new_items(current: list[UpdateItem], seen_urls: Iterable[str]) -> list[UpdateItem]:
    seen_set = set(seen_urls)
    return [item for item in current if item.url not in seen_set]


def diff_new_posts(current: list[str], seen_posts: Iterable[str]) -> list[str]:
    seen_set = set(seen_posts)
    return [post for post in current if post not in seen_set]


def merge_seen_urls(existing: list[str], current: list[UpdateItem], limit: int = 50) -> list[str]:
    merged = dedupe_preserve_order([item.url for item in current] + existing)
    return merged[:limit]


def merge_seen_posts(existing: list[str], current: list[str], limit: int = 50) -> list[str]:
    merged = dedupe_preserve_order(current + existing)
    return merged[:limit]


def dedupe_preserve_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def fetch_text(url: str, *, retries: int = 4, timeout: int = 30) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9,*/*;q=0.8",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in TRANSIENT_HTTP_CODES or attempt == retries:
                raise
        except urllib.error.URLError as exc:
            last_error = exc
            if attempt == retries:
                raise

        sleep_seconds = 2 ** (attempt - 1)
        time.sleep(sleep_seconds)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def build_skip_report(*, now_utc: dt.datetime, timezone: str, reason: str) -> str:
    return "\n".join(
        [
            "# Cursor updates report",
            "",
            f"- Run time (UTC): {now_utc.isoformat()}",
            f"- Timezone gate: {timezone}",
            f"- Status: skipped",
            f"- Reason: {reason}",
            "",
        ]
    )


def build_report(
    *,
    now_utc: dt.datetime,
    timezone: str,
    force: bool,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_snapshot: dict[str, object],
    new_changelog: list[UpdateItem],
    new_blog: list[UpdateItem],
    new_x_posts: list[str],
) -> str:
    lines = [
        "# Cursor updates report",
        "",
        f"- Run time (UTC): {now_utc.isoformat()}",
        f"- Timezone gate: {timezone}",
        f"- Mode: {'forced' if force else 'scheduled'}",
        "",
        "## New changelog items",
    ]
    lines.extend(render_item_list(new_changelog))
    lines.extend(["", "## New blog items"])
    lines.extend(render_item_list(new_blog))
    lines.extend(["", "## New official X posts"])
    lines.extend(render_post_list(new_x_posts))
    lines.extend(["", "## Latest changelog"])
    lines.extend(render_item_list(changelog_items))
    lines.extend(["", "## Latest blog"])
    lines.extend(render_item_list(blog_items))
    lines.extend(
        [
            "",
            "## Official X snapshot",
            f"- Account: Cursor (@cursor_ai) | {x_snapshot['profile_url']}",
            f"- Mirror: {x_snapshot['mirror_url']}",
            f"- Mirror published time: {x_snapshot['published_time'] or 'unknown'}",
        ]
    )
    lines.extend(render_post_list(x_snapshot["posts"]))
    lines.append("")
    return "\n".join(lines)


def render_item_list(items: list[UpdateItem]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {item.date} | {item.title} | {item.url}" for item in items]


def render_post_list(posts: list[str]) -> list[str]:
    if not posts:
        return ["- None"]
    return [f'- "{post}"' for post in posts]


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
