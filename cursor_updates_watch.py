#!/usr/bin/env python3
"""Daily watcher for Cursor changelog, blog, and official X posts.

This script is designed for an hourly cron automation. It only emits a daily
report at 09:00 in the configured timezone unless ``--force`` is used.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import textwrap
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
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
SOURCE_LIMIT_ON_FIRST_RUN = 5
X_POST_LIMIT_ON_FIRST_RUN = 8
HTTP_TIMEOUT_SECONDS = 30
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    )
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
    url: str
    lastmod: str


@dataclass(frozen=True)
class UpdateItem:
    title: str
    url: str
    published: str
    summary: str


def parse_args() -> argparse.Namespace:
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
        help=f"Local hour to run the scheduled digest (default: {DEFAULT_TARGET_HOUR}).",
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help=f"Directory for runtime state (default: {DEFAULT_STATE_DIR}).",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help=f"Markdown output path (default: {DEFAULT_OUTPUT_PATH}).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the time gate and duplicate scheduled-run guard.",
    )
    return parser.parse_args()


def fetch_text(url: str) -> str:
    request = urllib.request.Request(url, headers=HTTP_HEADERS)
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", "replace")


def fetch_via_jina(url: str) -> str:
    stripped = url.removeprefix("https://").removeprefix("http://")
    return fetch_text(f"{JINA_HTTP_PREFIX}{stripped}")


def parse_iso_timestamp(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def shorten(text: str, max_chars: int = 420) -> str:
    normalized = normalize_space(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 3].rstrip() + "..."


def normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def markdown_body(text: str) -> str:
    marker = "Markdown Content:"
    if marker not in text:
        return text.strip()
    return text.split(marker, 1)[1].strip()


def parse_header_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
        if line == "Markdown Content:":
            break
    return None


def parse_sitemap(xml_text: str) -> dict[str, list[SitemapEntry]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    blog_entries: list[SitemapEntry] = []
    changelog_entries: list[SitemapEntry] = []
    for url_node in root.findall("sm:url", namespace):
        loc = url_node.findtext("sm:loc", default="", namespaces=namespace).strip()
        lastmod = url_node.findtext("sm:lastmod", default="", namespaces=namespace).strip()
        if not loc or not lastmod:
            continue
        if loc.startswith("https://cursor.com/blog/"):
            blog_entries.append(SitemapEntry(url=loc, lastmod=lastmod))
        elif loc.startswith("https://cursor.com/changelog/"):
            changelog_entries.append(SitemapEntry(url=loc, lastmod=lastmod))
    blog_entries.sort(key=lambda item: item.lastmod, reverse=True)
    changelog_entries.sort(key=lambda item: item.lastmod, reverse=True)
    return {"blog": blog_entries, "changelog": changelog_entries}


def paragraph_blocks(text: str) -> list[str]:
    blocks = []
    for block in re.split(r"\n\s*\n", text):
        cleaned = block.strip()
        if cleaned:
            blocks.append(cleaned)
    return blocks


def clean_summary_block(block: str) -> str:
    lines = []
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


def parse_blog_update(page_text: str, fallback_url: str, fallback_published: str) -> UpdateItem:
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
        if cleaned == "[Changelog](http://cursor.com/changelog)":
            continue
        summary_blocks.append(cleaned)
        if len(summary_blocks) >= 2:
            break

    summary = shorten(" ".join(summary_blocks))
    return UpdateItem(title=title, url=fallback_url, published=published, summary=summary)


def clean_x_post(lines: Iterable[str]) -> str:
    filtered: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if not line:
            continue
        if line in {
            "Cursor",
            "@cursor_ai",
            "Pinned",
            "Cursor’s posts",
            "--------------",
            "The best way to code with AI.",
        }:
            continue
        if line.startswith("![Image "):
            continue
        if line.startswith("[![Image "):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        filtered.append(line)
    return "\n".join(filtered).strip()


def parse_x_posts(page_text: str, limit: int = 10) -> list[str]:
    body = markdown_body(page_text)
    lines = body.splitlines()
    posts: list[str] = []
    current: list[str] = []
    seen_header = False
    for raw_line in lines:
        line = raw_line.strip()
        if not seen_header:
            if line in {"Pinned", "Cursor’s posts"}:
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


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def diff_sitemap_entries(
    current_entries: list[SitemapEntry], previous_entries: dict[str, str]
) -> list[SitemapEntry]:
    changes = []
    for entry in current_entries:
        if previous_entries.get(entry.url) != entry.lastmod:
            changes.append(entry)
    return changes


def hash_post(text: str) -> str:
    return hashlib.sha256(normalize_space(text).encode("utf-8")).hexdigest()


def fetch_source_updates(
    entries: list[SitemapEntry],
    previous_entries: dict[str, str],
    parser,
) -> list[UpdateItem]:
    updates = []
    for entry in entries:
        page_text = fetch_via_jina(entry.url)
        updates.append(parser(page_text, entry.url, entry.lastmod))
    return updates


def fetch_x_timeline() -> list[str]:
    errors: list[str] = []
    for candidate_url in X_CANDIDATE_URLS:
        try:
            return parse_x_posts(fetch_text(candidate_url))
        except urllib.error.URLError as exc:
            errors.append(f"{candidate_url}: {exc}")
    raise FetchError(" ; ".join(errors))


def local_now(timezone_name: str) -> datetime:
    return datetime.now(tz=ZoneInfo(timezone_name))


def should_run_scheduled(
    now: datetime, target_hour: int, last_scheduled_date: str | None, force: bool
) -> tuple[bool, str]:
    if force:
        return True, "force"
    if now.hour != target_hour:
        return False, f"current local time {now.strftime('%Y-%m-%d %H:%M')} is outside {target_hour:02d}:00"
    local_date = now.date().isoformat()
    if last_scheduled_date == local_date:
        return False, f"scheduled digest for {local_date} already ran"
    return True, "scheduled"


def format_update_items(items: list[UpdateItem], empty_message: str) -> str:
    if not items:
        return f"- {empty_message}"
    rendered = []
    for index, item in enumerate(items, start=1):
        rendered.append(
            textwrap.dedent(
                f"""\
                {index}. [{item.title}]({item.url})
                   - 发布时间: {item.published}
                   - 摘要: {item.summary}
                """
            ).rstrip()
        )
    return "\n".join(rendered)


def format_x_posts(posts: list[str], empty_message: str) -> str:
    if not posts:
        return f"- {empty_message}"
    rendered = []
    for index, post in enumerate(posts, start=1):
        rendered.append(f"{index}. {shorten(post, max_chars=320)}")
    return "\n".join(rendered)


def build_report(
    now: datetime,
    mode: str,
    changelog_updates: list[UpdateItem],
    blog_updates: list[UpdateItem],
    x_posts: list[str],
    warnings: list[str],
) -> str:
    sections = [
        "# Cursor 每日更新",
        "",
        f"- 生成时间: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- 运行模式: {'强制校验' if mode == 'force' else '定时检查'}",
        "- 来源: changelog / blog / @cursor_ai",
        "",
        "## Changelog",
        format_update_items(changelog_updates, "今天没有检测到新的 changelog 页面变化。"),
        "",
        "## Blog",
        format_update_items(blog_updates, "今天没有检测到新的 blog 页面变化。"),
        "",
        "## 官方 X (@cursor_ai)",
        format_x_posts(x_posts, "今天没有检测到新的官方 X 发文。"),
    ]
    if warnings:
        sections.extend(
            [
                "",
                "## 抓取告警",
                *[f"- {warning}" for warning in warnings],
            ]
        )
    return "\n".join(sections).strip() + "\n"


def main() -> int:
    args = parse_args()
    now = local_now(args.timezone)
    state_dir = args.state_dir
    state_path = state_dir / "state.json"
    state = load_state(state_path)
    last_scheduled_date = state.get("last_scheduled_local_date")

    allowed, mode = should_run_scheduled(now, args.hour, last_scheduled_date, args.force)
    if not allowed:
        print(f"Skip: {mode}")
        return 0

    warnings: list[str] = []
    previous_snapshot = state.get("snapshot", {})
    previous_blog = previous_snapshot.get("blog", {})
    previous_changelog = previous_snapshot.get("changelog", {})
    previous_x_hashes = set(previous_snapshot.get("x_posts", []))

    source_successes = 0
    changelog_updates: list[UpdateItem] = []
    blog_updates: list[UpdateItem] = []
    x_updates: list[str] = []

    current_snapshot = {"blog": {}, "changelog": {}, "x_posts": []}

    try:
        sitemap = parse_sitemap(fetch_text(SITEMAP_URL))
        blog_entries = sitemap["blog"]
        changelog_entries = sitemap["changelog"]
        current_snapshot["blog"] = {entry.url: entry.lastmod for entry in blog_entries}
        current_snapshot["changelog"] = {entry.url: entry.lastmod for entry in changelog_entries}

        first_run = not previous_snapshot
        changed_blog_entries = diff_sitemap_entries(blog_entries, previous_blog)
        changed_changelog_entries = diff_sitemap_entries(changelog_entries, previous_changelog)
        blog_targets = (
            blog_entries[:SOURCE_LIMIT_ON_FIRST_RUN] if first_run else changed_blog_entries
        )
        changelog_targets = (
            changelog_entries[:SOURCE_LIMIT_ON_FIRST_RUN]
            if first_run
            else changed_changelog_entries
        )

        blog_updates = fetch_source_updates(blog_targets, previous_blog, parse_blog_update)
        changelog_updates = fetch_source_updates(
            changelog_targets, previous_changelog, parse_changelog_update
        )
        source_successes += 2
    except Exception as exc:  # pylint: disable=broad-except
        warnings.append(f"sitemap/blog/changelog 抓取失败: {exc}")

    try:
        x_posts = fetch_x_timeline()
        current_snapshot["x_posts"] = [hash_post(post) for post in x_posts]
        if previous_x_hashes:
            x_updates = [
                post for post in x_posts if hash_post(post) not in previous_x_hashes
            ]
        else:
            x_updates = x_posts[:X_POST_LIMIT_ON_FIRST_RUN]
        source_successes += 1
    except Exception as exc:  # pylint: disable=broad-except
        warnings.append(f"官方 X 抓取失败: {exc}")

    if source_successes == 0:
        raise FetchError("All sources failed: " + "; ".join(warnings))

    report = build_report(now, mode, changelog_updates, blog_updates, x_updates, warnings)
    args.output.write_text(report, encoding="utf-8")

    new_state = {
        "timezone": args.timezone,
        "target_hour": args.hour,
        "last_checked_at": now.isoformat(),
        "snapshot": current_snapshot,
    }
    if not args.force:
        new_state["last_scheduled_local_date"] = now.date().isoformat()
    elif last_scheduled_date:
        new_state["last_scheduled_local_date"] = last_scheduled_date
    save_state(state_path, new_state)

    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
