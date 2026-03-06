from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
OFFICIAL_X_PROFILE_URL = "https://x.com/cursor_ai"
OFFICIAL_X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TARGET_HOUR = 9
DEFAULT_REPORT_LIMIT = 5
DEFAULT_FEED_SCAN_LIMIT = 10
DEFAULT_X_LIMIT = 8
DEFAULT_TIMEOUT = 30
DEFAULT_MAX_ATTEMPTS = 4
USER_AGENT = "Mozilla/5.0 (compatible; CursorUpdatesWatch/1.0)"
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
ROOT_DIR = Path(__file__).resolve().parent
STATE_DIR = ROOT_DIR / ".cursor_updates"
STATE_PATH = STATE_DIR / "state.json"
LATEST_REPORT_PATH = STATE_DIR / "latest_report.md"
HISTORY_DIR = STATE_DIR / "history"


@dataclass(frozen=True)
class FeedItem:
    title: str
    url: str
    published_at: str


@dataclass(frozen=True)
class XSnapshot:
    account_name: str
    handle: str
    profile_url: str
    mirror_url: str
    mirror_published_time: str
    posts: list[str]


@dataclass(frozen=True)
class RunResult:
    status: str
    reason: str
    report: str
    changelog_items: list[FeedItem]
    blog_items: list[FeedItem]
    x_snapshot: XSnapshot | None


def ensure_aware_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def load_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def fetch_text(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    sleep_func: Callable[[float], None] = time.sleep,
) -> str:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9,*/*;q=0.8",
    }

    for attempt in range(1, max_attempts + 1):
        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, errors="replace")
        except HTTPError as exc:
            should_retry = exc.code in TRANSIENT_STATUS_CODES and attempt < max_attempts
            if not should_retry:
                raise
        except (URLError, TimeoutError) as exc:
            if attempt >= max_attempts:
                raise exc

        sleep_func(2 ** (attempt - 1))

    raise RuntimeError(f"Failed to fetch {url}")


def parse_sitemap_entries(xml_text: str) -> list[dict[str, str]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[dict[str, str]] = []
    for url_node in root.findall("sm:url", namespace):
        loc = (url_node.findtext("sm:loc", default="", namespaces=namespace) or "").strip()
        lastmod = (url_node.findtext("sm:lastmod", default="", namespaces=namespace) or "").strip()
        if not loc:
            continue
        entries.append({"loc": loc, "lastmod": lastmod})
    return entries


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def canonical_date(value: str) -> str:
    return parse_iso_datetime(value).date().isoformat()


def is_blog_post(url: str) -> bool:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    return len(path_parts) == 2 and path_parts[0] == "blog" and path_parts[1] != "topic"


def is_changelog_post(url: str) -> bool:
    path_parts = [part for part in urlparse(url).path.split("/") if part]
    return (
        len(path_parts) == 2
        and path_parts[0] == "changelog"
        and not path_parts[1].isdigit()
    )


def clean_title(raw_title: str) -> str:
    title = html.unescape(raw_title.replace("\\/", "/").replace('\\"', '"')).strip()
    title = re.sub(r"\s+[|·]\s+Cursor.*$", "", title)
    return title.strip()


def extract_page_title(page_html: str) -> str:
    patterns = [
        r'"headline":"(.*?)"',
        r'<meta\s+property="og:title"\s+content="(.*?)"',
        r"<title>(.*?)</title>",
    ]
    for pattern in patterns:
        match = re.search(pattern, page_html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return clean_title(match.group(1))
    raise ValueError("Unable to extract page title")


def build_feed_items(
    entries: list[dict[str, str]],
    *,
    entry_filter: Callable[[str], bool],
    limit: int,
    fetcher: Callable[[str], str],
) -> list[FeedItem]:
    filtered = [entry for entry in entries if entry_filter(entry["loc"])]
    filtered.sort(key=lambda entry: parse_iso_datetime(entry["lastmod"]), reverse=True)

    items: list[FeedItem] = []
    for entry in filtered[:limit]:
        page_html = fetcher(entry["loc"])
        items.append(
            FeedItem(
                title=extract_page_title(page_html),
                url=entry["loc"],
                published_at=canonical_date(entry["lastmod"]),
            )
        )
    return items


def clean_x_segment(segment: str) -> str:
    cleaned_lines: list[str] = []
    for raw_line in segment.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line == "Pinned":
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        if line.startswith("![Image") or line.startswith("[![Image"):
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue
        cleaned_lines.append(line)
    cleaned = " ".join(cleaned_lines).strip()
    if cleaned == "See everything new in Cursor:":
        return ""
    return cleaned


def parse_x_snapshot(markdown_text: str, *, limit: int = DEFAULT_X_LIMIT) -> XSnapshot:
    title_match = re.search(r"^Title:\s*(.+)$", markdown_text, flags=re.MULTILINE)
    published_match = re.search(r"^Published Time:\s*(.+)$", markdown_text, flags=re.MULTILINE)
    account_match = re.search(r"^([^\n]+)\s+\(@([A-Za-z0-9_]+)\)\s*/\s*X$", markdown_text, flags=re.MULTILINE)

    account_name = "Cursor"
    handle = "@cursor_ai"
    if account_match:
        account_name = account_match.group(1).replace("Title:", "").strip()
        handle = f"@{account_match.group(2).strip()}"
    elif title_match:
        account_name = title_match.group(1).split(" (@", 1)[0].strip()

    segments = re.split(
        r"\n\[\!\[Image \d+: Square profile picture[^\n]*\]\(https://x\.com/cursor_ai\)\n",
        markdown_text,
    )

    posts: list[str] = []
    for segment in segments[1:]:
        cleaned = clean_x_segment(segment)
        if not cleaned:
            continue
        if cleaned in posts:
            continue
        posts.append(cleaned)
        if len(posts) >= limit:
            break

    return XSnapshot(
        account_name=account_name,
        handle=handle,
        profile_url=OFFICIAL_X_PROFILE_URL,
        mirror_url=OFFICIAL_X_MIRROR_URL,
        mirror_published_time=published_match.group(1).strip() if published_match else "",
        posts=posts,
    )


def dedupe_latest(existing: list[str], new_items: list[str], *, max_items: int = 200) -> list[str]:
    merged: list[str] = []
    for value in [*new_items, *existing]:
        if value not in merged:
            merged.append(value)
        if len(merged) >= max_items:
            break
    return merged


def hash_post(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def should_execute(
    *,
    now_local: datetime,
    force: bool,
    state: dict[str, Any],
    timezone_name: str,
    target_hour: int,
) -> tuple[bool, str]:
    if force:
        return True, "forced run"

    if now_local.hour != target_hour:
        return (
            False,
            f"waiting for {target_hour:02d}:00 in {timezone_name}; current local time is {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        )

    if state.get("last_scheduled_success_date") == now_local.date().isoformat():
        return (
            False,
            f"scheduled run already completed for {now_local.date().isoformat()} in {timezone_name}",
        )

    return True, "scheduled run due"


def format_feed_items(items: list[FeedItem]) -> list[str]:
    if not items:
        return ["- None"]
    return [f"- {item.published_at} | {item.title} | {item.url}" for item in items]


def format_x_posts(posts: list[str]) -> list[str]:
    if not posts:
        return ["- None"]
    return [f"- {post}" for post in posts]


def render_report(
    *,
    status: str,
    reason: str,
    now_utc: datetime,
    now_local: datetime,
    timezone_name: str,
    force: bool,
    changelog_items: list[FeedItem],
    blog_items: list[FeedItem],
    x_snapshot: XSnapshot | None,
    new_changelog_items: list[FeedItem] | None = None,
    new_blog_items: list[FeedItem] | None = None,
    new_x_posts: list[str] | None = None,
) -> str:
    lines = [
        "# Cursor updates watch",
        "",
        f"- Status: {status}",
        f"- Reason: {reason}",
        f"- Mode: {'forced' if force else 'scheduled'}",
        f"- Run time (UTC): {now_utc.isoformat()}",
        f"- Run time ({timezone_name}): {now_local.isoformat()}",
    ]

    if x_snapshot is not None:
        lines.extend(
            [
                f"- Official X account: {x_snapshot.account_name} ({x_snapshot.handle})",
                f"- X profile URL: {x_snapshot.profile_url}",
                f"- X mirror URL: {x_snapshot.mirror_url}",
                f"- X mirror published time: {x_snapshot.mirror_published_time or 'unknown'}",
            ]
        )

    if status == "success":
        lines.extend(
            [
                "",
                "## New since last successful run",
                "",
                "### Changelog",
                *format_feed_items(new_changelog_items or []),
                "",
                "### Blog",
                *format_feed_items(new_blog_items or []),
                "",
                "### Official X posts",
                *format_x_posts(new_x_posts or []),
                "",
                "## Latest changelog snapshot",
                *format_feed_items(changelog_items),
                "",
                "## Latest blog snapshot",
                *format_feed_items(blog_items),
                "",
                "## Latest official X snapshot",
                *format_x_posts(x_snapshot.posts if x_snapshot else []),
            ]
        )

    return "\n".join(lines).strip() + "\n"


def write_latest_report(path: Path, report: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(report, encoding="utf-8")


def write_history_report(history_dir: Path, now_local: datetime, report: str) -> Path:
    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"{now_local.date().isoformat()}.md"
    history_path.write_text(report, encoding="utf-8")
    return history_path


def run_watch(
    *,
    force: bool = False,
    now: datetime | None = None,
    timezone_name: str = DEFAULT_TIMEZONE,
    target_hour: int = DEFAULT_TARGET_HOUR,
    report_limit: int = DEFAULT_REPORT_LIMIT,
    feed_scan_limit: int = DEFAULT_FEED_SCAN_LIMIT,
    x_limit: int = DEFAULT_X_LIMIT,
    state_path: Path = STATE_PATH,
    latest_report_path: Path = LATEST_REPORT_PATH,
    history_dir: Path = HISTORY_DIR,
    fetcher: Callable[[str], str] = fetch_text,
) -> RunResult:
    now_utc = ensure_aware_utc(now)
    now_local = now_utc.astimezone(ZoneInfo(timezone_name))
    state = load_state(state_path)
    should_run, reason = should_execute(
        now_local=now_local,
        force=force,
        state=state,
        timezone_name=timezone_name,
        target_hour=target_hour,
    )

    if not should_run:
        report = render_report(
            status="skipped",
            reason=reason,
            now_utc=now_utc,
            now_local=now_local,
            timezone_name=timezone_name,
            force=force,
            changelog_items=[],
            blog_items=[],
            x_snapshot=None,
        )
        write_latest_report(latest_report_path, report)
        return RunResult(
            status="skipped",
            reason=reason,
            report=report,
            changelog_items=[],
            blog_items=[],
            x_snapshot=None,
        )

    sitemap_text = fetcher(SITEMAP_URL)
    sitemap_entries = parse_sitemap_entries(sitemap_text)
    changelog_items = build_feed_items(
        sitemap_entries,
        entry_filter=is_changelog_post,
        limit=max(report_limit, feed_scan_limit),
        fetcher=fetcher,
    )
    blog_items = build_feed_items(
        sitemap_entries,
        entry_filter=is_blog_post,
        limit=max(report_limit, feed_scan_limit),
        fetcher=fetcher,
    )
    x_snapshot = parse_x_snapshot(fetcher(OFFICIAL_X_MIRROR_URL), limit=max(x_limit, report_limit))

    known_changelog_urls = set(state.get("known_changelog_urls", []))
    known_blog_urls = set(state.get("known_blog_urls", []))
    known_x_post_ids = set(state.get("known_x_post_ids", []))

    new_changelog_items = [item for item in changelog_items if item.url not in known_changelog_urls]
    new_blog_items = [item for item in blog_items if item.url not in known_blog_urls]

    x_posts_with_ids = [(hash_post(post), post) for post in x_snapshot.posts]
    new_x_posts = [post for post_id, post in x_posts_with_ids if post_id not in known_x_post_ids]

    snapshot_changelog = changelog_items[:report_limit]
    snapshot_blog = blog_items[:report_limit]
    snapshot_x_posts = x_snapshot.posts[:x_limit]
    snapshot_x = XSnapshot(
        account_name=x_snapshot.account_name,
        handle=x_snapshot.handle,
        profile_url=x_snapshot.profile_url,
        mirror_url=x_snapshot.mirror_url,
        mirror_published_time=x_snapshot.mirror_published_time,
        posts=snapshot_x_posts,
    )

    report = render_report(
        status="success",
        reason=reason,
        now_utc=now_utc,
        now_local=now_local,
        timezone_name=timezone_name,
        force=force,
        changelog_items=snapshot_changelog,
        blog_items=snapshot_blog,
        x_snapshot=snapshot_x,
        new_changelog_items=new_changelog_items,
        new_blog_items=new_blog_items,
        new_x_posts=new_x_posts,
    )

    write_latest_report(latest_report_path, report)
    history_path = write_history_report(history_dir, now_local, report)

    state["last_success_at"] = now_utc.isoformat()
    state["last_run_mode"] = "forced" if force else "scheduled"
    state["known_changelog_urls"] = dedupe_latest(
        state.get("known_changelog_urls", []),
        [item.url for item in changelog_items],
    )
    state["known_blog_urls"] = dedupe_latest(
        state.get("known_blog_urls", []),
        [item.url for item in blog_items],
    )
    state["known_x_post_ids"] = dedupe_latest(
        state.get("known_x_post_ids", []),
        [post_id for post_id, _ in x_posts_with_ids],
    )
    state["official_x_profile_url"] = x_snapshot.profile_url
    state["official_x_mirror_url"] = x_snapshot.mirror_url
    state["latest_report_path"] = str(latest_report_path)
    state["latest_history_path"] = str(history_path)
    if not force:
        state["last_scheduled_success_date"] = now_local.date().isoformat()

    save_state(state_path, state)

    return RunResult(
        status="success",
        reason=reason,
        report=report,
        changelog_items=snapshot_changelog,
        blog_items=snapshot_blog,
        x_snapshot=snapshot_x,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Watch Cursor changelog, blog, and official X updates.")
    parser.add_argument("--force", action="store_true", help="Run immediately instead of waiting for the scheduled hour.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA timezone name for the schedule gate.")
    parser.add_argument("--target-hour", type=int, default=DEFAULT_TARGET_HOUR, help="Local hour to run the scheduled fetch.")
    parser.add_argument("--report-limit", type=int, default=DEFAULT_REPORT_LIMIT, help="How many changelog/blog entries to show in the report.")
    parser.add_argument("--feed-scan-limit", type=int, default=DEFAULT_FEED_SCAN_LIMIT, help="How many sitemap entries per feed to scan for new URLs.")
    parser.add_argument("--x-limit", type=int, default=DEFAULT_X_LIMIT, help="How many official X posts to include in the report.")
    parser.add_argument("--state-dir", default=str(STATE_DIR), help="Directory for runtime state and generated reports.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    state_dir = Path(args.state_dir)

    result = run_watch(
        force=args.force,
        timezone_name=args.timezone,
        target_hour=args.target_hour,
        report_limit=args.report_limit,
        feed_scan_limit=args.feed_scan_limit,
        x_limit=args.x_limit,
        state_path=state_dir / "state.json",
        latest_report_path=state_dir / "latest_report.md",
        history_dir=state_dir / "history",
    )
    sys.stdout.write(result.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
