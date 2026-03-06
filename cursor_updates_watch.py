#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo


SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
JINA_HTTP_PREFIX = "https://r.jina.ai/http://"
STATE_VERSION = 1
SHANGHAI = ZoneInfo("Asia/Shanghai")
RUNTIME_DIRNAME = ".cursor_updates"
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/plain, text/html;q=0.9, application/xml;q=0.8",
}


FetchText = Callable[[str], str]


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_z(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def to_jina_http_url(url: str) -> str:
    if url.startswith("https://"):
        return JINA_HTTP_PREFIX + url[len("https://") :]
    if url.startswith("http://"):
        return JINA_HTTP_PREFIX + url[len("http://") :]
    return JINA_HTTP_PREFIX + url


def fetch_text(url: str, timeout: int = 30, retries: int = 3) -> str:
    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers=HEADERS)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504}:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
        if attempt + 1 < retries:
            time.sleep(2**attempt)
    assert last_error is not None
    raise last_error


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"version": STATE_VERSION}
    return json.loads(state_path.read_text(encoding="utf-8"))


def dump_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def parse_sitemap_entries(xml_text: str, section: str) -> list[dict]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    entries: list[dict] = []
    for node in root.findall("sm:url", namespace):
        loc = node.findtext("sm:loc", default="", namespaces=namespace).strip()
        lastmod = node.findtext("sm:lastmod", default="", namespaces=namespace).strip()
        if f"/{section}/" not in loc:
            continue
        entries.append({"url": loc, "published_at": lastmod})
    entries.sort(key=lambda item: item["published_at"], reverse=True)
    return entries


def clean_title(title: str) -> str:
    cleaned = html.unescape(title).strip()
    cleaned = re.sub(r"\s*[·|]\s*Cursor\s*$", "", cleaned)
    return cleaned.strip()


def fallback_title_from_url(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    slug = slug.replace("-", " ").strip()
    if re.fullmatch(r"\d{2} \d{2} \d{2}", slug):
        return slug
    return slug.title()


def extract_title_from_mirror(markdown_text: str, url: str) -> str:
    match = re.search(r"^Title:\s*(.+?)\s*$", markdown_text, flags=re.MULTILINE)
    if match:
        return clean_title(match.group(1))
    return fallback_title_from_url(url)


def collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_summary(markdown_text: str, title: str) -> str:
    _, _, markdown = markdown_text.partition("Markdown Content:")
    lines = [line.strip() for line in markdown.splitlines()]
    blocks: list[str] = []
    current: list[str] = []
    for line in lines:
        if not line:
            if current:
                blocks.append(" ".join(current))
                current = []
            continue
        if set(line) == {"="} or set(line) == {"-"}:
            continue
        current.append(line)
    if current:
        blocks.append(" ".join(current))
    normalized_title = clean_title(title)
    for block in blocks:
        candidate = collapse_whitespace(block)
        if not candidate or candidate == normalized_title:
            continue
        if candidate.startswith("[") or candidate.startswith("http://") or candidate.startswith("https://"):
            continue
        if candidate in {"Changelog", "Cursor", "@cursor_ai"}:
            continue
        if "Skip to content" in candidate:
            continue
        if len(candidate) < 40:
            continue
        return candidate
    return ""


def enrich_entries(entries: list[dict], fetcher: FetchText, limit: int = 5) -> list[dict]:
    enriched: list[dict] = []
    for entry in entries[:limit]:
        mirror = fetcher(to_jina_http_url(entry["url"]))
        title = extract_title_from_mirror(mirror, entry["url"])
        enriched.append(
            {
                "title": title,
                "url": entry["url"],
                "published_at": entry["published_at"],
                "summary": extract_summary(mirror, title),
            }
        )
    return enriched


def unique_posts(posts: list[str], limit: int = 5) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for post in posts:
        normalized = collapse_whitespace(post)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(normalized)
        if len(deduped) >= limit:
            break
    return deduped


def parse_x_timeline(markdown_text: str) -> dict:
    title_match = re.search(r"^Title:\s*(.+?)\s*$", markdown_text, flags=re.MULTILINE)
    published_match = re.search(r"^Published Time:\s*(.+?)\s*$", markdown_text, flags=re.MULTILINE)
    account = title_match.group(1).strip() if title_match else "Cursor (@cursor_ai)"
    account = re.sub(r"\s*/\s*X\s*$", "", account)
    published_time = published_match.group(1).strip() if published_match else ""
    _, _, body = markdown_text.partition("Markdown Content:")
    lines = [line.rstrip() for line in body.splitlines()]
    in_posts = False
    posts: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        if stripped == "Cursor’s posts":
            in_posts = True
            index += 1
            continue
        if not in_posts:
            index += 1
            continue
        if not stripped or stripped in {"--------------", "Pinned"}:
            index += 1
            continue
        if stripped.startswith("[![Image"):
            block: list[str] = []
            inner = index + 1
            while inner < len(lines):
                candidate = lines[inner].strip()
                if candidate.startswith("[![Image"):
                    break
                inner += 1
                if not candidate or candidate in {"Pinned", "--------------"}:
                    continue
                if candidate.startswith("![Image"):
                    continue
                if re.fullmatch(r"\d+:\d+", candidate):
                    continue
                if candidate in {"Cursor", "@cursor_ai", "The best way to code with AI."}:
                    continue
                block.append(candidate)
            text = collapse_whitespace(" ".join(block))
            if text:
                posts.append(text)
            index = inner
            continue
        index += 1
    return {
        "account": account,
        "profile_url": "https://x.com/cursor_ai",
        "mirror_url": X_MIRROR_URL,
        "published_time": published_time,
        "posts": unique_posts(posts),
    }


def collect_updates(fetcher: FetchText = fetch_text) -> dict:
    sitemap = fetcher(SITEMAP_URL)
    changelog_entries = enrich_entries(parse_sitemap_entries(sitemap, "changelog"), fetcher)
    blog_entries = enrich_entries(parse_sitemap_entries(sitemap, "blog"), fetcher)
    x_timeline = parse_x_timeline(fetcher(X_MIRROR_URL))
    return {
        "changelog": changelog_entries,
        "blog": blog_entries,
        "x": x_timeline,
    }


def diff_entries(current: list[dict], previous: list[dict], key: str) -> list[dict]:
    previous_values = {item.get(key, "") for item in previous}
    return [item for item in current if item.get(key, "") not in previous_values]


def diff_posts(current: list[str], previous: list[str]) -> list[str]:
    previous_values = {collapse_whitespace(item) for item in previous}
    return [item for item in current if collapse_whitespace(item) not in previous_values]


def should_run(state: dict, current_utc: datetime, force: bool) -> tuple[bool, str]:
    current_cn = current_utc.astimezone(SHANGHAI)
    current_date = current_cn.date().isoformat()
    if force:
        return True, "forced"
    if current_cn.hour != 9:
        return False, (
            "Skipped scheduled run: "
            f"current Asia/Shanghai time is {current_cn.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
            "automation only runs at 09:00."
        )
    if state.get("last_successful_scheduled_date") == current_date:
        return False, f"Skipped scheduled run: already completed for {current_date} Asia/Shanghai."
    return True, "scheduled"


def render_entries(title: str, entries: list[dict], new_entries: list[dict]) -> list[str]:
    lines = [f"## {title}"]
    if not entries:
        lines.append("- No items fetched.")
        return lines
    if new_entries:
        lines.append(f"- New items since last successful run: {len(new_entries)}")
        for entry in new_entries:
            lines.append(f"  - {entry['published_at'][:10]} | {entry['title']} | {entry['url']}")
    else:
        lines.append("- No new items since last successful run.")
    lines.append("- Latest seen:")
    for entry in entries:
        lines.append(f"  - {entry['published_at'][:10]} | {entry['title']} | {entry['url']}")
    return lines


def render_x_section(current: dict, new_posts: list[str]) -> list[str]:
    lines = ["## Official X (@cursor_ai)"]
    lines.append(f"- Mirror published time: {current.get('published_time', 'unknown')}")
    if new_posts:
        lines.append(f"- New visible posts since last successful run: {len(new_posts)}")
        for post in new_posts:
            lines.append(f"  - {post}")
    else:
        lines.append("- No new visible posts since last successful run.")
    lines.append("- Latest visible posts snapshot:")
    for post in current.get("posts", []):
        lines.append(f"  - {post}")
    return lines


def build_report(current_utc: datetime, mode: str, current: dict, previous: dict) -> str:
    new_changelog = diff_entries(current["changelog"], previous.get("changelog", []), "url")
    new_blog = diff_entries(current["blog"], previous.get("blog", []), "url")
    previous_posts = previous.get("x", {}).get("posts", [])
    new_posts = diff_posts(current["x"].get("posts", []), previous_posts)
    header = [
        "# Cursor daily updates",
        "",
        f"- Checked at (UTC): {isoformat_z(current_utc)}",
        f"- Checked at (Asia/Shanghai): {current_utc.astimezone(SHANGHAI).strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Run mode: {mode}",
        "",
    ]
    body = (
        render_entries("Changelog", current["changelog"], new_changelog)
        + [""]
        + render_entries("Blog", current["blog"], new_blog)
        + [""]
        + render_x_section(current["x"], new_posts)
    )
    return "\n".join(header + body).rstrip() + "\n"


def persist_report(base_dir: Path, current_utc: datetime, report: str) -> tuple[Path, Path]:
    runtime_dir = base_dir / RUNTIME_DIRNAME
    history_dir = runtime_dir / "history"
    history_dir.mkdir(parents=True, exist_ok=True)
    latest_path = runtime_dir / "latest_report.md"
    current_date = current_utc.astimezone(SHANGHAI).date().isoformat()
    history_path = history_dir / f"{current_date}.md"
    latest_path.write_text(report, encoding="utf-8")
    history_path.write_text(report, encoding="utf-8")
    return latest_path, history_path


def run(base_dir: Path, force: bool = False, fetcher: FetchText = fetch_text, current_utc: datetime | None = None) -> tuple[int, str]:
    current_utc = current_utc or now_utc()
    runtime_dir = base_dir / RUNTIME_DIRNAME
    state_path = runtime_dir / "state.json"
    state = load_state(state_path)
    should_execute, mode_or_reason = should_run(state, current_utc, force)
    if not should_execute:
        return 0, mode_or_reason
    previous_snapshot = state.get("latest", {})
    current_snapshot = collect_updates(fetcher)
    report = build_report(current_utc, mode_or_reason, current_snapshot, previous_snapshot)
    latest_path, history_path = persist_report(base_dir, current_utc, report)
    state.update(
        {
            "version": STATE_VERSION,
            "last_checked_at": isoformat_z(current_utc),
            "last_successful_run_at": isoformat_z(current_utc),
            "last_successful_mode": mode_or_reason,
            "latest_report_path": str(latest_path),
            "latest_history_path": str(history_path),
            "latest": current_snapshot,
        }
    )
    if mode_or_reason == "scheduled":
        state["last_successful_scheduled_date"] = current_utc.astimezone(SHANGHAI).date().isoformat()
    dump_state(state_path, state)
    return 0, report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X updates.")
    parser.add_argument("--force", action="store_true", help="Run immediately without the 09:00 Asia/Shanghai gate.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        code, output = run(base_dir=Path.cwd(), force=args.force)
    except (urllib.error.URLError, ET.ParseError, json.JSONDecodeError, OSError) as exc:
        print(f"Cursor update watch failed: {exc}", file=sys.stderr)
        return 1
    print(output)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
