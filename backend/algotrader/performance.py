"""Account value vs the S&P 500 for each chart period (1D, 1W, 1M, 3M, 6M, 1Y).

Each period has its own resolution, like a broker app: 5-minute points for a day, 15-minute
for a week, hourly for a month and daily beyond that. The S&P 500 line always covers the whole
period. If the account is younger than the period, its line starts where the account started
and is drawn from the S&P 500's level at that moment, so the gap between the lines is how far
ahead or behind the account is since it opened. The numbers in `stats` compare the two over
exactly the same days.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import pandas as pd

from .alpaca_client import AlpacaError
from .backtest import perf_stats

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PeriodSpec:
    history_tf: str          # Alpaca portfolio-history timeframe
    bars_tf: str             # Alpaca bars timeframe for the benchmark
    days: int                # how far back (calendar days)
    hours: tuple[int, int] | None = None   # regular-session window, minutes after midnight New York
    last_session: bool = False


# Alpaca only serves intraday account history for spans under 30 days, hence 28 for "1M".
PERIOD_SPECS = {
    "1D": PeriodSpec("5Min", "5Min", 6, (570, 960), last_session=True),
    "1W": PeriodSpec("15Min", "15Min", 7, (570, 960)),
    "1M": PeriodSpec("1H", "1Hour", 28, (540, 960)),
    "3M": PeriodSpec("1D", "1Day", 91),
    "6M": PeriodSpec("1D", "1Day", 182),
    "1A": PeriodSpec("1D", "1Day", 365),
}
PERIOD_ALIASES = {"1Y": "1A"}


def normalize_period(period: str) -> str | None:
    p = (period or "").upper()
    p = PERIOD_ALIASES.get(p, p)
    return p if p in PERIOD_SPECS else None


def history_series(h: dict, tz: ZoneInfo, daily: bool = False) -> pd.Series:
    """Alpaca portfolio history -> equity Series (New York timestamps, or dates if daily).

    Points before the account was funded come back as 0 or null and are dropped.
    Daily points are labelled with their trading date whether Alpaca stamps them at midnight
    New York or midnight UTC (which is 8 pm the evening before in New York).
    """
    pairs = [(ts, eq) for ts, eq in zip(h.get("timestamp") or [], h.get("equity") or []) if eq not in (None, 0)]
    if not pairs:
        return pd.Series(dtype=float)
    idx = pd.to_datetime([ts for ts, _ in pairs], unit="s", utc=True).tz_convert(tz)
    if daily:
        idx = (idx + pd.Timedelta(hours=6)).normalize()
    s = pd.Series([float(e) for _, e in pairs], index=idx)
    return s[~s.index.duplicated(keep="last")].sort_index()


def asof(values: pd.Series, index: pd.DatetimeIndex) -> pd.Series:
    """Latest value at or before each timestamp in `index`; NaN before the first value."""
    if values.empty:
        return pd.Series(float("nan"), index=index)
    values = values[~values.index.duplicated(keep="last")].sort_index()
    return values.reindex(values.index.union(index)).ffill().reindex(index)


def _regular_hours(stamps: pd.Series, hours) -> pd.Series:
    minutes = stamps.dt.hour * 60 + stamps.dt.minute
    return (minutes >= hours[0]) & (minutes < hours[1])


def benchmark_closes(client, benchmark: str, spec: PeriodSpec, now: pd.Timestamp, tz: ZoneInfo) -> pd.Series:
    start = (now - pd.Timedelta(days=spec.days)).date().isoformat()
    bars = client.get_bars([benchmark], start=start, timeframe=spec.bars_tf)
    if bars.empty:
        return pd.Series(dtype=float)
    stamps = bars["timestamp"].dt.tz_convert(tz)
    closes = pd.Series(bars["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(stamps))
    if spec.bars_tf == "1Day":
        closes.index = closes.index.normalize()
    if spec.hours:
        closes = closes[_regular_hours(stamps, spec.hours).to_numpy()]
    if spec.last_session and not closes.empty:
        last_day = closes.index.normalize().max()
        closes = closes[closes.index.normalize() == last_day]
    return closes[~closes.index.duplicated(keep="last")].sort_index()


def account_equity(client, spec: PeriodSpec, start: pd.Timestamp, now: pd.Timestamp, tz: ZoneInfo) -> tuple[pd.Series, str]:
    """Equity over the window, intraday if the period calls for it (daily as a fallback)."""
    end = now.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
    if spec.history_tf != "1D":
        try:
            h = client.get_portfolio_history(timeframe=spec.history_tf, start=start.isoformat(), end=end)
            eq = history_series(h, tz)
            if spec.hours and not eq.empty:
                eq = eq[_regular_hours(pd.Series(eq.index), spec.hours).to_numpy()]
            if not eq.empty:
                return eq, spec.history_tf
        except AlpacaError as exc:
            log.info("Intraday account history unavailable (%s) - using daily values", exc.message)
    h = client.get_portfolio_history(timeframe="1D", start=start.date().isoformat(), end=end)
    return history_series(h, tz, daily=True), "1D"


def build_performance(client, period: str, benchmark: str, now: pd.Timestamp, tz: ZoneInfo) -> dict:
    spec = PERIOD_SPECS[period]
    now = now.tz_convert(tz)
    bench = benchmark_closes(client, benchmark, spec, now, tz)
    window_start = bench.index[0] if not bench.empty else (now - pd.Timedelta(days=spec.days)).normalize()
    eq, account_tf = account_equity(client, spec, window_start, now, tz)

    # a daily account series can't sit on an intraday S&P timeline: show both daily instead
    if account_tf == "1D" and spec.bars_tf != "1Day" and not bench.empty:
        bench = bench.groupby(bench.index.normalize()).last()
    timeline = bench.index if not bench.empty else eq.index
    base = {"period": period, "benchmark": benchmark, "timeframe": account_tf, "intraday": account_tf != "1D"}
    if len(timeline) == 0:
        return {**base, "points": [], "stats": None, "account_start": None, "partial": False}

    eq_on = asof(eq, timeline) if not eq.empty else pd.Series(float("nan"), index=timeline)
    if not eq.empty:
        eq_on = eq_on.where(timeline >= eq.index[0])      # nothing before the account existed
    b_on = asof(bench, timeline) if not bench.empty else pd.Series(float("nan"), index=timeline)
    b_pct = b_on / b_on.dropna().iloc[0] - 1 if b_on.notna().any() else b_on

    started = eq_on.first_valid_index()
    # "partial": the account opened after this window began (an intraday series can miss the
    # first bar of the window just because the two sources stamp their bars differently)
    partial = started is not None and started.normalize() > timeline[0].normalize()
    if started is not None:
        e0 = float(eq_on.loc[started])
        lift = 1 + (float(b_pct.loc[started]) if pd.notna(b_pct.loc[started]) else 0.0)
        e_pct = lift * (eq_on / e0) - 1                     # starts on the S&P line when partial
    else:
        e_pct = eq_on

    points = []
    for ts in timeline:
        e, ep, bp = eq_on.loc[ts], e_pct.loc[ts], b_pct.loc[ts]
        points.append({
            "t": ts.isoformat(), "date": ts.strftime("%Y-%m-%d"),
            "equity": None if pd.isna(e) else round(float(e), 2),
            "equity_pct": None if pd.isna(ep) else round(float(ep), 5),
            "benchmark_pct": None if pd.isna(bp) else round(float(bp), 5),
        })

    stats = None
    if started is not None:
        acct = eq_on.loc[started:].dropna()
        bench_same = b_on.loc[started:].dropna()
        acct_ret = float(acct.iloc[-1] / acct.iloc[0] - 1)
        bench_ret = float(bench_same.iloc[-1] / bench_same.iloc[0] - 1) if len(bench_same) else None
        daily_eq = acct.groupby(acct.index.normalize()).last()
        stats = {
            "return": acct_ret,
            "benchmark_return": bench_ret,                      # same days as the account
            "benchmark_period_return": float(b_pct.dropna().iloc[-1]) if b_pct.notna().any() else None,
            "excess_return": (acct_ret - bench_ret) if bench_ret is not None else None,
            "max_drawdown": perf_stats(acct)["max_drawdown"],
            "sharpe": perf_stats(daily_eq)["sharpe"] if len(daily_eq) >= 10 else None,
            "start_equity": float(acct.iloc[0]), "end_equity": float(acct.iloc[-1]),
        }
    return {
        **base, "points": points, "stats": stats, "partial": bool(partial),
        "account_start": started.isoformat() if started is not None else None,
        "window_start": timeline[0].isoformat(),
    }
