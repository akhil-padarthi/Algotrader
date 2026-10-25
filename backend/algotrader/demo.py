"""Offline demo: synthetic market + stand-in Alpaca and FRED servers + real bot + real dashboard.

Lets you see the whole system working without API keys.  Prices, FRED data and news are
SYNTHETIC - the numbers it shows say nothing about real-market performance.
"""
from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field, replace

import pandas as pd

from .alpaca_client import AlpacaClient
from .api import create_app
from .context import SECTOR_ETFS, FredClient
from .data import clean_bars
from .fake_alpaca import ASSET_NAMES, FakeAlpaca, ServerThread
from .fake_sources import FakeFred
from .journal import Journal
from .pipeline import report, train_from_bars
from .synthetic import make_synthetic_bars, make_synthetic_macro, make_synthetic_news
from .trader import Trader

log = logging.getLogger(__name__)


@dataclass
class Demo:
    settings: object
    client: AlpacaClient
    fake: FakeAlpaca
    broker: ServerThread
    fred: FredClient
    fred_server: ServerThread
    accounts: list = field(default_factory=list)

    def new_account(self) -> tuple[str, str, str]:
        """A fresh stand-in Alpaca paper account ($100,000 cash) on the same market, for a demo user."""
        key, secret = "PKDEMO" + secrets.token_hex(8).upper(), secrets.token_hex(20)
        fake = FakeAlpaca(self.fake.bars, start_index=self.fake.i, api_key=key, secret_key=secret, news=self.fake.news)
        fake.live_price = self.fake.live_price          # the same live prices as everyone else
        server = ServerThread(fake.app)
        server.start()
        self.accounts.append((fake, server))
        return key, secret, server.url

    def user_client(self, key: str, secret: str, url: str) -> AlpacaClient:
        """A demo account's stand-in serves its own prices; real paper keys use Alpaca's data API."""
        local = url.startswith("http://127.0.0.1")
        return AlpacaClient(key, secret, url, url if local else "https://data.alpaca.markets", self.settings.alpaca_data_feed)

    def stop(self) -> None:
        self.broker.stop()
        self.fred_server.stop()
        for _, server in self.accounts:
            server.stop()


def build_demo(settings, sessions_to_simulate: int = 63, seed: int = 7, history_sessions: int = 1260) -> Demo:
    """Create the stand-in broker and FRED, train on 'past' data, then paper-trade N sessions."""
    total = history_sessions + sessions_to_simulate
    end = pd.Timestamp.today().normalize() - pd.offsets.BDay(1)
    extra = [e for e in SECTOR_ETFS if e not in settings.symbols]
    bars = clean_bars(make_synthetic_bars(
        settings.symbols + extra, sessions=total, end=end, seed=seed, signal_strength=0.15,
        benchmark=settings.benchmark,
    ))
    start = len(bars["date"].unique()) - 1 - sessions_to_simulate
    names = {s: ASSET_NAMES[s][0].split(" Common")[0].split(" Class")[0] for s in settings.symbols if s in ASSET_NAMES}
    news = make_synthetic_news(settings.symbols, end + pd.Timedelta(hours=20), days=int(sessions_to_simulate * 1.5) + 20,
                               seed=seed, names=names)
    fake = FakeAlpaca(bars, start_index=start, news=news)
    broker = ServerThread(fake.app)
    broker.start()
    fred_server = ServerThread(FakeFred(make_synthetic_macro(bars, settings.benchmark, seed)).app)
    fred_server.start()

    demo_dir = settings.data_dir / "demo"
    demo = replace(
        settings, demo_mode=True, alpaca_paper=True, alpaca_api_key="test-key", alpaca_secret_key="test-secret",
        alpaca_trading_url=broker.url, alpaca_data_url=broker.url, data_dir=demo_dir, model_dir=demo_dir / "models",
        trading_enabled=True, fred_url=fred_server.url, fred_api_key=None,
        database_url=None,     # the demo always uses throwaway local files
    )
    # every demo starts fresh, including its throwaway user accounts
    for stale in (demo.db_path, demo_dir / "macro.csv", demo.users_db_path,
                  demo.users_db_path.with_name("users.db-wal"), demo.users_db_path.with_name("users.db-shm")):
        if stale.exists():
            stale.unlink()
    client = AlpacaClient("test-key", "test-secret", broker.url, broker.url)
    fred = FredClient.from_settings(demo)

    # 1) train only on data the bot could have seen at the start (no peeking)
    from .context import load_macro
    hist = clean_bars(client.get_bars(demo.data_symbols, start="1900-01-01", end=fake.now()))
    macro = load_macro(demo, fred, refresh=True)
    macro = macro[macro.index <= fake.today]
    log.info("Demo: training on %d synthetic bars up to %s", len(hist), fake.today.date())
    result, bt = train_from_bars(demo, hist, macro=macro)
    print(report(result, bt))

    # 2) paper-trade forward one session at a time
    trader = Trader(demo, client, Journal(demo.db_path), fred=fred)
    trader.close_retry_delays = (0.05, 0.1)
    for _ in range(sessions_to_simulate):
        trader.run_cycle()
        fake.advance()
    trader.run_cycle()  # today's signals for the dashboard
    print(f"\nDemo: simulated {sessions_to_simulate} sessions of paper trading. "
          f"Equity ${fake.equity():,.2f} (started $100,000.00)")
    return Demo(demo, client, fake, broker, fred, fred_server)


def run_demo(settings, host: str = "127.0.0.1", port: int = 8000, sessions_to_simulate: int = 63, seed: int = 7):
    d = build_demo(settings, sessions_to_simulate, seed)
    stop = threading.Event()

    def ticker():
        while not stop.wait(1.5):
            d.fake.tick()

    threading.Thread(target=ticker, daemon=True).start()
    app = create_app(d.settings, client=d.client, journal=Journal(d.settings.db_path), fred=d.fred,
                     demo_accounts=d.new_account if d.settings.user_accounts else None, user_broker=d.user_client)
    print(f"\nDashboard (DEMO, synthetic data): http://{host}:{port}")
    if d.settings.user_accounts:
        print("Create an account there: the first one is the admin and sees the bot's account. Others can use a "
              "demo Alpaca account with $100,000 of pretend money.")
    print("Press Ctrl+C to stop.")
    try:
        app.run(host=host, port=port, threaded=True, use_reloader=False)
    finally:
        stop.set()
        d.stop()
