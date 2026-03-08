#!/usr/bin/env python3
"""Daily Cursor updates watcher.

Collects new Cursor updates from:
- Cursor changelog RSS
- Cursor blog pages discovered from the marketing sitemap
- Cursor official X account via the public syndication timeline

Designed for an hourly automation trigger but only performs a real update
during the 09:00 hour in Asia/Shanghai by default.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_TIMELINE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/cursor_ai"

DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_HOUR = 9
DEFAULT_BOOTSTRAP_DAYS = 7
DEFAULT_BOOTSTRAP_X_POSTS = 8
MAX_SEEN_IDS_PER_SOURCE = 200
REPORT_PATH = Path("cursor_updates.md")
STATE_PATH = Path(".cursor_updates/state.json")
HTTP_TIMEOUT_SECONDS = 30

RSS_NS = {
    "content": "http://purl.org/rss/1.0/modules/content/",
}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    item_id: str
    title: str
    summary: str
    url: str
    published_at: datetime | None


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def fetch_text(url: str, *, headers: dict[str, str] | None = None) -> str:
    request_headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        request_headers.update(headers)

    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def parse_rfc2822_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def strip_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    unescaped = unescape(without_tags)
    return re.sub(r"\s+", " ", unescaped).strip()


def shorten(value: str, limit: int = 240) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []

    for item in root.findall("./channel/item"):
        title = (item.findtext("title") or "").strip()
        url = (item.findtext("link") or "").strip()
        item_id = (item.findtext("guid") or url or title).strip()
        description = item.findtext("description") or ""
        content = item.findtext("content:encoded", default="", namespaces=RSS_NS) or ""
        summary = strip_html(content or description)
        published_at = parse_rfc2822_datetime(item.findtext("pubDate"))
        items.append(
            UpdateItem(
                source="changelog",
                item_id=item_id,
                title=title,
                summary=shorten(summary, 280),
                url=url,
                published_at=published_at,
            )
        )

    return items


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, datetime | None]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[tuple[str, datetime | None]] = []

    for url_node in root.findall("sm:url", namespace):
        loc = (url_node.findtext("sm:loc", default="", namespaces=namespace) or "").strip()
        if not loc.startswith("https://cursor.com/blog/"):
            continue

        parsed = urllib.parse.urlparse(loc)
        if re.fullmatch(r"/blog/[^/]+", parsed.path) is None:
            continue

        lastmod_text = url_node.findtext("sm:lastmod", default="", namespaces=namespace)
        entries.append((loc, parse_iso_datetime(lastmod_text)))

    entries.sort(key=lambda pair: pair[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return entries


def _extract_meta_content(html_text: str, property_name: str) -> str:
    patterns = [
        rf'<meta[^>]+property="{re.escape(property_name)}"[^>]+content="([^"]+)"',
        rf"<meta[^>]+property='{re.escape(property_name)}'[^>]+content='([^']+)'",
        rf'<meta[^>]+content="([^"]+)"[^>]+property="{re.escape(property_name)}"',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return unescape(match.group(1)).strip()
    return ""


def _extract_first(pattern: str, html_text: str) -> str:
    match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    return unescape(match.group(1)).strip()


def parse_blog_article(html_text: str, url: str, fallback_published_at: datetime | None) -> UpdateItem:
    title = _extract_meta_content(html_text, "og:title")
    if title.endswith(" · Cursor"):
        title = title[: -len(" · Cursor")].rstrip()

    summary = _extract_meta_content(html_text, "og:description")
    if not summary:
        summary = _extract_first(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html_text)

    published_raw = _extract_first(r'<time[^>]+dateTime="([^"]+)"', html_text)
    published_at = parse_iso_datetime(published_raw) or fallback_published_at

    item_id = url.rstrip("/")
    return UpdateItem(
        source="blog",
        item_id=item_id,
        title=title or url.rsplit("/", 1)[-1].replace("-", " "),
        summary=shorten(summary, 280),
        url=url,
        published_at=published_at,
    )


def _expand_urls(text: str, entities: dict) -> str:
    replacements: dict[str, str] = {}
    for url_entity in entities.get("urls", []):
        short_url = url_entity.get("url")
        expanded_url = url_entity.get("expanded_url") or url_entity.get("display_url")
        if short_url and expanded_url:
            replacements[short_url] = expanded_url

    for media_entity in entities.get("media", []):
        short_url = media_entity.get("url")
        if short_url:
            replacements[short_url] = ""

    updated = text
    for short_url, replacement in replacements.items():
        updated = updated.replace(short_url, replacement)

    return re.sub(r"\s+", " ", updated).strip()


def parse_x_timeline(html_text: str) -> list[UpdateItem]:
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("Could not locate __NEXT_DATA__ in X syndication response.")

    payload = json.loads(match.group(1))
    entries = payload["props"]["pageProps"]["timeline"]["entries"]
    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    seen_texts: set[str] = set()

    for entry in entries:
        if entry.get("type") != "tweet":
            continue

        tweet = entry.get("content", {}).get("tweet")
        if not tweet:
            continue

        tweet_id = tweet.get("id_str") or tweet.get("conversation_id_str")
        if not tweet_id or tweet_id in seen_ids:
            continue

        full_text = tweet.get("full_text") or tweet.get("text") or ""
        expanded_text = _expand_urls(full_text, tweet.get("entities", {}))
        normalized_text = re.sub(r"\s+", " ", expanded_text).strip()
        if not normalized_text or normalized_text in seen_texts:
            continue

        permalink = tweet.get("permalink") or f"/cursor_ai/status/{tweet_id}"
        created_at = parse_rfc2822_datetime(tweet.get("created_at"))
        item = UpdateItem(
            source="x",
            item_id=tweet_id,
            title=shorten(normalized_text, 90),
            summary=shorten(normalized_text, 280),
            url=f"https://x.com{permalink}",
            published_at=created_at,
        )
        items.append(item)
        seen_ids.add(tweet_id)
        seen_texts.add(normalized_text)

    return items


def load_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def filter_new_items(
    items: list[UpdateItem],
    *,
    source: str,
    state: dict,
    bootstrap_cutoff: datetime,
    bootstrap_x_posts: int,
) -> list[UpdateItem]:
    seen_ids = set(state.get("seen_ids", {}).get(source, []))
    filtered = [item for item in items if item.item_id not in seen_ids]

    if seen_ids:
        return filtered

    if source == "x":
        return filtered[:bootstrap_x_posts]

    recent_items: list[UpdateItem] = []
    for item in filtered:
        if item.published_at and item.published_at >= bootstrap_cutoff:
            recent_items.append(item)
    return recent_items


def build_report(
    *,
    generated_at: datetime,
    timezone_name: str,
    new_items: dict[str, list[UpdateItem]],
) -> str:
    total_items = sum(len(items) for items in new_items.values())
    lines = [
        "# Cursor 每日更新",
        "",
        f"- 生成时间：{generated_at.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 时区：{timezone_name}",
        "- 来源：Cursor changelog、Cursor blog、官方 X（@cursor_ai）",
        f"- 本次新增：{total_items} 条",
        "",
        "## 概览",
        "",
        f"- Changelog：{len(new_items['changelog'])} 条",
        f"- Blog：{len(new_items['blog'])} 条",
        f"- 官方 X：{len(new_items['x'])} 条",
        "",
    ]

    source_titles = {
        "changelog": "Changelog",
        "blog": "Blog",
        "x": "官方 X",
    }

    for source_key in ("changelog", "blog", "x"):
        lines.append(f"## {source_titles[source_key]}")
        lines.append("")
        items = new_items[source_key]
        if not items:
            lines.append("- 今日没有检测到新的内容。")
            lines.append("")
            continue

        for index, item in enumerate(items, start=1):
            published = (
                item.published_at.astimezone(generated_at.tzinfo).strftime("%Y-%m-%d %H:%M")
                if item.published_at
                else "未知"
            )
            lines.extend(
                [
                    f"### {index}. {item.title}",
                    f"- 时间：{published}",
                    f"- 链接：{item.url}",
                    f"- 摘要：{item.summary or '（无摘要）'}",
                    "",
                ]
            )

    return "\n".join(lines).rstrip() + "\n"


def update_state_with_items(
    state: dict,
    *,
    run_at: datetime,
    timezone_name: str,
    items_by_source: dict[str, list[UpdateItem]],
) -> dict:
    new_state = dict(state)
    seen_ids = {
        source: list(state.get("seen_ids", {}).get(source, []))
        for source in ("changelog", "blog", "x")
    }

    for source, items in items_by_source.items():
        combined = [item.item_id for item in items] + seen_ids[source]
        deduped: list[str] = []
        for item_id in combined:
            if item_id not in deduped:
                deduped.append(item_id)
        seen_ids[source] = deduped[:MAX_SEEN_IDS_PER_SOURCE]

    new_state["seen_ids"] = seen_ids
    new_state["last_checked_at"] = run_at.isoformat()
    new_state["last_run_date"] = run_at.astimezone(ZoneInfo(timezone_name)).date().isoformat()
    return new_state


def should_run_now(
    *,
    force: bool,
    state: dict,
    now_local: datetime,
    run_hour: int,
) -> tuple[bool, str]:
    if force:
        return True, "forced run"

    if now_local.hour != run_hour:
        return False, f"outside scheduled window: current hour is {now_local.hour:02d}"

    last_run_date = state.get("last_run_date")
    if last_run_date == now_local.date().isoformat():
        return False, "already completed for this local date"

    return True, "within scheduled window"


def fetch_changelog_items() -> list[UpdateItem]:
    return parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))


def fetch_blog_items(candidate_limit: int = 12) -> list[UpdateItem]:
    sitemap_entries = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
    items: list[UpdateItem] = []

    for url, fallback_published_at in sitemap_entries[:candidate_limit]:
        try:
            article_html = fetch_text(url)
            items.append(parse_blog_article(article_html, url, fallback_published_at))
        except urllib.error.URLError as exc:
            print(f"warning: failed to fetch blog article {url}: {exc}")

    items.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return items


def fetch_x_items() -> list[UpdateItem]:
    return parse_x_timeline(fetch_text(X_TIMELINE_URL))


def run(
    *,
    force: bool,
    update_state: bool,
    timezone_name: str,
    run_hour: int,
    bootstrap_days: int,
    bootstrap_x_posts: int,
    report_path: Path,
    state_path: Path,
) -> int:
    state = load_state(state_path)
    local_now = now_in_timezone(timezone_name)

    should_run, reason = should_run_now(
        force=force,
        state=state,
        now_local=local_now,
        run_hour=run_hour,
    )
    print(f"run check: {reason}")
    if not should_run:
        return 0

    changelog_items = fetch_changelog_items()
    blog_items = fetch_blog_items()
    x_items = fetch_x_items()

    bootstrap_cutoff = local_now.astimezone(timezone.utc) - timedelta(days=bootstrap_days)
    new_items = {
        "changelog": filter_new_items(
            changelog_items,
            source="changelog",
            state=state,
            bootstrap_cutoff=bootstrap_cutoff,
            bootstrap_x_posts=bootstrap_x_posts,
        ),
        "blog": filter_new_items(
            blog_items,
            source="blog",
            state=state,
            bootstrap_cutoff=bootstrap_cutoff,
            bootstrap_x_posts=bootstrap_x_posts,
        ),
        "x": filter_new_items(
            x_items,
            source="x",
            state=state,
            bootstrap_cutoff=bootstrap_cutoff,
            bootstrap_x_posts=bootstrap_x_posts,
        ),
    }

    report = build_report(
        generated_at=local_now,
        timezone_name=timezone_name,
        new_items=new_items,
    )
    report_path.write_text(report, encoding="utf-8")
    print(f"wrote report to {report_path}")

    if force and not update_state:
        print("state unchanged for forced run without --update-state")
        return 0

    updated_state = update_state_with_items(
        state,
        run_at=local_now,
        timezone_name=timezone_name,
        items_by_source={
            "changelog": changelog_items,
            "blog": blog_items,
            "x": x_items,
        },
    )
    save_state(state_path, updated_state)
    print(f"updated state at {state_path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate daily Cursor update summaries.")
    parser.add_argument("--force", action="store_true", help="Run immediately, ignoring the 09:00 gate.")
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="When used with --force, also persist state after the run.",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA timezone name.")
    parser.add_argument("--run-hour", type=int, default=DEFAULT_RUN_HOUR, help="Scheduled run hour.")
    parser.add_argument(
        "--bootstrap-days",
        type=int,
        default=DEFAULT_BOOTSTRAP_DAYS,
        help="How many days of dated content to include on first run.",
    )
    parser.add_argument(
        "--bootstrap-x-posts",
        type=int,
        default=DEFAULT_BOOTSTRAP_X_POSTS,
        help="How many recent X posts to include on first run.",
    )
    parser.add_argument(
        "--report-path",
        default=str(REPORT_PATH),
        help="Path to the generated markdown report.",
    )
    parser.add_argument(
        "--state-path",
        default=str(STATE_PATH),
        help="Path to the watcher state file.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(
        force=args.force,
        update_state=args.update_state,
        timezone_name=args.timezone,
        run_hour=args.run_hour,
        bootstrap_days=args.bootstrap_days,
        bootstrap_x_posts=args.bootstrap_x_posts,
        report_path=Path(args.report_path),
        state_path=Path(args.state_path),
    )


if __name__ == "__main__":
    raise SystemExit(main())
