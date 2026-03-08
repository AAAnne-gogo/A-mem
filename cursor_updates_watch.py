#!/usr/bin/env python3
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
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


ASIA_SHANGHAI = ZoneInfo("Asia/Shanghai")
CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
BLOG_MIRROR_PREFIX = "https://r.jina.ai/http://"
X_MIRROR_URL = "https://r.jina.ai/http://www.x.com/cursor_ai"
BOOTSTRAP_DAYS = 7
BOOTSTRAP_X_POSTS = 8
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}
DEFAULT_STATE_PATH = Path(".cursor_updates/state.json")
DEFAULT_REPORT_PATH = Path("cursor_updates.md")


@dataclass(frozen=True)
class UpdateItem:
    source: str
    item_id: str
    title: str
    link: str
    published: str | None
    summary: str


@dataclass(frozen=True)
class SourceSnapshot:
    source: str
    fetched_items: list[UpdateItem]
    selected_items: list[UpdateItem]
    error: str | None = None


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_html(value: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", value)
    return normalize_whitespace(html.unescape(without_tags))


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    trimmed = value.strip()
    if not trimmed:
        return None

    try:
        parsed = parsedate_to_datetime(trimmed)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except (TypeError, ValueError, IndexError):
        pass

    try:
        normalized = trimmed.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed
    except ValueError:
        return None


def datetime_to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def format_report_time(value: datetime | None) -> str:
    if value is None:
        return "未知"
    return value.astimezone(ASIA_SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %Z")


def fetch_text(url: str, timeout: int = 60) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def extract_jina_metadata(markdown_text: str) -> tuple[str | None, datetime | None, str]:
    title = None
    published = None

    for line in markdown_text.splitlines():
        if line.startswith("Title: "):
            title = line.removeprefix("Title: ").strip()
        elif line.startswith("Published Time: "):
            published = parse_datetime(line.removeprefix("Published Time: ").strip())

    if "Markdown Content:" not in markdown_text:
        return title, published, ""

    body = markdown_text.split("Markdown Content:", 1)[1].strip()
    return title, published, body


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel")
    if channel is None:
        return []

    items: list[UpdateItem] = []
    for node in channel.findall("item"):
        title = normalize_whitespace(node.findtext("title", default=""))
        link = normalize_whitespace(node.findtext("link", default=""))
        published = parse_datetime(node.findtext("pubDate"))
        description = strip_html(node.findtext("description", default=""))

        if not title or not link:
            continue

        items.append(
            UpdateItem(
                source="changelog",
                item_id=link,
                title=title,
                link=link,
                published=datetime_to_iso(published),
                summary=description,
            )
        )

    items.sort(
        key=lambda item: parse_datetime(item.published) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, datetime | None]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[tuple[str, datetime | None]] = []

    for node in root.findall("sm:url", namespace):
        loc = normalize_whitespace(node.findtext("sm:loc", default="", namespaces=namespace))
        if not loc:
            continue

        parsed = urlparse(loc)
        if parsed.netloc not in {"cursor.com", "www.cursor.com"}:
            continue
        if not parsed.path.startswith("/blog/"):
            continue
        if parsed.path in {"/blog/", "/blog"}:
            continue

        lastmod = parse_datetime(node.findtext("sm:lastmod", default="", namespaces=namespace))
        entries.append((loc.replace("https://www.cursor.com", "https://cursor.com"), lastmod))

    entries.sort(key=lambda entry: entry[1] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return entries


def parse_blog_article(markdown_text: str, fallback_url: str, fallback_published: datetime | None) -> UpdateItem:
    title, published, body = extract_jina_metadata(markdown_text)
    summary = ""

    for line in body.splitlines():
        stripped = line.strip()
        if not stripped:
            if summary:
                break
            continue
        if stripped.startswith(("![", "[![")):
            continue
        if set(stripped) == {"-"}:
            continue
        if stripped.startswith("[]("):
            continue
        summary = normalize_whitespace(stripped)
        break

    final_title = title or fallback_url.rsplit("/", 1)[-1].replace("-", " ").title()
    return UpdateItem(
        source="blog",
        item_id=fallback_url,
        title=final_title,
        link=fallback_url,
        published=datetime_to_iso(published or fallback_published),
        summary=summary,
    )


def parse_x_markdown(markdown_text: str) -> tuple[datetime | None, list[UpdateItem]]:
    _, fetched_at, body = extract_jina_metadata(markdown_text)
    if not body:
        return fetched_at, []

    lines = body.splitlines()
    started = False
    current_lines: list[str] = []
    raw_posts: list[str] = []

    def flush_current() -> None:
        nonlocal current_lines
        text = normalize_whitespace(" ".join(current_lines))
        if text:
            raw_posts.append(text)
        current_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        if not started:
            if line == "Cursor’s posts":
                started = True
            continue

        if not line:
            flush_current()
            continue

        if line == "--------------" or line == "Pinned":
            continue

        if line.startswith("[![Image"):
            flush_current()
            continue

        if line.startswith("![Image"):
            continue

        if re.fullmatch(r"\d+:\d{2}", line):
            continue

        if line in {
            "Cursor",
            "@cursor_ai",
            "The best way to code with AI.",
            "See everything new in Cursor:",
        }:
            continue

        current_lines.append(line)

    flush_current()

    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    for post_text in raw_posts:
        item_id = hashlib.sha256(post_text.encode("utf-8")).hexdigest()
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        title = post_text if len(post_text) <= 120 else post_text[:117].rstrip() + "..."
        items.append(
            UpdateItem(
                source="x",
                item_id=item_id,
                title=title,
                link="https://x.com/cursor_ai",
                published=datetime_to_iso(fetched_at),
                summary=post_text,
            )
        )

    return fetched_at, items


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"last_run_date": None, "seen": {"changelog": [], "blog": [], "x": []}}

    raw = json.loads(state_path.read_text(encoding="utf-8"))
    return {
        "last_run_date": raw.get("last_run_date"),
        "seen": {
            "changelog": list(raw.get("seen", {}).get("changelog", [])),
            "blog": list(raw.get("seen", {}).get("blog", [])),
            "x": list(raw.get("seen", {}).get("x", [])),
        },
    }


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def should_run(now: datetime, state: dict, force: bool = False) -> bool:
    if force:
        return True
    return now.hour == 9 and state.get("last_run_date") != now.date().isoformat()


def select_items_for_report(
    source: str,
    items: list[UpdateItem],
    seen_ids: set[str],
    now: datetime,
) -> list[UpdateItem]:
    if seen_ids:
        return [item for item in items if item.item_id not in seen_ids]

    if source == "x":
        return items[:BOOTSTRAP_X_POSTS]

    cutoff = now.astimezone(timezone.utc) - timedelta(days=BOOTSTRAP_DAYS)
    selected: list[UpdateItem] = []
    for item in items:
        published = parse_datetime(item.published)
        if published and published >= cutoff:
            selected.append(item)
    return selected


def fetch_changelog_snapshot(now: datetime, seen_ids: set[str]) -> SourceSnapshot:
    try:
        items = parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))
        return SourceSnapshot(
            source="changelog",
            fetched_items=items,
            selected_items=select_items_for_report("changelog", items, seen_ids, now),
        )
    except (ET.ParseError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        return SourceSnapshot("changelog", [], [], f"{type(exc).__name__}: {exc}")


def choose_blog_candidates(entries: list[tuple[str, datetime | None]], seen_ids: set[str]) -> list[tuple[str, datetime | None]]:
    candidates: list[tuple[str, datetime | None]] = []
    for url, published in entries:
        if url in seen_ids:
            continue
        candidates.append((url, published))
        if len(candidates) >= 8:
            break

    if candidates:
        return candidates
    return entries[:4]


def fetch_blog_snapshot(now: datetime, seen_ids: set[str]) -> SourceSnapshot:
    try:
        entries = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
        candidates = choose_blog_candidates(entries, seen_ids)
        items: list[UpdateItem] = []
        for url, published in candidates:
            markdown = fetch_text(BLOG_MIRROR_PREFIX + url.removeprefix("https://"))
            items.append(parse_blog_article(markdown, fallback_url=url, fallback_published=published))
        return SourceSnapshot(
            source="blog",
            fetched_items=items,
            selected_items=select_items_for_report("blog", items, seen_ids, now),
        )
    except (ET.ParseError, HTTPError, URLError, TimeoutError, ValueError) as exc:
        return SourceSnapshot("blog", [], [], f"{type(exc).__name__}: {exc}")


def fetch_x_snapshot(now: datetime, seen_ids: set[str]) -> SourceSnapshot:
    try:
        _, items = parse_x_markdown(fetch_text(X_MIRROR_URL, timeout=90))
        return SourceSnapshot(
            source="x",
            fetched_items=items,
            selected_items=select_items_for_report("x", items, seen_ids, now),
        )
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return SourceSnapshot("x", [], [], f"{type(exc).__name__}: {exc}")


def truncate_seen(items: list[str], limit: int = 200) -> list[str]:
    if len(items) <= limit:
        return items
    return items[-limit:]


def update_state_with_snapshots(state: dict, snapshots: list[SourceSnapshot], now: datetime) -> dict:
    next_state = {
        "last_run_date": now.date().isoformat(),
        "seen": {
            "changelog": list(state.get("seen", {}).get("changelog", [])),
            "blog": list(state.get("seen", {}).get("blog", [])),
            "x": list(state.get("seen", {}).get("x", [])),
        },
    }

    for snapshot in snapshots:
        key = snapshot.source
        existing = next_state["seen"].get(key, [])
        existing.extend(item.item_id for item in snapshot.fetched_items)
        deduped = list(dict.fromkeys(existing))
        next_state["seen"][key] = truncate_seen(deduped)

    return next_state


def render_section(title: str, snapshot: SourceSnapshot, time_label: str) -> str:
    lines = [f"## {title}", ""]
    if snapshot.error:
        lines.append(f"- 抓取失败：{snapshot.error}")
        lines.append("")
        return "\n".join(lines)

    if not snapshot.selected_items:
        lines.append("- 暂无新增")
        lines.append("")
        return "\n".join(lines)

    for item in snapshot.selected_items:
        lines.append(f"### {item.title}")
        if item.published:
            lines.append(f"- {time_label}：{format_report_time(parse_datetime(item.published))}")
        lines.append(f"- 链接：{item.link}")
        if item.summary:
            lines.append(f"- 摘要：{item.summary}")
        lines.append("")
    return "\n".join(lines)


def render_report(
    now: datetime,
    snapshots: list[SourceSnapshot],
    *,
    force: bool,
    persisted_state: bool,
) -> str:
    snapshot_map = {snapshot.source: snapshot for snapshot in snapshots}
    counts = {
        key: len(snapshot_map[key].selected_items) for key in ("changelog", "blog", "x")
    }

    lines = [
        "# Cursor 每日更新",
        "",
        f"- 生成时间：{format_report_time(now)}",
        f"- 运行模式：{'强制刷新' if force else '定时检查'}",
        f"- 状态持久化：{'已更新' if persisted_state else '未更新'}",
        f"- 本次新增：changelog {counts['changelog']} 条，blog {counts['blog']} 条，官方 X {counts['x']} 条",
        "",
        render_section("Changelog", snapshot_map["changelog"], "发布时间"),
        render_section("Blog", snapshot_map["blog"], "发布时间"),
        render_section("官方 X", snapshot_map["x"], "抓取时间"),
        "## 数据来源",
        "",
        f"- Changelog RSS：{CHANGELOG_RSS_URL}",
        f"- Blog sitemap：{BLOG_SITEMAP_URL}",
        f"- Blog 正文镜像：{BLOG_MIRROR_PREFIX}<article-url>",
        f"- 官方 X 镜像：{X_MIRROR_URL}",
        "- 定时策略：仅在 Asia/Shanghai 09:00 小时内执行一次；传入 --force 可跳过时间门控。",
        "",
    ]
    return "\n".join(lines)


def run_watch(
    *,
    now: datetime | None = None,
    force: bool = False,
    update_state: bool = False,
    state_path: Path = DEFAULT_STATE_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    changelog_fetcher: Callable[[datetime, set[str]], SourceSnapshot] = fetch_changelog_snapshot,
    blog_fetcher: Callable[[datetime, set[str]], SourceSnapshot] = fetch_blog_snapshot,
    x_fetcher: Callable[[datetime, set[str]], SourceSnapshot] = fetch_x_snapshot,
) -> tuple[bool, str]:
    current_time = now or datetime.now(ASIA_SHANGHAI)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=ASIA_SHANGHAI)
    else:
        current_time = current_time.astimezone(ASIA_SHANGHAI)

    state = load_state(state_path)
    if not should_run(current_time, state, force=force):
        message = (
            "Skip: outside Asia/Shanghai 09:00 window or already completed today. "
            f"Now={format_report_time(current_time)}"
        )
        return False, message

    seen = state.get("seen", {})
    snapshots = [
        changelog_fetcher(current_time, set(seen.get("changelog", []))),
        blog_fetcher(current_time, set(seen.get("blog", []))),
        x_fetcher(current_time, set(seen.get("x", []))),
    ]

    persist_state = (not force) or update_state
    report = render_report(current_time, snapshots, force=force, persisted_state=persist_state)
    report_path.write_text(report, encoding="utf-8")

    if persist_state:
        save_state(state_path, update_state_with_snapshots(state, snapshots, current_time))

    return True, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch daily Cursor changelog, blog, and X updates.")
    parser.add_argument("--force", action="store_true", help="Bypass the 09:00 Asia/Shanghai time gate.")
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="Persist seen items even when running with --force.",
    )
    parser.add_argument("--state-path", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--report-path", type=Path, default=DEFAULT_REPORT_PATH)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    did_run, output = run_watch(
        force=args.force,
        update_state=args.update_state,
        state_path=args.state_path,
        report_path=args.report_path,
    )
    print(output)
    return 0 if did_run else 0


if __name__ == "__main__":
    raise SystemExit(main())
