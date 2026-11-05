"""Price history and derived levels from yfinance.

Levels use the bars *before* the latest one, so "below the 13-week low" means
the latest close broke the prior 13 weeks' range.
"""

from __future__ import annotations

import logging
import math
from datetime import date

import pandas as pd

log = logging.getLogger("guardian.market")

W13, W26, W52, M1, W1 = 65, 130, 252, 21, 5


def download(tickers: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    tickers = sorted(set(tickers))
    out: dict[str, pd.DataFrame] = {}
    if not tickers:
        return out
    raw = yf.download(tickers, period="2y", interval="1d", auto_adjust=False,
                      group_by="ticker", progress=False, threads=True)
    for t in tickers:
        try:
            df = raw[t] if isinstance(raw.columns, pd.MultiIndex) else raw
            df = df.dropna(subset=["Close"])
            if not df.empty:
                out[t] = df
        except KeyError:
            pass
    missing = [t for t in tickers if t not in out]
    if missing:
        log.warning("no price history for %s", missing)
    return out


def premarket_prices(tickers: list[str]) -> dict[str, float]:
    """Best-effort pre/post-market prices. Missing values are simply absent."""
    import yfinance as yf

    out = {}
    for t in tickers:
        try:
            info = yf.Ticker(t).info
            p = info.get("preMarketPrice") or info.get("postMarketPrice")
            if p:
                out[t] = float(p)
        except Exception as e:  # yfinance raises many types
            log.debug("premarket %s: %s", t, e)
    return out


def _pct(a: float | None, b: float | None) -> float | None:
    if a is None or b in (None, 0) or (isinstance(b, float) and math.isnan(b)):
        return None
    return round((a / b - 1) * 100, 2)


def _r(x, n=2):
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), n)


def levels(df: pd.DataFrame | None, expected_last: date | None = None, vol_window: int = 30) -> dict:
    """Last price, % changes, 13w/26w/52w lows and highs, daily volatility."""
    if df is None or len(df) < 2:
        return {"available": False}
    close, low, high = df["Close"], df["Low"], df["High"]
    last = float(close.iloc[-1])
    last_date = df.index[-1].date()
    prior_low, prior_high = low.iloc[:-1], high.iloc[:-1]

    def back(n):
        return float(close.iloc[-1 - n]) if len(close) > n else None

    rets = close.pct_change().dropna().iloc[-vol_window:]
    vol = float(rets.std()) * 100 if len(rets) >= 10 else None
    d = {
        "available": True,
        "last": _r(last),
        "last_date": last_date.isoformat(),
        "stale": bool(expected_last and last_date < expected_last),
        "chg_1d_pct": _pct(last, back(1)),
        "chg_1w_pct": _pct(last, back(W1)),
        "chg_1m_pct": _pct(last, back(M1)),
        "low_13w": _r(prior_low.iloc[-W13:].min()),
        "low_26w": _r(prior_low.iloc[-W26:].min()),
        "low_52w": _r(prior_low.iloc[-W52:].min()),
        "high_13w": _r(prior_high.iloc[-W13:].max()),
        "high_26w": _r(prior_high.iloc[-W26:].max()),
        "high_52w": _r(prior_high.iloc[-W52:].max()),
        "daily_vol_pct": _r(vol),
        "history_days": len(df),
    }
    d["pct_from_13w_low"] = _pct(last, d["low_13w"])
    d["pct_from_26w_low"] = _pct(last, d["low_26w"])
    d["pct_from_52w_high"] = _pct(last, d["high_52w"])
    d["below_13w_low"] = d["low_13w"] is not None and last < d["low_13w"]
    d["below_26w_low"] = d["low_26w"] is not None and last < d["low_26w"]
    d["new_52w_low"] = d["low_52w"] is not None and float(low.iloc[-1]) <= d["low_52w"]
    if len(df) < W52:
        d["note"] = f"only {len(df)} days of history; 52w levels cover less than a year"
    return d


def earnings_date(ticker: str, today: date) -> str | None:
    import yfinance as yf

    try:
        cal = yf.Ticker(ticker).calendar
        dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if dates:
            future = sorted(d for d in dates if d >= today)
            return future[0].isoformat() if future else None
    except Exception as e:
        log.debug("earnings %s: %s", ticker, e)
    return None
