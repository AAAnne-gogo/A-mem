#!/usr/bin/env python3
"""Check Cursor changelog, blog, and official X updates."""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from html import unescape
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
)
CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_URL = "https://cursor.com/blog"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URL = "https://r.jina.ai/http://https://x.com/cursor_ai"


@dataclass
class UpdateEntry:
    title: str
    url: str
    published_at: str
    summary: str


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain,text/html,*/*",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def clean_html_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return normalize_whitespace(value)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_changelog_entries(html_text: str, limit: int) -> list[UpdateEntry]:
    entries: list[UpdateEntry] = []
    for block in re.findall(r"<article>(.*?)</article>", html_text, re.S):
        url_match = re.search(r'href="(/changelog/[^"]+)"', block)
        date_match = re.search(r'<time[^>]*dateTime="([^"]+)"', block)
        title_match = re.search(r"<h1[^>]*>.*?<a[^>]*>(.*?)</a>", block, re.S)
        summary_match = re.search(
            r'<div class="prose prose--block"><p>(.*?)</p>', block, re.S
        )
        if not (url_match and date_match and title_match):
            continue

        entries.append(
            UpdateEntry(
                title=clean_html_text(title_match.group(1)),
                url=f"https://cursor.com{url_match.group(1)}",
                published_at=date_match.group(1),
                summary=clean_html_text(summary_match.group(1))
                if summary_match
                else "",
            )
        )
        if len(entries) >= limit:
            break
    return entries


def parse_blog_entries(html_text: str, limit: int) -> list[UpdateEntry]:
    entries: list[UpdateEntry] = []
    pattern = re.compile(
        r'<article class="flex grow-1 flex-col mb-g1">(.*?)</article>', re.S
    )
    for block in pattern.findall(html_text):
        url_match = re.search(r'href="(/blog/(?!topic)[^"]+)"', block)
        title_match = re.search(
            r'<p class="type-base text-theme-text text-pretty">(.*?)</p>',
            block,
            re.S,
        )
        summary_match = re.search(
            r'<p class="type-base text-theme-text-sec text-pretty">(.*?)</p>',
            block,
            re.S,
        )
        date_match = re.search(r'<time[^>]*dateTime="([^"]+)"', block)
        if not (url_match and title_match and date_match):
            continue

        entries.append(
            UpdateEntry(
                title=clean_html_text(title_match.group(1)),
                url=f"https://cursor.com{url_match.group(1)}",
                published_at=date_match.group(1),
                summary=clean_html_text(summary_match.group(1))
                if summary_match
                else "",
            )
        )
        if len(entries) >= limit:
            break
    return entries


def is_time_like(line: str) -> bool:
    return bool(
        re.fullmatch(r"\d+:\d+(?::\d+)?", line)
        or re.fullmatch(r"\d+[smhdwy]", line)
        or re.fullmatch(r"\d+\s*(sec|min|hour|day|week|month|year)s?", line)
    )


def parse_x_snapshot(markdown_text: str, limit: int) -> dict[str, Any]:
    lines = [line.rstrip() for line in markdown_text.splitlines()]
    published_time = ""
    posts: list[str] = []

    for raw_line in lines:
        line = normalize_whitespace(raw_line)
        if not line:
            continue

        if line.startswith("Published Time:"):
            published_time = line.split(":", 1)[1].strip()
            continue

        if "Square profile picture" in line:
            continue

        if line.startswith("[![Image") or line.startswith("![Image"):
            continue
        if line in {
            "Markdown Content:",
            "Cursor",
            "@cursor_ai",
            "Cursor’s posts",
            "--------------",
            "Pinned",
            "The best way to code with AI.",
        }:
            continue
        if line.startswith("Title:") or line.startswith("URL Source:"):
            continue
        if is_time_like(line):
            continue

        posts.append(line)

    deduped_posts: list[str] = []
    seen_posts: set[str] = set()
    for post in posts:
        if post not in seen_posts:
            deduped_posts.append(post)
            seen_posts.add(post)
        if len(deduped_posts) >= limit:
            break

    return {
        "account_name": "Cursor",
        "handle": "@cursor_ai",
        "profile_url": X_PROFILE_URL,
        "published_time": published_time,
        "posts": deduped_posts,
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except JSONDecodeError:
        return {}


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def compare_entries(
    current_entries: list[UpdateEntry], previous_entries: list[dict[str, Any]]
) -> list[UpdateEntry]:
    previous_urls = {entry.get("url") for entry in previous_entries}
    return [entry for entry in current_entries if entry.url not in previous_urls]


def compare_posts(current_posts: list[str], previous_posts: list[str]) -> list[str]:
    previous_set = set(previous_posts)
    return [post for post in current_posts if post not in previous_set]


def markdown_entries(entries: list[UpdateEntry]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        lines.append(
            f"- {entry.published_at[:10]} | {entry.title} | {entry.url}"
        )
        if entry.summary:
            lines.append(f"  - {entry.summary}")
    return lines


def markdown_posts(posts: list[str]) -> list[str]:
    return [f"- {post}" for post in posts]


def build_report(
    *,
    checked_at_utc: str,
    checked_at_local: str,
    timezone_name: str,
    changelog_entries: list[UpdateEntry],
    blog_entries: list[UpdateEntry],
    x_snapshot: dict[str, Any],
    new_changelog: list[UpdateEntry],
    new_blog: list[UpdateEntry],
    new_posts: list[str],
    errors: list[str],
) -> str:
    lines = [
        "# Cursor updates check",
        "",
        f"- Checked at (UTC): {checked_at_utc}",
        f"- Checked at ({timezone_name}): {checked_at_local}",
        "",
        "## New items since previous check",
    ]

    if new_changelog:
        lines.append("")
        lines.append("### Changelog")
        lines.extend(markdown_entries(new_changelog))

    if new_blog:
        lines.append("")
        lines.append("### Blog")
        lines.extend(markdown_entries(new_blog))

    if new_posts:
        lines.append("")
        lines.append("### Official X visible posts")
        lines.extend(markdown_posts(new_posts))

    if not (new_changelog or new_blog or new_posts):
        lines.append("")
        lines.append("- No new changelog entries, blog posts, or visible X posts.")

    lines.extend(
        [
            "",
            "## Latest changelog",
            "",
            *markdown_entries(changelog_entries),
            "",
            "## Latest blog",
            "",
            *markdown_entries(blog_entries),
            "",
            "## Official X snapshot",
            "",
            f"- Account: {x_snapshot['account_name']} ({x_snapshot['handle']})",
            f"- Profile URL: {x_snapshot['profile_url']}",
        ]
    )

    if x_snapshot.get("published_time"):
        lines.append(f"- Mirror published time: {x_snapshot['published_time']}")

    lines.append("- Visible posts:")
    lines.extend(markdown_posts(x_snapshot["posts"]))

    if errors:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {error}" for error in errors)

    return "\n".join(lines).strip() + "\n"


def write_report(report_dir: Path, report_text: str, checked_at_utc: datetime) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    filename = checked_at_utc.strftime("cursor-updates-%Y%m%dT%H%M%SZ.md")
    report_path = report_dir / filename
    report_path.write_text(report_text, encoding="utf-8")
    return report_path


def current_state_payload(
    *,
    checked_at_utc: str,
    checked_at_local: str,
    timezone_name: str,
    changelog_entries: list[UpdateEntry],
    blog_entries: list[UpdateEntry],
    x_snapshot: dict[str, Any],
    report_path: Path,
) -> dict[str, Any]:
    return {
        "last_checked_utc": checked_at_utc,
        "last_checked_local": checked_at_local,
        "timezone": timezone_name,
        "latest_changelog": [asdict(entry) for entry in changelog_entries],
        "latest_blog": [asdict(entry) for entry in blog_entries],
        "x_snapshot": x_snapshot,
        "last_report": str(report_path),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check the latest Cursor changelog, blog, and official X updates."
        )
    )
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="Timezone used for the 9am gate and human-readable timestamps.",
    )
    parser.add_argument(
        "--target-hour",
        type=int,
        default=9,
        help="Only run the check when the local hour equals this value.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without waiting for the target hour.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(".cursor_updates/state.json"),
        help="JSON file used to persist the latest seen updates.",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=Path(".cursor_updates/reports"),
        help="Directory where markdown reports are written.",
    )
    parser.add_argument(
        "--json-output",
        type=Path,
        help="Optional JSON file that receives the current run payload.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="How many changelog/blog entries and X posts to keep.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.limit < 1:
        print("--limit must be at least 1", file=sys.stderr)
        return 2
    if args.target_hour < 0 or args.target_hour > 23:
        print("--target-hour must be between 0 and 23", file=sys.stderr)
        return 2

    try:
        timezone_info = ZoneInfo(args.timezone)
    except ZoneInfoNotFoundError:
        print(f"Unknown timezone: {args.timezone}", file=sys.stderr)
        return 2

    now_local = datetime.now(timezone_info)
    if not args.force and now_local.hour != args.target_hour:
        print(
            f"Skipping check: current hour in {args.timezone} is "
            f"{now_local.hour:02d}, target hour is {args.target_hour:02d}."
        )
        return 0

    checked_at_utc_dt = datetime.now(timezone.utc)
    checked_at_utc = checked_at_utc_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    checked_at_local = now_local.isoformat(timespec="seconds")

    errors: list[str] = []
    changelog_entries: list[UpdateEntry] = []
    blog_entries: list[UpdateEntry] = []
    x_snapshot: dict[str, Any] = {
        "account_name": "Cursor",
        "handle": "@cursor_ai",
        "profile_url": X_PROFILE_URL,
        "published_time": "",
        "posts": [],
    }

    try:
        changelog_entries = parse_changelog_entries(
            fetch_text(CHANGELOG_URL), limit=args.limit
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        errors.append(f"Failed to fetch changelog: {error}")

    try:
        blog_entries = parse_blog_entries(fetch_text(BLOG_URL), limit=args.limit)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        errors.append(f"Failed to fetch blog: {error}")

    try:
        x_snapshot = parse_x_snapshot(fetch_text(X_MIRROR_URL), limit=args.limit)
    except (HTTPError, URLError, TimeoutError, ValueError) as error:
        errors.append(f"Failed to fetch official X mirror: {error}")

    previous_state = load_state(args.state_file)
    previous_changelog = previous_state.get("latest_changelog", [])
    previous_blog = previous_state.get("latest_blog", [])
    previous_posts = previous_state.get("x_snapshot", {}).get("posts", [])

    new_changelog = compare_entries(changelog_entries, previous_changelog)
    new_blog = compare_entries(blog_entries, previous_blog)
    new_posts = compare_posts(x_snapshot.get("posts", []), previous_posts)

    report_text = build_report(
        checked_at_utc=checked_at_utc,
        checked_at_local=checked_at_local,
        timezone_name=args.timezone,
        changelog_entries=changelog_entries,
        blog_entries=blog_entries,
        x_snapshot=x_snapshot,
        new_changelog=new_changelog,
        new_blog=new_blog,
        new_posts=new_posts,
        errors=errors,
    )

    report_path = write_report(args.report_dir, report_text, checked_at_utc_dt)
    state_payload = current_state_payload(
        checked_at_utc=checked_at_utc,
        checked_at_local=checked_at_local,
        timezone_name=args.timezone,
        changelog_entries=changelog_entries,
        blog_entries=blog_entries,
        x_snapshot=x_snapshot,
        report_path=report_path,
    )

    ensure_parent(args.state_file)
    args.state_file.write_text(
        json.dumps(state_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    if args.json_output:
        ensure_parent(args.json_output)
        args.json_output.write_text(
            json.dumps(
                {
                    "state": state_payload,
                    "new_changelog": [asdict(entry) for entry in new_changelog],
                    "new_blog": [asdict(entry) for entry in new_blog],
                    "new_posts": new_posts,
                    "warnings": errors,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    print(report_text)

    if changelog_entries or blog_entries or x_snapshot.get("posts"):
        return 0 if not errors else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
