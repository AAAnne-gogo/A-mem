#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


CHANGELOG_RSS_URL = "https://cursor.com/changelog/rss.xml"
BLOG_SITEMAP_URL = "https://cursor.com/marketing/sitemap.xml"
X_PROFILE_URL = "https://x.com/cursor_ai"
DEFAULT_TIMEZONE = "Asia/Shanghai"
DEFAULT_OUTPUT_PATH = Path("/workspace/cursor_updates.md")
DEFAULT_STATE_PATH = Path("/workspace/.cursor_updates/state.json")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) CursorUpdatesWatch/1.0"
BOOTSTRAP_DAYS = 7
BOOTSTRAP_X_COUNT = 8
MAX_SEEN_ITEMS = 300
REQUEST_TIMEOUT = 30

X_TWEETS_FEATURES = {
    "rweb_video_screen_enabled": False,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": False,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": True,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "responsive_web_jetfuel_frame": False,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": True,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

X_TWEETS_FIELD_TOGGLES = {
    "withArticleRichContentState": True,
    "withArticlePlainText": False,
    "withAuxiliaryUserLabels": False,
}


@dataclass
class UpdateItem:
    source: str
    item_id: str
    title: str
    url: str
    published_at: str | None
    summary: str


@dataclass
class SourceResult:
    source: str
    fetched: list[UpdateItem]
    selected: list[UpdateItem]
    error: str | None = None


def now_in_timezone(timezone_name: str) -> datetime:
    return datetime.now(ZoneInfo(timezone_name))


def format_now(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S %Z")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    value = value.strip()
    if not value:
        return None

    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass

    try:
        normalized = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except ValueError:
        return None


def isoformat_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=True, indent=2, sort_keys=True)
        handle.write("\n")


def fetch_text(url: str, headers: dict[str, str] | None = None, timeout: int = REQUEST_TIMEOUT) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = REQUEST_TIMEOUT) -> Any:
    return json.loads(fetch_text(url, headers=headers, timeout=timeout))


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def shorten_text(value: str, limit: int = 280) -> str:
    value = value.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "..."


def trim_seen_items(items: list[str]) -> list[str]:
    deduped: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        deduped.append(item)
        seen.add(item)
        if len(deduped) >= MAX_SEEN_ITEMS:
            break
    return deduped


def merge_seen(previous: list[str], current: list[str]) -> list[str]:
    return trim_seen_items(current + previous)


def is_blog_article_url(url: str) -> bool:
    path = urlparse(url).path.rstrip("/")
    if not path:
        return False
    if path in {"/blog", "/en/blog"}:
        return False
    return bool(re.fullmatch(r"/(?:[A-Za-z-]+/)?blog/[^/]+", path))


def parse_changelog_items(xml_text: str) -> list[UpdateItem]:
    root = ET.fromstring(xml_text)
    items: list[UpdateItem] = []
    for node in root.findall("./channel/item"):
        title = (node.findtext("title") or "").strip()
        url = (node.findtext("link") or "").strip()
        if not title or not url:
            continue
        items.append(
            UpdateItem(
                source="changelog",
                item_id=url,
                title=title,
                url=url,
                published_at=isoformat_or_none(parse_datetime(node.findtext("pubDate"))),
                summary=shorten_text(clean_text(node.findtext("description"))),
            )
        )
    return items


def parse_blog_sitemap(xml_text: str) -> list[tuple[str, str | None]]:
    namespace = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    root = ET.fromstring(xml_text)
    candidates: list[tuple[str, str | None]] = []
    seen_urls: set[str] = set()
    for node in root.findall("sm:url", namespace):
        url = (node.findtext("sm:loc", default="", namespaces=namespace) or "").strip()
        lastmod = (node.findtext("sm:lastmod", default="", namespaces=namespace) or "").strip() or None
        if not url or not is_blog_article_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append((url, lastmod))

    def sort_key(item: tuple[str, str | None]) -> tuple[datetime, str]:
        published = parse_datetime(item[1]) or datetime.min.replace(tzinfo=timezone.utc)
        return (published, item[0])

    return sorted(candidates, key=sort_key, reverse=True)


def parse_blog_article(url: str, html_text: str, fallback_lastmod: str | None) -> UpdateItem | None:
    title_match = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', html_text)
    summary_match = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]+)"', html_text)
    time_match = re.search(r'<time[^>]*dateTime="([^"]+)"', html_text)
    published_json_match = re.search(r'"datePublished":"([^"]+)"', html_text)
    headline_match = re.search(r'"headline":"([^"]+)"', html_text)

    title = unescape((title_match.group(1) if title_match else "")).strip()
    if not title and headline_match:
        title = unescape(headline_match.group(1)).strip()
    if not title:
        return None
    if title.endswith(" · Cursor"):
        title = title[: -len(" · Cursor")].strip()

    published = (
        parse_datetime(time_match.group(1) if time_match else None)
        or parse_datetime(published_json_match.group(1) if published_json_match else None)
        or parse_datetime(fallback_lastmod)
    )
    summary = clean_text(summary_match.group(1) if summary_match else "")

    return UpdateItem(
        source="blog",
        item_id=url,
        title=title,
        url=url,
        published_at=isoformat_or_none(published),
        summary=shorten_text(summary),
    )


def extract_x_guest_token(profile_html: str) -> str:
    match = re.search(r'gt=(\d+);', profile_html)
    if not match:
        raise ValueError("Could not extract X guest token")
    return match.group(1)


def extract_x_main_js_url(profile_html: str) -> str:
    match = re.search(r'https://abs\.twimg\.com/responsive-web/client-web/main\.[^"]+\.js', profile_html)
    if not match:
        raise ValueError("Could not locate X main JS URL")
    return match.group(0)


def extract_x_bearer_token(main_js: str) -> str:
    match = re.search(r'Bearer ([A-Za-z0-9%\-_]+)', main_js)
    if not match:
        raise ValueError("Could not extract X bearer token")
    return match.group(1)


def extract_x_query_id(main_js: str, operation_name: str) -> str:
    pattern = rf'queryId:"([^"]+)",operationName:"{re.escape(operation_name)}"'
    match = re.search(pattern, main_js)
    if not match:
        raise ValueError(f"Could not extract X query id for {operation_name}")
    return match.group(1)


def build_x_headers(bearer_token: str, guest_token: str) -> dict[str, str]:
    return {
        "User-Agent": USER_AGENT,
        "Authorization": f"Bearer {bearer_token}",
        "x-guest-token": guest_token,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "Referer": X_PROFILE_URL,
    }


def x_api_url(query_id: str, operation_name: str, variables: dict[str, Any], features: dict[str, Any] | None = None,
              field_toggles: dict[str, Any] | None = None) -> str:
    params = {
        "variables": json.dumps(variables, separators=(",", ":")),
    }
    if features:
        params["features"] = json.dumps(features, separators=(",", ":"))
    if field_toggles:
        params["fieldToggles"] = json.dumps(field_toggles, separators=(",", ":"))
    return f"https://x.com/i/api/graphql/{query_id}/{operation_name}?{urllib.parse.urlencode(params)}"


def unwrap_tweet_result(result: dict[str, Any] | None) -> dict[str, Any] | None:
    current = result
    while isinstance(current, dict) and current.get("__typename") == "TweetWithVisibilityResults":
        current = current.get("tweet")
    if not isinstance(current, dict):
        return None
    if current.get("__typename") != "Tweet":
        return None
    return current


def iter_timeline_entries(payload: dict[str, Any]) -> list[dict[str, Any]]:
    instructions = (
        payload.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("timeline", {})
        .get("timeline", {})
        .get("instructions", [])
    )
    entries: list[dict[str, Any]] = []
    for instruction in instructions:
        if isinstance(instruction, dict):
            if "entry" in instruction and isinstance(instruction["entry"], dict):
                entries.append(instruction["entry"])
            for entry in instruction.get("entries", []):
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries


def expand_x_text(text: str, legacy: dict[str, Any]) -> str:
    expanded = text
    entities = legacy.get("entities", {})
    for url_entity in entities.get("urls", []):
        short = url_entity.get("url")
        replacement = url_entity.get("expanded_url") or url_entity.get("display_url") or short
        if short and replacement:
            expanded = expanded.replace(short, replacement)

    media_urls = {media.get("url") for media in entities.get("media", []) if media.get("url")}
    for media_url in media_urls:
        expanded = expanded.replace(media_url, "")

    expanded = unescape(expanded)
    expanded = re.sub(r"\s+", " ", expanded)
    return expanded.strip()


def parse_x_tweets(payload: dict[str, Any], screen_name: str) -> list[UpdateItem]:
    items: list[UpdateItem] = []
    seen_ids: set[str] = set()
    for entry in iter_timeline_entries(payload):
        content = entry.get("content", {})
        item_content = content.get("itemContent")
        if not isinstance(item_content, dict):
            continue

        tweet = unwrap_tweet_result(item_content.get("tweet_results", {}).get("result"))
        if not tweet:
            continue

        legacy = tweet.get("legacy", {})
        tweet_id = tweet.get("rest_id")
        full_text = legacy.get("full_text", "")
        if not tweet_id or not full_text or tweet_id in seen_ids:
            continue
        if "retweeted_status_result" in legacy or full_text.startswith("RT @"):
            continue

        seen_ids.add(tweet_id)
        items.append(
            UpdateItem(
                source="x",
                item_id=tweet_id,
                title=shorten_text(expand_x_text(full_text, legacy), 140),
                url=f"https://x.com/{screen_name}/status/{tweet_id}",
                published_at=isoformat_or_none(parse_datetime(legacy.get("created_at"))),
                summary=shorten_text(expand_x_text(full_text, legacy), 280),
            )
        )
    return items


def should_include_bootstrap_item(item: UpdateItem, now_utc: datetime) -> bool:
    published = parse_datetime(item.published_at)
    if not published:
        return False
    return published >= now_utc - timedelta(days=BOOTSTRAP_DAYS)


def select_items(fetched: list[UpdateItem], seen_ids: list[str], bootstrap_mode: str, now_utc: datetime) -> list[UpdateItem]:
    seen = set(seen_ids)
    if seen:
        return [item for item in fetched if item.item_id not in seen]
    if bootstrap_mode == "recent_days":
        return [item for item in fetched if should_include_bootstrap_item(item, now_utc)]
    if bootstrap_mode == "latest_x":
        return fetched[:BOOTSTRAP_X_COUNT]
    raise ValueError(f"Unsupported bootstrap mode: {bootstrap_mode}")


def fetch_changelog() -> list[UpdateItem]:
    return parse_changelog_items(fetch_text(CHANGELOG_RSS_URL, headers={"Accept": "application/rss+xml"}))


def fetch_blog() -> list[UpdateItem]:
    sitemap_text = fetch_text(BLOG_SITEMAP_URL, headers={"Accept": "application/xml,text/xml"})
    candidates = parse_blog_sitemap(sitemap_text)
    items: list[UpdateItem] = []
    for url, lastmod in candidates[:30]:
        article = parse_blog_article(url, fetch_text(url), lastmod)
        if article:
            items.append(article)

    items.sort(
        key=lambda item: parse_datetime(item.published_at) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    return items


def fetch_x_posts(screen_name: str = "cursor_ai") -> list[UpdateItem]:
    profile_html = fetch_text(X_PROFILE_URL)
    guest_token = extract_x_guest_token(profile_html)
    main_js_url = extract_x_main_js_url(profile_html)
    main_js = fetch_text(main_js_url, headers={"Referer": X_PROFILE_URL})
    bearer_token = extract_x_bearer_token(main_js)
    user_query_id = extract_x_query_id(main_js, "UserByScreenName")
    tweets_query_id = extract_x_query_id(main_js, "UserTweets")
    headers = build_x_headers(bearer_token, guest_token)

    user_payload = fetch_json(
        x_api_url(user_query_id, "UserByScreenName", {"screen_name": screen_name}),
        headers=headers,
    )
    user_id = (
        user_payload.get("data", {})
        .get("user", {})
        .get("result", {})
        .get("rest_id")
    )
    if not user_id:
        raise ValueError("Could not resolve Cursor official X user id")

    tweets_payload = fetch_json(
        x_api_url(
            tweets_query_id,
            "UserTweets",
            {
                "userId": user_id,
                "count": 20,
                "includePromotedContent": False,
                "withQuickPromoteEligibilityTweetFields": True,
                "withVoice": True,
                "withV2Timeline": True,
            },
            features=X_TWEETS_FEATURES,
            field_toggles=X_TWEETS_FIELD_TOGGLES,
        ),
        headers=headers,
    )
    return parse_x_tweets(tweets_payload, screen_name)


def run_source(source_name: str, fetcher: Any, seen_ids: list[str], bootstrap_mode: str, now_utc: datetime) -> SourceResult:
    try:
        fetched = fetcher()
        selected = select_items(fetched, seen_ids, bootstrap_mode, now_utc)
        return SourceResult(source=source_name, fetched=fetched, selected=selected)
    except (HTTPError, URLError, ET.ParseError, ValueError, KeyError, json.JSONDecodeError) as error:
        return SourceResult(source=source_name, fetched=[], selected=[], error=str(error))


def render_item(item: UpdateItem, timezone_name: str) -> list[str]:
    published = parse_datetime(item.published_at)
    if published:
        published_display = published.astimezone(ZoneInfo(timezone_name)).strftime("%Y-%m-%d %H:%M")
    else:
        published_display = "unknown"
    lines = [
        f"- **{item.title}**",
        f"  - 时间: {published_display}",
        f"  - 链接: {item.url}",
    ]
    if item.summary:
        lines.append(f"  - 摘要: {item.summary}")
    return lines


def render_section(title: str, items: list[UpdateItem], timezone_name: str, empty_message: str) -> list[str]:
    lines = [f"## {title}", ""]
    if not items:
        lines.append(empty_message)
        lines.append("")
        return lines
    for item in items:
        lines.extend(render_item(item, timezone_name))
    lines.append("")
    return lines


def build_report(changelog: SourceResult, blog: SourceResult, x_posts: SourceResult, timezone_name: str, generated_at: datetime) -> str:
    lines = [
        "# Cursor 每日更新",
        "",
        f"- 生成时间: {format_now(generated_at)}",
        f"- 时区: {timezone_name}",
        "- 数据源: changelog RSS, 官方 blog sitemap/article pages, 官方 X 账号公开时间线",
        "",
    ]

    lines.extend(render_section(f"Changelog（{len(changelog.selected)} 条）", changelog.selected, timezone_name, "今天没有发现新的 changelog 更新。"))
    lines.extend(render_section(f"Blog（{len(blog.selected)} 条）", blog.selected, timezone_name, "今天没有发现新的 blog 更新。"))
    lines.extend(render_section(f"官方 X 发文（{len(x_posts.selected)} 条）", x_posts.selected, timezone_name, "今天没有发现新的官方 X 发文。"))

    issues = []
    for result, label in [(changelog, "Changelog"), (blog, "Blog"), (x_posts, "官方 X")]:
        if result.error:
            issues.append(f"- {label}: {result.error}")
    if issues:
        lines.extend(["## 抓取异常", ""] + issues + [""])

    return "\n".join(lines).rstrip() + "\n"


def default_state() -> dict[str, Any]:
    return {
        "last_run_date": "",
        "seen_blog_urls": [],
        "seen_changelog_links": [],
        "seen_x_ids": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    state = default_state()
    state.update(read_json_file(path))
    for key in ("seen_blog_urls", "seen_changelog_links", "seen_x_ids"):
        if not isinstance(state.get(key), list):
            state[key] = []
    if not isinstance(state.get("last_run_date"), str):
        state["last_run_date"] = ""
    return state


def persist_state(path: Path, previous: dict[str, Any], changelog: SourceResult, blog: SourceResult, x_posts: SourceResult,
                  run_date: str | None) -> None:
    next_state = default_state()
    next_state["last_run_date"] = run_date if run_date is not None else previous.get("last_run_date", "")
    next_state["seen_changelog_links"] = (
        merge_seen(previous.get("seen_changelog_links", []), [item.item_id for item in changelog.fetched])
        if not changelog.error
        else previous.get("seen_changelog_links", [])
    )
    next_state["seen_blog_urls"] = (
        merge_seen(previous.get("seen_blog_urls", []), [item.item_id for item in blog.fetched])
        if not blog.error
        else previous.get("seen_blog_urls", [])
    )
    next_state["seen_x_ids"] = (
        merge_seen(previous.get("seen_x_ids", []), [item.item_id for item in x_posts.fetched])
        if not x_posts.error
        else previous.get("seen_x_ids", [])
    )
    write_json_file(path, next_state)


def should_run_now(force: bool, timezone_name: str, state: dict[str, Any], current_time: datetime | None = None) -> tuple[bool, str]:
    if force:
        return True, "forced run"

    current = current_time or now_in_timezone(timezone_name)
    if current.hour != 9:
        return False, f"skip: current local time is {format_now(current)}, not the 09:00 hour"

    local_date = current.date().isoformat()
    if state.get("last_run_date") == local_date:
        return False, f"skip: already ran for {local_date}"

    return True, "scheduled run window matched"


def write_report(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check Cursor changelog, blog, and official X posts.")
    parser.add_argument("--force", action="store_true", help="Run immediately instead of waiting for 09:00 in Asia/Shanghai.")
    parser.add_argument("--update-state", action="store_true", help="Persist seen-state during a forced run.")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="Timezone name used for schedule gating.")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Path to the markdown report.")
    parser.add_argument("--state-path", default=str(DEFAULT_STATE_PATH), help="Path to the local state file.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    output_path = Path(args.output)
    state_path = Path(args.state_path)
    state = load_state(state_path)
    should_run, reason = should_run_now(args.force, args.timezone, state)
    print(reason)
    if not should_run:
        return 0

    current_local = now_in_timezone(args.timezone)
    now_utc = current_local.astimezone(timezone.utc)

    changelog = run_source(
        "changelog",
        fetch_changelog,
        state.get("seen_changelog_links", []),
        "recent_days",
        now_utc,
    )
    blog = run_source(
        "blog",
        fetch_blog,
        state.get("seen_blog_urls", []),
        "recent_days",
        now_utc,
    )
    x_posts = run_source(
        "x",
        fetch_x_posts,
        state.get("seen_x_ids", []),
        "latest_x",
        now_utc,
    )

    report = build_report(changelog, blog, x_posts, args.timezone, current_local)
    write_report(output_path, report)
    print(f"wrote report to {output_path}")
    print(
        "selected counts:",
        json.dumps(
            {
                "changelog": len(changelog.selected),
                "blog": len(blog.selected),
                "x": len(x_posts.selected),
            },
            ensure_ascii=True,
            sort_keys=True,
        ),
    )

    if not any(result.error for result in (changelog, blog, x_posts)):
        run_date = current_local.date().isoformat() if not args.force else None
        if not args.force or args.update_state:
            persist_state(state_path, state, changelog, blog, x_posts, run_date=run_date)
            print(f"updated state at {state_path}")
    else:
        print("state not updated because at least one source failed")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
