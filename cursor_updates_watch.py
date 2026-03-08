#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import socket
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable
from urllib import error as urllib_error
from urllib import request as urllib_request
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


WORKSPACE_ROOT = Path(__file__).resolve().parent
STATE_DIR = WORKSPACE_ROOT / ".cursor_updates"
STATE_PATH = STATE_DIR / "state.json"
LATEST_REPORT_PATH = STATE_DIR / "latest_report.md"
HISTORY_DIR = STATE_DIR / "history"
PUBLIC_REPORT_PATH = WORKSPACE_ROOT / "cursor_updates.md"

LOCAL_TZ = ZoneInfo("Asia/Shanghai")

CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
BLOG_INDEX_URL = "https://cursor.com/en/blog"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://www.x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)

HTTP_RETRYABLE_CODES = {403, 429, 500, 502, 503, 504}
BOOTSTRAP_DAYS = 7
BOOTSTRAP_FALLBACK_COUNT = 4
X_FALLBACK_COUNT = 8
MAX_SEEN_PER_SOURCE = 200

DEFAULT_STATE = {
    "last_successful_local_date": None,
    "seen": {
        "changelog": [],
        "blog": [],
        "x": [],
    },
}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str = ""
    published_at: str | None = None
    summary: str = ""
    topic: str = ""
    identity: str = ""

    def with_identity(self) -> "UpdateItem":
        if self.identity:
            return self
        if self.url:
            return UpdateItem(
                source=self.source,
                title=self.title,
                url=self.url,
                published_at=self.published_at,
                summary=self.summary,
                topic=self.topic,
                identity=self.url,
            )
        payload = f"{self.source}\n{self.title}\n{self.summary}".encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        return UpdateItem(
            source=self.source,
            title=self.title,
            url=self.url,
            published_at=self.published_at,
            summary=self.summary,
            topic=self.topic,
            identity=digest,
        )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_local(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(LOCAL_TZ)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        if value.endswith("Z"):
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        return datetime.fromisoformat(value)
    except ValueError:
        pass
    try:
        return parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None


def isoformat_or_none(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def strip_tags(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return normalize_text(without_tags)


def title_from_url(url: str) -> str:
    slug = url.rstrip("/").split("/")[-1]
    return " ".join(part.capitalize() for part in slug.split("-") if part)


def request_text(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 4,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CursorUpdatesWatch/1.0)",
        "Accept": "application/rss+xml, application/xml, text/html, text/plain;q=0.9",
    }
    backoff = 1.0
    last_error: Exception | None = None
    for attempt in range(retries):
        req = urllib_request.Request(url, headers=headers)
        try:
            with urllib_request.urlopen(req, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except urllib_error.HTTPError as exc:
            last_error = exc
            if exc.code not in HTTP_RETRYABLE_CODES or attempt == retries - 1:
                raise
        except (urllib_error.URLError, TimeoutError, socket.timeout) as exc:
            last_error = exc
            if attempt == retries - 1:
                raise
        sleep_fn(backoff)
        backoff *= 2
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for item in root.findall("./channel/item"):
        title = normalize_text(item.findtext("title", default=""))
        link = normalize_text(item.findtext("link", default=""))
        summary = strip_tags(item.findtext("description", default=""))
        published_at = isoformat_or_none(parse_datetime(item.findtext("pubDate")))
        if not title or not link:
            continue
        items.append(
            UpdateItem(
                source="changelog",
                title=title,
                url=link,
                published_at=published_at,
                summary=summary,
            ).with_identity()
        )
    return sort_items(items)


def parse_blog_sitemap(xml_text: str) -> dict[str, UpdateItem]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    items: dict[str, UpdateItem] = {}
    for node in root.findall("sm:url", namespace):
        loc = normalize_text(node.findtext("sm:loc", default="", namespaces=namespace))
        if "/blog/" not in loc or "/blog/topic/" in loc:
            continue
        published_at = isoformat_or_none(
            parse_datetime(node.findtext("sm:lastmod", default="", namespaces=namespace))
        )
        items[loc] = UpdateItem(
            source="blog",
            title=title_from_url(loc),
            url=loc,
            published_at=published_at,
        ).with_identity()
    return items


def parse_blog_index(html_text: str) -> list[UpdateItem]:
    pattern = re.compile(
        r'<a[^>]+href="(?P<url>/blog/(?!topic/)[^"]+)"[^>]*>(?P<body>.*?)</a>',
        re.DOTALL,
    )
    items: list[UpdateItem] = []
    seen_urls: set[str] = set()
    for match in pattern.finditer(html_text):
        url = "https://cursor.com" + match.group("url")
        if url in seen_urls:
            continue
        body = match.group("body")
        paragraphs = re.findall(r"<p[^>]*>(.*?)</p>", body, flags=re.DOTALL)
        title = strip_tags(paragraphs[0]) if paragraphs else title_from_url(url)
        summary = strip_tags(paragraphs[1]) if len(paragraphs) > 1 else ""
        topic_match = re.search(r"<span[^>]*>(.*?)</span>", body, flags=re.DOTALL)
        topic = strip_tags(topic_match.group(1)) if topic_match else ""
        topic = re.sub(r"\s*[·.]+\s*$", "", topic)
        time_match = re.search(r'<time[^>]*dateTime="([^"]+)"', body)
        published_at = isoformat_or_none(parse_datetime(time_match.group(1) if time_match else ""))
        items.append(
            UpdateItem(
                source="blog",
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                topic=topic,
            ).with_identity()
        )
        seen_urls.add(url)
    return sort_items(items)


def merge_blog_items(index_items: list[UpdateItem], sitemap_items: dict[str, UpdateItem]) -> list[UpdateItem]:
    merged: dict[str, UpdateItem] = {item.url: item for item in index_items}
    for url, sitemap_item in sitemap_items.items():
        current = merged.get(url)
        if current is None:
            merged[url] = sitemap_item
            continue
        merged[url] = UpdateItem(
            source="blog",
            title=current.title or sitemap_item.title,
            url=url,
            published_at=current.published_at or sitemap_item.published_at,
            summary=current.summary,
            topic=current.topic,
            identity=url,
        )
    return sort_items(list(merged.values()))


def parse_x_posts(markdown_text: str) -> list[UpdateItem]:
    if "Markdown Content:" in markdown_text:
        markdown_text = markdown_text.split("Markdown Content:", 1)[1]
    if "Cursor’s posts" in markdown_text:
        markdown_text = markdown_text.split("Cursor’s posts", 1)[1]

    posts: list[str] = []
    current_lines: list[str] = []
    started = False

    def flush_current() -> None:
        if not current_lines:
            return
        text = normalize_text(" ".join(current_lines))
        if text:
            posts.append(text)
        current_lines.clear()

    for raw_line in markdown_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "--------------" or line == "Pinned":
            continue
        if re.match(r"^\[\!\[Image \d+: Square profile picture", line):
            if started:
                flush_current()
            started = True
            continue
        if not started:
            continue
        if line.startswith("![Image "):
            continue
        if re.match(r"^\d+:\d{2}$", line):
            continue
        current_lines.append(line)
    flush_current()

    unique_posts: list[UpdateItem] = []
    seen: set[str] = set()
    for text in posts:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        unique_posts.append(
            UpdateItem(
                source="x",
                title=text,
                url="https://x.com/cursor_ai",
                identity=digest,
            )
        )
        seen.add(digest)
    return unique_posts


def sort_items(items: list[UpdateItem]) -> list[UpdateItem]:
    def sort_key(item: UpdateItem) -> tuple[float, str]:
        published = parse_datetime(item.published_at) if item.published_at else None
        timestamp = published.timestamp() if published else float("-inf")
        return (timestamp, item.title)

    return sorted(items, key=sort_key, reverse=True)


def load_state(path: Path = STATE_PATH) -> dict:
    if not path.exists():
        return json.loads(json.dumps(DEFAULT_STATE))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return json.loads(json.dumps(DEFAULT_STATE))
    if not isinstance(data, dict):
        return json.loads(json.dumps(DEFAULT_STATE))
    seen = data.get("seen") if isinstance(data.get("seen"), dict) else {}
    merged = json.loads(json.dumps(DEFAULT_STATE))
    merged["last_successful_local_date"] = data.get("last_successful_local_date")
    for source in ("changelog", "blog", "x"):
        values = seen.get(source, [])
        if isinstance(values, list):
            merged["seen"][source] = [str(value) for value in values]
    return merged


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def should_run_scheduled(now: datetime, state: dict) -> tuple[bool, str]:
    local_now = to_local(now)
    if local_now.hour != 9:
        return False, f"outside the 09:00 Asia/Shanghai window ({local_now:%Y-%m-%d %H:%M %Z})"
    last_date = state.get("last_successful_local_date")
    if last_date == local_now.date().isoformat():
        return False, f"already completed for {last_date} Asia/Shanghai"
    return True, f"running scheduled check for {local_now.date().isoformat()}"


def select_report_items(source: str, items: list[UpdateItem], state: dict, now: datetime) -> list[UpdateItem]:
    seen = set(state.get("seen", {}).get(source, []))
    if seen:
        return [item for item in items if item.identity not in seen]
    if source == "x":
        return items[:X_FALLBACK_COUNT]

    local_now = to_local(now)
    threshold = local_now - timedelta(days=BOOTSTRAP_DAYS)
    recent = []
    for item in items:
        published = parse_datetime(item.published_at)
        if published and to_local(published) >= threshold:
            recent.append(item)
    if recent:
        return recent
    return items[:BOOTSTRAP_FALLBACK_COUNT]


def extend_seen(state: dict, source: str, items: list[UpdateItem]) -> None:
    current = list(state.setdefault("seen", {}).setdefault(source, []))
    for item in items:
        if item.identity not in current:
            current.append(item.identity)
    state["seen"][source] = current[-MAX_SEEN_PER_SOURCE:]


def fetch_x_items(fetch_text: Callable[[str], str]) -> list[UpdateItem]:
    errors: list[str] = []
    for url in X_MIRROR_URLS:
        try:
            text = fetch_text(url)
            items = parse_x_posts(text)
            if items:
                return items
        except Exception as exc:  # pragma: no cover - covered through fallback behavior in tests
            errors.append(f"{url}: {exc}")
    joined = "\n".join(errors)
    raise RuntimeError(f"Unable to fetch official X posts.\n{joined}")


def format_timestamp(value: str | None) -> str:
    parsed = parse_datetime(value)
    if parsed is None:
        return "unknown time"
    local = to_local(parsed)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def render_section(title: str, items: list[UpdateItem], empty_message: str) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.append(f"- {empty_message}")
        lines.append("")
        return lines

    for item in items:
        if item.source == "x":
            lines.append(f"- {item.title}")
            continue
        timestamp = format_timestamp(item.published_at)
        heading = f"- [{item.title}]({item.url}) - {timestamp}"
        if item.topic:
            heading += f" - {item.topic}"
        lines.append(heading)
        if item.summary:
            lines.append(f"  - {item.summary}")
    lines.append("")
    return lines


def build_report(
    *,
    now: datetime,
    mode: str,
    report_items: dict[str, list[UpdateItem]],
    errors: dict[str, str],
) -> str:
    local_now = to_local(now)
    lines = [
        "# Cursor updates",
        "",
        f"- Generated: {local_now:%Y-%m-%d %H:%M %Z}",
        f"- Mode: {mode}",
        "- Sources: changelog, blog, @cursor_ai on X",
        "",
        "## Summary",
        "",
        f"- Changelog updates: {len(report_items['changelog'])}",
        f"- Blog updates: {len(report_items['blog'])}",
        f"- Official X posts: {len(report_items['x'])}",
        "",
    ]
    if errors:
        lines.extend(["## Source errors", ""])
        for source, message in errors.items():
            lines.append(f"- {source}: {message}")
        lines.append("")
    lines.extend(render_section("Changelog", report_items["changelog"], "No new changelog items."))
    lines.extend(render_section("Blog", report_items["blog"], "No new blog posts."))
    lines.extend(render_section("Official X posts", report_items["x"], "No new official X posts."))
    return "\n".join(lines).strip() + "\n"


def write_report(report_text: str, now: datetime) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT_PATH.write_text(report_text, encoding="utf-8")
    PUBLIC_REPORT_PATH.write_text(report_text, encoding="utf-8")
    history_path = HISTORY_DIR / f"{to_local(now).date().isoformat()}.md"
    history_path.write_text(report_text, encoding="utf-8")


def run_watch(
    *,
    force: bool = False,
    now: datetime | None = None,
    fetch_text: Callable[[str], str] = request_text,
) -> tuple[int, str]:
    now = now or utc_now()
    state = load_state()

    if not force:
        should_run, reason = should_run_scheduled(now, state)
        if not should_run:
            return 0, reason

    errors: dict[str, str] = {}
    all_items: dict[str, list[UpdateItem]] = {
        "changelog": [],
        "blog": [],
        "x": [],
    }

    try:
        all_items["changelog"] = parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))
    except Exception as exc:
        errors["changelog"] = str(exc)

    try:
        blog_index_items = parse_blog_index(fetch_text(BLOG_INDEX_URL))
        blog_sitemap_items = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
        all_items["blog"] = merge_blog_items(blog_index_items, blog_sitemap_items)
    except Exception as exc:
        errors["blog"] = str(exc)

    try:
        all_items["x"] = fetch_x_items(fetch_text)
    except Exception as exc:
        errors["x"] = str(exc)

    report_items = {
        source: select_report_items(source, items, state, now)
        for source, items in all_items.items()
    }
    mode = "forced snapshot" if force else "scheduled daily check"
    report_text = build_report(now=now, mode=mode, report_items=report_items, errors=errors)
    write_report(report_text, now)

    if not force and not errors:
        for source, items in all_items.items():
            extend_seen(state, source, items)
        state["last_successful_local_date"] = to_local(now).date().isoformat()
        save_state(state)

    return 0, report_text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check official Cursor updates.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without 09:00 gating or state updates.",
    )
    args = parser.parse_args(argv)

    exit_code, message = run_watch(force=args.force)
    print(message)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
