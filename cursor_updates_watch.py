#!/usr/bin/env python3
from __future__ import annotations

import argparse
import html
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo


MARKETING_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_PROFILE_URL = "https://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_HOUR = 9
DEFAULT_RECENT_LIMIT = 5
DEFAULT_DISCOVERY_LIMIT = 20
DEFAULT_X_POST_LIMIT = 20
MAX_SEEN_IDS = 500
RETRYABLE_HTTP_STATUSES = {403, 429, 500, 502, 503, 504}
USER_AGENT = "Mozilla/5.0 (compatible; cursor-updates-watch/1.0)"


@dataclass(frozen=True)
class SourceLink:
    source: str
    url: str
    lastmod: str


@dataclass(frozen=True)
class UpdateItem:
    source: str
    item_id: str
    title: str
    url: str
    published_at: str
    summary: str


def workspace_root() -> Path:
    return Path(__file__).resolve().parent


def state_dir() -> Path:
    return workspace_root() / ".cursor_updates"


def state_file() -> Path:
    return state_dir() / "state.json"


def latest_report_file() -> Path:
    return state_dir() / "latest_report.md"


def root_report_file() -> Path:
    return workspace_root() / "cursor_updates.md"


def read_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "last_successful_run_date": None,
            "seen": {"blog": [], "changelog": [], "x": []},
        }
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data.setdefault("last_successful_run_date", None)
    seen = data.setdefault("seen", {})
    seen.setdefault("blog", [])
    seen.setdefault("changelog", [])
    seen.setdefault("x", [])
    return data


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")


def should_run_now(
    now: datetime,
    last_successful_run_date: str | None,
    scheduled_hour: int,
) -> tuple[bool, str]:
    if now.hour < scheduled_hour:
        return (
            False,
            f"Current local time {now.strftime('%H:%M')} is before the scheduled hour {scheduled_hour:02d}:00.",
        )
    today = now.date().isoformat()
    if last_successful_run_date == today:
        return False, f"The scheduled run for {today} already completed."
    return True, f"Running the first successful check after {scheduled_hour:02d}:00 for {today}."


def is_retryable_error(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in RETRYABLE_HTTP_STATUSES
    return isinstance(error, urllib.error.URLError)


def http_get_text(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
    retries: int = 3,
) -> str:
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)

    for attempt in range(1, retries + 1):
        request = urllib.request.Request(url, headers=merged_headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.read().decode("utf-8", "ignore")
        except Exception as error:  # pragma: no cover - exercised via helper tests
            if attempt >= retries or not is_retryable_error(error):
                raise
            time.sleep(2 ** (attempt - 1))
    raise RuntimeError(f"Failed to fetch {url}")


def http_get_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 25,
    retries: int = 3,
) -> Any:
    return json.loads(http_get_text(url, headers=headers, timeout=timeout, retries=retries))


def parse_iso_datetime(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value)


def normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def strip_html(fragment: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", fragment)
    return normalize_whitespace(html.unescape(without_tags))


def canonicalize_url(url: str) -> str:
    cleaned = url.split("#", 1)[0]
    cleaned = cleaned.split("?", 1)[0]
    if cleaned.endswith("/"):
        cleaned = cleaned[:-1]
    return cleaned


def parse_marketing_sitemap(xml_text: str) -> dict[str, list[SourceLink]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    grouped: dict[str, list[SourceLink]] = {"blog": [], "changelog": []}

    for url_node in root.findall("sm:url", namespace):
        loc = url_node.findtext("sm:loc", default="", namespaces=namespace).strip()
        lastmod = url_node.findtext("sm:lastmod", default="", namespaces=namespace).strip()
        canonical = canonicalize_url(loc)

        if re.fullmatch(r"https://cursor\.com/blog/[^/]+", canonical):
            grouped["blog"].append(SourceLink(source="blog", url=canonical, lastmod=lastmod))
        elif re.fullmatch(r"https://cursor\.com/changelog/[^/]+", canonical):
            grouped["changelog"].append(SourceLink(source="changelog", url=canonical, lastmod=lastmod))

    for source in grouped:
        grouped[source].sort(key=lambda item: item.lastmod or "", reverse=True)
    return grouped


def extract_first_json_ld(html_text: str, desired_type: str) -> dict[str, Any] | None:
    matches = re.findall(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>',
        html_text,
        re.S,
    )
    for match in matches:
        try:
            payload = json.loads(match)
        except json.JSONDecodeError:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for candidate in candidates:
            candidate_type = candidate.get("@type")
            if candidate_type == desired_type:
                return candidate
            if isinstance(candidate_type, list) and desired_type in candidate_type:
                return candidate
    return None


def parse_blog_item(url: str, html_text: str) -> UpdateItem:
    posting = extract_first_json_ld(html_text, "BlogPosting")
    if posting:
        return UpdateItem(
            source="blog",
            item_id=url,
            title=normalize_whitespace(posting.get("headline", "")) or url.rsplit("/", 1)[-1],
            url=url,
            published_at=posting.get("datePublished", ""),
            summary=normalize_whitespace(posting.get("description", "")),
        )

    title_match = re.search(r"<title>(.*?)</title>", html_text, re.S)
    time_match = re.search(r'<time[^>]+dateTime="([^"]+)"', html_text)
    description_match = re.search(r'<meta[^>]+name="description"[^>]+content="([^"]+)"', html_text)
    return UpdateItem(
        source="blog",
        item_id=url,
        title=strip_html(title_match.group(1)) if title_match else url.rsplit("/", 1)[-1],
        url=url,
        published_at=time_match.group(1) if time_match else "",
        summary=normalize_whitespace(description_match.group(1)) if description_match else "",
    )


def parse_changelog_item(url: str, html_text: str) -> UpdateItem:
    title_match = re.search(r"<title>(.*?)</title>", html_text, re.S)
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html_text, re.S)
    time_match = re.search(r'<time[^>]+dateTime="([^"]+)"', html_text)
    summary_match = re.search(r'<div class="prose[^"]*">\s*<p>(.*?)</p>', html_text, re.S)

    raw_title = ""
    if title_match:
        raw_title = strip_html(title_match.group(1))
        raw_title = re.sub(r"\s*[|·-]\s*Cursor\s*$", "", raw_title)
    elif h1_match:
        raw_title = strip_html(h1_match.group(1))

    return UpdateItem(
        source="changelog",
        item_id=url,
        title=raw_title or url.rsplit("/", 1)[-1],
        url=url,
        published_at=time_match.group(1) if time_match else "",
        summary=strip_html(summary_match.group(1)) if summary_match else "",
    )


def discover_cursor_site_updates() -> dict[str, list[SourceLink]]:
    sitemap_text = http_get_text(MARKETING_SITEMAP_URL)
    return parse_marketing_sitemap(sitemap_text)


def fetch_item_details(links: Iterable[SourceLink]) -> tuple[dict[str, UpdateItem], list[str]]:
    details: dict[str, UpdateItem] = {}
    failures: list[str] = []
    for link in links:
        try:
            html_text = http_get_text(link.url)
            if link.source == "blog":
                item = parse_blog_item(link.url, html_text)
            else:
                item = parse_changelog_item(link.url, html_text)

            if not item.published_at and link.lastmod:
                item = UpdateItem(
                    source=item.source,
                    item_id=item.item_id,
                    title=item.title,
                    url=item.url,
                    published_at=link.lastmod,
                    summary=item.summary,
                )
            details[link.url] = item
        except Exception as error:
            failures.append(f"{link.url}: {error}")
    return details, failures


def extract_guest_token(profile_html: str) -> str:
    match = re.search(r'document\.cookie="gt=([^;]+);', profile_html)
    if not match:
        raise ValueError("Could not extract X guest token from the profile page.")
    return match.group(1)


def extract_main_bundle_url(profile_html: str) -> str:
    match = re.search(
        r'<script[^>]+src="(https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js)"',
        profile_html,
    )
    if not match:
        raise ValueError("Could not locate the X main JavaScript bundle.")
    return match.group(1)


def extract_bearer_token(bundle_text: str) -> str:
    match = re.search(r"Bearer ([A-Za-z0-9%_-]+)", bundle_text)
    if not match:
        raise ValueError("Could not locate the X guest bearer token.")
    return urllib.parse.unquote(match.group(1))


def extract_query_id(bundle_text: str, operation_name: str) -> str:
    pattern = rf'queryId:"([A-Za-z0-9_-]+)",operationName:"{re.escape(operation_name)}"'
    match = re.search(pattern, bundle_text)
    if not match:
        raise ValueError(f"Could not locate the query id for {operation_name}.")
    return match.group(1)


def x_headers(bearer_token: str, guest_token: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {bearer_token}",
        "x-guest-token": guest_token,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "Accept": "application/json",
    }


def x_graphql_url(query_id: str, operation_name: str, variables: dict[str, Any]) -> str:
    params = urllib.parse.urlencode(
        {"variables": json.dumps(variables, separators=(",", ":"))}
    )
    return f"https://x.com/i/api/graphql/{query_id}/{operation_name}?{params}"


def unwrap_tweet_result(tweet_result: dict[str, Any]) -> dict[str, Any] | None:
    current = tweet_result
    while isinstance(current, dict) and current.get("__typename") == "TweetWithVisibilityResults":
        current = current.get("tweet")
    if not isinstance(current, dict):
        return None
    if current.get("__typename") in {"TweetTombstone", "TweetUnavailable"}:
        return None
    return current


def extract_note_tweet_text(tweet: dict[str, Any]) -> str:
    note_result = (
        tweet.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
    )
    return normalize_whitespace(note_result.get("text", ""))


def extract_tweet_text(tweet: dict[str, Any]) -> str:
    note_text = extract_note_tweet_text(tweet)
    if note_text:
        return note_text
    legacy = tweet.get("legacy", {})
    return normalize_whitespace(legacy.get("full_text", ""))


def iter_tweet_results_from_content(content: dict[str, Any]) -> Iterable[dict[str, Any]]:
    if not isinstance(content, dict):
        return

    item_content = content.get("itemContent")
    if isinstance(item_content, dict):
        tweet_result = item_content.get("tweet_results", {}).get("result")
        if isinstance(tweet_result, dict):
            yield tweet_result

    for item in content.get("items", []):
        nested = item.get("item", {}).get("itemContent") or item.get("itemContent")
        if isinstance(nested, dict):
            tweet_result = nested.get("tweet_results", {}).get("result")
            if isinstance(tweet_result, dict):
                yield tweet_result


def parse_x_timeline(payload: dict[str, Any], screen_name: str) -> list[UpdateItem]:
    instructions = (
        payload.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )

    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    canonical_screen_name = screen_name.lower()

    for instruction in instructions:
        entries: list[dict[str, Any]] = []
        if isinstance(instruction.get("entry"), dict):
            entries.append(instruction["entry"])
        for entry in instruction.get("entries", []):
            if isinstance(entry, dict):
                entries.append(entry)

        for entry in entries:
            content = entry.get("content", {})
            for tweet_result in iter_tweet_results_from_content(content):
                tweet = unwrap_tweet_result(tweet_result)
                if not tweet:
                    continue

                core_user = (
                    tweet.get("core", {})
                    .get("user_results", {})
                    .get("result", {})
                    .get("core", {})
                )
                if core_user.get("screen_name", "").lower() != canonical_screen_name:
                    continue

                legacy = tweet.get("legacy", {})
                if legacy.get("retweeted_status_result"):
                    continue

                tweet_id = tweet.get("rest_id") or legacy.get("id_str")
                tweet_text = extract_tweet_text(tweet)
                if not tweet_id or not tweet_text or tweet_id in seen_ids:
                    continue

                seen_ids.add(tweet_id)
                created_at = legacy.get("created_at", "")
                published_at = (
                    datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y").isoformat()
                    if created_at
                    else ""
                )

                items.append(
                    UpdateItem(
                        source="x",
                        item_id=tweet_id,
                        title=tweet_text[:120],
                        url=f"https://x.com/{screen_name}/status/{tweet_id}",
                        published_at=published_at,
                        summary=tweet_text,
                    )
                )

    if not items:
        raise ValueError("The X timeline request succeeded but no visible posts were parsed.")
    return items


def fetch_x_posts(screen_name: str, count: int) -> list[UpdateItem]:
    profile_url = X_PROFILE_URL if screen_name == "cursor_ai" else f"https://x.com/{screen_name}"
    profile_html = http_get_text(profile_url)
    guest_token = extract_guest_token(profile_html)
    bundle_url = extract_main_bundle_url(profile_html)
    bundle_text = http_get_text(bundle_url)
    bearer_token = extract_bearer_token(bundle_text)
    user_query_id = extract_query_id(bundle_text, "UserByScreenName")
    tweets_query_id = extract_query_id(bundle_text, "UserTweets")

    headers = x_headers(bearer_token, guest_token)
    user_url = x_graphql_url(
        user_query_id,
        "UserByScreenName",
        {"screen_name": screen_name, "withSafetyModeUserFields": True},
    )
    user_payload = http_get_json(user_url, headers=headers)
    user_result = user_payload["data"]["user"]["result"]
    user_rest_id = user_result["rest_id"]

    tweets_url = x_graphql_url(
        tweets_query_id,
        "UserTweets",
        {
            "userId": user_rest_id,
            "count": count,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        },
    )
    timeline_payload = http_get_json(tweets_url, headers=headers)
    return parse_x_timeline(timeline_payload, screen_name)


def pick_detail_links(
    discovered: list[SourceLink],
    seen_ids: set[str],
    recent_limit: int,
    discovery_limit: int,
) -> tuple[list[SourceLink], list[SourceLink]]:
    recent = discovered[:recent_limit]
    new_links = [item for item in discovered if item.url not in seen_ids][:discovery_limit]

    deduped: dict[str, SourceLink] = {}
    for link in recent + new_links:
        deduped[link.url] = link

    ordered = list(deduped.values())
    return new_links, ordered


def format_timestamp(value: str, timezone_name: str) -> str:
    if not value:
        return "unknown"
    try:
        dt = parse_iso_datetime(value)
    except ValueError:
        return value
    return dt.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")


def render_items(items: list[UpdateItem], timezone_name: str) -> list[str]:
    lines: list[str] = []
    for item in items:
        lines.append(f"- {format_timestamp(item.published_at, timezone_name)} | [{item.title}]({item.url})")
        if item.summary:
            lines.append(f"  - {item.summary}")
    return lines


def render_report(
    *,
    now: datetime,
    timezone_name: str,
    scheduled_hour: int,
    new_items: dict[str, list[UpdateItem]],
    latest_items: dict[str, list[UpdateItem]],
    errors: dict[str, str],
) -> str:
    lines = [
        "# Cursor daily updates",
        "",
        f"- Generated at: {now.strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"- Schedule: first successful run each day at or after {scheduled_hour:02d}:00 ({timezone_name})",
        "- Sources: Cursor changelog, Cursor blog, official X @cursor_ai",
        "",
    ]

    if not any(new_items.values()):
        lines.append("No new items were detected since the last successful daily run.")
        lines.append("")

    section_labels = {
        "changelog": "New changelog entries",
        "blog": "New blog posts",
        "x": "New official X posts",
    }
    latest_labels = {
        "changelog": "Latest changelog snapshot",
        "blog": "Latest blog snapshot",
        "x": "Latest official X snapshot",
    }

    for source in ("changelog", "blog", "x"):
        lines.append(f"## {section_labels[source]}")
        if errors.get(source):
            lines.append(f"Source error: {errors[source]}")
        elif new_items[source]:
            lines.extend(render_items(new_items[source], timezone_name))
        else:
            lines.append("No new items.")
        lines.append("")

    for source in ("changelog", "blog", "x"):
        lines.append(f"## {latest_labels[source]}")
        if errors.get(source):
            lines.append(f"Source error: {errors[source]}")
        elif latest_items[source]:
            lines.extend(render_items(latest_items[source], timezone_name))
        else:
            lines.append("No items available.")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def trim_seen_ids(values: Iterable[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value and value not in seen:
            seen.add(value)
            deduped.append(value)
        if len(deduped) >= MAX_SEEN_IDS:
            break
    return deduped


def write_report(markdown: str) -> None:
    latest_report_file().parent.mkdir(parents=True, exist_ok=True)
    latest_report_file().write_text(markdown, encoding="utf-8")
    root_report_file().write_text(markdown, encoding="utf-8")


def collect_updates(
    *,
    seen_state: dict[str, list[str]],
    recent_limit: int,
    discovery_limit: int,
    x_post_limit: int,
) -> tuple[dict[str, list[UpdateItem]], dict[str, list[UpdateItem]], dict[str, list[str]], dict[str, str]]:
    errors: dict[str, str] = {}
    new_items: dict[str, list[UpdateItem]] = {"blog": [], "changelog": [], "x": []}
    latest_items: dict[str, list[UpdateItem]] = {"blog": [], "changelog": [], "x": []}
    current_ids: dict[str, list[str]] = {"blog": [], "changelog": [], "x": []}

    try:
        discovered = discover_cursor_site_updates()
    except Exception as error:
        discovered = {"blog": [], "changelog": []}
        errors["blog"] = str(error)
        errors["changelog"] = str(error)

    for source in ("blog", "changelog"):
        if errors.get(source):
            continue
        source_links = discovered[source]
        current_ids[source] = [link.url for link in source_links]
        seen_ids = set(seen_state.get(source, []))
        new_links, detail_links = pick_detail_links(
            source_links,
            seen_ids,
            recent_limit=recent_limit,
            discovery_limit=discovery_limit,
        )
        if not detail_links:
            continue
        details, failures = fetch_item_details(detail_links)
        latest_items[source] = [details[link.url] for link in detail_links[:recent_limit] if link.url in details]
        new_items[source] = [details[link.url] for link in new_links if link.url in details]
        if failures:
            errors[source] = failures[0] if not details else f"Partial fetch failure: {failures[0]}"

    try:
        x_items = fetch_x_posts("cursor_ai", x_post_limit)
        latest_items["x"] = x_items[:recent_limit]
        current_ids["x"] = [item.item_id for item in x_items]
        seen_x = set(seen_state.get("x", []))
        new_items["x"] = [item for item in x_items if item.item_id not in seen_x][:discovery_limit]
    except Exception as error:
        errors["x"] = str(error)

    return new_items, latest_items, current_ids, errors


def run(force: bool, timezone_name: str, scheduled_hour: int) -> int:
    zone = ZoneInfo(timezone_name)
    now = datetime.now(zone)

    state = read_state(state_file())
    should_run, reason = should_run_now(
        now,
        state.get("last_successful_run_date"),
        scheduled_hour,
    )
    if not should_run and not force:
        print(reason)
        return 0

    try:
        new_items, latest_items, current_ids, errors = collect_updates(
            seen_state=state["seen"],
            recent_limit=DEFAULT_RECENT_LIMIT,
            discovery_limit=DEFAULT_DISCOVERY_LIMIT,
            x_post_limit=DEFAULT_X_POST_LIMIT,
        )
    except Exception as error:
        errors = {"runtime": str(error)}
        latest_items = {"blog": [], "changelog": [], "x": []}
        new_items = {"blog": [], "changelog": [], "x": []}
        current_ids = {"blog": [], "changelog": [], "x": []}

    report = render_report(
        now=now,
        timezone_name=timezone_name,
        scheduled_hour=scheduled_hour,
        new_items=new_items,
        latest_items=latest_items,
        errors=errors,
    )
    write_report(report)
    print(report)

    if errors:
        if "runtime" in errors:
            print(f"Fatal error: {errors['runtime']}", file=sys.stderr)
        return 1

    if force:
        return 0

    merged_seen = {
        "blog": trim_seen_ids(current_ids["blog"] + state["seen"]["blog"]),
        "changelog": trim_seen_ids(current_ids["changelog"] + state["seen"]["changelog"]),
        "x": trim_seen_ids(current_ids["x"] + state["seen"]["x"]),
    }
    state["seen"] = merged_seen
    if not force:
        state["last_successful_run_date"] = now.date().isoformat()
    write_state(state_file(), state)
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Check Cursor changelog, blog, and official X updates."
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run immediately and do not consume today's scheduled slot.",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TIMEZONE,
        help=f"Timezone for the daily schedule (default: {DEFAULT_TIMEZONE}).",
    )
    parser.add_argument(
        "--hour",
        type=int,
        default=DEFAULT_HOUR,
        help=f"Local scheduled hour in 24h format (default: {DEFAULT_HOUR}).",
    )
    args = parser.parse_args(argv)
    if not 0 <= args.hour <= 23:
        parser.error("--hour must be between 0 and 23")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return run(force=args.force, timezone_name=args.timezone, scheduled_hour=args.hour)


if __name__ == "__main__":
    raise SystemExit(main())
