"""Simple public DexScreener client for Phase 1/2."""

from __future__ import annotations

import httpx


class DexScreenerClient:
    def __init__(self, base_url: str = 'https://api.dexscreener.com') -> None:
        self.base_url = base_url.rstrip('/')

    def search_pairs(self, query: str) -> list[dict]:
        url = f'{self.base_url}/latest/dex/search/'
        response = httpx.get(url, params={'q': query}, timeout=20)
        response.raise_for_status()
        return response.json().get('pairs', [])
