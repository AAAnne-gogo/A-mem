#!/usr/bin/env python3
"""Check Cursor changelog, blog, and official X updates.

This script is designed for hourly automation triggers. By default it only
performs the network check during 09:00 in the configured timezone and writes
runtime state under .cursor_updates/.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_URL = "https://cursor.com/blog"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URL = "https://r.jina.ai/http://https://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_HOUR = 9
DEFAULT_TOP = 5
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


@dataclass
class UpdateEntry:
    date_iso: str
    date_label: str
    title: str
    url: str
    summary: str = ""
    category: str = ""


@dataclass
class XSnapshot:
    account_name: str
    handle: str
    profile_url: str
    mirror_url: str
    published_time: str
    posts: list[str]
    snapshot_hash: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Cursor changelog, blog, and official X updates."
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone used for the daily gate (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--run-hour",
        type=int,
        default=DEFAULT_RUN_HOUR,
        help=f"Local hour to run the daily check (default: {DEFAULT_RUN_HOUR}).",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"Number of recent entries to keep per source (default: {DEFAULT_TOP}).",
    )
    parser.add_argument(
        "--state-dir",
        default=".cursor_updates",
        help="Directory for runtime state and reports.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the time gate and run immediately.",
    )
    return parser.parse_args()


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error for {url}: {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc


def normalize_text(text: str) -> str:
    cleaned = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = unescape(cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def to_absolute_url(path_or_url: str) -> str:
    if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
        return path_or_url
    return f"https://cursor.com{path_or_url}"


def parse_cursor_changelog(html: str, limit: int) -> list[UpdateEntry]:
    pattern = re.compile(
        r'<a class="hover:text-theme-text inline-flex items-center" '
        r'href="(?P<url>/changelog/[^"]+)">.*?<time dateTime="(?P<date_iso>[^"]+)"'
        r'[^>]*>(?P<date_label>[^<]+)</time></a>.*?'
        r'<h1[^>]*>\s*<a[^>]*href="(?P=url)">(?P<title>[^<]+)</a>\s*</h1>.*?'
        r'<div class="prose prose--block">(?P<body>.*?)</div>',
        flags=re.S,
    )
    entries: list[UpdateEntry] = []
    seen_urls: set[str] = set()
    for match in pattern.finditer(html):
        url = to_absolute_url(match.group("url"))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        summary_match = re.search(r"<p>(.*?)</p>", match.group("body"), flags=re.S)
        summary = normalize_text(summary_match.group(1)) if summary_match else ""
        entries.append(
            UpdateEntry(
                date_iso=match.group("date_iso").split("T", 1)[0],
                date_label=normalize_text(match.group("date_label")),
                title=normalize_text(match.group("title")),
                url=url,
                summary=summary,
            )
        )
        if len(entries) >= limit:
            break
    if not entries:
        raise RuntimeError("Unable to parse Cursor changelog listing.")
    return entries


def parse_cursor_blog(html: str, limit: int) -> list[UpdateEntry]:
    pattern = re.compile(
        r'<article class="flex grow-1 flex-col mb-g1">.*?'
        r'<a class="card card--text.*?" href="(?P<url>/blog/(?!topic/)[^"]+)">.*?'
        r'<p class="type-base text-theme-text text-pretty">(?P<title>.*?)</p>.*?'
        r'<p class="type-base text-theme-text-sec text-pretty">(?P<summary>.*?)</p>.*?'
        r'<span class="capitalize">(?P<category>.*?)</span>'
        r'<time dateTime="(?P<date_iso>[^"]+)"[^>]*>(?P<date_label>[^<]+)</time>',
        flags=re.S,
    )
    entries: list[UpdateEntry] = []
    seen_urls: set[str] = set()
    for match in pattern.finditer(html):
        url = to_absolute_url(match.group("url"))
        if url in seen_urls:
            continue
        seen_urls.add(url)
        entries.append(
            UpdateEntry(
                date_iso=match.group("date_iso").split("T", 1)[0],
                date_label=normalize_text(match.group("date_label")),
                title=normalize_text(match.group("title")),
                url=url,
                summary=normalize_text(match.group("summary")),
                category=normalize_text(match.group("category")).rstrip(" ·."),
            )
        )
        if len(entries) >= limit:
            break
    if not entries:
        raise RuntimeError("Unable to parse Cursor blog listing.")
    return entries


def normalize_x_post(block: str) -> str:
    lines = [line.strip() for line in block.splitlines()]
    kept: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("[![Image") or line.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        if line in {"Pinned", "Show more"}:
            continue
        kept.append(line)
    text = " ".join(kept)
    return re.sub(r"\s+", " ", text).strip()


def unique_in_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def parse_x_snapshot(markdown: str, limit: int) -> XSnapshot:
    source_match = re.search(r"URL Source:\s*(.+)", markdown)
    published_match = re.search(r"Published Time:\s*(.+)", markdown)
    if not source_match or not published_match:
        raise RuntimeError("Unable to parse Cursor official X snapshot metadata.")

    section_match = re.search(r"Cursor[’']s posts\s*-+\s*(.*)", markdown, flags=re.S)
    if not section_match:
        raise RuntimeError("Unable to locate Cursor posts in X snapshot.")

    posts_section = section_match.group(1)
    block_pattern = re.compile(
        r"\]\(https://x\.com/cursor_ai[^)]*\)\s*\n\n(?P<block>.*?)(?=\n\n(?:\[!\[Image|!\[Image)|\Z)",
        flags=re.S,
    )
    post_chunks: list[str] = []
    for match in block_pattern.finditer(posts_section):
        block = match.group("block")
        for chunk in re.split(r"\n\s*\n", block):
            post_chunks.append(normalize_x_post(chunk))
    posts = unique_in_order(post_chunks)
    posts = [post for post in posts if post and post not in {"Cursor", "@cursor_ai"}][:limit]
    if not posts:
        raise RuntimeError("Unable to extract visible Cursor posts from X snapshot.")

    snapshot_hash = hashlib.sha256("\n".join(posts).encode("utf-8")).hexdigest()
    return XSnapshot(
        account_name="Cursor",
        handle="@cursor_ai",
        profile_url=X_PROFILE_URL,
        mirror_url=X_MIRROR_URL,
        published_time=published_match.group(1).strip(),
        posts=posts,
        snapshot_hash=snapshot_hash,
    )


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, data: dict) -> None:
    state_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def ensure_state_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def format_entry(entry: UpdateEntry, include_summary: bool = False) -> str:
    line = f"- {entry.date_iso} | {entry.title} | {entry.url}"
    if include_summary and entry.summary:
        line += f"\n  - {entry.summary}"
    return line


def build_report(
    *,
    checked_at_utc: datetime,
    local_now: datetime,
    forced: bool,
    changelog_entries: list[UpdateEntry],
    blog_entries: list[UpdateEntry],
    x_snapshot: XSnapshot,
    new_changelog: list[UpdateEntry],
    new_blog: list[UpdateEntry],
    x_changed: bool,
) -> str:
    lines = [
        "# Cursor updates report",
        "",
        f"- Checked at (UTC): {checked_at_utc.isoformat()}",
        f"- Local time ({local_now.tzinfo}): {local_now.isoformat()}",
        f"- Run mode: {'forced' if forced else 'scheduled'}",
        "",
        "## Summary",
        f"- New changelog entries: {len(new_changelog)}",
        f"- New blog posts: {len(new_blog)}",
        f"- Official X snapshot changed: {'yes' if x_changed else 'no'}",
        "",
        "## New changelog entries",
    ]
    if new_changelog:
        lines.extend(format_entry(entry, include_summary=True) for entry in new_changelog)
    else:
        lines.append("- No new changelog entries detected.")

    lines.extend(["", "## Latest changelog snapshot"])
    lines.extend(format_entry(entry, include_summary=True) for entry in changelog_entries)

    lines.extend(["", "## New blog posts"])
    if new_blog:
        lines.extend(format_entry(entry, include_summary=True) for entry in new_blog)
    else:
        lines.append("- No new blog posts detected.")

    lines.extend(["", "## Latest blog snapshot"])
    for entry in blog_entries:
        line = format_entry(entry, include_summary=True)
        if entry.category:
            line += f"\n  - Category: {entry.category}"
        lines.append(line)

    lines.extend(
        [
            "",
            "## Official X snapshot",
            f"- Account: {x_snapshot.account_name} ({x_snapshot.handle})",
            f"- Profile URL: {x_snapshot.profile_url}",
            f"- Public mirror used: {x_snapshot.mirror_url}",
            f"- Mirror published time: {x_snapshot.published_time}",
            f"- Snapshot changed: {'yes' if x_changed else 'no'}",
            "- Visible posts snapshot:",
        ]
    )
    lines.extend(f"  - {post}" for post in x_snapshot.posts)
    lines.append("")
    return "\n".join(lines)


def build_skip_report(
    *,
    checked_at_utc: datetime,
    local_now: datetime,
    reason: str,
    forced: bool,
) -> str:
    return "\n".join(
        [
            "# Cursor updates report",
            "",
            f"- Checked at (UTC): {checked_at_utc.isoformat()}",
            f"- Local time ({local_now.tzinfo}): {local_now.isoformat()}",
            f"- Run mode: {'forced' if forced else 'scheduled'}",
            f"- Status: skipped",
            f"- Reason: {reason}",
            "",
        ]
    )


def main() -> int:
    args = parse_args()
    if not 0 <= args.run_hour <= 23:
        print("--run-hour must be between 0 and 23.", file=sys.stderr)
        return 2

    checked_at = now_utc()
    local_now = checked_at.astimezone(ZoneInfo(args.timezone))
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    report_path = state_dir / "latest_report.md"

    ensure_state_dir(state_dir)
    state = load_state(state_path)
    local_date = local_now.date().isoformat()

    if not args.force and local_now.hour != args.run_hour:
        report = build_skip_report(
            checked_at_utc=checked_at,
            local_now=local_now,
            reason=f"outside scheduled hour {args.run_hour:02d}:00",
            forced=False,
        )
        report_path.write_text(report, encoding="utf-8")
        print(report)
        return 0

    if not args.force and state.get("last_checked_local_date") == local_date:
        report = build_skip_report(
            checked_at_utc=checked_at,
            local_now=local_now,
            reason="already checked today",
            forced=False,
        )
        report_path.write_text(report, encoding="utf-8")
        print(report)
        return 0

    changelog_entries = parse_cursor_changelog(fetch_text(CHANGELOG_URL), args.top)
    blog_entries = parse_cursor_blog(fetch_text(BLOG_URL), args.top)
    x_snapshot = parse_x_snapshot(fetch_text(X_MIRROR_URL), args.top)

    previous_changelog_urls = set(state.get("latest_changelog_urls", []))
    previous_blog_urls = set(state.get("latest_blog_urls", []))
    previous_x_hash = state.get("x_snapshot_hash")

    new_changelog = [
        entry for entry in changelog_entries if entry.url not in previous_changelog_urls
    ]
    new_blog = [entry for entry in blog_entries if entry.url not in previous_blog_urls]
    x_changed = previous_x_hash is not None and previous_x_hash != x_snapshot.snapshot_hash

    report = build_report(
        checked_at_utc=checked_at,
        local_now=local_now,
        forced=args.force,
        changelog_entries=changelog_entries,
        blog_entries=blog_entries,
        x_snapshot=x_snapshot,
        new_changelog=new_changelog,
        new_blog=new_blog,
        x_changed=x_changed,
    )
    report_path.write_text(report, encoding="utf-8")
    print(report)

    state_payload = {
        "last_checked_at_utc": checked_at.isoformat(),
        "last_checked_local_date": local_date,
        "timezone": args.timezone,
        "run_hour": args.run_hour,
        "top": args.top,
        "latest_changelog_urls": [entry.url for entry in changelog_entries],
        "latest_changelog_entries": [asdict(entry) for entry in changelog_entries],
        "latest_blog_urls": [entry.url for entry in blog_entries],
        "latest_blog_entries": [asdict(entry) for entry in blog_entries],
        "x_snapshot_hash": x_snapshot.snapshot_hash,
        "x_snapshot": asdict(x_snapshot),
    }
    save_state(state_path, state_payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
