#!/usr/bin/env python3
"""Collect recent GPT-Image-2 prompt posts and stage them for curation.

The repository's original crawler was not committed. This implementation is
reconstructed from the retained curation reports, result summaries, ingestion
index, and historical mapping files.

The crawler deliberately stops before mutating README/category files. It writes
reviewable artifacts to tmp/ and only publishes reports when explicitly asked.
"""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent.parent

DEFAULT_QUERIES = (
    "GPT-Image-2 prompt",
    '"gpt-image-2" prompt',
    "gpt image 2 prompt",
    "gpt-image-2 prompts",
    "gpt image 2 prompts",
    "GPT Image 2 prompt",
    "gptimage2 prompt",
    "GPT Image 2 Prompts",
    "GPT-Image-2",
    "gpt image 2",
)

CATEGORIES = (
    "E-commerce Cases",
    "Ad Creative Cases",
    "Portrait & Photography Cases",
    "Poster & Illustration Cases",
    "Character Design Cases",
    "UI & Social Media Mockup Cases",
    "Comparison & Community Examples",
)

CATEGORY_SLUGS = {
    "E-commerce Cases": "ecommerce",
    "Ad Creative Cases": "ad-creative",
    "Portrait & Photography Cases": "portrait",
    "Poster & Illustration Cases": "poster",
    "Character Design Cases": "character",
    "UI & Social Media Mockup Cases": "ui",
    "Comparison & Community Examples": "comparison",
}

URL_RE = re.compile(r"https?://(?:t\.co/\w+|\S+)", re.IGNORECASE)
STATUS_ID_RE = re.compile(r"(?:x|twitter)\.com/[^/]+/status/(\d+)", re.IGNORECASE)
CONVERSATION_QUERY_RE = re.compile(r"(?:^|\s)conversation_id:([^\s()]+)", re.IGNORECASE)
AUTHOR_QUERY_RE = re.compile(r"(?:^|\s)from:([^\s()]+)", re.IGNORECASE)
PROMPT_MARKER_RE = re.compile(
    r"(?im)(?:^|\n)\s*(?:[-*#>]+\s*)?(?:full\s+)?(?:image\s+)?"
    r"prompt(?:\s+\d+)?\s*(?:(?:[:：\-–—]|\u2b07\ufe0f?|\u2193)+|(?=\n))\s*"
)
TRAILING_TAGS_RE = re.compile(r"(?m)^\s*(?:#[\w\-]+\s*)+$")
SPACE_RE = re.compile(r"[ \t]+")

INSTRUCTION_RE = re.compile(
    r"\b(create|generate|transform|use|make|show|design|render|depict|compose|"
    r"keep|preserve|add|include|avoid)\b",
    re.IGNORECASE,
)
VISUAL_RE = re.compile(
    r"\b(image|photo|portrait|poster|illustration|lighting|background|composition|"
    r"camera|lens|style|render|scene|color|texture|layout|aspect ratio|storyboard|"
    r"infographic|character|product)\b",
    re.IGNORECASE,
)


class CrawlError(RuntimeError):
    """Expected operational failure with a concise user-facing message."""


@dataclass
class Tweet:
    tweet_id: str
    author_handle: str
    text: str
    created_at: str = ""
    conversation_id: str = ""
    in_reply_to_user_id: str = ""
    likes: int = 0
    views: int = 0
    retweets: int = 0
    replies: int = 0
    quotes: int = 0
    media_urls: list[str] = field(default_factory=list)
    media_alt_texts: list[str] = field(default_factory=list)
    explicit_prompt: str = ""
    explicit_title: str = ""

    @property
    def tweet_url(self) -> str:
        return f"https://x.com/{self.author_handle}/status/{self.tweet_id}"


class SearchProvider(Protocol):
    def search(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[Tweet]: ...

    def fetch_thread(
        self, conversation_id: str, author_handle: str,
        start: datetime, end: datetime, limit: int,
    ) -> list[Tweet]: ...


def utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_datetime(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in (
            "%a %b %d %H:%M:%S %z %Y",
            "%d %m %Y",
            "%Y-%m-%d",
        ):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            raise CrawlError(f"Unsupported datetime: {value}")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def timestamp_or_zero(value: str) -> float:
    try:
        return parse_datetime(value).timestamp() if value else 0.0
    except CrawlError:
        return 0.0


def tweet_is_within_window(tweet: Tweet, start: datetime, end: datetime) -> bool:
    if not tweet.created_at:
        return False
    try:
        created_at = parse_datetime(tweet.created_at)
    except CrawlError:
        return False
    return start <= created_at <= end


def int_value(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).replace(",", "").strip().lower()
    multiplier = 1
    if text.endswith("k"):
        multiplier, text = 1_000, text[:-1]
    elif text.endswith("m"):
        multiplier, text = 1_000_000, text[:-1]
    try:
        return int(float(text) * multiplier)
    except ValueError:
        return 0


def first_nonempty(mapping: dict[str, Any], *keys: str, default: Any = "") -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, "", [], {}):
            return value
    return default


def unique_strings(values: Iterable[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def extract_status_id(value: str) -> str:
    if value.isdigit():
        return value
    match = STATUS_ID_RE.search(value)
    return match.group(1) if match else ""


def media_from_generic(item: Any) -> tuple[list[str], list[str]]:
    if isinstance(item, str):
        return [item], []
    if not isinstance(item, dict):
        return [], []
    media_type = str(first_nonempty(item, "type", "media_type", default="photo"))
    if media_type not in ("photo", "image", "video", "animated_gif"):
        return [], []
    url = first_nonempty(
        item,
        "url",
        "media_url_https",
        "mediaUrl",
        "preview_image_url",
        "previewImageUrl",
    )
    alt = first_nonempty(item, "alt_text", "altText", "ext_alt_text")
    return ([str(url)] if url else []), ([str(alt)] if alt else [])


def normalize_generic_tweet(item: dict[str, Any]) -> Tweet | None:
    tweet_id = str(
        first_nonempty(item, "tweet_id", "id", "id_str", "rest_id", default="")
    )
    tweet_url = str(first_nonempty(item, "tweet_url", "url", "link", default=""))
    if not tweet_id:
        tweet_id = extract_status_id(tweet_url)
    if not tweet_id:
        return None

    author = first_nonempty(item, "author_handle", "username", "screen_name")
    author_obj = item.get("author") or item.get("user") or {}
    if not author and isinstance(author_obj, dict):
        author = first_nonempty(
            author_obj, "username", "screen_name", "userName", "handle"
        )
    if not author and tweet_url:
        match = re.search(r"(?:x|twitter)\.com/([^/]+)/status/", tweet_url)
        author = match.group(1) if match else "unknown"

    note_tweet = item.get("note_tweet") or item.get("noteTweet") or {}
    note_text = note_tweet.get("text", "") if isinstance(note_tweet, dict) else ""
    text = str(
        first_nonempty(
            item, "text", "full_text", "fullText", "rawText", default=note_text
        )
    )
    explicit_prompt = str(first_nonempty(item, "prompt_text", "prompt", default=""))
    if not text and explicit_prompt:
        text = explicit_prompt

    metrics = item.get("public_metrics") or item.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}

    raw_media: list[Any] = []
    for key in ("media", "images", "image", "photos"):
        value = item.get(key)
        if isinstance(value, list):
            raw_media.extend(value)
        elif value:
            raw_media.append(value)
    media_urls = list(item.get("media_urls") or [])
    alt_texts = list(item.get("media_alt_texts") or [])
    for media_item in raw_media:
        urls, alts = media_from_generic(media_item)
        media_urls.extend(urls)
        alt_texts.extend(alts)

    return Tweet(
        tweet_id=tweet_id,
        author_handle=str(author or "unknown").lstrip("@"),
        text=text,
        created_at=str(
            first_nonempty(item, "created_at", "createdAt", "published_at", default="")
        ),
        conversation_id=str(
            first_nonempty(item, "conversation_id", "conversationId", default=tweet_id)
        ),
        in_reply_to_user_id=str(
            first_nonempty(item, "in_reply_to_user_id", "inReplyToUserId", default="")
        ),
        likes=int_value(
            first_nonempty(
                item,
                "likes",
                "like_count",
                "likeCount",
                default=metrics.get("like_count"),
            )
        ),
        views=int_value(
            first_nonempty(
                item,
                "views",
                "view_count",
                "viewCount",
                default=metrics.get("impression_count"),
            )
        ),
        retweets=int_value(
            first_nonempty(
                item,
                "retweets",
                "retweet_count",
                "retweetCount",
                default=metrics.get("retweet_count"),
            )
        ),
        replies=int_value(
            first_nonempty(
                item,
                "replies",
                "reply_count",
                "replyCount",
                default=metrics.get("reply_count"),
            )
        ),
        quotes=int_value(
            first_nonempty(
                item,
                "quotes",
                "quote_count",
                "quoteCount",
                default=metrics.get("quote_count"),
            )
        ),
        media_urls=unique_strings(media_urls),
        media_alt_texts=unique_strings(alt_texts),
        explicit_prompt=explicit_prompt,
        explicit_title=str(first_nonempty(item, "title", "suggested_title", default="")),
    )


def normalize_x_api_payload(payload: Any) -> list[Tweet]:
    if not isinstance(payload, dict):
        return normalize_payload(payload)
    data = payload.get("data")
    if not isinstance(data, list):
        return normalize_payload(payload)

    includes = payload.get("includes") or {}
    users = {
        str(user.get("id")): user
        for user in includes.get("users", [])
        if isinstance(user, dict)
    }
    media = {
        str(item.get("media_key")): item
        for item in includes.get("media", [])
        if isinstance(item, dict)
    }
    tweets: list[Tweet] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        author = users.get(str(item.get("author_id")), {})
        attachments = item.get("attachments") or {}
        media_urls: list[str] = []
        alt_texts: list[str] = []
        for key in attachments.get("media_keys", []):
            urls, alts = media_from_generic(media.get(str(key), {}))
            media_urls.extend(urls)
            alt_texts.extend(alts)
        enriched = dict(item)
        enriched["author_handle"] = author.get("username", "unknown")
        enriched["media_urls"] = media_urls
        enriched["media_alt_texts"] = alt_texts
        tweet = normalize_generic_tweet(enriched)
        if tweet:
            tweets.append(tweet)
    return tweets


def normalize_payload(payload: Any) -> list[Tweet]:
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("data"), list)
        and (
            "includes" in payload
            or any(
                isinstance(item, dict) and "author_id" in item
                for item in payload["data"]
            )
        )
    ):
        return normalize_x_api_payload(payload)
    if isinstance(payload, dict):
        for key in (
            "tweets",
            "results",
            "items",
            "records",
            "candidates",
            "data",
        ):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            payload = [payload]
    if not isinstance(payload, list):
        raise CrawlError(
            "Provider JSON must be a list or an object containing "
            "tweets/results/data/candidates"
        )
    payload = flatten_thread_replies(payload)
    tweets: list[Tweet] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        tweet = normalize_generic_tweet(item)
        if tweet:
            tweets.append(tweet)
    return tweets


def flatten_thread_replies(payload: list[Any]) -> list[Any]:
    flattened: list[Any] = []
    for item in payload:
        if not isinstance(item, dict):
            flattened.append(item)
            continue

        root = dict(item)
        replies = root.pop("thread_replies", None)
        flattened.append(root)
        if not isinstance(replies, list):
            continue

        conversation_id = str(
            first_nonempty(root, "conversation_id", "conversationId", default="")
        )
        if not conversation_id:
            conversation_id = str(
                first_nonempty(root, "tweet_id", "id", "id_str", "rest_id", default="")
            )
        if not conversation_id:
            conversation_id = extract_status_id(
                str(first_nonempty(root, "tweet_url", "url", "link", default=""))
            )

        for reply in replies:
            if not isinstance(reply, dict):
                continue
            enriched = dict(reply)
            if conversation_id and not first_nonempty(
                enriched, "conversation_id", "conversationId", default=""
            ):
                enriched["conversation_id"] = conversation_id
            flattened.append(enriched)
    return flattened


def http_json(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: int = 45,
    retries: int = 3,
) -> dict[str, Any]:
    encoded = None
    request_headers = {"User-Agent": "awesome-gpt-image-2-prompt-crawler/1.0"}
    if headers:
        request_headers.update(headers)
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    for attempt in range(retries):
        try:
            request = Request(url, data=encoded, headers=request_headers)
            with urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < retries:
                time.sleep(2**attempt)
                continue
            raise CrawlError(f"HTTP {exc.code} from {url}: {detail[:500]}") from exc
        except (URLError, TimeoutError) as exc:
            if attempt + 1 < retries:
                time.sleep(2**attempt)
                continue
            raise CrawlError(f"Request failed for {url}: {exc}") from exc
    raise CrawlError(f"Request failed for {url}")


class XApiProvider:
    def __init__(self, bearer_token: str, base_url: str) -> None:
        if not bearer_token:
            raise CrawlError("X_BEARER_TOKEN or --x-bearer-token is required")
        self.bearer_token = bearer_token
        self.endpoint = base_url.rstrip("/") + "/2/tweets/search/recent"

    def search(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[Tweet]:
        tweets: list[Tweet] = []
        next_token = ""
        while len(tweets) < limit:
            page_size = min(100, max(10, limit - len(tweets)))
            params = {
                "query": f"({query}) -is:retweet",
                "start_time": utc_iso(start),
                "end_time": utc_iso(end),
                "max_results": str(page_size),
                "tweet.fields": (
                    "id,text,author_id,created_at,conversation_id,"
                    "in_reply_to_user_id,public_metrics,attachments,lang"
                ),
                "expansions": "author_id,attachments.media_keys",
                "media.fields": "media_key,type,url,preview_image_url,width,height,alt_text",
                "user.fields": "id,username,name",
            }
            if next_token:
                params["next_token"] = next_token
            payload = http_json(
                self.endpoint + "?" + urlencode(params),
                headers={"Authorization": f"Bearer {self.bearer_token}"},
            )
            tweets.extend(normalize_x_api_payload(payload))
            next_token = str((payload.get("meta") or {}).get("next_token", ""))
            if not next_token:
                break
        return tweets[:limit]

    def fetch_thread(
        self, conversation_id: str, author_handle: str,
        start: datetime, end: datetime, limit: int,
    ) -> list[Tweet]:
        query = f"conversation_id:{conversation_id} from:{author_handle}"
        return [
            item for item in self.search(query, start, end, limit)
            if (item.conversation_id or item.tweet_id) == conversation_id
            and item.author_handle.lower() == author_handle.lower()
        ]


class JsonProvider:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.tweets = normalize_payload(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise CrawlError(f"Cannot read provider JSON {path}: {exc}") from exc

    def search(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[Tweet]:
        conversation_match = CONVERSATION_QUERY_RE.search(query)
        author_match = AUTHOR_QUERY_RE.search(query)
        conversation_id = conversation_match.group(1) if conversation_match else ""
        author_handle = author_match.group(1).lstrip("@").lower() if author_match else ""
        selected: list[Tweet] = []
        for tweet in self.tweets:
            if conversation_id and (tweet.conversation_id or tweet.tweet_id) != conversation_id:
                continue
            if author_handle and tweet.author_handle.lower() != author_handle:
                continue
            if tweet.created_at:
                try:
                    created = parse_datetime(tweet.created_at)
                    if not start <= created <= end:
                        continue
                except CrawlError:
                    pass
            selected.append(tweet)
        return selected[:limit]

    def fetch_thread(
        self, conversation_id: str, author_handle: str,
        start: datetime, end: datetime, limit: int,
    ) -> list[Tweet]:
        query = f"conversation_id:{conversation_id} from:{author_handle}"
        return self.search(query, start, end, limit)


class CommandProvider:
    """Adapter for a local X CLI that emits JSON.

    The command template is trusted local configuration. Available placeholders:
    {query}, {start}, {end}, and {limit}. The query/time placeholders are shell
    quoted before interpolation.
    """

    def __init__(self, template: str) -> None:
        if "{query}" not in template:
            raise CrawlError("--search-command must contain a {query} placeholder")
        self.template = template

    def search(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[Tweet]:
        command = self.template.format(
            query=shlex.quote(query),
            start=shlex.quote(utc_iso(start)),
            end=shlex.quote(utc_iso(end)),
            limit=limit,
        )
        completed = subprocess.run(
            command,
            shell=True,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            raise CrawlError(
                f"Search command failed ({completed.returncode}): {completed.stderr.strip()}"
            )
        try:
            return normalize_payload(json.loads(completed.stdout))[:limit]
        except json.JSONDecodeError as exc:
            raise CrawlError("Search command did not return valid JSON") from exc

    def fetch_thread(
        self, conversation_id: str, author_handle: str,
        start: datetime, end: datetime, limit: int,
    ) -> list[Tweet]:
        query = f"conversation_id:{conversation_id} from:{author_handle}"
        return self.search(query, start, end, limit)


class OpenCLIProvider:
    """X/Twitter search via opencli browser automation (no API token needed).

    Requires opencli CLI and a Chrome browser logged into x.com.
    """

    def __init__(self, query_delay: float = 0) -> None:
        if not shutil.which("opencli"):
            raise CrawlError(
                "opencli CLI not found in PATH. "
                "Install with: npm install -g @jackwener/opencli"
            )
        self.query_delay = query_delay
        self._last_search = 0.0

    def search(
        self, query: str, start: datetime, end: datetime, limit: int
    ) -> list[Tweet]:
        # Enforce delay between queries
        elapsed = time.time() - self._last_search
        if elapsed < self.query_delay:
            time.sleep(self.query_delay - elapsed)

        start_utc = start.astimezone(timezone.utc)
        end_utc = end.astimezone(timezone.utc)
        since_date = start_utc.date().isoformat()
        until_date = end_utc.date()
        end_midnight = datetime.combine(
            until_date, datetime.min.time(), tzinfo=timezone.utc
        )
        if end_utc > end_midnight:
            until_date += timedelta(days=1)
        full_query = (
            f"({query}) since:{since_date} until:{until_date.isoformat()} "
            "-filter:retweets"
        )
        cmd = [
            "opencli", "twitter", "search", full_query,
            "--filter", "live",
            "--limit", str(limit),
            "-f", "json",
        ]

        # Retry on 429 rate limit
        for attempt in range(3):
            completed = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
            )
            self._last_search = time.time()
            if completed.returncode == 0:
                tweets = self._parse_opencli_output(completed.stdout)
                return [
                    tweet for tweet in tweets
                    if tweet_is_within_window(tweet, start_utc, end_utc)
                ][:limit]

            stderr = completed.stderr.strip()
            if "429" in stderr and attempt < 2:
                wait = 120 * (attempt + 1)
                print(f"  Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue

            raise CrawlError(
                f"opencli search failed ({completed.returncode}): {stderr}"
            )
        raise CrawlError("opencli search failed after retries")

    def fetch_thread(
        self, conversation_id: str, author_handle: str,
        start: datetime, end: datetime, limit: int,
    ) -> list[Tweet]:
        cmd = [
            "opencli", "twitter", "thread", conversation_id,
            "--limit", str(limit), "-f", "json",
        ]
        completed = subprocess.run(
            cmd, capture_output=True, text=True, timeout=120,
        )
        if completed.returncode != 0:
            raise CrawlError(
                f"opencli thread failed ({completed.returncode}): "
                f"{completed.stderr.strip()}"
            )
        all_tweets = self._parse_opencli_output(completed.stdout)
        return [
            t for t in all_tweets
            if t.author_handle.lower() == author_handle.lower()
        ][:limit]

    @staticmethod
    def _parse_opencli_output(raw: str) -> list[Tweet]:
        # opencli appends update notices after the JSON array
        json_str = raw.strip()
        last_bracket = json_str.rfind("]")
        if last_bracket != -1:
            json_str = json_str[:last_bracket + 1]
        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as exc:
            raise CrawlError(
                f"opencli returned invalid JSON: {exc}"
            ) from exc
        if not isinstance(data, list):
            raise CrawlError("opencli returned unexpected JSON structure")
        tweets: list[Tweet] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            tweet = OpenCLIProvider._normalize_opencli_tweet(item)
            if tweet:
                tweets.append(tweet)
        return tweets

    @staticmethod
    def _normalize_opencli_tweet(item: dict[str, Any]) -> Tweet | None:
        tweet_id = str(item.get("id", ""))
        if not tweet_id:
            return None
        author = str(item.get("author", "")).lstrip("@")
        text = item.get("text", "")
        created_at = item.get("created_at", "")
        likes = int_value(item.get("likes"))
        views_str = str(item.get("views", "0"))
        views = int(views_str) if views_str.isdigit() else 0
        media_urls = item.get("media_urls") or []
        if isinstance(media_urls, list):
            media_urls = [str(u) for u in media_urls]

        return Tweet(
            tweet_id=tweet_id,
            author_handle=author,
            text=text,
            created_at=created_at,
            conversation_id=str(item.get("conversation_id") or tweet_id),
            likes=likes,
            views=views,
            media_urls=media_urls,
        )


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = URL_RE.sub("", text)
    text = TRAILING_TAGS_RE.sub("", text)
    lines = [SPACE_RE.sub(" ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()


def prompt_likelihood(text: str) -> float:
    if not text:
        return 0.0
    length_score = min(len(text) / 600.0, 4.0)
    instruction_score = min(len(INSTRUCTION_RE.findall(text)) * 0.45, 2.5)
    visual_score = min(len(VISUAL_RE.findall(text)) * 0.15, 2.0)
    structure_score = (
        1.0
        if re.search(
            r"(?m)^(style|composition|lighting|scene|negative prompt|format)\s*[:：]",
            text,
            re.IGNORECASE,
        )
        else 0.0
    )
    json_score = 1.0 if text.lstrip().startswith(("{", "[")) else 0.0
    return length_score + instruction_score + visual_score + structure_score + json_score


def extract_prompt(tweet: Tweet, thread: Iterable[Tweet] = ()) -> tuple[str, str]:
    if tweet.explicit_prompt:
        prompt = clean_text(tweet.explicit_prompt)
        non_prompt = clean_text(tweet.text.replace(tweet.explicit_prompt, "", 1))
        return prompt, non_prompt

    authored = [tweet]
    authored.extend(
        item
        for item in thread
        if item.tweet_id != tweet.tweet_id
        and item.author_handle.lower() == tweet.author_handle.lower()
    )
    authored.sort(key=lambda item: timestamp_or_zero(item.created_at))

    prompt_parts: list[str] = []
    non_prompt_parts: list[str] = []
    fallback: list[tuple[float, str]] = []
    for item in authored:
        text = clean_text(item.text)
        if not text:
            continue
        marker = PROMPT_MARKER_RE.search(text)
        if marker:
            before = clean_text(text[: marker.start()])
            after = clean_text(text[marker.end() :])
            if before:
                non_prompt_parts.append(before)
            if after:
                prompt_parts.append(after)
            continue
        likelihood = prompt_likelihood(text)
        fallback.append((likelihood, text))

    for item in authored:
        for alt_text in item.media_alt_texts:
            cleaned_alt = clean_text(alt_text)
            if (
                len(cleaned_alt) >= 160
                and INSTRUCTION_RE.search(cleaned_alt)
                and len(VISUAL_RE.findall(cleaned_alt)) >= 3
            ):
                prompt_parts.append(cleaned_alt)

    if not prompt_parts and fallback:
        likely = [text for score, text in fallback if score >= 3.0]
        if likely:
            prompt_parts.extend(likely)
        else:
            non_prompt_parts.extend(text for _, text in fallback)
    else:
        for score, text in fallback:
            if score >= 4.5 and len(text) >= 160:
                prompt_parts.append(text)
            else:
                non_prompt_parts.append(text)

    return "\n\n".join(unique_strings(prompt_parts)), "\n\n".join(
        unique_strings(non_prompt_parts)
    )


def classify_category(prompt: str, non_prompt: str = "") -> str:
    text = f"{prompt}\n{non_prompt}".lower()
    weights: dict[str, tuple[tuple[str, int], ...]] = {
        "E-commerce Cases": (
            ("product photography", 5),
            ("packaging", 4),
            ("e-commerce", 5),
            ("ecommerce", 5),
            ("perfume", 3),
            ("skincare", 3),
            ("bottle", 2),
            ("commercial product", 4),
            ("food photography", 3),
        ),
        "Ad Creative Cases": (
            ("advertisement", 4),
            (" ad ", 2),
            ("campaign", 3),
            ("banner", 4),
            ("call to action", 4),
            ("discount", 3),
            ("marketing", 2),
        ),
        "Portrait & Photography Cases": (
            ("portrait", 5),
            ("face reference", 4),
            ("selfie", 4),
            ("facial", 2),
            ("skin texture", 2),
            ("85mm", 2),
            ("fashion photography", 3),
        ),
        "Poster & Illustration Cases": (
            ("poster", 5),
            ("illustration", 4),
            ("storyboard", 3),
            ("travel card", 3),
            ("editorial collage", 3),
            ("anime", 2),
            ("movie poster", 5),
        ),
        "Character Design Cases": (
            ("character sheet", 6),
            ("character design", 6),
            ("turnaround", 5),
            ("mascot", 4),
            ("costume sheet", 4),
            ("expression sheet", 4),
        ),
        "UI & Social Media Mockup Cases": (
            ("user interface", 6),
            (" ui ", 4),
            ("dashboard", 5),
            ("app screen", 5),
            ("social media", 5),
            ("mockup", 4),
            ("infographic", 4),
            ("brand identity", 3),
            ("design system", 4),
        ),
        "Comparison & Community Examples": (
            ("before and after", 6),
            ("before/after", 6),
            ("comparison", 5),
            ("style transfer", 5),
            ("transform the attached", 3),
            ("editing test", 4),
            ("restore", 3),
        ),
    }
    scores = {
        category: sum(weight for keyword, weight in keywords if keyword in text)
        for category, keywords in weights.items()
    }
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "Poster & Illustration Cases"


def suggest_title(tweet: Tweet, prompt: str, non_prompt: str, category: str) -> str:
    if tweet.explicit_title:
        return tweet.explicit_title.strip()[:100]
    candidates = [line.strip(" -*#>\t") for line in non_prompt.splitlines()]
    for line in candidates:
        line = re.sub(r"\b(?:GPT[- ]?Image\s*2(?:\.0)?|ChatGPT)\b", "", line, flags=re.IGNORECASE)
        line = SPACE_RE.sub(" ", line).strip(" :-–—|🔥✨⬇️")
        words = line.split()
        if 3 <= len(words) <= 12 and len(line) <= 90:
            return line
    first_sentence = re.split(r"[.!?\n]", prompt, maxsplit=1)[0].strip()
    first_sentence = re.sub(
        r"^(create|generate|design|make|transform|use)\s+(?:an?|the)?\s*",
        "",
        first_sentence,
        flags=re.IGNORECASE,
    )
    words = first_sentence.split()
    if words:
        return " ".join(words[:10]).strip(" ,:;-_")[:90]
    label = category.replace(" Cases", "").replace(" Examples", "")
    return f"{label} Prompt by @{tweet.author_handle}"


def quality_score(tweet: Tweet, prompt: str) -> float:
    if not prompt:
        return 0.0
    score = 0.0
    if tweet.media_urls:
        score += 2.0
    score += min(len(prompt) / 450.0, 4.0)
    score += min(len(INSTRUCTION_RE.findall(prompt)) * 0.25, 1.5)
    score += min(len(VISUAL_RE.findall(prompt)) * 0.08, 1.0)
    if prompt.lstrip().startswith(("{", "[")):
        score += 0.75
    engagement = tweet.likes + tweet.retweets * 2 + tweet.replies
    if engagement:
        score += min(math.log10(engagement + 1) / 2.0, 1.0)
    return round(score, 2)


def heuristic_decision(
    tweet: Tweet, prompt: str, min_prompt_chars: int
) -> tuple[str, str, float]:
    score = quality_score(tweet, prompt)
    if not tweet.media_urls:
        return "drop", "No output media attached.", score
    if len(prompt) < min_prompt_chars:
        return "drop", "No complete reusable inline prompt was recovered.", score
    if score >= 7.0:
        return (
            "keep",
            "Strong reusable prompt with output media and detailed visual instructions.",
            score,
        )
    return (
        "review",
        "Usable prompt with media; manual novelty and duplication review is recommended.",
        score,
    )


def openai_review(
    candidate: dict[str, Any], base_url: str, api_key: str, model: str
) -> dict[str, str]:
    if not api_key or not model:
        raise CrawlError(
            "CURATION_API_KEY and CURATION_MODEL (or matching CLI options) "
            "are required for --reviewer openai"
        )
    system = (
        "You curate a public GPT-Image-2 prompt repository. Return JSON only with "
        "action (keep, review, or drop), title, category, and reason. Keep only complete, "
        "reusable image prompts with corresponding media. Prefer novel mechanisms over "
        "generic glamour portraits. Category must be one of: "
        + "; ".join(CATEGORIES)
        + ". Title must be concise and descriptive."
    )
    user = json.dumps(
        {
            "tweet_url": candidate["tweet_url"],
            "author_handle": candidate["author_handle"],
            "likes": candidate["likes"],
            "views": candidate["views"],
            "media_count": len(candidate["media_urls"]),
            "prompt": candidate["prompt_text"],
            "context": candidate["non_prompt_text"],
        },
        ensure_ascii=False,
    )
    payload = http_json(
        base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        body={
            "model": model,
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
    )
    try:
        content = payload["choices"][0]["message"]["content"]
        result = json.loads(content)
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise CrawlError("Curation API returned an invalid response") from exc
    action = str(result.get("action", "review")).lower()
    category = str(result.get("category", candidate["suggested_category"]))
    return {
        "action": action if action in ("keep", "review", "drop") else "review",
        "title": str(result.get("title", candidate["suggested_title"]))[:100],
        "category": category if category in CATEGORIES else candidate["suggested_category"],
        "reason": str(result.get("reason", "Model review completed."))[:500],
    }


def collect_existing_tweet_ids(repo: Path) -> set[str]:
    ids: set[str] = set()
    ingestion_index = repo / "data" / "ingested_tweets.json"
    if ingestion_index.exists():
        try:
            payload = json.loads(ingestion_index.read_text(encoding="utf-8"))
            for record in payload.get("records", []):
                if not isinstance(record, dict):
                    continue
                tweet_id = str(record.get("tweet_id", "")) or extract_status_id(
                    str(record.get("tweet_url", ""))
                )
                if tweet_id:
                    ids.add(tweet_id)
        except (OSError, json.JSONDecodeError) as exc:
            raise CrawlError(f"Cannot parse {ingestion_index}: {exc}") from exc
    for pattern in ("README*.md", "cases/*.md"):
        for path in repo.glob(pattern):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            ids.update(STATUS_ID_RE.findall(text))
    return ids


def read_queries(path: Path | None) -> list[str]:
    if path is None:
        return list(DEFAULT_QUERIES)
    queries = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            queries.append(line)
    if not queries:
        raise CrawlError(f"No queries found in {path}")
    return queries


def media_extension(url: str, content_type: str = "") -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return ".jpg" if suffix == ".jpeg" else suffix
    guessed = mimetypes.guess_extension(content_type.split(";", 1)[0].strip())
    return ".jpg" if guessed in (None, ".jpe", ".jpeg") else guessed


def original_media_url(url: str) -> str:
    if "pbs.twimg.com/media/" not in url or "name=" in url:
        return url
    return url + ("&" if "?" in url else "?") + "name=orig"


def download_media(url: str, destination_without_suffix: Path) -> Path:
    request = Request(
        original_media_url(url),
        headers={"User-Agent": "Mozilla/5.0 awesome-gpt-image-2-prompt-crawler/1.0"},
    )
    try:
        with urlopen(request, timeout=60) as response:
            content_type = response.headers.get("Content-Type", "")
            payload = response.read(25 * 1024 * 1024 + 1)
    except (HTTPError, URLError, TimeoutError) as exc:
        raise CrawlError(f"Failed to download media {url}: {exc}") from exc
    if len(payload) > 25 * 1024 * 1024:
        raise CrawlError(f"Media exceeds 25 MiB: {url}")
    destination = destination_without_suffix.with_suffix(media_extension(url, content_type))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(payload)
    return destination


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def display_path(path: Path, repo: Path) -> str:
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)


def tweet_sort_key(tweet: Tweet) -> tuple[float, int, str]:
    created = timestamp_or_zero(tweet.created_at)
    return (-created, -tweet.likes, tweet.tweet_id)


def merge_tweets(current: Tweet, incoming: Tweet) -> Tweet:
    primary, secondary = (
        (incoming, current)
        if len(incoming.text) > len(current.text)
        else (current, incoming)
    )
    merged = Tweet(**asdict(primary))
    merged.media_urls = unique_strings(current.media_urls + incoming.media_urls)
    merged.media_alt_texts = unique_strings(
        current.media_alt_texts + incoming.media_alt_texts
    )
    merged.likes = max(current.likes, incoming.likes)
    merged.views = max(current.views, incoming.views)
    merged.retweets = max(current.retweets, incoming.retweets)
    merged.replies = max(current.replies, incoming.replies)
    merged.quotes = max(current.quotes, incoming.quotes)
    if len(secondary.explicit_prompt) > len(merged.explicit_prompt):
        merged.explicit_prompt = secondary.explicit_prompt
    if not merged.explicit_title:
        merged.explicit_title = secondary.explicit_title
    return merged


def build_provider(args: argparse.Namespace) -> SearchProvider:
    if args.provider == "x-api":
        return XApiProvider(args.x_bearer_token, args.x_api_base_url)
    if args.provider == "json":
        if not args.input_json:
            raise CrawlError("--input-json is required for --provider json")
        return JsonProvider(args.input_json)
    if args.provider == "command":
        if not args.search_command:
            raise CrawlError("--search-command is required for --provider command")
        return CommandProvider(args.search_command)
    if args.provider == "opencli":
        return OpenCLIProvider(query_delay=args.opencli_delay)
    raise CrawlError(f"Unknown provider: {args.provider}")


def run_pipeline(args: argparse.Namespace, provider: SearchProvider) -> dict[str, Any]:
    end = parse_datetime(args.end) if args.end else datetime.now(timezone.utc)
    start = end - timedelta(hours=args.window_hours)
    queries = read_queries(args.queries_file)
    existing_ids = collect_existing_tweet_ids(args.repo)

    raw_by_id: dict[str, Tweet] = {}
    query_hits: dict[str, list[str]] = {}
    raw_found_count = 0
    for query in queries:
        results = provider.search(query, start, end, args.per_query_limit)
        raw_found_count += len(results)
        query_hits[query] = [item.tweet_id for item in results]
        for tweet in results:
            current = raw_by_id.get(tweet.tweet_id)
            raw_by_id[tweet.tweet_id] = (
                tweet if current is None else merge_tweets(current, tweet)
            )

    new_tweets = [
        tweet for tweet_id, tweet in raw_by_id.items() if tweet_id not in existing_ids
    ]
    new_tweets.sort(key=tweet_sort_key)

    thread_cache: dict[tuple[str, str], list[Tweet]] = {}
    candidates: list[dict[str, Any]] = []
    for index, tweet in enumerate(new_tweets, start=1):
        thread: list[Tweet] = []
        prompt, non_prompt = extract_prompt(tweet)
        should_enrich = args.enrich_threads and (
            len(prompt) < args.min_prompt_chars
            or re.search(r"(?i)prompt.{0,20}(below|reply|thread|\u2b07|\u2193)", tweet.text)
        )
        if should_enrich:
            conversation_id = tweet.conversation_id or tweet.tweet_id
            thread_key = (conversation_id, tweet.author_handle.lower())
            if thread_key not in thread_cache:
                thread_cache[thread_key] = provider.fetch_thread(
                    conversation_id, tweet.author_handle,
                    start, end, args.thread_limit,
                )
            thread = thread_cache[thread_key]
            prompt, non_prompt = extract_prompt(tweet, thread)

        media_urls = unique_strings(
            tweet.media_urls
            + [url for item in thread for url in item.media_urls]
        )
        category = classify_category(prompt, non_prompt)
        review_tweet = Tweet(**asdict(tweet))
        review_tweet.media_urls = media_urls
        action, reason, score = heuristic_decision(
            review_tweet, prompt, args.min_prompt_chars
        )
        candidate = {
            "candidate_index": index,
            "tweet_url": tweet.tweet_url,
            "tweet_id": tweet.tweet_id,
            "author_handle": tweet.author_handle,
            "created_at": tweet.created_at,
            "likes": tweet.likes,
            "views": tweet.views,
            "retweets": tweet.retweets,
            "replies": tweet.replies,
            "quotes": tweet.quotes,
            "has_media": bool(media_urls),
            "media_urls": media_urls,
            "media_alt_texts": unique_strings(
                tweet.media_alt_texts
                + [alt for item in thread for alt in item.media_alt_texts]
            ),
            "prompt_text": prompt,
            "non_prompt_text": non_prompt,
            "has_inline_prompt": len(prompt) >= args.min_prompt_chars,
            "suggested_title": suggest_title(tweet, prompt, non_prompt, category),
            "suggested_action": action,
            "suggested_category": category,
            "suggested_category_slug": CATEGORY_SLUGS[category],
            "suggested_shape": "Shape B" if len(media_urls) > 1 else "Shape A",
            "quality_score": score,
            "decision_reason": reason,
            "thread_tweet_ids": [item.tweet_id for item in thread],
            "media_local_paths": [],
        }
        if args.reviewer == "openai" and candidate["has_inline_prompt"] and media_urls:
            review = openai_review(
                candidate,
                args.curation_base_url,
                args.curation_api_key,
                args.curation_model,
            )
            candidate["suggested_action"] = review["action"]
            candidate["suggested_title"] = review["title"]
            candidate["suggested_category"] = review["category"]
            candidate["suggested_category_slug"] = CATEGORY_SLUGS[review["category"]]
            candidate["decision_reason"] = review["reason"]
        candidates.append(candidate)

    prefiltered = [
        candidate
        for candidate in candidates
        if candidate["has_media"] and candidate["has_inline_prompt"]
    ]
    recommended = [
        candidate for candidate in candidates if candidate["suggested_action"] == "keep"
    ]

    download_targets: list[dict[str, Any]] = []
    if args.download_media == "all":
        download_targets = candidates
    elif args.download_media == "prefiltered":
        download_targets = prefiltered
    elif args.download_media == "kept":
        download_targets = recommended
    for candidate in download_targets:
        local_paths = []
        media_dir = args.output_dir / "media" / candidate["tweet_id"]
        for media_index, url in enumerate(candidate["media_urls"], start=1):
            try:
                path = download_media(url, media_dir / f"{media_index:02d}")
                local_paths.append(display_path(path, args.repo))
            except CrawlError as exc:
                candidate.setdefault("media_errors", []).append(str(exc))
        candidate["media_local_paths"] = local_paths

    generated_at = datetime.now(timezone.utc)
    artifact_paths = {
        "candidate_tweets": args.output_dir / "candidate_tweets.json",
        "prefiltered_candidates": args.output_dir / "prefiltered_candidates.json",
        "review_queue": args.output_dir / "review_queue.json",
        "query_hits": args.output_dir / "search_meta.json",
    }
    report = {
        "curation_date": end.date().isoformat(),
        "generated_at": generated_at.isoformat(),
        "search_window": {"from": utc_iso(start), "to": utc_iso(end)},
        "stats": {
            "raw_collected": raw_found_count,
            "unique_collected": len(raw_by_id),
            "existing_dropped": len(raw_by_id) - len(new_tweets),
            "after_dedup": len(new_tweets),
            "after_prefilter": len(prefiltered),
            "reviewed": len(prefiltered),
            "recommended_keep": len(recommended),
            "manual_review": sum(
                candidate["suggested_action"] == "review" for candidate in candidates
            ),
            "auto_dropped": sum(
                candidate["suggested_action"] == "drop" for candidate in candidates
            ),
        },
        "queries": queries,
        "provider": args.provider,
        "reviewer": args.reviewer,
        "curation_artifacts": {
            key: display_path(path, args.repo) for key, path in artifact_paths.items()
        },
        "candidates": candidates,
        "recommended_keep": recommended,
        "auto_dropped": [
            {
                "tweet_url": candidate["tweet_url"],
                "author_handle": candidate["author_handle"],
                "reason": candidate["decision_reason"],
            }
            for candidate in candidates
            if candidate["suggested_action"] == "drop"
        ],
    }
    summary = {
        "status": (
            "review_required"
            if any(c["suggested_action"] == "review" for c in candidates)
            else "curated"
        ),
        "run_timestamp": generated_at.isoformat(),
        "curation_date": end.date().isoformat(),
        "age_window_hours": args.window_hours,
        "search_window": report["search_window"],
        "queries": queries,
        "found_count": raw_found_count,
        "unique_found_count": len(raw_by_id),
        "after_dedup_count": len(new_tweets),
        "prefiltered_count": len(prefiltered),
        "reviewed_count": len(prefiltered),
        "recommended_keep_count": len(recommended),
        "auto_dropped_count": report["stats"]["auto_dropped"],
        "manual_review_count": report["stats"]["manual_review"],
        "top_candidates": [
            {
                key: candidate[key]
                for key in (
                    "candidate_index",
                    "tweet_url",
                    "author_handle",
                    "suggested_title",
                    "suggested_category",
                    "suggested_shape",
                    "likes",
                    "views",
                    "quality_score",
                )
            }
            for candidate in sorted(
                prefiltered,
                key=lambda item: (-item["quality_score"], -item["likes"]),
            )[: args.summary_limit]
        ],
        "curation_artifacts": report["curation_artifacts"],
    }

    write_json(artifact_paths["candidate_tweets"], candidates)
    write_json(artifact_paths["prefiltered_candidates"], prefiltered)
    write_json(
        artifact_paths["review_queue"],
        [candidate for candidate in candidates if candidate["suggested_action"] != "drop"],
    )
    write_json(
        artifact_paths["query_hits"],
        {
            "queries": queries,
            "query_hits": query_hits,
            "search_window": report["search_window"],
        },
    )
    write_json(args.output_dir / "curation_report.json", report)
    write_json(args.output_dir / "summary.json", summary)

    if args.publish_report:
        report_path = args.repo / "data" / f"curation_report_{end.date().isoformat()}.json"
        timestamp = end.strftime("%Y%m%d_%H%M%S")
        summary_path = (
            args.repo
            / "result"
            / f"gpt_image_2_recent_prompts_{timestamp}_summary.json"
        )
        write_json(report_path, report)
        write_json(summary_path, summary)
    return {"report": report, "summary": summary}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Search recent X posts for reusable GPT-Image-2 prompts and stage "
            "curation artifacts."
        )
    )
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument(
        "--provider", choices=("x-api", "json", "command", "opencli"), default="opencli"
    )
    parser.add_argument("--input-json", type=Path)
    parser.add_argument("--search-command", default=os.getenv("X_SEARCH_COMMAND", ""))
    parser.add_argument(
        "--x-bearer-token", default=os.getenv("X_BEARER_TOKEN", "")
    )
    parser.add_argument(
        "--x-api-base-url", default=os.getenv("X_API_BASE_URL", "https://api.x.com")
    )
    parser.add_argument("--queries-file", type=Path)
    parser.add_argument("--end", help="UTC/ISO end time; defaults to now")
    parser.add_argument("--window-hours", type=int, default=24)
    parser.add_argument("--per-query-limit", type=int, default=100)
    parser.add_argument("--thread-limit", type=int, default=30)
    parser.add_argument("--enrich-threads", action="store_true")
    parser.add_argument("--opencli-delay", type=float, default=0,
                        help="Seconds to wait between opencli queries (default 0)")
    parser.add_argument("--min-prompt-chars", type=int, default=160)
    parser.add_argument(
        "--reviewer", choices=("heuristic", "openai"), default="heuristic"
    )
    parser.add_argument(
        "--curation-base-url",
        default=os.getenv("CURATION_BASE_URL", "https://api.openai.com/v1"),
    )
    parser.add_argument(
        "--curation-api-key",
        default=os.getenv("CURATION_API_KEY", os.getenv("OPENAI_API_KEY", "")),
    )
    parser.add_argument(
        "--curation-model", default=os.getenv("CURATION_MODEL", "")
    )
    parser.add_argument(
        "--download-media",
        choices=("none", "prefiltered", "kept", "all"),
        default="prefiltered",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--summary-limit", type=int, default=30)
    parser.add_argument("--publish-report", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.repo = args.repo.resolve()
    if args.input_json:
        args.input_json = args.input_json.resolve()
    if args.queries_file:
        args.queries_file = args.queries_file.resolve()
    if not args.end:
        # Recent-search indexes can lag real time briefly. A small delay avoids
        # asking the API for an end_time it has not made searchable yet.
        args.end = utc_iso(datetime.now(timezone.utc) - timedelta(seconds=30))
    end = parse_datetime(args.end)
    if args.output_dir is None:
        args.output_dir = (
            args.repo
            / "tmp"
            / f"awesome-gpt-image-2-prompts-daily-{end.strftime('%Y%m%dT%H%M%SZ')}"
        )
    else:
        args.output_dir = args.output_dir.resolve()
    try:
        provider = build_provider(args)
        result = run_pipeline(args, provider)
    except (CrawlError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    stats = result["report"]["stats"]
    print(
        json.dumps(
            {
                "output_dir": str(args.output_dir),
                "search_window": result["report"]["search_window"],
                "stats": stats,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
