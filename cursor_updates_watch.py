#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib import error, request
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = BASE_DIR / ".cursor_updates"
STATE_PATH = RUNTIME_DIR / "state.json"
LATEST_REPORT_PATH = RUNTIME_DIR / "latest_report.md"
HISTORY_DIR = RUNTIME_DIR / "history"
SUMMARY_PATH = BASE_DIR / "cursor_updates.md"

TIMEZONE_NAME = "Asia/Shanghai"
SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_ACCOUNT_NAME = "Cursor"
X_ACCOUNT_HANDLE = "@cursor_ai"
X_ACCOUNT_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://r.jina.ai/http://twitter.com/cursor_ai",
)

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) CursorUpdatesWatcher/1.0"
REQUEST_TIMEOUT = 30
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}

TITLE_PATTERNS = (
    re.compile(r'property="og:title" content="([^"]+)"'),
    re.compile(r'name="twitter:title" content="([^"]+)"'),
    re.compile(r"<title>(.*?)</title>", re.DOTALL),
)
DATE_PATTERNS = (
    re.compile(r'"datePublished":"([^"]+)"'),
    re.compile(r'property="article:published_time" content="([^"]+)"'),
)
DESCRIPTION_PATTERNS = (
    re.compile(r'property="og:description" content="([^"]+)"'),
    re.compile(r'name="description" content="([^"]+)"'),
    re.compile(r'"description":"([^"]+)"'),
)


class WatcherError(RuntimeError):
    """Raised when a remote source cannot be parsed or fetched."""


@dataclass(frozen=True)
class UpdateItem:
    title: str
    url: str
    published_at: str | None = None
    summary: str | None = None
    is_new: bool = False


@dataclass(frozen=True)
class XSnapshot:
    posts: list[str]
    mirror_url: str
    mirror_published_time: str | None


def now_in_timezone() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE_NAME))


def fetch_text(url: str, *, timeout: int = REQUEST_TIMEOUT, retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        req = request.Request(
            url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            },
        )
        try:
            with request.urlopen(req, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, "replace")
        except error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_STATUS_CODES or attempt == retries - 1:
                raise
        except (error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries - 1:
                raise
        time.sleep(2**attempt)
    raise WatcherError(f"unable to fetch {url}: {last_error}")


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def should_run(now: datetime, state: dict, *, force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced run"
    if now.hour != 9:
        return False, (
            f"Current {TIMEZONE_NAME} time is {now.strftime('%H:%M')}, "
            "outside the 09:00 window."
        )
    if state.get("last_success_date") == now.date().isoformat():
        return False, f"Already completed the 09:00 check for {now.date().isoformat()}."
    return True, "scheduled window is open"


def parse_sitemap_entries(xml_text: str, section: str) -> list[tuple[str, str | None]]:
    root = ElementTree.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    prefix = f"https://cursor.com/{section}/"
    entries: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for url_node in root.findall("sm:url", namespace):
        loc = url_node.find("sm:loc", namespace)
        if loc is None or not loc.text:
            continue
        value = loc.text.strip()
        if not value.startswith(prefix):
            continue
        if value in seen:
            continue
        lastmod = url_node.find("sm:lastmod", namespace)
        seen.add(value)
        entries.append((value, clean_text(lastmod.text) if lastmod is not None else None))
    return entries


def parse_sitemap_urls(xml_text: str, section: str) -> list[str]:
    return [url for url, _lastmod in parse_sitemap_entries(xml_text, section)]


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def normalize_title(title: str | None) -> str:
    if not title:
        return "Untitled"
    normalized = clean_text(title) or "Untitled"
    normalized = re.sub(r"\s*[|·-]\s*Cursor$", "", normalized).strip()
    return normalized or "Untitled"


def parse_page_metadata(page_text: str) -> tuple[str, str | None, str | None]:
    title = None
    published_at = None
    summary = None

    for pattern in TITLE_PATTERNS:
        match = pattern.search(page_text)
        if match:
            title = match.group(1)
            break

    for pattern in DATE_PATTERNS:
        match = pattern.search(page_text)
        if match:
            published_at = clean_text(match.group(1))
            break

    for pattern in DESCRIPTION_PATTERNS:
        match = pattern.search(page_text)
        if match:
            summary = clean_text(match.group(1))
            break

    return normalize_title(title), published_at, summary


def format_date_label(value: str | None) -> str:
    if not value:
        return "unknown date"
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value


def fetch_cursor_section(section: str, *, max_items: int) -> list[UpdateItem]:
    sitemap_text = fetch_text(SITEMAP_URL)
    sitemap_entries = parse_sitemap_entries(sitemap_text, section)
    items: list[UpdateItem] = []
    for url, lastmod in sitemap_entries:
        page_text = fetch_text(url)
        title, published_at, summary = parse_page_metadata(page_text)
        items.append(
            UpdateItem(
                title=title,
                url=url,
                published_at=published_at or lastmod,
                summary=summary,
            )
        )
        if len(items) >= max_items:
            break
    if not items:
        raise WatcherError(f"no {section} entries found in sitemap")
    return items


def parse_x_mirror(text: str) -> tuple[list[str], str | None]:
    published_match = re.search(r"^Published Time:\s*(.+)$", text, re.MULTILINE)
    published_time = clean_text(published_match.group(1)) if published_match else None

    posts_header_match = re.search(r"Cursor[’']s posts\s*\n-+\s*\n", text)
    if not posts_header_match:
        raise WatcherError("unable to find official X post list in mirrored page")

    content = text[posts_header_match.end() :]
    posts: list[str] = []
    current_lines: list[str] = []
    seen_post_marker = False

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if line.startswith("[![Image") and "Square profile picture" in line:
            if current_lines:
                posts.append(" ".join(current_lines))
                current_lines = []
            seen_post_marker = True
            continue
        if not seen_post_marker:
            continue
        if not line:
            continue
        if line in {"Pinned", "Cursor", "@cursor_ai", "The best way to code with AI."}:
            continue
        if line.startswith("![Image"):
            continue
        if line.startswith("[![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        current_lines.append(line)

    if current_lines:
        posts.append(" ".join(current_lines))

    cleaned_posts: list[str] = []
    seen: set[str] = set()
    for post in posts:
        normalized = clean_text(post)
        if not normalized:
            continue
        if normalized in seen:
            continue
        seen.add(normalized)
        cleaned_posts.append(normalized)

    return cleaned_posts, published_time


def fetch_official_x_posts(*, max_items: int) -> XSnapshot:
    errors: list[str] = []
    for mirror_url in X_MIRROR_URLS:
        try:
            text = fetch_text(mirror_url)
            posts, published_time = parse_x_mirror(text)
            if not posts:
                raise WatcherError("mirror returned no visible posts")
            return XSnapshot(
                posts=posts[:max_items],
                mirror_url=mirror_url,
                mirror_published_time=published_time,
            )
        except Exception as exc:  # pragma: no cover - covered by fallback behavior tests indirectly
            errors.append(f"{mirror_url}: {exc}")
    raise WatcherError("unable to fetch official X posts; " + " | ".join(errors))


def mark_new_entries(items: Iterable[UpdateItem], seen_values: set[str], key: str) -> list[UpdateItem]:
    marked: list[UpdateItem] = []
    for item in items:
        value = getattr(item, key)
        marked.append(replace(item, is_new=value not in seen_values))
    return marked


def build_report(
    *,
    now: datetime,
    mode: str,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_snapshot: XSnapshot,
) -> str:
    lines = [
        "# Cursor updates report",
        "",
        f"- Generated at: {now.isoformat()}",
        f"- Mode: {mode}",
        f"- Timezone gate: {TIMEZONE_NAME} 09:00",
        f"- Official X account: {X_ACCOUNT_NAME} ({X_ACCOUNT_HANDLE})",
        f"- Official X profile: {X_ACCOUNT_URL}",
        f"- X mirror used: {x_snapshot.mirror_url}",
    ]
    if x_snapshot.mirror_published_time:
        lines.append(f"- X mirror published time: {x_snapshot.mirror_published_time}")

    lines.extend(
        [
            "",
            f"## Changelog ({len(changelog_items)})",
            "",
        ]
    )
    for item in changelog_items:
        label = "NEW" if item.is_new else "seen"
        lines.append(f"- [{label}] {format_date_label(item.published_at)} | {item.title} | {item.url}")
        if item.summary:
            lines.append(f"  - {item.summary}")

    lines.extend(["", f"## Blog ({len(blog_items)})", ""])
    for item in blog_items:
        label = "NEW" if item.is_new else "seen"
        lines.append(f"- [{label}] {format_date_label(item.published_at)} | {item.title} | {item.url}")
        if item.summary:
            lines.append(f"  - {item.summary}")

    lines.extend(["", f"## Official X posts ({len(x_snapshot.posts)})", ""])
    for post in x_snapshot.posts:
        lines.append(f"- {post}")

    lines.append("")
    return "\n".join(lines)


def write_report(report_text: str, now: datetime) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT_PATH.write_text(report_text, encoding="utf-8")
    SUMMARY_PATH.write_text(report_text, encoding="utf-8")
    history_path = HISTORY_DIR / f"{now.date().isoformat()}.md"
    history_path.write_text(report_text, encoding="utf-8")


def build_next_state(
    previous_state: dict,
    *,
    now: datetime,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_snapshot: XSnapshot,
) -> dict:
    state = dict(previous_state)
    state["last_success_date"] = now.date().isoformat()
    state["last_success_timestamp"] = now.isoformat()
    state["seen"] = {
        "changelog": [item.url for item in changelog_items],
        "blog": [item.url for item in blog_items],
        "x_posts": list(x_snapshot.posts),
    }
    return state


def run(*, force: bool, max_items: int) -> int:
    current_time = now_in_timezone()
    state = load_state()
    allowed, reason = should_run(current_time, state, force=force)
    if not allowed:
        print(reason)
        return 0

    seen = state.get("seen", {})
    changelog_items = mark_new_entries(
        fetch_cursor_section("changelog", max_items=max_items),
        set(seen.get("changelog", [])),
        "url",
    )
    blog_items = mark_new_entries(
        fetch_cursor_section("blog", max_items=max_items),
        set(seen.get("blog", [])),
        "url",
    )
    x_snapshot = fetch_official_x_posts(max_items=max_items + 3)
    report_text = build_report(
        now=current_time,
        mode="force" if force else "scheduled",
        changelog_items=changelog_items,
        blog_items=blog_items,
        x_snapshot=x_snapshot,
    )
    write_report(report_text, current_time)

    if not force:
        next_state = build_next_state(
            state,
            now=current_time,
            changelog_items=changelog_items,
            blog_items=blog_items,
            x_snapshot=x_snapshot,
        )
        save_state(next_state)

    print(report_text)
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Cursor changelog, blog, and official X updates.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without consuming the scheduled 09:00 slot.",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=5,
        help="Maximum number of changelog and blog items to fetch.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        return run(force=args.force, max_items=max(1, args.max_items))
    except Exception as exc:
        print(f"cursor_updates_watch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
