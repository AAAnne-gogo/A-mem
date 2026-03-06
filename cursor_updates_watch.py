#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

MARKETING_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
X_PROFILE_URL = "https://x.com/cursor_ai"
DEFAULT_LIMIT = 5
RUN_TIMEZONE = ZoneInfo("Asia/Shanghai")
RUN_HOUR = 9
USER_AGENT = "Mozilla/5.0 (compatible; cursor-updates-watch/1.0)"


@dataclass(frozen=True)
class FeedEntry:
    title: str
    url: str
    published_at: str


FetchText = Callable[[str], str]


def utc_now() -> datetime:
    return datetime.now(UTC)


def ensure_aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_date(value: str) -> str:
    if not value:
        return ""
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
        return match.group(1) if match else value


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def should_run(local_now: datetime, last_successful_run_local_date: str | None, force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced"
    if local_now.hour != RUN_HOUR:
        return False, f"outside_run_window:{local_now.hour:02d}"
    if last_successful_run_local_date == local_now.date().isoformat():
        return False, "already_ran_today"
    return True, "scheduled"


def parse_sitemap_entries(xml_text: str, section: str, limit: int) -> list[tuple[str, str]]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    prefix = f"https://cursor.com/{section}/"
    rows: list[tuple[str, str]] = []
    for url_node in root.findall("sm:url", namespace):
        loc = url_node.findtext("sm:loc", default="", namespaces=namespace)
        lastmod = url_node.findtext("sm:lastmod", default="", namespaces=namespace)
        if loc.startswith(prefix):
            rows.append((lastmod, loc))
    rows.sort(key=lambda row: (row[0], row[1]), reverse=True)
    return rows[:limit]


def clean_page_title(raw_title: str) -> str:
    title = unescape(raw_title).strip()
    title = re.sub(r"\s+[·|]\s+Cursor(?:\s*-.*)?$", "", title).strip()
    title = re.sub(r"\s+", " ", title)
    return title


def extract_title_and_published(html_text: str, fallback_date: str) -> tuple[str, str]:
    title_match = re.search(r"<title>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL)
    if title_match:
        title = clean_page_title(title_match.group(1))
    else:
        header_match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.IGNORECASE | re.DOTALL)
        title = clean_page_title(header_match.group(1)) if header_match else "Untitled"

    published_match = re.search(r'"datePublished":"([^"]+)"', html_text)
    published_at = canonical_date(published_match.group(1)) if published_match else canonical_date(fallback_date)
    return title, published_at


def collect_feed(section: str, limit: int, fetcher: FetchText) -> list[FeedEntry]:
    sitemap_text = fetcher(MARKETING_SITEMAP_URL)
    items = parse_sitemap_entries(sitemap_text, section=section, limit=limit)
    entries: list[FeedEntry] = []
    for lastmod, url in items:
        page_text = fetcher(url)
        title, published_at = extract_title_and_published(page_text, fallback_date=lastmod)
        entries.append(FeedEntry(title=title, url=url, published_at=published_at))
    return entries


def extract_x_posts(markdown_text: str, limit: int) -> list[str]:
    if "Cursor’s posts" not in markdown_text:
        return []
    section = markdown_text.split("Cursor’s posts", 1)[1]
    chunks = re.split(r"\n(?=\[!\[Image \d+: Square profile picture)", section)
    posts: list[str] = []
    for chunk in chunks:
        lines: list[str] = []
        for raw_line in chunk.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line in {"--------------", "Pinned"}:
                continue
            if line.startswith("[![Image") or line.startswith("![Image"):
                continue
            if re.fullmatch(r"\d+:\d+", line):
                continue
            lines.append(line)
        if not lines:
            continue
        post_text = " ".join(lines).strip()
        if len(post_text) < 12:
            continue
        if post_text not in posts:
            posts.append(post_text)
        if len(posts) >= limit:
            break
    return posts


def collect_x_snapshot(limit: int, fetcher: FetchText) -> dict:
    mirror_text = fetcher(X_MIRROR_URL)
    published_match = re.search(r"Published Time:\s*(.+)", mirror_text)
    return {
        "profile_url": X_PROFILE_URL,
        "mirror_url": X_MIRROR_URL,
        "published_time": published_match.group(1).strip() if published_match else "",
        "posts": extract_x_posts(mirror_text, limit=limit),
    }


def compute_new_entries(entries: list[FeedEntry], seen_urls: list[str]) -> list[FeedEntry]:
    seen = set(seen_urls)
    return [entry for entry in entries if entry.url not in seen]


def compute_new_posts(posts: list[str], seen_posts: list[str]) -> list[str]:
    seen = set(seen_posts)
    return [post for post in posts if post not in seen]


def merge_seen(existing: list[str], current: list[str], cap: int = 200) -> list[str]:
    merged: list[str] = []
    for item in current + existing:
        if item not in merged:
            merged.append(item)
        if len(merged) >= cap:
            break
    return merged


def format_feed(entries: list[FeedEntry]) -> list[str]:
    if not entries:
        return ["- None"]
    return [f"- {entry.published_at} | {entry.title} | {entry.url}" for entry in entries]


def format_posts(posts: list[str]) -> list[str]:
    if not posts:
        return ["- None"]
    return [f'- "{post}"' for post in posts]


def build_report(result: dict) -> str:
    lines = [
        "# Cursor updates watch",
        "",
        f"- Status: {result['status']}",
        f"- Trigger mode: {'force' if result['force'] else 'scheduled'}",
        f"- Checked at (UTC): {result['checked_at_utc']}",
        f"- Checked at (Asia/Shanghai): {result['checked_at_local']}",
    ]

    if result["status"].startswith("skipped"):
        lines.extend(
            [
                f"- Skip reason: {result['reason']}",
                "",
                "No fetch was performed in this run.",
            ]
        )
        return "\n".join(lines) + "\n"

    errors: dict[str, str] = result["errors"]
    if errors:
        lines.extend(["", "## Source errors"])
        lines.extend(f"- {name}: {message}" for name, message in sorted(errors.items()))

    lines.extend(
        [
            "",
            "## New since last successful run",
            "",
            "### Changelog",
            *format_feed(result["new_changelog"]),
            "",
            "### Blog",
            *format_feed(result["new_blog"]),
            "",
            "### Official X",
            *format_posts(result["new_x_posts"]),
            "",
            "## Latest changelog snapshot",
            *format_feed(result["changelog"]),
            "",
            "## Latest blog snapshot",
            *format_feed(result["blog"]),
            "",
            "## Latest official X snapshot",
            f"- Account: Cursor (@cursor_ai) | {result['x']['profile_url']}",
            f"- Mirror: {result['x']['mirror_url']}",
            f"- Mirror published time: {result['x']['published_time'] or 'unknown'}",
            *format_posts(result["x"]["posts"]),
        ]
    )
    return "\n".join(lines) + "\n"


def snapshot_to_state(result: dict, previous_state: dict) -> dict:
    previous_seen = previous_state.get("seen", {})
    changelog_urls = [entry.url for entry in result["changelog"]]
    blog_urls = [entry.url for entry in result["blog"]]
    x_posts = result["x"]["posts"]
    return {
        "last_checked_at": result["checked_at_utc"],
        "last_successful_run_local_date": result["checked_at_local"][:10],
        "latest": {
            "changelog": [asdict(entry) for entry in result["changelog"]],
            "blog": [asdict(entry) for entry in result["blog"]],
            "x": result["x"],
        },
        "seen": {
            "changelog_urls": merge_seen(previous_seen.get("changelog_urls", []), changelog_urls),
            "blog_urls": merge_seen(previous_seen.get("blog_urls", []), blog_urls),
            "x_posts": merge_seen(previous_seen.get("x_posts", []), x_posts, cap=50),
        },
    }


def run_watch(
    *,
    now: datetime | None = None,
    force: bool = False,
    limit: int = DEFAULT_LIMIT,
    state_dir: Path = Path(".cursor_updates"),
    fetcher: FetchText = fetch_text,
) -> dict:
    checked_at_utc = ensure_aware_utc(now)
    checked_at_local = checked_at_utc.astimezone(RUN_TIMEZONE)
    state_dir.mkdir(parents=True, exist_ok=True)
    history_dir = state_dir / "history"
    state_file = state_dir / "state.json"
    latest_report_file = state_dir / "latest_report.md"
    previous_state = load_json(state_file)

    should_execute, reason = should_run(
        checked_at_local,
        previous_state.get("last_successful_run_local_date"),
        force,
    )
    if not should_execute:
        result = {
            "status": "skipped",
            "reason": reason,
            "force": force,
            "checked_at_utc": isoformat_z(checked_at_utc),
            "checked_at_local": checked_at_local.isoformat(),
            "errors": {},
        }
        write_text(latest_report_file, build_report(result))
        return result

    errors: dict[str, str] = {}
    changelog: list[FeedEntry] = []
    blog: list[FeedEntry] = []
    x_snapshot = {
        "profile_url": X_PROFILE_URL,
        "mirror_url": X_MIRROR_URL,
        "published_time": "",
        "posts": [],
    }

    try:
        changelog = collect_feed("changelog", limit=limit, fetcher=fetcher)
    except Exception as exc:  # pragma: no cover - exercised through result shape
        errors["changelog"] = str(exc)

    try:
        blog = collect_feed("blog", limit=limit, fetcher=fetcher)
    except Exception as exc:  # pragma: no cover - exercised through result shape
        errors["blog"] = str(exc)

    try:
        x_snapshot = collect_x_snapshot(limit=limit, fetcher=fetcher)
    except Exception as exc:  # pragma: no cover - exercised through result shape
        errors["x"] = str(exc)

    seen = previous_state.get("seen", {})
    new_changelog = compute_new_entries(changelog, seen.get("changelog_urls", []))
    new_blog = compute_new_entries(blog, seen.get("blog_urls", []))
    new_x_posts = compute_new_posts(x_snapshot["posts"], seen.get("x_posts", []))

    result = {
        "status": "success" if not errors else "partial_failure",
        "reason": reason,
        "force": force,
        "checked_at_utc": isoformat_z(checked_at_utc),
        "checked_at_local": checked_at_local.isoformat(),
        "errors": errors,
        "changelog": changelog,
        "blog": blog,
        "x": x_snapshot,
        "new_changelog": new_changelog,
        "new_blog": new_blog,
        "new_x_posts": new_x_posts,
    }

    report = build_report(result)
    write_text(latest_report_file, report)
    if result["status"] == "success":
        next_state = snapshot_to_state(result, previous_state)
        write_json(state_file, next_state)
        history_file = history_dir / f"{checked_at_local.date().isoformat()}.md"
        write_text(history_file, report)
    return result


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X updates.")
    parser.add_argument("--force", action="store_true", help="Run immediately without the 09:00 gate.")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Number of latest items to keep from each source.",
    )
    parser.add_argument(
        "--state-dir",
        default=".cursor_updates",
        help="Directory used for runtime state and markdown reports.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_watch(
        force=args.force,
        limit=args.limit,
        state_dir=Path(args.state_dir),
    )
    latest_report = Path(args.state_dir) / "latest_report.md"
    if latest_report.exists():
        sys.stdout.write(latest_report.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
