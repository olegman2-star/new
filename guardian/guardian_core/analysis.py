"""Deterministic risk rules. The language model writes the report; the numbers
that enforce the hard rules (stops, open risk, sector failures, status floor)
are computed here so they cannot drift between runs."""

from __future__ import annotations

import math

SEVERITY = {"GREEN": 0, "YELLOW": 1, "RED": 2}


# ---------------------------------------------------------------- stops

def avoid_round(price: float) -> float:
    """Nudge a stop off round numbers (whole dollars, quarters), where stops
    cluster and get run. Always nudges down, so the stop only gets wider."""
    p = math.floor(price * 100 + 1e-6) / 100
    cents = int(round(p * 100)) % 100
    if cents % 25 == 0:
        p -= 0.03 if p < 20 else 0.07 if p < 200 else 0.13
    return round(p, 2)


def compute_stop(last: float, lv: dict, vol_multiple: float, buffer_pct: float) -> dict:
    """Just below the nearest meaningful level that is at least
    vol_multiple x daily volatility under the price."""
    vol = lv.get("daily_vol_pct")
    if vol is None:
        return {"stop": None, "basis": "no volatility data"}
    floor = last * (1 - vol_multiple * vol / 100)  # stop must be at or below this
    cands = []
    for name in ("low_13w", "low_26w", "low_52w"):
        L = lv.get(name)
        if L is not None and L < last:
            cands.append((L, name))
    # A level only counts if it sits between 1.5x and 3x daily vol below the
    # price; a level further away (e.g. a 52w low 80% down) is not "nearest".
    too_far = last * (1 - 2 * vol_multiple * vol / 100)
    for L, name in sorted(cands, reverse=True):  # nearest level first
        s = L * (1 - buffer_pct / 100)
        if s < too_far:
            break
        if s <= floor:
            return {"stop": avoid_round(s), "basis": f"below {name.replace('_', ' ')} {L:.2f}"}
    return {"stop": avoid_round(floor),
            "basis": f"{vol_multiple}x daily vol ({vol:.1f}%)" + ("" if cands else "; price below all levels")}


def stop_audit(positions: list[dict], lvls: dict, stop_book: list[dict], ratchet: dict,
               cfg: dict, account_value: float | None) -> dict:
    r = cfg["risk"]
    rows, lottery, total_risk, uncovered_total = [], [], 0.0, 0.0
    for p in positions:
        sym, shares = p["symbol"], p["shares"]
        lv = lvls.get(sym) or {}
        last = p.get("last") or lv.get("last")
        value = (last or 0) * shares
        if shares <= 0:
            continue
        if sym in cfg.get("lottery_tickets", []) or (last and value < r["lottery_threshold_usd"]):
            lottery.append({"symbol": sym, "shares": shares, "value": round(value, 2) if last else None})
            continue
        if not last:
            rows.append({"symbol": sym, "shares": shares, "error": "no price: cannot audit"})
            continue

        live = [o for o in stop_book if o["symbol"] == sym]
        covered = sum(o["shares"] for o in live)
        live_stop = max((o["stop"] for o in live if o.get("stop")), default=None)
        calc = compute_stop(last, lv, r["stop_vol_multiple"], r["stop_buffer_pct"])
        prior = ratchet.get(sym)

        candidates = [x for x in (calc["stop"], live_stop, prior) if x is not None]
        suggested = max(candidates) if candidates else None
        source = "computed"
        if suggested is not None and suggested != calc["stop"]:
            source = "live stop (never lower)" if suggested == live_stop else "prior suggestion (never lower)"

        breached = suggested is not None and suggested >= last
        vol = lv.get("daily_vol_pct")
        tight = (not breached and suggested is not None and vol is not None
                 and (last - suggested) / last * 100 < r["stop_vol_multiple"] * vol)

        uncovered = max(0.0, shares - covered)
        # Open risk: covered shares at their live stop, uncovered at the
        # suggested stop (i.e. the risk once Oleg places the suggestion).
        risk = 0.0
        for o in live:
            if o.get("stop"):
                risk += max(0.0, last - o["stop"]) * o["shares"]
        if suggested is not None:
            risk += max(0.0, last - suggested) * uncovered
        total_risk += risk
        uncovered_total += uncovered * last

        avg = p.get("avg_cost")
        if breached:
            action = "PRICE AT/BELOW STOP LEVEL: plan says exit"
        elif uncovered > 0:
            action = f"PLACE GTC stop {uncovered:g} sh @ {suggested:.2f}"
        elif live_stop is not None and suggested > live_stop:
            action = f"RAISE stop {live_stop:.2f} -> {suggested:.2f}"
        else:
            action = "OK"
        if live and len({o.get('stop') for o in live}) > 1:
            action += " (stops at different levels: consolidate upward)"

        rows.append({
            "symbol": sym, "shares": shares, "last": round(last, 2),
            "avg_cost": avg,
            "covered_shares": covered, "uncovered_shares": uncovered,
            "critical_uncovered": uncovered > 0,
            "live_stop": live_stop, "computed_stop": calc["stop"], "computed_basis": calc["basis"],
            "prior_suggestion": prior, "suggested_stop": suggested, "suggested_source": source,
            "pct_below_last": round((last - suggested) / last * 100, 2) if suggested else None,
            "tighter_than_vol_rule": tight,
            "breached": breached,
            "risk_if_hit_usd": round(risk, 2),
            "pnl_vs_cost_if_hit_usd": round((suggested - avg) * shares, 2) if (suggested and avg) else None,
            "action": action,
        })

    present = {p["symbol"] for p in positions}
    for sym in cfg.get("lottery_tickets", []):
        if sym not in present:
            lottery.append({"symbol": sym, "shares": None, "value": None, "note": "not in positions data"})

    risk_pct = round(total_risk / account_value * 100, 2) if account_value else None
    return {
        "rows": rows,
        "lottery_tickets": lottery,
        "total_open_risk_usd": round(total_risk, 2),
        "total_open_risk_pct_account": risk_pct,
        "uncovered_value_usd": round(uncovered_total, 2),
        "risk_limit_pct": r["max_open_risk_pct"],
        "risk_over_limit": risk_pct is not None and risk_pct > r["max_open_risk_pct"],
        "method": ("open risk = (last - stop) x shares; covered shares at their live stop, "
                   "uncovered shares at the suggested stop"),
    }


def next_ratchet(audit: dict, prev: dict) -> dict:
    """Suggested stops to remember, never lower than before."""
    out = dict(prev)
    for row in audit["rows"]:
        s = row.get("suggested_stop")
        if s is not None:
            out[row["symbol"]] = max(s, prev.get(row["symbol"], s))
    return out


# ---------------------------------------------------------------- sectors / flags

def sector_filter(etf_levels: dict, cfg: dict) -> dict:
    sf = cfg["sector_filter"]
    out = {}
    for etf, lv in etf_levels.items():
        if not lv.get("available"):
            out[etf] = {"status": "NO DATA"}
            continue
        reasons = []
        if sf["fail_below_13w_low"] and lv["below_13w_low"]:
            reasons.append(f"below 13w low {lv['low_13w']}")
        if lv["chg_1m_pct"] is not None and lv["chg_1m_pct"] < sf["fail_month_drop_pct"]:
            reasons.append(f"down {lv['chg_1m_pct']}% in a month")
        out[etf] = {"status": "FAIL" if reasons else "PASS", "reasons": reasons,
                    "last": lv["last"], "chg_1m_pct": lv["chg_1m_pct"],
                    "low_13w": lv["low_13w"], "pct_from_13w_low": lv["pct_from_13w_low"],
                    "stale": lv["stale"]}
    return out


def position_flags(lv: dict, earnings_tdays: int | None, warn_days: int) -> list[str]:
    f = []
    if lv.get("below_13w_low") or lv.get("below_26w_low"):
        f.append("BREAKDOWN")
    if lv.get("new_52w_low"):
        f.append("NEW 52W LOW")
    if earnings_tdays is not None and earnings_tdays <= warn_days:
        f.append(f"EARNINGS in {earnings_tdays} trading days")
    if lv.get("stale"):
        f.append("STALE PRICE")
    return f


def status_floor(audit: dict, sectors: dict, chip_warning: bool, data_gaps: list[str],
                 any_breakdown: bool) -> tuple[str, list[str]]:
    reasons_red, reasons_yellow = [], []
    if audit["risk_over_limit"]:
        reasons_red.append(f"open risk {audit['total_open_risk_pct_account']}% of account exceeds "
                           f"{audit['risk_limit_pct']}% limit")
    if any(r.get("breached") for r in audit["rows"]):
        reasons_red.append("a position trades at or below its stop level")
    if any(r.get("critical_uncovered") for r in audit["rows"]):
        reasons_yellow.append("shares without a GTC stop")
    if any(s.get("status") == "FAIL" for s in sectors.values()):
        reasons_yellow.append("sector filter failure")
    if chip_warning:
        reasons_yellow.append("chip warning from Korea")
    if any_breakdown:
        reasons_yellow.append("position breakdown")
    if data_gaps:
        reasons_yellow.append("data gaps: " + ", ".join(data_gaps))
    if reasons_red:
        return "RED", reasons_red + reasons_yellow
    if reasons_yellow:
        return "YELLOW", reasons_yellow
    return "GREEN", []
