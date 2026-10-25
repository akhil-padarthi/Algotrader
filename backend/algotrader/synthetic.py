"""Synthetic OHLCV generator for tests and the offline demo.

``signal_strength=0`` produces a pure random walk: no model should be able
to beat a coin flip on it, which is how the tests detect look-ahead leaks.
``signal_strength>0`` plants weak short-term momentum that a correct
pipeline should be able to learn.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def make_synthetic_bars(
    symbols: list[str],
    sessions: int = 1260,
    end: str | pd.Timestamp | None = None,
    seed: int = 0,
    signal_strength: float = 0.0,
    benchmark: str | None = "SPY",
) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    end = pd.Timestamp(end or pd.Timestamp.today()).normalize()
    dates = pd.bdate_range(end=end, periods=sessions)
    n = len(dates)

    mkt = rng.normal(0.0004, 0.011, n)
    frames = []

    def ohlcv(symbol: str, rets: np.ndarray, start_price: float, sigma: float) -> pd.DataFrame:
        close = start_price * np.cumprod(1 + rets)
        prev = np.concatenate([[start_price], close[:-1]])
        open_ = prev * np.exp(rng.normal(0, 0.25 * sigma, n))
        hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.4 * sigma, n)))
        lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.4 * sigma, n)))
        vol = (rng.lognormal(15, 0.3, n) * (1 + 20 * np.abs(rets))).astype(np.int64)
        return pd.DataFrame({
            "date": dates, "symbol": symbol, "open": open_.round(4), "high": hi.round(4),
            "low": lo.round(4), "close": close.round(4), "volume": vol,
        })

    if benchmark:
        frames.append(ohlcv(benchmark, mkt, 400.0, 0.011))

    for symbol in symbols:
        beta = rng.uniform(0.7, 1.3)
        sigma = rng.uniform(0.012, 0.022)
        eps = rng.normal(0, sigma, n)
        idio = np.zeros(n)
        for t in range(n):
            momentum = idio[max(0, t - 5):t].mean() if t else 0.0
            idio[t] = signal_strength * momentum * 2.5 + eps[t]
        rets = np.clip(beta * mkt + idio + 0.0002, -0.4, 0.4)
        frames.append(ohlcv(symbol, rets, rng.uniform(30, 400), sigma))

    return pd.concat(frames, ignore_index=True)


def make_synthetic_macro(bars: pd.DataFrame, benchmark: str = "SPY", seed: int = 0) -> pd.DataFrame:
    """FRED-like series that move with the synthetic market: VIX follows realised volatility,
    rates and spreads wander. Indexed by business day, like FRED's daily series."""
    rng = np.random.default_rng(seed + 101)
    spy = bars[bars["symbol"] == benchmark].sort_values("date").set_index("date")["close"]
    dates = pd.bdate_range(spy.index.min() - pd.Timedelta(days=400), spy.index.max())
    ret = spy.pct_change().reindex(dates)
    filler = pd.Series(rng.normal(0, 0.011, len(dates)), index=dates)
    ret = ret.fillna(filler)
    vol = ret.rolling(21, min_periods=5).std().bfill() * np.sqrt(252) * 100
    vix = (vol * 1.15 + rng.normal(0, 1.2, len(dates))).clip(lower=9)
    dgs10 = 4.0 + np.cumsum(rng.normal(0, 0.04, len(dates)))
    t10y2y = 0.3 + np.cumsum(rng.normal(0, 0.02, len(dates)))
    baa10y = (1.8 + 0.04 * (vix - 16) + np.cumsum(rng.normal(0, 0.01, len(dates)))).clip(lower=0.8)
    out = pd.DataFrame({"VIXCLS": vix.round(2), "DGS10": np.round(dgs10, 2), "T10Y2Y": np.round(t10y2y, 2),
                        "BAA10Y": np.round(baa10y, 2)}, index=dates)
    # FRED has gaps on holidays; blank a few days like the real files do
    gaps = rng.choice(len(dates), size=max(1, len(dates) // 60), replace=False)
    out.iloc[gaps, 1:] = np.nan
    return out


_NEWS_GOOD = [
    "{name} beats estimates as demand stays strong",
    "Analyst upgrades {sym}, raises price target",
    "{name} shares climb after record revenue quarter",
    "{name} wins contract, expands partnership",
]
_NEWS_BAD = [
    "{name} misses estimates, shares slide",
    "{sym} downgraded to sell on slowdown concerns",
    "{name} faces probe; stock falls",
    "{name} cuts guidance as weakness spreads",
]
_NEWS_NEUTRAL = [
    "{name} to present at industry conference next week",
    "What to watch in {sym} this week",
    "{name} announces date for quarterly results call",
]


def make_synthetic_news(symbols: list[str], end: pd.Timestamp, days: int = 20, seed: int = 0,
                        names: dict[str, str] | None = None) -> list[dict]:
    """Deterministic Benzinga-style articles (newest first) for the stand-in news endpoint."""
    rng = np.random.default_rng(seed + 202)
    names = names or {}
    end = pd.Timestamp(end)
    end = end.tz_localize("UTC") if end.tzinfo is None else end.tz_convert("UTC")
    out, next_id = [], 40_000_000
    for sym in symbols:
        mood = rng.choice([-1, 0, 1])
        for _ in range(int(rng.integers(max(4, days // 4), max(8, days // 2)))):
            r = rng.random()
            pool = _NEWS_GOOD if r < 0.33 + 0.25 * mood else _NEWS_BAD if r < 0.75 else _NEWS_NEUTRAL
            headline = pool[int(rng.integers(len(pool)))].format(sym=sym, name=names.get(sym, sym))
            created = end - pd.Timedelta(minutes=int(rng.integers(30, days * 24 * 60)))
            next_id += 1
            out.append({
                "id": next_id, "headline": headline, "author": "Staff", "source": "benzinga",
                "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"), "updated_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "summary": "", "url": f"https://www.benzinga.com/news/{next_id}", "images": [], "symbols": [sym],
            })
    return sorted(out, key=lambda a: a["created_at"], reverse=True)
