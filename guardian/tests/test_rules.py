"""Offline tests for the deterministic rules. Run: python -m pytest tests -q"""

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from guardian_core import analysis, market, options, report, schedule  # noqa: E402
from guardian_core.ibkr_flex import parse_statement  # noqa: E402
import xml.etree.ElementTree as ET  # noqa: E402

CFG = yaml.safe_load((ROOT / "config.yaml").read_text())


def frame(closes, spread=0.01):
    idx = pd.bdate_range(end="2026-11-04", periods=len(closes))
    c = pd.Series(closes, index=idx, dtype=float)
    return pd.DataFrame({"Close": c, "Low": c * (1 - spread), "High": c * (1 + spread)})


# ---------------------------------------------------------------- stops

def test_avoid_round_moves_down_off_round_numbers():
    assert analysis.avoid_round(945.00) == 944.87
    assert analysis.avoid_round(187.50) == 187.43
    assert analysis.avoid_round(19.40) == 19.40
    assert analysis.avoid_round(10.00) == 9.97


def test_stop_respects_vol_floor_and_picks_level():
    lv = {"daily_vol_pct": 2.0, "low_13w": 98.0, "low_26w": 95.5, "low_52w": 80.0}
    # 13w low at 98 is within 1.5x vol (97 floor) of 100, so skip to 26w.
    s = analysis.compute_stop(100.0, lv, 1.5, 0.3)
    assert s["stop"] < 97 and "26w" in s["basis"]
    lv["low_13w"] = 96.0
    s = analysis.compute_stop(100.0, lv, 1.5, 0.3)
    assert "13w" in s["basis"] and s["stop"] < 96


def test_stop_below_all_levels_uses_vol():
    lv = {"daily_vol_pct": 3.0, "low_13w": 120.0, "low_26w": 110.0, "low_52w": 105.0}
    s = analysis.compute_stop(100.0, lv, 1.5, 0.3)
    assert s["stop"] <= 95.5 and "vol" in s["basis"]


def _audit(last, stop_book, ratchet, nav=100_000, shares=50):
    pos = [{"symbol": "NVDA", "shares": shares, "avg_cost": 150.0, "last": last}]
    lv = {"NVDA": {"daily_vol_pct": 2.0, "low_13w": last * 0.95, "low_26w": last * 0.8, "low_52w": last * 0.7}}
    return analysis.stop_audit(pos, lv, stop_book, ratchet, CFG, nav)


def test_never_lower_than_prior_or_live():
    a = _audit(200.0, [], {"NVDA": 190.0})
    row = a["rows"][0]
    assert row["computed_stop"] < 190 and row["suggested_stop"] == 190.0
    assert "never lower" in row["suggested_source"]
    a = _audit(200.0, [{"symbol": "NVDA", "shares": 50, "stop": 192.0}], {"NVDA": 150.0})
    assert a["rows"][0]["suggested_stop"] == 192.0
    nxt = analysis.next_ratchet(a, {"NVDA": 195.0})
    assert nxt["NVDA"] == 195.0


def test_uncovered_is_critical_and_counted():
    a = _audit(200.0, [{"symbol": "NVDA", "shares": 20, "stop": 185.0}], {})
    row = a["rows"][0]
    assert row["uncovered_shares"] == 30 and row["critical_uncovered"]
    assert row["action"].startswith("PLACE GTC stop 30")
    expected = (200 - 185) * 20 + (200 - row["suggested_stop"]) * 30
    assert a["total_open_risk_usd"] == pytest.approx(expected, abs=0.01)


def test_breach_and_red_when_over_limit():
    a = _audit(180.0, [], {"NVDA": 185.0})
    assert a["rows"][0]["breached"]
    a = _audit(200.0, [], {}, nav=5_000)
    assert a["risk_over_limit"]
    status, why = analysis.status_floor(a, {}, False, [], False)
    assert status == "RED" and "exceeds" in why[0]


def test_lottery_tickets_skipped():
    pos = [{"symbol": "INO", "shares": 100, "last": 1.2}, {"symbol": "XYZ", "shares": 10, "last": 20.0}]
    a = analysis.stop_audit(pos, {}, [], {}, CFG, 50_000)
    syms = {x["symbol"] for x in a["lottery_tickets"]}
    assert {"INO", "XYZ", "VCX", "ZOMDF"} <= syms and not a["rows"]


# ---------------------------------------------------------------- levels / sectors

def test_levels_and_sector_fail():
    closes = list(np.linspace(100, 120, 260)) + [95.0]
    lv = market.levels(frame(closes), date(2026, 11, 4))
    assert lv["below_13w_low"] and lv["chg_1m_pct"] < -10
    sec = analysis.sector_filter({"SMH": lv}, CFG)
    assert sec["SMH"]["status"] == "FAIL" and len(sec["SMH"]["reasons"]) == 2
    assert "BREAKDOWN" in analysis.position_flags(lv, 3, 10)
    assert any("EARNINGS" in f for f in analysis.position_flags(lv, 3, 10))


def test_levels_stale_flag():
    lv = market.levels(frame([10.0] * 50), date(2026, 11, 5))
    assert lv["stale"]


# ---------------------------------------------------------------- schedule

def test_premarket_window_follows_ny_dst():
    ok, _, target = schedule.decide_run(datetime(2026, 11, 5, 8, 35, tzinfo=schedule.NY), "premarket", CFG)
    assert ok and target.strftime("%H:%M") == "08:30"
    ok, _, _ = schedule.decide_run(datetime(2026, 11, 5, 7, 25, tzinfo=schedule.NY), "premarket", CFG)
    assert not ok
    ok, why, _ = schedule.decide_run(datetime(2026, 11, 26, 8, 30, tzinfo=schedule.NY), "premarket", CFG)
    assert not ok and "not an NYSE trading day" in why


def test_early_close_session():
    info = schedule.session_info(date(2026, 11, 27))
    assert info.early_close and info.close_ny.strftime("%H:%M") == "13:00"
    ok, _, target = schedule.decide_run(datetime(2026, 11, 27, 13, 20, tzinfo=schedule.NY), "close", CFG)
    assert ok and target.strftime("%H:%M") == "13:15"


def test_trading_days_between_skips_holiday():
    assert schedule.trading_days_between(date(2026, 11, 25), date(2026, 11, 30)) == 2


# ---------------------------------------------------------------- options

def chain():
    return [
        {"right": "P", "strike": 475.0, "bid": 9.8, "ask": 10.2, "iv": 0.42},
        {"right": "P", "strike": 420.0, "bid": 2.9, "ask": 3.1, "iv": 0.48},
        {"right": "P", "strike": 20.0, "bid": 1.4, "ask": 1.6, "iv": 0.9},
        {"right": "C", "strike": 25.0, "bid": 1.9, "ask": 2.1, "iv": 0.85},
    ]


def test_put_spread_math():
    spec = CFG["hedges"]["candidates"][1]
    r = options.price_structure(spec, chain(), 500.0, 0, date(2026, 11, 5), 25)
    assert r["net_per_share"] == 7.0 and r["net_cost_usd"] == 700.0
    assert r["max_payout_usd"] == (55 - 7) * 100 and r["breakeven"] == 468.0
    assert r["dte"] == 43 and not r["stale"]


def test_collar_math_and_stale_flag():
    spec = CFG["hedges"]["candidates"][0]
    r = options.price_structure(spec, chain(), 22.0, 200, date(2026, 11, 5), 25)
    assert r["net_per_share"] == -0.5  # credit
    assert r["max_loss_on_covered_shares_usd"] == (22 - 20 - 0.5) * 200
    bad = [dict(o, bid=0) for o in chain()]
    assert options.price_structure(spec, bad, 22.0, 200, date(2026, 11, 5), 25)["stale"]


def test_choose_monthly_expiry():
    exps = [date(2026, 12, 11), date(2026, 12, 18), date(2027, 1, 15)]
    assert options.choose_expiry(exps, date(2026, 11, 5), 30, 75) == date(2026, 12, 18)


# ---------------------------------------------------------------- report enforcement

def test_enforce_raises_status_and_strips_em_dash():
    text = "GREEN: all calm — nothing to do\n| a | b |\n|---|---|\n| 1 | 2 |"
    out, notes = report.enforce(text, "RED", ["open risk 9% exceeds 8% limit"],
                                CFG["report"]["closing_lines"], 60)
    assert "—" not in out
    assert out.splitlines()[0].startswith("**RED**")
    assert out.rstrip().endswith("Not financial advice. Oleg makes every decision.")


def test_enforce_keeps_more_severe_status():
    out, _ = report.enforce("RED: defense", "YELLOW", ["x"], [], 60)
    assert out.startswith("RED")


def test_telegram_tables_become_pre():
    md = "# Title\n**RED**: x < y\n| Ticker | Stop |\n|---|---|\n| NVDA | 187.43 |\nend"
    chunks = report.markdown_to_telegram(md)
    joined = "".join(chunks)
    assert "<pre>Ticker Stop" in joined and "x &lt; y" in joined and "<b>Title</b>" in joined
    assert "---" not in joined


# ---------------------------------------------------------------- flex

FLEX = """<FlexQueryResponse queryName="g" type="AF"><FlexStatements count="1">
<FlexStatement accountId="U1234567" whenGenerated="20261105;080102">
<EquitySummaryInBase><EquitySummaryByReportDateInBase reportDate="20261104" total="152340.55"/></EquitySummaryInBase>
<OpenPositions>
<OpenPosition assetCategory="STK" symbol="NVDA" position="50" markPrice="201.1" positionValue="10055"
 costBasisPrice="150.2" fifoPnlUnrealized="2545" currency="USD" levelOfDetail="SUMMARY"/>
<OpenPosition assetCategory="STK" symbol="NVDA" position="50" levelOfDetail="LOT"/>
<OpenPosition assetCategory="OPT" symbol="SMH   261218P00475000" underlyingSymbol="SMH" putCall="P"
 strike="475" expiry="20261218" multiplier="100" position="1" markPrice="10.1" positionValue="1010"
 levelOfDetail="SUMMARY"/>
</OpenPositions></FlexStatement></FlexStatements></FlexQueryResponse>"""


def test_parse_flex():
    d = parse_statement(ET.fromstring(FLEX))
    assert d["net_liquidation"] == 152340.55
    assert len(d["stocks"]) == 1 and d["stocks"][0]["avg_cost"] == 150.2
    assert d["options"][0]["expiry"] == "2026-12-18" and d["options"][0]["right"] == "P"
    assert d["orders"] is None


def test_far_level_falls_back_to_vol_floor():
    # SNDK on Nov 5 2026: 26w low too close, next level (52w) 80% away.
    lv = {"daily_vol_pct": 4.81, "low_13w": 1014.0, "low_26w": 998.19, "low_52w": 183.0}
    s = analysis.compute_stop(1000.50, lv, 1.5, 0.3)
    assert 925 < s["stop"] < 929 and "vol" in s["basis"]
