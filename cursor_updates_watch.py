from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, Iterable
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo


CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
JINA_HTTP_PREFIX = "https://r.jina.ai/http://"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_RUN_HOUR = 9
BOOTSTRAP_DAYS = 7
BLOG_RECENT_CANDIDATES = 12
X_BOOTSTRAP_LIMIT = 8
MAX_SEEN_PER_SOURCE = 200
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36"
)


FetchText = Callable[[str], str]


@dataclass
class UpdateItem:
    source: str
    item_id: str
    title: str
    link: str
    published_at: str | None
    summary: str


@dataclass
class RunResult:
    executed: bool
    skipped_reason: str | None
    report: str | None
    counts: dict[str, int]
    state: dict


def ascii_clean(text: str) -> str:
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return text


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", ascii_clean(html.unescape(text or "")).strip())


def strip_html(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", text or "")
    return normalize_whitespace(without_tags)


def slug_to_title(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    return normalize_whitespace(slug.replace("-", " ").title())


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def to_local_datetime(dt: datetime, timezone_name: str = DEFAULT_TIMEZONE) -> datetime:
    return dt.astimezone(ZoneInfo(timezone_name))


def format_date(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return to_local_datetime(dt).strftime("%Y-%m-%d")


def parse_email_date(value: str | None) -> datetime | None:
    if not value:
        return None
    return parsedate_to_datetime(value)


def parse_iso_date(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized)


def fetch_text(url: str, timeout: int = 30) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {
            "last_run_date": None,
            "last_checked_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    seen = raw.get("seen") or {}
    return {
        "last_run_date": raw.get("last_run_date"),
        "last_checked_at": raw.get("last_checked_at"),
        "seen": {
            "changelog": list(seen.get("changelog") or []),
            "blog": list(seen.get("blog") or []),
            "x": list(seen.get("x") or []),
        },
    }


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def trim_seen(values: Iterable[str]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        ordered.append(value)
        seen.add(value)
    return ordered[:MAX_SEEN_PER_SOURCE]


def should_run(
    now: datetime,
    state: dict,
    force: bool,
    timezone_name: str = DEFAULT_TIMEZONE,
    run_hour: int = DEFAULT_RUN_HOUR,
) -> tuple[bool, str | None]:
    local_now = to_local_datetime(now, timezone_name)
    if force:
        return True, None
    if local_now.hour != run_hour:
        return False, (
            f"Current local time is {local_now.strftime('%Y-%m-%d %H:%M')} "
            f"{timezone_name}; scheduled hour is {run_hour:02d}:00."
        )
    if state.get("last_run_date") == local_now.date().isoformat():
        return False, f"Already checked updates for {local_now.date().isoformat()}."
    return True, None


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for item in root.findall("./channel/item"):
        title = normalize_whitespace(item.findtext("title", default=""))
        link = normalize_whitespace(item.findtext("link", default=""))
        if not title or not link:
            continue
        published = parse_email_date(item.findtext("pubDate"))
        summary = strip_html(item.findtext("description", default=""))
        items.append(
            UpdateItem(
                source="changelog",
                item_id=link,
                title=title,
                link=link,
                published_at=format_date(published),
                summary=summary,
            )
        )
    return items


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, datetime | None]]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries: list[tuple[str, datetime | None]] = []
    for node in root.findall("sm:url", namespace):
        loc = normalize_whitespace(node.findtext("sm:loc", default="", namespaces=namespace))
        if not re.fullmatch(r"https://cursor\.com/blog/[^/]+", loc):
            continue
        lastmod = parse_iso_date(
            node.findtext("sm:lastmod", default="", namespaces=namespace)
        )
        entries.append((loc, lastmod))
    entries.sort(
        key=lambda entry: entry[1] or datetime.min.replace(tzinfo=ZoneInfo("UTC")),
        reverse=True,
    )
    return entries


def to_jina_url(url: str) -> str:
    if url.startswith("https://"):
        return JINA_HTTP_PREFIX + url[len("https://") :]
    if url.startswith("http://"):
        return JINA_HTTP_PREFIX + url[len("http://") :]
    return JINA_HTTP_PREFIX + url


def extract_leading_summary(lines: list[str], start_index: int) -> str:
    summary_lines: list[str] = []
    for line in lines[start_index:]:
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("[![") or stripped.startswith("!["):
            continue
        if stripped.startswith("[]("):
            if summary_lines:
                break
            continue
        if stripped.startswith("#"):
            if summary_lines:
                break
            continue
        if stripped.startswith(">"):
            if summary_lines:
                break
            continue
        if re.fullmatch(r"-{3,}", stripped):
            if summary_lines:
                break
            continue
        summary_lines.append(stripped)
        if len(summary_lines) >= 3:
            break
    return normalize_whitespace(" ".join(summary_lines))


def parse_jina_article(markdown_text: str, fallback_url: str) -> UpdateItem:
    lines = markdown_text.splitlines()
    title = ""
    published = None
    content_index = 0
    for index, line in enumerate(lines):
        if line.startswith("Title:"):
            title = normalize_whitespace(line.split(":", 1)[1])
        elif line.startswith("Published Time:"):
            published = parse_iso_date(line.split(":", 1)[1].strip())
        elif line.strip() == "Markdown Content:":
            content_index = index + 1
            break
    summary = extract_leading_summary(lines, content_index)
    if not title:
        title = slug_to_title(fallback_url)
    return UpdateItem(
        source="blog",
        item_id=fallback_url,
        title=title,
        link=fallback_url,
        published_at=format_date(published),
        summary=summary,
    )


def parse_x_posts(markdown_text: str) -> list[str]:
    capture = False
    posts: list[str] = []
    seen: set[str] = set()
    for raw_line in markdown_text.splitlines():
        line = normalize_whitespace(raw_line)
        if not line:
            continue
        if line == "Cursor's posts":
            capture = True
            continue
        if not capture:
            continue
        if line in {"Pinned", "Cursor", "@cursor_ai"}:
            continue
        if line.startswith("Title:") or line.startswith("URL Source:"):
            continue
        if line.startswith("Published Time:") or line.startswith("Markdown Content:"):
            continue
        if line.startswith("[![") or line.startswith("![") or line.startswith("[]("):
            continue
        if re.fullmatch(r"-{3,}", line):
            continue
        if re.fullmatch(r"\d+:\d{2}", line):
            continue
        if len(line) < 20:
            continue
        if line in seen:
            continue
        seen.add(line)
        posts.append(line)
    return posts


def make_x_item(post_text: str) -> UpdateItem:
    digest = hashlib.sha256(post_text.encode("utf-8")).hexdigest()
    return UpdateItem(
        source="x",
        item_id=digest,
        title=truncate(post_text, 80),
        link="https://x.com/cursor_ai",
        published_at=None,
        summary=post_text,
    )


def select_changelog_updates(
    items: list[UpdateItem],
    seen_ids: set[str],
    now: datetime,
) -> list[UpdateItem]:
    if seen_ids:
        return [item for item in items if item.item_id not in seen_ids]
    cutoff_date = to_local_datetime(now).date() - timedelta(days=BOOTSTRAP_DAYS)
    return [
        item
        for item in items
        if item.published_at
        and datetime.fromisoformat(item.published_at).date() >= cutoff_date
    ]


def select_blog_candidates(
    sitemap_entries: list[tuple[str, datetime | None]],
    seen_ids: set[str],
    now: datetime,
) -> list[str]:
    if seen_ids:
        candidates = [url for url, _ in sitemap_entries if url not in seen_ids]
        return candidates[:BLOG_RECENT_CANDIDATES]
    cutoff_date = to_local_datetime(now).date() - timedelta(days=BOOTSTRAP_DAYS)
    selected: list[str] = []
    for url, lastmod in sitemap_entries:
        if lastmod and to_local_datetime(lastmod).date() >= cutoff_date:
            selected.append(url)
    return selected


def build_report(
    now: datetime,
    forced: bool,
    changelog_updates: list[UpdateItem],
    blog_updates: list[UpdateItem],
    x_updates: list[UpdateItem],
) -> str:
    local_now = to_local_datetime(now)
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- Generated at: {local_now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Run mode: {'forced' if forced else 'scheduled'}",
        "- Sources: changelog, blog, official X (@cursor_ai)",
        "",
        f"## Changelog ({len(changelog_updates)})",
    ]
    lines.extend(render_section(changelog_updates))
    lines.extend(["", f"## Blog ({len(blog_updates)})"])
    lines.extend(render_section(blog_updates))
    lines.extend(["", f"## Official X ({len(x_updates)})"])
    lines.extend(render_x_section(x_updates))
    lines.append("")
    return "\n".join(lines)


def render_section(items: list[UpdateItem]) -> list[str]:
    if not items:
        return ["- No new updates found."]
    lines: list[str] = []
    for item in items:
        prefix = f"- {item.published_at} " if item.published_at else "- "
        lines.append(f"{prefix}[{item.title}]({item.link})")
        if item.summary:
            lines.append(f"  - {item.summary}")
    return lines


def render_x_section(items: list[UpdateItem]) -> list[str]:
    if not items:
        return ["- No new posts found."]
    lines: list[str] = []
    for item in items:
        lines.append(f"- {item.summary}")
    return lines


def run(
    now: datetime,
    state_path: Path,
    output_path: Path,
    force: bool = False,
    update_state_on_force: bool = False,
    fetcher: FetchText = fetch_text,
) -> RunResult:
    state = load_state(state_path)
    should_execute, skipped_reason = should_run(now=now, state=state, force=force)
    if not should_execute:
        return RunResult(
            executed=False,
            skipped_reason=skipped_reason,
            report=None,
            counts={"changelog": 0, "blog": 0, "x": 0},
            state=state,
        )

    changelog_items = parse_changelog_rss(fetcher(CHANGELOG_RSS_URL))
    changelog_updates = select_changelog_updates(
        changelog_items, set(state["seen"]["changelog"]), now
    )

    sitemap_entries = parse_blog_sitemap(fetcher(BLOG_SITEMAP_URL))
    blog_candidates = select_blog_candidates(
        sitemap_entries, set(state["seen"]["blog"]), now
    )
    blog_updates = [
        parse_jina_article(fetcher(to_jina_url(url)), fallback_url=url)
        for url in blog_candidates
    ]

    x_posts = parse_x_posts(fetcher(X_MIRROR_URL))
    if state["seen"]["x"]:
        fresh_x_posts = [post for post in x_posts if make_x_item(post).item_id not in set(state["seen"]["x"])]
    else:
        fresh_x_posts = x_posts[:X_BOOTSTRAP_LIMIT]
    x_updates = [make_x_item(post) for post in fresh_x_posts]

    report = build_report(
        now=now,
        forced=force,
        changelog_updates=changelog_updates,
        blog_updates=blog_updates,
        x_updates=x_updates,
    )
    output_path.write_text(report, encoding="utf-8")

    if not force or update_state_on_force:
        local_now = to_local_datetime(now)
        state["last_run_date"] = local_now.date().isoformat()
        state["last_checked_at"] = local_now.isoformat()
        state["seen"]["changelog"] = trim_seen(
            [item.item_id for item in changelog_items] + state["seen"]["changelog"]
        )
        state["seen"]["blog"] = trim_seen(
            [url for url, _ in sitemap_entries] + state["seen"]["blog"]
        )
        state["seen"]["x"] = trim_seen(
            [make_x_item(post).item_id for post in x_posts] + state["seen"]["x"]
        )
        save_state(state_path, state)

    return RunResult(
        executed=True,
        skipped_reason=None,
        report=report,
        counts={
            "changelog": len(changelog_updates),
            "blog": len(blog_updates),
            "x": len(x_updates),
        },
        state=state,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Cursor changelog, blog, and official X updates. "
            "By default, it only runs during the 09:00 hour in Asia/Shanghai."
        )
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the scheduled-hour check and refresh the markdown report now.",
    )
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="When combined with --force, also persist seen items and last_run_date.",
    )
    parser.add_argument(
        "--now",
        help="Override the current time with an ISO-8601 timestamp for testing.",
    )
    parser.add_argument(
        "--state-path",
        default=".cursor_updates/state.json",
        help="Path to the persisted state JSON file.",
    )
    parser.add_argument(
        "--output-path",
        default="cursor_updates.md",
        help="Path to the markdown report that will be written on successful runs.",
    )
    return parser.parse_args()


def parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(tz=ZoneInfo("UTC"))
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=ZoneInfo("UTC"))
    return parsed


def main() -> int:
    args = parse_args()
    result = run(
        now=parse_now(args.now),
        state_path=Path(args.state_path),
        output_path=Path(args.output_path),
        force=args.force,
        update_state_on_force=args.update_state,
    )
    if not result.executed:
        print(f"Skipped: {result.skipped_reason}")
        return 0
    print(
        "Wrote Cursor updates report to "
        f"{args.output_path} "
        f"(changelog={result.counts['changelog']}, "
        f"blog={result.counts['blog']}, x={result.counts['x']})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
