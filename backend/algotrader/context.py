"""Current market conditions from trusted public sources.

* FRED, the Federal Reserve Bank of St. Louis's economic database (no key needed):
    VIXCLS   Cboe Volatility Index (VIX): how much the S&P 500 is expected to move
    DGS10    10-year US Treasury yield (Federal Reserve H.15)
    T10Y2Y   10-year minus 2-year Treasury yield ("the yield curve")
    BAA10Y   Moody's Baa corporate bond yield minus the 10-year Treasury (credit stress)
* The 11 Select Sector SPDR ETFs (XLK, XLF, ...), priced through Alpaca like every stock.
* News from Benzinga, delivered by Alpaca's news API (same API keys).

FRED values are used by the model with a one-day publication lag: a value dated d is only used
from d+1 onwards, both in training and live, because FRED posts most daily series the next
morning.  The news check is a live safety rule (the backtest can't replay old news).
"""
from __future__ import annotations

import io
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests

log = logging.getLogger(__name__)

FRED_URL = "https://fred.stlouisfed.org"
FRED_API_URL = "https://api.stlouisfed.org"
MACRO_CACHE = "macro.csv"

FRED_SERIES = {
    "VIXCLS": {"name": "Volatility index (VIX)", "source": "Cboe via FRED"},
    "DGS10": {"name": "10-year Treasury yield", "source": "Federal Reserve via FRED"},
    "T10Y2Y": {"name": "Yield curve (10-year minus 2-year)", "source": "Federal Reserve Bank of St. Louis (FRED)"},
    "BAA10Y": {"name": "Corporate credit spread", "source": "Moody's via FRED"},
}
MACRO_FEATURES = ["vix", "vix_chg_5", "vix_z_252", "yc_10y2y", "dgs10_chg_20", "credit_spread", "credit_chg_20"]

SECTOR_ETFS = {
    "XLK": "Technology", "XLC": "Communication services", "XLY": "Consumer discretionary",
    "XLP": "Consumer staples", "XLF": "Financials", "XLV": "Health care", "XLI": "Industrials",
    "XLE": "Energy", "XLB": "Materials", "XLU": "Utilities", "XLRE": "Real estate",
}
# GICS sector of common large caps -> its Select Sector SPDR. Anything else uses the benchmark.
DEFAULT_SECTORS = {
    **dict.fromkeys(["AAPL", "MSFT", "NVDA", "AVGO", "ORCL", "CRM", "ADBE", "AMD", "INTC", "CSCO", "IBM",
                     "QCOM", "TXN", "NOW", "INTU", "MU", "AMAT", "PLTR"], "XLK"),
    **dict.fromkeys(["GOOGL", "GOOG", "META", "NFLX", "DIS", "VZ", "T", "CMCSA", "TMUS"], "XLC"),
    **dict.fromkeys(["AMZN", "TSLA", "HD", "MCD", "NKE", "SBUX", "LOW", "BKNG", "TJX"], "XLY"),
    **dict.fromkeys(["WMT", "COST", "PG", "KO", "PEP", "PM", "MO", "CL"], "XLP"),
    **dict.fromkeys(["JPM", "BAC", "WFC", "GS", "MS", "C", "V", "MA", "BRK.B", "AXP", "PYPL", "SCHW", "BLK"], "XLF"),
    **dict.fromkeys(["UNH", "JNJ", "LLY", "PFE", "MRK", "ABBV", "TMO", "ABT", "AMGN", "CVS", "ISRG"], "XLV"),
    **dict.fromkeys(["BA", "CAT", "GE", "HON", "UPS", "RTX", "LMT", "DE", "UNP"], "XLI"),
    **dict.fromkeys(["XOM", "CVX", "COP", "SLB", "EOG", "OXY"], "XLE"),
    **dict.fromkeys(["LIN", "APD", "SHW", "FCX", "NEM"], "XLB"),
    **dict.fromkeys(["NEE", "DUK", "SO", "D"], "XLU"),
    **dict.fromkeys(["AMT", "PLD", "EQIX", "O", "SPG"], "XLRE"),
}


class SourceUnavailable(RuntimeError):
    pass


# ---------------------------------------------------------------------- FRED
class FredClient:
    """Reads FRED series. With an API key it uses the official API, otherwise FRED's public
    CSV download (the same file as the "Download" button on fred.stlouisfed.org)."""

    def __init__(self, base_url: str = FRED_URL, api_key: str | None = None, api_url: str = FRED_API_URL,
                 timeout: float = 20.0, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "AlgoTrader/1.3 (personal trading dashboard)")

    @classmethod
    def from_settings(cls, settings) -> "FredClient":
        return cls(settings.fred_url, settings.fred_api_key, settings.fred_api_url)

    def series(self, series_id: str, start: str) -> pd.Series:
        try:
            if self.api_key:
                r = self.session.get(f"{self.api_url}/fred/series/observations", timeout=self.timeout, params={
                    "series_id": series_id, "api_key": self.api_key, "file_type": "json", "observation_start": start})
                r.raise_for_status()
                obs = r.json().get("observations") or []
                dates = [o.get("date") for o in obs]
                values = [o.get("value") for o in obs]
            else:
                r = self.session.get(f"{self.base_url}/graph/fredgraph.csv", timeout=self.timeout,
                                     params={"id": series_id, "cosd": start})
                r.raise_for_status()
                df = pd.read_csv(io.StringIO(r.text), dtype=str)
                if df.shape[1] < 2:
                    raise SourceUnavailable(f"FRED returned an unexpected file for {series_id}")
                dates, values = df.iloc[:, 0].tolist(), df.iloc[:, 1].tolist()   # header: DATE or observation_date
        except (requests.RequestException, ValueError) as exc:
            raise SourceUnavailable(f"FRED {series_id}: {exc}") from exc
        s = pd.Series(pd.to_numeric(pd.Series(values), errors="coerce").to_numpy(),   # "." / "" = no value
                      index=pd.to_datetime(pd.Series(dates), errors="coerce"), name=series_id)
        s = s[s.index.notna()].dropna().sort_index()
        return s[~s.index.duplicated(keep="last")]

    def frame(self, series_ids, start: str) -> pd.DataFrame:
        cols = {sid: self.series(sid, start) for sid in series_ids}
        return pd.DataFrame(cols).sort_index()


def load_macro(settings, fred: FredClient | None = None, years: float | None = None,
               refresh: bool = False, max_age_hours: float = 6.0) -> pd.DataFrame:
    """All FRED series, cached in data/macro.csv. Falls back to the cache if FRED can't be reached."""
    path = Path(settings.data_dir) / MACRO_CACHE
    years = years if years is not None else settings.history_years + 1.5
    start = (pd.Timestamp.now().normalize() - pd.DateOffset(days=int(365.25 * years))).date()
    cached = None
    if path.exists():
        cached = pd.read_csv(path, index_col=0, parse_dates=True)
        fresh = time.time() - path.stat().st_mtime < max_age_hours * 3600
        if (not refresh and fresh and set(FRED_SERIES) <= set(cached.columns)
                and cached.index.min() <= pd.Timestamp(start) + pd.Timedelta(days=10)):
            return cached
    try:
        macro = (fred or FredClient.from_settings(settings)).frame(list(FRED_SERIES), start.isoformat())
    except SourceUnavailable as exc:
        if cached is not None and set(FRED_SERIES) <= set(cached.columns):
            log.warning("%s - using the saved copy from %s", exc,
                        datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
            return cached
        raise
    if macro.dropna(how="all").empty:
        raise SourceUnavailable("FRED returned no data")
    path.parent.mkdir(parents=True, exist_ok=True)
    macro.to_csv(path, date_format="%Y-%m-%d")
    return macro


def macro_features(macro: pd.DataFrame) -> pd.DataFrame:
    """Derived features on FRED's own calendar (index = observation date)."""
    m = macro.sort_index().ffill()
    vix = m["VIXCLS"]
    mean, sd = vix.rolling(252, min_periods=126).mean(), vix.rolling(252, min_periods=126).std()
    out = pd.DataFrame({
        "vix": vix,
        "vix_chg_5": vix.diff(5),
        "vix_z_252": (vix - mean) / sd.replace(0, np.nan),
        "yc_10y2y": m["T10Y2Y"],
        "dgs10_chg_20": m["DGS10"].diff(20),
        "credit_spread": m["BAA10Y"],
        "credit_chg_20": m["BAA10Y"].diff(20),
    })
    return out.replace([np.inf, -np.inf], np.nan)


def align_macro(features: pd.DataFrame, dates) -> pd.DataFrame:
    """Macro features as they were known on each trading date: a value dated d counts from d+1."""
    dates = pd.DatetimeIndex(pd.to_datetime(pd.Series(dates).unique())).sort_values()
    avail = features.copy()
    avail.index = pd.DatetimeIndex(avail.index) + pd.Timedelta(days=1)
    avail = avail[~avail.index.duplicated(keep="last")].sort_index()
    out = avail.reindex(avail.index.union(dates)).ffill().reindex(dates)
    out.index.name = "date"
    return out


def macro_snapshot(macro: pd.DataFrame) -> list[dict]:
    """Latest reading of each indicator for the dashboard, with a plain-words status."""
    out = []
    m = macro.sort_index()
    for sid, meta in FRED_SERIES.items():
        if sid not in m:
            continue
        s = m[sid].dropna()
        if s.empty:
            continue
        value, date = float(s.iloc[-1]), s.index[-1]
        if sid == "VIXCLS":
            prev = s.iloc[-6] if len(s) > 5 else s.iloc[0]
            change, change_label = value - float(prev), "vs a week ago"
            status, tone = ("Calm", "good") if value < 15 else ("Normal", "neutral") if value < 20 else \
                ("Nervous", "warn") if value < 30 else ("Fearful", "bad")
            explain = "Expected size of S&P 500 moves over the next month. Below 15 is calm, above 30 is fearful."
            unit = ""
        elif sid == "DGS10":
            prev = s.iloc[-21] if len(s) > 20 else s.iloc[0]
            change, change_label = value - float(prev), "vs a month ago"
            status, tone = ("Rising", "warn") if change > 0.15 else ("Falling", "good") if change < -0.15 else ("Steady", "neutral")
            explain = "What the US government pays to borrow for 10 years. Fast rises tend to weigh on stock prices."
            unit = "%"
        elif sid == "T10Y2Y":
            prev = s.iloc[-21] if len(s) > 20 else s.iloc[0]
            change, change_label = value - float(prev), "vs a month ago"
            status, tone = ("Inverted", "warn") if value < 0 else ("Normal", "neutral")
            explain = ("Long-term minus short-term interest rates. Below zero (inverted) has often come "
                       "before economic slowdowns.")
            unit = " pts"
        else:  # BAA10Y
            prev = s.iloc[-21] if len(s) > 20 else s.iloc[0]
            change, change_label = value - float(prev), "vs a month ago"
            status, tone = ("Stressed", "bad") if value > 3 else ("Widening", "warn") if change > 0.2 else ("Healthy", "good")
            explain = ("Extra interest companies pay over the government. When it widens, lenders are "
                       "getting nervous about the economy.")
            unit = " pts"
        out.append({
            "id": sid, "name": meta["name"], "value": round(value, 2), "unit": unit,
            "change": round(change, 2), "change_label": change_label, "date": date.strftime("%Y-%m-%d"),
            "status": status, "tone": tone, "explain": explain, "source": meta["source"],
            "url": f"https://fred.stlouisfed.org/series/{sid}",
        })
    return out


# ------------------------------------------------------------------- sectors
def parse_sector_overrides(text: str | None) -> dict[str, str]:
    out = {}
    for part in (text or "").split(","):
        if ":" in part:
            sym, etf = part.split(":", 1)
            if sym.strip() and etf.strip():
                out[sym.strip().upper()] = etf.strip().upper()
    return out


def sector_map(symbols, benchmark: str, overrides: dict[str, str] | None = None) -> dict[str, str]:
    overrides = overrides or {}
    return {s: overrides.get(s) or DEFAULT_SECTORS.get(s) or benchmark for s in symbols}


def sector_table(bars: pd.DataFrame) -> list[dict]:
    """1-day and 1-month change of each sector ETF, and whether it's above its 50-day average."""
    out = []
    for etf, name in SECTOR_ETFS.items():
        g = bars[bars["symbol"] == etf].sort_values("date")
        if len(g) < 2:
            continue
        c = g["close"].astype(float).reset_index(drop=True)
        sma50 = c.rolling(50, min_periods=20).mean().iloc[-1]
        out.append({
            "symbol": etf, "name": name, "close": round(float(c.iloc[-1]), 2),
            "change_1d": float(c.iloc[-1] / c.iloc[-2] - 1),
            "change_1m": float(c.iloc[-1] / c.iloc[max(len(c) - 22, 0)] - 1),
            "above_50d": bool(c.iloc[-1] > sma50) if pd.notna(sma50) else None,
        })
    return sorted(out, key=lambda r: -r["change_1m"])


# ---------------------------------------------------------------------- news
# A small finance word list written for this project. Phrases are matched first.
POSITIVE_PHRASES = [
    "beats estimates", "beat estimates", "tops estimates", "above estimates", "better than expected",
    "raises guidance", "raised guidance", "raises outlook", "raised outlook", "raises forecast", "lifts forecast",
    "record revenue", "record high", "all-time high", "raises price target", "price target raised",
    "upgraded to buy", "upgrade to buy", "strong demand", "share buyback", "stock buyback", "raises dividend",
    "dividend increase", "wins contract", "fda approval",
]
NEGATIVE_PHRASES = [
    "misses estimates", "missed estimates", "below estimates", "worse than expected", "weaker than expected",
    "cuts guidance", "cut guidance", "lowers guidance", "lowered guidance", "cuts outlook", "lowers outlook",
    "cuts forecast", "lowers price target", "cuts price target", "price target cut", "downgraded to sell",
    "downgrade to sell", "job cuts", "short seller", "class action", "data breach", "going concern", "profit warning",
]
POSITIVE_WORDS = set("""
beat beats tops surpass surpasses surpassed exceed exceeds exceeded record upgrade upgrades upgraded outperform
outperforms bullish surge surges surged soar soars soared jump jumps jumped rally rallies rallied gain gains gained
climb climbs climbed rebound rebounds rebounded boost boosts boosted strong stronger growth grows expand expands
expanded profitable approval approved partnership wins won launch launches launched breakthrough optimistic upbeat
robust accelerates accelerated accelerating buyback upside
""".split())
NEGATIVE_WORDS = set("""
miss misses missed downgrade downgrades downgraded underperform underperforms bearish plunge plunges plunged
plummet plummets plummeted tumble tumbles tumbled sink sinks sank slump slumps slumped slide slides slid drop drops
dropped fall falls fell decline declines declined lawsuit sued sues probe investigation recall recalls recalled
layoff layoffs bankruptcy default defaults fraud warns warned warning delay delays delayed halt halts halted fined
penalty antitrust weak weaker weakness slowdown concern concerns crash crashes crashed selloff sell-off downside
""".split())
_WORD_RE = re.compile(r"[a-z][a-z\-']*")


def text_sentiment(text: str) -> tuple[float, int, int]:
    """(score from -1 to 1, positive hits, negative hits) for a headline + summary."""
    t = f" {(text or '').lower()} "
    pos = neg = 0
    for phrase in POSITIVE_PHRASES:
        if f" {phrase}" in t:
            pos += 1
            t = t.replace(phrase, " ")
    for phrase in NEGATIVE_PHRASES:
        if f" {phrase}" in t:
            neg += 1
            t = t.replace(phrase, " ")
    for w in _WORD_RE.findall(t):
        if w in POSITIVE_WORDS:
            pos += 1
        elif w in NEGATIVE_WORDS:
            neg += 1
    return ((pos - neg) / (pos + neg) if pos + neg else 0.0), pos, neg


def tone_label(score: float | None) -> str:
    if score is None:
        return "none"
    return "positive" if score >= 0.2 else "negative" if score <= -0.2 else "neutral"


def article_view(a: dict) -> dict:
    """What the dashboard needs from one Alpaca/Benzinga article."""
    headline = (a.get("headline") or "").strip()
    summary = re.sub(r"<[^>]+>", " ", a.get("summary") or "").strip()
    score, pos, neg = text_sentiment(f"{headline}. {summary}")
    return {
        "id": a.get("id"), "headline": headline, "summary": summary[:280],
        "url": a.get("url") if str(a.get("url") or "").startswith(("https://", "http://")) else None,
        "source": (a.get("source") or "news").title(), "author": a.get("author") or "",
        "created_at": a.get("created_at"), "symbols": a.get("symbols") or [],
        "sentiment": round(score, 2), "tone": tone_label(score),
    }


def news_by_symbol(articles: list[dict], symbols, since: pd.Timestamp | None = None) -> dict[str, dict]:
    """Per symbol: how many recent articles, how many read positive/negative, average score."""
    out = {}
    for sym in symbols:
        mine = [a for a in articles if sym in a["symbols"]
                and (since is None or pd.Timestamp(a["created_at"]) >= since)]
        scores = [a["sentiment"] for a in mine]
        avg = float(np.mean(scores)) if scores else None
        out[sym] = {
            "count": len(mine), "positive": sum(a["tone"] == "positive" for a in mine),
            "negative": sum(a["tone"] == "negative" for a in mine),
            "score": None if avg is None else round(avg, 2), "tone": tone_label(avg),
        }
    return out


def news_vetoes(summary: dict[str, dict], min_articles: int, block_score: float) -> dict[str, str]:
    """Symbols the bot shouldn't open new positions in because recent news is clearly negative."""
    out = {}
    for sym, s in summary.items():
        if s["count"] >= min_articles and s["score"] is not None and s["score"] <= block_score:
            out[sym] = (f"recent news is mostly negative ({s['negative']} of {s['count']} headlines "
                        f"in the last few days)")
    return out


def utc_now() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc))
