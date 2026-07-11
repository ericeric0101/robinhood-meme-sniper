"""Apify client helpers for Phase 1/2 tweet scraping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


FREE_PLAN_API_BLOCK_TEXT = "doesn't allow the use of API in the Free Plan"
PAID_PLAN_UPSELL_TEXT = "subscribe to a paid plan"


class ApifyError(RuntimeError):
    """Base Apify integration error."""


class ApifyAccessError(ApifyError):
    """Raised when the current Apify plan cannot use the actor via API."""


@dataclass(slots=True)
class ApifyRunResult:
    run_id: str
    status: str
    status_message: str | None
    dataset_id: str | None
    items: list[dict[str, Any]]
    log_excerpt: str | None = None


class ApifyClient:
    def __init__(self, api_token: str, actor_id: str, *, base_url: str = "https://api.apify.com/v2") -> None:
        self.api_token = api_token
        self.actor_id = actor_id
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(timeout=180)

    def _actor_url(self, suffix: str) -> str:
        return f"{self.base_url}/acts/{self.actor_id}{suffix}"

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        params = dict(kwargs.pop("params", {}) or {})
        params.setdefault("token", self.api_token)
        response = self.http.request(method, url, params=params, **kwargs)
        response.raise_for_status()
        return response

    def fetch_dataset_items(self, dataset_id: str) -> list[dict[str, Any]]:
        response = self._request("GET", f"{self.base_url}/datasets/{dataset_id}/items", params={"clean": "true", "format": "json"})
        data = response.json()
        return data if isinstance(data, list) else []

    def fetch_run_log(self, run_id: str) -> str:
        response = self._request("GET", f"{self.base_url}/actor-runs/{run_id}/log")
        return response.text

    def run_actor(self, actor_input: dict[str, Any], *, wait_for_finish: int = 180) -> ApifyRunResult:
        """Run the actor through the /runs endpoint and fetch dataset items.

        We intentionally use /runs instead of only the sync-items endpoint because
        the run metadata gives us `statusMessage`, which is necessary to
        distinguish a genuine zero-result query from Apify's free/demo-mode
        behaviour on this actor.
        """
        response = self._request(
            "POST",
            self._actor_url("/runs"),
            params={"waitForFinish": wait_for_finish},
            json=actor_input,
        )
        payload = response.json().get("data") or {}
        run_id = str(payload.get("id") or "")
        dataset_id = payload.get("defaultDatasetId")
        status_message = payload.get("statusMessage")
        items = self.fetch_dataset_items(dataset_id) if dataset_id else []

        result = ApifyRunResult(
            run_id=run_id,
            status=str(payload.get("status") or "UNKNOWN"),
            status_message=status_message,
            dataset_id=dataset_id,
            items=items,
        )

        if self._is_free_plan_api_block(result):
            result.log_excerpt = self.fetch_run_log(run_id)[:4000] if run_id else None
            raise ApifyAccessError(
                "Apify actor API access is blocked on the current FREE plan. "
                "Upgrade the Apify account to a paid plan, then rerun the same query."
            )

        return result

    @staticmethod
    def _is_free_plan_api_block(result: ApifyRunResult) -> bool:
        message = (result.status_message or "").lower()
        if FREE_PLAN_API_BLOCK_TEXT.lower() in message:
            return True
        if not result.items:
            return False
        placeholder_items = all(
            isinstance(item, dict) and (item.get("noResults") is True or item.get("demo") is True)
            for item in result.items
        )
        return placeholder_items and PAID_PLAN_UPSELL_TEXT in message
