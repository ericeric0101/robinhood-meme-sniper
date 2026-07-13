from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Protocol

import httpx

from rh_meme_sniper.config import settings
from rh_meme_sniper.sources.apify_client import ApifyClient
from rh_meme_sniper.sources.apify_x import build_search_input


class TwitterAPIIOAccessError(RuntimeError):
    """Raised when TwitterAPI.io rejects a request due to auth/credits/plan limits."""


class XSearchProvider(Protocol):
    provider_name: str

    def search_tweets(
        self,
        *,
        query: str,
        max_items: int,
        sort: str,
        tweet_language: str | None,
    ) -> list[dict]: ...

    def get_user_tweets(self, *, user_name: str, max_items: int) -> list[dict]: ...

    def get_account_balance(self) -> dict[str, int] | None: ...


@dataclass(slots=True)
class ApifyXProvider:
    api_token: str | None
    actor_id: str
    provider_name: str = 'apify'

    def get_account_balance(self) -> dict[str, int] | None:
        return None

    def get_user_tweets(self, *, user_name: str, max_items: int) -> list[dict]:
        return self.search_tweets(query=f'from:{user_name}', max_items=max_items, sort='Latest', tweet_language=None)

    def search_tweets(
        self,
        *,
        query: str,
        max_items: int,
        sort: str,
        tweet_language: str | None,
    ) -> list[dict]:
        if not self.api_token:
            raise RuntimeError('APIFY_API_TOKEN is missing in .env')

        client = ApifyClient(self.api_token, self.actor_id)
        actor_input = build_search_input(
            query,
            max_items=max_items,
            sort=sort,
            tweet_language=tweet_language,
            actor_id=self.actor_id,
        )
        run = client.run_actor(actor_input)
        return [item for item in run.items if isinstance(item, dict)]


@dataclass(slots=True)
class TwitterAPIIOProvider:
    api_key: str | None
    base_url: str = 'https://api.twitterapi.io'
    provider_name: str = 'twitterapiio'

    def get_account_balance(self) -> dict[str, int] | None:
        if not self.api_key:
            raise RuntimeError('TWITTERAPIIO_API_KEY is missing in .env')
        response = httpx.get(
            f'{self.base_url.rstrip("/")}/oapi/my/info',
            headers={'x-api-key': self.api_key},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = response.text.strip() or str(exc)
            raise TwitterAPIIOAccessError(f'TwitterAPI.io balance check failed: {message}') from exc
        payload = response.json()
        if not isinstance(payload, dict):
            return None
        return {
            'recharge_credits': int(payload.get('recharge_credits') or 0),
            'total_bonus_credits': int(payload.get('total_bonus_credits') or 0),
        }

    @staticmethod
    def _map_query_type(sort: str) -> str:
        normalized = (sort or 'Latest').strip()
        if normalized == 'Top':
            return 'Top'
        return 'Latest'

    @staticmethod
    def _augment_query(query: str, tweet_language: str | None) -> str:
        return query.strip()

    @staticmethod
    def _extract_tweets_from_payload(payload: dict) -> list[dict]:
        data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
        tweets = payload.get('tweets') or data.get('tweets') or []
        return [item for item in tweets if isinstance(item, dict)]

    def get_user_tweets(self, *, user_name: str, max_items: int) -> list[dict]:
        if not self.api_key:
            raise RuntimeError('TWITTERAPIIO_API_KEY is missing in .env')

        endpoint = f'{self.base_url.rstrip("/")}/twitter/user/last_tweets'
        headers = {'X-API-Key': self.api_key}
        tweets: list[dict] = []
        cursor: str | None = None
        while len(tweets) < max_items:
            params = {'userName': user_name}
            if cursor:
                params['cursor'] = cursor
            response = httpx.get(endpoint, params=params, headers=headers, timeout=30)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                message = response.text.strip() or str(exc)
                raise TwitterAPIIOAccessError(f'TwitterAPI.io user timeline request failed: {message}') from exc
            payload = response.json()
            page_tweets = self._extract_tweets_from_payload(payload)
            tweets.extend(page_tweets)
            data = payload.get('data') if isinstance(payload.get('data'), dict) else {}
            has_next_page = bool(payload.get('has_next_page') or data.get('has_next_page'))
            cursor = str(payload.get('next_cursor') or data.get('next_cursor') or '').strip() or None
            if not has_next_page or not cursor or not page_tweets:
                break
        return tweets[:max_items]

    def search_tweets(
        self,
        *,
        query: str,
        max_items: int,
        sort: str,
        tweet_language: str | None,
    ) -> list[dict]:
        if not self.api_key:
            raise RuntimeError('TWITTERAPIIO_API_KEY is missing in .env')

        endpoint = f'{self.base_url.rstrip("/")}/twitter/tweet/advanced_search'
        params = {
            'query': self._augment_query(query, tweet_language),
            'queryType': self._map_query_type(sort),
        }
        headers = {'X-API-Key': self.api_key}
        tweets: list[dict] = []
        cursor: str | None = None

        while len(tweets) < max_items:
            page_params = dict(params)
            if cursor:
                page_params['cursor'] = cursor
            response = httpx.get(endpoint, params=page_params, headers=headers, timeout=30)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                message = None
                try:
                    payload = response.json()
                    if isinstance(payload, dict):
                        message = payload.get('message') or payload.get('error')
                except Exception:
                    message = None
                if not message:
                    message = response.text.strip() or str(exc)
                raise TwitterAPIIOAccessError(f'TwitterAPI.io request failed: {message}') from exc
            payload = response.json()
            page_tweets = [item for item in payload.get('tweets', []) if isinstance(item, dict)]
            tweets.extend(page_tweets)
            has_next_page = bool(payload.get('has_next_page'))
            cursor = str(payload.get('next_cursor') or '').strip() or None
            if not has_next_page or not cursor or not page_tweets:
                break

        return tweets[:max_items]


def get_x_provider(provider_name: str | None = None, actor_id: str | None = None) -> XSearchProvider:
    selected = (provider_name or os.getenv('X_PROVIDER') or settings.x_provider or 'apify').strip().lower()
    if selected in {'apify'}:
        return ApifyXProvider(
            api_token=os.getenv('APIFY_API_TOKEN') or settings.apify_api_token,
            actor_id=actor_id or os.getenv('APIFY_X_ACTOR_ID') or settings.apify_x_actor_id,
        )
    if selected in {'twitterapiio', 'twitterapi.io'}:
        return TwitterAPIIOProvider(api_key=os.getenv('TWITTERAPIIO_API_KEY') or os.getenv('TWITTERAPI_IO_KEY') or settings.twitterapiio_api_key)
    raise ValueError(f'Unsupported X provider: {selected}')
