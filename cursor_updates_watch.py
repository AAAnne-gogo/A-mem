#!/usr/bin/env python3
"""Track Cursor changelog, blog, and official X updates.

The watcher is designed for hourly automation triggers. By default it only
performs a live fetch during the 09:00 hour in Asia/Shanghai and records a
single successful scheduled run per local date. Use --force to bypass the
time/date gate for verification.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://www.x.com/cursor_ai",
)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
STATE_DIR_NAME = ".cursor_updates"
REPORT_TIME_FORMAT = "%Y-%m-%d %H:%M:%S %Z"
HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Cursor Updates Watcher)",
    "Accept": "text/plain,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
MAX_ITEMS = 10


@dataclass
class FeedEntry:
    title: str
    url: str
    date: str


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_runtime_paths(workspace: Path) -> tuple[Path, Path, Path]:
    state_dir = workspace / STATE_DIR_NAME
    history_dir = state_dir / "history"
    state_dir.mkdir(exist_ok=True)
    history_dir.mkdir(exist_ok=True)
    return state_dir, state_dir / "state.json", history_dir


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict) -> None:
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def request_text(url: str, timeout: int = 30, retries: int = 4) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as error:
            last_error = error
            if error.code not in {403, 429, 500, 502, 503, 504} or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt == retries - 1:
                raise
        time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def parse_iso_datetime(value: str) -> datetime | None:
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def format_date(value: str | None, fallback: str | None = None) -> str:
    for candidate in (value, fallback):
        if candidate is None:
            continue
        parsed = parse_iso_datetime(candidate)
        if parsed is not None:
            return parsed.date().isoformat()
        match = re.search(r"(\d{2})-(\d{2})-(\d{2})", candidate)
        if match:
            month, day, year = match.groups()
            return f"20{year}-{month}-{day}"
    return "unknown"


def clean_title(raw_title: str) -> str:
    title = unescape(raw_title).strip()
    for suffix in (" · Cursor", " | Cursor", " - Cursor"):
        if title.endswith(suffix):
            return title[: -len(suffix)].strip()
    return title


def extract_html_title(html: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return "Untitled"
    return clean_title(re.sub(r"\s+", " ", match.group(1)))


def extract_page_date(html: str, fallback: str | None = None) -> str:
    patterns = (
        r"<time[^>]*datetime=\"([^\"]+)\"",
        r"datePublished\":\"([^\"]+)\"",
        r"article:published_time\" content=\"([^\"]+)\"",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return format_date(match.group(1), fallback)
    return format_date(fallback)


def parse_sitemap(xml_text: str, section: str) -> list[dict[str, str]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    results: list[dict[str, str]] = []
    for url_node in root.findall("sm:url", namespace):
        loc_node = url_node.find("sm:loc", namespace)
        if loc_node is None or not loc_node.text:
            continue
        loc = loc_node.text.strip()
        if f"/{section}/" not in loc:
            continue
        lastmod_node = url_node.find("sm:lastmod", namespace)
        lastmod = lastmod_node.text.strip() if lastmod_node is not None and lastmod_node.text else ""
        results.append({"loc": loc, "lastmod": lastmod})
    results.sort(key=lambda item: parse_iso_datetime(item["lastmod"]) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return results


def fetch_feed_entries(fetch_text: Callable[[str], str], sitemap_text: str, section: str, limit: int = MAX_ITEMS) -> list[FeedEntry]:
    entries: list[FeedEntry] = []
    for item in parse_sitemap(sitemap_text, section)[:limit]:
        page_html = fetch_text(item["loc"])
        entries.append(
            FeedEntry(
                title=extract_html_title(page_html),
                url=item["loc"],
                date=extract_page_date(page_html, item["lastmod"]),
            )
        )
    return entries


def is_markdown_image_line(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("[![Image") or stripped.startswith("![Image")


def extract_x_posts(x_text: str, limit: int = MAX_ITEMS) -> tuple[str, str, list[str]]:
    published_time = ""
    account = "Cursor (@cursor_ai)"
    lines = x_text.splitlines()
    for line in lines:
        if line.startswith("Published Time:"):
            published_time = line.split(":", 1)[1].strip()
            break

    posts: list[str] = []
    seen: set[str] = set()
    in_posts_section = False
    for raw_line in lines:
        line = raw_line.strip()
        if line == "Cursor’s posts":
            in_posts_section = True
            continue
        if not in_posts_section or not line:
            continue
        if line in {"--------------", "Pinned", "Cursor", "@cursor_ai"}:
            continue
        if is_markdown_image_line(line):
            continue
        if re.fullmatch(r"\d+:\d+(?::\d+)?", line):
            continue
        if len(line) < 8:
            continue
        if line not in seen:
            seen.add(line)
            posts.append(line)
        if len(posts) >= limit:
            break
    return account, published_time, posts


def should_run(force: bool, now_local: datetime, state: dict) -> tuple[bool, str]:
    if force:
        return True, "forced run"
    if now_local.hour != 9:
        return False, f"outside 09:00 Asia/Shanghai window (current hour: {now_local.hour:02d})"
    last_scheduled_date = state.get("last_successful_scheduled_date")
    if last_scheduled_date == now_local.date().isoformat():
        return False, f"already completed scheduled run for {last_scheduled_date}"
    return True, "scheduled run allowed"


def diff_entries(current: list[FeedEntry], previous_urls: list[str]) -> list[FeedEntry]:
    previous = set(previous_urls)
    return [entry for entry in current if entry.url not in previous]


def diff_posts(current: list[str], previous_posts: list[str]) -> list[str]:
    previous = set(previous_posts)
    return [post for post in current if post not in previous]


def render_entry_list(entries: list[FeedEntry]) -> list[str]:
    if not entries:
        return ["- None"]
    return [f"- {entry.date} | {entry.title} | {entry.url}" for entry in entries]


def render_post_list(posts: list[str]) -> list[str]:
    if not posts:
        return ["- None"]
    return [f'- "{post}"' for post in posts]


def build_report(
    *,
    checked_at_utc: datetime,
    run_mode: str,
    changelog_entries: list[FeedEntry],
    blog_entries: list[FeedEntry],
    x_account: str,
    x_published_time: str,
    x_posts: list[str],
    new_changelog: list[FeedEntry],
    new_blog: list[FeedEntry],
    new_x_posts: list[str],
) -> str:
    checked_local = checked_at_utc.astimezone(LOCAL_TZ).strftime(REPORT_TIME_FORMAT)
    lines = [
        "# Cursor updates report",
        "",
        f"- Checked at (UTC): {checked_at_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Checked at (Asia/Shanghai): {checked_local}",
        f"- Run mode: {run_mode}",
        "",
        "## New since previous snapshot",
        "",
        "### Changelog",
        *render_entry_list(new_changelog),
        "",
        "### Blog",
        *render_entry_list(new_blog),
        "",
        "### Official X posts",
        *render_post_list(new_x_posts),
        "",
        "## Latest changelog snapshot",
        *render_entry_list(changelog_entries),
        "",
        "## Latest blog snapshot",
        *render_entry_list(blog_entries),
        "",
        "## Official X snapshot",
        f"- Account: {x_account}",
        f"- Mirror published time: {x_published_time or 'unknown'}",
        *render_post_list(x_posts),
        "",
    ]
    return "\n".join(lines)


def run(force: bool = False, workspace: Path | None = None, fetch_text: Callable[[str], str] = request_text) -> dict:
    workspace = workspace or Path(__file__).resolve().parent
    state_dir, state_path, history_dir = ensure_runtime_paths(workspace)
    latest_report_path = state_dir / "latest_report.md"
    state = load_state(state_path)

    checked_at_utc = utc_now()
    checked_local = checked_at_utc.astimezone(LOCAL_TZ)
    should_fetch, reason = should_run(force, checked_local, state)
    if not should_fetch:
        return {
            "status": "skipped",
            "reason": reason,
            "checked_at_utc": checked_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }

    sitemap_text = fetch_text(SITEMAP_URL)
    changelog_entries = fetch_feed_entries(fetch_text, sitemap_text, "changelog")
    blog_entries = fetch_feed_entries(fetch_text, sitemap_text, "blog")

    x_text = ""
    for x_url in X_MIRROR_URLS:
        try:
            x_text = fetch_text(x_url)
            break
        except Exception:
            continue
    if not x_text:
        raise RuntimeError("Unable to fetch official Cursor X mirror from all configured URLs.")
    x_account, x_published_time, x_posts = extract_x_posts(x_text)

    previous_latest = state.get("latest", {})
    new_changelog = diff_entries(changelog_entries, previous_latest.get("changelog_urls", []))
    new_blog = diff_entries(blog_entries, previous_latest.get("blog_urls", []))
    new_x_posts = diff_posts(x_posts, previous_latest.get("x_posts", []))

    run_mode = "forced verification" if force else "scheduled"
    report = build_report(
        checked_at_utc=checked_at_utc,
        run_mode=run_mode,
        changelog_entries=changelog_entries,
        blog_entries=blog_entries,
        x_account=x_account,
        x_published_time=x_published_time,
        x_posts=x_posts,
        new_changelog=new_changelog,
        new_blog=new_blog,
        new_x_posts=new_x_posts,
    )
    latest_report_path.write_text(report + "\n", encoding="utf-8")
    history_path = history_dir / f"{checked_local.date().isoformat()}.md"
    history_path.write_text(report + "\n", encoding="utf-8")

    next_state = {
        "last_checked_utc": checked_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "latest": {
            "changelog": [asdict(entry) for entry in changelog_entries],
            "changelog_urls": [entry.url for entry in changelog_entries],
            "blog": [asdict(entry) for entry in blog_entries],
            "blog_urls": [entry.url for entry in blog_entries],
            "x_account": x_account,
            "x_mirror_published_time": x_published_time,
            "x_posts": x_posts,
        },
    }
    if not force:
        next_state["last_successful_scheduled_date"] = checked_local.date().isoformat()
    elif "last_successful_scheduled_date" in state:
        next_state["last_successful_scheduled_date"] = state["last_successful_scheduled_date"]
    save_state(state_path, next_state)

    return {
        "status": "fetched",
        "reason": reason,
        "checked_at_utc": checked_at_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "report_path": str(latest_report_path),
        "history_path": str(history_path),
        "new_counts": {
            "changelog": len(new_changelog),
            "blog": len(new_blog),
            "x_posts": len(new_x_posts),
        },
    }


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X updates.")
    parser.add_argument("--force", action="store_true", help="Bypass the 09:00 Asia/Shanghai scheduled gate.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    try:
        result = run(force=args.force)
    except Exception as error:  # pragma: no cover - exercised via CLI
        print(f"cursor updates watcher failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
