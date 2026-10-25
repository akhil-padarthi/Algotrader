"""Historical data: download from Alpaca, clean with Pandas, cache to disk."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

PRICE_COLS = ["open", "high", "low", "close"]
BAR_COLS = ["date", "symbol", "open", "high", "low", "close", "volume"]
MARKET_TZ = "America/New_York"
# A one-day move this large on an adjusted series is almost always bad data.
MAX_ABS_DAILY_RETURN = 0.6


@dataclass
class CleaningReport:
    rows_in: int = 0
    rows_out: int = 0
    duplicates: int = 0
    missing_prices: int = 0
    non_positive: int = 0
    fixed_high_low: int = 0
    outliers_removed: int = 0
    per_symbol: dict = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{self.rows_in} rows in -> {self.rows_out} clean rows "
            f"(dupes {self.duplicates}, missing {self.missing_prices}, "
            f"non-positive {self.non_positive}, outliers {self.outliers_removed}, "
            f"high/low fixed {self.fixed_high_low})"
        )


def clean_bars(raw: pd.DataFrame, report: CleaningReport | None = None) -> pd.DataFrame:
    """Turn raw bars into a tidy daily frame.

    Input columns: symbol, timestamp *or* date, open, high, low, close, volume.
    Output columns: date (tz-naive midnight, New York trading date), symbol,
    open, high, low, close, volume — sorted by symbol then date, one row per
    (symbol, date).
    """
    report = report if report is not None else CleaningReport()
    report.rows_in = len(raw)
    if raw.empty:
        return pd.DataFrame(columns=BAR_COLS)

    df = raw.copy()
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()

    if "date" not in df.columns:
        ts = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
        # Alpaca daily bars are stamped 04:00/05:00 UTC (midnight New York);
        # converting to New York first keeps the right calendar day.
        df["date"] = ts.dt.tz_convert(MARKET_TZ).dt.tz_localize(None).dt.normalize()
    else:
        dates = pd.to_datetime(df["date"])
        if getattr(dates.dt, "tz", None) is not None:
            dates = dates.dt.tz_convert(MARKET_TZ).dt.tz_localize(None)
        df["date"] = dates.dt.normalize()

    for col in PRICE_COLS + ["volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["date"] = df["date"].astype("datetime64[ns]")

    before = len(df)
    df = df.sort_values(["symbol", "date"]).drop_duplicates(["symbol", "date"], keep="last")
    report.duplicates = before - len(df)

    missing = df[PRICE_COLS].isna().any(axis=1)
    report.missing_prices = int(missing.sum())
    df = df[~missing]

    non_pos = (df[PRICE_COLS] <= 0).any(axis=1)
    report.non_positive = int(non_pos.sum())
    df = df[~non_pos]

    df["volume"] = df["volume"].fillna(0).clip(lower=0)

    # High must be the highest price of the bar and low the lowest.
    true_high = df[["open", "high", "close"]].max(axis=1)
    true_low = df[["open", "low", "close"]].min(axis=1)
    bad = (df["high"] < true_high) | (df["low"] > true_low)
    report.fixed_high_low = int(bad.sum())
    df["high"] = true_high
    df["low"] = true_low

    # Drop isolated spikes: a huge move that fully reverses the next day.
    removed = 0
    keep_parts = []
    for symbol, g in df.groupby("symbol", sort=False):
        g = g.copy()
        for _ in range(3):  # a few passes in case spikes are adjacent
            ret = g["close"].pct_change()
            nxt = g["close"].shift(-1) / g["close"] - 1
            spike = (ret.abs() > MAX_ABS_DAILY_RETURN) & (nxt.abs() > MAX_ABS_DAILY_RETURN * 0.5) & (
                np.sign(ret) != np.sign(nxt)
            )
            if not spike.any():
                break
            removed += int(spike.sum())
            g = g[~spike]
        report.per_symbol[symbol] = len(g)
        keep_parts.append(g)
    report.outliers_removed = removed
    df = pd.concat(keep_parts) if keep_parts else df.iloc[0:0]

    df = df[BAR_COLS].sort_values(["symbol", "date"]).reset_index(drop=True)
    report.rows_out = len(df)
    return df


def cache_path(data_dir: Path) -> Path:
    return Path(data_dir) / "bars_1day.csv"


def save_bars(df: pd.DataFrame, data_dir: Path) -> Path:
    path = cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, date_format="%Y-%m-%d")
    return path


def load_cached_bars(data_dir: Path) -> pd.DataFrame | None:
    path = cache_path(data_dir)
    if not path.exists():
        return None
    df = pd.read_csv(path, parse_dates=["date"])
    df["date"] = df["date"].astype("datetime64[ns]")
    return df


def fetch_history(client, symbols: list[str], years: int, end: datetime | None = None) -> pd.DataFrame:
    """Download ``years`` of split/dividend-adjusted daily bars and clean them."""
    end = end or datetime.now(timezone.utc) - timedelta(minutes=16)  # free plan: SIP data >15 min old
    start = end - timedelta(days=int(365.25 * years) + 5)
    raw = client.get_bars(symbols, start=start.date().isoformat(), end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    report = CleaningReport()
    clean = clean_bars(raw, report)
    log.info("Cleaning: %s", report.summary())
    missing = sorted(set(symbols) - set(clean["symbol"].unique()))
    if missing:
        log.warning("No data returned for: %s", ", ".join(missing))
    return clean


def load_history(settings, client=None, refresh: bool = False) -> pd.DataFrame:
    """Return cleaned history from cache, downloading it if needed."""
    if not refresh:
        cached = load_cached_bars(settings.data_dir)
        if cached is not None and set(settings.data_symbols) <= set(cached["symbol"].unique()):
            log.info("Using cached bars (%d rows) from %s", len(cached), cache_path(settings.data_dir))
            return cached
    if client is None:
        raise RuntimeError("No cached data and no Alpaca client: set ALPACA_API_KEY/ALPACA_SECRET_KEY")
    df = fetch_history(client, settings.data_symbols, settings.history_years)
    if df.empty:
        raise RuntimeError("Alpaca returned no bars - check symbols, API keys and ALPACA_DATA_FEED")
    path = save_bars(df, settings.data_dir)
    log.info("Saved %d rows to %s", len(df), path)
    return df


def fetch_recent(client, symbols: list[str], sessions: int = 260, end: datetime | None = None) -> pd.DataFrame:
    """Recent bars for the live cycle (enough sessions to warm up every indicator).

    ``end`` should be the broker clock time so tests/demo can run on a
    simulated calendar.
    """
    end = (end or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(minutes=16)
    start = end - timedelta(days=int(sessions * 1.5) + 10)
    raw = client.get_bars(symbols, start=start.date().isoformat(), end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    return clean_bars(raw)
