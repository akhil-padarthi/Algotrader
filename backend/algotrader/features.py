"""Feature engineering with Pandas.

Rule #1: a feature on date *t* may only use information available at the
close of *t*.  Every column below is a backward-looking rolling/EWM
statistic; the only forward-looking column is the training label, which is
built separately in :func:`add_labels` and never used as an input.

Execution model shared by the backtest and the live bot:
    signal computed after the close of day t  ->  order fills at the open of t+1
so the label measures the return from open(t+1) to open(t+1+horizon).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .context import MACRO_FEATURES, align_macro, macro_features

WARMUP_SESSIONS = 100  # longest lookback used below

STOCK_FEATURES = [
    "ret_1", "ret_5", "ret_10", "ret_20",
    "gap", "range_pct",
    "close_sma10", "close_sma20", "close_sma50", "close_sma100", "sma10_sma50",
    "rsi_14", "stoch_14",
    "macd", "macd_signal", "macd_hist",
    "bb_pctb", "bb_width",
    "atr_14",
    "vol_20", "vol_ratio",
    "volume_z", "volume_ratio",
    "dist_high_100", "dist_low_100",
]
MARKET_FEATURES = [
    "mkt_ret_5", "mkt_ret_20", "mkt_close_sma50", "mkt_vol_20",
    "rel_strength_20", "corr_mkt_60",
]
# Current-conditions features (see context.py): the stock's sector ETF, and FRED macro data.
SECTOR_FEATURES = ["sector_ret_5", "sector_ret_20", "sector_close_sma50", "rel_sector_20"]


def _wilder(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(alpha=1.0 / n, adjust=False, min_periods=n).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _wilder(delta.clip(lower=0), n)
    loss = _wilder(-delta.clip(upper=0), n)
    rs = gain / loss.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    out = out.where(loss != 0, 100.0)  # no losses in window -> RSI 100
    return out.where(gain.notna())


def stock_features(g: pd.DataFrame) -> pd.DataFrame:
    """Indicators for ONE symbol. ``g`` is sorted by date."""
    o, h, l, c, v = g["open"], g["high"], g["low"], g["close"], g["volume"].astype(float)
    out = pd.DataFrame(index=g.index)
    ret1 = c.pct_change()
    out["ret_1"] = ret1
    for n in (5, 10, 20):
        out[f"ret_{n}"] = c.pct_change(n)
    out["gap"] = o / c.shift(1) - 1
    out["range_pct"] = (h - l) / c

    sma = {n: c.rolling(n, min_periods=n).mean() for n in (10, 20, 50, 100)}
    for n, s in sma.items():
        out[f"close_sma{n}"] = c / s - 1
    out["sma10_sma50"] = sma[10] / sma[50] - 1

    out["rsi_14"] = rsi(c, 14) / 100.0
    low14 = l.rolling(14, min_periods=14).min()
    high14 = h.rolling(14, min_periods=14).max()
    out["stoch_14"] = (c - low14) / (high14 - low14).replace(0, np.nan)

    ema12 = c.ewm(span=12, adjust=False, min_periods=12).mean()
    ema26 = c.ewm(span=26, adjust=False, min_periods=26).mean()
    macd = (ema12 - ema26) / c
    signal = macd.ewm(span=9, adjust=False, min_periods=9).mean()
    out["macd"], out["macd_signal"], out["macd_hist"] = macd, signal, macd - signal

    sd20 = c.rolling(20, min_periods=20).std()
    upper, lower = sma[20] + 2 * sd20, sma[20] - 2 * sd20
    out["bb_pctb"] = (c - lower) / (upper - lower).replace(0, np.nan)
    out["bb_width"] = (upper - lower) / sma[20]

    prev_c = c.shift(1)
    tr = pd.concat([h - l, (h - prev_c).abs(), (l - prev_c).abs()], axis=1).max(axis=1)
    out["atr_14"] = _wilder(tr, 14) / c

    vol20 = ret1.rolling(20, min_periods=20).std()
    out["vol_20"] = vol20
    out["vol_ratio"] = ret1.rolling(5, min_periods=5).std() / vol20.replace(0, np.nan)

    logv = np.log1p(v)
    out["volume_z"] = (logv - logv.rolling(20, min_periods=20).mean()) / logv.rolling(20, min_periods=20).std().replace(0, np.nan)
    out["volume_ratio"] = v / v.rolling(20, min_periods=20).mean().replace(0, np.nan)

    out["dist_high_100"] = c / h.rolling(100, min_periods=100).max() - 1
    out["dist_low_100"] = c / l.rolling(100, min_periods=100).min() - 1
    return out


def market_frame(bench: pd.DataFrame) -> pd.DataFrame:
    """Benchmark (SPY) context, one row per date."""
    b = bench.sort_values("date").set_index("date")
    c = b["close"]
    ret1 = c.pct_change()
    sma50 = c.rolling(50, min_periods=50).mean()
    return pd.DataFrame({
        "mkt_ret_1": ret1,
        "mkt_ret_5": c.pct_change(5),
        "mkt_ret_20": c.pct_change(20),
        "mkt_close_sma50": c / sma50 - 1,
        "mkt_vol_20": ret1.rolling(20, min_periods=20).std(),
        "market_ok": (c > sma50) | sma50.isna(),
    })


def build_features(
    bars: pd.DataFrame,
    benchmark: str | None = "SPY",
    use_market: bool = True,
    symbols: list[str] | None = None,
    sectors: dict[str, str] | None = None,
    macro: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Compute features for the tradable symbols.

    ``symbols``: which symbols get rows (default: everything except the benchmark).
    ``sectors``: symbol -> sector ETF (whose bars must be in ``bars``) for the sector features.
    ``macro``:   FRED data (context.load_macro) for the macro features, lagged one day.

    Returns a long frame with: date, symbol, OHLCV, every feature column,
    and ``market_ok`` (bool, used by the strategy's market filter).
    Warm-up rows are kept (with NaNs) so the caller decides what to drop.
    """
    if bars.empty:
        raise ValueError("No bars to build features from")
    bars = bars.sort_values(["symbol", "date"]).reset_index(drop=True)
    has_bench = bool(benchmark) and benchmark in set(bars["symbol"])
    if symbols is not None:
        stocks = bars[bars["symbol"].isin(symbols)]
    else:
        stocks = bars[bars["symbol"] != benchmark] if has_bench else bars
    if stocks.empty:
        raise ValueError("None of the symbols have price data")

    parts = []
    for _, g in stocks.groupby("symbol", sort=True):
        g = g.reset_index(drop=True)
        parts.append(pd.concat([g, stock_features(g)], axis=1))
    frame = pd.concat(parts, ignore_index=True)

    if has_bench and use_market:
        mkt = market_frame(bars[bars["symbol"] == benchmark])
        frame = frame.merge(mkt, left_on="date", right_index=True, how="left")
        frame["rel_strength_20"] = frame["ret_20"] - frame["mkt_ret_20"]
        corr = []
        for _, g in frame.groupby("symbol", sort=False):
            corr.append(g["ret_1"].rolling(60, min_periods=60).corr(g["mkt_ret_1"]))
        frame["corr_mkt_60"] = pd.concat(corr)
        frame = frame.drop(columns=["mkt_ret_1"])
        frame["market_ok"] = frame["market_ok"].fillna(True).astype(bool)
    else:
        frame["market_ok"] = True

    if sectors:
        frame = add_sector_features(frame, bars, sectors)
    if macro is not None and not macro.empty:
        aligned = align_macro(macro_features(macro), frame["date"])
        frame = frame.merge(aligned, left_on="date", right_index=True, how="left")

    frame = frame.replace([np.inf, -np.inf], np.nan)
    return frame.sort_values(["date", "symbol"]).reset_index(drop=True)


def sector_frame(etf_bars: pd.DataFrame) -> pd.DataFrame:
    c = etf_bars.sort_values("date").set_index("date")["close"]
    return pd.DataFrame({
        "sector_ret_5": c.pct_change(5),
        "sector_ret_20": c.pct_change(20),
        "sector_close_sma50": c / c.rolling(50, min_periods=50).mean() - 1,
    })


def add_sector_features(frame: pd.DataFrame, bars: pd.DataFrame, sectors: dict[str, str]) -> pd.DataFrame:
    """Each stock gets its sector ETF's momentum, and how it's doing relative to that sector."""
    available = set(bars["symbol"])
    cache = {etf: sector_frame(bars[bars["symbol"] == etf]) for etf in set(sectors.values()) if etf in available}
    parts = []
    for sym, g in frame.groupby("symbol", sort=False):
        etf = sectors.get(sym)
        sf = cache.get(etf)
        if sf is None:
            parts.append(g.assign(**{c: np.nan for c in SECTOR_FEATURES[:3]}))
        else:
            parts.append(g.merge(sf, left_on="date", right_index=True, how="left"))
    out = pd.concat(parts, ignore_index=True)
    out["rel_sector_20"] = out["ret_20"] - out["sector_ret_20"]
    return out


def feature_columns(frame: pd.DataFrame, context: bool = False) -> list[str]:
    """Base set: the stock's own price history plus the S&P 500. With ``context``: also the
    sector ETF and FRED macro features, when the frame has them."""
    cols = list(STOCK_FEATURES)
    if all(c in frame.columns for c in MARKET_FEATURES):
        cols += MARKET_FEATURES
    if context:
        for group in (SECTOR_FEATURES, MACRO_FEATURES):
            if all(c in frame.columns for c in group):
                cols += group
    return cols


def has_context(frame: pd.DataFrame) -> bool:
    return len(feature_columns(frame, context=True)) > len(feature_columns(frame))


def add_labels(frame: pd.DataFrame, horizon: int, threshold: float) -> pd.DataFrame:
    """Forward return from the next open to the open ``horizon`` sessions later."""
    out = frame.sort_values(["symbol", "date"]).copy()
    grp = out.groupby("symbol", sort=False)["open"]
    entry = grp.shift(-1)
    exit_ = grp.shift(-(horizon + 1))
    out["fwd_return"] = exit_ / entry - 1
    out["label"] = np.where(out["fwd_return"].isna(), np.nan, (out["fwd_return"] > threshold).astype(float))
    return out.sort_values(["date", "symbol"]).reset_index(drop=True)


def latest_rows(frame: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    """Most recent complete feature row per symbol (for live trading)."""
    ok = frame.dropna(subset=cols)
    if ok.empty:
        return ok
    idx = ok.groupby("symbol")["date"].idxmax()
    return ok.loc[idx].sort_values("symbol").reset_index(drop=True)
