#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parent
STATE_DIR = REPO_ROOT / ".cursor_updates"
STATE_FILE = STATE_DIR / "state.json"
LATEST_REPORT_FILE = STATE_DIR / "latest_report.md"
HISTORY_DIR = STATE_DIR / "history"
PUBLIC_REPORT_FILE = REPO_ROOT / "cursor_updates.md"

CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_INDEX_URL = "https://cursor.com/en/blog"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)

TARGET_TIMEZONE = (
    ZoneInfo("Asia/Shanghai")
    if ZoneInfo is not None
    else timezone(timedelta(hours=8), name="Asia/Shanghai")
)
TARGET_HOUR = 9
DEFAULT_LIMIT = 5
DEFAULT_BLOG_CANDIDATE_LIMIT = 12
TRANSIENT_STATUS_CODES = {403, 429, 500, 502, 503, 504}

CHANGELOG_ENTRY_RE = re.compile(
    r'<a[^>]+href="(?P<href>/changelog/[^"]+)"[^>]*>\s*'
    r'(?:<span class="label">[^<]+</span>\s*<span>[^<]*</span>\s*)?'
    r'<time[^>]+dateTime="(?P<published_at>[^"]+)"[^>]*>.*?</time>\s*</a>'
    r'.*?<header[^>]*>\s*<h1[^>]*>.*?<a[^>]+href="(?P=href)">(?P<title>.*?)</a>.*?</h1>.*?</header>'
    r'.*?<div class="prose prose--block">\s*<p>(?P<summary>.*?)</p>',
    re.DOTALL,
)
SCRIPT_JSON_RE = re.compile(
    r'<script type="application/ld\+json">(.*?)</script>',
    re.DOTALL,
)
HTML_TAG_RE = re.compile(r"<[^>]+>")
PROFILE_IMAGE_RE = re.compile(r"^\[!\[Image \d+: Square profile picture")
DURATION_RE = re.compile(r"^\d+:\d+$")
IMAGE_LINE_RE = re.compile(r"^!\[Image \d+")
BLOG_URL_RE = re.compile(r"^https://cursor\.com/blog/[^/]+$")


@dataclass(frozen=True)
class UpdateItem:
    title: str
    url: str
    published_at: str
    summary: str


@dataclass(frozen=True)
class XPost:
    text: str

    @property
    def key(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class SourceError:
    source: str
    message: str


@dataclass
class ReportData:
    changelog: list[UpdateItem] = field(default_factory=list)
    blog: list[UpdateItem] = field(default_factory=list)
    x_posts: list[XPost] = field(default_factory=list)
    errors: list[SourceError] = field(default_factory=list)


@dataclass
class RunResult:
    status: str
    reason: str
    report_path: Path | None = None
    history_path: Path | None = None
    report_markdown: str = ""


def clean_html_text(value: str) -> str:
    value = html.unescape(value)
    value = HTML_TAG_RE.sub(" ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def format_display_date(value: str) -> str:
    try:
        return parse_iso_datetime(value).astimezone(TARGET_TIMEZONE).strftime("%Y-%m-%d")
    except ValueError:
        return value


def default_state() -> dict[str, Any]:
    return {
        "last_successful_scheduled_date": None,
        "seen": {
            "changelog": [],
            "blog": [],
            "x": [],
        },
    }


def load_state(state_file: Path = STATE_FILE) -> dict[str, Any]:
    if not state_file.exists():
        return default_state()
    try:
        data = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()

    state = default_state()
    if isinstance(data, dict):
        state["last_successful_scheduled_date"] = data.get("last_successful_scheduled_date")
        seen = data.get("seen", {})
        if isinstance(seen, dict):
            for key in ("changelog", "blog", "x"):
                values = seen.get(key, [])
                if isinstance(values, list):
                    state["seen"][key] = [str(item) for item in values]
    return state


def save_state(state: dict[str, Any], state_file: Path = STATE_FILE) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    state_file.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


class Fetcher:
    def __init__(
        self,
        timeout: int = 30,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        user_agent: str = "Mozilla/5.0 (CursorUpdatesWatcher)",
    ) -> None:
        self.timeout = timeout
        self.retries = retries
        self.backoff_seconds = backoff_seconds
        self.user_agent = user_agent

    def fetch_text(self, url: str) -> str:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": self.user_agent,
                        "Accept-Language": "en-US,en;q=0.9",
                    },
                )
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return response.read().decode("utf-8", errors="replace")
            except urllib.error.HTTPError as error:
                last_error = error
                if error.code not in TRANSIENT_STATUS_CODES or attempt + 1 == self.retries:
                    break
            except (urllib.error.URLError, TimeoutError) as error:
                last_error = error
                if attempt + 1 == self.retries:
                    break

            sleep_seconds = self.backoff_seconds * (2**attempt)
            time.sleep(sleep_seconds)

        if last_error is None:
            raise RuntimeError(f"Failed to fetch {url}")
        raise RuntimeError(f"Failed to fetch {url}: {last_error}") from last_error


def parse_changelog_html(html_text: str, limit: int = DEFAULT_LIMIT) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    seen_urls: set[str] = set()
    for match in CHANGELOG_ENTRY_RE.finditer(html_text):
        href = match.group("href")
        url = f"https://cursor.com{href}" if href.startswith("/") else href
        if url in seen_urls:
            continue
        seen_urls.add(url)
        items.append(
            UpdateItem(
                title=clean_html_text(match.group("title")),
                url=url,
                published_at=match.group("published_at"),
                summary=clean_html_text(match.group("summary")),
            )
        )
        if len(items) >= limit:
            break
    return items


def fetch_changelog_updates(fetcher: Fetcher, limit: int = DEFAULT_LIMIT) -> list[UpdateItem]:
    return parse_changelog_html(fetcher.fetch_text(CHANGELOG_URL), limit=limit)


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, str]]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries: list[tuple[str, str]] = []
    for node in root.findall("sm:url", namespace):
        loc = (node.findtext("sm:loc", default="", namespaces=namespace) or "").strip()
        lastmod = (node.findtext("sm:lastmod", default="", namespaces=namespace) or "").strip()
        if BLOG_URL_RE.match(loc):
            entries.append((loc, lastmod))
    entries.sort(
        key=lambda item: parse_iso_datetime(item[1]) if item[1] else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return entries


def parse_blog_article_html(html_text: str, fallback_url: str) -> UpdateItem | None:
    for raw_json in SCRIPT_JSON_RE.findall(html_text):
        try:
            data = json.loads(html.unescape(raw_json))
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        item_type = data.get("@type")
        types = item_type if isinstance(item_type, list) else [item_type]
        if "BlogPosting" not in types:
            continue
        headline = str(data.get("headline", "")).strip()
        published_at = str(data.get("datePublished", "")).strip()
        summary = str(data.get("description", "")).strip()
        url = str(data.get("url") or fallback_url).strip()
        if headline and published_at and url:
            return UpdateItem(
                title=headline,
                url=url,
                published_at=published_at,
                summary=summary,
            )
    return None


def fetch_blog_updates(
    fetcher: Fetcher,
    limit: int = DEFAULT_LIMIT,
    candidate_limit: int = DEFAULT_BLOG_CANDIDATE_LIMIT,
) -> list[UpdateItem]:
    sitemap_entries = parse_blog_sitemap(fetcher.fetch_text(BLOG_SITEMAP_URL))
    items: list[UpdateItem] = []
    for url, _lastmod in sitemap_entries[:candidate_limit]:
        item = parse_blog_article_html(fetcher.fetch_text(url), fallback_url=url)
        if item is not None:
            items.append(item)
        if len(items) >= limit:
            break
    return items


def parse_x_feed_markdown(markdown_text: str, limit: int = DEFAULT_LIMIT) -> list[XPost]:
    lines = [line.rstrip() for line in markdown_text.splitlines()]
    try:
        content_start = lines.index("Markdown Content:") + 1
    except ValueError:
        content_start = 0

    blocks: list[list[str]] = []
    current: list[str] = []
    in_posts = False
    for raw_line in lines[content_start:]:
        line = raw_line.strip()
        if line == "Cursor’s posts":
            in_posts = True
            current = []
            continue
        if not in_posts:
            continue
        if PROFILE_IMAGE_RE.match(line):
            if current:
                blocks.append(current)
            current = []
            continue
        current.append(line)
    if current:
        blocks.append(current)

    posts: list[XPost] = []
    seen_texts: set[str] = set()
    for block in blocks:
        filtered_lines = []
        for line in block:
            if not line:
                continue
            if line == "Pinned":
                continue
            if line.startswith("--------------"):
                continue
            if IMAGE_LINE_RE.match(line):
                continue
            if DURATION_RE.match(line):
                continue
            filtered_lines.append(line)

        if not filtered_lines:
            continue

        text = re.sub(r"\s+", " ", " ".join(filtered_lines)).strip()
        if not text or text in seen_texts:
            continue

        seen_texts.add(text)
        posts.append(XPost(text=text))
        if len(posts) >= limit:
            break

    return posts


def fetch_x_updates(fetcher: Fetcher, limit: int = DEFAULT_LIMIT) -> list[XPost]:
    last_error: Exception | None = None
    for url in X_MIRROR_URLS:
        try:
            posts = parse_x_feed_markdown(fetcher.fetch_text(url), limit=limit)
        except Exception as error:  # noqa: BLE001
            last_error = error
            continue
        if posts:
            return posts
    if last_error is None:
        raise RuntimeError("Failed to fetch Cursor X mirror")
    raise RuntimeError(f"Failed to fetch Cursor X mirror: {last_error}") from last_error


def collect_report_data(fetcher: Fetcher, limit: int = DEFAULT_LIMIT) -> ReportData:
    report = ReportData()

    try:
        report.changelog = fetch_changelog_updates(fetcher, limit=limit)
    except Exception as error:  # noqa: BLE001
        report.errors.append(SourceError("changelog", str(error)))

    try:
        report.blog = fetch_blog_updates(fetcher, limit=limit)
    except Exception as error:  # noqa: BLE001
        report.errors.append(SourceError("blog", str(error)))

    try:
        report.x_posts = fetch_x_updates(fetcher, limit=limit)
    except Exception as error:  # noqa: BLE001
        report.errors.append(SourceError("official_x", str(error)))

    return report


def update_keys(items: Iterable[UpdateItem]) -> list[str]:
    return [item.url for item in items]


def trim_seen(values: list[str], limit: int = 100) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
        if len(deduped) >= limit:
            break
    return deduped


def compute_new_updates(
    report: ReportData,
    state: dict[str, Any],
) -> tuple[list[UpdateItem], list[UpdateItem], list[XPost]]:
    seen = state.get("seen", {})
    seen_changelog = set(seen.get("changelog", []))
    seen_blog = set(seen.get("blog", []))
    seen_x = set(seen.get("x", []))

    new_changelog = [item for item in report.changelog if item.url not in seen_changelog]
    new_blog = [item for item in report.blog if item.url not in seen_blog]
    new_x = [post for post in report.x_posts if post.key not in seen_x]
    return new_changelog, new_blog, new_x


def render_update_items(items: Iterable[UpdateItem]) -> list[str]:
    lines = []
    for item in items:
        lines.append(
            f"- {format_display_date(item.published_at)} - "
            f"[{item.title}]({item.url}) - {item.summary}"
        )
    return lines


def render_x_posts(posts: Iterable[XPost]) -> list[str]:
    return [f"- {post.text}" for post in posts]


def render_report(
    report: ReportData,
    state: dict[str, Any],
    now_local: datetime,
    force: bool,
) -> str:
    new_changelog, new_blog, new_x = compute_new_updates(report, state)

    lines = [
        "# Cursor updates report",
        "",
        f"- Generated at: {now_local.isoformat()}",
        f"- Mode: {'forced' if force else 'scheduled'}",
        f"- Target schedule: daily at {TARGET_HOUR:02d}:00 {TARGET_TIMEZONE}",
        f"- Sources: {CHANGELOG_URL}, {BLOG_INDEX_URL}, {X_PROFILE_URL}",
        "",
        "## New since last successful scheduled check",
    ]

    if not new_changelog and not new_blog and not new_x:
        lines.append("- No unseen updates were detected in the fetched result set.")
    else:
        if new_changelog:
            lines.extend(["", "### Changelog", *render_update_items(new_changelog)])
        if new_blog:
            lines.extend(["", "### Blog", *render_update_items(new_blog)])
        if new_x:
            lines.extend(["", "### Official X", *render_x_posts(new_x)])

    lines.extend(["", "## Latest changelog entries"])
    lines.extend(render_update_items(report.changelog) or ["- No changelog entries fetched."])

    lines.extend(["", "## Latest blog posts"])
    lines.extend(render_update_items(report.blog) or ["- No blog posts fetched."])

    lines.extend(["", "## Latest official X posts"])
    lines.extend(render_x_posts(report.x_posts) or ["- No official X posts fetched."])

    if report.errors:
        lines.extend(["", "## Fetch errors"])
        for error in report.errors:
            lines.append(f"- {error.source}: {error.message}")

    return "\n".join(lines).rstrip() + "\n"


def should_run_scheduled(now_local: datetime, state: dict[str, Any]) -> tuple[bool, str]:
    if now_local.tzinfo is None:
        raise ValueError("now_local must be timezone-aware")

    if now_local.hour != TARGET_HOUR:
        return (
            False,
            f"Current Asia/Shanghai hour is {now_local.hour:02d}; only runs during the {TARGET_HOUR:02d}:00 hour.",
        )

    already_ran = state.get("last_successful_scheduled_date")
    today = now_local.date().isoformat()
    if already_ran == today:
        return False, f"A scheduled run already succeeded on {today}."

    return True, "Scheduled window is open."


def write_report_files(
    markdown: str,
    now_local: datetime,
    force: bool,
    latest_report_file: Path = LATEST_REPORT_FILE,
    public_report_file: Path = PUBLIC_REPORT_FILE,
    history_dir: Path = HISTORY_DIR,
) -> Path | None:
    latest_report_file.parent.mkdir(parents=True, exist_ok=True)
    latest_report_file.write_text(markdown, encoding="utf-8")
    public_report_file.write_text(markdown, encoding="utf-8")

    if force:
        return None

    history_dir.mkdir(parents=True, exist_ok=True)
    history_path = history_dir / f"{now_local.date().isoformat()}.md"
    history_path.write_text(markdown, encoding="utf-8")
    return history_path


def run_watch(
    *,
    fetcher: Fetcher | None = None,
    now: datetime | None = None,
    force: bool = False,
    limit: int = DEFAULT_LIMIT,
    state_file: Path = STATE_FILE,
    latest_report_file: Path = LATEST_REPORT_FILE,
    public_report_file: Path = PUBLIC_REPORT_FILE,
    history_dir: Path = HISTORY_DIR,
) -> RunResult:
    fetcher = fetcher or Fetcher()
    now_local = (now or datetime.now(TARGET_TIMEZONE)).astimezone(TARGET_TIMEZONE)
    state = load_state(state_file)

    if not force:
        should_run, reason = should_run_scheduled(now_local, state)
        if not should_run:
            return RunResult(status="skipped", reason=reason)

    report = collect_report_data(fetcher, limit=limit)
    markdown = render_report(report, state, now_local, force=force)
    history_path = write_report_files(
        markdown,
        now_local,
        force=force,
        latest_report_file=latest_report_file,
        public_report_file=public_report_file,
        history_dir=history_dir,
    )

    if report.errors:
        return RunResult(
            status="error",
            reason="One or more sources failed to fetch.",
            report_path=latest_report_file,
            history_path=history_path,
            report_markdown=markdown,
        )

    if not force:
        new_seen_changelog = update_keys(report.changelog) + state["seen"]["changelog"]
        new_seen_blog = update_keys(report.blog) + state["seen"]["blog"]
        new_seen_x = [post.key for post in report.x_posts] + state["seen"]["x"]
        state["seen"]["changelog"] = trim_seen(new_seen_changelog)
        state["seen"]["blog"] = trim_seen(new_seen_blog)
        state["seen"]["x"] = trim_seen(new_seen_x)
        state["last_successful_scheduled_date"] = now_local.date().isoformat()
        save_state(state, state_file=state_file)

    return RunResult(
        status="ran",
        reason="Report generated successfully.",
        report_path=latest_report_file,
        history_path=history_path,
        report_markdown=markdown,
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Check official Cursor changelog, blog, and @cursor_ai updates. "
            "Scheduled mode only runs during the 09:00 hour in Asia/Shanghai."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the time gate and generate a report without updating seen-state.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Number of latest items to keep per source.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_watch(force=args.force, limit=args.limit)
    if result.status == "skipped":
        print(result.reason)
        return 0

    if result.report_path is not None:
        print(f"Saved report to {result.report_path}")
    if result.history_path is not None:
        print(f"Saved history entry to {result.history_path}")
    print(result.report_markdown)

    if result.status == "error":
        print(result.reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
