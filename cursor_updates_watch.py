#!/usr/bin/env python3
"""Fetch Cursor updates from the official changelog, blog, and X account.

Default behavior is designed for an hourly automation trigger:
- Only runs during the 09:00 hour in Asia/Shanghai.
- Only runs once per local day.
- Persists seen items and the last successful run date in .cursor_updates/state.json.

Use --force to bypass the schedule gate. A forced run refreshes the markdown report
immediately, but only updates state when --update-state is also provided.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


TIMEZONE_NAME = "Asia/Shanghai"
CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
BLOG_MIRROR_PREFIX = "https://r.jina.ai/http://cursor.com"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
X_PROFILE_URL = "https://x.com/cursor_ai"
DEFAULT_STATE_PATH = Path(".cursor_updates/state.json")
DEFAULT_REPORT_PATH = Path("cursor_updates.md")
DEFAULT_BOOTSTRAP_DAYS = 7
DEFAULT_X_BOOTSTRAP_COUNT = 8
REQUEST_TIMEOUT_SECONDS = 60
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
)
JINA_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/plain,text/markdown;q=0.9,*/*;q=0.8",
}


@dataclass(frozen=True)
class UpdateItem:
    item_id: str
    title: str
    link: str
    summary: str
    published_at: datetime | None = None


@dataclass(frozen=True)
class BlogIndexEntry:
    url: str
    lastmod: datetime


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text or "")
    return normalize_space(html.unescape(no_tags))


def truncate(text: str, limit: int = 320) -> str:
    compact = normalize_space(text)
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3].rstrip() + "..."


def parse_datetime(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("empty datetime")
    if "," in text:
        dt = parsedate_to_datetime(text)
    else:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def format_datetime(dt: datetime | None) -> str:
    if dt is None:
        return "Unknown"
    return dt.astimezone(ZoneInfo(TIMEZONE_NAME)).strftime("%Y-%m-%d %H:%M %Z")


def fetch_text(url: str, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = Request(url, headers=request_headers)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", "replace")


def load_state(path: Path) -> dict:
    if not path.exists():
        return default_state()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()
    return {
        "last_run_date": data.get("last_run_date"),
        "seen": {
            "changelog": list(dict.fromkeys(data.get("seen", {}).get("changelog", []))),
            "blog": list(dict.fromkeys(data.get("seen", {}).get("blog", []))),
            "x": list(dict.fromkeys(data.get("seen", {}).get("x", []))),
        },
    }


def default_state() -> dict:
    return {
        "last_run_date": None,
        "seen": {"changelog": [], "blog": [], "x": []},
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def should_run(now_local: datetime, state: dict, force: bool) -> tuple[bool, str]:
    if force:
        return True, "Forced run."
    if now_local.hour != 9:
        return False, f"Skipping: local hour is {now_local.hour:02d}, outside the 09:00 window."
    if state.get("last_run_date") == now_local.date().isoformat():
        return False, "Skipping: updates were already checked today."
    return True, "Scheduled run is allowed."


def parse_changelog_feed(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for item in root.findall("./channel/item"):
        title = normalize_space(item.findtext("title", default="Untitled changelog entry"))
        link = normalize_space(item.findtext("link", default="https://cursor.com/changelog"))
        description_element = item.find("description")
        if description_element is None:
            description = ""
        else:
            description = strip_html("".join(description_element.itertext()))
        pub_date = parse_datetime(item.findtext("pubDate", default=""))
        items.append(
            UpdateItem(
                item_id=link,
                title=title,
                link=link,
                summary=truncate(description),
                published_at=pub_date,
            )
        )
    items.sort(key=lambda entry: entry.published_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return items


def parse_blog_sitemap(xml_text: str) -> list[BlogIndexEntry]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[BlogIndexEntry] = []
    seen_urls: set[str] = set()
    for url_el in root.findall("sm:url", namespace):
        loc = normalize_space(url_el.findtext("sm:loc", default="", namespaces=namespace))
        if not loc.startswith("https://cursor.com/blog/"):
            continue
        if loc in seen_urls:
            continue
        seen_urls.add(loc)
        lastmod = parse_datetime(url_el.findtext("sm:lastmod", default="", namespaces=namespace))
        entries.append(BlogIndexEntry(url=loc, lastmod=lastmod))
    entries.sort(key=lambda entry: entry.lastmod, reverse=True)
    return entries


def title_from_url(url: str) -> str:
    slug = urlparse(url).path.rsplit("/", 1)[-1]
    if not slug:
        return "Untitled blog post"
    return slug.replace("-", " ").title()


def parse_blog_title(markdown_text: str, fallback_url: str) -> str:
    match = re.search(r"^Title:\s*(.+)$", markdown_text, re.MULTILINE)
    if match:
        return normalize_space(match.group(1))
    return title_from_url(fallback_url)


def extract_blog_summary(markdown_text: str) -> str:
    body = markdown_text.split("Markdown Content:", 1)[-1]
    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(normalize_space(" ".join(current)))
            current.clear()

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line:
            flush()
            if len(paragraphs) >= 2:
                break
            continue
        if line.startswith("Title:") or line.startswith("URL Source:") or line.startswith("Published Time:"):
            continue
        if line.startswith("![" ) or line.startswith("[![Image"):
            continue
        if line.startswith(">"):
            flush()
            break
        if line.startswith("[](") or line.startswith("#"):
            flush()
            break
        if re.fullmatch(r"-{3,}", line):
            continue
        current.append(line)
    flush()

    if not paragraphs:
        return "No summary extracted from the mirrored blog content."
    return truncate(" ".join(paragraphs[:2]))


def fetch_blog_item(entry: BlogIndexEntry) -> UpdateItem:
    mirror_url = f"{BLOG_MIRROR_PREFIX}{urlparse(entry.url).path}"
    markdown_text = fetch_text(mirror_url, headers=JINA_HEADERS)
    return UpdateItem(
        item_id=entry.url,
        title=parse_blog_title(markdown_text, entry.url),
        link=entry.url,
        summary=extract_blog_summary(markdown_text),
        published_at=entry.lastmod,
    )


PROFILE_MARKER_RE = re.compile(
    r"^\[\!\[Image \d+: Square profile picture(?: and Opens profile photo)?[^\]]*\]\([^)]+\)\]"
    r"\(https://x\.com/cursor_ai(?:/photo)?\)\s*$"
)
MEDIA_DURATION_RE = re.compile(r"^\d+:\d{2}(?::\d{2})?$")


def parse_x_mirror(markdown_text: str) -> list[UpdateItem]:
    body = markdown_text.split("Cursor\u2019s posts", 1)[-1]
    lines = body.splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []
    started = False

    for raw_line in lines:
        line = raw_line.rstrip()
        if PROFILE_MARKER_RE.match(line.strip()):
            started = True
            if current:
                blocks.append(current)
            current = []
            continue
        if not started:
            continue
        if line.strip() == "Pinned":
            continue
        current.append(line)
    if current:
        blocks.append(current)

    items: list[UpdateItem] = []
    seen_texts: set[str] = set()
    for block in blocks:
        cleaned_lines: list[str] = []
        for raw_line in block:
            line = raw_line.strip()
            if not line:
                if cleaned_lines and cleaned_lines[-1] != "":
                    cleaned_lines.append("")
                continue
            if line.startswith("![Image") or line.startswith("[![Image"):
                continue
            if MEDIA_DURATION_RE.fullmatch(line):
                continue
            cleaned_lines.append(line)

        text = "\n".join(cleaned_lines).strip()
        text = re.sub(r"\n{3,}", "\n\n", text)
        normalized = normalize_space(text)
        if not normalized:
            continue
        if normalized in seen_texts:
            continue
        seen_texts.add(normalized)

        first_sentence = re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)[0]
        title = truncate(first_sentence, limit=120)
        item_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
        items.append(
            UpdateItem(
                item_id=item_id,
                title=title,
                link=X_PROFILE_URL,
                summary=truncate(normalized),
                published_at=None,
            )
        )
    return items


def select_dated_updates(
    items: Iterable[UpdateItem],
    seen_ids: set[str],
    now_utc: datetime,
    bootstrap_days: int,
) -> list[UpdateItem]:
    items_list = list(items)
    if seen_ids:
        return [item for item in items_list if item.item_id not in seen_ids]
    cutoff = now_utc - timedelta(days=bootstrap_days)
    return [item for item in items_list if item.published_at and item.published_at >= cutoff]


def select_x_updates(items: list[UpdateItem], seen_ids: set[str], bootstrap_count: int) -> list[UpdateItem]:
    if seen_ids:
        return [item for item in items if item.item_id not in seen_ids]
    return items[:bootstrap_count]


def render_source_section(title: str, items: list[UpdateItem], include_timestamp: bool) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.extend(["No new items.", ""])
        return lines

    for index, item in enumerate(items, start=1):
        lines.append(f"### {index}. {item.title}")
        if include_timestamp and item.published_at is not None:
            lines.append(f"- Published: {format_datetime(item.published_at)}")
        lines.append(f"- Link: {item.link}")
        lines.append(f"- Summary: {item.summary}")
        lines.append("")
    return lines


def render_report(
    generated_at: datetime,
    mode: str,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_items: list[UpdateItem],
    errors: list[str],
) -> str:
    lines = [
        "# Cursor updates watch",
        "",
        f"- Generated at: {format_datetime(generated_at)}",
        f"- Mode: {mode}",
        f"- New changelog entries: {len(changelog_items)}",
        f"- New blog posts: {len(blog_items)}",
        f"- New X posts: {len(x_items)}",
        "",
    ]
    if errors:
        lines.extend(["## Fetch errors", ""])
        for error in errors:
            lines.append(f"- {error}")
        lines.append("")

    lines.extend(render_source_section("Changelog", changelog_items, include_timestamp=True))
    lines.extend(render_source_section("Blog", blog_items, include_timestamp=True))
    lines.extend(render_source_section("Official X", x_items, include_timestamp=False))
    return "\n".join(lines).rstrip() + "\n"


def collect_updates(now_local: datetime, state: dict, bootstrap_days: int, x_bootstrap_count: int) -> tuple[dict, str, bool]:
    errors: list[str] = []
    seen = state.get("seen", {})
    now_utc = now_local.astimezone(UTC)

    changelog_current: list[UpdateItem] = []
    changelog_new: list[UpdateItem] = []
    try:
        changelog_current = parse_changelog_feed(fetch_text(CHANGELOG_RSS_URL))
        changelog_new = select_dated_updates(
            changelog_current,
            set(seen.get("changelog", [])),
            now_utc,
            bootstrap_days,
        )
    except (ET.ParseError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        errors.append(f"Changelog fetch failed: {exc}")

    blog_index: list[BlogIndexEntry] = []
    blog_new: list[UpdateItem] = []
    try:
        blog_index = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
        selected_blog_entries: list[BlogIndexEntry]
        if seen.get("blog"):
            selected_blog_entries = [entry for entry in blog_index if entry.url not in set(seen.get("blog", []))]
        else:
            cutoff = now_utc - timedelta(days=bootstrap_days)
            selected_blog_entries = [entry for entry in blog_index if entry.lastmod >= cutoff]
        blog_new = [fetch_blog_item(entry) for entry in selected_blog_entries]
    except (ET.ParseError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        errors.append(f"Blog fetch failed: {exc}")

    x_current: list[UpdateItem] = []
    x_new: list[UpdateItem] = []
    try:
        x_current = parse_x_mirror(fetch_text(X_MIRROR_URL, headers=JINA_HEADERS))
        x_new = select_x_updates(x_current, set(seen.get("x", [])), x_bootstrap_count)
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        errors.append(f"X fetch failed: {exc}")

    updated_state = {
        "last_run_date": now_local.date().isoformat(),
        "seen": {
            "changelog": [item.item_id for item in changelog_current],
            "blog": [entry.url for entry in blog_index],
            "x": [item.item_id for item in x_current],
        },
    }
    report = render_report(
        generated_at=now_local,
        mode="live",
        changelog_items=changelog_new,
        blog_items=blog_new,
        x_items=x_new,
        errors=errors,
    )
    return updated_state, report, not errors


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Bypass the schedule gate.")
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="Persist seen items and last_run_date even during a forced run.",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to the state file (default: {DEFAULT_STATE_PATH}).",
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Path to the markdown report (default: {DEFAULT_REPORT_PATH}).",
    )
    parser.add_argument(
        "--bootstrap-days",
        type=int,
        default=DEFAULT_BOOTSTRAP_DAYS,
        help=f"When state is empty, include dated items from the last N days (default: {DEFAULT_BOOTSTRAP_DAYS}).",
    )
    parser.add_argument(
        "--x-bootstrap-count",
        type=int,
        default=DEFAULT_X_BOOTSTRAP_COUNT,
        help=(
            "When state is empty, include the latest N unique X posts "
            f"(default: {DEFAULT_X_BOOTSTRAP_COUNT})."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    now_local = datetime.now(ZoneInfo(TIMEZONE_NAME))
    state = load_state(args.state_path)

    should_execute, message = should_run(now_local, state, args.force)
    print(message)
    if not should_execute:
        return 0

    updated_state, report, success = collect_updates(
        now_local=now_local,
        state=state,
        bootstrap_days=args.bootstrap_days,
        x_bootstrap_count=args.x_bootstrap_count,
    )
    args.report_path.write_text(report, encoding="utf-8")
    print(f"Wrote report to {args.report_path}")

    persist_state = success and (not args.force or args.update_state)
    if persist_state:
        save_state(args.state_path, updated_state)
        print(f"Updated state at {args.state_path}")
    elif args.force and not args.update_state:
        print("Forced run completed without persisting state.")

    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
