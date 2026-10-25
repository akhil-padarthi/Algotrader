"""End-to-end: the real CLI against the stand-in Alpaca server, and the pipeline on real prices."""
import os
import subprocess
import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from algotrader.backtest import run_backtest
from algotrader.config import StrategyParams
from algotrader.data import clean_bars
from algotrader.features import add_labels, build_features
from algotrader.model import train_and_evaluate
from tests.helpers import FAST_MODEL, FakeBrokerTest

BACKEND = Path(__file__).resolve().parent.parent


class CliEndToEnd(FakeBrokerTest):
    sessions = 900
    start_offset = 5

    def cli(self, *args, expect=0):
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ALPACA_", "SYMBOLS"))}
        env.update({
            "ALPACA_API_KEY": "test-key", "ALPACA_SECRET_KEY": "test-secret",
            "ALPACA_TRADING_URL": self.server.url, "ALPACA_DATA_URL": self.server.url,
            "SYMBOLS": ",".join(self.symbols), "DATA_DIR": str(self.tmp / "data"), "MODEL_DIR": str(self.tmp / "models"),
            "N_ESTIMATORS": "60", "CV_SPLITS": "3", "USE_MARKET_FILTER": "false", "LOG_LEVEL": "WARNING",
        })
        proc = subprocess.run([sys.executable, "-m", "algotrader", "--env-file", "/nonexistent", *args],
                              cwd=BACKEND, env=env, capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, expect, f"{args}\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}")
        return proc.stdout

    def test_full_workflow(self):
        out = self.cli("check")
        self.assertIn("All checks passed", out)

        out = self.cli("train")
        self.assertIn("out-of-sample", out)
        self.assertIn("BACKTEST", out)
        for f in ("model.joblib", "model_meta.json", "oos_predictions.csv", "backtest.json"):
            self.assertTrue((self.tmp / "models" / f).exists(), f)
        self.assertTrue((self.tmp / "data" / "bars_1day.csv").exists())

        out = self.cli("backtest", "--json")
        self.assertIn('"total_return"', out)

        out = self.cli("trade", "--dry-run")
        self.assertIn("Status: ok", out)
        self.assertEqual(self.fake.orders, [])            # dry run placed nothing

        out = self.cli("trade")
        self.assertIn("Status: ok", out)
        n_orders = sum(1 for line in out.splitlines() if line.strip().startswith("ORDER"))
        self.assertEqual(n_orders, len(self.fake.orders))

    def test_check_fails_cleanly_with_bad_keys(self):
        env_bad = dict(os.environ, ALPACA_API_KEY="bad", ALPACA_SECRET_KEY="bad",
                       ALPACA_TRADING_URL=self.server.url, ALPACA_DATA_URL=self.server.url)
        proc = subprocess.run([sys.executable, "-m", "algotrader", "--env-file", "/nonexistent", "check"],
                              cwd=BACKEND, env=env_bad, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("401", proc.stderr + proc.stdout)

    def test_live_mode_refused_without_confirmation(self):
        env = dict(os.environ, ALPACA_PAPER="false", ALPACA_API_KEY="k", ALPACA_SECRET_KEY="s")
        env.pop("LIVE_TRADING_CONFIRM", None)
        proc = subprocess.run([sys.executable, "-m", "algotrader", "--env-file", "/nonexistent", "trade"],
                              cwd=BACKEND, env=env, capture_output=True, text=True, timeout=120)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("real money", proc.stderr)


def load_real_goog():
    """GOOG daily OHLCV 2004-2008, shipped with matplotlib as sample data (real market prices)."""
    try:
        import matplotlib
    except ImportError:
        return None
    path = Path(matplotlib.__file__).parent / "mpl-data" / "sample_data" / "goog.npz"
    if not path.exists():
        return None
    p = np.load(path)["price_data"]
    df = pd.DataFrame({"date": p["date"].astype("datetime64[ns]"), "open": p["open"], "high": p["high"],
                       "low": p["low"], "close": p["close"], "volume": p["volume"]})
    df["symbol"] = "GOOG"
    return df


class RealDataPipeline(unittest.TestCase):
    """Runs the full pipeline on real prices (no network needed)."""

    def test_goog_2004_2008(self):
        raw = load_real_goog()
        if raw is None:
            self.skipTest("matplotlib sample data not available")
        bars = clean_bars(raw)
        self.assertGreater(len(bars), 1000)
        frame = add_labels(build_features(bars, benchmark=None), 5, 0.01)
        res = train_and_evaluate(frame, FAST_MODEL, 0.58)
        auc = res.meta["oos"]["auc"]
        self.assertTrue(0.35 < auc < 0.70, f"implausible out-of-sample AUC on real data: {auc}")
        bt = run_backtest(res.oos_predictions, StrategyParams(use_market_filter=False))
        m = bt.metrics
        # accounting identity: every dollar of P&L is explained by a trade
        self.assertAlmostEqual(m["final_equity"], m["initial_equity"] + bt.trades["pnl"].sum(), places=4)
        self.assertGreaterEqual(m["max_drawdown"], -1.0)
        print(f"\n  [real GOOG data] OOS AUC {auc:.3f}, backtest {m['total_return']:+.1%}, "
              f"{m['trades']} trades, period {m['start']}..{m['end']}")


if __name__ == "__main__":
    unittest.main()
