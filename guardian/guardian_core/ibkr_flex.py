"""IBKR Flex Web Service client: positions, cost basis, account value.

Two-step protocol: SendRequest returns a reference code, GetStatement returns
the XML once IBKR has generated it (error 1019 means "not ready yet").
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime

import requests

BASE = "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService"
HEADERS = {"User-Agent": "guardian-risk-report/1.0"}
RETRYABLE = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}


class FlexError(RuntimeError):
    pass


def _get(url: str, params: dict) -> ET.Element:
    r = requests.get(url, params=params, headers=HEADERS, timeout=60)
    r.raise_for_status()
    try:
        return ET.fromstring(r.content)
    except ET.ParseError as e:
        raise FlexError(f"IBKR returned non-XML: {r.text[:200]}") from e


def fetch_statement(token: str, query_id: str, attempts: int = 12, wait_s: float = 10) -> ET.Element:
    root = _get(f"{BASE}/SendRequest", {"t": token, "q": query_id, "v": "3"})
    if root.findtext("Status") != "Success":
        raise FlexError(
            f"SendRequest failed: {root.findtext('ErrorCode')} {root.findtext('ErrorMessage')}"
        )
    ref = root.findtext("ReferenceCode")
    url = root.findtext("Url") or f"{BASE}/GetStatement"
    for _ in range(attempts):
        time.sleep(wait_s)
        stmt = _get(url, {"t": token, "q": ref, "v": "3"})
        if stmt.tag == "FlexQueryResponse":
            return stmt
        code = stmt.findtext("ErrorCode") or ""
        if code not in RETRYABLE:
            raise FlexError(f"GetStatement failed: {code} {stmt.findtext('ErrorMessage')}")
    raise FlexError("Flex statement not ready after retries")


def _f(attrs: dict, *keys: str) -> float | None:
    for k in keys:
        v = attrs.get(k)
        if v not in (None, ""):
            try:
                return float(v)
            except ValueError:
                pass
    return None


def parse_statement(root: ET.Element) -> dict:
    """Extract stock positions, option positions, account value and any open
    orders (only if the query happens to include an order section)."""
    stmt = root.find(".//FlexStatement")
    if stmt is None:
        raise FlexError("No FlexStatement in response")
    stocks, options = [], []
    for el in stmt.iter("OpenPosition"):
        a = el.attrib
        # Lot-level rows duplicate the summary row.
        if a.get("levelOfDetail", "SUMMARY").upper() not in ("SUMMARY", ""):
            continue
        cat = a.get("assetCategory", "")
        qty = _f(a, "position") or 0.0
        row = {
            "symbol": a.get("symbol", "").split()[0] if cat == "STK" else a.get("symbol", ""),
            "shares": qty,
            "avg_cost": _f(a, "costBasisPrice", "openPrice"),
            "mark": _f(a, "markPrice"),
            "value": _f(a, "positionValue"),
            "unrealized_pnl": _f(a, "fifoPnlUnrealized"),
            "currency": a.get("currency"),
        }
        if cat == "STK":
            stocks.append(row)
        elif cat in ("OPT", "FOP"):
            expiry = a.get("expiry", "")
            row.update({
                "underlying": a.get("underlyingSymbol"),
                "right": (a.get("putCall") or "")[:1].upper(),
                "strike": _f(a, "strike"),
                "expiry": datetime.strptime(expiry, "%Y%m%d").date().isoformat() if expiry else None,
                "multiplier": _f(a, "multiplier") or 100.0,
                "cost_basis": _f(a, "costBasisMoney"),
            })
            options.append(row)

    nav = None
    for el in stmt.iter("EquitySummaryByReportDateInBase"):
        nav = _f(el.attrib, "total") or nav
    if nav is None:
        for el in stmt.iter("ChangeInNAV"):
            nav = _f(el.attrib, "endingValue") or nav
    cash = None
    for el in stmt.iter("CashReportCurrency"):
        if el.attrib.get("currency") == "BASE_SUMMARY":
            cash = _f(el.attrib, "endingCash")

    orders = []
    for tag in ("OpenOrder", "PendingOrder"):
        for el in stmt.iter(tag):
            a = el.attrib
            orders.append({
                "symbol": a.get("symbol"),
                "side": a.get("buySell"),
                "type": a.get("orderType"),
                "shares": abs(_f(a, "quantity", "remainingQuantity") or 0),
                "stop": _f(a, "auxPrice", "stopPrice", "price"),
                "tif": a.get("timeInForce"),
            })

    return {
        "account_id": stmt.attrib.get("accountId"),
        "as_of": stmt.attrib.get("whenGenerated") or stmt.attrib.get("toDate"),
        "stocks": stocks,
        "options": options,
        "net_liquidation": nav,
        "cash": cash,
        "orders": orders or None,
    }


def load_positions(token: str, query_id: str) -> dict:
    return parse_statement(fetch_statement(token, query_id))
