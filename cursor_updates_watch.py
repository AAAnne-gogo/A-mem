#!/usr/bin/env python3
"""Watch official Cursor updates and write a daily report."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree
from zoneinfo import ZoneInfo


USER_AGENT = "Mozilla/5.0 (CursorUpdatesWatcher/1.0)"
TIME_ZONE = ZoneInfo("Asia/Shanghai")
TARGET_HOUR = 9
ROOT_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = ROOT_DIR / ".cursor_updates"
STATE_PATH = RUNTIME_DIR / "state.json"
LATEST_REPORT_PATH = RUNTIME_DIR / "latest_report.md"
ROOT_REPORT_PATH = ROOT_DIR / "cursor_updates.md"
HISTORY_DIR = RUNTIME_DIR / "history"
CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_INDEX_URL = "https://cursor.com/en/blog"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_SOURCE_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai/",
    "https://r.jina.ai/http://www.x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)
BLOG_FETCH_LIMIT = 10
CHANGELOG_BOOTSTRAP_LOOKBACK_DAYS = 7
BLOG_BOOTSTRAP_LOOKBACK_DAYS = 7
X_BOOTSTRAP_LIMIT = 8
SEEN_LIMIT = 200
RETRYABLE_HTTP_CODES = {403, 408, 425, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str
    summary: str
    published: datetime | None
    identity: str


def log(message: str) -> None:
    print(message, flush=True)


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def html_to_text(value: str) -> str:
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_whitespace(unescape(value))


def clean_cursor_title(value: str) -> str:
    value = normalize_whitespace(unescape(value))
    for suffix in (" · Cursor", " | Cursor - The AI Code Editor", " | Cursor"):
        if value.endswith(suffix):
            return value[: -len(suffix)].strip()
    return value


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_rfc822_datetime(value: str) -> datetime:
    parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def parse_iso_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def make_identity(value: str) -> str:
    return hashlib.sha256(normalize_whitespace(value).lower().encode("utf-8")).hexdigest()


def fetch_text(urls: str | Iterable[str], timeout: int = 30, retries: int = 3) -> str:
    if isinstance(urls, str):
        url_candidates = [urls]
    else:
        url_candidates = list(urls)

    last_error: Exception | None = None
    for url in url_candidates:
        for attempt in range(1, retries + 1):
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_HTTP_CODES or attempt == retries:
                    break
                time.sleep(2 ** (attempt - 1))
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == retries:
                    break
                time.sleep(2 ** (attempt - 1))
    if last_error is None:
        raise RuntimeError("No URLs supplied")
    raise RuntimeError(f"Failed to fetch source: {last_error}") from last_error


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ElementTree.fromstring(xml_text)
    items: list[UpdateItem] = []
    for node in root.findall("./channel/item"):
        title = clean_cursor_title(node.findtext("title", default=""))
        url = normalize_whitespace(node.findtext("link", default=""))
        summary = html_to_text(node.findtext("description", default=""))
        pub_date = parse_rfc822_datetime(node.findtext("pubDate", default=""))
        items.append(
            UpdateItem(
                source="changelog",
                title=title,
                url=url,
                summary=summary,
                published=pub_date,
                identity=url or make_identity(f"changelog:{title}:{summary}"),
            )
        )
    return sorted(items, key=lambda item: item.published or datetime.min.replace(tzinfo=timezone.utc), reverse=True)


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, datetime]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ElementTree.fromstring(xml_text)
    candidates: list[tuple[str, datetime]] = []
    for node in root.findall("sm:url", namespace):
        loc = normalize_whitespace(node.findtext("sm:loc", default="", namespaces=namespace))
        if not loc.startswith("https://cursor.com/blog/"):
            continue
        lastmod_raw = normalize_whitespace(node.findtext("sm:lastmod", default="", namespaces=namespace))
        if not lastmod_raw:
            continue
        candidates.append((loc, parse_iso_datetime(lastmod_raw)))

    deduped: dict[str, datetime] = {}
    for url, published in candidates:
        existing = deduped.get(url)
        if existing is None or published > existing:
            deduped[url] = published
    ordered = sorted(deduped.items(), key=lambda item: item[1], reverse=True)
    return ordered[:BLOG_FETCH_LIMIT]


def parse_blog_index_links(html_text: str) -> list[tuple[str, datetime | None]]:
    matches = re.findall(r"https://cursor\.com/blog/[a-zA-Z0-9][a-zA-Z0-9\-_/]*", html_text)
    seen: set[str] = set()
    results: list[tuple[str, datetime | None]] = []
    for url in matches:
        if url in seen:
            continue
        seen.add(url)
        results.append((url, None))
        if len(results) >= BLOG_FETCH_LIMIT:
            break
    return results


def parse_blog_article(html_text: str, url: str, published: datetime | None) -> UpdateItem:
    title = None
    description = None
    for pattern in (
        r'<meta property="og:title" content="(.*?)"',
        r'<meta name="twitter:title" content="(.*?)"',
        r"<title>(.*?)</title>",
    ):
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = clean_cursor_title(match.group(1))
            break
    for pattern in (
        r'<meta property="og:description" content="(.*?)"',
        r'<meta name="description" content="(.*?)"',
        r'<meta name="twitter:description" content="(.*?)"',
    ):
        match = re.search(pattern, html_text, flags=re.IGNORECASE | re.DOTALL)
        if match:
            description = normalize_whitespace(unescape(match.group(1)))
            break

    if not title:
        title = clean_cursor_title(url.rstrip("/").rsplit("/", 1)[-1].replace("-", " "))
    if not description:
        description = ""

    return UpdateItem(
        source="blog",
        title=title,
        url=url,
        summary=description,
        published=published,
        identity=url,
    )


def fetch_blog_items() -> list[UpdateItem]:
    try:
        candidates = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
    except Exception:
        candidates = parse_blog_index_links(fetch_text(BLOG_INDEX_URL))

    items: list[UpdateItem] = []
    for url, published in candidates:
        try:
            article_html = fetch_text(url)
        except Exception:
            article_html = ""
        items.append(parse_blog_article(article_html, url, published))
    return items


def parse_x_markdown(markdown_text: str) -> list[UpdateItem]:
    if "Cursor’s posts" in markdown_text:
        markdown_text = markdown_text.split("Cursor’s posts", 1)[1]

    lines = [line.strip() for line in markdown_text.splitlines()]
    candidates: list[str] = []
    for line in lines:
        if not line:
            continue
        if line.startswith("Title:") or line.startswith("URL Source:") or line.startswith("Published Time:"):
            continue
        if line == "--------------" or line == "Pinned":
            continue
        if line.startswith("[![Image"):
            continue
        if line.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        if line in {"Cursor", "@cursor_ai", "Markdown Content:", "!", "Don’t miss what’s happening", "People on X are the first to know."}:
            continue
        candidates.append(normalize_whitespace(line))

    items: list[UpdateItem] = []
    seen: set[str] = set()
    for text in candidates:
        identity = make_identity(f"x:{text}")
        if identity in seen:
            continue
        seen.add(identity)
        items.append(
            UpdateItem(
                source="x",
                title=text,
                url="https://x.com/cursor_ai",
                summary="",
                published=None,
                identity=identity,
            )
        )
    return items


def fetch_x_items() -> list[UpdateItem]:
    markdown_text = fetch_text(X_SOURCE_URLS, timeout=45)
    return parse_x_markdown(markdown_text)


def should_run_scheduled(now_local: datetime, state: dict[str, object]) -> tuple[bool, str]:
    if now_local.hour != TARGET_HOUR:
        return False, f"Current Asia/Shanghai hour is {now_local.hour:02d}; waiting for {TARGET_HOUR:02d}:00."
    last_success_date = state.get("last_success_date")
    if last_success_date == now_local.date().isoformat():
        return False, f"Scheduled run already completed for {last_success_date}."
    return True, "Scheduled window is open."


def select_new_items(
    items: list[UpdateItem],
    seen_ids: set[str],
    now_utc: datetime,
    bootstrap_lookback_days: int | None = None,
    bootstrap_limit: int | None = None,
) -> list[UpdateItem]:
    if seen_ids:
        selected = [item for item in items if item.identity not in seen_ids]
    else:
        selected = items
        if bootstrap_lookback_days is not None:
            cutoff = now_utc - timedelta(days=bootstrap_lookback_days)
            selected = [item for item in selected if item.published is None or item.published >= cutoff]
        if bootstrap_limit is not None:
            selected = selected[:bootstrap_limit]
    return selected


def format_item(item: UpdateItem) -> str:
    prefix = ""
    if item.published is not None:
        prefix = f"{item.published.astimezone(TIME_ZONE):%Y-%m-%d} - "
    summary = f" - {item.summary}" if item.summary else ""
    return f"- {prefix}[{item.title}]({item.url}){summary}"


def build_report(
    now_local: datetime,
    mode: str,
    sections: dict[str, list[UpdateItem]],
    errors: dict[str, str],
) -> str:
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- Generated at: {now_local:%Y-%m-%d %H:%M:%S %Z}",
        f"- Mode: {mode}",
        "- Sources: changelog RSS, blog pages, official X mirror",
        "",
        "## Summary",
        f"- Changelog: {len(sections['changelog'])} new item(s)",
        f"- Blog: {len(sections['blog'])} new item(s)",
        f"- Official X: {len(sections['x'])} new post(s)",
        "",
    ]

    if errors:
        lines.extend(["## Fetch Warnings", ""])
        for source, error in errors.items():
            lines.append(f"- {source}: {error}")
        lines.append("")

    lines.extend(["## Changelog", ""])
    lines.extend([format_item(item) for item in sections["changelog"]] or ["- No new changelog entries detected."])
    lines.append("")
    lines.extend(["## Blog", ""])
    lines.extend([format_item(item) for item in sections["blog"]] or ["- No new blog posts detected."])
    lines.append("")
    lines.extend(["## Official X", ""])
    lines.extend([f"- {item.title}" for item in sections["x"]] or ["- No new official X posts detected."])
    lines.append("")
    return "\n".join(lines)


def ensure_runtime_dirs() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def load_state() -> dict[str, object]:
    if not STATE_PATH.exists():
        return {"seen": {"changelog": [], "blog": [], "x": []}}
    return json.loads(STATE_PATH.read_text(encoding="utf-8"))


def store_state(state: dict[str, object]) -> None:
    ensure_runtime_dirs()
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def update_seen(previous: list[str], items: list[UpdateItem]) -> list[str]:
    merged = list(previous)
    known = set(previous)
    for item in items:
        if item.identity in known:
            continue
        merged.insert(0, item.identity)
        known.add(item.identity)
    return merged[:SEEN_LIMIT]


def write_report(report_text: str, report_date: str) -> None:
    ensure_runtime_dirs()
    history_path = HISTORY_DIR / f"{report_date}.md"
    LATEST_REPORT_PATH.write_text(report_text, encoding="utf-8")
    ROOT_REPORT_PATH.write_text(report_text, encoding="utf-8")
    history_path.write_text(report_text, encoding="utf-8")


def collect_updates(now_utc: datetime, state: dict[str, object]) -> tuple[dict[str, list[UpdateItem]], dict[str, str], dict[str, list[UpdateItem]]]:
    seen = state.get("seen", {})
    all_items: dict[str, list[UpdateItem]] = {"changelog": [], "blog": [], "x": []}
    new_items: dict[str, list[UpdateItem]] = {"changelog": [], "blog": [], "x": []}
    errors: dict[str, str] = {}

    try:
        changelog_items = parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))
        all_items["changelog"] = changelog_items
        new_items["changelog"] = select_new_items(
            changelog_items,
            set(seen.get("changelog", [])),
            now_utc,
            bootstrap_lookback_days=CHANGELOG_BOOTSTRAP_LOOKBACK_DAYS,
        )
    except Exception as exc:
        errors["changelog"] = str(exc)

    try:
        blog_items = fetch_blog_items()
        all_items["blog"] = blog_items
        new_items["blog"] = select_new_items(
            blog_items,
            set(seen.get("blog", [])),
            now_utc,
            bootstrap_lookback_days=BLOG_BOOTSTRAP_LOOKBACK_DAYS,
        )
    except Exception as exc:
        errors["blog"] = str(exc)

    try:
        x_items = fetch_x_items()
        all_items["x"] = x_items
        new_items["x"] = select_new_items(
            x_items,
            set(seen.get("x", [])),
            now_utc,
            bootstrap_limit=X_BOOTSTRAP_LIMIT,
        )
    except Exception as exc:
        errors["x"] = str(exc)

    return new_items, errors, all_items


def run(force: bool, now_utc: datetime | None = None) -> int:
    now_utc = now_utc or datetime.now(timezone.utc)
    now_local = now_utc.astimezone(TIME_ZONE)
    state = load_state()

    if not force:
        should_run, reason = should_run_scheduled(now_local, state)
        log(reason)
        if not should_run:
            return 0

    mode = "force" if force else "scheduled"
    new_items, errors, all_items = collect_updates(now_utc, state)
    if not any(all_items.values()) and errors:
        log("All sources failed; report not written.")
        for source, error in errors.items():
            log(f"- {source}: {error}")
        return 1

    report_text = build_report(now_local, mode, new_items, errors)
    write_report(report_text, now_local.date().isoformat())
    log(f"Report written to {ROOT_REPORT_PATH}")

    if force:
        log("Force mode enabled; seen-state and daily slot were not updated.")
        return 0

    seen = state.setdefault("seen", {"changelog": [], "blog": [], "x": []})
    for source, items in all_items.items():
        seen[source] = update_seen(list(seen.get(source, [])), items)
    state["last_success_at"] = isoformat_z(now_utc)
    state["last_success_date"] = now_local.date().isoformat()
    store_state(state)
    log(f"State updated for {state['last_success_date']}.")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Run immediately and do not update seen-state.")
    parser.add_argument(
        "--now-utc",
        help="Override current time in UTC (ISO-8601). Intended for tests and manual verification.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    now_utc = parse_iso_datetime(args.now_utc) if args.now_utc else None
    return run(force=args.force, now_utc=now_utc)


if __name__ == "__main__":
    raise SystemExit(main())
