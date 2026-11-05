"""Stocktwits public API: trending symbols and per-symbol bull/bear ratio."""

from __future__ import annotations

import logging

import requests

log = logging.getLogger("guardian.sentiment")
API = "https://api.stocktwits.com/api/2"
HEADERS = {"User-Agent": "Mozilla/5.0 (guardian-risk-report)", "Accept": "application/json"}


def _get(path: str) -> dict | None:
    try:
        r = requests.get(f"{API}/{path}", headers=HEADERS, timeout=20)
        if r.status_code != 200:
            log.warning("stocktwits %s -> HTTP %s", path, r.status_code)
            return None
        return r.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("stocktwits %s: %s", path, e)
        return None


def trending() -> list[str] | None:
    data = _get("trending/symbols.json")
    if not data:
        return None
    return [s.get("symbol") for s in data.get("symbols", []) if s.get("symbol")]


def symbol_sentiment(symbol: str) -> dict:
    data = _get(f"streams/symbol/{symbol}.json")
    if not data:
        return {"available": False}
    bull = bear = 0
    msgs = data.get("messages", [])
    for m in msgs:
        s = ((m.get("entities") or {}).get("sentiment") or {}).get("basic")
        if s == "Bullish":
            bull += 1
        elif s == "Bearish":
            bear += 1
    tagged = bull + bear
    ratio = round(bull / tagged * 100) if tagged else None
    flag = None
    if tagged >= 10 and ratio is not None:
        if ratio >= 85:
            flag = "EUPHORIA"
        elif ratio <= 25:
            flag = "CAPITULATION"
    return {
        "available": True,
        "messages": len(msgs),
        "bullish": bull,
        "bearish": bear,
        "bull_pct_of_tagged": ratio,
        "watchers": (data.get("symbol") or {}).get("watchlist_count"),
        "flag": flag,
    }


def collect(symbols: list[str]) -> dict:
    trend = trending()
    per = {}
    for s in symbols:
        d = symbol_sentiment(s)
        d["trending"] = (s in trend) if trend is not None else None
        per[s] = d
    return {"trending_list_available": trend is not None, "symbols": per,
            "note": "bull % is of messages the authors tagged; last ~30 messages only"}
