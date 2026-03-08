from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence, TypeVar
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
CHANGELOG_URL = "https://cursor.com/changelog"
BLOG_INDEX_URL = "https://cursor.com/en/blog"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URLS = (
    "https://r.jina.ai/http://x.com/cursor_ai",
    "https://r.jina.ai/http://twitter.com/cursor_ai",
    "https://r.jina.ai/http://x.com/cursor_ai?output=1",
)
RUNTIME_DIR = Path(".cursor_updates")
STATE_PATH = RUNTIME_DIR / "state.json"
LATEST_REPORT_PATH = RUNTIME_DIR / "latest_report.md"
HISTORY_DIR = RUNTIME_DIR / "history"
PUBLIC_REPORT_PATH = Path("cursor_updates.md")
MAX_CHANGELOG_ITEMS = 5
MAX_BLOG_ITEMS = 5
MAX_X_ITEMS = 8
STATE_SEEN_LIMIT = 200
RETRIABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
DEFAULT_TIMEOUT_SECONDS = 25
DEFAULT_RETRIES = 3
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Cursor Updates Watch)",
    "Accept": "text/html,application/xhtml+xml,application/xml,text/plain;q=0.9,*/*;q=0.8",
}
X_SKIP_LINES = {
    "Cursor",
    "@cursor_ai",
    "The best way to code with AI.",
    "Cursor’s posts",
    "Posts",
    "Replies",
    "Media",
    "Likes",
    "Highlights",
    "Pinned",
}
X_SKIP_PREFIXES = (
    "Title:",
    "URL Source:",
    "Published Time:",
    "Markdown Content:",
    "See everything new in Cursor",
    "Translate post",
    "Show more",
    "This media may contain",
)
ItemT = TypeVar("ItemT")


@dataclass(frozen=True)
class ChangelogEntry:
    id: str
    title: str
    url: str
    published_at: str
    summary: str


@dataclass(frozen=True)
class BlogPost:
    id: str
    title: str
    url: str
    published_at: str
    summary: str


@dataclass(frozen=True)
class XPost:
    id: str
    text: str


@dataclass(frozen=True)
class RunResult:
    status: str
    message: str
    report_path: Path | None = None


def normalize_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def clean_html_fragment(value: str) -> str:
    cleaned = re.sub(r"<(script|style)\b.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"</?(p|div|section|article|li|ul|ol|br|h[1-6]|time|span)\b[^>]*>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    return normalize_whitespace(cleaned)


def iso_to_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return value[:10]


def fetch_text(url: str, timeout: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_with_retries(
    url: str,
    *,
    retries: int = DEFAULT_RETRIES,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fetch_text(url, timeout=timeout)
        except HTTPError as error:
            last_error = error
            if error.code not in RETRIABLE_STATUS_CODES or attempt >= retries:
                raise
        except URLError as error:
            last_error = error
            if attempt >= retries:
                raise
        sleep(2 ** (attempt - 1))
    assert last_error is not None
    raise last_error


def fetch_first_available(
    urls: Sequence[str],
    *,
    retries: int = DEFAULT_RETRIES,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, str]:
    failures: list[str] = []
    for url in urls:
        try:
            return url, fetch_with_retries(url, retries=retries, timeout=timeout, sleep=sleep)
        except Exception as error:  # pragma: no cover - exercised by integration path
            failures.append(f"{url}: {error}")
    raise RuntimeError("Unable to fetch any X mirror:\n" + "\n".join(failures))


def parse_changelog_entries(html_text: str, limit: int = MAX_CHANGELOG_ITEMS) -> list[ChangelogEntry]:
    pattern = re.compile(
        r'href="(?P<href>/changelog/[^"]+)">'
        r'(?:<span class="label">.*?</span><span>.*?</span>)?'
        r"<time dateTime=\"(?P<published>[^\"]+)\"[^>]*>.*?</time>"
        r".*?<h1[^>]*>\s*<a[^>]*href=\"[^\"]+\">(?P<title>.*?)</a>\s*</h1>"
        r".*?<div class=\"prose prose--block\">\s*<p>(?P<summary>.*?)</p>",
        flags=re.DOTALL,
    )
    entries: list[ChangelogEntry] = []
    seen_ids: set[str] = set()
    for match in pattern.finditer(html_text):
        entry_id = html.unescape(match.group("href"))
        if entry_id in seen_ids:
            continue
        title = clean_html_fragment(match.group("title"))
        summary = clean_html_fragment(match.group("summary"))
        if not title:
            continue
        entries.append(
            ChangelogEntry(
                id=entry_id,
                title=title,
                url=f"https://cursor.com{entry_id}",
                published_at=match.group("published"),
                summary=summary,
            )
        )
        seen_ids.add(entry_id)
        if len(entries) >= limit:
            break
    return entries


def parse_blog_sitemap(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    articles: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for node in root.findall("sm:url", namespace):
        url = node.findtext("sm:loc", default="", namespaces=namespace).strip()
        if not re.fullmatch(r"https://cursor\.com/blog/[^/?#]+", url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)
        lastmod = node.findtext("sm:lastmod", default="", namespaces=namespace).strip()
        articles.append({"url": url, "lastmod": lastmod})
    articles.sort(key=lambda item: (item["lastmod"], item["url"]), reverse=True)
    return articles


def extract_meta_content(html_text: str, name: str) -> str:
    patterns = (
        rf'<meta[^>]+property="{re.escape(name)}"[^>]+content="([^"]+)"',
        rf'<meta[^>]+content="([^"]+)"[^>]+property="{re.escape(name)}"',
        rf'<meta[^>]+name="{re.escape(name)}"[^>]+content="([^"]+)"',
        rf'<meta[^>]+content="([^"]+)"[^>]+name="{re.escape(name)}"',
    )
    for pattern in patterns:
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return normalize_whitespace(match.group(1))
    return ""


def parse_blog_article(html_text: str, url: str, lastmod: str = "") -> BlogPost:
    title = extract_meta_content(html_text, "og:title")
    if not title:
        title_match = re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
        title = clean_html_fragment(title_match.group(1)) if title_match else url.rsplit("/", 1)[-1]
    if title.endswith(" · Cursor"):
        title = title[: -len(" · Cursor")]

    summary = extract_meta_content(html_text, "description") or extract_meta_content(html_text, "og:description")
    if not summary:
        summary_match = re.search(r"<p>(.*?)</p>", html_text, flags=re.IGNORECASE | re.DOTALL)
        summary = clean_html_fragment(summary_match.group(1)) if summary_match else ""

    published = extract_meta_content(html_text, "article:published_time")
    if not published:
        time_match = re.search(r'<time[^>]*dateTime="([^"]+)"', html_text, flags=re.IGNORECASE)
        published = time_match.group(1) if time_match else lastmod

    return BlogPost(
        id=url,
        title=title,
        url=url,
        published_at=published,
        summary=summary,
    )


def parse_x_posts(markdown_text: str, limit: int = MAX_X_ITEMS) -> list[XPost]:
    if "Markdown Content:" in markdown_text:
        markdown_text = markdown_text.split("Markdown Content:", 1)[1]

    paragraphs: list[str] = []
    current: list[str] = []

    def flush() -> None:
        if current:
            paragraphs.append(normalize_whitespace(" ".join(current)))
            current.clear()

    for raw_line in markdown_text.splitlines():
        line = normalize_whitespace(raw_line)
        if not line:
            flush()
            continue
        if line in X_SKIP_LINES:
            flush()
            continue
        if any(line.startswith(prefix) for prefix in X_SKIP_PREFIXES):
            flush()
            continue
        if line.startswith("[![") or line.startswith("![") or line.startswith("[Image"):
            continue
        if line.startswith("http://") or line.startswith("https://"):
            continue
        current.append(line)
    flush()

    posts: list[XPost] = []
    seen_texts: set[str] = set()
    for paragraph in paragraphs:
        if len(paragraph) < 20:
            continue
        if paragraph in seen_texts:
            continue
        seen_texts.add(paragraph)
        post_id = hashlib.sha256(paragraph.encode("utf-8")).hexdigest()[:16]
        posts.append(XPost(id=post_id, text=paragraph))
        if len(posts) >= limit:
            break
    return posts


def load_state(path: Path | None = None) -> dict[str, object]:
    path = path or STATE_PATH
    if not path.exists():
        return {
            "last_success_local_date": None,
            "last_success_run_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("seen", {})
    data["seen"].setdefault("changelog", [])
    data["seen"].setdefault("blog", [])
    data["seen"].setdefault("x", [])
    data.setdefault("last_success_local_date", None)
    data.setdefault("last_success_run_at", None)
    return data


def merge_seen(existing: Iterable[str], fresh: Iterable[str], *, limit: int = STATE_SEEN_LIMIT) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for item in list(fresh) + list(existing):
        if item in seen:
            continue
        seen.add(item)
        merged.append(item)
        if len(merged) >= limit:
            break
    return merged


def should_run(now: datetime, state: dict[str, object], *, force: bool = False) -> tuple[bool, str]:
    local_now = now.astimezone(SHANGHAI_TZ)
    if force:
        return True, "Forced run requested."
    if local_now.hour != 9:
        return False, (
            f"Skipping scheduled run at {local_now.isoformat()} because it only runs during "
            "the 09:00 hour in Asia/Shanghai."
        )
    if state.get("last_success_local_date") == local_now.date().isoformat():
        return False, f"Skipping scheduled run because {local_now.date().isoformat()} already succeeded."
    return True, "Scheduled window open."


def select_new_items(items: Sequence[ItemT], seen_ids: set[str], *, key: Callable[[ItemT], str]) -> list[ItemT]:
    return [item for item in items if key(item) not in seen_ids]


def render_updates_section(title: str, lines: Sequence[str]) -> list[str]:
    rendered = [f"### {title}"]
    if not lines:
        rendered.append("- No new items detected.")
    else:
        rendered.extend(lines)
    rendered.append("")
    return rendered


def render_changelog_item(entry: ChangelogEntry) -> str:
    return f"- {iso_to_date(entry.published_at)} [{entry.title}]({entry.url}) - {entry.summary}"


def render_blog_item(post: BlogPost) -> str:
    return f"- {iso_to_date(post.published_at)} [{post.title}]({post.url}) - {post.summary}"


def render_x_item(post: XPost) -> str:
    return f"- {post.text}"


def build_report(
    *,
    local_now: datetime,
    x_source_url: str,
    changelog_entries: Sequence[ChangelogEntry],
    blog_posts: Sequence[BlogPost],
    x_posts: Sequence[XPost],
    new_changelog: Sequence[ChangelogEntry],
    new_blog: Sequence[BlogPost],
    new_x_posts: Sequence[XPost],
) -> str:
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- Date (Asia/Shanghai): {local_now.date().isoformat()}",
        f"- Generated at: {local_now.isoformat()}",
        f"- Sources: [Changelog]({CHANGELOG_URL}) | [Blog]({BLOG_INDEX_URL}) | [Official X mirror]({x_source_url})",
        "",
        "## New since last successful run",
        "",
    ]
    lines.extend(render_updates_section("Changelog", [render_changelog_item(entry) for entry in new_changelog]))
    lines.extend(render_updates_section("Blog", [render_blog_item(post) for post in new_blog]))
    lines.extend(render_updates_section("Official X (@cursor_ai)", [render_x_item(post) for post in new_x_posts]))
    lines.append("## Latest snapshot")
    lines.append("")
    lines.extend(
        render_updates_section("Changelog", [render_changelog_item(entry) for entry in changelog_entries[:MAX_CHANGELOG_ITEMS]])
    )
    lines.extend(render_updates_section("Blog", [render_blog_item(post) for post in blog_posts[:MAX_BLOG_ITEMS]]))
    lines.extend(render_updates_section("Official X (@cursor_ai)", [render_x_item(post) for post in x_posts[:MAX_X_ITEMS]]))
    return "\n".join(lines).strip() + "\n"


def ensure_runtime_dirs() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def write_report(report: str, *, local_date: str) -> None:
    ensure_runtime_dirs()
    LATEST_REPORT_PATH.write_text(report, encoding="utf-8")
    (HISTORY_DIR / f"{local_date}.md").write_text(report, encoding="utf-8")
    PUBLIC_REPORT_PATH.write_text(report, encoding="utf-8")


def write_state(state: dict[str, object], path: Path | None = None) -> None:
    path = path or STATE_PATH
    ensure_runtime_dirs()
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def collect_blog_posts(
    fetcher: Callable[[str], str],
    *,
    existing_seen: set[str],
    max_items: int = MAX_BLOG_ITEMS,
) -> list[BlogPost]:
    sitemap_xml = fetcher(BLOG_SITEMAP_URL)
    candidates = parse_blog_sitemap(sitemap_xml)
    selected_urls: list[dict[str, str]] = []
    for candidate in candidates:
        if len(selected_urls) < max_items or candidate["url"] not in existing_seen:
            selected_urls.append(candidate)
        if len(selected_urls) >= max(max_items, 10):
            break

    posts: list[BlogPost] = []
    for candidate in selected_urls:
        html_text = fetcher(candidate["url"])
        posts.append(parse_blog_article(html_text, candidate["url"], candidate["lastmod"]))
    posts.sort(key=lambda item: (item.published_at, item.url), reverse=True)
    return posts


def run_watch(
    *,
    force: bool = False,
    now: datetime | None = None,
    fetcher: Callable[[str], str] | None = None,
    x_fetcher: Callable[[Sequence[str]], tuple[str, str]] | None = None,
) -> RunResult:
    current_time = now or datetime.now(timezone.utc)
    state = load_state()
    should_execute, reason = should_run(current_time, state, force=force)
    if not should_execute:
        return RunResult(status="skipped", message=reason, report_path=PUBLIC_REPORT_PATH if PUBLIC_REPORT_PATH.exists() else None)

    fetch = fetcher or (lambda url: fetch_with_retries(url))
    pick_x = x_fetcher or (lambda urls: fetch_first_available(urls))

    changelog_entries = parse_changelog_entries(fetch(CHANGELOG_URL))
    seen_blog = set(state["seen"]["blog"])
    blog_posts = collect_blog_posts(fetch, existing_seen=seen_blog)
    x_source_url, x_text = pick_x(X_MIRROR_URLS)
    x_posts = parse_x_posts(x_text)

    seen_changelog = set(state["seen"]["changelog"])
    seen_x = set(state["seen"]["x"])
    new_changelog = select_new_items(changelog_entries, seen_changelog, key=lambda item: item.id)
    new_blog = select_new_items(blog_posts, seen_blog, key=lambda item: item.id)
    new_x_posts = select_new_items(x_posts, seen_x, key=lambda item: item.id)

    local_now = current_time.astimezone(SHANGHAI_TZ)
    report = build_report(
        local_now=local_now,
        x_source_url=x_source_url,
        changelog_entries=changelog_entries,
        blog_posts=blog_posts,
        x_posts=x_posts,
        new_changelog=new_changelog,
        new_blog=new_blog,
        new_x_posts=new_x_posts,
    )
    write_report(report, local_date=local_now.date().isoformat())

    if not force:
        state["last_success_local_date"] = local_now.date().isoformat()
        state["last_success_run_at"] = current_time.astimezone(timezone.utc).isoformat()
        state["seen"]["changelog"] = merge_seen(state["seen"]["changelog"], [item.id for item in changelog_entries])
        state["seen"]["blog"] = merge_seen(state["seen"]["blog"], [item.id for item in blog_posts])
        state["seen"]["x"] = merge_seen(state["seen"]["x"], [item.id for item in x_posts])
        write_state(state)

    return RunResult(status="ok", message="Report generated successfully.", report_path=PUBLIC_REPORT_PATH)


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fetch daily Cursor changelog, blog, and official X updates.")
    parser.add_argument("--force", action="store_true", help="Run immediately without schedule gating or state mutation.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    result = run_watch(force=args.force)
    print(result.message)
    if result.report_path is not None:
        print(f"Report: {result.report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
