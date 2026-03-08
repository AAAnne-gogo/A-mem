from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_DIR = ROOT / ".cursor_updates"
DEFAULT_STATE_FILE = DEFAULT_STATE_DIR / "state.json"
DEFAULT_REPORT_FILE = DEFAULT_STATE_DIR / "latest_report.md"

CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_URL = "https://cursor.com/blog"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_ACCOUNT_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)

TARGET_TIMEZONE = ZoneInfo("Asia/Shanghai")
TARGET_HOUR = 9
BOOTSTRAP_LOOKBACK_DAYS = 7
MAX_SEEN_IDS = 200
DEFAULT_X_POST_LIMIT = 8


@dataclass(frozen=True)
class UpdateItem:
    source: str
    item_id: str
    title: str
    summary: str
    url: str
    published_at: str
    source_label: str


@dataclass(frozen=True)
class ScheduleDecision:
    should_run: bool
    reason: str
    local_now: datetime


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 CursorUpdatesWatch/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def normalize_whitespace(value: str) -> str:
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def clean_html_text(value: str) -> str:
    without_comments = re.sub(r"<!--.*?-->", " ", value, flags=re.S)
    without_tags = re.sub(r"<[^>]+>", " ", without_comments)
    unescaped = html.unescape(without_tags)
    return normalize_whitespace(unescaped)


def truncate_text(value: str, limit: int = 320) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def iso_to_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def localize_time(value: str) -> str:
    return iso_to_datetime(value).astimezone(TARGET_TIMEZONE).strftime("%Y-%m-%d %H:%M")


def stable_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_state(path: Path) -> dict:
    default_state = {
        "last_successful_local_date": None,
        "last_successful_run_at": None,
        "seen": {"changelog": [], "blog": [], "x": []},
    }
    if not path.exists():
        return default_state
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state
    seen = data.get("seen", {})
    return {
        "last_successful_local_date": data.get("last_successful_local_date"),
        "last_successful_run_at": data.get("last_successful_run_at"),
        "seen": {
            "changelog": list(seen.get("changelog", [])),
            "blog": list(seen.get("blog", [])),
            "x": list(seen.get("x", [])),
        },
    }


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def should_run_now(
    state: dict,
    now_utc: datetime | None = None,
    timezone_name: str = "Asia/Shanghai",
    target_hour: int = TARGET_HOUR,
) -> ScheduleDecision:
    if now_utc is None:
        now_utc = datetime.now(timezone.utc)
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))
    if local_now.hour != target_hour:
        return ScheduleDecision(
            should_run=False,
            reason=f"Current local hour is {local_now.hour:02d}, waiting for {target_hour:02d}:00 in {timezone_name}.",
            local_now=local_now,
        )
    if state.get("last_successful_local_date") == local_now.date().isoformat():
        return ScheduleDecision(
            should_run=False,
            reason=f"Already ran for local date {local_now.date().isoformat()}.",
            local_now=local_now,
        )
    return ScheduleDecision(
        should_run=True,
        reason=f"Scheduled run allowed for {local_now.date().isoformat()} {local_now.strftime('%H:%M')} {timezone_name}.",
        local_now=local_now,
    )


def summarize_paragraphs(paragraphs: Iterable[str], max_paragraphs: int = 2) -> str:
    selected = []
    for paragraph in paragraphs:
        cleaned = normalize_whitespace(paragraph)
        if not cleaned:
            continue
        selected.append(cleaned)
        if len(selected) >= max_paragraphs:
            break
    return truncate_text(" ".join(selected))


def parse_changelog_html(html_text: str) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    for block in re.findall(r"<article.*?</article>", html_text, flags=re.S | re.I):
        title_match = re.search(
            r"<h1[^>]*>.*?<a[^>]+href=\"([^\"]+)\"[^>]*>(.*?)</a>.*?</h1>",
            block,
            flags=re.S | re.I,
        )
        date_match = re.search(r"<time[^>]+dateTime=\"([^\"]+)\"", block, flags=re.I)
        if not title_match or not date_match:
            continue
        paragraphs = []
        for paragraph_html in re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.S | re.I):
            if "<time" in paragraph_html.lower():
                continue
            cleaned = clean_html_text(paragraph_html)
            if cleaned:
                paragraphs.append(cleaned)
        relative_url = title_match.group(1)
        title = clean_html_text(title_match.group(2))
        item_url = urljoin(CHANGELOG_URL, relative_url)
        items.append(
            UpdateItem(
                source="changelog",
                item_id=item_url,
                title=title,
                summary=summarize_paragraphs(paragraphs),
                url=item_url,
                published_at=date_match.group(1),
                source_label="Cursor Changelog",
            )
        )
    return items


def parse_blog_html(html_text: str) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    for block in re.findall(r"<article.*?</article>", html_text, flags=re.S | re.I):
        link_match = re.search(r"<a[^>]+href=\"(/blog/[^\"]+)\"", block, flags=re.I)
        date_match = re.search(r"<time[^>]+dateTime=\"([^\"]+)\"", block, flags=re.I)
        paragraph_matches = re.findall(r"<p[^>]*>(.*?)</p>", block, flags=re.S | re.I)
        if not link_match or not date_match or len(paragraph_matches) < 2:
            continue
        title = clean_html_text(paragraph_matches[0])
        summary = clean_html_text(paragraph_matches[1])
        category_match = re.search(r"<span[^>]*>(.*?)</span>", block, flags=re.S | re.I)
        category = clean_html_text(category_match.group(1)) if category_match else ""
        category = re.sub(r"[·\s]+$", "", category)
        full_summary = summary if not category else f"{summary} ({category})"
        item_url = urljoin(BLOG_URL, link_match.group(1))
        items.append(
            UpdateItem(
                source="blog",
                item_id=item_url,
                title=title,
                summary=truncate_text(full_summary),
                url=item_url,
                published_at=date_match.group(1),
                source_label="Cursor Blog",
            )
        )
    return items


def parse_blog_sitemap(xml_text: str) -> dict[str, str]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ElementTree.fromstring(xml_text)
    entries: dict[str, str] = {}
    for url_node in root.findall("sm:url", namespace):
        loc_node = url_node.find("sm:loc", namespace)
        lastmod_node = url_node.find("sm:lastmod", namespace)
        if loc_node is None or lastmod_node is None:
            continue
        location = (loc_node.text or "").strip()
        if "/blog/" not in location:
            continue
        entries[location] = (lastmod_node.text or "").strip()
    return entries


def parse_x_markdown(markdown_text: str, fetched_at: datetime, limit: int = DEFAULT_X_POST_LIMIT) -> list[UpdateItem]:
    if "Cursor’s posts" in markdown_text:
        markdown_text = markdown_text.split("Cursor’s posts", 1)[1]
    lines = markdown_text.splitlines()
    posts: list[UpdateItem] = []
    seen_ids: set[str] = set()
    current_lines: list[str] = []
    pending_pinned = False
    current_pinned = False
    in_post = False

    def flush_current() -> None:
        nonlocal current_lines, current_pinned
        if not current_lines:
            return
        text = normalize_whitespace(" ".join(current_lines))
        current_lines = []
        if len(text) < 20:
            current_pinned = False
            return
        item_id = stable_hash(text)
        if item_id in seen_ids:
            current_pinned = False
            return
        seen_ids.add(item_id)
        title = text if len(text) <= 88 else text[:85].rstrip() + "..."
        if current_pinned:
            title = "[Pinned] " + title
        posts.append(
            UpdateItem(
                source="x",
                item_id=item_id,
                title=title,
                summary=text,
                url=X_ACCOUNT_URL,
                published_at=fetched_at.isoformat(),
                source_label="Cursor X",
            )
        )
        current_pinned = False

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line == "--------------":
            continue
        if line == "Pinned":
            pending_pinned = True
            continue
        if line.startswith("[![Image") and "profile picture" in line.lower():
            if in_post:
                flush_current()
            in_post = True
            current_pinned = pending_pinned
            pending_pinned = False
            continue
        if not in_post:
            continue
        if line.startswith("[![Image") or line.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d{2}", line):
            continue
        if line in {"Cursor", "@cursor_ai", "The best way to code with AI."}:
            continue
        current_lines.append(line)
        if len(posts) >= limit:
            break

    if len(posts) < limit:
        flush_current()

    return posts[:limit]


def dedupe_items(items: Iterable[UpdateItem]) -> list[UpdateItem]:
    seen_ids: set[str] = set()
    deduped: list[UpdateItem] = []
    for item in items:
        if item.item_id in seen_ids:
            continue
        seen_ids.add(item.item_id)
        deduped.append(item)
    return deduped


def filter_new_items(
    items: Iterable[UpdateItem],
    seen_ids: set[str],
    window_start: datetime | None = None,
) -> list[UpdateItem]:
    filtered: list[UpdateItem] = []
    for item in items:
        if item.item_id in seen_ids:
            continue
        if window_start is not None and item.source in {"changelog", "blog"}:
            try:
                published_dt = iso_to_datetime(item.published_at)
            except ValueError:
                published_dt = None
            if published_dt is not None and published_dt < window_start:
                continue
        filtered.append(item)
    return filtered


def update_seen_ids(existing: list[str], new_ids: Iterable[str], limit: int = MAX_SEEN_IDS) -> list[str]:
    combined = list(existing)
    for item_id in new_ids:
        if item_id not in combined:
            combined.append(item_id)
    if len(combined) > limit:
        combined = combined[-limit:]
    return combined


def select_x_source() -> tuple[str, str]:
    errors: list[str] = []
    for url in X_MIRROR_URLS:
        try:
            return url, fetch_text(url, timeout=40)
        except (HTTPError, URLError, TimeoutError) as exc:
            errors.append(f"{url}: {exc}")
    raise RuntimeError("Unable to fetch Cursor X mirror. " + " | ".join(errors))


def collect_updates(state: dict, now_utc: datetime, x_post_limit: int) -> tuple[dict[str, list[UpdateItem]], list[str]]:
    errors: list[str] = []
    successful_run_at = state.get("last_successful_run_at")
    if successful_run_at:
        window_start = iso_to_datetime(successful_run_at)
    else:
        window_start = now_utc - timedelta(days=BOOTSTRAP_LOOKBACK_DAYS)

    results = {"changelog": [], "blog": [], "x": []}

    try:
        changelog_html = fetch_text(CHANGELOG_URL, timeout=40)
        changelog_items = dedupe_items(parse_changelog_html(changelog_html))
        results["changelog"] = filter_new_items(
            changelog_items,
            set(state["seen"]["changelog"]),
            window_start=window_start,
        )
    except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
        errors.append(f"Changelog fetch failed: {exc}")

    try:
        blog_html = fetch_text(BLOG_URL, timeout=40)
        blog_items = dedupe_items(parse_blog_html(blog_html))
        try:
            sitemap_xml = fetch_text(BLOG_SITEMAP_URL, timeout=40)
            lastmods = parse_blog_sitemap(sitemap_xml)
            blog_items = dedupe_items([
                UpdateItem(
                    source=item.source,
                    item_id=item.item_id,
                    title=item.title,
                    summary=item.summary,
                    url=item.url,
                    published_at=lastmods.get(item.url, item.published_at),
                    source_label=item.source_label,
                )
                for item in blog_items
            ])
        except (HTTPError, URLError, TimeoutError, ElementTree.ParseError):
            pass
        results["blog"] = filter_new_items(
            blog_items,
            set(state["seen"]["blog"]),
            window_start=window_start,
        )
    except (HTTPError, URLError, TimeoutError, RuntimeError) as exc:
        errors.append(f"Blog fetch failed: {exc}")

    try:
        _, x_markdown = select_x_source()
        x_items = parse_x_markdown(x_markdown, fetched_at=now_utc, limit=x_post_limit)
        results["x"] = filter_new_items(
            x_items,
            set(state["seen"]["x"]),
            window_start=None,
        )
    except RuntimeError as exc:
        errors.append(f"X fetch failed: {exc}")

    return results, errors


def render_section(title: str, items: list[UpdateItem]) -> list[str]:
    lines = [f"## {title}"]
    if not items:
        lines.append("- 无新增")
        return lines
    for item in items:
        timestamp = localize_time(item.published_at)
        lines.append(f"- {timestamp} | [{item.title}]({item.url})")
        if item.summary and item.summary != item.title:
            lines.append(f"  - {item.summary}")
    return lines


def render_report(
    updates: dict[str, list[UpdateItem]],
    errors: list[str],
    local_now: datetime,
    force: bool,
    last_successful_run_at: str | None,
) -> str:
    total_updates = sum(len(items) for items in updates.values())
    lines = [
        "# Cursor 更新报告",
        "",
        f"- 生成时间: {local_now.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- 运行模式: {'force' if force else 'scheduled'}",
        "- 调度规则: Asia/Shanghai 每天 09:00 执行一次",
        f"- 最近一次成功计划执行: {last_successful_run_at or '无'}",
        f"- 本次新增条目数: {total_updates}",
        "",
    ]
    if errors:
        lines.extend(["## 抓取告警"] + [f"- {message}" for message in errors] + [""])
    lines.extend(render_section("Changelog", updates["changelog"]))
    lines.append("")
    lines.extend(render_section("Blog", updates["blog"]))
    lines.append("")
    lines.extend(render_section("官方 X 发文", updates["x"]))
    lines.append("")
    if total_updates == 0 and not errors:
        lines.append("今天没有发现新的 Cursor 官方更新。")
    return "\n".join(lines).strip() + "\n"


def persist_report(report_path: Path, report: str) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report, encoding="utf-8")


def run(force: bool, state_path: Path, report_path: Path, x_post_limit: int) -> tuple[int, str]:
    state = load_state(state_path)
    now_utc = datetime.now(timezone.utc)
    decision = should_run_now(state, now_utc=now_utc)
    if not force and not decision.should_run:
        existing_report = report_path.read_text(encoding="utf-8") if report_path.exists() else ""
        lines = [
            f"跳过执行: {decision.reason}",
            f"本地时间: {decision.local_now.strftime('%Y-%m-%d %H:%M %Z')}",
            f"报告路径: {report_path}",
        ]
        if existing_report:
            lines.append("已保留上一份报告。")
        return 0, "\n".join(lines) + "\n"

    updates, errors = collect_updates(state, now_utc=now_utc, x_post_limit=x_post_limit)
    report = render_report(
        updates=updates,
        errors=errors,
        local_now=decision.local_now if not force else now_utc.astimezone(TARGET_TIMEZONE),
        force=force,
        last_successful_run_at=state.get("last_successful_run_at"),
    )
    persist_report(report_path, report)

    successful_sources = sum(1 for source in ("changelog", "blog", "x") if source not in {e.split()[0].lower() for e in errors})
    if successful_sources == 0:
        return 1, report

    if not force:
        local_now = now_utc.astimezone(TARGET_TIMEZONE)
        state["last_successful_local_date"] = local_now.date().isoformat()
        state["last_successful_run_at"] = now_utc.isoformat()
        for source in ("changelog", "blog", "x"):
            state["seen"][source] = update_seen_ids(
                state["seen"][source],
                [item.item_id for item in updates[source]],
            )
        save_state(state_path, state)

    return 0, report


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Fetch and report daily Cursor changelog, blog, and official X updates."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately and generate a report without consuming today's scheduled slot.",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="Directory that stores watcher state and the default report.",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Markdown file written on successful fetches. Defaults to <state-dir>/latest_report.md.",
    )
    parser.add_argument(
        "--x-post-limit",
        type=int,
        default=DEFAULT_X_POST_LIMIT,
        help="Maximum number of recent Cursor X posts to consider per run.",
    )
    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    state_dir = args.state_dir
    state_path = state_dir / "state.json"
    report_path = args.report or (state_dir / "latest_report.md")
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    exit_code, output = run(
        force=args.force,
        state_path=state_path,
        report_path=report_path,
        x_post_limit=max(1, args.x_post_limit),
    )
    sys.stdout.write(output)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
