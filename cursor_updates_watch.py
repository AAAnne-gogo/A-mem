#!/usr/bin/env python3
"""Daily Cursor update watcher.

Fetches the latest items from the public Cursor changelog, Cursor blog, and
the official Cursor X timeline. It is designed to run on an hourly cron, but
only performs a full check once per day after 09:00 Asia/Shanghai unless
forced.
"""

from __future__ import annotations

import argparse
import dataclasses
import html
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_HOUR = 9
DEFAULT_MAX_ITEMS = 5
DEFAULT_STATE_DIR = Path(".cursor_updates")
DEFAULT_STATE_PATH = DEFAULT_STATE_DIR / "state.json"
DEFAULT_REPORT_PATH = Path("cursor_updates.md")

CHANGELOG_URL = "https://www.cursor.com/changelog"
BLOG_URL = "https://www.cursor.com/blog"
X_TIMELINE_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/"
    "screen-name/cursor_ai?lang=en"
)

SOURCE_LABELS = {
    "changelog": "Cursor changelog",
    "blog": "Cursor blog",
    "x": "Cursor official X",
}

SOURCE_URLS = {
    "changelog": CHANGELOG_URL,
    "blog": BLOG_URL,
    "x": "https://x.com/cursor_ai",
}

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

CHANGELOG_ENTRY_RE = re.compile(
    r'href="(?P<href>/changelog/[^"]+)">\s*'
    r'<time dateTime="(?P<published_at>[^"]+)"[^>]*>[^<]+</time>\s*</a>.*?'
    r"<h1[^>]*>\s*<a[^>]*href=\"(?P=href)\">(?P<title>.*?)</a>\s*</h1>.*?"
    r'<div class="prose prose--block">(?P<body>.*?)</div>',
    re.DOTALL,
)

BLOG_ENTRY_RE = re.compile(
    r'<a class="card card--text[^"]*" href="(?P<href>/blog/(?!topic/)[^"]+)">.*?'
    r'<p class="type-base text-theme-text text-pretty">(?P<title>.*?)</p>\s*'
    r'<p class="type-base text-theme-text-sec text-pretty">(?P<summary>.*?)</p>.*?'
    r'<span class="capitalize">(?P<topic>.*?)</span>\s*'
    r'<time dateTime="(?P<published_at>[^"]+)"[^>]*>[^<]+</time>',
    re.DOTALL,
)

NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)

TCO_URL_RE = re.compile(r"https://t\.co/\w+")
TAG_RE = re.compile(r"<[^>]+>")
WHITESPACE_RE = re.compile(r"\s+")


class CursorUpdatesError(RuntimeError):
    """Raised when a source cannot be fetched or parsed."""


@dataclass(frozen=True)
class UpdateItem:
    source: str
    item_id: str
    title: str
    summary: str
    published_at: str
    url: str


@dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    report_path: str | None = None
    new_counts: dict[str, int] | None = None


def normalize_text(value: str) -> str:
    no_comments = value.replace("<!-- -->", " ")
    no_tags = TAG_RE.sub(" ", no_comments)
    unescaped = html.unescape(no_tags).replace("\xa0", " ")
    return WHITESPACE_RE.sub(" ", unescaped).strip()


def compact_post_text(value: str, limit: int = 90) -> str:
    cleaned = WHITESPACE_RE.sub(" ", TCO_URL_RE.sub("", value)).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "..."


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"})
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "ignore")
    except (HTTPError, URLError) as exc:
        raise CursorUpdatesError(f"Failed to fetch {url}: {exc}") from exc


def parse_changelog(html_text: str, max_items: int = DEFAULT_MAX_ITEMS) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    for match in CHANGELOG_ENTRY_RE.finditer(html_text):
        body = match.group("body")
        summary_match = re.search(r"<p>(.*?)</p>", body, re.DOTALL)
        summary = normalize_text(summary_match.group(1) if summary_match else "")
        href = match.group("href")
        item_id = href.rsplit("/", 1)[-1]
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append(
            UpdateItem(
                source="changelog",
                item_id=item_id,
                title=normalize_text(match.group("title")),
                summary=summary,
                published_at=match.group("published_at"),
                url=f"https://cursor.com{href}",
            )
        )
        if len(items) >= max_items:
            break
    if not items:
        raise CursorUpdatesError("Unable to parse any changelog entries.")
    return items


def parse_blog(html_text: str, max_items: int = DEFAULT_MAX_ITEMS) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    for match in BLOG_ENTRY_RE.finditer(html_text):
        href = match.group("href")
        item_id = href.rsplit("/", 1)[-1]
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        topic = normalize_text(match.group("topic")).replace("·", "").strip()
        summary = normalize_text(match.group("summary"))
        if topic:
            summary = f"[{topic}] {summary}"
        items.append(
            UpdateItem(
                source="blog",
                item_id=item_id,
                title=normalize_text(match.group("title")),
                summary=summary,
                published_at=match.group("published_at"),
                url=f"https://cursor.com{href}",
            )
        )
        if len(items) >= max_items:
            break
    if not items:
        raise CursorUpdatesError("Unable to parse any blog entries.")
    return items


def parse_x_timeline(html_text: str, max_items: int = DEFAULT_MAX_ITEMS) -> list[UpdateItem]:
    match = NEXT_DATA_RE.search(html_text)
    if not match:
        raise CursorUpdatesError("Unable to locate X timeline data.")

    try:
        data = json.loads(match.group(1))
        entries = data["props"]["pageProps"]["timeline"]["entries"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise CursorUpdatesError("Unable to decode X timeline JSON.") from exc

    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.get("type") != "tweet":
            continue
        tweet = entry.get("content", {}).get("tweet", {})
        item_id = tweet.get("id_str")
        if not item_id or item_id in seen_ids:
            continue
        seen_ids.add(item_id)

        # Limit the feed to original top-level posts. Retweets and replies make
        # the daily summary noisy and are not usually release updates.
        if tweet.get("retweeted_status") or tweet.get("in_reply_to_status_id_str"):
            continue

        text = normalize_text(tweet.get("full_text") or tweet.get("text") or "")
        if not text:
            continue
        permalink = tweet.get("permalink") or f"/cursor_ai/status/{item_id}"
        items.append(
            UpdateItem(
                source="x",
                item_id=item_id,
                title=compact_post_text(text),
                summary=text,
                published_at=tweet.get("created_at", ""),
                url=f"https://x.com{permalink}",
            )
        )
        if len(items) >= max_items:
            break

    if not items:
        raise CursorUpdatesError("Unable to parse any official X posts.")
    return items


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"last_run_local_date": None, "seen_ids": {}}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def should_run(
    *,
    now: datetime,
    timezone_name: str,
    run_hour: int,
    state: dict,
    force: bool = False,
) -> tuple[bool, str, datetime]:
    local_now = now.astimezone(ZoneInfo(timezone_name))
    local_date = local_now.date().isoformat()
    last_run_local_date = state.get("last_run_local_date")

    if force:
        return True, "Forced run requested.", local_now
    if local_now.hour < run_hour:
        return False, f"Local time {local_now:%H:%M} is before {run_hour:02d}:00.", local_now
    if last_run_local_date == local_date:
        return False, f"Already checked on {local_date}.", local_now
    return True, "Daily run window is open.", local_now


def compute_new_items(items_by_source: dict[str, list[UpdateItem]], state: dict) -> dict[str, list[UpdateItem]]:
    seen_ids = {
        source: set(values)
        for source, values in (state.get("seen_ids") or {}).items()
    }
    new_items: dict[str, list[UpdateItem]] = {}
    for source, items in items_by_source.items():
        source_seen = seen_ids.get(source, set())
        new_items[source] = [item for item in items if item.item_id not in source_seen]
    return new_items


def update_state_for_run(
    *,
    state: dict,
    items_by_source: dict[str, list[UpdateItem]],
    local_now: datetime,
) -> dict:
    next_seen_ids = {
        source: [item.item_id for item in items]
        for source, items in items_by_source.items()
    }
    return {
        "last_run_local_date": local_now.date().isoformat(),
        "last_checked_at": local_now.isoformat(),
        "seen_ids": next_seen_ids,
    }


def format_item(item: UpdateItem) -> str:
    summary = f" - {item.summary}" if item.summary else ""
    return f"- [{item.title}]({item.url}) ({item.published_at}){summary}"


def render_report(
    *,
    local_now: datetime,
    items_by_source: dict[str, list[UpdateItem]],
    new_items_by_source: dict[str, list[UpdateItem]],
    first_run: bool,
) -> str:
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- Last checked (Asia/Shanghai): {local_now:%Y-%m-%d %H:%M:%S %Z}",
        "- Sources:",
        f"  - Changelog: {SOURCE_URLS['changelog']}",
        f"  - Blog: {SOURCE_URLS['blog']}",
        f"  - Official X: {SOURCE_URLS['x']}",
        "",
        "## New since previous check",
        "",
    ]

    if first_run:
        lines.append("- Initial baseline created. Current top items are listed below.")
    else:
        total_new = sum(len(items) for items in new_items_by_source.values())
        if total_new == 0:
            lines.append("- No new items were detected across changelog, blog, or official X.")
        else:
            for source in ("changelog", "blog", "x"):
                source_new_items = new_items_by_source[source]
                lines.append(f"### {SOURCE_LABELS[source]}")
                if source_new_items:
                    lines.extend(format_item(item) for item in source_new_items)
                else:
                    lines.append("- No new items.")
                lines.append("")

    if first_run:
        lines.append("")

    for source in ("changelog", "blog", "x"):
        lines.extend(
            [
                f"## Latest {SOURCE_LABELS[source]} items",
                "",
            ]
        )
        lines.extend(format_item(item) for item in items_by_source[source])
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def write_report(report_path: Path, report_text: str) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(report_text, encoding="utf-8")


def run_watch(
    *,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    run_hour: int = DEFAULT_RUN_HOUR,
    force: bool = False,
    state_path: Path = DEFAULT_STATE_PATH,
    report_path: Path = DEFAULT_REPORT_PATH,
    max_items: int = DEFAULT_MAX_ITEMS,
    fetcher: Callable[[str], str] = fetch_text,
) -> RunResult:
    current_time = now or datetime.now(tz=ZoneInfo("UTC"))
    state = load_state(state_path)
    should_execute, reason, local_now = should_run(
        now=current_time,
        timezone_name=timezone_name,
        run_hour=run_hour,
        state=state,
        force=force,
    )
    if not should_execute:
        return RunResult(status="skipped", message=reason)

    items_by_source = {
        "changelog": parse_changelog(fetcher(CHANGELOG_URL), max_items=max_items),
        "blog": parse_blog(fetcher(BLOG_URL), max_items=max_items),
        "x": parse_x_timeline(fetcher(X_TIMELINE_URL), max_items=max_items),
    }
    first_run = not any((state.get("seen_ids") or {}).values())
    new_items_by_source = compute_new_items(items_by_source, state)
    report_text = render_report(
        local_now=local_now,
        items_by_source=items_by_source,
        new_items_by_source=new_items_by_source,
        first_run=first_run,
    )
    write_report(report_path, report_text)
    save_state(
        state_path,
        update_state_for_run(state=state, items_by_source=items_by_source, local_now=local_now),
    )

    new_counts = {
        source: len(items)
        for source, items in new_items_by_source.items()
    }
    return RunResult(
        status="updated",
        message="Cursor updates report refreshed.",
        report_path=str(report_path),
        new_counts=new_counts,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X posts.")
    parser.add_argument("--force", action="store_true", help="Ignore the 9am gate and last-run guard.")
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone used for the daily run gate (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--run-hour",
        type=int,
        default=DEFAULT_RUN_HOUR,
        help=f"Local hour after which one run per day is allowed (default: {DEFAULT_RUN_HOUR}).",
    )
    parser.add_argument(
        "--state-dir",
        default=str(DEFAULT_STATE_DIR),
        help=f"Directory used for runtime state (default: {DEFAULT_STATE_DIR}).",
    )
    parser.add_argument(
        "--report-path",
        default=str(DEFAULT_REPORT_PATH),
        help=f"Where to write the markdown report (default: {DEFAULT_REPORT_PATH}).",
    )
    parser.add_argument(
        "--max-items",
        type=int,
        default=DEFAULT_MAX_ITEMS,
        help=f"Maximum number of latest items to retain per source (default: {DEFAULT_MAX_ITEMS}).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    report_path = Path(args.report_path)

    try:
        result = run_watch(
            timezone_name=args.timezone,
            run_hour=args.run_hour,
            force=args.force,
            state_path=state_path,
            report_path=report_path,
            max_items=args.max_items,
        )
    except CursorUpdatesError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    payload = dataclasses.asdict(result)
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
