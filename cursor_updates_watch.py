from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
OFFICIAL_X_URL = "https://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
USER_AGENT = (
    "Mozilla/5.0 (compatible; CursorUpdatesWatch/1.0; "
    "+https://cursor.com)"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html, text/plain;q=0.9, */*;q=0.8",
}
LATEST_ITEMS_LIMIT = 5
NEW_ITEMS_LIMIT = 10
X_POSTS_LIMIT = 8


@dataclass(frozen=True)
class FeedEntry:
    url: str
    lastmod: str | None = None
    title: str | None = None


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def normalize_iso_datetime(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return value.strip()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def parse_sortable_datetime(value: str | None) -> datetime:
    normalized = normalize_iso_datetime(value)
    if not normalized:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_sitemap_entries(xml_text: str, url_prefix: str) -> list[FeedEntry]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[FeedEntry] = []

    for node in root.findall("sm:url", namespace):
        loc = (node.findtext("sm:loc", default="", namespaces=namespace) or "").strip()
        if not loc.startswith(url_prefix):
            continue
        lastmod = node.findtext("sm:lastmod", default=None, namespaces=namespace)
        entries.append(FeedEntry(url=loc, lastmod=normalize_iso_datetime(lastmod)))

    entries.sort(key=lambda item: (parse_sortable_datetime(item.lastmod), item.url), reverse=True)
    return entries


def strip_tags(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value)


def clean_title(value: str) -> str:
    title = unescape(strip_tags(value))
    title = re.sub(r"\s+", " ", title).strip()
    for suffix in (" - Cursor", " | Cursor"):
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
    return title


def fallback_title_from_url(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if not slug:
        return url
    words = [part for part in slug.split("-") if part]
    if not words:
        return slug
    return " ".join(word.capitalize() for word in words)


def extract_title_from_html(html_text: str, url: str) -> str:
    patterns = [
        r"<h1[^>]*>(.*?)</h1>",
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](.*?)["\']',
        r"<title>(.*?)</title>",
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        title = clean_title(match.group(1))
        if title:
            return title
    return fallback_title_from_url(url)


def resolve_titles(entries: list[FeedEntry], title_cache: dict[str, str]) -> list[FeedEntry]:
    resolved: list[FeedEntry] = []
    for entry in entries:
        title = title_cache.get(entry.url)
        if not title:
            page_html = fetch_text(entry.url)
            title = extract_title_from_html(page_html, entry.url)
            title_cache[entry.url] = title
        resolved.append(FeedEntry(url=entry.url, lastmod=entry.lastmod, title=title))
    return resolved


def markdown_to_plain_text(value: str) -> str:
    value = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def extract_x_posts(markdown_text: str, limit: int = X_POSTS_LIMIT) -> dict[str, Any]:
    lines = markdown_text.splitlines()
    title_match = re.search(r"^Title:\s*(.+)$", markdown_text, flags=re.MULTILINE)
    published_time_match = re.search(
        r"^Published Time:\s*(.+)$", markdown_text, flags=re.MULTILINE
    )
    source_match = re.search(r"^URL Source:\s*(.+)$", markdown_text, flags=re.MULTILINE)

    started = False
    current_block: list[str] = []
    posts: list[str] = []
    seen_posts: set[str] = set()

    def flush_current_block() -> None:
        nonlocal current_block
        if not current_block:
            return
        text = markdown_to_plain_text(" ".join(current_block))
        current_block = []
        if not text:
            return
        if text in seen_posts:
            return
        seen_posts.add(text)
        posts.append(text)

    for raw_line in lines:
        line = raw_line.strip()
        if not started:
            if line == "Cursor’s posts":
                started = True
            continue

        if not line:
            flush_current_block()
            continue

        if line in {"--------------", "Pinned", "Cursor", "@cursor_ai"}:
            flush_current_block()
            continue

        if line.startswith("[![Image") or line.startswith("![Image"):
            flush_current_block()
            continue

        if line == "The best way to code with AI.":
            continue

        if re.fullmatch(r"\d+:\d+", line):
            continue

        current_block.append(line)

    flush_current_block()

    account = title_match.group(1) if title_match else "Cursor (@cursor_ai)"
    if account.endswith(" / X"):
        account = account[:-4]

    source_url = source_match.group(1) if source_match else X_MIRROR_URL
    if source_url.startswith("http://x.com/"):
        source_url = "https://" + source_url[len("http://") :]

    return {
        "account": account,
        "profile_url": OFFICIAL_X_URL,
        "source_url": source_url,
        "mirror_url": X_MIRROR_URL,
        "published_time": published_time_match.group(1) if published_time_match else None,
        "posts": posts[:limit],
    }


def iso_date_in_timezone(now_utc: datetime, timezone_name: str) -> str:
    return now_utc.astimezone(ZoneInfo(timezone_name)).date().isoformat()


def should_run_now(
    now_utc: datetime,
    timezone_name: str,
    state: dict[str, Any],
    force: bool,
) -> tuple[bool, str]:
    if force:
        return True, "force run requested"

    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    if local_now.hour != 9:
        return False, (
            f"scheduled check runs at 09:00 {timezone_name}; "
            f"current local time is {local_now.strftime('%H:%M:%S')}"
        )

    last_scheduled_date = state.get("last_scheduled_date")
    if last_scheduled_date == local_now.date().isoformat():
        return False, f"scheduled run for {last_scheduled_date} already completed"

    return True, "within scheduled window"


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def build_source_snapshot(
    entries: list[FeedEntry],
    previous_source_state: dict[str, Any] | None,
) -> dict[str, Any]:
    previous_source_state = previous_source_state or {}
    previous_seen_urls = set(previous_source_state.get("seen_urls", []))
    title_cache = dict(previous_source_state.get("title_cache", {}))
    is_initial_run = not previous_seen_urls

    latest_entries = resolve_titles(entries[:LATEST_ITEMS_LIMIT], title_cache)
    new_candidates = [] if is_initial_run else [entry for entry in entries if entry.url not in previous_seen_urls]
    new_entries = resolve_titles(new_candidates[:NEW_ITEMS_LIMIT], title_cache)

    return {
        "initial_run": is_initial_run,
        "seen_urls": [entry.url for entry in entries],
        "title_cache": title_cache,
        "latest": [asdict(entry) for entry in latest_entries],
        "new": [asdict(entry) for entry in new_entries],
    }


def build_x_snapshot(markdown_text: str, previous_source_state: dict[str, Any] | None) -> dict[str, Any]:
    previous_source_state = previous_source_state or {}
    parsed = extract_x_posts(markdown_text)
    previous_posts = set(previous_source_state.get("seen_posts", []))
    is_initial_run = not previous_posts
    new_posts = [] if is_initial_run else [post for post in parsed["posts"] if post not in previous_posts]

    return {
        "initial_run": is_initial_run,
        "account": parsed["account"],
        "profile_url": parsed["profile_url"],
        "source_url": parsed["source_url"],
        "mirror_url": parsed["mirror_url"],
        "published_time": parsed["published_time"],
        "posts": parsed["posts"],
        "new_posts": new_posts,
        "seen_posts": parsed["posts"],
    }


def format_feed_entry(entry: dict[str, Any]) -> str:
    timestamp = entry.get("lastmod") or "unknown date"
    date_text = timestamp.split("T", 1)[0]
    title = entry.get("title") or fallback_title_from_url(entry["url"])
    return f"- {date_text} | {title} | {entry['url']}"


def build_report(
    now_utc: datetime,
    timezone_name: str,
    force: bool,
    changelog_snapshot: dict[str, Any],
    blog_snapshot: dict[str, Any],
    x_snapshot: dict[str, Any],
) -> str:
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    lines = [
        "# Cursor updates report",
        "",
        f"- Run mode: {'forced verification' if force else 'scheduled check'}",
        f"- Run time (UTC): {now_utc.isoformat()}",
        f"- Run time ({timezone_name}): {local_now.isoformat()}",
        "",
        "## Summary",
    ]

    if changelog_snapshot["initial_run"] and blog_snapshot["initial_run"] and x_snapshot["initial_run"]:
        lines.append("- No previous state was found, so this run established a baseline snapshot.")
    else:
        lines.extend(
            [
                f"- New changelog items: {len(changelog_snapshot['new'])}",
                f"- New blog posts: {len(blog_snapshot['new'])}",
                f"- New official X posts: {len(x_snapshot['new_posts'])}",
            ]
        )

    if changelog_snapshot["new"]:
        lines.extend(["", "## New changelog items"])
        lines.extend(format_feed_entry(entry) for entry in changelog_snapshot["new"])

    if blog_snapshot["new"]:
        lines.extend(["", "## New blog posts"])
        lines.extend(format_feed_entry(entry) for entry in blog_snapshot["new"])

    if x_snapshot["new_posts"]:
        lines.extend(["", "## New official X posts"])
        lines.extend(f'- "{post}"' for post in x_snapshot["new_posts"])

    lines.extend(["", "## Latest changelog snapshot"])
    lines.extend(format_feed_entry(entry) for entry in changelog_snapshot["latest"])

    lines.extend(["", "## Latest blog snapshot"])
    lines.extend(format_feed_entry(entry) for entry in blog_snapshot["latest"])

    lines.extend(
        [
            "",
            "## Latest official X snapshot",
            f"- Account: {x_snapshot['account']}",
            f"- Profile URL: {x_snapshot['profile_url']}",
            f"- Mirror URL: {x_snapshot['mirror_url']}",
            f"- Mirror published time: {x_snapshot['published_time'] or 'unknown'}",
        ]
    )
    lines.extend(f'- "{post}"' for post in x_snapshot["posts"])

    return "\n".join(lines) + "\n"


def write_report(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def parse_now(value: str | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    parsed = datetime.fromisoformat(cleaned)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def run_watch(state_dir: Path, timezone_name: str, force: bool, now_utc: datetime) -> tuple[bool, str]:
    state_path = state_dir / "state.json"
    latest_report_path = state_dir / "latest_report.md"
    history_dir = state_dir / "history"

    state = load_state(state_path)
    should_run, reason = should_run_now(now_utc, timezone_name, state, force)
    if not should_run:
        message = (
            f"Skipping Cursor updates check: {reason}. "
            f"UTC now: {now_utc.isoformat()}."
        )
        print(message)
        return False, message

    sitemap_xml = fetch_text(SITEMAP_URL)
    changelog_entries = parse_sitemap_entries(sitemap_xml, "https://cursor.com/changelog/")
    blog_entries = parse_sitemap_entries(sitemap_xml, "https://cursor.com/blog/")
    x_markdown = fetch_text(X_MIRROR_URL)

    previous_sources = state.get("sources", {})
    changelog_snapshot = build_source_snapshot(
        changelog_entries,
        previous_sources.get("changelog"),
    )
    blog_snapshot = build_source_snapshot(blog_entries, previous_sources.get("blog"))
    x_snapshot = build_x_snapshot(x_markdown, previous_sources.get("x"))

    report = build_report(
        now_utc=now_utc,
        timezone_name=timezone_name,
        force=force,
        changelog_snapshot=changelog_snapshot,
        blog_snapshot=blog_snapshot,
        x_snapshot=x_snapshot,
    )

    write_report(latest_report_path, report)

    updated_state = dict(state)
    updated_state["timezone"] = timezone_name
    updated_state["last_run_utc"] = now_utc.isoformat()
    updated_state["sources"] = {
        "changelog": changelog_snapshot,
        "blog": blog_snapshot,
        "x": x_snapshot,
    }

    if force:
        updated_state["last_forced_run_utc"] = now_utc.isoformat()
    else:
        updated_state["last_scheduled_run_utc"] = now_utc.isoformat()
        updated_state["last_scheduled_date"] = iso_date_in_timezone(now_utc, timezone_name)
        history_path = history_dir / f"{updated_state['last_scheduled_date']}.md"
        write_report(history_path, report)

    save_state(state_path, updated_state)
    print(report)
    return True, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Check the latest official Cursor changelog, blog, and X updates. "
            "Scheduled runs only execute at 09:00 Asia/Shanghai unless --force is used."
        )
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".cursor_updates"),
        help="Directory used for watcher state and generated reports.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help="Local timezone used for the 09:00 schedule gate.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Optional ISO-8601 timestamp for deterministic runs and tests.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the 09:00 gate and duplicate-run protection.",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    now_utc = parse_now(args.now)
    try:
        run_watch(
            state_dir=args.state_dir,
            timezone_name=args.timezone,
            force=args.force,
            now_utc=now_utc,
        )
    except Exception as exc:
        print(f"Cursor updates check failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
