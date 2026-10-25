"""Central configuration.

Every setting can be overridden with an environment variable (or a line in
`.env`).  Defaults are deliberately conservative: paper trading, small
positions, stop-losses on every entry.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path

try:  # python-dotenv is optional at runtime
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

BACKEND_DIR = Path(__file__).resolve().parent.parent
PROJECT_DIR = BACKEND_DIR.parent

PAPER_TRADING_URL = "https://paper-api.alpaca.markets"
LIVE_TRADING_URL = "https://api.alpaca.markets"
DATA_URL = "https://data.alpaca.markets"

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_REAL_MONEY"

DEFAULT_SYMBOLS = "AAPL,MSFT,NVDA,AMZN,GOOGL,META,JPM,V,UNH,XOM"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _bool(name: str, default: bool) -> bool:
    value = _env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "y", "on"}


def _float(name: str, default: float) -> float:
    value = _env(name)
    return default if value is None else float(value)


def _int(name: str, default: int) -> int:
    value = _env(name)
    return default if value is None else int(value)


def _list(name: str, default: str) -> list[str]:
    value = _env(name, default) or ""
    return [s.strip().upper() for s in value.split(",") if s.strip()]


@dataclass(frozen=True)
class ModelParams:
    horizon_days: int = 5            # predict the return over the next N sessions
    label_threshold: float = 0.01    # "good entry" = forward return above +1%
    n_estimators: int = 400
    max_depth: int = 6
    min_samples_leaf: int = 40
    max_features: str = "sqrt"
    test_fraction: float = 0.25      # most recent 25% of dates held out, never trained on
    cv_splits: int = 5
    random_state: int = 42


@dataclass(frozen=True)
class StrategyParams:
    entry_threshold: float = 0.58    # buy when P(up > threshold) >= this
    exit_threshold: float = 0.45     # sell when probability falls below this
    max_positions: int = 5
    position_size_pct: float = 0.18  # of equity per position (5 x 18% = 90% max invested)
    stop_loss_pct: float = 0.05
    take_profit_pct: float = 0.10
    max_holding_days: int = 10
    use_market_filter: bool = True   # only open new longs when SPY > its 50-day average
    max_daily_loss_pct: float = 0.03 # no new entries after a -3% day
    slippage_bps: float = 5.0        # assumed cost per side in the backtest


@dataclass(frozen=True)
class Settings:
    # Alpaca
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_paper: bool = True
    alpaca_trading_url: str = PAPER_TRADING_URL
    alpaca_data_url: str = DATA_URL
    alpaca_data_feed: str = "iex"
    live_trading_confirm: str | None = None

    # Universe & data
    symbols: list[str] = field(default_factory=lambda: DEFAULT_SYMBOLS.split(","))
    sector_overrides: dict = field(default_factory=dict)   # SECTORS=SYM:ETF,... (defaults in context.py)
    benchmark: str = "SPY"
    history_years: int = 5
    data_dir: Path = BACKEND_DIR / "data"
    model_dir: Path = BACKEND_DIR / "models"
    database_url: str | None = None  # PostgreSQL (e.g. Supabase) for accounts + journal; None = SQLite files

    # Current market conditions (FRED, sector ETFs, news)
    use_market_context: bool = True  # offer FRED + sector features to the model (kept only if they help in testing)
    fred_url: str = "https://fred.stlouisfed.org"
    fred_api_url: str = "https://api.stlouisfed.org"
    fred_api_key: str | None = None  # optional; without it the public CSV download is used
    news_check: bool = True          # skip new buys when recent news is clearly negative (live only)
    news_lookback_days: int = 3
    news_min_articles: int = 3
    news_block_score: float = -0.4

    # Bot behaviour
    trading_enabled: bool = True     # False = compute signals, place no orders
    manual_trading: bool = True      # buy/sell buttons in the dashboard
    manual_trading_note: str = ""    # why they are off, when switched off for safety
    trade_time: str = "16:20"        # America/New_York, after the close; orders fill next open
    timezone: str = "America/New_York"

    # Dashboard API
    api_host: str = "127.0.0.1"     # this computer only; 0.0.0.0 = reachable from the network
    api_port: int = 8000
    dashboard_user: str = "admin"
    dashboard_password: str | None = None
    cors_origins: list[str] = field(default_factory=list)
    stream_interval_seconds: float = 5.0
    frontend_dist: Path = PROJECT_DIR / "frontend" / "dist"
    demo_mode: bool = False

    # User accounts (sign up, log in, each person trades their own Alpaca paper account)
    user_accounts: bool = True
    allow_signups: bool = True
    secret_key: str | None = None    # encrypts saved Alpaca keys; default: backend/data/secret.key
    session_days: int = 30           # how long a log-in lasts on one browser
    user_trading_url: str = PAPER_TRADING_URL   # people's own accounts are always paper accounts
    local_only: bool | None = None   # None: decided from api_host

    # Hosting
    live_stream: bool = True         # push live updates (Server-Sent Events); off on Vercel = poll instead
    trust_proxy: bool = False        # behind a proxy that sets the visitor's address (Vercel): use its headers
    cron_secret: str | None = None   # turns on /api/cron/trade, the daily bot run called by a scheduler

    model: ModelParams = field(default_factory=ModelParams)
    strategy: StrategyParams = field(default_factory=StrategyParams)

    # ------------------------------------------------------------------ helpers
    @property
    def db_path(self) -> Path:
        return self.data_dir / "algotrader.db"

    @property
    def users_db_path(self) -> Path:
        return self.data_dir / "users.db"

    @property
    def journal_db(self) -> str | Path:
        """Where the bot's journal is kept: DATABASE_URL, or the SQLite file."""
        return self.database_url or self.db_path

    @property
    def users_db(self) -> str | Path:
        """Where user accounts are kept: DATABASE_URL, or the SQLite file."""
        return self.database_url or self.users_db_path

    @property
    def secret_key_path(self) -> Path:
        return self.data_dir / "secret.key"

    @property
    def is_local_only(self) -> bool:
        """Only this computer can open the dashboard (so the first account may become the admin)."""
        if self.local_only is not None:
            return self.local_only
        return self.api_host in ("127.0.0.1", "localhost", "::1")

    @property
    def has_credentials(self) -> bool:
        return bool(self.alpaca_api_key and self.alpaca_secret_key)

    @property
    def mode(self) -> str:
        if self.demo_mode:
            return "demo"
        return "paper" if self.alpaca_paper else "live"

    @property
    def all_symbols(self) -> list[str]:
        out = list(dict.fromkeys(self.symbols))
        if self.benchmark not in out:
            out.append(self.benchmark)
        return out

    @property
    def sector_map(self) -> dict[str, str]:
        from .context import sector_map
        return sector_map(self.symbols, self.benchmark, self.sector_overrides)

    @property
    def sector_etfs(self) -> list[str]:
        """Sector ETFs the bot's stocks belong to (needed for the model's sector features)."""
        return sorted({e for e in self.sector_map.values() if e not in self.all_symbols})

    @property
    def data_symbols(self) -> list[str]:
        """Everything whose price history the model needs: stocks, benchmark, their sector ETFs."""
        return self.all_symbols + (self.sector_etfs if self.use_market_context else [])

    def guard_exposed_dashboard(self, exposed: bool) -> "Settings":
        """A dashboard other devices can reach must not place orders without a log-in or password."""
        if exposed:
            self = replace(self, local_only=False)
        if exposed and self.manual_trading and not self.dashboard_password and not self.user_accounts:
            return replace(self, manual_trading=False, manual_trading_note=(
                "Buying and selling from the dashboard is off because it can be reached from other devices "
                "and has no log-in. Set USER_ACCOUNTS=true (or DASHBOARD_PASSWORD) in backend/.env and restart."))
        return self

    def with_overrides(self, **kwargs) -> "Settings":
        return replace(self, **kwargs)

    def validate(self) -> None:
        """Refuse obviously dangerous or broken configurations."""
        if not self.alpaca_paper and self.live_trading_confirm != LIVE_CONFIRM_PHRASE:
            raise ValueError(
                "ALPACA_PAPER=false would trade real money. Set "
                f"LIVE_TRADING_CONFIRM={LIVE_CONFIRM_PHRASE} if you really mean it."
            )
        s = self.strategy
        if not 0 < s.position_size_pct <= 1:
            raise ValueError("POSITION_SIZE_PCT must be in (0, 1]")
        if s.position_size_pct * s.max_positions > 1.0 + 1e-9:
            raise ValueError("POSITION_SIZE_PCT x MAX_POSITIONS must not exceed 100% (no leverage)")
        if not 0 <= s.exit_threshold <= s.entry_threshold <= 1:
            raise ValueError("Need 0 <= EXIT_THRESHOLD <= ENTRY_THRESHOLD <= 1")
        if not 0 < s.stop_loss_pct < 1 or not 0 < s.take_profit_pct < 5:
            raise ValueError("STOP_LOSS_PCT / TAKE_PROFIT_PCT out of range")
        if self.model.horizon_days < 1:
            raise ValueError("HORIZON_DAYS must be >= 1")
        if not self.symbols:
            raise ValueError("SYMBOLS is empty")


def parse_sector_overrides(text: str | None) -> dict[str, str]:
    from .context import parse_sector_overrides as parse
    return parse(text)


def _database_url(raw: str | None) -> str | None:
    from .db import normalize_url
    raw = (raw or "").strip().strip('"').strip("'")
    return normalize_url(raw) if raw else None


def load_settings(env_file: str | os.PathLike | None = None, **overrides) -> Settings:
    """Build Settings from the environment (and an optional .env file)."""
    if load_dotenv is not None:
        if env_file:
            load_dotenv(env_file, override=False)
        else:
            for candidate in (BACKEND_DIR / ".env", PROJECT_DIR / ".env"):
                if candidate.exists():
                    load_dotenv(candidate, override=False)

    paper = _bool("ALPACA_PAPER", True)
    model = ModelParams(
        horizon_days=_int("HORIZON_DAYS", ModelParams.horizon_days),
        label_threshold=_float("LABEL_THRESHOLD", ModelParams.label_threshold),
        n_estimators=_int("N_ESTIMATORS", ModelParams.n_estimators),
        max_depth=_int("MAX_DEPTH", ModelParams.max_depth),
        min_samples_leaf=_int("MIN_SAMPLES_LEAF", ModelParams.min_samples_leaf),
        test_fraction=_float("TEST_FRACTION", ModelParams.test_fraction),
        cv_splits=_int("CV_SPLITS", ModelParams.cv_splits),
        random_state=_int("RANDOM_STATE", ModelParams.random_state),
    )
    strategy = StrategyParams(
        entry_threshold=_float("ENTRY_THRESHOLD", StrategyParams.entry_threshold),
        exit_threshold=_float("EXIT_THRESHOLD", StrategyParams.exit_threshold),
        max_positions=_int("MAX_POSITIONS", StrategyParams.max_positions),
        position_size_pct=_float("POSITION_SIZE_PCT", StrategyParams.position_size_pct),
        stop_loss_pct=_float("STOP_LOSS_PCT", StrategyParams.stop_loss_pct),
        take_profit_pct=_float("TAKE_PROFIT_PCT", StrategyParams.take_profit_pct),
        max_holding_days=_int("MAX_HOLDING_DAYS", StrategyParams.max_holding_days),
        use_market_filter=_bool("USE_MARKET_FILTER", StrategyParams.use_market_filter),
        max_daily_loss_pct=_float("MAX_DAILY_LOSS_PCT", StrategyParams.max_daily_loss_pct),
        slippage_bps=_float("SLIPPAGE_BPS", StrategyParams.slippage_bps),
    )
    data_dir = Path(_env("DATA_DIR", str(BACKEND_DIR / "data")))
    model_dir = Path(_env("MODEL_DIR", str(BACKEND_DIR / "models")))
    cors = _env("CORS_ORIGINS", "") or ""

    settings = Settings(
        alpaca_api_key=_env("ALPACA_API_KEY"),
        alpaca_secret_key=_env("ALPACA_SECRET_KEY"),
        alpaca_paper=paper,
        alpaca_trading_url=_env("ALPACA_TRADING_URL", PAPER_TRADING_URL if paper else LIVE_TRADING_URL),
        alpaca_data_url=_env("ALPACA_DATA_URL", DATA_URL),
        alpaca_data_feed=_env("ALPACA_DATA_FEED", "iex"),
        live_trading_confirm=_env("LIVE_TRADING_CONFIRM"),
        symbols=_list("SYMBOLS", DEFAULT_SYMBOLS),
        benchmark=(_env("BENCHMARK", "SPY") or "SPY").upper(),
        history_years=_int("HISTORY_YEARS", 5),
        data_dir=data_dir,
        model_dir=model_dir,
        database_url=_database_url(_env("DATABASE_URL")),
        sector_overrides=parse_sector_overrides(_env("SECTORS")),
        use_market_context=_bool("USE_MARKET_CONTEXT", True),
        fred_url=_env("FRED_URL", "https://fred.stlouisfed.org"),
        fred_api_url=_env("FRED_API_URL", "https://api.stlouisfed.org"),
        fred_api_key=_env("FRED_API_KEY"),
        news_check=_bool("NEWS_CHECK", True),
        news_lookback_days=_int("NEWS_LOOKBACK_DAYS", 3),
        news_min_articles=_int("NEWS_MIN_ARTICLES", 3),
        news_block_score=_float("NEWS_BLOCK_SCORE", -0.4),
        trading_enabled=_bool("TRADING_ENABLED", True),
        manual_trading=_bool("MANUAL_TRADING", True),
        trade_time=_env("TRADE_TIME", "16:20"),
        timezone=_env("TIMEZONE", "America/New_York"),
        api_host=_env("API_HOST", "127.0.0.1"),
        api_port=_int("PORT", _int("API_PORT", 8000)),
        dashboard_user=_env("DASHBOARD_USER", "admin"),
        dashboard_password=_env("DASHBOARD_PASSWORD"),
        cors_origins=[o.strip() for o in cors.split(",") if o.strip()],
        stream_interval_seconds=_float("STREAM_INTERVAL_SECONDS", 5.0),
        frontend_dist=Path(_env("FRONTEND_DIST", str(PROJECT_DIR / "frontend" / "dist"))),
        demo_mode=_bool("DEMO_MODE", False),
        user_accounts=_bool("USER_ACCOUNTS", True),
        allow_signups=_bool("ALLOW_SIGNUPS", True),
        secret_key=_env("SECRET_KEY") or None,
        session_days=_int("SESSION_DAYS", 30),
        live_stream=_bool("LIVE_STREAM", True),
        trust_proxy=_bool("TRUST_PROXY", False),
        cron_secret=_env("CRON_SECRET") or None,
        model=model,
        strategy=strategy,
    )
    if overrides:
        settings = replace(settings, **overrides)
    return settings


def setup_logging(level: str | None = None) -> None:
    logging.basicConfig(
        level=getattr(logging, (level or _env("LOG_LEVEL", "INFO")).upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


__all__ = [
    "Settings", "ModelParams", "StrategyParams", "load_settings", "setup_logging",
    "LIVE_CONFIRM_PHRASE", "PAPER_TRADING_URL", "LIVE_TRADING_URL", "DATA_URL",
]
