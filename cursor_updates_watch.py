#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Iterable, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
STATE_DIR = Path(".cursor_updates")
STATE_PATH = STATE_DIR / "state.json"
LATEST_REPORT_PATH = STATE_DIR / "latest_report.md"
HISTORY_DIR = STATE_DIR / "history"
PUBLIC_REPORT_PATH = Path("cursor_updates.md")

CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
BLOG_INDEX_URL = "https://cursor.com/en/blog"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai/",
    "https://r.jina.ai/http://www.x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)

RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str
    published_at: Optional[datetime] = None
    summary: str = ""

    @property
    def identity(self) -> str:
        if self.url:
            return self.url
        digest = hashlib.sha256(self.title.encode("utf-8")).hexdigest()
        return f"{self.source}:{digest}"


def shanghai_now(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(SHANGHAI_TZ)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_whitespace(unescape(text))


def slug_to_title(url: str) -> str:
    segment = urlparse(url).path.rstrip("/").split("/")[-1]
    return segment.replace("-", " ").strip().title() or url


def parse_iso_datetime(value: str) -> Optional[datetime]:
    if not value:
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_pubdate(value: str) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def load_state() -> dict:
    if not STATE_PATH.exists():
        return {
            "last_success_date": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
    with STATE_PATH.open("r", encoding="utf-8") as handle:
        state = json.load(handle)
    seen = state.setdefault("seen", {})
    for key in ("changelog", "blog", "x"):
        seen.setdefault(key, [])
    state.setdefault("last_success_date", None)
    return state


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)


def should_run_scheduled(now: datetime, last_success_date: Optional[str]) -> tuple[bool, str]:
    local_now = shanghai_now(now)
    if local_now.hour != 9:
        return False, (
            f"Skipping scheduled run: current Shanghai time is "
            f"{local_now.strftime('%Y-%m-%d %H:%M CST')}, outside the 09:00 hour."
        )
    local_date = local_now.date().isoformat()
    if last_success_date == local_date:
        return False, f"Skipping scheduled run: already completed for {local_date} CST."
    return True, f"Scheduled window open for {local_date} CST."


def fetch_text(url: str, timeout: int = 30, retries: int = 3, pause_seconds: float = 1.5) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        request = Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                )
            },
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, "replace")
        except HTTPError as error:
            last_error = error
            if error.code not in RETRYABLE_STATUS_CODES or attempt == retries:
                raise
        except URLError as error:
            last_error = error
            if attempt == retries:
                raise
        except TimeoutError as error:
            last_error = error
            if attempt == retries:
                raise
        time.sleep(pause_seconds * attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for item in root.findall("./channel/item"):
        title = normalize_whitespace(item.findtext("title", default=""))
        url = normalize_whitespace(item.findtext("link", default=""))
        published_at = parse_pubdate(item.findtext("pubDate", default=""))
        summary = strip_html(item.findtext("description", default=""))
        if title and url:
            items.append(
                UpdateItem(
                    source="changelog",
                    title=title,
                    url=url,
                    published_at=published_at,
                    summary=summary,
                )
            )
    return items


def extract_html_title(html: str, fallback_url: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\'](.*?)["\']',
        r"<title>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            title = normalize_whitespace(unescape(match.group(1)))
            title = re.sub(r"\s+[·|-]\s+Cursor$", "", title)
            if title:
                return title
    return slug_to_title(fallback_url)


def extract_html_description(html: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']',
        r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\'](.*?)["\']',
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE | re.DOTALL)
        if match:
            description = normalize_whitespace(unescape(match.group(1)))
            if description:
                return description
    return ""


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, Optional[datetime]]]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries: list[tuple[str, Optional[datetime]]] = []
    for node in root.findall("sm:url", namespace):
        loc = normalize_whitespace(node.findtext("sm:loc", default="", namespaces=namespace))
        if "/blog/" not in loc or not loc.startswith("https://cursor.com/"):
            continue
        if not re.match(r"^https://cursor\.com/blog/[^/]+/?$", loc):
            continue
        lastmod = parse_iso_datetime(node.findtext("sm:lastmod", default="", namespaces=namespace))
        entries.append((loc.rstrip("/"), lastmod))
    entries.sort(key=lambda item: item[1] or datetime.min.replace(tzinfo=UTC), reverse=True)
    deduped: list[tuple[str, Optional[datetime]]] = []
    seen_urls: set[str] = set()
    for url, lastmod in entries:
        if url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append((url, lastmod))
    return deduped


def parse_blog_index(html: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for match in re.finditer(r'href=["\'](/blog/[^"\']+)["\']', html, flags=re.IGNORECASE):
        url = f"https://cursor.com{match.group(1).split('?')[0]}".rstrip("/")
        if url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def fetch_blog_updates() -> list[UpdateItem]:
    blog_urls: list[tuple[str, Optional[datetime]]] = []
    try:
        sitemap = fetch_text(BLOG_SITEMAP_URL)
        blog_urls = parse_blog_sitemap(sitemap)[:10]
    except Exception:
        blog_urls = []

    if not blog_urls:
        fallback_html = fetch_text(BLOG_INDEX_URL)
        blog_urls = [(url, None) for url in parse_blog_index(fallback_html)[:10]]

    updates: list[UpdateItem] = []
    for url, lastmod in blog_urls:
        try:
            html = fetch_text(url)
            title = extract_html_title(html, url)
            summary = extract_html_description(html)
        except Exception:
            title = slug_to_title(url)
            summary = ""
        updates.append(
            UpdateItem(
                source="blog",
                title=title,
                url=url,
                published_at=lastmod,
                summary=summary,
            )
        )
    return updates


def parse_x_mirror_posts(markdown_text: str) -> list[UpdateItem]:
    marker = "Cursor’s posts"
    if marker not in markdown_text:
        return []
    lines = [line.strip() for line in markdown_text.splitlines()]
    start_index = lines.index(marker) + 1
    candidates: list[str] = []
    for line in lines[start_index:]:
        if not line:
            continue
        if line in {"--------------", "Pinned"}:
            continue
        if line.startswith("[![Image") or line.startswith("![Image"):
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue
        if line in {"Cursor", "@cursor_ai", "The best way to code with AI."}:
            continue
        cleaned = normalize_whitespace(line)
        if len(cleaned) < 12:
            continue
        candidates.append(cleaned)

    posts: list[UpdateItem] = []
    seen_titles: set[str] = set()
    for title in candidates:
        if title in seen_titles:
            continue
        seen_titles.add(title)
        digest = hashlib.sha256(title.encode("utf-8")).hexdigest()[:16]
        posts.append(
            UpdateItem(
                source="x",
                title=title,
                url=f"{X_PROFILE_URL}#post-{digest}",
                published_at=None,
                summary=title,
            )
        )
    return posts


def fetch_x_updates() -> list[UpdateItem]:
    last_error: Optional[Exception] = None
    for url in X_MIRROR_URLS:
        try:
            text = fetch_text(url, timeout=45)
            posts = parse_x_mirror_posts(text)
            if posts:
                return posts
        except Exception as error:
            last_error = error
    if last_error is not None:
        raise last_error
    return []


def select_updates(
    updates: list[UpdateItem],
    seen_identities: Iterable[str],
    now: datetime,
    bootstrap_limit: int,
) -> list[UpdateItem]:
    seen = set(seen_identities)
    if seen:
        return [item for item in updates if item.identity not in seen]

    window_start = now.astimezone(UTC) - timedelta(days=7)
    selected = [item for item in updates if item.published_at and item.published_at >= window_start]
    if selected:
        return selected[:bootstrap_limit]

    # Fallback for sources discovered from an index page without per-item dates.
    return updates[:bootstrap_limit]


def trim_seen(values: list[str], keep: int = 200) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped[:keep]


def render_section(section_title: str, updates: list[UpdateItem], empty_message: str) -> list[str]:
    lines = [f"## {section_title}", ""]
    if not updates:
        lines.append(f"- {empty_message}")
        lines.append("")
        return lines
    for item in updates:
        published_label = ""
        if item.published_at:
            published_label = f" ({item.published_at.astimezone(SHANGHAI_TZ).strftime('%Y-%m-%d')})"
        summary = f" - {item.summary}" if item.summary and item.summary != item.title else ""
        lines.append(f"- [{item.title}]({item.url}){published_label}{summary}")
    lines.append("")
    return lines


def build_report(
    changelog_updates: list[UpdateItem],
    blog_updates: list[UpdateItem],
    x_updates: list[UpdateItem],
    now: datetime,
    force: bool,
) -> str:
    local_now = shanghai_now(now)
    mode = "forced snapshot" if force else "scheduled run"
    lines = [
        f"# Cursor Updates Watch - {local_now.strftime('%Y-%m-%d %H:%M CST')}",
        "",
        f"- Mode: {mode}",
        f"- Changelog source: {CHANGELOG_RSS_URL}",
        f"- Blog sources: {BLOG_SITEMAP_URL} (fallback: {BLOG_INDEX_URL})",
        f"- Official X source: {X_PROFILE_URL} via Jina AI mirror",
        "",
        "## Summary",
        "",
        f"- Changelog items: {len(changelog_updates)}",
        f"- Blog posts: {len(blog_updates)}",
        f"- Official X posts: {len(x_updates)}",
        "",
    ]
    lines.extend(render_section("Changelog", changelog_updates, "No new changelog items detected."))
    lines.extend(render_section("Blog", blog_updates, "No new blog posts detected."))
    lines.extend(render_section("Official X (@cursor_ai)", x_updates, "No new X posts detected."))
    return "\n".join(lines).rstrip() + "\n"


def write_report(report: str, now: datetime) -> None:
    local_date = shanghai_now(now).date().isoformat()
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT_PATH.write_text(report, encoding="utf-8")
    PUBLIC_REPORT_PATH.write_text(report, encoding="utf-8")
    (HISTORY_DIR / f"{local_date}.md").write_text(report, encoding="utf-8")


def run(force: bool = False, now: Optional[datetime] = None) -> int:
    now = now or datetime.now(UTC)
    state = load_state()

    if not force:
        allowed, reason = should_run_scheduled(now, state.get("last_success_date"))
        print(reason)
        if not allowed:
            return 0

    changelog_all = parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))
    blog_all = fetch_blog_updates()
    x_all = fetch_x_updates()

    changelog_updates = select_updates(changelog_all, state["seen"]["changelog"], now, bootstrap_limit=50)
    blog_updates = select_updates(blog_all, state["seen"]["blog"], now, bootstrap_limit=50)
    if state["seen"]["x"]:
        x_updates = [item for item in x_all if item.identity not in set(state["seen"]["x"])]
    else:
        x_updates = x_all[:8]

    report = build_report(changelog_updates, blog_updates, x_updates, now, force=force)
    write_report(report, now)

    if not force:
        local_date = shanghai_now(now).date().isoformat()
        state["last_success_date"] = local_date
        state["seen"]["changelog"] = trim_seen(
            [item.identity for item in changelog_all] + state["seen"]["changelog"]
        )
        state["seen"]["blog"] = trim_seen(
            [item.identity for item in blog_all] + state["seen"]["blog"]
        )
        state["seen"]["x"] = trim_seen([item.identity for item in x_all] + state["seen"]["x"])
        save_state(state)

    print(report)
    if force:
        print("Forced snapshot completed without updating seen-state.")
    else:
        print(f"State updated at {STATE_PATH}.")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch daily Cursor changelog, blog, and X updates.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without the 09:00 Shanghai gate and without updating seen-state.",
    )
    args = parser.parse_args(argv)
    try:
        return run(force=args.force)
    except Exception as error:
        print(f"Cursor updates watcher failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
