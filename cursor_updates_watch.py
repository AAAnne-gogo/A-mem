#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_SCHEDULE_HOUR = 9
BOOTSTRAP_LOOKBACK_DAYS = 7
BOOTSTRAP_X_POSTS = 8
SEEN_LIMIT = 200

CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_URL = "https://cursor.com/blog"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
BLOG_ATOM_URL = "https://cursor.com/atom.xml"
X_TIMELINE_URL = "https://r.jina.ai/http://x.com/cursor_ai"

HTML_TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    link: str
    published_at: str | None = None
    summary: str = ""


@dataclass(frozen=True)
class XPost:
    text: str
    pinned: bool = False


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": "CursorUpdatesWatch/1.0",
            "Accept": "text/html,application/xml,application/rss+xml;q=0.9,*/*;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="replace")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code} for {url}") from exc
    except URLError as exc:
        raise RuntimeError(f"Network error for {url}: {exc.reason}") from exc


def clean_html(text: str) -> str:
    without_tags = HTML_TAG_RE.sub(" ", text)
    return WHITESPACE_RE.sub(" ", unescape(without_tags)).strip()


def normalize_text(text: str) -> str:
    return WHITESPACE_RE.sub(" ", text).strip()


def parse_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_date(value: str | None, timezone_name: str) -> str:
    if not value:
        return "unknown time"
    return (
        parse_datetime(value)
        .astimezone(ZoneInfo(timezone_name))
        .strftime("%Y-%m-%d %H:%M %Z")
    )


def to_absolute_url(url: str) -> str:
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if not url.startswith("/"):
        url = "/" + url
    return "https://cursor.com" + url


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for item_el in root.findall("./channel/item"):
        title = (item_el.findtext("title") or "").strip()
        link = (item_el.findtext("link") or "").strip()
        description = clean_html(item_el.findtext("description") or "")
        published_raw = (item_el.findtext("pubDate") or "").strip()
        published_at = None
        if published_raw:
            published_at = parsedate_to_datetime(published_raw).astimezone(
                timezone.utc
            ).isoformat()
        if title and link:
            items.append(
                UpdateItem(
                    source="changelog",
                    title=title,
                    link=link,
                    published_at=published_at,
                    summary=description,
                )
            )
    return sort_dated_items(items)


def parse_blog_index(html_text: str) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    for article in re.findall(r"<article\b.*?</article>", html_text, re.S):
        link_match = re.search(r'href="(/blog/[^"#?]+)"', article)
        time_match = re.search(r'<time[^>]+dateTime="([^"]+)"', article)
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", article, re.S)
        if not link_match or not time_match or not paragraphs:
            continue
        link = to_absolute_url(link_match.group(1))
        if "/blog/topic/" in link:
            continue
        title = clean_html(paragraphs[0])
        summary = clean_html(paragraphs[1]) if len(paragraphs) > 1 else ""
        if not title:
            continue
        published_at = parse_datetime(time_match.group(1)).astimezone(
            timezone.utc
        ).isoformat()
        items.append(
            UpdateItem(
                source="blog",
                title=title,
                link=link,
                published_at=published_at,
                summary=summary,
            )
        )
    return sort_dated_items(dedupe_items(items))


def parse_blog_sitemap(xml_text: str) -> list[UpdateItem]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for url_el in root.findall(".//sm:url", namespace):
        link = (url_el.findtext("sm:loc", namespaces=namespace) or "").strip()
        lastmod = (url_el.findtext("sm:lastmod", namespaces=namespace) or "").strip()
        if not link or "/blog/" not in link or "/blog/topic/" in link:
            continue
        slug = link.rstrip("/").split("/")[-1]
        title = slug.replace("-", " ").title()
        published_at = None
        if lastmod:
            published_at = parse_datetime(lastmod).astimezone(timezone.utc).isoformat()
        items.append(
            UpdateItem(
                source="blog",
                title=title,
                link=link,
                published_at=published_at,
                summary="",
            )
        )
    return sort_dated_items(dedupe_items(items))


def parse_blog_atom(xml_text: str) -> list[UpdateItem]:
    namespace = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for entry in root.findall("./atom:entry", namespace):
        title = clean_html(entry.findtext("atom:title", default="", namespaces=namespace))
        summary = clean_html(
            entry.findtext("atom:summary", default="", namespaces=namespace)
        )
        link_el = entry.find("atom:link", namespace)
        link = link_el.attrib.get("href", "").strip() if link_el is not None else ""
        updated = (entry.findtext("atom:updated", default="", namespaces=namespace) or "").strip()
        published_at = None
        if updated:
            published_at = parse_datetime(updated).astimezone(timezone.utc).isoformat()
        if title and link:
            items.append(
                UpdateItem(
                    source="blog",
                    title=title,
                    link=link,
                    published_at=published_at,
                    summary=summary,
                )
            )
    return sort_dated_items(dedupe_items(items))


def parse_x_timeline(markdown_text: str) -> list[XPost]:
    start_marker = "Cursor’s posts"
    if start_marker not in markdown_text:
        return []

    section = markdown_text.split(start_marker, 1)[1]
    lines = [line.rstrip() for line in section.splitlines()]
    posts: list[XPost] = []
    current_lines: list[str] = []
    next_is_pinned = False
    current_pinned = False

    def flush_current() -> None:
        nonlocal current_lines, current_pinned
        text = normalize_text(" ".join(current_lines))
        if text:
            posts.append(XPost(text=text, pinned=current_pinned))
        current_lines = []
        current_pinned = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line or set(line) == {"-"}:
            continue
        if line == "Pinned":
            next_is_pinned = True
            continue
        if line.startswith("[![Image") and "profile picture" in line:
            flush_current()
            current_pinned = next_is_pinned
            next_is_pinned = False
            continue
        if line.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        if line.lower() in {"alt", "show more"}:
            continue
        current_lines.append(line)

    flush_current()

    deduped: list[XPost] = []
    seen_texts: set[str] = set()
    for post in posts:
        key = post.text
        if key in seen_texts:
            continue
        seen_texts.add(key)
        deduped.append(post)
    return deduped


def sort_dated_items(items: list[UpdateItem]) -> list[UpdateItem]:
    return sorted(
        items,
        key=lambda item: parse_datetime(item.published_at).timestamp()
        if item.published_at
        else 0,
        reverse=True,
    )


def dedupe_items(items: list[UpdateItem]) -> list[UpdateItem]:
    by_link: dict[str, UpdateItem] = {}
    for item in items:
        existing = by_link.get(item.link)
        if existing is None:
            by_link[item.link] = item
            continue
        existing_time = parse_datetime(existing.published_at).timestamp() if existing.published_at else 0
        new_time = parse_datetime(item.published_at).timestamp() if item.published_at else 0
        if new_time > existing_time:
            by_link[item.link] = item
    return list(by_link.values())


def load_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {
            "last_scheduled_run_date": None,
            "last_run_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
    try:
        return json.loads(state_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {
            "last_scheduled_run_date": None,
            "last_run_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }


def save_state(state_file: Path, state: dict[str, Any]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def item_key(item: UpdateItem) -> str:
    return item.link


def x_key(post: XPost) -> str:
    return hashlib.sha256(post.text.encode("utf-8")).hexdigest()


def should_run(now_local: datetime, schedule_hour: int, last_scheduled_run_date: str | None) -> tuple[bool, str]:
    local_date = now_local.date().isoformat()
    if last_scheduled_run_date == local_date:
        return False, f"already ran for {local_date}"
    if now_local.hour != schedule_hour:
        return False, (
            f"waiting for {schedule_hour:02d}:00 in {now_local.tzname()}, "
            f"current local time is {now_local.strftime('%H:%M')}"
        )
    return True, "scheduled window open"


def select_new_dated_items(
    items: list[UpdateItem],
    seen_keys: set[str],
    now_utc: datetime,
    bootstrap_lookback_days: int = BOOTSTRAP_LOOKBACK_DAYS,
) -> list[UpdateItem]:
    if seen_keys:
        return [item for item in items if item_key(item) not in seen_keys]

    cutoff = now_utc - timedelta(days=bootstrap_lookback_days)
    selected: list[UpdateItem] = []
    for item in items:
        if not item.published_at:
            continue
        if parse_datetime(item.published_at) >= cutoff:
            selected.append(item)
    return selected


def select_new_x_posts(posts: list[XPost], seen_keys: set[str]) -> list[XPost]:
    if seen_keys:
        return [post for post in posts if x_key(post) not in seen_keys]
    return posts[:BOOTSTRAP_X_POSTS]


def update_seen(existing: list[str], latest: list[str], limit: int = SEEN_LIMIT) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in latest + existing:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
        if len(merged) >= limit:
            break
    return merged


def fetch_changelog() -> list[UpdateItem]:
    return parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))


def fetch_blog() -> list[UpdateItem]:
    index_items = parse_blog_index(fetch_text(BLOG_URL))
    if index_items:
        return index_items

    sitemap_items = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
    if sitemap_items:
        return sitemap_items

    return parse_blog_atom(fetch_text(BLOG_ATOM_URL))


def fetch_x_posts() -> list[XPost]:
    return parse_x_timeline(fetch_text(X_TIMELINE_URL))


def render_section(title: str, lines: list[str]) -> list[str]:
    rendered = [f"## {title}"]
    if lines:
        rendered.extend(lines)
    else:
        rendered.append("- No new items detected.")
    rendered.append("")
    return rendered


def build_report(
    now_local: datetime,
    timezone_name: str,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_posts: list[XPost],
    errors: list[str],
) -> str:
    lines = [
        "# Cursor Daily Update Watch",
        "",
        f"- Generated at: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Time zone: {timezone_name}",
        (
            "- Summary: "
            f"{len(changelog_items)} changelog update(s), "
            f"{len(blog_items)} blog update(s), "
            f"{len(x_posts)} X post(s)."
        ),
        "",
    ]

    lines.extend(
        render_section(
            "Changelog",
            [
                (
                    f"- [{item.title}]({item.link}) "
                    f"({format_date(item.published_at, timezone_name)})"
                    + (f" - {item.summary}" if item.summary else "")
                )
                for item in changelog_items
            ],
        )
    )

    lines.extend(
        render_section(
            "Blog",
            [
                (
                    f"- [{item.title}]({item.link}) "
                    f"({format_date(item.published_at, timezone_name)})"
                    + (f" - {item.summary}" if item.summary else "")
                )
                for item in blog_items
            ],
        )
    )

    lines.extend(
        render_section(
            "Official X (@cursor_ai)",
            [
                f"- {'[Pinned] ' if post.pinned else ''}{post.text}"
                for post in x_posts
            ],
        )
    )

    if errors:
        lines.extend(render_section("Warnings", [f"- {message}" for message in errors]))

    return "\n".join(lines).strip() + "\n"


def write_report(state_dir: Path, now_local: datetime, report: str) -> Path:
    reports_dir = state_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    filename = now_local.strftime("%Y-%m-%d_%H%M%S_%Z.md")
    report_path = reports_dir / filename
    report_path.write_text(report, encoding="utf-8")
    (state_dir / "latest.md").write_text(report, encoding="utf-8")
    return report_path


def run_watch(force: bool, timezone_name: str, schedule_hour: int, state_dir: Path) -> tuple[str, str]:
    now_utc = datetime.now(timezone.utc)
    now_local = now_utc.astimezone(ZoneInfo(timezone_name))

    state_file = state_dir / "state.json"
    state = load_state(state_file)

    if not force:
        allowed, reason = should_run(
            now_local=now_local,
            schedule_hour=schedule_hour,
            last_scheduled_run_date=state.get("last_scheduled_run_date"),
        )
        if not allowed:
            return "skipped", reason

    errors: list[str] = []

    changelog_items: list[UpdateItem] = []
    blog_items: list[UpdateItem] = []
    x_posts: list[XPost] = []

    try:
        changelog_items = fetch_changelog()
    except Exception as exc:  # pragma: no cover - exercised in live runs.
        errors.append(f"changelog fetch failed: {exc}")

    try:
        blog_items = fetch_blog()
    except Exception as exc:  # pragma: no cover - exercised in live runs.
        errors.append(f"blog fetch failed: {exc}")

    try:
        x_posts = fetch_x_posts()
    except Exception as exc:  # pragma: no cover - exercised in live runs.
        errors.append(f"X fetch failed: {exc}")

    seen = state.get("seen", {})
    new_changelog = select_new_dated_items(
        changelog_items,
        set(seen.get("changelog", [])),
        now_utc,
    )
    new_blog = select_new_dated_items(
        blog_items,
        set(seen.get("blog", [])),
        now_utc,
    )
    new_x = select_new_x_posts(
        x_posts,
        set(seen.get("x", [])),
    )

    report = build_report(
        now_local=now_local,
        timezone_name=timezone_name,
        changelog_items=new_changelog,
        blog_items=new_blog,
        x_posts=new_x,
        errors=errors,
    )
    report_path = write_report(state_dir, now_local, report)

    if not force:
        state["last_scheduled_run_date"] = now_local.date().isoformat()
        state["last_run_at"] = now_utc.isoformat()
        state["seen"] = {
            "changelog": update_seen(
                seen.get("changelog", []),
                [item_key(item) for item in changelog_items],
            ),
            "blog": update_seen(
                seen.get("blog", []),
                [item_key(item) for item in blog_items],
            ),
            "x": update_seen(
                seen.get("x", []),
                [x_key(post) for post in x_posts],
            ),
        }
        save_state(state_file, state)

    return "success", f"report written to {report_path}"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check Cursor changelog, blog, and official X updates once per day "
            "during the configured local 09:00 hour."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without schedule gating and without updating seen-state.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"IANA timezone name, default: {DEFAULT_TIMEZONE}.",
    )
    parser.add_argument(
        "--schedule-hour",
        type=int,
        default=DEFAULT_SCHEDULE_HOUR,
        help=f"Local hour to run, default: {DEFAULT_SCHEDULE_HOUR}.",
    )
    parser.add_argument(
        "--state-dir",
        default=".cursor_updates",
        help="Directory for reports and state files.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    status, message = run_watch(
        force=args.force,
        timezone_name=args.timezone,
        schedule_hour=args.schedule_hour,
        state_dir=Path(args.state_dir),
    )
    print(f"[{status}] {message}")
    latest_report = Path(args.state_dir) / "latest.md"
    if latest_report.exists():
        print("")
        print(latest_report.read_text(encoding="utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
