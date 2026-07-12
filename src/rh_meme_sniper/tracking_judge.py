from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from rh_meme_sniper.config import settings


@dataclass(slots=True)
class TrackingJudgeDecision:
    status: str
    reason: str


class TrackingJudge(Protocol):
    def judge(self, *, query: str, candidate: Any, cluster: Any) -> dict[str, str] | TrackingJudgeDecision | None:
        ...


class OpenAICompatibleTrackingJudge:
    def __init__(self, *, endpoint: str, api_key: str, model: str, timeout: int = 30) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def judge(self, *, query: str, candidate: Any, cluster: Any) -> dict[str, str] | None:
        prompt = {
            'query': query,
            'candidate': candidate.model_dump() if hasattr(candidate, 'model_dump') else dict(candidate),
            'cluster': cluster.model_dump() if hasattr(cluster, 'model_dump') else dict(cluster),
            'instruction': (
                'Return compact JSON with keys status and reason. '
                'status must be one of ignore, watch, strong_candidate. '
                'Prefer ignore for generic brand words, generic Robinhood brand derivatives, extracted tweet blobs, or weak/non-crypto context. '
                'Do not auto-reject derivative variants like Baby Cash Cat solely because they are variants; those are usually watch unless there is stronger negative evidence. '
                'Strongly prefer strong_candidate for exact/canonical matches when the query directly matches the candidate name or symbol and market/context signals are solid. '
                'Prefer strong_candidate for specific meme token candidates with strong market/context signals, especially exact or canonical matches.'
            ),
        }
        response = httpx.post(
            self.endpoint,
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'Content-Type': 'application/json',
            },
            json={
                'model': self.model,
                'messages': [
                    {'role': 'system', 'content': 'You are a precise meme-token candidate judge.'},
                    {'role': 'user', 'content': str(prompt)},
                ],
                'response_format': {'type': 'json_object'},
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        content = (((payload.get('choices') or [{}])[0].get('message') or {}).get('content') or '').strip()
        if not content:
            return None
        import json
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None
        return {
            'status': str(parsed.get('status') or '').strip(),
            'reason': str(parsed.get('reason') or '').strip(),
        }


def get_tracking_judge_from_settings() -> TrackingJudge | None:
    if not settings.tracking_judge_enabled:
        return None
    if not settings.tracking_judge_endpoint or not settings.tracking_judge_api_key or not settings.tracking_judge_model:
        return None
    return OpenAICompatibleTrackingJudge(
        endpoint=str(settings.tracking_judge_endpoint),
        api_key=str(settings.tracking_judge_api_key),
        model=str(settings.tracking_judge_model),
        timeout=int(settings.tracking_judge_timeout_seconds),
    )
