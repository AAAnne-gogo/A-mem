#!/usr/bin/env python3
"""Track daily official Cursor updates from changelog, blog, and X."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
TARGET_TZ = ZoneInfo("Asia/Shanghai")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) CursorUpdatesBot/1.0"
)

CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_INDEX_URL = "https://cursor.com/en/blog"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
OFFICIAL_X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)


@dataclass(frozen=True)
class UpdateItem:
    source: str
    title: str
    url: str
    published_at: str | None
    summary: str | None
    item_id: str


@dataclass(frozen=True)
class WatcherPaths:
    state_dir: Path
    state_path: Path
    latest_report_path: Path
    history_dir: Path
    public_report_path: Path


@dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    report: str
    report_path: Path


def default_paths(root: Path = ROOT) -> WatcherPaths:
    state_dir = root / ".cursor_updates"
    return WatcherPaths(
        state_dir=state_dir,
        state_path=state_dir / "state.json",
        latest_report_path=state_dir / "latest_report.md",
        history_dir=state_dir / "history",
        public_report_path=root / "cursor_updates.md",
    )


def request_text(url: str, timeout: int = 30, retries: int = 3) -> str:
    """Fetch text with light retry logic for transient failures."""
    last_error: Exception | None = None
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,text/plain;q=0.8,*/*;q=0.7",
    }

    for attempt in range(retries):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                encoding = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(encoding, "replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {403, 429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc

        if attempt < retries - 1:
            time.sleep(2**attempt)

    raise RuntimeError(f"failed to fetch {url}: {last_error}")


def strip_tags(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def stable_item_id(source: str, title: str, url: str, published_at: str | None) -> str:
    payload = "||".join([source, title, url, published_at or ""])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]


def parse_changelog(html_text: str, limit: int = 5) -> list[UpdateItem]:
    pattern = re.compile(
        r'href="(?P<href>/changelog/[^"]+)"><time dateTime="(?P<published>[^"]+)"[^>]*>.*?</time>'
        r'.*?<h[1-6][^>]*>.*?<a[^>]+href="(?P=href)">(?P<title>[^<]+)</a>.*?</h[1-6]>'
        r'.*?<div class="prose prose--block"><p>(?P<summary>.*?)</p>',
        re.S,
    )
    items: list[UpdateItem] = []
    seen_ids: set[str] = set()

    for match in pattern.finditer(html_text):
        title = compact_text(match.group("title"))
        url = urllib.parse.urljoin(CHANGELOG_URL, match.group("href"))
        published_at = compact_text(match.group("published"))
        summary = strip_tags(match.group("summary"))
        item_id = stable_item_id("changelog", title, url, published_at)
        if item_id in seen_ids:
            continue
        seen_ids.add(item_id)
        items.append(
            UpdateItem(
                source="changelog",
                title=title,
                url=url,
                published_at=published_at,
                summary=summary,
                item_id=item_id,
            )
        )
        if len(items) >= limit:
            break

    if not items:
        raise ValueError("unable to parse changelog entries")

    return items


def parse_blog_sitemap(xml_text: str) -> list[str]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    urls: list[str] = []
    seen: set[str] = set()

    for node in root.findall("sm:url", namespace):
        loc = node.find("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if loc is None or not loc.text:
            continue
        parsed = urllib.parse.urlparse(loc.text)
        if parsed.netloc != "cursor.com" or not parsed.path.startswith("/blog/"):
            continue
        if loc.text in seen:
            continue
        seen.add(loc.text)
        urls.append(loc.text)

    if not urls:
        raise ValueError("blog sitemap did not contain blog URLs")

    return urls


def parse_blog_article(html_text: str, url: str) -> UpdateItem:
    scripts = re.findall(
        r'<script type="application/ld\+json">(.*?)</script>',
        html_text,
        re.S,
    )
    for script in scripts:
        payload = html.unescape(script)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if isinstance(data, list):
            candidates = data
        else:
            candidates = [data]

        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if candidate.get("@type") != "BlogPosting":
                continue
            title = compact_text(candidate.get("headline", ""))
            published_at = compact_text(candidate.get("datePublished", "")) or None
            summary = compact_text(candidate.get("description", "")) or None
            if title:
                return UpdateItem(
                    source="blog",
                    title=title,
                    url=url,
                    published_at=published_at,
                    summary=summary,
                    item_id=stable_item_id("blog", title, url, published_at),
                )

    title_match = re.search(r"<title>(.*?)</title>", html_text, re.S | re.I)
    if title_match:
        title = compact_text(title_match.group(1)).replace(" · Cursor", "")
        published_match = re.search(r'"datePublished":"([^"]+)"', html_text)
        description_match = re.search(r'"description":"([^"]+)"', html_text)
        return UpdateItem(
            source="blog",
            title=title,
            url=url,
            published_at=published_match.group(1) if published_match else None,
            summary=compact_text(description_match.group(1)) if description_match else None,
            item_id=stable_item_id(
                "blog",
                title,
                url,
                published_match.group(1) if published_match else None,
            ),
        )

    raise ValueError(f"unable to parse blog article metadata: {url}")


def parse_x_posts(markdown_text: str, limit: int = 5) -> list[UpdateItem]:
    section_marker = "Cursor’s posts"
    if section_marker not in markdown_text:
        section_marker = "Cursor's posts"
    if section_marker not in markdown_text:
        raise ValueError("unable to locate official X posts section")

    section = markdown_text.split(section_marker, 1)[1]
    lines = section.splitlines()
    items: list[UpdateItem] = []
    seen: set[str] = set()
    ignored_lines = {"Pinned", "Learn more:"}

    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line in ignored_lines:
            continue
        if line.startswith("[![Image") or line.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        if len(line) < 15:
            continue
        if line in seen:
            continue
        seen.add(line)
        items.append(
            UpdateItem(
                source="x",
                title=line,
                url=OFFICIAL_X_PROFILE_URL,
                published_at=None,
                summary="Newest-first profile snapshot from the official @cursor_ai X account.",
                item_id=stable_item_id("x", line, OFFICIAL_X_PROFILE_URL, None),
            )
        )
        if len(items) >= limit:
            break

    if not items:
        raise ValueError("unable to parse official X posts")

    return items


def fetch_blog_updates(limit: int) -> list[UpdateItem]:
    sitemap_xml = request_text(BLOG_SITEMAP_URL)
    urls = parse_blog_sitemap(sitemap_xml)
    items: list[UpdateItem] = []
    for url in urls:
        article_html = request_text(url)
        items.append(parse_blog_article(article_html, url))
        if len(items) >= limit:
            break
    return items


def fetch_x_updates(limit: int) -> list[UpdateItem]:
    last_error: Exception | None = None
    for url in X_MIRROR_URLS:
        try:
            markdown_text = request_text(url, timeout=60, retries=2)
            return parse_x_posts(markdown_text, limit=limit)
        except Exception as exc:  # pragma: no cover - exercised via higher-level tests
            last_error = exc
    raise RuntimeError(f"failed to fetch official X posts: {last_error}")


def collect_updates(limit: int = 5) -> tuple[dict[str, list[UpdateItem]], list[str]]:
    sources: dict[str, Callable[[], list[UpdateItem]]] = {
        "changelog": lambda: parse_changelog(request_text(CHANGELOG_URL), limit=limit),
        "blog": lambda: fetch_blog_updates(limit=limit),
        "x": lambda: fetch_x_updates(limit=limit),
    }

    collected: dict[str, list[UpdateItem]] = {}
    errors: list[str] = []

    for source, fetcher in sources.items():
        try:
            collected[source] = fetcher()
        except Exception as exc:
            errors.append(f"{source}: {exc}")
            collected[source] = []

    if not any(collected.values()):
        raise RuntimeError("all sources failed")

    return collected, errors


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "last_success_local_date": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def should_run(local_now: dt.datetime, state: dict, force: bool = False) -> tuple[bool, str]:
    local_date = local_now.date().isoformat()
    if force:
        return True, "forced run"
    if local_now.hour != 9:
        return False, f"scheduled run skipped at {local_now.strftime('%H:%M %Z')}; only runs during 09:00 hour"
    if state.get("last_success_local_date") == local_date:
        return False, f"scheduled run already completed on {local_date}"
    return True, "scheduled run"


def diff_new_items(items_by_source: dict[str, list[UpdateItem]], state: dict) -> dict[str, list[UpdateItem]]:
    seen = state.get("seen", {})
    new_items: dict[str, list[UpdateItem]] = {}
    for source, items in items_by_source.items():
        seen_ids = set(seen.get(source, []))
        new_items[source] = [item for item in items if item.item_id not in seen_ids]
    return new_items


def update_state_with_items(state: dict, items_by_source: dict[str, list[UpdateItem]], local_now: dt.datetime) -> dict:
    next_state = {
        "last_success_local_date": local_now.date().isoformat(),
        "seen": {},
    }
    previous_seen = state.get("seen", {})

    for source, items in items_by_source.items():
        merged = list(previous_seen.get(source, []))
        for item in items:
            if item.item_id not in merged:
                merged.append(item.item_id)
        next_state["seen"][source] = merged[-200:]

    return next_state


def format_items(items: list[UpdateItem], show_empty: str) -> list[str]:
    if not items:
        return [show_empty]

    lines: list[str] = []
    for item in items:
        suffix = f" ({item.published_at})" if item.published_at else ""
        lines.append(f"- [{item.title}]({item.url}){suffix}")
        if item.summary:
            lines.append(f"  - {item.summary}")
    return lines


def render_report(
    *,
    local_now: dt.datetime,
    mode: str,
    reason: str,
    items_by_source: dict[str, list[UpdateItem]],
    new_items: dict[str, list[UpdateItem]],
    errors: list[str],
) -> str:
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- Generated at: {local_now.isoformat()}",
        f"- Run mode: {mode}",
        f"- Reason: {reason}",
        "",
        "## Newly observed updates",
        "",
        "### Changelog",
        *format_items(new_items.get("changelog", []), "- No newly observed changelog entries."),
        "",
        "### Blog",
        *format_items(new_items.get("blog", []), "- No newly observed blog posts."),
        "",
        "### Official X (@cursor_ai)",
        *format_items(new_items.get("x", []), "- No newly observed official X posts."),
        "",
        "## Latest snapshot",
        "",
        "### Changelog",
        *format_items(items_by_source.get("changelog", []), "- Unable to fetch changelog snapshot."),
        "",
        "### Blog",
        *format_items(items_by_source.get("blog", []), "- Unable to fetch blog snapshot."),
        "",
        "### Official X (@cursor_ai)",
        *format_items(items_by_source.get("x", []), "- Unable to fetch official X snapshot."),
    ]

    if errors:
        lines.extend(
            [
                "",
                "## Fetch warnings",
                "",
                *[f"- {error}" for error in errors],
            ]
        )

    return "\n".join(lines).rstrip() + "\n"


def write_report(
    paths: WatcherPaths,
    local_now: dt.datetime,
    report: str,
    *,
    write_public: bool = True,
    write_history: bool = True,
) -> None:
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    paths.latest_report_path.write_text(report, encoding="utf-8")
    if write_public:
        paths.public_report_path.write_text(report, encoding="utf-8")
    if write_history:
        paths.history_dir.mkdir(parents=True, exist_ok=True)
        history_path = paths.history_dir / f"{local_now.date().isoformat()}.md"
        history_path.write_text(report, encoding="utf-8")


def run(
    *,
    force: bool = False,
    now: dt.datetime | None = None,
    limit: int = 5,
    paths: WatcherPaths | None = None,
) -> RunResult:
    paths = paths or default_paths()
    current_time = now or dt.datetime.now(dt.timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=dt.timezone.utc)
    local_now = current_time.astimezone(TARGET_TZ)

    state = load_state(paths.state_path)
    should_execute, reason = should_run(local_now, state, force=force)

    if not should_execute:
        report = render_report(
            local_now=local_now,
            mode="skipped",
            reason=reason,
            items_by_source={},
            new_items={},
            errors=[],
        )
        if not paths.latest_report_path.exists():
            write_report(
                paths,
                local_now,
                report,
                write_public=False,
                write_history=False,
            )
        return RunResult("skipped", reason, report, paths.latest_report_path)

    items_by_source, errors = collect_updates(limit=limit)
    new_items = diff_new_items(items_by_source, state)
    mode = "forced" if force else "scheduled"
    report = render_report(
        local_now=local_now,
        mode=mode,
        reason=reason,
        items_by_source=items_by_source,
        new_items=new_items,
        errors=errors,
    )
    write_report(paths, local_now, report)

    if not force:
        save_state(paths.state_path, update_state_with_items(state, items_by_source, local_now))

    return RunResult("success", "report written", report, paths.latest_report_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the 09:00 Asia/Shanghai schedule gate and state dedupe.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of items to keep per source in the report.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    result = run(force=args.force, limit=args.limit)
    print(result.report)
    return 0 if result.status in {"success", "skipped"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
