from __future__ import annotations

import argparse
import html
import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


TIMEZONE_NAME = "Asia/Shanghai"
RUN_HOUR = 9
MAX_ITEMS = 5
SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0"}
RETRYABLE_HTTP_CODES = {429, 500, 502, 503, 504}


@dataclass(frozen=True)
class RuntimePaths:
    runtime_dir: Path
    state_path: Path
    latest_report_path: Path
    history_dir: Path


def default_paths(base_dir: Path | None = None) -> RuntimePaths:
    root = (base_dir or Path(__file__).resolve().parent).resolve()
    runtime_dir = root / ".cursor_updates"
    return RuntimePaths(
        runtime_dir=runtime_dir,
        state_path=runtime_dir / "state.json",
        latest_report_path=runtime_dir / "latest_report.md",
        history_dir=runtime_dir / "history",
    )


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now(now: datetime | None = None) -> datetime:
    current = now or utc_now()
    return current.astimezone(ZoneInfo(TIMEZONE_NAME))


def ensure_runtime_dirs(paths: RuntimePaths) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    paths.history_dir.mkdir(parents=True, exist_ok=True)


def load_state(paths: RuntimePaths) -> dict:
    if not paths.state_path.exists():
        return {
            "last_run_utc": None,
            "last_successful_scheduled_date": None,
            "latest": {"changelog": [], "blog": [], "x_posts": []},
            "recent_runs": [],
        }
    return json.loads(paths.state_path.read_text(encoding="utf-8"))


def save_state(paths: RuntimePaths, state: dict) -> None:
    paths.state_path.write_text(
        json.dumps(state, indent=2, ensure_ascii=True, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def fetch_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 30,
    retries: int = 3,
    backoff_seconds: float = 1.0,
) -> str:
    merged_headers = dict(DEFAULT_HEADERS)
    if headers:
        merged_headers.update(headers)

    for attempt in range(retries + 1):
        request = Request(url, headers=merged_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            if exc.code not in RETRYABLE_HTTP_CODES or attempt >= retries:
                raise
        except URLError:
            if attempt >= retries:
                raise
        time.sleep(backoff_seconds * (2**attempt))

    raise RuntimeError(f"Failed to fetch {url}")


def parse_sitemap_entries(xml_text: str) -> list[dict[str, str]]:
    root = ET.fromstring(xml_text)
    namespace_match = re.match(r"\{(.+)\}", root.tag)
    namespace = {"sm": namespace_match.group(1)} if namespace_match else {}
    url_tag = "sm:url" if namespace else "url"
    loc_tag = "sm:loc" if namespace else "loc"
    lastmod_tag = "sm:lastmod" if namespace else "lastmod"
    entries: list[dict[str, str]] = []

    for node in root.findall(url_tag, namespace):
        loc = node.findtext(loc_tag, default="", namespaces=namespace).strip()
        lastmod = node.findtext(lastmod_tag, default="", namespaces=namespace).strip()
        if loc:
            entries.append({"loc": loc, "lastmod": lastmod})

    return entries


def select_section_entries(
    sitemap_entries: list[dict[str, str]],
    section: str,
    *,
    limit: int = MAX_ITEMS,
) -> list[dict[str, str]]:
    pattern = re.compile(rf"^https://cursor\.com/{section}/[^/]+/?$")
    selected = [entry for entry in sitemap_entries if pattern.match(entry["loc"])]
    selected.sort(key=lambda item: item.get("lastmod", ""), reverse=True)
    return selected[:limit]


def normalize_date(value: str) -> str:
    if not value:
        return ""
    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        return value[:10]


def parse_title_from_html(page_text: str) -> str:
    patterns = (
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']',
        r"<title>(.*?)</title>",
    )
    for pattern in patterns:
        match = re.search(pattern, page_text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        title = html.unescape(re.sub(r"\s+", " ", match.group(1))).strip()
        title = re.sub(r"\s*(?:\||-|·)\s*Cursor$", "", title).strip()
        if title:
            return title
    return ""


def fallback_title(url: str) -> str:
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    if re.search(r"[A-Za-z]", slug):
        return slug.replace("-", " ").replace("_", " ").strip().title()
    return url


def build_section_items(
    sitemap_entries: list[dict[str, str]],
    section: str,
    fetcher: Callable[..., str],
    *,
    limit: int = MAX_ITEMS,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for entry in select_section_entries(sitemap_entries, section, limit=limit):
        title = ""
        try:
            title = parse_title_from_html(fetcher(entry["loc"]))
        except Exception:
            title = ""
        items.append(
            {
                "published": normalize_date(entry.get("lastmod", "")),
                "title": title or fallback_title(entry["loc"]),
                "url": entry["loc"],
            }
        )
    return items


def parse_x_published_time(markdown_text: str) -> str:
    match = re.search(r"^Published Time:\s*(.+)$", markdown_text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def parse_x_posts(markdown_text: str, *, limit: int = MAX_ITEMS + 5) -> list[str]:
    normalized = markdown_text.replace("\u2019", "'")
    lines = [line.strip() for line in normalized.splitlines()]
    started = False
    current_block: list[str] = []
    blocks: list[str] = []

    def flush_block() -> None:
        if not current_block:
            return
        text = " ".join(current_block)
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            blocks.append(text)
        current_block.clear()

    for line in lines:
        if not started:
            if line == "Cursor's posts":
                started = True
            continue

        if not line or line == "--------------" or line == "Pinned":
            continue
        if line.startswith("[![Image"):
            flush_block()
            continue
        if line.startswith("![Image"):
            continue
        if re.fullmatch(r"\d+:\d+", line):
            continue
        if line in {"Cursor", "@cursor_ai"}:
            continue
        current_block.append(line)

    flush_block()

    unique_blocks: list[str] = []
    seen: set[str] = set()
    for block in blocks:
        if block not in seen:
            seen.add(block)
            unique_blocks.append(block)
        if len(unique_blocks) >= limit:
            break

    return unique_blocks


def compute_new_items(current: dict, previous: dict) -> dict:
    previous_changelog_urls = {item["url"] for item in previous.get("changelog", [])}
    previous_blog_urls = {item["url"] for item in previous.get("blog", [])}
    previous_x_posts = set(previous.get("x_posts", []))

    return {
        "changelog": [
            item for item in current["changelog"] if item["url"] not in previous_changelog_urls
        ],
        "blog": [item for item in current["blog"] if item["url"] not in previous_blog_urls],
        "x_posts": [post for post in current["x_posts"] if post not in previous_x_posts],
    }


def should_run(
    now: datetime,
    state: dict,
    *,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        return True, "Forced run requested."

    shanghai_time = local_now(now)
    if shanghai_time.hour != RUN_HOUR:
        return (
            False,
            f"Skipping scheduled run at {shanghai_time.isoformat()} because it is outside 09:00 {TIMEZONE_NAME}.",
        )

    last_scheduled_date = state.get("last_successful_scheduled_date")
    current_date = shanghai_time.date().isoformat()
    if last_scheduled_date == current_date:
        return (
            False,
            f"Skipping scheduled run because {current_date} already completed successfully.",
        )

    return True, f"Scheduled run allowed for {current_date} at {shanghai_time.isoformat()}."


def build_report(
    *,
    now: datetime,
    force: bool,
    snapshot: dict,
    new_items: dict,
    x_published_time: str,
) -> str:
    shanghai_time = local_now(now)
    run_type = "forced" if force else "scheduled"
    lines = [
        "# Cursor updates report",
        "",
        f"- Run type: {run_type}",
        f"- Generated at (UTC): {now.isoformat()}",
        f"- Local time ({TIMEZONE_NAME}): {shanghai_time.isoformat()}",
        f"- X mirror published time: {x_published_time or 'unknown'}",
        "",
        "## New since previous snapshot",
        "",
    ]

    if new_items["changelog"]:
        lines.append("### Changelog")
        lines.extend(
            f"- {item['published']} | {item['title']} | {item['url']}"
            for item in new_items["changelog"]
        )
        lines.append("")

    if new_items["blog"]:
        lines.append("### Blog")
        lines.extend(
            f"- {item['published']} | {item['title']} | {item['url']}"
            for item in new_items["blog"]
        )
        lines.append("")

    if new_items["x_posts"]:
        lines.append("### Official X")
        lines.extend(f"- {post}" for post in new_items["x_posts"])
        lines.append("")

    if not any(new_items.values()):
        lines.append("- No newly observed items since the previous snapshot.")
        lines.append("")

    lines.extend(
        [
            "## Latest changelog",
            "",
            *[
                f"- {item['published']} | {item['title']} | {item['url']}"
                for item in snapshot["changelog"]
            ],
            "",
            "## Latest blog",
            "",
            *[
                f"- {item['published']} | {item['title']} | {item['url']}"
                for item in snapshot["blog"]
            ],
            "",
            "## Official X posts",
            "",
            *[f"- {post}" for post in snapshot["x_posts"]],
            "",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def write_report(paths: RuntimePaths, now: datetime, report: str, *, force: bool) -> None:
    paths.latest_report_path.write_text(report, encoding="utf-8")
    local_date = local_now(now).date().isoformat()
    history_name = (
        f"{local_date}-force-{local_now(now).strftime('%H%M%S')}.md"
        if force
        else f"{local_date}.md"
    )
    (paths.history_dir / history_name).write_text(report, encoding="utf-8")


def fetch_snapshot(fetcher: Callable[..., str], *, limit: int = MAX_ITEMS) -> tuple[dict, str]:
    sitemap_entries = parse_sitemap_entries(fetcher(SITEMAP_URL))
    x_markdown = fetcher(X_MIRROR_URL)
    snapshot = {
        "changelog": build_section_items(sitemap_entries, "changelog", fetcher, limit=limit),
        "blog": build_section_items(sitemap_entries, "blog", fetcher, limit=limit),
        "x_posts": parse_x_posts(x_markdown, limit=max(limit + 5, 10)),
    }
    return snapshot, parse_x_published_time(x_markdown)


def run(
    *,
    force: bool = False,
    now: datetime | None = None,
    paths: RuntimePaths | None = None,
    limit: int = MAX_ITEMS,
    fetcher: Callable[..., str] = fetch_text,
) -> tuple[int, str]:
    current_time = now or utc_now()
    runtime_paths = paths or default_paths()
    ensure_runtime_dirs(runtime_paths)
    state = load_state(runtime_paths)

    allowed, message = should_run(current_time, state, force=force)
    if not allowed:
        return 0, message

    snapshot, x_published_time = fetch_snapshot(fetcher, limit=limit)
    new_items = compute_new_items(snapshot, state.get("latest", {}))
    report = build_report(
        now=current_time,
        force=force,
        snapshot=snapshot,
        new_items=new_items,
        x_published_time=x_published_time,
    )
    write_report(runtime_paths, current_time, report, force=force)

    state["last_run_utc"] = current_time.isoformat()
    if not force:
        state["last_successful_scheduled_date"] = local_now(current_time).date().isoformat()
    state["latest"] = snapshot
    state["recent_runs"] = (
        [
            {
                "timestamp_utc": current_time.isoformat(),
                "run_type": "forced" if force else "scheduled",
                "report_path": str(runtime_paths.latest_report_path),
            }
        ]
        + state.get("recent_runs", [])
    )[:20]
    save_state(runtime_paths, state)
    return 0, report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Watch Cursor changelog, blog, and official X updates."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately and do not consume the scheduled 09:00 slot.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=MAX_ITEMS,
        help="Maximum number of changelog and blog entries to include.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    status_code, output = run(force=args.force, limit=max(1, args.limit))
    print(output)
    return status_code


if __name__ == "__main__":
    raise SystemExit(main())
