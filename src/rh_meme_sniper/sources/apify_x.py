from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from rh_meme_sniper.extract.ca_parser import extract_contract_addresses
from rh_meme_sniper.models import RawEvent


DEFAULT_ACTOR_ID = "xtdata~twitter-x-scraper"
XTDATA_ACTOR_ID = "xtdata~twitter-x-scraper"
VALID_SORTS = {"Latest", "Top", "Latest + Top"}
XTDATA_SORT_MAP = {
    "Latest": "Latest",
    "Top": "Top",
    "Latest + Top": "Both",
}


def build_search_input(
    query: str,
    max_items: int = 100,
    *,
    sort: str = "Top",
    tweet_language: str | None = "en",
    actor_id: str = DEFAULT_ACTOR_ID,
) -> dict[str, Any]:
    if sort not in VALID_SORTS:
        raise ValueError(f"Unsupported Apify sort: {sort}")

    normalized_actor_id = actor_id.replace("/", "~") if actor_id else DEFAULT_ACTOR_ID
    payload: dict[str, Any]

    if normalized_actor_id == XTDATA_ACTOR_ID:
        payload = {
            "searchTerms": [query],
            "sort": XTDATA_SORT_MAP[sort],
            "includeSearchTerms": True,
        }
    else:
        payload = {
            "searchTerms": [query],
            "sort": sort,
        }

    if max_items > 0:
        payload["maxItems"] = max_items
    if tweet_language:
        payload["tweetLanguage"] = tweet_language
    return payload


def is_no_results_item(item: dict[str, Any]) -> bool:
    return bool(item.get("noResults") or item.get("demo"))


def _to_iso8601(value: str | None) -> str:
    if not value:
        return ""

    raw = value.strip()
    if not raw:
        return ""

    if raw.endswith("Z"):
        return raw

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = parsedate_to_datetime(raw)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z")


def _build_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for candidate in [item.get("url"), item.get("twitterUrl")]:
        if candidate and candidate not in urls:
            urls.append(candidate)
    return urls


def _author_handle(item: dict[str, Any]) -> str | None:
    author = item.get("author") or {}
    return (
        author.get("userName")
        or author.get("username")
        or author.get("screen_name")
        or item.get("author_username")
    )


def _engagement_metrics(item: dict[str, Any]) -> dict[str, int]:
    like_count = int(item.get("likeCount") or item.get("favorite_count") or item.get("likes") or 0)
    retweet_count = int(item.get("retweetCount") or item.get("retweet_count") or item.get("retweets") or 0)
    reply_count = int(item.get("replyCount") or item.get("reply_count") or item.get("replies") or 0)
    quote_count = int(item.get("quoteCount") or item.get("quote_count") or 0)
    return {
        "like_count": like_count,
        "retweet_count": retweet_count,
        "reply_count": reply_count,
        "quote_count": quote_count,
        "engagement_total": like_count + retweet_count + reply_count + quote_count,
    }


def _tweet_text(item: dict[str, Any]) -> str:
    return item.get("text") or item.get("fullText") or item.get("full_text") or ""


def normalize_tweet_item(item: dict[str, Any]) -> RawEvent:
    text = _tweet_text(item)
    urls = _build_urls(item)
    created_at = _to_iso8601(str(item.get("createdAt") or item.get("created_at") or ""))
    metrics = _engagement_metrics(item)
    return RawEvent(
        source="x",
        source_id=str(item.get("id") or item.get("tweetId") or item.get("tweet_id") or ""),
        observed_at=created_at,
        author_handle=_author_handle(item),
        text=text,
        urls=urls,
        contract_addresses=extract_contract_addresses(text),
        symbols=[value for value in [item.get("symbol")] if value],
        names=[value for value in [item.get("name")] if value],
        metrics=metrics,
    )


def tweet_record(item: dict[str, Any]) -> dict[str, Any]:
    author = item.get("author") or {}
    metrics = _engagement_metrics(item)
    return {
        "id": str(item.get("id") or item.get("tweetId") or item.get("tweet_id") or ""),
        "author": author.get("userName") or author.get("username") or author.get("screen_name") or item.get("author_username"),
        "createdAt": _to_iso8601(str(item.get("createdAt") or item.get("created_at") or "")),
        "text": _tweet_text(item),
        "url": item.get("url") or item.get("twitterUrl"),
        "engagement": metrics,
        "searchTerms": item.get("searchTerms"),
    }