#!/usr/bin/env python3
"""Fetch and persist daily Cursor updates from official public sources."""

from __future__ import annotations

import argparse
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
OFFICIAL_X_URL = "https://x.com/cursor_ai"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"

ROOT_DIR = Path(__file__).resolve().parent
STATE_DIR = ROOT_DIR / ".cursor_updates"
STATE_FILE = STATE_DIR / "state.json"
REPORT_FILE = ROOT_DIR / "cursor_updates.md"

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class UpdateItem:
    title: str
    url: str
    published_at: datetime | None
    summary: str


def default_state() -> dict[str, Any]:
    return {
        "last_run_date": None,
        "seen_changelog_urls": [],
        "seen_blog_urls": [],
        "seen_x_posts": [],
    }


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    state = default_state()
    if not path.exists():
        return state

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        return state

    for key in state:
        if key in raw:
            state[key] = raw[key]
    return state


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def merge_seen(new_values: list[str], existing_values: list[str], limit: int = 200) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in list(new_values) + list(existing_values):
        if not value or value in seen:
            continue
        merged.append(value)
        seen.add(value)
        if len(merged) >= limit:
            break
    return merged


def should_run(now_local: datetime, state: dict[str, Any], force: bool = False) -> tuple[bool, str]:
    today = now_local.date().isoformat()
    if force:
        return True, "forced"
    if now_local.hour != 9:
        return False, f"skipping outside 09:00 Asia/Shanghai window ({now_local.isoformat()})"
    if state.get("last_run_date") == today:
        return False, f"skipping duplicate run for {today}"
    return True, f"scheduled run for {today}"


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    text = value.strip()
    if not text:
        return None

    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        pass

    try:
        parsed = parsedate_to_datetime(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError, IndexError):
        return None


def strip_html_tags(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    return " ".join(html.unescape(without_tags).split())


def normalize_text(text: str) -> str:
    return " ".join((text or "").split())


def parse_rss_items(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []

    for item in root.findall("./channel/item"):
        title = normalize_text(item.findtext("title", default=""))
        url = normalize_text(item.findtext("link", default=""))
        published_at = parse_datetime(item.findtext("pubDate"))
        summary = strip_html_tags(item.findtext("description", default=""))
        if title and url:
            items.append(UpdateItem(title=title, url=url, published_at=published_at, summary=summary))

    return sorted(
        items,
        key=lambda entry: entry.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def parse_blog_sitemap(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    items: list[UpdateItem] = []

    for node in root.findall("sm:url", namespace):
        url = normalize_text(node.findtext("sm:loc", default="", namespaces=namespace))
        if not re.match(r"^https://cursor\.com/blog/[^/]+$", url):
            continue

        published_at = parse_datetime(node.findtext("sm:lastmod", default="", namespaces=namespace))
        items.append(
            UpdateItem(
                title=humanize_slug(url),
                url=url,
                published_at=published_at,
                summary="",
            )
        )

    return sorted(
        items,
        key=lambda entry: entry.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def humanize_slug(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().title()


def extract_html_title(page_html: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r"<title>(.*?)</title>",
    )

    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        title = normalize_text(strip_html_tags(match.group(1)))
        title = re.sub(r"\s*[·|-]\s*Cursor$", "", title, flags=re.IGNORECASE)
        if title:
            return title

    return ""


def extract_html_description(page_html: str) -> str:
    patterns = (
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']',
    )

    for pattern in patterns:
        match = re.search(pattern, page_html, re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        description = normalize_text(strip_html_tags(match.group(1)))
        if description:
            return description

    return ""


def enrich_blog_item(item: UpdateItem) -> UpdateItem:
    page_html = fetch_text(item.url)
    title = extract_html_title(page_html) or item.title
    summary = extract_html_description(page_html)
    return UpdateItem(title=title, url=item.url, published_at=item.published_at, summary=summary)


def select_dated_updates(
    items: list[UpdateItem],
    seen_urls: list[str],
    now_utc: datetime,
    bootstrap_days: int = 7,
) -> list[UpdateItem]:
    if seen_urls:
        seen = set(seen_urls)
        return [item for item in items if item.url not in seen]

    cutoff = now_utc - timedelta(days=bootstrap_days)
    return [item for item in items if item.published_at and item.published_at >= cutoff]


def parse_x_posts(markdown_text: str) -> list[str]:
    lines = [line.strip() for line in markdown_text.splitlines()]
    try:
        start = lines.index("Cursor’s posts")
    except ValueError:
        start = 0

    image_re = re.compile(r"^\!\[Image \d+\]")
    duration_re = re.compile(r"^\d+:\d+(?::\d+)?$")
    noise_prefixes = ("Title:", "URL Source:", "Published Time:", "Markdown Content:")

    posts: list[str] = []
    current_lines: list[str] = []
    seen = set()
    in_feed = False

    def flush_current() -> None:
        nonlocal current_lines
        text = normalize_text(" ".join(current_lines))
        current_lines = []
        if not text or text in seen:
            return
        seen.add(text)
        posts.append(text)

    def is_profile_marker(line: str) -> bool:
        return (
            line.startswith("[![Image ")
            and "Square profile picture" in line
            and "(https://x.com/cursor_ai" in line
        )

    for line in lines[start + 1 :]:
        if not line or line == "--------------" or line == "Pinned":
            continue
        if line.startswith(noise_prefixes):
            continue
        if is_profile_marker(line):
            if in_feed:
                flush_current()
            in_feed = True
            continue
        if not in_feed:
            continue
        if image_re.match(line) or duration_re.match(line):
            continue
        if line in {"Cursor", "@cursor_ai", "The best way to code with AI."}:
            continue
        current_lines.append(line)

    flush_current()
    return posts


def select_x_updates(posts: list[str], seen_posts: list[str], bootstrap_limit: int = 8) -> list[str]:
    if seen_posts:
        seen = set(seen_posts)
        return [post for post in posts if post not in seen]
    return posts[:bootstrap_limit]


def format_date(dt: datetime | None) -> str:
    if dt is None:
        return "n/a"
    return dt.astimezone(timezone.utc).date().isoformat()


def render_updates_section(name: str, items: list[UpdateItem]) -> list[str]:
    lines = [f"## {name}", ""]
    if not items:
        lines.append("- No new items.")
        lines.append("")
        return lines

    for item in items:
        lines.append(f"- [{item.title}]({item.url}) ({format_date(item.published_at)})")
        if item.summary:
            lines.append(f"  - {item.summary}")
    lines.append("")
    return lines


def render_x_section(posts: list[str]) -> list[str]:
    lines = ["## Official X (@cursor_ai)", ""]
    if not posts:
        lines.append(f"- No new items. Source: {OFFICIAL_X_URL}")
        lines.append("")
        return lines

    for post in posts:
        lines.append(f"- {post}")
    lines.append("")
    lines.append(f"Source: {OFFICIAL_X_URL}")
    lines.append("")
    return lines


def render_errors(errors: list[str]) -> list[str]:
    if not errors:
        return []
    lines = ["## Source errors", ""]
    for error in errors:
        lines.append(f"- {error}")
    lines.append("")
    return lines


def render_report(
    now_utc: datetime,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_posts: list[str],
    errors: list[str],
    force: bool,
) -> str:
    generated_local = now_utc.astimezone(SHANGHAI_TZ)
    lines = [
        "# Cursor updates",
        "",
        f"- Generated at: {generated_local.isoformat()}",
        f"- Mode: {'forced' if force else 'scheduled'}",
        f"- Changelog source: {CHANGELOG_RSS_URL}",
        f"- Blog source: {BLOG_SITEMAP_URL}",
        f"- Official X source: {X_MIRROR_URL}",
        "",
        "## Summary",
        "",
        f"- Changelog: {len(changelog_items)} new item(s)",
        f"- Blog: {len(blog_items)} new item(s)",
        f"- Official X: {len(x_posts)} new item(s)",
        "",
    ]
    lines.extend(render_updates_section("Changelog", changelog_items))
    lines.extend(render_updates_section("Blog", blog_items))
    lines.extend(render_x_section(x_posts))
    lines.extend(render_errors(errors))
    return "\n".join(lines).rstrip() + "\n"


def collect_updates(state: dict[str, Any], now_utc: datetime) -> tuple[list[UpdateItem], list[UpdateItem], list[str], list[str]]:
    errors: list[str] = []

    changelog_items: list[UpdateItem] = []
    try:
        changelog_feed = fetch_text(CHANGELOG_RSS_URL)
        changelog_items = select_dated_updates(
            parse_rss_items(changelog_feed),
            state.get("seen_changelog_urls", []),
            now_utc,
        )
    except (ET.ParseError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        errors.append(f"changelog fetch failed: {exc}")

    blog_items: list[UpdateItem] = []
    try:
        blog_sitemap = fetch_text(BLOG_SITEMAP_URL)
        raw_blog_items = select_dated_updates(
            parse_blog_sitemap(blog_sitemap),
            state.get("seen_blog_urls", []),
            now_utc,
        )
        blog_items = [enrich_blog_item(item) for item in raw_blog_items]
    except (ET.ParseError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        errors.append(f"blog fetch failed: {exc}")

    x_posts: list[str] = []
    try:
        mirrored_x = fetch_text(X_MIRROR_URL)
        x_posts = select_x_updates(parse_x_posts(mirrored_x), state.get("seen_x_posts", []))
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        errors.append(f"x fetch failed: {exc}")

    return changelog_items, blog_items, x_posts, errors


def write_report(text: str, path: Path = REPORT_FILE) -> None:
    path.write_text(text, encoding="utf-8")


def run(force: bool = False, now_utc: datetime | None = None) -> int:
    current_utc = now_utc or datetime.now(timezone.utc)
    current_local = current_utc.astimezone(SHANGHAI_TZ)
    state = load_state()

    should_execute, reason = should_run(current_local, state, force=force)
    print(reason)
    if not should_execute:
        return 0

    changelog_items, blog_items, x_posts, errors = collect_updates(state, current_utc)
    report = render_report(current_utc, changelog_items, blog_items, x_posts, errors, force=force)
    write_report(report)

    state["last_run_date"] = current_local.date().isoformat()
    state["seen_changelog_urls"] = merge_seen(
        [item.url for item in changelog_items],
        state.get("seen_changelog_urls", []),
    )
    state["seen_blog_urls"] = merge_seen(
        [item.url for item in blog_items],
        state.get("seen_blog_urls", []),
    )
    state["seen_x_posts"] = merge_seen(
        x_posts,
        state.get("seen_x_posts", []),
    )
    save_state(state)

    print(
        "wrote report with "
        f"{len(changelog_items)} changelog, "
        f"{len(blog_items)} blog, "
        f"{len(x_posts)} x items"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without the 09:00 Asia/Shanghai gate.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return run(force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
