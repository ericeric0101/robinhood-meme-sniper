from __future__ import annotations

import re

EVM_ADDRESS_RE = re.compile(r"0x[a-fA-F0-9]{40}")
URL_RE = re.compile(r"https?://[^\s)]+")


def _dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if value not in seen:
            out.append(value)
            seen.add(value)
    return out



def extract_contract_addresses(text: str | None) -> list[str]:
    if not text:
        return []
    return _dedupe_keep_order(EVM_ADDRESS_RE.findall(text))



def extract_urls(text: str | None) -> list[str]:
    if not text:
        return []
    return _dedupe_keep_order(URL_RE.findall(text))
