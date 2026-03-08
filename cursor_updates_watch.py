from __future__ import annotations

import argparse
import html
import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".cursor_updates"
STATE_PATH = STATE_DIR / "state.json"
REPORT_PATH = ROOT / "cursor_updates.md"
RUN_TZ = ZoneInfo("Asia/Shanghai")
RUN_HOUR = 9

CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_TIMELINE_URL = "https://syndication.twitter.com/srv/timeline-profile/screen-name/cursor_ai"
USER_AGENT = "Mozilla/5.0 (compatible; CursorUpdatesBot/1.0)"


@dataclass(frozen=True)
class UpdateItem:
    source: str
    item_id: str
    title: str
    url: str
    published_at: str
    summary: str

    def published_dt(self) -> datetime:
        return datetime.fromisoformat(self.published_at)


def fetch_text(url: str) -> str:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def collapse_whitespace(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def strip_html(value: str) -> str:
    return collapse_whitespace(re.sub(r"<[^>]+>", " ", value))


def parse_rfc2822(value: str) -> datetime:
    return parsedate_to_datetime(value).astimezone(UTC)


def parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def unique_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def trim_text(value: str, limit: int = 280) -> str:
    value = collapse_whitespace(value)
    if len(value) <= limit:
        return value
    return value[: limit - 3].rstrip() + "..."


def load_state() -> dict[str, Any]:
    if not STATE_PATH.exists():
        return {"last_run_date": "", "seen": {"changelog": [], "blog": [], "x": []}}

    with STATE_PATH.open("r", encoding="utf-8") as handle:
        raw_state = json.load(handle)

    seen = raw_state.get("seen", {})
    return {
        "last_run_date": raw_state.get("last_run_date", ""),
        "seen": {
            "changelog": list(seen.get("changelog", [])),
            "blog": list(seen.get("blog", [])),
            "x": list(seen.get("x", [])),
        },
    }


def save_state(state: dict[str, Any]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def parse_changelog_rss(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []

    for node in root.findall("./channel/item"):
        title = collapse_whitespace(node.findtext("title", default=""))
        url = collapse_whitespace(node.findtext("link", default=""))
        published_at = format_timestamp(parse_rfc2822(node.findtext("pubDate", default="")))
        summary = strip_html(node.findtext("description", default=""))
        items.append(
            UpdateItem(
                source="changelog",
                item_id=url or title,
                title=title or "Untitled changelog entry",
                url=url,
                published_at=published_at,
                summary=trim_text(summary),
            )
        )

    return sorted(items, key=lambda item: item.published_dt(), reverse=True)


def parse_blog_sitemap(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    entries: list[dict[str, str]] = []

    for node in root.findall("sm:url", namespace):
        url = collapse_whitespace(node.findtext("sm:loc", default="", namespaces=namespace))
        lastmod = collapse_whitespace(node.findtext("sm:lastmod", default="", namespaces=namespace))
        if not url.startswith("https://cursor.com/blog/"):
            continue
        entries.append({"url": url, "lastmod": lastmod})

    entries.sort(key=lambda item: item["lastmod"], reverse=True)
    return entries


def extract_meta_tag(html_text: str, *, property_name: str | None = None, name: str | None = None) -> str:
    if property_name:
        pattern = rf'<meta[^>]+property=["\']{re.escape(property_name)}["\'][^>]+content=["\']([^"\']+)["\']'
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return collapse_whitespace(match.group(1))
    if name:
        pattern = rf'<meta[^>]+name=["\']{re.escape(name)}["\'][^>]+content=["\']([^"\']+)["\']'
        match = re.search(pattern, html_text, flags=re.IGNORECASE)
        if match:
            return collapse_whitespace(match.group(1))
    return ""


def extract_title(html_text: str) -> str:
    match = re.search(r"<title>(.*?)</title>", html_text, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    title = collapse_whitespace(match.group(1))
    return re.sub(r"\s*\|\s*Cursor.*$", "", title).strip()


def clean_blog_title(title: str) -> str:
    return re.sub(r"\s*[·|]\s*Cursor.*$", "", collapse_whitespace(title)).strip()


def build_blog_item(url: str, lastmod: str) -> UpdateItem:
    page = fetch_text(url)
    title = clean_blog_title(
        extract_meta_tag(page, property_name="og:title")
        or extract_meta_tag(page, name="twitter:title")
        or extract_title(page)
        or url.rsplit("/", 1)[-1].replace("-", " ").title()
    )
    summary = (
        extract_meta_tag(page, name="description")
        or extract_meta_tag(page, property_name="og:description")
        or "Cursor blog update"
    )

    return UpdateItem(
        source="blog",
        item_id=url,
        title=title,
        url=url,
        published_at=format_timestamp(parse_iso8601(lastmod)),
        summary=trim_text(summary),
    )


def parse_x_timeline_html(html_text: str) -> list[UpdateItem]:
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_text,
        flags=re.DOTALL,
    )
    if not match:
        raise ValueError("Could not find embedded timeline JSON in the X response.")

    payload = json.loads(match.group(1))
    entries = payload["props"]["pageProps"]["timeline"]["entries"]
    items: list[UpdateItem] = []

    for entry in entries:
        if entry.get("type") != "tweet":
            continue

        tweet = entry["content"]["tweet"]
        legacy = tweet.get("legacy", {})
        tweet_id = (
            str(tweet.get("rest_id") or tweet.get("id_str") or tweet.get("conversation_id_str") or "").strip()
        )
        text = legacy.get("full_text") or tweet.get("text") or ""
        entities = legacy.get("entities") or tweet.get("entities") or {}
        replacements: dict[str, str] = {}

        for url_entity in entities.get("urls", []):
            short = url_entity.get("url")
            expanded = url_entity.get("expanded_url") or url_entity.get("expanded_url_https")
            if short and expanded:
                replacements[short] = expanded

        for media in entities.get("media", []):
            short = media.get("url")
            expanded = media.get("expanded_url")
            if short and expanded:
                replacements[short] = expanded

        for short, expanded in replacements.items():
            text = text.replace(short, expanded)

        text = re.sub(r"\s+", " ", text).strip()
        if not tweet_id or text.startswith("RT @"):
            continue
        created_at = legacy.get("created_at") or tweet.get("created_at")
        published_at = format_timestamp(parse_rfc2822(created_at))
        title = trim_text(text, limit=80) or "Cursor post on X"
        items.append(
            UpdateItem(
                source="x",
                item_id=tweet_id,
                title=title,
                url=f"https://x.com/cursor_ai/status/{tweet_id}",
                published_at=published_at,
                summary=trim_text(text, limit=500),
            )
        )

    unique_items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    for item in sorted(items, key=lambda entry: entry.published_dt(), reverse=True):
        if item.item_id in seen_ids:
            continue
        seen_ids.add(item.item_id)
        unique_items.append(item)
    return unique_items


def select_new_items(
    items: list[UpdateItem],
    seen_ids: list[str],
    *,
    now: datetime,
    bootstrap_days: int | None = None,
    bootstrap_limit: int | None = None,
) -> list[UpdateItem]:
    seen = set(seen_ids)
    if seen:
        return [item for item in items if item.item_id not in seen]

    selected = items
    if bootstrap_days is not None:
        threshold = now.astimezone(UTC) - timedelta(days=bootstrap_days)
        selected = [item for item in selected if item.published_dt() >= threshold]
    if bootstrap_limit is not None:
        selected = selected[:bootstrap_limit]
    return selected


def merge_seen(existing: list[str], latest: list[str], *, limit: int = 500) -> list[str]:
    return unique_preserving_order(latest + existing)[:limit]


def should_run(now: datetime, state: dict[str, Any], *, force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced run"

    local_now = now.astimezone(RUN_TZ)
    if local_now.hour != RUN_HOUR:
        return False, f"skipping: outside {RUN_HOUR:02d}:00 hour in {RUN_TZ.key}"

    local_date = local_now.date().isoformat()
    if state.get("last_run_date") == local_date:
        return False, f"skipping: already ran for {local_date}"

    return True, "scheduled run"


def render_section(title: str, items: list[UpdateItem], empty_message: str) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.append(f"- {empty_message}")
        lines.append("")
        return lines

    for item in items:
        published_local = item.published_dt().astimezone(RUN_TZ).strftime("%Y-%m-%d %H:%M %Z")
        lines.extend(
            [
                f"### [{item.title}]({item.url})",
                f"- 发布时间: {published_local}",
                f"- 摘要: {item.summary}",
                "",
            ]
        )
    return lines


def render_report(
    *,
    checked_at: datetime,
    mode: str,
    changelog_items: list[UpdateItem],
    blog_items: list[UpdateItem],
    x_items: list[UpdateItem],
) -> str:
    checked_local = checked_at.astimezone(RUN_TZ).strftime("%Y-%m-%d %H:%M %Z")
    checked_utc = checked_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Cursor Daily Updates",
        "",
        f"- 检查时间: {checked_local} ({checked_utc})",
        f"- 执行模式: {mode}",
        "- 来源: changelog / blog / official X",
        "",
    ]
    lines.extend(render_section("Changelog", changelog_items, "最近一次检查后没有新增 changelog。"))
    lines.extend(render_section("Blog", blog_items, "最近一次检查后没有新增 blog 文章。"))
    lines.extend(render_section("Official X", x_items, "最近一次检查后没有新增官方 X 发文。"))
    return "\n".join(lines).rstrip() + "\n"


def collect_updates(state: dict[str, Any], now: datetime) -> tuple[dict[str, list[UpdateItem]], dict[str, list[str]]]:
    changelog_items = parse_changelog_rss(fetch_text(CHANGELOG_RSS_URL))
    changelog_seen = state["seen"]["changelog"]
    new_changelog = select_new_items(changelog_items, changelog_seen, now=now, bootstrap_days=7)

    blog_entries = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
    blog_seen = set(state["seen"]["blog"])
    if blog_seen:
        candidate_blog_entries = [entry for entry in blog_entries if entry["url"] not in blog_seen][:10]
    else:
        threshold = now.astimezone(UTC) - timedelta(days=7)
        candidate_blog_entries = [
            entry for entry in blog_entries if parse_iso8601(entry["lastmod"]) >= threshold
        ]
    new_blog = [build_blog_item(entry["url"], entry["lastmod"]) for entry in candidate_blog_entries]

    x_items = parse_x_timeline_html(fetch_text(X_TIMELINE_URL))
    x_seen = state["seen"]["x"]
    new_x = select_new_items(x_items, x_seen, now=now, bootstrap_limit=8)

    all_ids = {
        "changelog": [item.item_id for item in changelog_items],
        "blog": [entry["url"] for entry in blog_entries],
        "x": [item.item_id for item in x_items],
    }
    new_items = {"changelog": new_changelog, "blog": new_blog, "x": new_x}
    return new_items, all_ids


def write_report(report: str) -> None:
    REPORT_PATH.write_text(report, encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X updates.")
    parser.add_argument("--force", action="store_true", help="Run immediately, ignoring the 09:00 schedule.")
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="Persist seen-state even during a forced run.",
    )
    return parser


def main() -> int:
    args = build_arg_parser().parse_args()
    state = load_state()
    now = datetime.now(UTC)
    should_execute, mode = should_run(now, state, force=args.force)

    if not should_execute:
        print(mode)
        return 0

    new_items, all_ids = collect_updates(state, now)
    report = render_report(
        checked_at=now,
        mode=mode,
        changelog_items=new_items["changelog"],
        blog_items=new_items["blog"],
        x_items=new_items["x"],
    )
    write_report(report)

    persist_state = (not args.force) or args.update_state
    if persist_state:
        if not args.force:
            local_date = now.astimezone(RUN_TZ).date().isoformat()
            state["last_run_date"] = local_date
        for source, ids in all_ids.items():
            state["seen"][source] = merge_seen(state["seen"][source], ids)
        save_state(state)

    print(
        "wrote report with "
        f"{len(new_items['changelog'])} changelog, "
        f"{len(new_items['blog'])} blog, "
        f"{len(new_items['x'])} X items"
    )
    if persist_state:
        print(f"updated state: {STATE_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
