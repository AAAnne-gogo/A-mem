#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT_DIR = Path(__file__).resolve().parent
STATE_DIR = ROOT_DIR / ".cursor_updates"
STATE_PATH = STATE_DIR / "state.json"
LATEST_REPORT_PATH = STATE_DIR / "latest_report.md"
HISTORY_DIR = STATE_DIR / "history"

DEFAULT_TIMEZONE = "Asia/Shanghai"
SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
USER_AGENT = "Mozilla/5.0 (CursorUpdatesWatch/1.0)"


@dataclass(frozen=True)
class FeedEntry:
    kind: str
    title: str
    url: str
    slug: str
    lastmod: str | None
    published_date: str | None


@dataclass(frozen=True)
class XSnapshot:
    account_name: str
    account_handle: str
    profile_url: str
    mirror_url: str
    published_time: str | None
    posts: list[str]


def fetch_text(url: str, timeout: int = 45) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def parse_iso_datetime(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    try:
        return datetime.fromisoformat(raw_value.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_to_date(raw_value: str | None) -> str | None:
    parsed = parse_iso_datetime(raw_value)
    return parsed.date().isoformat() if parsed else None


def slug_to_title(slug: str) -> str:
    return slug.replace("-", " ").strip().title()


def clean_page_title(raw_title: str, fallback_slug: str) -> str:
    title = normalize_whitespace(raw_title)
    title = re.sub(r"\s+[·\-\|]\s+Cursor$", "", title)
    return title or slug_to_title(fallback_slug)


def extract_html_title(html: str, fallback_slug: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:title["\']',
        r"<title>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            return clean_page_title(match.group(1), fallback_slug)
    return slug_to_title(fallback_slug)


def fetch_page_title(url: str, slug: str) -> str:
    try:
        html = fetch_text(url)
    except Exception:
        return slug_to_title(slug)
    return extract_html_title(html, slug)


def parse_sitemap_entries(xml_text: str) -> list[FeedEntry]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[FeedEntry] = []
    pattern = re.compile(r"^https://cursor\.com/(?P<kind>blog|changelog)/(?P<slug>[^/?#]+)/?$")

    for url_element in root.findall("sm:url", namespace):
        loc_element = url_element.find("sm:loc", namespace)
        lastmod_element = url_element.find("sm:lastmod", namespace)
        if loc_element is None or not loc_element.text:
            continue

        loc = loc_element.text.strip()
        match = pattern.match(loc)
        if not match:
            continue

        lastmod = lastmod_element.text.strip() if lastmod_element is not None and lastmod_element.text else None
        entries.append(
            FeedEntry(
                kind=match.group("kind"),
                title=slug_to_title(match.group("slug")),
                url=loc,
                slug=match.group("slug"),
                lastmod=lastmod,
                published_date=iso_to_date(lastmod),
            )
        )

    entries.sort(
        key=lambda entry: (
            parse_iso_datetime(entry.lastmod) or datetime.min.replace(tzinfo=timezone.utc),
            entry.url,
        ),
        reverse=True,
    )
    return entries


def enrich_feed_entries(entries: list[FeedEntry], limit: int = 5) -> list[FeedEntry]:
    enriched: list[FeedEntry] = []
    for entry in entries[:limit]:
        enriched.append(
            FeedEntry(
                kind=entry.kind,
                title=fetch_page_title(entry.url, entry.slug),
                url=entry.url,
                slug=entry.slug,
                lastmod=entry.lastmod,
                published_date=entry.published_date,
            )
        )
    return enriched


def should_skip_x_line(line: str) -> bool:
    lowered = line.lower()
    if not line:
        return True
    if line.startswith("[") or line.startswith("!"):
        return True
    if lowered in {
        "cursor",
        "@cursor_ai",
        "pinned",
        "cursor’s posts",
        "cursor's posts",
        "the best way to code with ai.",
    }:
        return True
    if set(line) <= {"-"}:
        return True
    if re.fullmatch(r"\d+:\d+", line):
        return True
    if re.fullmatch(r"[\d,.]+[kmb]?", lowered):
        return True
    if len(line) < 20:
        return True
    return False


def extract_x_posts(mirror_text: str, limit: int = 5) -> tuple[str | None, list[str]]:
    published_match = re.search(r"^Published Time:\s*(.+)$", mirror_text, flags=re.MULTILINE)
    published_time = published_match.group(1).strip() if published_match else None
    body = mirror_text.split("Markdown Content:", 1)[-1]

    if "Don’t miss what’s happening" in body or "Don't miss what's happening" in body:
        raise RuntimeError("X mirror returned a login wall instead of profile posts.")

    collecting = False
    posts: list[str] = []
    seen: set[str] = set()

    for raw_line in body.splitlines():
        line = normalize_whitespace(raw_line)
        if not line:
            continue
        if line in {"Cursor’s posts", "Cursor's posts"}:
            collecting = True
            continue
        if not collecting:
            continue
        if should_skip_x_line(line):
            continue
        if line.lower().startswith("cursor reposted"):
            continue
        normalized_key = line.casefold()
        if normalized_key in seen:
            continue
        seen.add(normalized_key)
        posts.append(line)
        if len(posts) >= limit:
            break

    if not posts:
        raise RuntimeError("Could not extract any visible posts from the Cursor X mirror.")

    return published_time, posts


def fetch_x_snapshot(limit: int = 5) -> XSnapshot:
    mirror_text = fetch_text(X_MIRROR_URL, timeout=60)
    published_time, posts = extract_x_posts(mirror_text, limit=limit)
    return XSnapshot(
        account_name="Cursor",
        account_handle="@cursor_ai",
        profile_url=X_PROFILE_URL,
        mirror_url=X_MIRROR_URL,
        published_time=published_time,
        posts=posts,
    )


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def evaluate_run_gate(
    now_utc: datetime,
    timezone_name: str,
    last_success_local_date: str | None,
    force: bool,
) -> tuple[bool, str, datetime]:
    local_now = now_utc.astimezone(ZoneInfo(timezone_name))

    if force:
        return True, "Proceeding because --force was provided.", local_now

    if local_now.hour != 9:
        return False, f"Skipping run: local time in {timezone_name} is {local_now:%H:%M}, expected 09:00 hour.", local_now

    if last_success_local_date == local_now.date().isoformat():
        return False, f"Skipping run: successful check for {last_success_local_date} already exists.", local_now

    return True, "Scheduled run window matched.", local_now


def diff_feed_entries(current: list[FeedEntry], previous: list[dict[str, Any]]) -> list[FeedEntry]:
    previous_urls = {item.get("url") for item in previous}
    return [entry for entry in current if entry.url not in previous_urls]


def diff_x_posts(current_posts: list[str], previous_snapshot: dict[str, Any]) -> list[str]:
    previous_posts = set(previous_snapshot.get("posts", []))
    return [post for post in current_posts if post not in previous_posts]


def feed_lines(entries: list[FeedEntry]) -> list[str]:
    lines: list[str] = []
    for entry in entries:
        published_date = entry.published_date or "unknown-date"
        lines.append(f"- {published_date} | {entry.title} | {entry.url}")
    return lines or ["- None"]


def quoted_lines(items: list[str]) -> list[str]:
    return [f'- "{item}"' for item in items] or ["- None"]


def build_report(
    now_utc: datetime,
    local_now: datetime,
    timezone_name: str,
    changelog_entries: list[FeedEntry],
    blog_entries: list[FeedEntry],
    x_snapshot: XSnapshot,
    new_changelog_entries: list[FeedEntry],
    new_blog_entries: list[FeedEntry],
    new_x_posts: list[str],
) -> str:
    lines = [
        "# Cursor updates report",
        "",
        f"- Checked at (UTC): {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"- Checked at ({timezone_name}): {local_now.isoformat(timespec='seconds')}",
        "",
        "## New since previous successful run",
        "",
        "### Changelog",
        *feed_lines(new_changelog_entries),
        "",
        "### Blog",
        *feed_lines(new_blog_entries),
        "",
        "### Official X posts",
        *quoted_lines(new_x_posts),
        "",
        "## Latest changelog seen",
        *feed_lines(changelog_entries),
        "",
        "## Latest blog seen",
        *feed_lines(blog_entries),
        "",
        "## Official X account",
        f"- Account confirmed: {x_snapshot.account_name} ({x_snapshot.account_handle})",
        f"- Profile URL: {x_snapshot.profile_url}",
        f"- Public mirror used in latest run: {x_snapshot.mirror_url}",
        f"- Mirror published time in latest run: {x_snapshot.published_time or 'unknown'}",
        "- Last confirmed visible posts snapshot:",
        *quoted_lines(x_snapshot.posts),
        "",
    ]
    return "\n".join(lines)


def write_report(local_date: str, report: str) -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT_PATH.write_text(report + "\n", encoding="utf-8")
    (HISTORY_DIR / f"{local_date}.md").write_text(report + "\n", encoding="utf-8")


def build_state_payload(
    previous_state: dict[str, Any],
    now_utc: datetime,
    local_now: datetime,
    timezone_name: str,
    changelog_entries: list[FeedEntry],
    blog_entries: list[FeedEntry],
    x_snapshot: XSnapshot,
    force: bool,
) -> dict[str, Any]:
    state_payload: dict[str, Any] = {
        "last_success_at": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "last_success_local_timestamp": local_now.isoformat(timespec="seconds"),
        "last_observed_local_date": local_now.date().isoformat(),
        "last_run_mode": "force" if force else "scheduled",
        "timezone": timezone_name,
        "latest_changelog": [asdict(entry) for entry in changelog_entries],
        "latest_blog": [asdict(entry) for entry in blog_entries],
        "x_snapshot": asdict(x_snapshot),
    }
    if force:
        state_payload["last_success_local_date"] = previous_state.get("last_success_local_date")
    else:
        state_payload["last_success_local_date"] = local_now.date().isoformat()
    return state_payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X posts.")
    parser.add_argument("--force", action="store_true", help="Run immediately, ignoring the 09:00 schedule and duplicate-run guard.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help=f"IANA timezone name. Default: {DEFAULT_TIMEZONE}")
    parser.add_argument("--limit", type=int, default=5, help="How many recent items to keep per source.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    now_utc = datetime.now(timezone.utc)
    state = load_state(STATE_PATH)
    should_run, message, local_now = evaluate_run_gate(
        now_utc=now_utc,
        timezone_name=args.timezone,
        last_success_local_date=state.get("last_success_local_date"),
        force=args.force,
    )

    if not should_run:
        print(message)
        return 0

    print(message)

    try:
        sitemap_text = fetch_text(SITEMAP_URL)
        parsed_entries = parse_sitemap_entries(sitemap_text)
        changelog_entries = enrich_feed_entries([entry for entry in parsed_entries if entry.kind == "changelog"], limit=args.limit)
        blog_entries = enrich_feed_entries([entry for entry in parsed_entries if entry.kind == "blog"], limit=args.limit)
        x_snapshot = fetch_x_snapshot(limit=args.limit)
    except Exception as exc:
        print(f"Cursor update fetch failed: {exc}", file=sys.stderr)
        return 1

    new_changelog_entries = diff_feed_entries(changelog_entries, state.get("latest_changelog", []))
    new_blog_entries = diff_feed_entries(blog_entries, state.get("latest_blog", []))
    new_x_posts = diff_x_posts(x_snapshot.posts, state.get("x_snapshot", {}))
    report = build_report(
        now_utc=now_utc,
        local_now=local_now,
        timezone_name=args.timezone,
        changelog_entries=changelog_entries,
        blog_entries=blog_entries,
        x_snapshot=x_snapshot,
        new_changelog_entries=new_changelog_entries,
        new_blog_entries=new_blog_entries,
        new_x_posts=new_x_posts,
    )

    write_report(local_now.date().isoformat(), report)
    save_state(
        STATE_PATH,
        build_state_payload(
            previous_state=state,
            now_utc=now_utc,
            local_now=local_now,
            timezone_name=args.timezone,
            changelog_entries=changelog_entries,
            blog_entries=blog_entries,
            x_snapshot=x_snapshot,
            force=args.force,
        ),
    )

    print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
