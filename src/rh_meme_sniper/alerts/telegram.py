from __future__ import annotations

import html

import httpx

from rh_meme_sniper.models import CandidateToken


def _dexscreener_token_url(candidate: CandidateToken) -> str:
    return f"https://dexscreener.com/{candidate.chain}/{candidate.contract_address}"


def render_candidate_alert(candidate: CandidateToken) -> str:
    title = html.escape(candidate.name or candidate.symbol or candidate.cluster_id)
    contract_address = html.escape(candidate.contract_address)
    return (
        f"🚨 {title}\n"
        f"CA: <code>{contract_address}</code>\n"
        f"<a href=\"{_dexscreener_token_url(candidate)}\">DexScreener</a>\n"
        f"verdict: {html.escape(candidate.verdict)}\n"
        f"authenticity: {candidate.authenticity_score}\n"
        f"market: {candidate.market_score}\n"
        f"alert_score: {candidate.alert_score}\n"
        f"liq_usd: {candidate.liquidity_usd}\n"
        f"volume_1h: {candidate.volume_1h}"
    )


class TelegramAlerter:
    def __init__(self, bot_token: str, chat_id: str) -> None:
        self.bot_token = bot_token
        self.chat_id = chat_id

    def send_text(self, text: str) -> dict:
        url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        response = httpx.post(
            url,
            json={
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        response.raise_for_status()
        return response.json()
