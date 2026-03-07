#!/usr/bin/env python3
"""Track Cursor changelog, blog, and official X updates.

The watcher is intended for an hourly automation trigger. By default it only
performs a live fetch during the 09:00 hour in Asia/Shanghai and only records
one successful scheduled run per local date. Use --force to bypass the time
gate for verification runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import textwrap
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_TARGET_HOUR = 9
DEFAULT_STATE_DIR = Path(".cursor_updates")
DEFAULT_OUTPUT_PATH = Path("cursor_updates.md")
SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
JINA_HTTP_PREFIX = "https://r.jina.ai/http://"
X_CANDIDATE_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://www.x.com/cursor_ai",
)
SOURCE_LIMIT = 5
FIRST_RUN_X_LIMIT = 8
X_POST_LIMIT = 10
HTTP_TIMEOUT_SECONDS = 30
HTTP_RETRIES = 4
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/plain,text/html,application/xhtml+xml,"
        "application/xml;q=0.9,*/*;q=0.8"
    ),
}
FOOTER_SENTINELS = {
    "### Product",
    "### Resources",
    "### Company",
    "### Legal",
    "### Connect",
}


class FetchError(RuntimeError):
    """Raised when all fetch attempts for a source fail."""


@dataclass(frozen=True)
class SitemapEntry:
    """A single entry from Cursor's sitemap."""

    url: str
    lastmod: str


@dataclass(frozen=True)
class UpdateItem:
    """Normalized blog/changelog entry used in reports."""

    title: str
    url: str
    published: str
    summary: str


def parse_args(argv: list[str]) -> argparse.Namespace:
    """Parse CLI arguments."""

    parser = argparse.ArgumentParser(
        description="Check Cursor changelog, blog, and official X updates."
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone for the daily gate (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=DEFAULT_TARGET_HOUR,
        help=f"Local hour to allow scheduled fetches (default: {DEFAULT_TARGET_HOUR}).",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Runtime state directory (default: {DEFAULT_STATE_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Markdown report output path (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the scheduled gate for verification runs.",
    )
    return parser.parse_args(argv)


def resolve_path(workspace: Path, path: Path) -> Path:
    """Resolve relative runtime paths against the workspace."""

    if path.is_absolute():
        return path
    return workspace / path


def request_text(
    url: str,
    timeout: int = HTTP_TIMEOUT_SECONDS,
    retries: int = HTTP_RETRIES,
    sleep_func: Callable[[float], None] = time.sleep,
) -> str:
    """Fetch text with retries for transient failures."""

    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=HTTP_HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            last_error = error
            transient = error.code in {403, 429, 500, 502, 503, 504}
            if not transient or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError) as error:
            last_error = error
            if attempt == retries - 1:
                raise
        sleep_func(2**attempt)
    assert last_error is not None
    raise last_error


def fetch_via_jina(url: str, fetch_text: Callable[[str], str]) -> str:
    """Fetch a URL through the jina.ai public text mirror."""

    stripped = url.removeprefix("https://").removeprefix("http://")
    return fetch_text(f"{JINA_HTTP_PREFIX}{stripped}")


def parse_known_datetime(value: str) -> datetime | None:
    """Parse ISO timestamps and RFC 2822-style dates."""

    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_space(text: str) -> str:
    """Collapse repeated whitespace."""

    return re.sub(r"\s+", " ", text).strip()


def shorten(text: str, max_chars: int = 420) -> str:
    """Trim long summaries without breaking markdown too aggressively."""

    normalized = normalize_space(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def markdown_body(text: str) -> str:
    """Extract the markdown body from a jina mirror response."""

    marker = "Markdown Content:"
    if marker not in text:
        return text.strip()
    return text.split(marker, 1)[1].strip()


def parse_header_value(text: str, prefix: str) -> str | None:
    """Read a simple header value before the markdown body begins."""

    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
        if line == "Markdown Content:":
            break
    return None


def parse_sitemap(xml_text: str) -> dict[str, list[SitemapEntry]]:
    """Split Cursor's sitemap into blog and changelog entries."""

    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    blog_entries: list[SitemapEntry] = []
    changelog_entries: list[SitemapEntry] = []
    for url_node in root.findall("sm:url", namespace):
        loc = url_node.findtext("sm:loc", default="", namespaces=namespace).strip()
        lastmod = url_node.findtext(
            "sm:lastmod", default="", namespaces=namespace
        ).strip()
        if not loc or not lastmod:
            continue
        if loc.startswith("https://cursor.com/blog/"):
            blog_entries.append(SitemapEntry(url=loc, lastmod=lastmod))
        elif loc.startswith("https://cursor.com/changelog/"):
            changelog_entries.append(SitemapEntry(url=loc, lastmod=lastmod))
    blog_entries.sort(
        key=lambda item: parse_known_datetime(item.lastmod)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    changelog_entries.sort(
        key=lambda item: parse_known_datetime(item.lastmod)
        or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return {"blog": blog_entries, "changelog": changelog_entries}


def paragraph_blocks(text: str) -> list[str]:
    """Split markdown-ish text into paragraph blocks."""

    blocks = []
    for block in re.split(r"\n\s*\n", text):
        cleaned = block.strip()
        if cleaned:
            blocks.append(cleaned)
    return blocks


def clean_summary_block(block: str) -> str:
    """Drop obvious navigational and media-only lines from a block."""

    lines: list[str] = []
    for raw_line in block.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("[]("):
            continue
        if line.startswith("### "):
            continue
        if line.startswith("![Image "):
            continue
        if line.startswith("[![Image "):
            continue
        if re.fullmatch(r"-{3,}|={3,}", line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def parse_blog_update(
    page_text: str, fallback_url: str, fallback_published: str
) -> UpdateItem:
    """Parse a Cursor blog entry from jina mirror output."""

    title = parse_header_value(page_text, "Title: ") or fallback_url.rsplit("/", 1)[-1]
    published = parse_header_value(page_text, "Published Time: ") or fallback_published
    body = markdown_body(page_text)
    summary_blocks = []
    for block in paragraph_blocks(body):
        cleaned = clean_summary_block(block)
        if not cleaned:
            continue
        summary_blocks.append(cleaned)
        if len(summary_blocks) >= 2:
            break
    summary = shorten(" ".join(summary_blocks))
    return UpdateItem(title=title, url=fallback_url, published=published, summary=summary)


def extract_changelog_date(lines: list[str], fallback_published: str) -> str:
    """Handle normal and version-prefixed changelog dates."""

    date_pattern = re.compile(
        r"^(?:[\d.]+\s+)?([A-Z][a-z]{2} \d{1,2}, \d{4}) · \[Changelog\]"
    )
    for line in lines:
        match = date_pattern.match(line.strip())
        if match:
            return match.group(1)
    return fallback_published


def parse_changelog_update(
    page_text: str, fallback_url: str, fallback_published: str
) -> UpdateItem:
    """Parse a Cursor changelog page from jina mirror output."""

    raw_title = parse_header_value(page_text, "Title: ") or fallback_url.rsplit("/", 1)[-1]
    title = raw_title.removesuffix(" · Cursor")
    body = markdown_body(page_text)
    lines = body.splitlines()
    published = extract_changelog_date(lines, fallback_published)

    title_indexes = [index for index, line in enumerate(lines) if line.strip() == title]
    start_index = title_indexes[-1] + 1 if title_indexes else 0

    content_lines: list[str] = []
    for raw_line in lines[start_index:]:
        line = raw_line.strip()
        if not line:
            content_lines.append("")
            continue
        if line.startswith("[Next post"):
            break
        if line in FOOTER_SENTINELS:
            break
        if line.startswith("[Skip to content]"):
            continue
        if line.startswith("[Cursor]("):
            continue
        if line.startswith("[Sign in]"):
            continue
        if line.startswith("[Changelog]("):
            continue
        if line.startswith("© "):
            break
        if line.startswith("🌐"):
            break
        if re.fullmatch(r"=+|-+", line):
            continue
        content_lines.append(line)

    summary_blocks = []
    for block in paragraph_blocks("\n".join(content_lines)):
        cleaned = clean_summary_block(block)
        if not cleaned:
            continue
        if cleaned == title:
            continue
        summary_blocks.append(cleaned)
        if len(summary_blocks) >= 2:
            break

    summary = shorten(" ".join(summary_blocks))
    return UpdateItem(title=title, url=fallback_url, published=published, summary=summary)


def clean_x_post(lines: Iterable[str]) -> str:
    """Remove timeline chrome and media-only lines from an X post block."""

    filtered: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line in {
            "Cursor",
            "@cursor_ai",
            "Pinned",
            "Cursor's posts",
            "Cursor’s posts",
            "--------------",
            "The best way to code with AI.",
        }:
            continue
        if line.startswith("![Image "):
            continue
        if line.startswith("[![Image "):
            continue
        if re.fullmatch(r"\d+:\d+(?::\d+)?", line):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()


def parse_x_posts(page_text: str, limit: int = X_POST_LIMIT) -> list[str]:
    """Extract distinct post texts from the public X mirror output."""

    body = markdown_body(page_text)
    lines = body.splitlines()
    posts: list[str] = []
    current: list[str] = []
    seen_header = False

    for raw_line in lines:
        line = raw_line.strip()
        if not seen_header:
            if line in {"Pinned", "Cursor's posts", "Cursor’s posts"}:
                seen_header = True
            continue
        if line.startswith("[![Image ") and "Square profile picture" in line:
            if current:
                posts.append(clean_x_post(current))
                current = []
            continue
        current.append(line)
    if current:
        posts.append(clean_x_post(current))

    unique_posts: list[str] = []
    seen_hashes: set[str] = set()
    for post in posts:
        normalized = normalize_space(post)
        if not normalized:
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        if digest in seen_hashes:
            continue
        seen_hashes.add(digest)
        unique_posts.append(post)
        if len(unique_posts) >= limit:
            break
    return unique_posts


def parse_x_published_time(page_text: str) -> str | None:
    """Read the Published Time header from the X mirror response."""

    return parse_header_value(page_text, "Published Time: ")


def ensure_runtime_paths(workspace: Path, state_dir: Path) -> tuple[Path, Path, Path, Path]:
    """Create runtime directories and return resolved paths."""

    resolved_state_dir = resolve_path(workspace, state_dir)
    history_dir = resolved_state_dir / "history"
    latest_report_path = resolved_state_dir / "latest_report.md"
    resolved_state_dir.mkdir(parents=True, exist_ok=True)
    history_dir.mkdir(parents=True, exist_ok=True)
    return resolved_state_dir, resolved_state_dir / "state.json", history_dir, latest_report_path


def load_state(state_path: Path) -> dict:
    """Load runtime state if it exists."""

    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict) -> None:
    """Persist runtime state."""

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def diff_sitemap_entries(
    current_entries: list[SitemapEntry], previous_entries: dict[str, str]
) -> list[SitemapEntry]:
    """Return entries whose lastmod changed or which are new."""

    return [
        entry
        for entry in current_entries
        if previous_entries.get(entry.url) != entry.lastmod
    ]


def hash_post(text: str) -> str:
    """Stable hash used for X post deduplication between runs."""

    return hashlib.sha256(normalize_space(text).encode("utf-8")).hexdigest()


def fetch_source_updates(
    entries: list[SitemapEntry],
    parser: Callable[[str, str, str], UpdateItem],
    fetch_text: Callable[[str], str],
) -> list[UpdateItem]:
    """Fetch and parse a set of sitemap entries."""

    updates: list[UpdateItem] = []
    for entry in entries:
        page_text = fetch_via_jina(entry.url, fetch_text)
        updates.append(parser(page_text, entry.url, entry.lastmod))
    return updates


def fetch_x_timeline(fetch_text: Callable[[str], str]) -> tuple[str, list[str]]:
    """Try each configured public X mirror until one succeeds."""

    errors: list[str] = []
    for candidate_url in X_CANDIDATE_URLS:
        try:
            page_text = fetch_text(candidate_url)
            published_time = parse_x_published_time(page_text) or "unknown"
            return published_time, parse_x_posts(page_text)
        except urllib.error.URLError as exc:
            errors.append(f"{candidate_url}: {exc}")
        except TimeoutError as exc:
            errors.append(f"{candidate_url}: {exc}")
    raise FetchError(" ; ".join(errors))


def local_now(timezone_name: str) -> datetime:
    """Get the current local time for a timezone name."""

    return datetime.now(tz=ZoneInfo(timezone_name))


def should_run_scheduled(
    now: datetime, target_hour: int, last_scheduled_date: str | None, force: bool
) -> tuple[bool, str]:
    """Decide whether a scheduled run should execute."""

    if force:
        return True, "forced run"
    if now.hour != target_hour:
        return (
            False,
            f"outside {target_hour:02d}:00 local window "
            f"(current local time: {now.strftime('%Y-%m-%d %H:%M')})",
        )
    local_date = now.date().isoformat()
    if last_scheduled_date == local_date:
        return False, f"scheduled digest for {local_date} already ran"
    return True, "scheduled run"


def format_update_items(items: list[UpdateItem], empty_message: str) -> str:
    """Render blog/changelog entries as markdown."""

    if not items:
        return f"- {empty_message}"
    rendered = []
    for index, item in enumerate(items, start=1):
        rendered.append(
            textwrap.dedent(
                f"""\
                {index}. [{item.title}]({item.url})
                   - Published: {item.published}
                   - Summary: {item.summary}
                """
            ).rstrip()
        )
    return "\n".join(rendered)


def format_x_posts(posts: list[str], empty_message: str) -> str:
    """Render X post snippets as markdown."""

    if not posts:
        return f"- {empty_message}"
    return "\n".join(
        f'{index}. "{shorten(post, max_chars=320)}"'
        for index, post in enumerate(posts, start=1)
    )


def build_report(
    *,
    now: datetime,
    mode: str,
    latest_changelog: list[UpdateItem],
    latest_blog: list[UpdateItem],
    latest_x_posts: list[str],
    new_changelog: list[UpdateItem],
    new_blog: list[UpdateItem],
    new_x_posts: list[str],
    x_published_time: str,
    x_source_url: str,
    warnings: list[str],
) -> str:
    """Build the markdown report."""

    sections = [
        "# Cursor updates report",
        "",
        f"- Checked at: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Run mode: {mode}",
        f"- Sources: changelog, blog, @cursor_ai via {x_source_url}",
        f"- X mirror published time: {x_published_time}",
        "",
        "## New since previous snapshot",
        "",
        "### Changelog",
        format_update_items(new_changelog, "No newly changed changelog pages detected."),
        "",
        "### Blog",
        format_update_items(new_blog, "No newly changed blog pages detected."),
        "",
        "### Official X posts",
        format_x_posts(new_x_posts, "No newly visible official X posts detected."),
        "",
        "## Latest changelog snapshot",
        format_update_items(
            latest_changelog,
            "No changelog entries could be fetched for the latest snapshot.",
        ),
        "",
        "## Latest blog snapshot",
        format_update_items(
            latest_blog,
            "No blog entries could be fetched for the latest snapshot.",
        ),
        "",
        "## Latest official X snapshot",
        format_x_posts(
            latest_x_posts,
            "No official X posts could be fetched for the latest snapshot.",
        ),
    ]
    if warnings:
        sections.extend(
            [
                "",
                "## Warnings",
                *[f"- {warning}" for warning in warnings],
            ]
        )
    return "\n".join(sections).strip() + "\n"


def run(
    *,
    force: bool = False,
    timezone_name: str = DEFAULT_TIMEZONE,
    target_hour: int = DEFAULT_TARGET_HOUR,
    workspace: Path | None = None,
    state_dir: Path = DEFAULT_STATE_DIR,
    output_path: Path = DEFAULT_OUTPUT_PATH,
    fetch_text: Callable[[str], str] = request_text,
) -> dict:
    """Run the watcher once."""

    workspace = workspace or Path(__file__).resolve().parent
    state_dir_path, state_path, history_dir, latest_report_path = ensure_runtime_paths(
        workspace, state_dir
    )
    resolved_output_path = resolve_path(workspace, output_path)
    resolved_output_path.parent.mkdir(parents=True, exist_ok=True)

    state = load_state(state_path)
    last_scheduled_date = state.get("last_scheduled_local_date")
    now = local_now(timezone_name)

    allowed, reason = should_run_scheduled(now, target_hour, last_scheduled_date, force)
    if not allowed:
        return {
            "status": "skipped",
            "reason": reason,
            "checked_at": now.isoformat(),
        }

    warnings: list[str] = []
    previous_snapshot = state.get("snapshot", {})
    previous_blog = previous_snapshot.get("blog", {})
    previous_changelog = previous_snapshot.get("changelog", {})
    previous_x_hashes = set(previous_snapshot.get("x_posts", []))

    source_successes = 0
    latest_changelog: list[UpdateItem] = []
    latest_blog: list[UpdateItem] = []
    new_changelog: list[UpdateItem] = []
    new_blog: list[UpdateItem] = []
    latest_x_posts: list[str] = []
    new_x_posts: list[str] = []
    x_published_time = "unknown"
    x_source_url = X_CANDIDATE_URLS[0]

    current_snapshot = {"blog": {}, "changelog": {}, "x_posts": []}

    try:
        sitemap = parse_sitemap(fetch_text(SITEMAP_URL))
        blog_entries = sitemap["blog"]
        changelog_entries = sitemap["changelog"]
        current_snapshot["blog"] = {entry.url: entry.lastmod for entry in blog_entries}
        current_snapshot["changelog"] = {
            entry.url: entry.lastmod for entry in changelog_entries
        }

        first_run = not previous_snapshot
        latest_blog_entries = blog_entries[:SOURCE_LIMIT]
        latest_changelog_entries = changelog_entries[:SOURCE_LIMIT]
        changed_blog_entries = (
            latest_blog_entries
            if first_run
            else diff_sitemap_entries(blog_entries, previous_blog)
        )
        changed_changelog_entries = (
            latest_changelog_entries
            if first_run
            else diff_sitemap_entries(changelog_entries, previous_changelog)
        )

        latest_blog = fetch_source_updates(latest_blog_entries, parse_blog_update, fetch_text)
        latest_changelog = fetch_source_updates(
            latest_changelog_entries, parse_changelog_update, fetch_text
        )

        changed_blog_urls = {entry.url for entry in changed_blog_entries}
        changed_changelog_urls = {entry.url for entry in changed_changelog_entries}
        new_blog = [item for item in latest_blog if item.url in changed_blog_urls]
        new_changelog = [
            item for item in latest_changelog if item.url in changed_changelog_urls
        ]
        source_successes += 2
    except Exception as exc:  # pylint: disable=broad-except
        warnings.append(f"sitemap/blog/changelog fetch failed: {exc}")

    x_errors: list[str] = []
    for candidate_url in X_CANDIDATE_URLS:
        try:
            page_text = fetch_text(candidate_url)
            latest_x_posts = parse_x_posts(page_text)
            x_published_time = parse_x_published_time(page_text) or "unknown"
            x_source_url = candidate_url
            current_snapshot["x_posts"] = [hash_post(post) for post in latest_x_posts]
            if previous_x_hashes:
                new_x_posts = [
                    post
                    for post in latest_x_posts
                    if hash_post(post) not in previous_x_hashes
                ]
            else:
                new_x_posts = latest_x_posts[:FIRST_RUN_X_LIMIT]
            source_successes += 1
            break
        except Exception as exc:  # pylint: disable=broad-except
            x_errors.append(f"{candidate_url}: {exc}")
    if x_errors and not latest_x_posts:
        warnings.append("official X fetch failed: " + " ; ".join(x_errors))

    if source_successes == 0:
        raise FetchError("All sources failed: " + "; ".join(warnings))

    mode = "forced verification" if force else "scheduled"
    report = build_report(
        now=now,
        mode=mode,
        latest_changelog=latest_changelog,
        latest_blog=latest_blog,
        latest_x_posts=latest_x_posts,
        new_changelog=new_changelog,
        new_blog=new_blog,
        new_x_posts=new_x_posts,
        x_published_time=x_published_time,
        x_source_url=x_source_url,
        warnings=warnings,
    )

    latest_report_path.write_text(report, encoding="utf-8")
    history_path = history_dir / f"{now.date().isoformat()}.md"
    history_path.write_text(report, encoding="utf-8")
    resolved_output_path.write_text(report, encoding="utf-8")

    new_state = {
        "last_checked_at": now.isoformat(),
        "snapshot": current_snapshot,
        "target_hour": target_hour,
        "timezone": timezone_name,
    }
    if not force:
        new_state["last_scheduled_local_date"] = now.date().isoformat()
    elif last_scheduled_date:
        new_state["last_scheduled_local_date"] = last_scheduled_date
    save_state(state_path, new_state)

    return {
        "status": "fetched",
        "reason": reason,
        "checked_at": now.isoformat(),
        "latest_report_path": str(latest_report_path),
        "history_path": str(history_path),
        "output_path": str(resolved_output_path),
        "state_dir": str(state_dir_path),
        "new_counts": {
            "changelog": len(new_changelog),
            "blog": len(new_blog),
            "x_posts": len(new_x_posts),
        },
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    """CLI entry point."""

    args = parse_args(argv or sys.argv[1:])
    try:
        result = run(
            force=args.force,
            timezone_name=args.timezone,
            target_hour=args.hour,
            state_dir=args.state_dir,
            output_path=args.output,
        )
    except Exception as error:  # pragma: no cover
        print(f"cursor updates watcher failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
