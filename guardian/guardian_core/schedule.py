"""NYSE session logic. All times are derived from New York time, so the run
time follows U.S. DST rather than a fixed UTC or Israel clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd

NY = ZoneInfo("America/New_York")


@lru_cache(maxsize=2)
def calendar(name: str = "XNYS"):
    return xcals.get_calendar(name)


@dataclass
class SessionInfo:
    date: date
    is_session: bool
    open_ny: datetime | None
    close_ny: datetime | None
    early_close: bool
    previous_session: date

    def as_dict(self) -> dict:
        return {
            "date": self.date.isoformat(),
            "is_trading_day": self.is_session,
            "open_ny": self.open_ny.strftime("%H:%M") if self.open_ny else None,
            "close_ny": self.close_ny.strftime("%H:%M") if self.close_ny else None,
            "early_close": self.early_close,
            "previous_session": self.previous_session.isoformat(),
        }


def session_info(day: date, cal_name: str = "XNYS") -> SessionInfo:
    cal = calendar(cal_name)
    ts = pd.Timestamp(day)
    is_session = bool(cal.is_session(ts))
    open_ny = close_ny = None
    early = False
    if is_session:
        open_ny = cal.session_open(ts).tz_convert(NY).to_pydatetime()
        close_ny = cal.session_close(ts).tz_convert(NY).to_pydatetime()
        early = (close_ny.hour, close_ny.minute) < (16, 0)
        prev = cal.previous_session(ts).date()
    else:
        prev = cal.date_to_session(ts, direction="previous").date()
    return SessionInfo(day, is_session, open_ny, close_ny, early, prev)


def trading_days_between(start: date, end: date, cal_name: str = "XNYS") -> int:
    """Sessions strictly after `start` up to and including `end`."""
    if end <= start:
        return 0
    cal = calendar(cal_name)
    sessions = cal.sessions_in_range(pd.Timestamp(start + timedelta(days=1)), pd.Timestamp(end))
    return len(sessions)


def next_sessions(start: date, n: int, cal_name: str = "XNYS") -> list[date]:
    """The next n sessions, starting with `start` itself if it is a session."""
    cal = calendar(cal_name)
    sessions = cal.sessions_in_range(pd.Timestamp(start), pd.Timestamp(start + timedelta(days=n * 2 + 10)))
    return [s.date() for s in sessions[:n]]


def decide_run(now_ny: datetime, session: str, cfg: dict) -> tuple[bool, str, datetime | None]:
    """Return (should_run, reason, target_time) for the given session kind."""
    info = session_info(now_ny.date(), cfg.get("exchange_calendar", "XNYS"))
    if not info.is_session:
        return False, f"{info.date} is not an NYSE trading day (holiday or weekend)", None
    sch = cfg["schedule"]
    if session == "premarket":
        target = info.open_ny - timedelta(minutes=sch["premarket_minutes_before_open"])
        early, late = sch["premarket_early_tolerance_min"], sch["premarket_late_tolerance_min"]
    else:
        target = info.close_ny + timedelta(minutes=sch["close_minutes_after"])
        early, late = sch["close_early_tolerance_min"], sch["close_late_tolerance_min"]
    lo, hi = target - timedelta(minutes=early), target + timedelta(minutes=late)
    if not (lo <= now_ny <= hi):
        return False, (
            f"{session} target is {target:%H:%M} NY; window {lo:%H:%M}-{hi:%H:%M}, "
            f"now {now_ny:%H:%M} NY"
        ), target
    return True, f"{session} window open (target {target:%H:%M} NY)", target


def auto_session(now_ny: datetime, cfg: dict) -> str:
    """Pick premarket before noon NY, close after."""
    return "premarket" if now_ny.hour < 12 else "close"
