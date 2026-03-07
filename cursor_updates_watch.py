#!/usr/bin/env python3
"""Watch official Cursor updates from changelog, blog, and X.

This script is designed for hourly automation triggers. It only performs the
scheduled fetch during the 09:00 hour in Asia/Shanghai unless ``--force`` is
used. Results are written to:

- .cursor_updates/latest_report.md
- .cursor_updates/history/YYYY-MM-DD.md (scheduled successes only)
- cursor_updates.md
- .cursor_updates/state.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

MARKETING_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_URL = "https://cursor.com/en/blog"
X_ACCOUNT_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)

LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEDULE_HOUR = 9
DEFAULT_LIMIT = 5
FETCH_LIMIT = 8
MAX_SEEN_IDS = 400
USER_AGENT = "Mozilla/5.0 (compatible; CursorUpdatesWatch/1.0)"

STATE_DIR = Path(".cursor_updates")
STATE_PATH = STATE_DIR / "state.json"
LEGACY_STATE_PATH = STATE_DIR / "seen_state.json"
LATEST_REPORT_PATH = STATE_DIR / "latest_report.md"
HISTORY_DIR = STATE_DIR / "history"
ROOT_REPORT_PATH = Path("cursor_updates.md")


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str
    published: str
    summary: str

    @property
    def identifier(self) -> str:
        key = self.url or self.title
        return f"{self.source}:{key}"


class ScheduledSkip(RuntimeError):
    """Raised when the scheduled run should not execute."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _normalize_date(value: str | None) -> str:
    if not value:
        return ""
    value = value.strip()
    if not value:
        return ""
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return dt.datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        match = re.search(r"(\d{4}-\d{2}-\d{2})", value)
        return match.group(1) if match else value


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_html(text: str) -> str:
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return _collapse_whitespace(html.unescape(no_tags))


def _truncate(text: str, limit: int = 180) -> str:
    text = _collapse_whitespace(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _request(url: str) -> urllib.request.Request:
    return urllib.request.Request(url, headers={"User-Agent": USER_AGENT})


def fetch_text(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    backoff_seconds: float = 1.5,
    opener: Callable[..., object] = urllib.request.urlopen,
) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with opener(_request(url), timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            last_error = error
            transient = error.code in {403, 429, 500, 502, 503, 504}
            if attempt < retries - 1 and transient:
                time.sleep(backoff_seconds * (2**attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            last_error = error
            if attempt < retries - 1:
                time.sleep(backoff_seconds * (2**attempt))
                continue
            raise
    assert last_error is not None
    raise last_error


def parse_sitemap(xml_text: str) -> list[tuple[str, str]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[tuple[str, str]] = []
    for url_node in root.findall("sm:url", namespace):
        loc = url_node.findtext("sm:loc", default="", namespaces=namespace).strip()
        lastmod = url_node.findtext("sm:lastmod", default="", namespaces=namespace).strip()
        if not loc:
            continue
        entries.append((loc, _normalize_date(lastmod)))
    return entries


def parse_blog_article(html_text: str, url: str, fallback_date: str = "") -> UpdateItem:
    blocks = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    for block in blocks:
        try:
            payload = json.loads(block)
        except json.JSONDecodeError:
            continue
        candidates: Iterable[object]
        if isinstance(payload, list):
            candidates = payload
        else:
            candidates = (payload,)
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "BlogPosting":
                continue
            title = _collapse_whitespace(str(item.get("headline", "")))
            summary = _collapse_whitespace(str(item.get("description", "")))
            published = _normalize_date(str(item.get("datePublished", ""))) or fallback_date
            return UpdateItem(
                source="blog",
                title=title or url.rsplit("/", 1)[-1],
                url=url,
                published=published,
                summary=summary,
            )

    title_match = re.search(r"<title>(.*?)</title>", html_text, flags=re.DOTALL)
    description_match = re.search(
        r'<meta name="description" content="(.*?)"',
        html_text,
        flags=re.DOTALL,
    )
    title = _strip_html(title_match.group(1)) if title_match else url.rsplit("/", 1)[-1]
    title = re.sub(r"\s*·\s*Cursor$", "", title).strip()
    summary = _strip_html(description_match.group(1)) if description_match else ""
    return UpdateItem(
        source="blog",
        title=title,
        url=url,
        published=fallback_date,
        summary=summary,
    )


def parse_changelog(html_text: str, limit: int = DEFAULT_LIMIT) -> list[UpdateItem]:
    pattern = re.compile(
        r'<article>.*?<time dateTime="(?P<date>[^"]+)".*?</time>.*?'
        r'<h1[^>]*>.*?<a [^>]*href="(?P<href>/changelog/[^"]+)">(?P<title>.*?)</a>.*?</h1>.*?'
        r'<div class="prose prose--block"><p>(?P<summary>.*?)</p>',
        flags=re.DOTALL,
    )
    items: list[UpdateItem] = []
    seen_urls: set[str] = set()
    for match in pattern.finditer(html_text):
        url = "https://cursor.com" + match.group("href")
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            UpdateItem(
                source="changelog",
                title=_strip_html(match.group("title")),
                url=url,
                published=_normalize_date(match.group("date")),
                summary=_strip_html(match.group("summary")),
            )
        )
        if len(items) >= limit:
            break
    return items


def _is_x_marker(line: str) -> bool:
    return line.startswith("[![Image") and "x.com/cursor_ai" in line


def _clean_x_block(lines: list[str]) -> str:
    ignored_literals = {
        "Pinned",
        "Cursor",
        "@cursor_ai",
        "Cursor’s posts",
        "Cursor's posts",
    }
    clean_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if (
            not line
            or line in ignored_literals
            or re.fullmatch(r"-{3,}", line)
            or line.startswith("Title:")
            or line.startswith("URL Source:")
            or line.startswith("Published Time:")
            or line.startswith("Markdown Content:")
            or line.startswith("![]")
            or line.startswith("![")
            or line.startswith("[![")
            or re.fullmatch(r"\d+:\d+", line)
        ):
            continue
        clean_lines.append(line)
    return _collapse_whitespace(" ".join(clean_lines))


def parse_x_posts(markdown_text: str, limit: int = DEFAULT_LIMIT) -> list[UpdateItem]:
    lines = [line.rstrip() for line in markdown_text.splitlines()]
    posts: list[str] = []
    block: list[str] = []
    in_posts_section = False

    for raw_line in lines:
        line = raw_line.strip()
        if not in_posts_section:
            if line in {"Cursor’s posts", "Cursor's posts"}:
                in_posts_section = True
            continue

        if _is_x_marker(line):
            text = _clean_x_block(block)
            if text:
                posts.append(text)
            block = []
            continue
        block.append(line)

    final_text = _clean_x_block(block)
    if final_text:
        posts.append(final_text)

    deduped: list[str] = []
    seen: set[str] = set()
    for post in posts:
        if post in seen:
            continue
        seen.add(post)
        deduped.append(post)
        if len(deduped) >= limit:
            break

    return [
        UpdateItem(
            source="x",
            title=_truncate(post, 100),
            url=X_ACCOUNT_URL,
            published="",
            summary=post,
        )
        for post in deduped
    ]


def fetch_blog_updates(limit: int = DEFAULT_LIMIT) -> list[UpdateItem]:
    sitemap_text = fetch_text(MARKETING_SITEMAP_URL)
    entries = parse_sitemap(sitemap_text)
    blog_entries = [
        (url, lastmod)
        for url, lastmod in entries
        if re.match(r"^https://cursor\.com/blog/[^/]+$", url)
    ]
    blog_entries.sort(key=lambda item: (item[1], item[0]), reverse=True)

    items: list[UpdateItem] = []
    for url, lastmod in blog_entries[: max(limit, FETCH_LIMIT)]:
        article_html = fetch_text(url)
        items.append(parse_blog_article(article_html, url, fallback_date=lastmod))

    items.sort(key=lambda item: (item.published, item.title), reverse=True)
    return items[:limit]


def fetch_changelog_updates(limit: int = DEFAULT_LIMIT) -> list[UpdateItem]:
    changelog_html = fetch_text(CHANGELOG_URL)
    return parse_changelog(changelog_html, limit=limit)


def fetch_x_updates(limit: int = DEFAULT_LIMIT) -> tuple[list[UpdateItem], str]:
    last_error: Exception | None = None
    for url in X_MIRROR_URLS:
        try:
            text = fetch_text(url)
            posts = parse_x_posts(text, limit=limit)
            if posts:
                return posts, url
        except Exception as error:  # pragma: no cover - exercised via integration run
            last_error = error
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("No X mirror returned any posts.")


def load_state() -> dict[str, object]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    if LEGACY_STATE_PATH.exists():
        legacy_state = json.loads(LEGACY_STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(legacy_state, dict):
            return legacy_state
    return {
        "seen_ids": [],
        "last_successful_scheduled_local_date": "",
        "last_run_utc": "",
    }


def save_state(state: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def ensure_scheduled_window(
    state: dict[str, object],
    *,
    now: dt.datetime | None = None,
    force: bool = False,
) -> dt.datetime:
    now_utc = now or _utc_now()
    local_now = now_utc.astimezone(LOCAL_TIMEZONE)
    if force:
        return local_now
    if local_now.hour != SCHEDULE_HOUR:
        raise ScheduledSkip(
            f"Skipping scheduled fetch because local time is "
            f"{local_now.strftime('%H:%M')} Asia/Shanghai; "
            f"the watcher only runs during the {SCHEDULE_HOUR:02d}:00 hour."
        )
    scheduled_date = local_now.date().isoformat()
    if state.get("last_successful_scheduled_local_date") == scheduled_date:
        raise ScheduledSkip(
            f"Skipping scheduled fetch because a successful run already happened "
            f"on {scheduled_date} Asia/Shanghai."
        )
    return local_now


def collect_updates(limit: int = DEFAULT_LIMIT) -> tuple[dict[str, list[UpdateItem]], str]:
    updates = {
        "changelog": fetch_changelog_updates(limit=limit),
        "blog": fetch_blog_updates(limit=limit),
    }
    x_items, x_source_url = fetch_x_updates(limit=limit)
    updates["x"] = x_items
    return updates, x_source_url


def compute_new_items(
    updates: dict[str, list[UpdateItem]],
    seen_ids: Iterable[str],
) -> dict[str, list[UpdateItem]]:
    seen = set(seen_ids)
    result: dict[str, list[UpdateItem]] = {}
    for source, items in updates.items():
        result[source] = [item for item in items if item.identifier not in seen]
    return result


def summarize_counts(groups: dict[str, list[UpdateItem]]) -> str:
    return ", ".join(f"{source}={len(items)}" for source, items in groups.items())


def render_report(
    *,
    generated_at_utc: dt.datetime,
    local_now: dt.datetime,
    force: bool,
    updates: dict[str, list[UpdateItem]],
    new_items: dict[str, list[UpdateItem]],
    x_source_url: str,
) -> str:
    lines = [
        "# Cursor 更新追踪报告",
        "",
        f"- 生成时间（UTC）: {generated_at_utc.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 本地时间（Asia/Shanghai）: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 运行模式: {'强制检查' if force else '计划检查'}",
        f"- 官方来源: changelog={CHANGELOG_URL} | blog={BLOG_URL} | X={x_source_url}",
        "",
        "## 本次新增",
        "",
    ]

    total_new = sum(len(items) for items in new_items.values())
    if total_new == 0:
        lines.extend(
            [
                "本次相对于已记录状态未发现新增更新。",
                "",
            ]
        )
    else:
        for source in ("changelog", "blog", "x"):
            items = new_items.get(source, [])
            lines.append(f"### {source}")
            if not items:
                lines.append("- 无新增")
                lines.append("")
                continue
            for item in items:
                prefix = f"{item.published} " if item.published else ""
                lines.append(f"- {prefix}[{item.title}]({item.url})")
                if item.summary:
                    lines.append(f"  - {item.summary}")
            lines.append("")

    lines.extend(
        [
            "## 最近抓取快照",
            "",
        ]
    )
    for source in ("changelog", "blog", "x"):
        items = updates.get(source, [])
        lines.append(f"### {source}")
        for item in items:
            prefix = f"{item.published} " if item.published else ""
            lines.append(f"- {prefix}[{item.title}]({item.url})")
            if item.summary:
                lines.append(f"  - {item.summary}")
        if not items:
            lines.append("- 抓取失败或无结果")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def persist_report(report_text: str, *, local_now: dt.datetime, scheduled_success: bool) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT_PATH.write_text(report_text, encoding="utf-8")
    ROOT_REPORT_PATH.write_text(report_text, encoding="utf-8")
    if scheduled_success:
        HISTORY_DIR.mkdir(parents=True, exist_ok=True)
        history_path = HISTORY_DIR / f"{local_now.date().isoformat()}.md"
        history_path.write_text(report_text, encoding="utf-8")


def run(*, force: bool = False, limit: int = DEFAULT_LIMIT) -> str:
    state = load_state()
    local_now = ensure_scheduled_window(state, force=force)
    generated_at_utc = _utc_now()
    updates, x_source_url = collect_updates(limit=limit)
    seen_ids = state.get("seen_ids", [])
    if not isinstance(seen_ids, list):
        seen_ids = []
    new_items = compute_new_items(updates, seen_ids)

    report_text = render_report(
        generated_at_utc=generated_at_utc,
        local_now=local_now,
        force=force,
        updates=updates,
        new_items=new_items,
        x_source_url=x_source_url,
    )
    persist_report(report_text, local_now=local_now, scheduled_success=not force)

    if not force:
        updated_seen_ids = list(seen_ids)
        seen_set = set(updated_seen_ids)
        for items in updates.values():
            for item in items:
                if item.identifier not in seen_set:
                    updated_seen_ids.append(item.identifier)
                    seen_set.add(item.identifier)

        state["seen_ids"] = updated_seen_ids[-MAX_SEEN_IDS:]
        state["last_successful_scheduled_local_date"] = local_now.date().isoformat()
        state["last_run_utc"] = generated_at_utc.isoformat()
        state["last_snapshot"] = {
            source: [asdict(item) for item in items]
            for source, items in updates.items()
        }
        save_state(state)

    return report_text


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore the 09:00 Asia/Shanghai schedule gate and run immediately.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Number of items to keep per source in the report.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        report = run(force=args.force, limit=max(1, args.limit))
    except ScheduledSkip as error:
        print(str(error))
        return 0
    except Exception as error:
        print(f"Cursor updates watcher failed: {error}")
        return 1

    first_lines = report.splitlines()[:12]
    print("\n".join(first_lines))
    print("")
    print(f"Saved report to {LATEST_REPORT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
