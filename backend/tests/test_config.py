import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from algotrader.config import LIVE_CONFIRM_PHRASE, LIVE_TRADING_URL, PAPER_TRADING_URL, Settings, load_settings


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="algotrader-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_defaults_are_paper_and_valid(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = load_settings(env_file="/nonexistent")
        self.assertTrue(s.alpaca_paper)
        self.assertEqual(s.alpaca_trading_url, PAPER_TRADING_URL)
        self.assertEqual(s.mode, "paper")
        self.assertEqual(s.benchmark, "SPY")
        self.assertEqual(s.api_host, "127.0.0.1")  # dashboard private to this computer by default
        self.assertIn("SPY", s.all_symbols)
        s.validate()

    def test_env_overrides(self):
        env = {"SYMBOLS": "aapl, msft", "ENTRY_THRESHOLD": "0.6", "MAX_POSITIONS": "3", "PORT": "9001",
               "USE_MARKET_FILTER": "false", "TRADING_ENABLED": "no"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = load_settings(env_file="/nonexistent")
        self.assertEqual(s.symbols, ["AAPL", "MSFT"])
        self.assertEqual(s.strategy.entry_threshold, 0.6)
        self.assertEqual(s.strategy.max_positions, 3)
        self.assertEqual(s.api_port, 9001)
        self.assertFalse(s.strategy.use_market_filter)
        self.assertFalse(s.trading_enabled)

    def test_live_trading_needs_explicit_confirmation(self):
        with mock.patch.dict(os.environ, {"ALPACA_PAPER": "false"}, clear=True):
            s = load_settings(env_file="/nonexistent")
        self.assertEqual(s.alpaca_trading_url, LIVE_TRADING_URL)
        with self.assertRaises(ValueError):
            s.validate()
        replace(s, live_trading_confirm=LIVE_CONFIRM_PHRASE).validate()

    def test_env_example_parses_to_the_defaults(self):
        example = Path(__file__).resolve().parents[2] / ".env.example"
        with mock.patch.dict(os.environ, {}, clear=True):
            s = load_settings(env_file=example)
            self.assertEqual(os.environ.get("TRADE_TIME"), "16:20")  # really came from the file
        with mock.patch.dict(os.environ, {}, clear=True):
            d = Settings()
        self.assertEqual(s.strategy, d.strategy)
        self.assertEqual(s.model, d.model)
        self.assertEqual((s.symbols, s.trade_time, s.alpaca_paper, s.dashboard_password, s.api_host),
                         (d.symbols, "16:20", True, None, "127.0.0.1"))

    def test_wsgi_entry_point(self):
        import importlib
        import sys
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "DATA_DIR": str(self.tmp)},
                             clear=True):
            sys.modules.pop("algotrader.wsgi", None)
            wsgi = importlib.import_module("algotrader.wsgi")
        self.assertEqual(wsgi.app.test_client().get("/api/health").get_json()["status"], "ok")
        # served to the network: trading needs a log-in (accounts are on by default), and the first
        # account does not become the admin automatically
        self.assertTrue(wsgi.settings.manual_trading)
        self.assertFalse(wsgi.settings.is_local_only)
        # without accounts or a password the dashboard can't place orders
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "USER_ACCOUNTS": "false",
                                          "DATA_DIR": str(self.tmp)}, clear=True):
            sys.modules.pop("algotrader.wsgi", None)
            wsgi = importlib.import_module("algotrader.wsgi")
        self.assertFalse(wsgi.settings.manual_trading)
        self.assertIn("USER_ACCOUNTS", wsgi.settings.manual_trading_note)
        with mock.patch.dict(os.environ, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s", "USER_ACCOUNTS": "false",
                                          "DASHBOARD_PASSWORD": "pw", "DATA_DIR": str(self.tmp)}, clear=True):
            sys.modules.pop("algotrader.wsgi", None)
            wsgi = importlib.import_module("algotrader.wsgi")
        self.assertTrue(wsgi.settings.manual_trading)

    def test_exposed_dashboard_guard(self):
        s = Settings(user_accounts=False)
        self.assertTrue(Settings().guard_exposed_dashboard(exposed=True).manual_trading)   # log-in required anyway
        self.assertTrue(s.guard_exposed_dashboard(exposed=False).manual_trading)      # this computer only
        self.assertFalse(s.guard_exposed_dashboard(exposed=True).manual_trading)      # network, no password
        self.assertTrue(replace(s, dashboard_password="pw").guard_exposed_dashboard(exposed=True).manual_trading)
        off = replace(s, manual_trading=False).guard_exposed_dashboard(exposed=True)
        self.assertEqual(off.manual_trading_note, "")                                  # user's own choice

    def test_rejects_leverage_and_bad_thresholds(self):
        s = Settings()
        with self.assertRaises(ValueError):
            replace(s, strategy=replace(s.strategy, position_size_pct=0.3, max_positions=5)).validate()
        with self.assertRaises(ValueError):
            replace(s, strategy=replace(s.strategy, entry_threshold=0.4, exit_threshold=0.5)).validate()
        with self.assertRaises(ValueError):
            replace(s, symbols=[]).validate()


if __name__ == "__main__":
    unittest.main()
