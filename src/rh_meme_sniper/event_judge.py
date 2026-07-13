from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

import httpx

from rh_meme_sniper.config import settings


VALID_EVENT_TYPES = {
    'mention',
    'tease',
    'denial',
    'identity_linked',
    'community_coordination',
    'creator_engaged',
    'fee_or_airdrop_catalyst',
    'momentum_confirmation',
    'exit_risk',
    'contract',
    'cashtag',
    'link',
}


@dataclass(slots=True)
class EventJudgeDecision:
    event_type: str
    confidence: float
    reason: str
    risk_flags: list[str]


class EventJudge(Protocol):
    def judge(
        self,
        *,
        text: str,
        symbols: list[str],
        contracts: list[str],
        urls: list[str],
        author_handle: str | None,
    ) -> dict | EventJudgeDecision | None:
        ...


class OpenAICompatibleEventJudge:
    def __init__(self, *, endpoint: str, api_key: str, model: str, timeout: int = 30) -> None:
        self.endpoint = endpoint
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def judge(
        self,
        *,
        text: str,
        symbols: list[str],
        contracts: list[str],
        urls: list[str],
        author_handle: str | None,
    ) -> EventJudgeDecision | None:
        prompt = {
            'text': text,
            'author_handle': author_handle,
            'symbols': symbols,
            'contracts': contracts,
            'urls': urls,
            'allowed_event_types': sorted(VALID_EVENT_TYPES),
            'instruction': (
                'Classify this X/Twitter KOL activity into one lifecycle event type. '
                'Return compact JSON with keys event_type, confidence, reason, risk_flags. '
                'Use identity_linked for subtle person/entity/narrative association. '
                'Use creator_engaged when the named/linked person appears to participate or endorse. '
                'Use community_coordination for takeover/coordination around a token or CA. '
                'Use exit_risk for scam/rug/dev-dump/do-not-buy language. '
                'If uncertain, use mention with low confidence.'
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
                    {'role': 'system', 'content': 'You are a precise KOL meme-coin lifecycle event classifier.'},
                    {'role': 'user', 'content': json.dumps(prompt, ensure_ascii=False)},
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
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return None
        event_type = str(parsed.get('event_type') or '').strip()
        if event_type not in VALID_EVENT_TYPES:
            return None
        try:
            confidence = float(parsed.get('confidence') or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        risk_flags_raw = parsed.get('risk_flags') or []
        risk_flags = [str(item).strip() for item in risk_flags_raw if str(item).strip()] if isinstance(risk_flags_raw, list) else []
        return EventJudgeDecision(
            event_type=event_type,
            confidence=max(0.0, min(1.0, confidence)),
            reason=str(parsed.get('reason') or '').strip(),
            risk_flags=risk_flags,
        )


def normalize_event_judge_decision(value: dict | EventJudgeDecision | None) -> EventJudgeDecision | None:
    if value is None:
        return None
    if isinstance(value, EventJudgeDecision):
        return value if value.event_type in VALID_EVENT_TYPES else None
    if not isinstance(value, dict):
        return None
    event_type = str(value.get('event_type') or '').strip()
    if event_type not in VALID_EVENT_TYPES:
        return None
    try:
        confidence = float(value.get('confidence') or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    risk_flags_raw = value.get('risk_flags') or []
    risk_flags = [str(item).strip() for item in risk_flags_raw if str(item).strip()] if isinstance(risk_flags_raw, list) else []
    return EventJudgeDecision(
        event_type=event_type,
        confidence=max(0.0, min(1.0, confidence)),
        reason=str(value.get('reason') or '').strip(),
        risk_flags=risk_flags,
    )


def get_event_judge_from_settings() -> EventJudge | None:
    if not settings.event_judge_enabled:
        return None
    endpoint = settings.event_judge_endpoint or settings.tracking_judge_endpoint
    api_key = settings.event_judge_api_key or settings.tracking_judge_api_key
    model = settings.event_judge_model or settings.tracking_judge_model
    if not endpoint or not api_key or not model:
        return None
    return OpenAICompatibleEventJudge(
        endpoint=str(endpoint),
        api_key=str(api_key),
        model=str(model),
        timeout=int(settings.event_judge_timeout_seconds),
    )
