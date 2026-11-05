"""Option quotes (Tradier, else delayed yfinance) and hedge math.

All hedge prices are computed at the mid. Quotes with a missing side or a wide
spread are flagged stale so the report never presents them as tradeable.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timezone

import requests

log = logging.getLogger("guardian.options")


class QuoteSource:
    name = "none"

    def expirations(self, symbol: str) -> list[date]:
        return []

    def chain(self, symbol: str, expiry: date) -> list[dict]:
        return []


class Tradier(QuoteSource):
    def __init__(self, token: str, sandbox: bool = False):
        self.base = "https://sandbox.tradier.com/v1" if sandbox else "https://api.tradier.com/v1"
        self.h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        self.name = "tradier-sandbox (delayed)" if sandbox else "tradier"

    def _get(self, path, **params):
        r = requests.get(f"{self.base}{path}", headers=self.h, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def expirations(self, symbol):
        d = (self._get("/markets/options/expirations", symbol=symbol).get("expirations") or {}).get("date") or []
        d = [d] if isinstance(d, str) else d
        return [date.fromisoformat(x) for x in d]

    def chain(self, symbol, expiry):
        data = self._get("/markets/options/chains", symbol=symbol,
                         expiration=expiry.isoformat(), greeks="true")
        opts = (data.get("options") or {}).get("option") or []
        opts = [opts] if isinstance(opts, dict) else opts
        out = []
        for o in opts:
            ts = o.get("bid_date") or o.get("trade_date")
            out.append({
                "right": "P" if o.get("option_type") == "put" else "C",
                "strike": float(o["strike"]),
                "bid": o.get("bid"), "ask": o.get("ask"), "last": o.get("last"),
                "iv": ((o.get("greeks") or {}).get("mid_iv")),
                "quote_time": datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat() if ts else None,
            })
        return out


class YahooOptions(QuoteSource):
    name = "yfinance (delayed, may be stale)"

    def expirations(self, symbol):
        import yfinance as yf
        return [date.fromisoformat(x) for x in yf.Ticker(symbol).options]

    def chain(self, symbol, expiry):
        import yfinance as yf
        ch = yf.Ticker(symbol).option_chain(expiry.isoformat())
        out = []
        for right, df in (("C", ch.calls), ("P", ch.puts)):
            for _, r in df.iterrows():
                lt = r.get("lastTradeDate")
                out.append({
                    "right": right, "strike": float(r["strike"]),
                    "bid": float(r["bid"]) if r["bid"] == r["bid"] else None,
                    "ask": float(r["ask"]) if r["ask"] == r["ask"] else None,
                    "last": float(r["lastPrice"]) if r["lastPrice"] == r["lastPrice"] else None,
                    "iv": float(r["impliedVolatility"]) if r["impliedVolatility"] == r["impliedVolatility"] else None,
                    "quote_time": lt.isoformat() if hasattr(lt, "isoformat") else None,
                })
        return out


def quote_source() -> QuoteSource:
    tok = os.environ.get("TRADIER_TOKEN")
    if tok:
        return Tradier(tok, sandbox=os.environ.get("TRADIER_SANDBOX", "").lower() in ("1", "true", "yes"))
    return YahooOptions()


def _leg_quote(chain: list[dict], right: str, strike: float, stale_spread_pct: float) -> dict:
    match = [o for o in chain if o["right"] == right and abs(o["strike"] - strike) < 1e-6]
    if not match:
        return {"found": False, "stale": True, "why": "strike not listed"}
    o = dict(match[0])
    bid, ask = o.get("bid") or 0, o.get("ask") or 0
    why = []
    if bid <= 0 or ask <= 0:
        why.append("missing bid or ask")
    elif ask < bid:
        why.append("crossed quote")
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else None
    if mid and (ask - bid) / mid * 100 > stale_spread_pct:
        why.append(f"spread {round((ask - bid) / mid * 100)}% of mid")
    o.update({"found": True, "mid": round(mid, 2) if mid else None,
              "stale": bool(why), "why": ", ".join(why) or None})
    return o


def price_structure(spec: dict, chain: list[dict], spot: float | None,
                    shares_held: float, today: date, stale_spread_pct: float) -> dict:
    """Cost, max payout and breakeven at mid for puts, put spreads and collars."""
    n = int(spec["contracts"])
    expiry = date.fromisoformat(str(spec["expiry"]))
    legs = []
    for leg in spec["legs"]:
        q = _leg_quote(chain, leg["right"], float(leg["strike"]), stale_spread_pct)
        legs.append({**leg, "contracts": n, "quote": q})
    res = {
        "name": spec.get("name"), "underlying": spec["underlying"], "kind": spec["kind"],
        "expiry": expiry.isoformat(), "dte": (expiry - today).days, "contracts": n,
        "spot": spot, "legs": legs,
        "stale": any(l["quote"]["stale"] for l in legs),
        "order_note": "limit order at mid, never market",
    }
    if any(l["quote"].get("mid") is None for l in legs):
        res["priced"] = False
        return res
    sign = {"buy": 1, "sell": -1}
    net = sum(sign[l["side"]] * l["quote"]["mid"] for l in legs)  # per share, + = debit
    res.update({"priced": True, "net_per_share": round(net, 2),
                "net_cost_usd": round(net * 100 * n, 2)})
    longs = sorted(l["strike"] for l in legs if l["side"] == "buy" and l["right"] == "P")
    shorts_p = sorted(l["strike"] for l in legs if l["side"] == "sell" and l["right"] == "P")
    shorts_c = sorted(l["strike"] for l in legs if l["side"] == "sell" and l["right"] == "C")
    kind = spec["kind"]
    if kind == "put_spread" and longs and shorts_p:
        width = longs[-1] - shorts_p[0]
        res["max_payout_usd"] = round((width - net) * 100 * n, 2)
        res["breakeven"] = round(longs[-1] - net, 2)
        res["protects_between"] = [shorts_p[0], longs[-1]]
    elif kind == "put" and longs:
        res["max_payout_usd"] = round((longs[-1] - net) * 100 * n, 2)
        res["breakeven"] = round(longs[-1] - net, 2)
    elif kind == "collar" and longs and shorts_c:
        res["floor"] = longs[-1]
        res["cap"] = shorts_c[0]
        if spot:
            res["max_loss_on_covered_shares_usd"] = round((spot - longs[-1] + net) * 100 * n, 2)
            res["max_gain_on_covered_shares_usd"] = round((shorts_c[0] - spot - net) * 100 * n, 2)
        res["covered_shares"] = 100 * n
        res["shares_held"] = shares_held
        if shares_held < 100 * n:
            res["warning"] = f"collar covers {100 * n} shares but only {shares_held:g} held: short calls would be naked"
    iv = [l["quote"].get("iv") for l in legs if l["side"] == "buy" and l["quote"].get("iv")]
    res["long_leg_iv_pct"] = round(iv[0] * 100, 1) if iv else None
    return res


def choose_expiry(exps: list[date], today: date, min_dte: int, max_dte: int) -> date | None:
    """Prefer a standard monthly (third Friday) inside the DTE window."""
    inside = [e for e in exps if min_dte <= (e - today).days <= max_dte]
    monthly = [e for e in inside if e.weekday() == 4 and 15 <= e.day <= 21]
    pick = monthly or inside
    return pick[0] if pick else None


def nearest_strike(chain: list[dict], right: str, target: float) -> float | None:
    strikes = sorted({o["strike"] for o in chain if o["right"] == right})
    return min(strikes, key=lambda s: abs(s - target)) if strikes else None


def auto_sector_spread(etf: str, cluster_value: float, spot: float, src: QuoteSource,
                       today: date, cfg: dict, stale_spread_pct: float) -> dict:
    exp = choose_expiry(src.expirations(etf), today, cfg["min_dte"], cfg["max_dte"])
    if not exp:
        return {"name": f"{etf} auto put spread", "priced": False, "why": "no expiry in DTE window"}
    chain = src.chain(etf, exp)
    k_long = nearest_strike(chain, "P", spot * (1 - cfg["long_otm_pct"] / 100))
    k_short = nearest_strike(chain, "P", spot * (1 - cfg["short_otm_pct"] / 100))
    if not k_long or not k_short or k_short >= k_long:
        return {"name": f"{etf} auto put spread", "priced": False, "why": "strikes unavailable"}
    contracts = max(1, round(cluster_value / (spot * 100)))
    spec = {"name": f"{etf} put spread sized to cluster", "underlying": etf, "kind": "put_spread",
            "expiry": exp.isoformat(), "contracts": contracts,
            "legs": [{"side": "buy", "right": "P", "strike": k_long},
                     {"side": "sell", "right": "P", "strike": k_short}]}
    res = price_structure(spec, chain, spot, 0, today, stale_spread_pct)
    res["cluster_value_usd"] = round(cluster_value, 2)
    res["notional_usd"] = round(contracts * 100 * spot, 2)
    return res
