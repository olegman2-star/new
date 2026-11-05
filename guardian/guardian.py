#!/usr/bin/env python3
"""Guardian: pre-market portfolio protection report for Oleg's IBKR account.

Advice only. This program never places, modifies or cancels orders: it has
read-only IBKR access (Flex Web Service) and no order endpoint anywhere.

    python guardian.py --dry-run            # print locally, send nothing
    python guardian.py                      # scheduled run (time + holiday checks)
    python guardian.py --session close      # end-of-day stop review
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path

import yaml

from guardian_core import analysis, market, options, report, schedule, sentiment
from guardian_core.ibkr_flex import FlexError, load_positions

ROOT = Path(__file__).resolve().parent
log = logging.getLogger("guardian")


def load_config(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# ---------------------------------------------------------------- data collection

def get_positions(cfg: dict, use_baseline: bool) -> tuple[dict, list[str]]:
    gaps = []
    tok, qid = os.environ.get("IBKR_FLEX_TOKEN"), os.environ.get("IBKR_FLEX_QUERY_ID")
    if not use_baseline and tok and qid:
        try:
            pos = load_positions(tok, qid)
            pos["source"] = f"IBKR Flex (live, generated {pos.get('as_of')})"
            return pos, gaps
        except (FlexError, OSError) as e:
            log.error("IBKR Flex failed: %s", e)
            gaps.append("IBKR positions unavailable")
    elif not use_baseline:
        gaps.append("IBKR Flex credentials not set")
    stocks = [{"symbol": s, "shares": float(n), "avg_cost": None}
              for s, n in cfg["baseline_holdings"].items()]
    return {"source": "BASELINE from config.yaml, NOT LIVE: share counts may be out of date",
            "stocks": stocks, "options": [], "net_liquidation": None, "orders": None}, gaps


def get_stop_book(pos: dict, cfg: dict) -> tuple[list[dict], str]:
    if pos.get("orders"):
        def is_stop(o):
            kind = (o.get("type") or "").upper()
            return (o.get("side") or "").upper().startswith("S") and ("STP" in kind or "TRAIL" in kind)
        stops = [o for o in pos["orders"] if is_stop(o)]
        gtc = [o for o in stops if (o.get("tif") or "GTC").upper() == "GTC"]
        return gtc, "IBKR open orders (live)"
    ms = cfg.get("manual_stops") or {}
    book = [{"symbol": o["symbol"], "shares": float(o["shares"]), "stop": float(o["stop"]),
             "tif": o.get("tif", "GTC")} for o in ms.get("orders", [])]
    return book, f"MANUAL list in config.yaml (updated {ms.get('updated')}), NOT LIVE: confirm in IBKR"


def collect(cfg: dict, session: str, today: date, args) -> dict:
    info = schedule.session_info(today, cfg["exchange_calendar"])
    # Pre-market, the latest complete U.S. bar is the previous session's.
    expected_us = info.previous_session if session == "premarket" else today

    pos, gaps = get_positions(cfg, args.baseline)
    if args.account_value:
        pos["net_liquidation"] = args.account_value
    stop_book, stops_source = get_stop_book(pos, cfg)

    holdings = [p for p in pos["stocks"] if p["shares"] > 0]
    syms = [p["symbol"] for p in holdings]
    sector_of = {s: cfg["holding_sector"].get(s, cfg["default_sector_etf"]) for s in syms}
    unmapped = [s for s in syms if s not in cfg["holding_sector"] and s not in cfg["lottery_tickets"]]
    etfs = sorted(set(cfg["sector_etfs"]) | {e for s, e in sector_of.items() if s not in cfg["lottery_tickets"]})
    asia = cfg["asia"]["tickers"]
    hedge_unds = {h["underlying"] for h in cfg["hedges"]["candidates"]}

    hist = market.download(syms + etfs + cfg["us_indices"] + [cfg["vix_ticker"]]
                           + list(asia.values()) + list(hedge_unds))
    lv = {t: market.levels(hist.get(t), expected_us, cfg["risk"]["vol_window_days"]) for t in hist}
    for t in set(syms + etfs):
        lv.setdefault(t, {"available": False})
    if any(not lv[s].get("available") for s in syms):
        gaps.append("prices missing for " + ", ".join(s for s in syms if not lv[s].get("available")))

    pre = market.premarket_prices(syms + cfg["us_indices"]) if session == "premarket" else {}

    # Step 1: Asia.
    asia_out = {}
    for name, t in asia.items():
        d = lv.get(t, {"available": False})
        asia_out[name] = {k: d.get(k) for k in ("last", "last_date", "chg_1d_pct", "chg_1w_pct")} | {
            "ticker": t, "available": d.get("available", False),
            "traded_today": d.get("last_date") == today.isoformat()}
    kospi, hynix = asia_out.get("KOSPI", {}), asia_out.get("SK Hynix", {})
    chip_warning = bool(
        (kospi.get("traded_today") and (kospi.get("chg_1d_pct") or 0) < cfg["asia"]["kospi_warning_pct"])
        or (hynix.get("traded_today") and (hynix.get("chg_1d_pct") or 0) < cfg["asia"]["hynix_warning_pct"]))
    kospi_big = abs(kospi.get("chg_1d_pct") or 0) >= 5

    # Step 2: U.S. context.
    us = {}
    for t in cfg["us_indices"] + [cfg["vix_ticker"]]:
        d = lv.get(t, {})
        us[t] = {k: d.get(k) for k in ("last", "last_date", "chg_1d_pct", "pct_from_52w_high", "stale")}
        if t in pre:
            us[t]["premarket"] = pre[t]

    # Step 3: sectors.
    sectors = analysis.sector_filter({e: lv[e] for e in etfs}, cfg)

    # Step 4: positions.
    warn = cfg["risk"]["earnings_warning_trading_days"]
    rows, any_breakdown, big_movers = [], False, []
    for p in holdings:
        s, d = p["symbol"], lv[p["symbol"]]
        last = d.get("last")
        ed = market.earnings_date(s, today) if s not in cfg["lottery_tickets"] else None
        etd = schedule.trading_days_between(today, date.fromisoformat(ed)) if ed else None
        flags = analysis.position_flags(d, etd, warn) if d.get("available") else ["NO PRICE DATA"]
        any_breakdown |= "BREAKDOWN" in flags
        if abs(d.get("chg_1d_pct") or 0) > cfg["risk"]["big_move_pct"]:
            big_movers.append(s)
        avg = p.get("avg_cost")
        rows.append({
            "symbol": s, "shares": p["shares"], "avg_cost": avg, "last": last,
            "premarket": pre.get(s),
            "value": round(last * p["shares"], 2) if last else None,
            "unrealized_pnl": p.get("unrealized_pnl") if p.get("unrealized_pnl") is not None
            else (round((last - avg) * p["shares"], 2) if (last and avg) else None),
            **{k: d.get(k) for k in ("chg_1d_pct", "chg_1w_pct", "chg_1m_pct", "low_13w", "low_26w",
                                     "low_52w", "pct_from_13w_low", "pct_from_26w_low",
                                     "daily_vol_pct", "last_date")},
            "sector_etf": sector_of[s],
            "sector_failed": sectors.get(sector_of[s], {}).get("status") == "FAIL",
            "next_earnings": ed, "earnings_in_trading_days": etd,
            "flags": flags,
        })

    # Step 5: stops.
    state_path = ROOT / "state" / "stops.json"
    ratchet = {**cfg.get("proposed_stops", {}), **load_json(state_path, {})}
    audit = analysis.stop_audit(rows, lv, stop_book, ratchet, cfg, pos.get("net_liquidation"))
    if pos.get("net_liquidation") is None:
        gaps.append("account value unknown: open risk % cannot be computed")

    # Step 6: hedges.
    hedges = collect_hedges(cfg, pos, rows, lv, today)

    # Step 7: sentiment and events.
    st = sentiment.collect([s for s in syms if s not in cfg["lottery_tickets"]])
    horizon = schedule.next_sessions(today, 6, cfg["exchange_calendar"])
    events = [e for e in cfg["macro"]["events"]
              if horizon[0] <= date.fromisoformat(str(e["date"])) <= horizon[-1]]
    earnings_soon = [{"symbol": r["symbol"], "date": r["next_earnings"]} for r in rows
                     if r["earnings_in_trading_days"] is not None and r["earnings_in_trading_days"] <= 5]

    floor, floor_reasons = analysis.status_floor(audit, sectors, chip_warning, gaps, any_breakdown)
    return {
        "_ratchet_next": analysis.next_ratchet(audit, ratchet),
        "run": {"session": session, "generated_ny": datetime.now(schedule.NY).strftime("%Y-%m-%d %H:%M %Z"),
                "market": info.as_dict(),
                "early_close_note": (f"EARLY CLOSE today at {info.close_ny:%H:%M} NY" if info.early_close else None),
                "us_prices_as_of": expected_us.isoformat()},
        "status_floor": floor, "status_floor_reasons": floor_reasons,
        "data_gaps": gaps,
        "positions_source": pos["source"],
        "stops_source": stops_source,
        "account": {"net_liquidation": pos.get("net_liquidation"), "cash": pos.get("cash"),
                    "account_id_last4": (pos.get("account_id") or "")[-4:] or None},
        "asia": asia_out, "chip_warning": chip_warning, "kospi_move_5pct_or_more": kospi_big,
        "us_market": us,
        "sectors": sectors, "unmapped_holdings": unmapped,
        "positions": rows,
        "stop_audit": audit,
        "hedges": hedges,
        "sentiment": st,
        "events_next_5_sessions": {"window": [horizon[0].isoformat(), horizon[-1].isoformat()],
                                   "earnings": earnings_soon, "macro_known": events},
        "big_movers": big_movers,
    }


def collect_hedges(cfg: dict, pos: dict, rows: list[dict], lv: dict, today: date) -> dict:
    hc = cfg["hedges"]
    spread_pct = hc["stale_spread_pct"]
    src = options.quote_source()
    out = {"quote_source": src.name, "candidates": [], "auto": [], "existing": []}
    held = {r["symbol"]: r["shares"] for r in rows}

    for spec in hc["candidates"]:
        try:
            exp = date.fromisoformat(str(spec["expiry"]))
            chain = src.chain(spec["underlying"], exp)
            out["candidates"].append(options.price_structure(
                spec, chain, lv.get(spec["underlying"], {}).get("last"),
                held.get(spec["underlying"], 0), today, spread_pct))
        except Exception as e:
            out["candidates"].append({"name": spec.get("name"), "priced": False, "why": str(e)[:200]})

    if hc["auto_sector_spread"]["enabled"]:
        clusters: dict[str, list[dict]] = {}
        for r in rows:
            if r["symbol"] in cfg["lottery_tickets"] or not r.get("value") or r["shares"] >= 100:
                continue
            if r["value"] < cfg["risk"]["lottery_threshold_usd"]:
                continue
            clusters.setdefault(r["sector_etf"], []).append(r)
        for etf, members in clusters.items():
            value = sum(m["value"] for m in members)
            spot = lv.get(etf, {}).get("last")
            if len(members) < 2 or value < hc["auto_sector_spread"]["min_cluster_usd"] or not spot:
                continue
            try:
                res = options.auto_sector_spread(etf, value, spot, src, today, hc["auto_sector_spread"], spread_pct)
            except Exception as e:
                res = {"name": f"{etf} auto put spread", "priced": False, "why": str(e)[:200]}
            res["cluster_members"] = [m["symbol"] for m in members]
            out["auto"].append(res)

    for o in pos.get("options", []):
        dte = (date.fromisoformat(o["expiry"]) - today).days if o.get("expiry") else None
        rec = "HOLD"
        if dte is not None and dte < hc["roll_if_dte_below"]:
            rec = "ROLL or CLOSE (under 14 days)"
        out["existing"].append({**o, "dte": dte, "recommendation": rec})
    return out


# ---------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the report, send nothing, save nothing")
    ap.add_argument("--session", choices=["auto", "premarket", "close"], default="auto")
    ap.add_argument("--force", action="store_true", help="ignore the time window and the already-sent check")
    ap.add_argument("--baseline", action="store_true", help="use config baseline holdings instead of IBKR")
    ap.add_argument("--no-llm", action="store_true", help="print the collected data instead of a report")
    ap.add_argument("--account-value", type=float, help="override account value (testing)")
    ap.add_argument("--config", type=Path, default=ROOT / "config.yaml")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    cfg = load_config(args.config)
    now = datetime.now(schedule.NY)
    session = schedule.auto_session(now, cfg) if args.session == "auto" else args.session
    today = now.date()

    if not args.force:
        ok, why, _ = schedule.decide_run(now, session, cfg)
        log.info(why)
        if not ok:
            print(f"Skipping: {why}")
            return 0
    report_dir = ROOT / cfg["report"]["dir"]
    out_path = report_dir / f"{today.isoformat()}-{session}.md"
    sent_path = ROOT / "state" / "sent.json"
    sent = load_json(sent_path, {})
    run_key = f"{today.isoformat()}-{session}"
    if run_key in sent and not args.force and not args.dry_run:
        print(f"Skipping: {run_key} report already sent at {sent[run_key]}")
        return 0

    if not args.dry_run:
        missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "ANTHROPIC_API_KEY") if not os.environ.get(k)]
        if missing:
            print(f"::error::missing secrets: {', '.join(missing)}")
            return 2

    data = collect(cfg, session, today, args)
    ratchet_next = data.pop("_ratchet_next")

    if args.no_llm:
        print(json.dumps(data, indent=1, default=str))
        return 0

    system_prompt = (ROOT / "prompts" / "system.md").read_text()
    text = report.generate(system_prompt, report.build_user_message(data, session), cfg["llm"])
    text, notes = report.enforce(text, data["status_floor"], data["status_floor_reasons"],
                                 cfg["report"]["closing_lines"], cfg["report"]["max_lines"])
    for n in notes:
        log.info("format: %s", n)

    if args.dry_run:
        print(text)
        return 0

    tg_token, tg_chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    report.send_telegram(tg_token, tg_chat, text)
    report.send_telegram_document(tg_token, tg_chat, out_path.name, text)
    report_dir.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text)
    (ROOT / "state").mkdir(exist_ok=True)
    (ROOT / "state" / "stops.json").write_text(json.dumps(ratchet_next, indent=2, sort_keys=True) + "\n")
    sent[run_key] = datetime.now(schedule.NY).isoformat(timespec="minutes")
    sent_path.write_text(json.dumps(dict(sorted(sent.items())[-60:]), indent=2) + "\n")
    # Status only: report content stays out of logs (the repo may be public).
    print(f"Sent {run_key}: status {text.split(chr(10), 1)[0][:40]}")
    if any(g.startswith("IBKR") for g in data["data_gaps"]) and not args.baseline:
        print("::error::report sent, but IBKR positions were unavailable (baseline used)")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
