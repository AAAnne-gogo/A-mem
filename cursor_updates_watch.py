#!/usr/bin/env python3
"""Fetch and summarize daily Cursor updates from official sources."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo
import xml.etree.ElementTree as ET


CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_TIMELINE_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
OFFICIAL_X_URL = "https://x.com/cursor_ai"

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_HOUR = 9
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_REPORT_PATH = Path("cursor_updates.md")
DEFAULT_STATE_PATH = Path(".cursor_updates/state.json")
REQUEST_TIMEOUT_SECONDS = 20
MAX_SEEN_IDS = 200
BOOTSTRAP_FEED_LIMIT = 5
BOOTSTRAP_X_LIMIT = 8

CONTENT_ENCODED_TAG = "{http://purl.org/rss/1.0/modules/content/}encoded"
SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    source_id: str
    title: str
    summary: str
    url: str
    published_at: str | None = None


@dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    report_path: Path | None = None
    selected_items: dict[str, list[UpdateItem]] | None = None


def fetch_text(url: str, timeout: int = REQUEST_TIMEOUT_SECONDS) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "CursorDailyUpdates/1.0"
            )
        },
    )
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch daily Cursor updates from changelog, blog, and X."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately instead of waiting for the scheduled hour.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone used for scheduling (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=DEFAULT_HOUR,
        help=f"Hour of day to run, in the configured timezone (default: {DEFAULT_HOUR}).",
    )
    parser.add_argument(
        "--lookback-days",
        type=int,
        default=DEFAULT_LOOKBACK_DAYS,
        help=(
            "Initial bootstrap window in days for dated sources such as the "
            "changelog and blog."
        ),
    )
    parser.add_argument(
        "--report-path",
        type=Path,
        default=DEFAULT_REPORT_PATH,
        help=f"Markdown report output path (default: {DEFAULT_REPORT_PATH}).",
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"State file path (default: {DEFAULT_STATE_PATH}).",
    )
    return parser.parse_args()


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def ensure_timezone(dt: datetime, timezone_name: str) -> datetime:
    target_zone = ZoneInfo(timezone_name)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=target_zone)
    return dt.astimezone(target_zone)


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {
            "last_run_date": None,
            "last_run_at": None,
            "timezone": DEFAULT_TIMEZONE,
            "seen": {"changelog": [], "blog": [], "x": []},
        }

    with state_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)

    data.setdefault("last_run_date", None)
    data.setdefault("last_run_at", None)
    data.setdefault("timezone", DEFAULT_TIMEZONE)
    seen = data.setdefault("seen", {})
    seen.setdefault("changelog", [])
    seen.setdefault("blog", [])
    seen.setdefault("x", [])
    return data


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with state_path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def compact_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return compact_whitespace(html.unescape(without_tags))


def clean_summary(value: str, limit: int = 420) -> str:
    cleaned = compact_whitespace(value)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def slug_to_title(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    words = [word for word in slug.split("-") if word]
    if not words:
        return "Untitled blog post"
    return " ".join(word.upper() if word.isupper() else word.capitalize() for word in words)


def title_from_text(text: str, limit: int = 72) -> str:
    cleaned = clean_summary(text, limit)
    return cleaned or "Official X post"


def parse_rfc2822_to_iso(value: str | None) -> str | None:
    if not value:
        return None
    return parsedate_to_datetime(value).astimezone(timezone.utc).isoformat()


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso_date_display(value: str | None) -> str:
    if not value:
        return "Unknown"
    parsed = parse_iso8601(value)
    if parsed is None:
        return value
    return parsed.date().isoformat()


def extract_meta_content(patterns: Iterable[str], html_text: str) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, html_text, re.IGNORECASE | re.DOTALL)
        if match:
            return compact_whitespace(html.unescape(match.group(1)))
    return None


def parse_blog_metadata(html_text: str, url: str) -> tuple[str, str]:
    raw_title = extract_meta_content(
        [
            r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
            r'<title>(.*?)</title>',
        ],
        html_text,
    )
    if raw_title:
        title = re.sub(r"\s*[·|]\s*Cursor\s*$", "", raw_title).strip()
    else:
        title = slug_to_title(url)

    summary = extract_meta_content(
        [
            r'<meta[^>]+name="description"[^>]+content="([^"]+)"',
            r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"',
        ],
        html_text,
    )
    return title, clean_summary(summary or "")


def fetch_changelog_items(fetcher: Callable[[str], str]) -> list[UpdateItem]:
    rss_text = fetcher(CHANGELOG_RSS_URL)
    root = ET.fromstring(rss_text)
    items: list[UpdateItem] = []

    for node in root.findall("./channel/item"):
        link = compact_whitespace(node.findtext("link") or node.findtext("guid") or "")
        title = compact_whitespace(node.findtext("title") or "Untitled changelog item")
        description = node.findtext(CONTENT_ENCODED_TAG) or node.findtext("description") or ""
        summary = clean_summary(strip_html(description))
        published_at = parse_rfc2822_to_iso(node.findtext("pubDate"))
        items.append(
            UpdateItem(
                source="changelog",
                source_id=link or title,
                title=title,
                summary=summary,
                url=link,
                published_at=published_at,
            )
        )

    items.sort(key=lambda item: item.published_at or "", reverse=True)
    return items


def fetch_blog_index(fetcher: Callable[[str], str]) -> list[UpdateItem]:
    sitemap_text = fetcher(BLOG_SITEMAP_URL)
    root = ET.fromstring(sitemap_text)
    items: list[UpdateItem] = []

    for node in root.findall("sm:url", SITEMAP_NS):
        url = compact_whitespace(node.findtext("sm:loc", default="", namespaces=SITEMAP_NS))
        if "/blog/" not in url:
            continue

        items.append(
            UpdateItem(
                source="blog",
                source_id=url,
                title=slug_to_title(url),
                summary="",
                url=url,
                published_at=(
                    node.findtext("sm:lastmod", default=None, namespaces=SITEMAP_NS) or None
                ),
            )
        )

    items.sort(key=lambda item: item.published_at or "", reverse=True)
    return items


def enrich_blog_items(items: Iterable[UpdateItem], fetcher: Callable[[str], str]) -> list[UpdateItem]:
    enriched: list[UpdateItem] = []
    for item in items:
        page_text = fetcher(item.url)
        title, summary = parse_blog_metadata(page_text, item.url)
        enriched.append(
            UpdateItem(
                source=item.source,
                source_id=item.source_id,
                title=title,
                summary=summary,
                url=item.url,
                published_at=item.published_at,
            )
        )
    return enriched


def clean_x_block(block_lines: Iterable[str]) -> str:
    cleaned_lines: list[str] = []
    for line in block_lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"Pinned", "--------------"}:
            continue
        if stripped.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d{2}", stripped):
            continue
        cleaned_lines.append(stripped)

    return compact_whitespace(" ".join(cleaned_lines))


def hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def fetch_x_posts(fetcher: Callable[[str], str]) -> list[UpdateItem]:
    timeline_text = fetcher(X_TIMELINE_MIRROR_URL)
    lines = [line.rstrip() for line in timeline_text.splitlines()]
    items: list[UpdateItem] = []
    current_block: list[str] = []
    in_posts_section = False
    seen_hashes: set[str] = set()

    for line in lines:
        stripped = line.strip()
        if not in_posts_section:
            if stripped == "Cursor’s posts":
                in_posts_section = True
            continue

        if stripped.startswith("[![Image"):
            if current_block:
                post_text = clean_x_block(current_block)
                if post_text:
                    post_hash = hash_text(post_text)
                    if post_hash not in seen_hashes:
                        seen_hashes.add(post_hash)
                        items.append(
                            UpdateItem(
                                source="x",
                                source_id=post_hash,
                                title=title_from_text(post_text),
                                summary=post_text,
                                url=OFFICIAL_X_URL,
                                published_at=None,
                            )
                        )
                current_block = []
            continue

        current_block.append(line)

    if current_block:
        post_text = clean_x_block(current_block)
        if post_text:
            post_hash = hash_text(post_text)
            if post_hash not in seen_hashes:
                items.append(
                    UpdateItem(
                        source="x",
                        source_id=post_hash,
                        title=title_from_text(post_text),
                        summary=post_text,
                        url=OFFICIAL_X_URL,
                        published_at=None,
                    )
                )

    return items


def select_dated_items(
    items: list[UpdateItem],
    seen_ids: set[str],
    now: datetime,
    lookback_days: int,
    bootstrap_limit: int = BOOTSTRAP_FEED_LIMIT,
) -> list[UpdateItem]:
    unseen_items = [item for item in items if item.source_id not in seen_ids]
    if seen_ids:
        return unseen_items

    cutoff = now.astimezone(timezone.utc) - timedelta(days=lookback_days)
    recent_items = [
        item
        for item in items
        if item.published_at and (parse_iso8601(item.published_at) or cutoff) >= cutoff
    ]
    if recent_items:
        return recent_items
    return items[:bootstrap_limit]


def select_x_items(
    items: list[UpdateItem],
    seen_ids: set[str],
    bootstrap_limit: int = BOOTSTRAP_X_LIMIT,
) -> list[UpdateItem]:
    unseen_items = [item for item in items if item.source_id not in seen_ids]
    if seen_ids:
        return unseen_items
    return items[:bootstrap_limit]


def merge_seen_ids(existing: list[str], new_ids: Iterable[str], limit: int = MAX_SEEN_IDS) -> list[str]:
    merged = list(existing)
    for item_id in new_ids:
        if item_id not in merged:
            merged.append(item_id)
    return merged[-limit:]


def render_source_section(title: str, items: list[UpdateItem], note: str | None = None) -> list[str]:
    lines = [f"## {title}", ""]
    if note:
        lines.append(note)
        lines.append("")

    if not items:
        lines.append("- 今日未发现新增内容。")
        lines.append("")
        return lines

    for index, item in enumerate(items, start=1):
        lines.append(f"### {index}. {item.title}")
        if item.published_at:
            lines.append(f"- 日期: {iso_date_display(item.published_at)}")
        lines.append(f"- 链接: {item.url}")
        if item.summary and item.summary != item.title:
            lines.append(f"- 摘要: {item.summary}")
        lines.append("")

    return lines


def build_report(
    now: datetime,
    timezone_name: str,
    selected_items: dict[str, list[UpdateItem]],
) -> str:
    local_date = now.date().isoformat()
    lines = [
        f"# Cursor 每日更新 - {local_date}",
        "",
        f"- 生成时间: {now.isoformat()} ({timezone_name})",
        f"- Changelog 新增: {len(selected_items['changelog'])}",
        f"- Blog 新增: {len(selected_items['blog'])}",
        f"- 官方 X 新增: {len(selected_items['x'])}",
        "",
    ]
    lines.extend(render_source_section("Changelog", selected_items["changelog"]))
    lines.extend(render_source_section("Blog", selected_items["blog"]))
    lines.extend(
        render_source_section(
            "官方 X",
            selected_items["x"],
            note="注：官方 X 时间线通过公开镜像解析，可能不包含独立推文链接与精确发布时间。",
        )
    )
    return "\n".join(lines).rstrip() + "\n"


def should_run_now(
    now: datetime,
    scheduled_hour: int,
    last_run_date: str | None,
    force: bool,
) -> tuple[bool, str]:
    if force:
        return True, "Forced run requested."
    if now.hour != scheduled_hour:
        return (
            False,
            f"Current local hour is {now.hour:02d}, waiting for {scheduled_hour:02d}:00.",
        )
    today = now.date().isoformat()
    if last_run_date == today:
        return False, f"Updates already collected for {today}."
    return True, "Scheduled run window matched."


def run_watcher(
    *,
    now: datetime | None = None,
    fetcher: Callable[[str], str] = fetch_text,
    timezone_name: str = DEFAULT_TIMEZONE,
    scheduled_hour: int = DEFAULT_HOUR,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    report_path: Path = DEFAULT_REPORT_PATH,
    state_path: Path = DEFAULT_STATE_PATH,
    force: bool = False,
) -> RunResult:
    local_now = ensure_timezone(now or now_in_timezone(timezone_name), timezone_name)
    state = load_state(state_path)

    should_run, message = should_run_now(
        local_now,
        scheduled_hour,
        state.get("last_run_date"),
        force,
    )
    if not should_run:
        return RunResult(status="skipped", message=message)

    changelog_items = fetch_changelog_items(fetcher)
    blog_index = fetch_blog_index(fetcher)
    x_items = fetch_x_posts(fetcher)

    selected_changelog = select_dated_items(
        changelog_items,
        set(state["seen"]["changelog"]),
        local_now,
        lookback_days,
    )
    selected_blog = enrich_blog_items(
        select_dated_items(
            blog_index,
            set(state["seen"]["blog"]),
            local_now,
            lookback_days,
        ),
        fetcher,
    )
    selected_x = select_x_items(x_items, set(state["seen"]["x"]))

    selected_items = {
        "changelog": selected_changelog,
        "blog": selected_blog,
        "x": selected_x,
    }
    report_text = build_report(local_now, timezone_name, selected_items)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")

    state["last_run_date"] = local_now.date().isoformat()
    state["last_run_at"] = local_now.isoformat()
    state["timezone"] = timezone_name
    state["seen"]["changelog"] = merge_seen_ids(
        state["seen"]["changelog"], [item.source_id for item in changelog_items]
    )
    state["seen"]["blog"] = merge_seen_ids(
        state["seen"]["blog"], [item.source_id for item in blog_index]
    )
    state["seen"]["x"] = merge_seen_ids(
        state["seen"]["x"], [item.source_id for item in x_items]
    )
    save_state(state_path, state)

    return RunResult(
        status="success",
        message=(
            "Collected "
            f"{len(selected_changelog)} changelog item(s), "
            f"{len(selected_blog)} blog post(s), and "
            f"{len(selected_x)} X post(s)."
        ),
        report_path=report_path,
        selected_items=selected_items,
    )


def main() -> int:
    args = parse_args()
    result = run_watcher(
        timezone_name=args.timezone,
        scheduled_hour=args.hour,
        lookback_days=args.lookback_days,
        report_path=args.report_path,
        state_path=args.state_path,
        force=args.force,
    )
    print(result.message)
    if result.report_path:
        print(f"Report written to {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
