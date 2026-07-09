"""Minimal Apify client for Phase 1/2 tweet scraping."""

from __future__ import annotations

import httpx


class ApifyClient:
    def __init__(self, api_token: str, actor_id: str) -> None:
        self.api_token = api_token
        self.actor_id = actor_id
        self.base_url = 'https://api.apify.com/v2'

    def run_actor(self, actor_input: dict) -> list[dict]:
        run_url = f'{self.base_url}/acts/{self.actor_id}/run-sync-get-dataset-items'
        response = httpx.post(run_url, params={'token': self.api_token}, json=actor_input, timeout=120)
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []
