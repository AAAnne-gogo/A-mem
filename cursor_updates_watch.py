#!/usr/bin/env python3
"""Check official Cursor updates and write a daily report.

This watcher is designed to run from an hourly cron automation, but it only
performs a real fetch once per local day during the 09:00 hour in
Asia/Shanghai. A forced run bypasses the schedule gate and generates a report
without consuming the daily slot.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import re
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as et
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


TIMEZONE = ZoneInfo("Asia/Shanghai")
SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
OFFICIAL_X_ACCOUNT_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = [
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
]
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) CursorUpdatesWatch/1.0 Safari/537.36"
    ),
    "Accept": "text/plain,text/html;q=0.9,*/*;q=0.8",
}
ARTICLE_LIMIT = 5
X_POST_LIMIT = 8
SEEN_LIMIT = 200
RETRY_STATUS_CODES = {403, 429, 500, 502, 503, 504}


@dataclasses.dataclass(frozen=True)
class FeedItem:
    section: str
    title: str
    url: str
    published_at: str | None = None

    @property
    def identifier(self) -> str:
        if self.section in {"blog", "changelog"}:
            return self.url
        return normalize_whitespace(self.title)


@dataclasses.dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    report_path: Path | None = None
    public_report_path: Path | None = None
    history_report_path: Path | None = None
    new_counts: dict[str, int] | None = None


def workspace_root() -> Path:
    return Path(__file__).resolve().parent


def build_paths(root_dir: Path) -> dict[str, Path]:
    runtime_dir = root_dir / ".cursor_updates"
    return {
        "runtime_dir": runtime_dir,
        "state": runtime_dir / "state.json",
        "latest_report": runtime_dir / "latest_report.md",
        "history_dir": runtime_dir / "history",
        "public_report": root_dir / "cursor_updates.md",
    }


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def parse_iso_datetime(value: str | None) -> dt.datetime:
    if not value:
        return dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    normalized = value.replace("Z", "+00:00")
    return dt.datetime.fromisoformat(normalized)


def format_date(value: str | None) -> str:
    if not value:
        return "unknown"
    return parse_iso_datetime(value).date().isoformat()


def strip_cursor_suffix(title: str) -> str:
    return re.sub(r"\s*[|·-]\s*Cursor\s*$", "", title).strip()


def fetch_text(url: str, *, timeout: int = 30, retries: int = 4) -> str:
    request = urllib.request.Request(url, headers=HTTP_HEADERS)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRY_STATUS_CODES or attempt == retries:
                break
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt == retries:
                break
        time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def extract_title(page_text: str) -> str:
    patterns = [
        r'property="og:title"\s+content="([^"]+)"',
        r"<title>(.*?)</title>",
        r'"headline":"([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text, re.IGNORECASE | re.DOTALL)
        if match:
            title = html.unescape(match.group(1))
            return strip_cursor_suffix(normalize_whitespace(title))
    raise ValueError("Could not extract page title")


def extract_published_at(page_text: str) -> str | None:
    patterns = [
        r'"datePublished":"([^"]+)"',
        r'"dateModified":"([^"]+)"',
        r'<time[^>]*datetime="([^"]+)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, page_text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def parse_sitemap_entries(sitemap_text: str, section: str) -> list[tuple[str, str | None]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = et.fromstring(sitemap_text)
    prefix = f"https://cursor.com/{section}/"
    entries: list[tuple[str, str | None]] = []
    for url_node in root.findall("sm:url", namespace):
        loc = url_node.findtext("sm:loc", default="", namespaces=namespace)
        if not loc.startswith(prefix):
            continue
        lastmod = url_node.findtext("sm:lastmod", default=None, namespaces=namespace)
        entries.append((loc, lastmod))
    entries.sort(key=lambda item: parse_iso_datetime(item[1]), reverse=True)
    return entries


def fetch_official_articles(
    *,
    section: str,
    sitemap_text: str,
    fetch_text_fn: Callable[[str], str],
    limit: int = ARTICLE_LIMIT,
) -> list[FeedItem]:
    items: list[FeedItem] = []
    for url, lastmod in parse_sitemap_entries(sitemap_text, section)[:limit]:
        page_text = fetch_text_fn(url)
        items.append(
            FeedItem(
                section=section,
                title=extract_title(page_text),
                url=url,
                published_at=extract_published_at(page_text) or lastmod,
            )
        )
    return items


def parse_x_posts(markdown_text: str, *, limit: int = X_POST_LIMIT) -> list[FeedItem]:
    if "Markdown Content:" in markdown_text:
        markdown_text = markdown_text.split("Markdown Content:", 1)[1]

    lines = [line.strip() for line in markdown_text.splitlines()]
    in_posts = False
    current_lines: list[str] = []
    posts: list[FeedItem] = []
    seen: set[str] = set()

    def flush_current() -> None:
        if not current_lines:
            return
        text = normalize_whitespace(" ".join(current_lines))
        current_lines.clear()
        if not text or text in seen:
            return
        seen.add(text)
        posts.append(
            FeedItem(
                section="x",
                title=text,
                url=OFFICIAL_X_ACCOUNT_URL,
                published_at=None,
            )
        )

    for line in lines:
        if not in_posts:
            if line == "Cursor’s posts":
                in_posts = True
            continue

        if not line or line in {"--------------", "Pinned"}:
            continue

        if line.startswith("[![Image"):
            flush_current()
            continue

        if line.startswith("![Image"):
            continue

        if re.fullmatch(r"\d+:\d{2}", line):
            continue

        current_lines.append(line)

    flush_current()
    return posts[:limit]


def fetch_official_x_posts(fetch_text_fn: Callable[[str], str], *, limit: int = X_POST_LIMIT) -> list[FeedItem]:
    last_error: Exception | None = None
    for url in X_MIRROR_URLS:
        try:
            return parse_x_posts(fetch_text_fn(url), limit=limit)
        except Exception as exc:  # pragma: no cover - covered through fallback behavior
            last_error = exc
    raise RuntimeError(f"Failed to fetch official Cursor X posts: {last_error}")


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {
            "last_successful_run_local_date": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=True), encoding="utf-8")


def merge_seen(existing: list[str], new_values: list[str], *, limit: int = SEEN_LIMIT) -> list[str]:
    merged: list[str] = []
    for value in new_values + existing:
        if value not in merged:
            merged.append(value)
    return merged[:limit]


def should_run(now: dt.datetime, state: dict, *, force: bool = False) -> tuple[bool, str]:
    if force:
        return True, "Forced run requested."

    local_now = now.astimezone(TIMEZONE)
    local_date = local_now.date().isoformat()
    if local_now.hour != 9:
        return False, (
            f"Skip: local time {local_now.strftime('%Y-%m-%d %H:%M %Z')} "
            "is outside the 09:00 hour."
        )
    if state.get("last_successful_run_local_date") == local_date:
        return False, f"Skip: already completed a scheduled run on {local_date}."
    return True, f"Scheduled run allowed for {local_date}."


def collect_updates(fetch_text_fn: Callable[[str], str]) -> dict[str, list[FeedItem]]:
    sitemap_text = fetch_text_fn(SITEMAP_URL)
    return {
        "changelog": fetch_official_articles(
            section="changelog",
            sitemap_text=sitemap_text,
            fetch_text_fn=fetch_text_fn,
        ),
        "blog": fetch_official_articles(
            section="blog",
            sitemap_text=sitemap_text,
            fetch_text_fn=fetch_text_fn,
        ),
        "x": fetch_official_x_posts(fetch_text_fn),
    }


def detect_new_items(state: dict, updates: dict[str, list[FeedItem]]) -> dict[str, list[FeedItem]]:
    seen = state.get("seen", {})
    new_items: dict[str, list[FeedItem]] = {}
    for section, items in updates.items():
        seen_values = set(seen.get(section, []))
        new_items[section] = [item for item in items if item.identifier not in seen_values]
    return new_items


def update_state_for_success(state: dict, updates: dict[str, list[FeedItem]], local_date: str) -> dict:
    next_state = {
        "last_successful_run_local_date": local_date,
        "seen": {},
    }
    previous_seen = state.get("seen", {})
    for section, items in updates.items():
        next_state["seen"][section] = merge_seen(
            previous_seen.get(section, []),
            [item.identifier for item in items],
        )
    return next_state


def render_item(item: FeedItem) -> str:
    if item.section == "x":
        return f"- {item.title}"
    return f"- {format_date(item.published_at)} - [{item.title}]({item.url})"


def render_report(
    *,
    now: dt.datetime,
    updates: dict[str, list[FeedItem]],
    new_items: dict[str, list[FeedItem]],
    force: bool,
) -> str:
    local_now = now.astimezone(TIMEZONE)
    mode = "forced" if force else "scheduled"
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- Generated at: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Mode: {mode}",
        f"- Official X account: {OFFICIAL_X_ACCOUNT_URL}",
        "",
        "## New items since the previous successful scheduled run",
        "",
        f"- Changelog: {len(new_items['changelog'])}",
        f"- Blog: {len(new_items['blog'])}",
        f"- Official X posts: {len(new_items['x'])}",
        "",
    ]

    section_titles = {
        "changelog": "New changelog items",
        "blog": "New blog items",
        "x": "New official X posts",
    }
    for section in ("changelog", "blog", "x"):
        lines.append(f"## {section_titles[section]}")
        lines.append("")
        if new_items[section]:
            lines.extend(render_item(item) for item in new_items[section])
        else:
            lines.append("- No new items detected.")
        lines.append("")

    lines.extend(
        [
            "## Latest tracked snapshot",
            "",
            "### Changelog",
            "",
        ]
    )
    lines.extend(render_item(item) for item in updates["changelog"])
    lines.extend(["", "### Blog", ""])
    lines.extend(render_item(item) for item in updates["blog"])
    lines.extend(["", "### Official X posts", ""])
    lines.extend(render_item(item) for item in updates["x"])
    lines.append("")
    return "\n".join(lines)


def write_reports(
    *,
    root_dir: Path,
    local_date: str,
    report: str,
    persist_history: bool,
) -> tuple[Path, Path, Path | None]:
    paths = build_paths(root_dir)
    paths["runtime_dir"].mkdir(parents=True, exist_ok=True)
    paths["history_dir"].mkdir(parents=True, exist_ok=True)
    paths["latest_report"].write_text(report, encoding="utf-8")
    paths["public_report"].write_text(report, encoding="utf-8")

    history_path: Path | None = None
    if persist_history:
        history_path = paths["history_dir"] / f"{local_date}.md"
        history_path.write_text(report, encoding="utf-8")

    return paths["latest_report"], paths["public_report"], history_path


def run_watch(
    *,
    force: bool = False,
    now: dt.datetime | None = None,
    root_dir: Path | None = None,
    fetch_text_fn: Callable[[str], str] = fetch_text,
) -> RunResult:
    root_dir = root_dir or workspace_root()
    paths = build_paths(root_dir)
    now = now or dt.datetime.now(dt.timezone.utc)
    state = load_state(paths["state"])
    allowed, reason = should_run(now, state, force=force)
    if not allowed:
        return RunResult(status="skipped", message=reason)

    updates = collect_updates(fetch_text_fn)
    new_items = detect_new_items(state, updates)
    local_date = now.astimezone(TIMEZONE).date().isoformat()
    report = render_report(now=now, updates=updates, new_items=new_items, force=force)
    latest_report_path, public_report_path, history_report_path = write_reports(
        root_dir=root_dir,
        local_date=local_date,
        report=report,
        persist_history=not force,
    )

    if not force:
        save_state(paths["state"], update_state_for_success(state, updates, local_date))

    return RunResult(
        status="ran",
        message=reason,
        report_path=latest_report_path,
        public_report_path=public_report_path,
        history_report_path=history_report_path,
        new_counts={section: len(items) for section, items in new_items.items()},
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the schedule gate and generate a report without updating state.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run_watch(force=args.force)
    print(result.message)
    if result.status == "ran":
        print(f"Latest report: {result.report_path}")
        print(f"Public report: {result.public_report_path}")
        if result.history_report_path:
            print(f"History report: {result.history_report_path}")
        print(f"New counts: {json.dumps(result.new_counts, ensure_ascii=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
