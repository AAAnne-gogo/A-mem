#!/usr/bin/env python3
"""Fetch daily Cursor updates from official public sources.

This watcher is intended to run on an hourly automation trigger but only
produces an update during the 09:00 hour in Asia/Shanghai unless --force is
used.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / ".cursor_updates"
STATE_FILE = STATE_DIR / "state.json"
REPORT_FILE = ROOT / "cursor_updates.md"

SCHEDULE_TZ = ZoneInfo("Asia/Shanghai")
SCHEDULE_HOUR = 9
BOOTSTRAP_DAYS = 7
SEEN_LIMIT = 500
DEFAULT_TIMEOUT = 30
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)

CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_PROFILE_URL = "https://x.com/cursor_ai"
X_SYNDICATION_URL = (
    "https://syndication.twitter.com/srv/timeline-profile/"
    "screen-name/cursor_ai?lang=en&showReplies=false"
)
X_SCREEN_NAME = "cursor_ai"
X_MAIN_SCRIPT_RE = re.compile(
    r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js'
)
X_BEARER_TOKEN_RE = re.compile(r"AAAAAA[A-Za-z0-9%_-]{50,}")
X_GUEST_TOKEN_RE = re.compile(r'document\.cookie="gt=(\d+);')


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def now_in_schedule_tz() -> datetime:
    return utc_now().astimezone(SCHEDULE_TZ)


def fetch_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
        payload = response.read()
        charset = response.headers.get_content_charset() or "utf-8"
    return payload.decode(charset, "replace")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    return " ".join(value.split())


def parse_rfc2822(value: str | None) -> datetime | None:
    if not value:
        return None
    return parsedate_to_datetime(value).astimezone(timezone.utc)


def parse_iso8601(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = value.replace("Z", "+00:00")
    return datetime.fromisoformat(normalized).astimezone(timezone.utc)


def format_timestamp(value: datetime) -> str:
    return value.astimezone(SCHEDULE_TZ).strftime("%Y-%m-%d %H:%M %Z")


def isoformat_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_state(path: Path = STATE_FILE) -> dict[str, Any]:
    if not path.exists():
        return {
            "last_run_date": None,
            "last_checked_at": None,
            "seen": {"changelog": [], "blog": [], "x": []},
        }

    raw = json.loads(path.read_text(encoding="utf-8"))
    seen = raw.get("seen", {})
    return {
        "last_run_date": raw.get("last_run_date"),
        "last_checked_at": raw.get("last_checked_at"),
        "seen": {
            "changelog": list(seen.get("changelog", [])),
            "blog": list(seen.get("blog", [])),
            "x": list(seen.get("x", [])),
        },
    }


def save_state(state: dict[str, Any], path: Path = STATE_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def should_run(now_local: datetime, state: dict[str, Any], force: bool) -> tuple[bool, str]:
    if force:
        return True, "forced run"
    if now_local.hour != SCHEDULE_HOUR:
        return False, f"outside scheduled hour ({now_local.hour:02d}:00 {now_local.tzname()})"

    today = now_local.date().isoformat()
    if state.get("last_run_date") == today:
        return False, f"already ran for {today}"
    return True, "scheduled run"


def keep_recent_ids(new_ids: list[str], existing_ids: list[str]) -> list[str]:
    merged: list[str] = []
    for value in [*new_ids, *existing_ids]:
        if value and value not in merged:
            merged.append(value)
        if len(merged) >= SEEN_LIMIT:
            break
    return merged


def select_items(
    items: list[dict[str, Any]],
    seen_ids: list[str],
    id_key: str,
    now_utc: datetime,
    last_checked_at: str | None = None,
) -> list[dict[str, Any]]:
    if seen_ids:
        seen_set = set(seen_ids)
        last_checked = parse_iso8601(last_checked_at)
        selected = []
        for item in items:
            if item[id_key] in seen_set:
                continue
            published_at = parse_iso8601(item.get("published_at"))
            if last_checked and published_at and published_at <= last_checked:
                continue
            selected.append(item)
        return selected

    cutoff = now_utc - timedelta(days=BOOTSTRAP_DAYS)
    selected = []
    for item in items:
        published_at = parse_iso8601(item.get("published_at"))
        if published_at and published_at >= cutoff:
            selected.append(item)
    return selected


def parse_changelog_feed(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    items: list[dict[str, Any]] = []
    for node in root.findall("./channel/item"):
        link = (node.findtext("link") or "").strip()
        title = clean_text(node.findtext("title"))
        published_at = parse_rfc2822(node.findtext("pubDate"))
        description = clean_text(node.findtext("description"))
        guid = clean_text(node.findtext("guid")) or link
        if not link or not title or published_at is None:
            continue
        items.append(
            {
                "id": guid,
                "title": title,
                "url": link,
                "published_at": isoformat_utc(published_at),
                "summary": description,
            }
        )

    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items


def fetch_changelog_items() -> list[dict[str, Any]]:
    return parse_changelog_feed(fetch_text(CHANGELOG_RSS_URL))


def normalize_blog_url(url: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    normalized_path = parsed.path.rstrip("/")
    if not normalized_path:
        normalized_path = "/"
    return urllib.parse.urlunsplit(("https", "cursor.com", normalized_path, "", ""))


def parse_blog_sitemap(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    items: list[dict[str, Any]] = []
    for node in root.findall("sm:url", ns):
        loc = (node.findtext("sm:loc", namespaces=ns) or "").strip()
        lastmod = (node.findtext("sm:lastmod", namespaces=ns) or "").strip()
        if not loc:
            continue
        normalized = normalize_blog_url(loc)
        parsed = urllib.parse.urlsplit(normalized)
        if parsed.path == "/blog":
            continue
        if not parsed.path.startswith("/blog/"):
            continue
        slug = parsed.path[len("/blog/") :]
        if not slug or "/" in slug:
            continue
        items.append({"url": normalized, "lastmod": lastmod})

    items.sort(key=lambda item: item["lastmod"], reverse=True)
    return items


class BlogMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.og_title = ""
        self.og_description = ""
        self.meta_description = ""
        self.published_at = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key.lower(): (value or "") for key, value in attrs}
        if tag == "meta":
            name = attr_map.get("name", "").lower()
            prop = attr_map.get("property", "").lower()
            content = attr_map.get("content", "")
            if prop == "og:title" and content:
                self.og_title = content
            elif prop == "og:description" and content:
                self.og_description = content
            elif name == "description" and content:
                self.meta_description = content
        elif tag == "time" and attr_map.get("datetime"):
            self.published_at = attr_map["datetime"]


def parse_blog_article(
    url: str,
    html_text: str,
    fallback_published_at: str | None = None,
) -> dict[str, Any]:
    parser = BlogMetadataParser()
    parser.feed(html_text)

    title = clean_text(parser.og_title)
    title = re.sub(r"\s*[·|-]\s*Cursor\s*$", "", title).strip()
    summary = clean_text(parser.og_description or parser.meta_description)
    published_at = parse_iso8601(parser.published_at) or parse_iso8601(fallback_published_at)
    if not title or published_at is None:
        raise ValueError(f"Unable to parse Cursor blog metadata for {url}")

    return {
        "id": url,
        "title": title,
        "url": url,
        "published_at": isoformat_utc(published_at),
        "summary": summary,
    }


def fetch_blog_items(state: dict[str, Any], now_utc: datetime) -> list[dict[str, Any]]:
    sitemap_items = parse_blog_sitemap(fetch_text(BLOG_SITEMAP_URL))
    seen_urls = set(state["seen"]["blog"])
    last_checked = parse_iso8601(state.get("last_checked_at"))
    candidates: list[dict[str, Any]] = []

    if seen_urls:
        for item in sitemap_items:
            if item["url"] in seen_urls:
                continue
            lastmod = parse_iso8601(item.get("lastmod"))
            if last_checked and lastmod and lastmod <= last_checked:
                continue
            candidates.append(item)
    else:
        cutoff = now_utc - timedelta(days=BOOTSTRAP_DAYS)
        candidates = [
            item
            for item in sitemap_items
            if parse_iso8601(item.get("lastmod")) and parse_iso8601(item["lastmod"]) >= cutoff
        ]

    items: list[dict[str, Any]] = []
    for candidate in candidates:
        html_text = fetch_text(candidate["url"])
        items.append(parse_blog_article(candidate["url"], html_text, candidate.get("lastmod")))

    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items


def parse_js_string_array(value: str) -> list[str]:
    return re.findall(r'"([^"]+)"', value)


def extract_x_main_script_url(html_text: str) -> str:
    match = X_MAIN_SCRIPT_RE.search(html_text)
    if not match:
        raise ValueError("Unable to locate the X main script URL")
    return match.group(0)


def extract_x_guest_token(html_text: str) -> str:
    match = X_GUEST_TOKEN_RE.search(html_text)
    if not match:
        raise ValueError("Unable to locate the X guest token")
    return match.group(1)


def extract_x_bearer_token(js_text: str) -> str:
    match = X_BEARER_TOKEN_RE.search(js_text)
    if not match:
        raise ValueError("Unable to locate the X bearer token")
    return urllib.parse.unquote(match.group(0))


def extract_x_graphql_operation(js_text: str, operation_name: str) -> dict[str, Any]:
    pattern = re.compile(
        rf'queryId:"([^"]+)",operationName:"{re.escape(operation_name)}".*?'
        r'metadata:\{featureSwitches:\[(.*?)\],fieldToggles:\[(.*?)\]\}',
        re.DOTALL,
    )
    match = pattern.search(js_text)
    if not match:
        raise ValueError(f"Unable to locate X GraphQL operation metadata for {operation_name}")
    return {
        "query_id": match.group(1),
        "operation_name": operation_name,
        "features": parse_js_string_array(match.group(2)),
        "field_toggles": parse_js_string_array(match.group(3)),
    }


def build_x_graphql_context() -> dict[str, Any]:
    profile_html = fetch_text(X_PROFILE_URL)
    guest_token = extract_x_guest_token(profile_html)
    main_script_url = extract_x_main_script_url(profile_html)
    main_script = fetch_text(main_script_url)
    return {
        "guest_token": guest_token,
        "bearer_token": extract_x_bearer_token(main_script),
        "operations": {
            "UserByScreenName": extract_x_graphql_operation(main_script, "UserByScreenName"),
            "UserTweets": extract_x_graphql_operation(main_script, "UserTweets"),
        },
    }


def fetch_x_graphql_json(
    context: dict[str, Any],
    operation_name: str,
    variables: dict[str, Any],
) -> dict[str, Any]:
    operation = context["operations"][operation_name]
    params: dict[str, str] = {
        "variables": json.dumps(variables, separators=(",", ":")),
        "features": json.dumps(
            {feature: True for feature in operation["features"]},
            separators=(",", ":"),
        ),
    }
    if operation["field_toggles"]:
        params["fieldToggles"] = json.dumps(
            {toggle: True for toggle in operation["field_toggles"]},
            separators=(",", ":"),
        )

    url = (
        f"https://x.com/i/api/graphql/{operation['query_id']}/"
        f"{operation['operation_name']}?{urllib.parse.urlencode(params)}"
    )
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Authorization": f"Bearer {context['bearer_token']}",
            "x-guest-token": context["guest_token"],
            "x-twitter-active-user": "yes",
            "x-twitter-client-language": "en",
        },
    )
    with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        payload = response.read().decode(charset, "replace")
    return json.loads(payload)


def iter_x_tweet_results(node: Any) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    if isinstance(node, dict):
        tweet_results = node.get("tweet_results")
        if isinstance(tweet_results, dict):
            matches.append(tweet_results)
        for value in node.values():
            matches.extend(iter_x_tweet_results(value))
    elif isinstance(node, list):
        for item in node:
            matches.extend(iter_x_tweet_results(item))
    return matches


def unwrap_x_tweet_result(result: Any) -> dict[str, Any] | None:
    current = result
    while isinstance(current, dict):
        typename = current.get("__typename")
        if typename == "Tweet":
            return current
        if typename == "TweetWithVisibilityResults":
            current = current.get("tweet")
            continue
        if typename in {"TweetTombstone", "TweetUnavailable"}:
            return None
        if isinstance(current.get("result"), dict):
            current = current["result"]
            continue
        if isinstance(current.get("tweet"), dict):
            current = current["tweet"]
            continue
        return None
    return None


def parse_x_graphql_timeline(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for wrapper in iter_x_tweet_results(payload):
        tweet = unwrap_x_tweet_result(wrapper.get("result"))
        if not tweet:
            continue

        legacy = tweet.get("legacy", {})
        tweet_id = str(tweet.get("rest_id") or legacy.get("id_str") or "").strip()
        full_text = str(legacy.get("full_text") or legacy.get("text") or "").strip()
        created_at = parse_rfc2822(legacy.get("created_at"))
        user_result = tweet.get("core", {}).get("user_results", {}).get("result", {})
        screen_name = (
            user_result.get("core", {}).get("screen_name")
            or user_result.get("legacy", {}).get("screen_name")
            or ""
        )

        if not tweet_id or not full_text or created_at is None or screen_name.lower() != X_SCREEN_NAME:
            continue
        if full_text.startswith("RT @"):
            continue
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)

        replacements: list[tuple[int, int, str]] = []
        entities = legacy.get("entities", {})
        for url_entity in entities.get("urls", []):
            indices = url_entity.get("indices") or []
            if len(indices) != 2:
                continue
            replacements.append(
                (
                    int(indices[0]),
                    int(indices[1]),
                    str(url_entity.get("expanded_url") or url_entity.get("display_url") or "").strip(),
                )
            )

        media_entities = []
        media_entities.extend(entities.get("media", []))
        media_entities.extend(legacy.get("extended_entities", {}).get("media", []))
        dedup_media: set[tuple[int, int]] = set()
        for media in media_entities:
            indices = media.get("indices") or []
            if len(indices) != 2:
                continue
            key = (int(indices[0]), int(indices[1]))
            if key in dedup_media:
                continue
            dedup_media.add(key)
            replacements.append((key[0], key[1], ""))

        text = apply_entity_replacements(full_text, replacements)
        items.append(
            {
                "id": tweet_id,
                "title": truncate_text(text),
                "url": f"https://x.com/{screen_name}/status/{tweet_id}",
                "published_at": isoformat_utc(created_at),
                "summary": text,
            }
        )

    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items


def fetch_x_items_via_graphql() -> list[dict[str, Any]]:
    context = build_x_graphql_context()
    user_payload = fetch_x_graphql_json(
        context,
        "UserByScreenName",
        {"screen_name": X_SCREEN_NAME, "withSafetyModeUserFields": True},
    )
    user = user_payload["data"]["user"]["result"]
    user_rest_id = str(user.get("rest_id") or "").strip()
    if not user_rest_id:
        raise ValueError("Unable to resolve the Cursor X account rest_id")

    tweets_payload = fetch_x_graphql_json(
        context,
        "UserTweets",
        {
            "userId": user_rest_id,
            "count": 20,
            "includePromotedContent": False,
            "withQuickPromoteEligibilityTweetFields": True,
            "withVoice": True,
            "withV2Timeline": True,
        },
    )
    return parse_x_graphql_timeline(tweets_payload)


def extract_next_data_json(html_text: str) -> dict[str, Any]:
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html_text,
        re.DOTALL,
    )
    if not match:
        raise ValueError("Unable to locate __NEXT_DATA__ payload in X timeline page")
    return json.loads(match.group(1))


def apply_entity_replacements(text: str, replacements: list[tuple[int, int, str]]) -> str:
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    return " ".join(text.split())


def truncate_text(text: str, limit: int = 110) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def parse_x_timeline_page(html_text: str) -> list[dict[str, Any]]:
    payload = extract_next_data_json(html_text)
    entries = payload["props"]["pageProps"]["timeline"]["entries"]
    items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for entry in entries:
        if entry.get("type") != "tweet":
            continue
        tweet = entry.get("content", {}).get("tweet", {})
        tweet_id = str(tweet.get("id_str") or tweet.get("conversation_id_str") or "").strip()
        permalink = str(tweet.get("permalink") or "").strip()
        full_text = str(tweet.get("full_text") or tweet.get("text") or "").strip()
        created_at = parse_rfc2822(tweet.get("created_at"))
        screen_name = (
            tweet.get("user", {}).get("screen_name")
            or tweet.get("legacy", {}).get("screen_name")
            or ""
        )
        if not tweet_id or not full_text or created_at is None or screen_name.lower() != "cursor_ai":
            continue
        if full_text.startswith("RT @"):
            continue
        if tweet_id in seen_ids:
            continue
        seen_ids.add(tweet_id)

        replacements: list[tuple[int, int, str]] = []
        entities = tweet.get("entities", {})
        for url_entity in entities.get("urls", []):
            indices = url_entity.get("indices") or []
            if len(indices) != 2:
                continue
            replacements.append(
                (
                    int(indices[0]),
                    int(indices[1]),
                    str(url_entity.get("expanded_url") or url_entity.get("display_url") or "").strip(),
                )
            )
        media_entities = []
        media_entities.extend(entities.get("media", []))
        media_entities.extend(tweet.get("extended_entities", {}).get("media", []))
        dedup_media: set[tuple[int, int]] = set()
        for media in media_entities:
            indices = media.get("indices") or []
            if len(indices) != 2:
                continue
            key = (int(indices[0]), int(indices[1]))
            if key in dedup_media:
                continue
            dedup_media.add(key)
            replacements.append((key[0], key[1], ""))

        text = apply_entity_replacements(full_text, replacements)
        items.append(
            {
                "id": tweet_id,
                "title": truncate_text(text),
                "url": urllib.parse.urljoin("https://x.com", permalink or f"/cursor_ai/status/{tweet_id}"),
                "published_at": isoformat_utc(created_at),
                "summary": text,
            }
        )

    items.sort(key=lambda item: item["published_at"], reverse=True)
    return items


def fetch_x_items() -> list[dict[str, Any]]:
    try:
        return fetch_x_items_via_graphql()
    except Exception:
        return parse_x_timeline_page(fetch_text(X_SYNDICATION_URL))


def render_item(item: dict[str, Any]) -> str:
    published_at = parse_iso8601(item["published_at"])
    published_label = format_timestamp(published_at) if published_at else item["published_at"]
    lines = [
        f"- [{item['title']}]({item['url']})",
        f"  - Published: {published_label}",
    ]
    summary = item.get("summary", "").strip()
    if summary and summary != item["title"]:
        lines.append(f"  - Summary: {summary}")
    return "\n".join(lines)


def render_section(title: str, items: list[dict[str, Any]], empty_message: str) -> str:
    lines = [f"## {title} ({len(items)})", ""]
    if not items:
        lines.append(empty_message)
        lines.append("")
        return "\n".join(lines)
    for item in items:
        lines.append(render_item(item))
        lines.append("")
    return "\n".join(lines)


def render_report(
    generated_at: datetime,
    changelog_items: list[dict[str, Any]],
    blog_items: list[dict[str, Any]],
    x_items: list[dict[str, Any]],
    run_reason: str,
) -> str:
    total_items = len(changelog_items) + len(blog_items) + len(x_items)
    lines = [
        "# Cursor updates watch",
        "",
        f"Generated at: {format_timestamp(generated_at)}",
        f"Run reason: {run_reason}",
        "Sources:",
        f"- Changelog RSS: {CHANGELOG_RSS_URL}",
        f"- Blog sitemap: {BLOG_SITEMAP_URL}",
        f"- Official X timeline: {X_PROFILE_URL}",
        "",
        (
            "No new items were found in this run."
            if total_items == 0
            else f"Found {total_items} new item(s) in this run."
        ),
        "",
        render_section("Changelog", changelog_items, "No new changelog entries."),
        render_section("Blog", blog_items, "No new blog posts."),
        render_section("Official X posts (@cursor_ai)", x_items, "No new official X posts."),
    ]
    return "\n".join(lines).rstrip() + "\n"


def persist_run_state(
    state: dict[str, Any],
    changelog_items: list[dict[str, Any]],
    blog_items: list[dict[str, Any]],
    x_items: list[dict[str, Any]],
    now_utc: datetime,
    scheduled_run: bool,
) -> dict[str, Any]:
    next_state = {
        "last_run_date": state.get("last_run_date"),
        "last_checked_at": isoformat_utc(now_utc),
        "seen": {
            "changelog": keep_recent_ids(
                [item["id"] for item in changelog_items],
                state["seen"]["changelog"],
            ),
            "blog": keep_recent_ids(
                [item["id"] for item in blog_items],
                state["seen"]["blog"],
            ),
            "x": keep_recent_ids(
                [item["id"] for item in x_items],
                state["seen"]["x"],
            ),
        },
    }
    if scheduled_run:
        next_state["last_run_date"] = now_utc.astimezone(SCHEDULE_TZ).date().isoformat()
    return next_state


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="Run regardless of schedule gating.")
    parser.add_argument(
        "--update-state",
        action="store_true",
        help="Persist seen-state for a forced run as well.",
    )
    args = parser.parse_args(argv)

    state = load_state()
    now_utc = utc_now()
    now_local = now_utc.astimezone(SCHEDULE_TZ)
    should_execute, reason = should_run(now_local, state, force=args.force)
    if not should_execute:
        print(f"Skip: {reason}.")
        return 0

    changelog_all = fetch_changelog_items()
    blog_all = fetch_blog_items(state, now_utc)
    x_all = fetch_x_items()

    changelog_items = select_items(
        changelog_all,
        state["seen"]["changelog"],
        "id",
        now_utc,
        state.get("last_checked_at"),
    )
    blog_items = select_items(
        blog_all,
        state["seen"]["blog"],
        "id",
        now_utc,
        state.get("last_checked_at"),
    )
    x_items = select_items(
        x_all,
        state["seen"]["x"],
        "id",
        now_utc,
        state.get("last_checked_at"),
    )

    report = render_report(now_local, changelog_items, blog_items, x_items, reason)
    REPORT_FILE.write_text(report, encoding="utf-8")

    should_persist = not args.force or args.update_state
    if should_persist:
        next_state = persist_run_state(
            state=state,
            changelog_items=changelog_items,
            blog_items=blog_items,
            x_items=x_items,
            now_utc=now_utc,
            scheduled_run=not args.force,
        )
        save_state(next_state)

    print(
        "Updated report with "
        f"{len(changelog_items)} changelog, {len(blog_items)} blog, {len(x_items)} X post(s)."
    )
    if should_persist:
        print(f"State updated at {STATE_FILE}.")
    else:
        print("State not updated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
