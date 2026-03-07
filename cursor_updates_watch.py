#!/usr/bin/env python3
"""Watch official Cursor updates from changelog, blog, and X."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_MIRROR_URL = "https://r.jina.ai/http://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_LIMIT = 5
RETRYABLE_STATUS_CODES = {403, 429, 500, 502, 503, 504}
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) CursorUpdatesWatch/1.0"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check official Cursor changelog, blog, and X updates."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately and ignore the 09:00 Asia/Shanghai gate.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help="IANA timezone name for the scheduled 09:00 gate.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Number of latest changelog and blog posts to capture.",
    )
    parser.add_argument(
        "--state-dir",
        default=".cursor_updates",
        help="Directory for runtime state and generated reports.",
    )
    return parser.parse_args()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def fetch_text(
    url: str,
    *,
    timeout: int = 30,
    retries: int = 3,
    accept: str | None = None,
) -> str:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept

    last_error: Exception | None = None
    for attempt in range(retries):
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in RETRYABLE_STATUS_CODES or attempt == retries - 1:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == retries - 1:
                raise
        time.sleep(2**attempt)

    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=True) + "\n",
        encoding="utf-8",
    )


def should_run_now(
    now_local: datetime,
    state: dict[str, Any],
    *,
    force: bool = False,
) -> tuple[bool, str]:
    if force:
        return True, "forced run"

    if now_local.hour != 9:
        return False, f"local time {now_local:%Y-%m-%d %H:%M} is outside the 09:00 gate"

    scheduled_date = now_local.date().isoformat()
    if state.get("last_scheduled_date_local") == scheduled_date:
        return False, f"scheduled run already completed on {scheduled_date}"

    return True, "within the scheduled 09:00 gate"


def parse_sitemap_entries(xml_text: str, section: str) -> list[dict[str, str]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    path_prefix = f"/{section}/"
    entries: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for node in root.findall("sm:url", namespace):
        loc = node.findtext("sm:loc", default="", namespaces=namespace).strip()
        if not loc or loc in seen_urls:
            continue

        parsed = urllib.parse.urlparse(loc)
        if parsed.netloc not in {"cursor.com", "www.cursor.com"}:
            continue
        if not parsed.path.startswith(path_prefix):
            continue

        lastmod = node.findtext("sm:lastmod", default="", namespaces=namespace).strip()
        entries.append({"url": loc, "lastmod": lastmod})
        seen_urls.add(loc)

    entries.sort(
        key=lambda item: (
            parse_iso_datetime(item["lastmod"])
            if item["lastmod"]
            else datetime.min.replace(tzinfo=timezone.utc),
            item["url"],
        ),
        reverse=True,
    )
    return entries


def mirror_url_for(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    suffix = parsed.path or "/"
    if parsed.query:
        suffix = f"{suffix}?{parsed.query}"
    return f"https://r.jina.ai/http://{parsed.netloc}{suffix}"


def extract_header_value(text: str, key: str) -> str:
    pattern = rf"^{re.escape(key)}:\s*(.+)$"
    match = re.search(pattern, text, flags=re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_markdown_body(text: str) -> str:
    marker = "Markdown Content:"
    if marker not in text:
        return text
    return text.split(marker, 1)[1].lstrip()


def strip_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text)
    text = re.sub(r"^\*\s+", "", text)
    text = re.sub(r"^>\s*", "", text)
    return " ".join(text.split()).strip()


def is_page_noise(line: str) -> bool:
    if not line:
        return True
    if re.fullmatch(r"[=-]{3,}", line):
        return True
    if line.startswith("[![Image"):
        return True
    if line.startswith("![Image"):
        return True
    if line.startswith("[]("):
        return True
    if line.startswith("*   "):
        return True
    if line.startswith("[Skip to content]"):
        return True
    if line.startswith("[Cursor]"):
        return True
    if line.startswith("[Sign in]"):
        return True
    if line.startswith("### Product"):
        return True
    if line in {"Cursor", "Product", "Enterprise", "Pricing", "Resources", "Changelog"}:
        return True
    if line.endswith("↓") or line.endswith("→"):
        return True
    if re.fullmatch(r"[A-Z][a-z]{2} \d{1,2}, \d{4} .+", line):
        return True
    return False


def extract_summary(section: str, title: str, body: str) -> str:
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if section == "changelog":
        title_key = strip_markdown(title)
        for index, line in enumerate(lines):
            if strip_markdown(line) == title_key:
                lines = lines[index + 1 :]
                break

    summary_lines: list[str] = []
    for line in lines:
        if is_page_noise(line):
            continue
        cleaned = strip_markdown(line)
        if not cleaned or cleaned == title:
            continue
        if cleaned.startswith("Next post"):
            break
        summary_lines.append(cleaned)
        if len(summary_lines) >= 3 or len(" ".join(summary_lines)) >= 360:
            break

    return " ".join(summary_lines)


def fetch_section_entries(section: str, limit: int) -> list[dict[str, str]]:
    sitemap_text = fetch_text(SITEMAP_URL)
    entries = parse_sitemap_entries(sitemap_text, section)[:limit]
    results: list[dict[str, str]] = []

    for entry in entries:
        page_text = fetch_text(mirror_url_for(entry["url"]), accept="text/plain")
        title = extract_header_value(page_text, "Title")
        if title.endswith(" · Cursor"):
            title = title[: -len(" · Cursor")]
        published = extract_header_value(page_text, "Published Time") or entry["lastmod"]
        body = extract_markdown_body(page_text)
        results.append(
            {
                "title": title or entry["url"].rsplit("/", 1)[-1],
                "url": entry["url"],
                "published": published,
                "summary": extract_summary(section, title, body),
            }
        )

    return results


def is_x_noise(line: str) -> bool:
    if not line:
        return True
    if re.fullmatch(r"[=-]{3,}", line):
        return True
    if line.startswith("[![Image"):
        return True
    if line.startswith("![Image"):
        return True
    if line in {
        "Cursor",
        "@cursor_ai",
        "Cursor's posts",
        "Cursor’s posts",
        "The best way to code with AI.",
        "Pinned",
    }:
        return True
    if re.fullmatch(r"\d+:\d+", line):
        return True
    return False


def extract_x_posts(text: str, limit: int = 8) -> list[str]:
    body = extract_markdown_body(text)
    posts: list[str] = []
    seen: set[str] = set()

    for raw_line in body.splitlines():
        line = raw_line.strip()
        if is_x_noise(line):
            continue
        cleaned = strip_markdown(line)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        posts.append(cleaned)
        if len(posts) >= limit:
            break

    return posts


def fetch_x_updates(limit: int = 8) -> dict[str, Any]:
    text = fetch_text(X_MIRROR_URL, accept="text/plain")
    return {
        "published_time": extract_header_value(text, "Published Time"),
        "profile_url": "https://x.com/cursor_ai",
        "posts": extract_x_posts(text, limit=limit),
    }


def compute_new_entries(
    previous: list[dict[str, Any]],
    current: list[dict[str, Any]],
    key: str,
) -> list[dict[str, Any]]:
    previous_values = {item.get(key, "") for item in previous}
    return [item for item in current if item.get(key, "") not in previous_values]


def compute_new_posts(previous: list[str], current: list[str]) -> list[str]:
    previous_values = set(previous)
    return [item for item in current if item not in previous_values]


def build_report(
    *,
    generated_at: datetime,
    timezone_name: str,
    mode: str,
    changelog: list[dict[str, str]],
    blog: list[dict[str, str]],
    x_updates: dict[str, Any],
    previous_state: dict[str, Any],
) -> str:
    previous_latest = previous_state.get("latest", {})
    new_changelog = compute_new_entries(
        previous_latest.get("changelog", []), changelog, "url"
    )
    new_blog = compute_new_entries(previous_latest.get("blog", []), blog, "url")
    new_x_posts = compute_new_posts(
        previous_latest.get("x_posts", []), x_updates.get("posts", [])
    )

    lines = [
        "# Cursor updates report",
        "",
        f"- Generated at: {isoformat_utc(generated_at)}",
        f"- Timezone gate: {timezone_name}",
        f"- Run mode: {mode}",
        "",
        "## New since the last successful run",
    ]

    if not any([new_changelog, new_blog, new_x_posts]):
        lines.append("- No new changelog entries, blog posts, or visible X posts were detected.")
    else:
        if new_changelog:
            lines.append("- Changelog:")
            for item in new_changelog:
                lines.append(f"  - {item['published']} | {item['title']} | {item['url']}")
        if new_blog:
            lines.append("- Blog:")
            for item in new_blog:
                lines.append(f"  - {item['published']} | {item['title']} | {item['url']}")
        if new_x_posts:
            lines.append("- X:")
            for item in new_x_posts:
                lines.append(f"  - {item}")

    lines.extend(["", "## Latest changelog entries"])
    for item in changelog:
        lines.append(f"- {item['published']} | {item['title']} | {item['url']}")
        if item.get("summary"):
            lines.append(f"  - {item['summary']}")

    lines.extend(["", "## Latest blog posts"])
    for item in blog:
        lines.append(f"- {item['published']} | {item['title']} | {item['url']}")
        if item.get("summary"):
            lines.append(f"  - {item['summary']}")

    lines.extend(
        [
            "",
            "## Latest official X posts",
            f"- Account: Cursor (@cursor_ai) | {x_updates['profile_url']}",
            f"- Mirror published time: {x_updates.get('published_time', '')}",
        ]
    )
    for item in x_updates.get("posts", []):
        lines.append(f"- {item}")

    return "\n".join(lines) + "\n"


def save_report(state_dir: Path, report: str, now_local: datetime) -> tuple[Path, Path]:
    latest_path = state_dir / "latest_report.md"
    history_path = state_dir / "history" / f"{now_local.date().isoformat()}.md"
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.write_text(report, encoding="utf-8")
    history_path.write_text(report, encoding="utf-8")
    return latest_path, history_path


def run(args: argparse.Namespace) -> int:
    state_dir = Path(args.state_dir)
    state_path = state_dir / "state.json"
    state = load_state(state_path)

    now_utc = utc_now()
    now_local = now_utc.astimezone(ZoneInfo(args.timezone))
    should_run, reason = should_run_now(now_local, state, force=args.force)

    if not should_run:
        print(f"Skipping Cursor updates check: {reason}.")
        return 0

    changelog = fetch_section_entries("changelog", args.limit)
    blog = fetch_section_entries("blog", args.limit)
    x_updates = fetch_x_updates(limit=max(8, args.limit + 3))

    mode = "forced" if args.force else "scheduled"
    report = build_report(
        generated_at=now_utc,
        timezone_name=args.timezone,
        mode=mode,
        changelog=changelog,
        blog=blog,
        x_updates=x_updates,
        previous_state=state,
    )
    latest_path, history_path = save_report(state_dir, report, now_local)

    next_state = {
        "last_success_at": isoformat_utc(now_utc),
        "last_run_mode": mode,
        "last_reason": reason,
        "latest_report_path": str(latest_path),
        "history_report_path": str(history_path),
        "latest": {
            "changelog": changelog,
            "blog": blog,
            "x_posts": x_updates["posts"],
            "x_published_time": x_updates.get("published_time", ""),
        },
    }
    if not args.force:
        next_state["last_scheduled_date_local"] = now_local.date().isoformat()
    else:
        next_state["last_scheduled_date_local"] = state.get("last_scheduled_date_local", "")

    write_json(state_path, next_state)
    print(report)
    return 0


def main() -> int:
    args = parse_args()
    try:
        return run(args)
    except Exception as exc:  # pragma: no cover - surfaced in CLI output
        print(f"Cursor updates watch failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
