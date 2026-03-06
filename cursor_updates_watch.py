#!/usr/bin/env python3
"""Check Cursor changelog, blog, and official X updates on a 9am schedule."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from dataclasses import asdict, dataclass
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / ".cursor_updates"
STATE_PATH = STATE_DIR / "state.json"
REPORT_PATH = STATE_DIR / "latest_report.md"

TIMEZONE = ZoneInfo("Asia/Shanghai")
USER_AGENT = "Mozilla/5.0 (compatible; CursorUpdatesWatch/1.0)"
FETCH_TIMEOUT_SECONDS = 30
CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_URL = "https://cursor.com/blog"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URL = "https://r.jina.ai/http://https://x.com/cursor_ai"
MAX_ITEMS = 5


@dataclass(frozen=True)
class FeedItem:
    date: str
    title: str
    url: str
    summary: str | None = None


@dataclass(frozen=True)
class XSnapshot:
    account_name: str
    handle: str
    profile_url: str
    mirror_url: str
    published_time: str | None
    posts: list[str]


@dataclass(frozen=True)
class Snapshot:
    checked_at_utc: str
    checked_at_local: str
    changelog: list[FeedItem]
    blog: list[FeedItem]
    x: XSnapshot


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check Cursor changelog, blog, and official X updates. "
            "Runs only at 09:00 Asia/Shanghai unless --force is passed."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the 09:00 Asia/Shanghai schedule gate.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=MAX_ITEMS,
        help=f"Number of latest items to keep per source (default: {MAX_ITEMS}).",
    )
    return parser.parse_args()


def now_local() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc).astimezone(TIMEZONE)


def should_run(current_time: dt.datetime) -> bool:
    return current_time.hour == 9


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error while fetching {url}: {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error while fetching {url}: {exc.reason}") from exc


def normalize_text(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(without_tags)).strip()


def dedupe_items(items: Iterable[FeedItem]) -> list[FeedItem]:
    seen_urls: set[str] = set()
    result: list[FeedItem] = []
    for item in items:
        if item.url in seen_urls:
            continue
        seen_urls.add(item.url)
        result.append(item)
    return result


def parse_changelog(html: str, max_items: int) -> list[FeedItem]:
    pattern = re.compile(
        r'<a[^>]*href="(?P<href>/changelog/[^"]+)"[^>]*>.*?'
        r'<time[^>]*dateTime="(?P<date>[^"]+)"[^>]*>.*?</time>\s*</a>'
        r'.*?<h1[^>]*>\s*<a[^>]*href="(?P=href)"[^>]*>(?P<title>[^<]+)</a>\s*</h1>'
        r'.*?<div class="prose prose--block">\s*<p>(?P<summary>.*?)</p>',
        re.S,
    )
    items = [
        FeedItem(
            date=match.group("date")[:10],
            title=normalize_text(match.group("title")),
            url=f"https://cursor.com{match.group('href')}",
            summary=normalize_text(match.group("summary")),
        )
        for match in pattern.finditer(html)
    ]
    return dedupe_items(items)[:max_items]


def parse_blog(html: str, max_items: int) -> list[FeedItem]:
    pattern = re.compile(
        r'<article[^>]*>\s*<a[^>]*href="(?P<href>/blog/[^"]+)"[^>]*>.*?'
        r'<p class="type-base text-theme-text text-pretty">(?P<title>[^<]+)</p>\s*'
        r'<p class="type-base text-theme-text-sec text-pretty">(?P<summary>[^<]+)</p>'
        r'.*?<time[^>]*dateTime="(?P<date>[^"]+)"',
        re.S,
    )
    items = []
    for match in pattern.finditer(html):
        href = match.group("href")
        if href.startswith("/blog/topic"):
            continue
        items.append(
            FeedItem(
                date=match.group("date")[:10],
                title=normalize_text(match.group("title")),
                url=f"https://cursor.com{href}",
                summary=normalize_text(match.group("summary")),
            )
        )
    return dedupe_items(items)[:max_items]


def extract_x_posts(markdown_text: str, max_items: int) -> list[str]:
    posts_section = markdown_text.split("Cursor’s posts", 1)
    if len(posts_section) < 2:
        return []

    chunks = re.split(
        r"\[\!\[Image .*?\]\(https://x\.com/cursor_ai\)\n\n",
        posts_section[1],
        flags=re.S,
    )
    posts: list[str] = []
    seen_posts: set[str] = set()

    for chunk in chunks[1:]:
        lines = [line.strip() for line in chunk.splitlines()]
        collected: list[str] = []
        for line in lines:
            if not line:
                if collected:
                    break
                continue
            if line == "Pinned":
                continue
            if line.startswith("![Image"):
                break
            if re.fullmatch(r"\d+:\d{2}", line):
                break
            collected.append(line)
        candidate = normalize_text(" ".join(collected))
        if not candidate or candidate in seen_posts:
            continue
        seen_posts.add(candidate)
        posts.append(candidate)
        if len(posts) >= max_items:
            break
    return posts


def parse_x_snapshot(markdown_text: str, max_items: int) -> XSnapshot:
    account_match = re.search(
        r"Title:\s*(?P<name>.+?)\s+\((?P<handle>@[^)]+)\)\s*/\s*X",
        markdown_text,
    )
    published_match = re.search(r"Published Time:\s*(?P<value>.+)", markdown_text)

    return XSnapshot(
        account_name=account_match.group("name").strip() if account_match else "Cursor",
        handle=account_match.group("handle").strip() if account_match else "@cursor_ai",
        profile_url=X_PROFILE_URL,
        mirror_url=X_MIRROR_URL,
        published_time=published_match.group("value").strip() if published_match else None,
        posts=extract_x_posts(markdown_text, max_items),
    )


def load_previous_state() -> dict:
    if not STATE_PATH.exists():
        return {}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def to_serializable(snapshot: Snapshot) -> dict:
    data = asdict(snapshot)
    data["changelog"] = [asdict(item) for item in snapshot.changelog]
    data["blog"] = [asdict(item) for item in snapshot.blog]
    data["x"] = asdict(snapshot.x)
    return data


def diff_items(current: list[FeedItem], previous: list[dict]) -> list[FeedItem]:
    previous_urls = {item.get("url") for item in previous}
    return [item for item in current if item.url not in previous_urls]


def diff_x(current: XSnapshot, previous: dict) -> list[str]:
    previous_posts = previous.get("posts") or []
    return [post for post in current.posts if post not in previous_posts]


def render_item_lines(items: list[FeedItem]) -> list[str]:
    lines: list[str] = []
    for item in items:
        detail = f" - {item.summary}" if item.summary else ""
        lines.append(f"- {item.date} | {item.title} | {item.url}{detail}")
    return lines or ["- None"]


def build_report(
    snapshot: Snapshot,
    previous_state: dict,
    forced: bool,
    updates_found: bool,
) -> str:
    new_changelog = diff_items(snapshot.changelog, previous_state.get("changelog", []))
    new_blog = diff_items(snapshot.blog, previous_state.get("blog", []))
    new_x_posts = diff_x(snapshot.x, previous_state.get("x", {}))

    lines = [
        "# Cursor daily updates",
        "",
        f"- Checked at (UTC): {snapshot.checked_at_utc}",
        f"- Checked at (Asia/Shanghai): {snapshot.checked_at_local}",
        f"- Mode: {'forced run' if forced else 'scheduled run'}",
        f"- Status: {'updates found' if updates_found else 'no new updates'}",
        "",
        "## New since previous snapshot",
        "",
        "### Changelog",
        *render_item_lines(new_changelog),
        "",
        "### Blog",
        *render_item_lines(new_blog),
        "",
        "### Official X",
    ]

    if new_x_posts:
        lines.extend(f"- {post}" for post in new_x_posts)
    else:
        lines.append("- None")

    lines.extend(
        [
            f"- Snapshot published time: {snapshot.x.published_time or 'Unknown'}",
            "",
            "## Latest snapshot",
            "",
            "### Changelog",
            *render_item_lines(snapshot.changelog),
            "",
            "### Blog",
            *render_item_lines(snapshot.blog),
            "",
            "### Official X",
            f"- Account: {snapshot.x.account_name} ({snapshot.x.handle})",
            f"- Profile URL: {snapshot.x.profile_url}",
            f"- Mirror URL: {snapshot.x.mirror_url}",
            f"- Snapshot published time: {snapshot.x.published_time or 'Unknown'}",
        ]
    )

    if snapshot.x.posts:
        lines.append("- Latest visible posts:")
        lines.extend(f"  - {post}" for post in snapshot.x.posts)
    else:
        lines.append("- Latest visible posts: None")

    return "\n".join(lines) + "\n"


def build_snapshot(max_items: int) -> Snapshot:
    changelog_html = fetch_text(CHANGELOG_URL)
    blog_html = fetch_text(BLOG_URL)
    x_markdown = fetch_text(X_MIRROR_URL)

    current_local = now_local()
    current_utc = current_local.astimezone(dt.timezone.utc)

    return Snapshot(
        checked_at_utc=current_utc.isoformat().replace("+00:00", "Z"),
        checked_at_local=current_local.isoformat(),
        changelog=parse_changelog(changelog_html, max_items),
        blog=parse_blog(blog_html, max_items),
        x=parse_x_snapshot(x_markdown, max_items),
    )


def write_outputs(snapshot: Snapshot, report_text: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(to_serializable(snapshot), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    REPORT_PATH.write_text(report_text, encoding="utf-8")


def main() -> int:
    args = parse_args()
    current_local = now_local()

    if not args.force and not should_run(current_local):
        print(
            "Skipped Cursor update check because it is not 09:00 in Asia/Shanghai. "
            f"Current local time: {current_local.isoformat()}",
            file=sys.stdout,
        )
        return 0

    previous_state = load_previous_state()
    snapshot = build_snapshot(args.max_items)
    updates_found = bool(
        diff_items(snapshot.changelog, previous_state.get("changelog", []))
        or diff_items(snapshot.blog, previous_state.get("blog", []))
        or diff_x(snapshot.x, previous_state.get("x", {}))
    )

    report_text = build_report(snapshot, previous_state, args.force, updates_found)
    write_outputs(snapshot, report_text)
    print(report_text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
