#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

LOCAL_TZ = ZoneInfo("Asia/Shanghai")
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
BOOTSTRAP_DAYS = 7
BOOTSTRAP_FALLBACK_LIMIT = 5
X_BOOTSTRAP_LIMIT = 8
MAX_SEEN_PER_SOURCE = 100
HTTP_TIMEOUT_SECONDS = 30
RETRYABLE_HTTP_CODES = {403, 429, 500, 502, 503, 504}


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str
    identity: str
    published_at: str | None = None
    summary: str = ""

    def published_datetime(self) -> datetime | None:
        if not self.published_at:
            return None
        return parse_datetime(self.published_at)


@dataclass(frozen=True)
class RuntimePaths:
    base_dir: Path

    @property
    def state_dir(self) -> Path:
        return self.base_dir / ".cursor_updates"

    @property
    def history_dir(self) -> Path:
        return self.state_dir / "history"

    @property
    def state_path(self) -> Path:
        return self.state_dir / "state.json"

    @property
    def latest_report_path(self) -> Path:
        return self.state_dir / "latest_report.md"

    @property
    def summary_path(self) -> Path:
        return self.base_dir / "cursor_updates.md"


def now_utc() -> datetime:
    return datetime.now(UTC)


def parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_html(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", text)
    return collapse_whitespace(unescape(without_tags))


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = re.sub(r"/+$", "", parts.path or "/")
    if not path:
        path = "/"
    return urlunsplit((scheme, netloc, path, "", ""))


def build_identity(source: str, url: str, title: str) -> str:
    if source == "x":
        return collapse_whitespace(title).lower()
    return normalize_url(url)


def slug_to_title(url: str) -> str:
    slug = normalize_url(url).rstrip("/").split("/")[-1]
    return collapse_whitespace(unescape(slug.replace("-", " ").replace("_", " "))).title()


def fetch_text(
    url: str,
    *,
    timeout: int = HTTP_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    attempts: int = 4,
) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; CursorUpdatesWatch/1.0)",
        "Accept": "text/html,application/rss+xml,application/xml,text/plain,*/*",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = Request(url, headers=headers)
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            last_error = error
            if error.code not in RETRYABLE_HTTP_CODES or attempt == attempts - 1:
                raise
        except URLError as error:
            last_error = error
            if attempt == attempts - 1:
                raise
        sleep(2**attempt)
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed to fetch {url}")


def parse_changelog_items(xml_text: str) -> list[UpdateItem]:
    root = ElementTree.fromstring(xml_text)
    items: list[UpdateItem] = []
    for node in root.findall("./channel/item"):
        title = collapse_whitespace(node.findtext("title", default=""))
        url = collapse_whitespace(node.findtext("link", default=""))
        description = strip_html(node.findtext("description", default=""))
        pub_date = node.findtext("pubDate", default="").strip()
        published_at = None
        if pub_date:
            published_at = format_datetime(parsedate_to_datetime(pub_date))
        if not title or not url:
            continue
        normalized_url = normalize_url(url)
        items.append(
            UpdateItem(
                source="changelog",
                title=title,
                url=normalized_url,
                identity=build_identity("changelog", normalized_url, title),
                published_at=published_at,
                summary=description,
            )
        )
    return sort_items(items)


ARTICLE_RE = re.compile(
    r"<article\b.*?<a\b[^>]*href=\"(?P<href>/blog/[^\"]+)\"[^>]*>"
    r".*?<p\b[^>]*>(?P<title>.*?)</p>"
    r"(?:.*?<p\b[^>]*>(?P<summary>.*?)</p>)?"
    r"(?:.*?<time\b[^>]*datetime=\"(?P<date>[^\"]+)\")?",
    re.DOTALL,
)


def parse_blog_index_items(html_text: str) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    seen: set[str] = set()
    for match in ARTICLE_RE.finditer(html_text):
        href = match.group("href")
        url = normalize_url(urljoin("https://cursor.com", href))
        if url in seen:
            continue
        seen.add(url)
        title = strip_html(match.group("title") or "")
        summary = strip_html(match.group("summary") or "")
        published_at = None
        if match.group("date"):
            published_at = format_datetime(parse_datetime(match.group("date")))
        if not title:
            title = slug_to_title(url)
        items.append(
            UpdateItem(
                source="blog",
                title=title,
                url=url,
                identity=build_identity("blog", url, title),
                published_at=published_at,
                summary=summary,
            )
        )
    return sort_items(items)


def parse_blog_sitemap_items(xml_text: str) -> list[UpdateItem]:
    root = ElementTree.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    items: list[UpdateItem] = []
    seen: set[str] = set()
    for node in root.findall("sm:url", namespace):
        loc = collapse_whitespace(node.findtext("sm:loc", default="", namespaces=namespace))
        if not loc:
            continue
        url = normalize_url(loc)
        if "/blog/" not in url:
            continue
        if re.search(r"/(cn|ja|zh-Hant|es|fr|pt-BR|ko|de)/blog/", url):
            continue
        if url in seen:
            continue
        seen.add(url)
        lastmod = collapse_whitespace(node.findtext("sm:lastmod", default="", namespaces=namespace))
        published_at = format_datetime(parse_datetime(lastmod)) if lastmod else None
        items.append(
            UpdateItem(
                source="blog",
                title=slug_to_title(url),
                url=url,
                identity=build_identity("blog", url, slug_to_title(url)),
                published_at=published_at,
                summary="",
            )
        )
    return sort_items(items)


def merge_blog_items(index_items: list[UpdateItem], sitemap_items: list[UpdateItem]) -> list[UpdateItem]:
    if not sitemap_items:
        return index_items
    index_by_url = {item.url: item for item in index_items}
    merged: list[UpdateItem] = []
    seen: set[str] = set()
    for item in sitemap_items:
        if item.url in seen:
            continue
        seen.add(item.url)
        indexed = index_by_url.get(item.url)
        title = indexed.title if indexed and indexed.title else item.title
        summary = indexed.summary if indexed else item.summary
        published_at = indexed.published_at if indexed and indexed.published_at else item.published_at
        merged.append(
            UpdateItem(
                source="blog",
                title=title,
                url=item.url,
                identity=build_identity("blog", item.url, title),
                published_at=published_at,
                summary=summary,
            )
        )
    for item in index_items:
        if item.url in seen:
            continue
        seen.add(item.url)
        merged.append(item)
    return sort_items(merged)


X_POST_RE = re.compile(
    r"\[\!\[Image \d+: Square profile picture.*?\]\(https?://x\.com/cursor_ai\)\s*(?P<body>.*?)(?=\n\[\!\[Image \d+: Square profile picture|\Z)",
    re.DOTALL,
)


def is_x_noise_line(line: str) -> bool:
    return bool(re.fullmatch(r"\d+:\d{2}", line))


def parse_x_items(markdown_text: str) -> list[UpdateItem]:
    posts_section = markdown_text.split("Cursor’s posts", 1)
    content = posts_section[1] if len(posts_section) == 2 else markdown_text
    items: list[UpdateItem] = []
    seen: set[str] = set()
    for match in X_POST_RE.finditer(content):
        body = match.group("body")
        lines: list[str] = []
        for raw_line in body.splitlines():
            line = collapse_whitespace(raw_line)
            if not line:
                continue
            if line == "Pinned":
                continue
            if line.startswith("!["):
                continue
            if line.startswith("[!["):
                continue
            if is_x_noise_line(line):
                continue
            lines.append(line)
        if not lines:
            continue
        text = collapse_whitespace(" ".join(lines))
        if not text or text == "Pinned":
            continue
        identity = build_identity("x", X_PROFILE_URL, text)
        if identity in seen:
            continue
        seen.add(identity)
        title = text if len(text) <= 140 else text[:137].rstrip() + "..."
        items.append(
            UpdateItem(
                source="x",
                title=title,
                url=X_PROFILE_URL,
                identity=identity,
                summary=text,
            )
        )
    return items


def sort_items(items: list[UpdateItem]) -> list[UpdateItem]:
    def sort_key(item: UpdateItem) -> tuple[int, float, str]:
        published = item.published_datetime()
        timestamp = published.timestamp() if published else float("-inf")
        return (1 if published else 0, timestamp, item.title.lower())

    return sorted(items, key=sort_key, reverse=True)


def select_updates(
    items: list[UpdateItem],
    *,
    source: str,
    seen_identities: set[str],
    reference_now: datetime,
) -> list[UpdateItem]:
    if seen_identities:
        return [item for item in items if item.identity not in seen_identities]
    if source in {"changelog", "blog"}:
        cutoff = reference_now.astimezone(UTC) - timedelta(days=BOOTSTRAP_DAYS)
        recent = [
            item
            for item in items
            if item.published_datetime() and item.published_datetime() >= cutoff
        ]
        if recent:
            return recent
        return items[:BOOTSTRAP_FALLBACK_LIMIT]
    if source == "x":
        return items[:X_BOOTSTRAP_LIMIT]
    return items


def trim_seen(items: list[UpdateItem]) -> list[str]:
    seen_values: list[str] = []
    for item in items[:MAX_SEEN_PER_SOURCE]:
        seen_values.append(item.identity)
    return seen_values


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.history_dir.mkdir(parents=True, exist_ok=True)


def load_state(paths: RuntimePaths) -> dict:
    if not paths.state_path.exists():
        return {"last_success_local_date": None, "seen": {}}
    return json.loads(paths.state_path.read_text(encoding="utf-8"))


def save_state(paths: RuntimePaths, state: dict) -> None:
    ensure_runtime_dirs(paths)
    paths.state_path.write_text(
        json.dumps(state, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def format_item_line(item: UpdateItem) -> str:
    parts = [f"- [{item.title}]({item.url})"]
    if item.published_at:
        local_date = parse_datetime(item.published_at).astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
        parts.append(f" ({local_date})")
    if item.summary:
        summary = item.summary
        if len(summary) > 240:
            summary = summary[:237].rstrip() + "..."
        parts.append(f": {summary}")
    return "".join(parts)


def render_report(
    *,
    updates_by_source: dict[str, list[UpdateItem]],
    local_now: datetime,
    forced: bool,
) -> str:
    mode = "forced snapshot" if forced else "scheduled update"
    lines = [
        "# Cursor updates",
        "",
        f"- Generated at: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Mode: {mode}",
        "",
    ]
    labels = {
        "changelog": "Changelog",
        "blog": "Blog",
        "x": "Official X",
    }
    for source in ("changelog", "blog", "x"):
        lines.append(f"## {labels[source]}")
        items = updates_by_source.get(source, [])
        if not items:
            lines.append("- No new items.")
        else:
            lines.extend(format_item_line(item) for item in items)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_report(paths: RuntimePaths, report_text: str, *, local_now: datetime, save_history: bool) -> None:
    ensure_runtime_dirs(paths)
    paths.latest_report_path.write_text(report_text, encoding="utf-8")
    paths.summary_path.write_text(report_text, encoding="utf-8")
    if save_history:
        history_path = paths.history_dir / f"{local_now.strftime('%Y-%m-%d')}.md"
        history_path.write_text(report_text, encoding="utf-8")


def collect_updates(
    *,
    fetcher: Callable[[str], str],
) -> dict[str, list[UpdateItem]]:
    changelog_items = parse_changelog_items(fetcher(CHANGELOG_RSS_URL))
    blog_index_items = parse_blog_index_items(fetcher(BLOG_INDEX_URL))
    blog_sitemap_items = parse_blog_sitemap_items(fetcher(BLOG_SITEMAP_URL))
    x_errors: list[Exception] = []
    x_items: list[UpdateItem] = []
    for x_url in X_MIRROR_URLS:
        try:
            x_items = parse_x_items(fetcher(x_url))
        except Exception as error:  # pragma: no cover - exercised through integration behavior
            x_errors.append(error)
            continue
        if x_items:
            break
    if not x_items and x_errors:
        raise x_errors[-1]
    return {
        "changelog": changelog_items,
        "blog": merge_blog_items(blog_index_items, blog_sitemap_items),
        "x": x_items,
    }


def run(
    *,
    now: datetime | None = None,
    force: bool = False,
    base_dir: Path | None = None,
    fetcher: Callable[[str], str] = fetch_text,
) -> dict:
    current_utc = now.astimezone(UTC) if now else now_utc()
    local_now = current_utc.astimezone(LOCAL_TZ)
    paths = RuntimePaths(base_dir=base_dir or Path(__file__).resolve().parent)
    state = load_state(paths)
    today = local_now.strftime("%Y-%m-%d")

    if not force:
        if local_now.hour != 9:
            return {
                "status": "skipped",
                "reason": "outside_scheduled_hour",
                "local_time": local_now.isoformat(),
            }
        if state.get("last_success_local_date") == today:
            return {
                "status": "skipped",
                "reason": "already_ran_today",
                "local_time": local_now.isoformat(),
            }

    collected = collect_updates(fetcher=fetcher)
    seen = state.get("seen", {})
    selected: dict[str, list[UpdateItem]] = {}
    for source, items in collected.items():
        selected[source] = select_updates(
            items,
            source=source,
            seen_identities=set(seen.get(source, [])),
            reference_now=current_utc,
        )

    report_text = render_report(updates_by_source=selected, local_now=local_now, forced=force)
    write_report(paths, report_text, local_now=local_now, save_history=not force)

    if not force:
        state = {
            "last_success_local_date": today,
            "seen": {source: trim_seen(items) for source, items in collected.items()},
        }
        save_state(paths, state)

    return {
        "status": "ok",
        "forced": force,
        "local_time": local_now.isoformat(),
        "report_path": str(paths.summary_path),
        "report": report_text,
        "updates": {source: [asdict(item) for item in items] for source, items in selected.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check official Cursor changelog, blog, and X updates.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately without consuming the once-per-day 09:00 scheduled slot.",
    )
    args = parser.parse_args(argv)

    try:
        result = run(force=args.force)
    except Exception as error:
        print(f"cursor_updates_watch failed: {error}", file=sys.stderr)
        return 1

    if result["status"] == "skipped":
        print(f"Skipped: {result['reason']} at {result['local_time']}")
        return 0

    print(f"Wrote report to {result['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
