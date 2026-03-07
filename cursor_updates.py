from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) CursorAutomation/1.0"
CHANGELOG_URL = "https://www.cursor.com/changelog"
BLOG_URL = "https://www.cursor.com/blog"
X_SNAPSHOT_URL = "https://r.jina.ai/http://x.com/cursor_ai"
OFFICIAL_X_URL = "https://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_HOUR = 9
STATE_PATH = ".cursor_updates_state.json"
OUTPUT_DIR = "reports/cursor-updates"


@dataclass(frozen=True)
class UpdateEntry:
    source: str
    entry_id: str
    title: str
    url: str
    published_at: str | None = None
    summary: str | None = None
    category: str | None = None


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def strip_tags(raw_html: str) -> str:
    text = re.sub(r"<[^>]+>", " ", raw_html)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_url(base: str, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return base.rstrip("/") + "/" + path.lstrip("/")


def dedupe_entries(entries: Iterable[UpdateEntry]) -> list[UpdateEntry]:
    deduped: list[UpdateEntry] = []
    seen_ids: set[str] = set()
    for entry in entries:
        if entry.entry_id in seen_ids:
            continue
        seen_ids.add(entry.entry_id)
        deduped.append(entry)
    return deduped


def parse_changelog_html(content: str, max_items: int = 10) -> list[UpdateEntry]:
    entries: list[UpdateEntry] = []
    chunks = content.split("<article>")
    for chunk in chunks[1:]:
        article = chunk.split("</article>", 1)[0]
        href_match = re.search(r'href="(?P<href>/changelog/[^"]+)"', article)
        title_match = re.search(r"<h1[^>]*>.*?<a[^>]*>(?P<title>.*?)</a>", article, re.S)
        time_match = re.search(
            r'<time dateTime="(?P<iso>[^"]+)"[^>]*>(?P<label>[^<]+)</time>',
            article,
            re.S,
        )
        if not href_match or not title_match or not time_match:
            continue

        paragraph_matches = re.findall(r"<p>(.*?)</p>", article, re.S)
        paragraphs = [strip_tags(match) for match in paragraph_matches]
        paragraphs = [paragraph for paragraph in paragraphs if paragraph and paragraph != "Changelog"]
        summary = " ".join(paragraphs[:2]) if paragraphs else None

        version_match = re.search(r'<span class="label">(?P<version>[^<]+)</span>', article)
        version = strip_tags(version_match.group("version")) if version_match else None
        title = strip_tags(title_match.group("title"))
        if version and version not in title:
            title = f"{title} (v{version})"

        entries.append(
            UpdateEntry(
                source="changelog",
                entry_id=normalize_url("https://cursor.com", href_match.group("href")),
                title=title,
                url=normalize_url("https://cursor.com", href_match.group("href")),
                published_at=time_match.group("iso"),
                summary=summary,
            )
        )
    entries = dedupe_entries(entries)
    if len(entries) > max_items:
        entries = entries[:max_items]

    if not entries:
        raise ValueError("Failed to parse any changelog entries.")
    return entries


def parse_blog_html(content: str, max_items: int = 10) -> list[UpdateEntry]:
    entries: list[UpdateEntry] = []
    chunks = content.split('<article class="flex grow-1 flex-col mb-g1">')
    for chunk in chunks[1:]:
        article = chunk.split("</article>", 1)[0]
        href_match = re.search(r'href="(?P<href>/blog/(?!topic/)[^"]+)"', article)
        title_match = re.search(
            r'<p class="type-base text-theme-text text-pretty">(?P<title>.*?)</p>',
            article,
            re.S,
        )
        summary_match = re.search(
            r'<p class="type-base text-theme-text-sec text-pretty">(?P<summary>.*?)</p>',
            article,
            re.S,
        )
        category_match = re.search(r'<span class="capitalize">(?P<category>.*?)<!-- -->', article, re.S)
        time_match = re.search(
            r'<time dateTime="(?P<iso>[^"]+)"[^>]*>(?P<label>[^<]+)</time>',
            article,
            re.S,
        )
        if not href_match or not title_match or not summary_match or not time_match:
            continue

        category = strip_tags(category_match.group("category")) if category_match else None
        entries.append(
            UpdateEntry(
                source="blog",
                entry_id=normalize_url("https://cursor.com", href_match.group("href")),
                title=strip_tags(title_match.group("title")),
                url=normalize_url("https://cursor.com", href_match.group("href")),
                published_at=time_match.group("iso"),
                summary=strip_tags(summary_match.group("summary")),
                category=category,
            )
        )
    entries = dedupe_entries(entries)
    if len(entries) > max_items:
        entries = entries[:max_items]

    if not entries:
        raise ValueError("Failed to parse any blog entries.")
    return entries


def _is_noise_x_line(line: str) -> bool:
    boilerplate_fragments = (
        "Log in",
        "Sign up",
        "Follow",
        "Click to Follow",
        "Joined ",
        "Following",
        "Followers",
        "Posts",
        "Affiliates",
        "Replies",
        "Highlights",
        "Media",
        "See new posts",
        "Don’t miss what’s happening",
        "People on X are the first to know.",
        "New to X?",
        "Create account",
        "Terms of Service",
        "Privacy Policy",
        "Cookie Policy",
        "Accessibility",
        "Ads info",
        "© ",
    )
    exact_noise = {
        "Pinned",
        "Cursor",
        "@cursor_ai",
        "The best way to code with AI.",
        "cursor.com",
    }
    if not line:
        return True
    if line in exact_noise:
        return True
    if line.startswith("[") or line.startswith("!"):
        return True
    if re.fullmatch(r"\d+:\d+", line):
        return True
    return any(fragment in line for fragment in boilerplate_fragments)


def parse_x_markdown(content: str, max_items: int = 10) -> tuple[str | None, list[UpdateEntry]]:
    published_match = re.search(r"Published Time:\s*(.+)", content)
    snapshot_time = None
    if published_match:
        snapshot_time = parsedate_to_datetime(published_match.group(1)).isoformat()

    body = content.split("Markdown Content:", 1)[-1]
    if "Pinned" in body:
        body = body.split("Pinned", 1)[1]

    candidates: list[str] = []
    seen: set[str] = set()
    for raw_line in body.splitlines():
        line = re.sub(r"\s+", " ", raw_line).strip()
        if _is_noise_x_line(line):
            continue
        if len(line) < 12:
            continue
        if line in seen:
            continue
        seen.add(line)
        candidates.append(line)
        if len(candidates) >= max_items:
            break

    if not candidates:
        raise ValueError("Failed to parse any recent X posts.")

    entries = [
        UpdateEntry(
            source="x",
            entry_id=hashlib.sha1(text.encode("utf-8")).hexdigest(),
            title=text,
            url=OFFICIAL_X_URL,
            published_at=snapshot_time,
        )
        for text in candidates
    ]
    return snapshot_time, entries


def load_state(path: Path) -> dict:
    if not path.exists():
        return {
            "last_successful_run_at": None,
            "last_successful_local_date": None,
            "seen_ids": {"changelog": [], "blog": [], "x": []},
        }
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def merge_seen_ids(previous_ids: Iterable[str], new_ids: Iterable[str], limit: int = 200) -> list[str]:
    merged: list[str] = []
    for value in list(new_ids) + list(previous_ids):
        if value not in merged:
            merged.append(value)
        if len(merged) >= limit:
            break
    return merged


def collect_new_entries(entries: list[UpdateEntry], seen_ids: Iterable[str]) -> list[UpdateEntry]:
    seen = set(seen_ids)
    return [entry for entry in entries if entry.entry_id not in seen]


def should_run_now(now_local: datetime, target_hour: int) -> bool:
    return now_local.hour == target_hour


def format_entry(entry: UpdateEntry, index: int) -> str:
    lines = [f"### {index}. {entry.title}"]
    if entry.published_at:
        lines.append(f"- Published: {entry.published_at}")
    if entry.category:
        lines.append(f"- Category: {entry.category}")
    lines.append(f"- Link: {entry.url}")
    if entry.summary:
        lines.append(f"- Summary: {entry.summary}")
    return "\n".join(lines)


def build_report(
    *,
    now_local: datetime,
    timezone_name: str,
    changelog_entries: list[UpdateEntry],
    blog_entries: list[UpdateEntry],
    x_entries: list[UpdateEntry],
    new_changelog_entries: list[UpdateEntry],
    new_blog_entries: list[UpdateEntry],
    new_x_entries: list[UpdateEntry],
    x_snapshot_time: str | None,
    first_run: bool,
) -> str:
    header_lines = [
        f"# Cursor Daily Updates - {now_local.date().isoformat()}",
        "",
        f"- Generated at: {now_local.isoformat()}",
        f"- Timezone: {timezone_name}",
        f"- Sources: changelog, blog, official X (@cursor_ai)",
        "",
        "## Summary",
        f"- Changelog: {len(new_changelog_entries)} new item(s), {len(changelog_entries)} latest item(s) scanned",
        f"- Blog: {len(new_blog_entries)} new item(s), {len(blog_entries)} latest item(s) scanned",
        f"- X: {len(new_x_entries)} new post(s), {len(x_entries)} recent post(s) scanned",
    ]
    if x_snapshot_time:
        header_lines.append(f"- X snapshot captured at: {x_snapshot_time}")
    if first_run:
        header_lines.append("- Mode: initial baseline snapshot (no previous state found)")

    sections = []
    source_sections = [
        ("Changelog", changelog_entries, new_changelog_entries),
        ("Blog", blog_entries, new_blog_entries),
        ("Official X", x_entries, new_x_entries),
    ]
    for title, latest_entries, new_entries in source_sections:
        display_entries = new_entries if new_entries else latest_entries[:3]
        sections.append(f"## {title}")
        if new_entries:
            sections.append(f"New items since the last successful run: {len(new_entries)}")
        else:
            sections.append("No new items since the last successful run. Latest snapshot:")
        sections.append("")
        for index, entry in enumerate(display_entries, start=1):
            sections.append(format_entry(entry, index))
            sections.append("")

    return "\n".join(header_lines + [""] + sections).rstrip() + "\n"


def resolve_repo_path(base_dir: Path, value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (base_dir / path)


def current_local_time(tz: ZoneInfo) -> datetime:
    return datetime.now(timezone.utc).astimezone(tz)


def run(args: argparse.Namespace) -> int:
    base_dir = Path(__file__).resolve().parent
    output_dir = resolve_repo_path(base_dir, args.output_dir)
    state_path = resolve_repo_path(base_dir, args.state_file)
    output_dir.mkdir(parents=True, exist_ok=True)

    tz = ZoneInfo(args.timezone)
    now_local = current_local_time(tz)

    if not args.force and not should_run_now(now_local, args.hour):
        print(
            f"Skipping run: current local time is {now_local.isoformat()}, "
            f"scheduled hour is {args.hour:02d}:00.",
            file=sys.stdout,
        )
        return 0

    state = load_state(state_path)
    if not args.force and state.get("last_successful_local_date") == now_local.date().isoformat():
        print(f"Skipping run: report for {now_local.date().isoformat()} already exists.", file=sys.stdout)
        return 0

    changelog_entries = parse_changelog_html(fetch_text(CHANGELOG_URL), max_items=args.max_changelog)
    blog_entries = parse_blog_html(fetch_text(BLOG_URL), max_items=args.max_blog)
    x_snapshot_time, x_entries = parse_x_markdown(fetch_text(X_SNAPSHOT_URL), max_items=args.max_x)

    seen_ids = state.get("seen_ids", {"changelog": [], "blog": [], "x": []})
    new_changelog_entries = collect_new_entries(changelog_entries, seen_ids.get("changelog", []))
    new_blog_entries = collect_new_entries(blog_entries, seen_ids.get("blog", []))
    new_x_entries = collect_new_entries(x_entries, seen_ids.get("x", []))
    first_run = not any(seen_ids.get(source) for source in ("changelog", "blog", "x"))

    report = build_report(
        now_local=now_local,
        timezone_name=args.timezone,
        changelog_entries=changelog_entries,
        blog_entries=blog_entries,
        x_entries=x_entries,
        new_changelog_entries=new_changelog_entries,
        new_blog_entries=new_blog_entries,
        new_x_entries=new_x_entries,
        x_snapshot_time=x_snapshot_time,
        first_run=first_run,
    )

    report_path = output_dir / f"{now_local.date().isoformat()}.md"
    report_path.write_text(report, encoding="utf-8")

    state["last_successful_run_at"] = now_local.isoformat()
    state["last_successful_local_date"] = now_local.date().isoformat()
    state["timezone"] = args.timezone
    state["seen_ids"] = {
        "changelog": merge_seen_ids(seen_ids.get("changelog", []), [entry.entry_id for entry in changelog_entries]),
        "blog": merge_seen_ids(seen_ids.get("blog", []), [entry.entry_id for entry in blog_entries]),
        "x": merge_seen_ids(seen_ids.get("x", []), [entry.entry_id for entry in x_entries]),
    }
    try:
        report_location = str(report_path.relative_to(base_dir))
    except ValueError:
        report_location = str(report_path)

    state["latest_report"] = {
        "path": report_location,
        "generated_at": now_local.isoformat(),
        "counts": {
            "changelog": len(new_changelog_entries),
            "blog": len(new_blog_entries),
            "x": len(new_x_entries),
        },
    }
    save_state(state_path, state)

    print(f"Wrote report to {report_path}", file=sys.stdout)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect daily Cursor updates from changelog, blog, and official X posts."
    )
    parser.add_argument("--force", action="store_true", help="Run immediately even outside the scheduled hour.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA timezone name. Default: Asia/Shanghai.")
    parser.add_argument("--hour", type=int, default=DEFAULT_HOUR, help="Scheduled local hour to run. Default: 9.")
    parser.add_argument("--max-changelog", type=int, default=6, help="How many changelog items to scan.")
    parser.add_argument("--max-blog", type=int, default=8, help="How many blog items to scan.")
    parser.add_argument("--max-x", type=int, default=10, help="How many recent X posts to scan.")
    parser.add_argument("--state-file", default=STATE_PATH, help="Path to the persisted state file.")
    parser.add_argument("--output-dir", default=OUTPUT_DIR, help="Directory where markdown reports are written.")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
