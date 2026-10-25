"""Shared fixtures for the test-suite."""
from __future__ import annotations

import logging
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd

from algotrader.alpaca_client import AlpacaClient
from algotrader.config import ModelParams, Settings, StrategyParams
from algotrader.data import clean_bars
from algotrader.fake_alpaca import FakeAlpaca, ServerThread
from algotrader.synthetic import make_synthetic_bars

logging.getLogger("werkzeug").setLevel(logging.ERROR)
logging.getLogger("algotrader").setLevel(logging.CRITICAL)

SYMBOLS = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
FAST_MODEL = ModelParams(n_estimators=60, min_samples_leaf=40, cv_splits=3)


def synthetic(sessions=700, seed=1, signal=0.0, symbols=SYMBOLS, end="2026-10-09"):
    return clean_bars(make_synthetic_bars(symbols, sessions=sessions, end=end, seed=seed, signal_strength=signal))


def bars_from_closes(closes, symbol="AAA", start="2024-01-02", opens=None, highs=None, lows=None, volume=1_000_000):
    """Hand-made bars for exact-arithmetic tests."""
    closes = np.asarray(closes, dtype=float)
    dates = pd.bdate_range(start, periods=len(closes))
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    highs = np.maximum(opens, closes) if highs is None else np.asarray(highs, dtype=float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, dtype=float)
    return pd.DataFrame({"date": dates, "symbol": symbol, "open": opens, "high": highs, "low": lows,
                         "close": closes, "volume": volume})


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="algotrader-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class FakeBrokerTest(TempDirTest):
    """Starts a stand-in Alpaca server on localhost for each test."""

    sessions = 400
    start_offset = 30       # sessions left to replay after the start
    signal = 0.25
    symbols = SYMBOLS

    def setUp(self):
        super().setUp()
        self.bars = synthetic(self.sessions, seed=5, signal=self.signal, symbols=self.symbols)
        n_dates = self.bars["date"].nunique()
        self.fake = FakeAlpaca(self.bars, start_index=n_dates - 1 - self.start_offset)
        self.server = ServerThread(self.fake.app)
        self.server.start()
        self.client = AlpacaClient("test-key", "test-secret", self.server.url, self.server.url)
        self.client.backoff_scale = 0
        self.settings = Settings(
            alpaca_api_key="test-key", alpaca_secret_key="test-secret",
            alpaca_trading_url=self.server.url, alpaca_data_url=self.server.url,
            symbols=list(self.symbols), data_dir=self.tmp, model_dir=self.tmp / "models",
            model=FAST_MODEL, strategy=StrategyParams(),
            user_accounts=False,     # single-user behaviour; accounts have their own tests (test_accounts.py)
        )

    def tearDown(self):
        self.server.stop()
        super().tearDown()

    def with_strategy(self, **kw):
        self.settings = replace(self.settings, strategy=replace(self.settings.strategy, **kw))
        return self.settings


class StubModel:
    """Model double with fixed probabilities per symbol (default 0.5)."""

    def __init__(self, probas=None, feature_cols=None):
        from algotrader.features import MARKET_FEATURES, STOCK_FEATURES
        self.probas = dict(probas or {})
        self.feature_cols = feature_cols or (STOCK_FEATURES + MARKET_FEATURES)
        self.meta = {}

    def predict_proba(self, rows):
        return np.array([self.probas.get(s, 0.5) for s in rows["symbol"]], dtype=float)
