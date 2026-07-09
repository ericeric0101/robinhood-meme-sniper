from __future__ import annotations

from typing import Any

from rh_meme_sniper.extract.ca_parser import extract_contract_addresses
from rh_meme_sniper.models import RawEvent



def build_search_input(query: str, max_items: int = 100, *, sort: str = "Latest") -> dict[str, Any]:
    return {
        "searchTerms": [query],
        "maxItems": max_items,
        "sort": sort,
    }



def normalize_tweet_item(item: dict[str, Any]) -> RawEvent:
    text = item.get("text") or item.get("fullText") or ""
    url = item.get("url") or item.get("twitterUrl")
    author = item.get("author") or {}
    metrics = {
        "like_count": int(item.get("likeCount") or item.get("favorite_count") or 0),
        "retweet_count": int(item.get("retweetCount") or item.get("retweet_count") or 0),
        "reply_count": int(item.get("replyCount") or item.get("reply_count") or 0),
        "quote_count": int(item.get("quoteCount") or item.get("quote_count") or 0),
    }
    urls = [url] if url else []
    return RawEvent(
        source="x",
        source_id=str(item.get("id") or item.get("tweetId") or ""),
        observed_at=str(item.get("createdAt") or item.get("created_at") or ""),
        author_handle=author.get("userName") or author.get("username") or item.get("author_username"),
        text=text,
        urls=urls,
        contract_addresses=extract_contract_addresses(text),
        symbols=[value for value in [item.get("symbol")] if value],
        names=[value for value in [item.get("name")] if value],
        metrics=metrics,
    )
