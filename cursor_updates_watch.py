from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


USER_AGENT = "Mozilla/5.0 (Cursor Updates Watcher)"
CURSOR_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_FEED_URLS = (
    "https://r.jina.ai/http://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
SCHEDULED_HOUR = 9
FETCH_TIMEOUT_SECONDS = 30
ENTRY_WINDOW = 8
SEEN_LIMIT = 200

ROOT = Path(__file__).resolve().parent
REPORT_DIR = ROOT / ".cursor_updates"
STATE_PATH = REPORT_DIR / "seen_state.json"
LATEST_REPORT_PATH = REPORT_DIR / "latest_report.md"
FRIENDLY_REPORT_PATH = ROOT / "cursor_updates.md"


class FetchError(RuntimeError):
    """Raised when all remote sources fail."""


@dataclass(frozen=True)
class UpdateItem:
    source: str
    identifier: str
    title: str
    url: str | None
    published: datetime | None
    summary: str


@dataclass(frozen=True)
class RunResult:
    skipped: bool
    reason: str
    report: str


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_html_tags(value: str) -> str:
    text = re.sub(r"<br\s*/?>", " ", value, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return normalize_whitespace(html.unescape(text))


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    cleaned = value.strip()
    try:
        if cleaned.endswith("Z"):
            return datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        return datetime.fromisoformat(cleaned)
    except ValueError:
        pass

    for fmt in ("%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(cleaned, fmt).replace(tzinfo=LOCAL_TIMEZONE)
        except ValueError:
            continue

    return None


def format_timestamp(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(LOCAL_TIMEZONE).strftime("%Y-%m-%d %H:%M %Z")


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        return response.read().decode(charset, errors="replace")


def fetch_first_success(urls: Iterable[str]) -> str:
    last_error: Exception | None = None
    for url in urls:
        try:
            return fetch_text(url)
        except (HTTPError, URLError, TimeoutError, ValueError) as exc:
            last_error = exc
    raise FetchError(f"all remote sources failed: {last_error}")


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"last_scheduled_run_date": None, "seen": {"blog": [], "changelog": [], "x": []}}

    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"last_scheduled_run_date": None, "seen": {"blog": [], "changelog": [], "x": []}}


def write_state(state: dict[str, Any]) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def write_report(report: str) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    LATEST_REPORT_PATH.write_text(report, encoding="utf-8")
    FRIENDLY_REPORT_PATH.write_text(report, encoding="utf-8")


def should_run_now(now_utc: datetime, state: dict[str, Any], force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced run"

    local_now = now_utc.astimezone(LOCAL_TIMEZONE)
    if local_now.hour != SCHEDULED_HOUR:
        return False, (
            f"Skip: current local time is {local_now.strftime('%Y-%m-%d %H:%M %Z')}, "
            f"scheduled hour is {SCHEDULED_HOUR:02d}:00 {LOCAL_TIMEZONE.key}."
        )

    run_date = local_now.date().isoformat()
    if state.get("last_scheduled_run_date") == run_date:
        return False, f"Skip: already completed scheduled run for {run_date}."

    return True, f"scheduled run for {run_date}"


def parse_sitemap_entries(xml_text: str, section: str, limit: int = ENTRY_WINDOW) -> list[tuple[str, datetime | None]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[tuple[str, datetime | None]] = []
    prefix = f"https://cursor.com/{section}/"

    for url in root.findall("sm:url", namespace):
        loc = url.findtext("sm:loc", default="", namespaces=namespace).strip()
        if not loc.startswith(prefix):
            continue

        lastmod = parse_datetime(url.findtext("sm:lastmod", default="", namespaces=namespace))
        entries.append((loc, lastmod))

    entries.sort(key=lambda item: item[1] or datetime.min.replace(tzinfo=UTC), reverse=True)
    return entries[:limit]


def iter_json_nodes(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for nested in value.values():
            yield from iter_json_nodes(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from iter_json_nodes(nested)


def extract_json_ld_objects(html_text: str) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []
    for block in re.findall(
        r'<script[^>]+application/ld\+json[^>]*>\s*(.*?)\s*</script>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        raw = html.unescape(block.strip())
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        objects.extend(iter_json_nodes(payload))
    return objects


def extract_title_tag(html_text: str) -> str | None:
    match = re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    title = strip_html_tags(match.group(1))
    title = re.sub(r"\s*[·|-]\s*Cursor$", "", title).strip()
    return title or None


def extract_meta_description(html_text: str) -> str | None:
    match = re.search(
        r'<meta[^>]+name="description"[^>]+content="(.*?)"',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None
    return normalize_whitespace(html.unescape(match.group(1)))


def extract_first_paragraph(html_text: str) -> str | None:
    match = re.search(r"<p>(.*?)</p>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return strip_html_tags(match.group(1))


def build_blog_item(url: str, fallback_published: datetime | None) -> UpdateItem:
    html_text = fetch_text(url)
    title = None
    published = fallback_published
    summary = None

    for node in extract_json_ld_objects(html_text):
        node_type = node.get("@type")
        if isinstance(node_type, list):
            node_types = set(node_type)
        elif isinstance(node_type, str):
            node_types = {node_type}
        else:
            node_types = set()

        if node_types.intersection({"Article", "BlogPosting", "NewsArticle"}):
            title = title or normalize_whitespace(str(node.get("headline", "")).strip())
            published = parse_datetime(str(node.get("datePublished", "")).strip()) or published
            summary = summary or normalize_whitespace(str(node.get("description", "")).strip())

    title = title or extract_title_tag(html_text) or url.rsplit("/", 1)[-1]
    summary = summary or extract_meta_description(html_text) or extract_first_paragraph(html_text) or ""
    return UpdateItem(
        source="blog",
        identifier=url,
        title=title,
        url=url,
        published=published,
        summary=summary,
    )


def build_changelog_item(url: str, fallback_published: datetime | None) -> UpdateItem:
    html_text = fetch_text(url)
    title = None
    published = fallback_published
    summary = None

    heading_match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if heading_match:
        title = strip_html_tags(heading_match.group(1))

    time_match = re.search(r"<time[^>]*>(.*?)</time>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if time_match:
        published = parse_datetime(strip_html_tags(time_match.group(1))) or published

    summary_match = re.search(
        r'<div class="prose prose--block">.*?<p>(.*?)</p>',
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if summary_match:
        summary = strip_html_tags(summary_match.group(1))

    title = title or extract_title_tag(html_text) or url.rsplit("/", 1)[-1]
    summary = summary or extract_first_paragraph(html_text) or extract_meta_description(html_text) or ""
    return UpdateItem(
        source="changelog",
        identifier=url,
        title=title,
        url=url,
        published=published,
        summary=summary,
    )


def build_x_items(markdown_text: str, limit: int = ENTRY_WINDOW) -> list[UpdateItem]:
    content = markdown_text.split("Markdown Content:", 1)[-1]
    lines = [line.rstrip() for line in content.splitlines()]
    posts: list[str] = []
    current: list[str] = []

    def flush_current() -> None:
        if not current:
            return
        text = normalize_whitespace(" ".join(current))
        if text:
            posts.append(text)
        current.clear()

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in {"Cursor", "@cursor_ai", "Pinned", "Cursor’s posts", "--------------"}:
            continue
        if stripped == "The best way to code with AI.":
            continue
        if stripped.startswith("[![Image") and "https://x.com/cursor_ai" in stripped:
            flush_current()
            continue
        if stripped.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", stripped):
            continue
        current.append(stripped)

    flush_current()

    seen: set[str] = set()
    items: list[UpdateItem] = []
    for text in posts:
        identifier = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        if identifier in seen:
            continue
        seen.add(identifier)
        items.append(
            UpdateItem(
                source="x",
                identifier=identifier,
                title=text[:120] + ("..." if len(text) > 120 else ""),
                url="https://x.com/cursor_ai",
                published=None,
                summary=text,
            )
        )

    return items[:limit]


def merge_seen(existing: list[str], items: list[UpdateItem]) -> list[str]:
    current_ids = [item.identifier for item in items]
    merged = current_ids + [identifier for identifier in existing if identifier not in current_ids]
    return merged[:SEEN_LIMIT]


def annotate_newness(items: list[UpdateItem], seen_ids: set[str]) -> list[tuple[UpdateItem, bool]]:
    return [(item, item.identifier not in seen_ids) for item in items]


def render_section(title: str, items: list[tuple[UpdateItem, bool]]) -> str:
    lines = [f"## {title}", ""]
    if not items:
        lines.append("- No items found.")
        lines.append("")
        return "\n".join(lines)

    for item, is_new in items:
        prefix = "[NEW] " if is_new else ""
        lines.append(f"- {prefix}{item.title}")
        if item.url:
            lines.append(f"  - URL: {item.url}")
        lines.append(f"  - Published: {format_timestamp(item.published)}")
        if item.summary:
            lines.append(f"  - Summary: {item.summary}")
        lines.append("")
    return "\n".join(lines)


def render_report(
    now_utc: datetime,
    mode_reason: str,
    blog_items: list[tuple[UpdateItem, bool]],
    changelog_items: list[tuple[UpdateItem, bool]],
    x_items: list[tuple[UpdateItem, bool]],
) -> str:
    local_now = now_utc.astimezone(LOCAL_TIMEZONE)
    blog_new = sum(1 for _, is_new in blog_items if is_new)
    changelog_new = sum(1 for _, is_new in changelog_items if is_new)
    x_new = sum(1 for _, is_new in x_items if is_new)

    parts = [
        "# Cursor Daily Updates",
        "",
        f"- Generated at (UTC): {now_utc.astimezone(UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"- Generated at ({LOCAL_TIMEZONE.key}): {local_now.strftime('%Y-%m-%d %H:%M %Z')}",
        f"- Run mode: {mode_reason}",
        "",
        "## Summary",
        "",
        f"- Changelog: {changelog_new} new / {len(changelog_items)} checked",
        f"- Blog: {blog_new} new / {len(blog_items)} checked",
        f"- Official X posts: {x_new} new / {len(x_items)} checked",
        "",
        render_section("Changelog", changelog_items),
        render_section("Blog", blog_items),
        render_section("Official X", x_items),
    ]
    return "\n".join(parts).strip() + "\n"


def run(force: bool = False, now_utc: datetime | None = None) -> RunResult:
    now_utc = now_utc or datetime.now(UTC)
    state = load_state()
    should_run, reason = should_run_now(now_utc, state, force=force)

    if not should_run:
        return RunResult(skipped=True, reason=reason, report=reason + "\n")

    sitemap_text = fetch_text(CURSOR_SITEMAP_URL)
    blog_urls = parse_sitemap_entries(sitemap_text, "blog")
    changelog_urls = parse_sitemap_entries(sitemap_text, "changelog")
    x_feed_text = fetch_first_success(X_FEED_URLS)

    blog_updates = [build_blog_item(url, published) for url, published in blog_urls]
    changelog_updates = [build_changelog_item(url, published) for url, published in changelog_urls]
    x_updates = build_x_items(x_feed_text)

    seen = state.get("seen", {})
    blog_with_flags = annotate_newness(blog_updates, set(seen.get("blog", [])))
    changelog_with_flags = annotate_newness(changelog_updates, set(seen.get("changelog", [])))
    x_with_flags = annotate_newness(x_updates, set(seen.get("x", [])))

    report = render_report(now_utc, reason, blog_with_flags, changelog_with_flags, x_with_flags)
    write_report(report)

    if not force:
        local_date = now_utc.astimezone(LOCAL_TIMEZONE).date().isoformat()
        state["last_scheduled_run_date"] = local_date
        state["seen"] = {
            "blog": merge_seen(seen.get("blog", []), blog_updates),
            "changelog": merge_seen(seen.get("changelog", []), changelog_updates),
            "x": merge_seen(seen.get("x", []), x_updates),
        }
        write_state(state)

    return RunResult(skipped=False, reason=reason, report=report)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Cursor changelog, blog, and official X updates on a daily schedule."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately and generate a report without consuming the scheduled slot.",
    )
    parser.add_argument(
        "--now-utc",
        help="Override the current UTC timestamp in ISO format, useful for tests.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    now_utc = parse_datetime(args.now_utc) if args.now_utc else None
    if now_utc and now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=UTC)

    result = run(force=args.force, now_utc=now_utc)
    sys.stdout.write(result.report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
