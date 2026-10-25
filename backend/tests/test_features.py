import unittest

import numpy as np
import pandas as pd

from algotrader.features import (
    MARKET_FEATURES, STOCK_FEATURES, WARMUP_SESSIONS, add_labels, build_features, feature_columns, latest_rows, rsi,
)
from tests.helpers import bars_from_closes, synthetic


class FeatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bars = synthetic(sessions=400, seed=2)
        cls.frame = build_features(cls.bars, "SPY")

    def test_all_feature_columns_present(self):
        cols = feature_columns(self.frame)
        self.assertEqual(cols, STOCK_FEATURES + MARKET_FEATURES)
        self.assertNotIn("SPY", self.frame["symbol"].unique())   # benchmark is context, not a trade
        self.assertIn("market_ok", self.frame)

    def test_no_lookahead(self):
        """Appending future bars must not change any feature on earlier dates."""
        cut = self.bars["date"].sort_values().unique()[300]
        past = build_features(self.bars[self.bars["date"] <= cut], "SPY")
        full = self.frame[self.frame["date"] <= cut]
        cols = feature_columns(past) + ["market_ok"]
        a = past.sort_values(["symbol", "date"]).reset_index(drop=True)[cols]
        b = full.sort_values(["symbol", "date"]).reset_index(drop=True)[cols]
        pd.testing.assert_frame_equal(a, b, check_exact=False, rtol=1e-10, atol=1e-12)

    def test_warmup_rows_are_nan_then_complete(self):
        g = self.frame[self.frame["symbol"] == "AAA"].sort_values("date")
        cols = feature_columns(self.frame)
        self.assertTrue(g[cols].iloc[: WARMUP_SESSIONS - 1].isna().any(axis=1).all())
        self.assertFalse(g[cols].iloc[WARMUP_SESSIONS + 5:].isna().any().any())
        self.assertFalse(np.isinf(g[cols].to_numpy(dtype=float)).any())

    def test_rsi_bounds_and_extremes(self):
        up = pd.Series(np.arange(1, 60, dtype=float))
        self.assertAlmostEqual(rsi(up).iloc[-1], 100.0)
        down = pd.Series(np.arange(60, 1, -1, dtype=float))
        self.assertAlmostEqual(rsi(down).iloc[-1], 0.0)
        r = rsi(self.bars[self.bars["symbol"] == "AAA"]["close"].reset_index(drop=True)).dropna()
        self.assertTrue(((r >= 0) & (r <= 100)).all())

    def test_labels_use_next_open_to_open(self):
        opens = [10, 11, 12, 13, 14, 15, 16, 17]
        bars = bars_from_closes(opens, opens=opens)
        lab = add_labels(build_features(bars, benchmark=None), horizon=2, threshold=0.1)
        lab = lab.sort_values("date").reset_index(drop=True)
        # row 0: enter at open[1]=11, exit at open[3]=13 -> +18.18%
        self.assertAlmostEqual(lab.loc[0, "fwd_return"], 13 / 11 - 1)
        self.assertEqual(lab.loc[0, "label"], 1.0)
        # row 5: enter 16 exit 17? needs open[7]... open[1+5]=16, open[3+5] missing -> NaN
        self.assertTrue(np.isnan(lab.loc[5, "label"]))
        self.assertEqual(int(lab["label"].notna().sum()), len(opens) - 3)

    def test_latest_rows_one_per_symbol(self):
        cols = feature_columns(self.frame)
        latest = latest_rows(self.frame, cols)
        self.assertEqual(sorted(latest["symbol"]), sorted(self.frame["symbol"].unique()))
        self.assertTrue((latest["date"] == self.frame["date"].max()).all())

    def test_market_filter_flag(self):
        spy = self.bars[self.bars["symbol"] == "SPY"].set_index("date")["close"]
        expected = spy > spy.rolling(50).mean()
        got = self.frame[self.frame["symbol"] == "AAA"].set_index("date")["market_ok"]
        common = expected.index[60:]
        self.assertTrue((got.loc[common] == expected.loc[common]).all())


if __name__ == "__main__":
    unittest.main()
