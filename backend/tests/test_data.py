import unittest

import numpy as np
import pandas as pd

from algotrader.data import CleaningReport, clean_bars, load_cached_bars, save_bars
from tests.helpers import TempDirTest, bars_from_closes


def raw(rows):
    return pd.DataFrame(rows, columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"])


class CleanBarsTests(TempDirTest):
    def test_timestamps_become_new_york_trading_dates(self):
        # Alpaca stamps daily bars at midnight New York = 05:00Z in winter, 04:00Z in summer
        df = clean_bars(raw([
            ["aapl", "2024-01-02T05:00:00Z", 10, 11, 9, 10.5, 100],
            ["AAPL", "2024-07-01T04:00:00Z", 10, 11, 9, 10.5, 100],
        ]))
        self.assertEqual(df["date"].dt.strftime("%Y-%m-%d").tolist(), ["2024-01-02", "2024-07-01"])
        self.assertEqual(df["symbol"].unique().tolist(), ["AAPL"])
        self.assertEqual(df["date"].dtype, "datetime64[ns]")

    def test_removes_duplicates_missing_and_non_positive(self):
        rep = CleaningReport()
        df = clean_bars(raw([
            ["X", "2024-01-02T05:00:00Z", 10, 11, 9, 10, 100],
            ["X", "2024-01-02T05:00:00Z", 10, 11, 9, 10.2, 100],   # duplicate -> keep last
            ["X", "2024-01-03T05:00:00Z", None, 11, 9, 10, 100],   # missing open
            ["X", "2024-01-04T05:00:00Z", 10, 11, 0, 10, 100],     # non-positive low
            ["X", "2024-01-05T05:00:00Z", 10, 11, 9, 10.1, None],  # missing volume -> 0
        ]), rep)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["close"].iloc[0], 10.2)
        self.assertEqual(df["volume"].iloc[1], 0)
        self.assertEqual((rep.duplicates, rep.missing_prices, rep.non_positive), (1, 1, 1))

    def test_fixes_inconsistent_high_low(self):
        rep = CleaningReport()
        df = clean_bars(raw([["X", "2024-01-02T05:00:00Z", 10, 9.5, 10.5, 10.2, 1]]), rep)
        self.assertEqual(df["high"].iloc[0], 10.2)
        self.assertEqual(df["low"].iloc[0], 10.0)
        self.assertEqual(rep.fixed_high_low, 1)

    def test_drops_isolated_spike_but_keeps_real_moves(self):
        closes = [100, 101, 102, 300, 103, 104, 70, 69, 68]  # 300 = bad print; 104 -> 70 is a real drop that sticks
        df = clean_bars(bars_from_closes(closes))
        self.assertNotIn(300, df["close"].tolist())
        self.assertIn(70, df["close"].tolist())
        self.assertEqual(len(df), len(closes) - 1)

    def test_empty_input(self):
        self.assertTrue(clean_bars(raw([])).empty)

    def test_cache_roundtrip(self):
        df = clean_bars(bars_from_closes(np.linspace(10, 20, 30)))
        save_bars(df, self.tmp)
        back = load_cached_bars(self.tmp)
        pd.testing.assert_frame_equal(df.reset_index(drop=True), back[df.columns].reset_index(drop=True), check_dtype=False)


if __name__ == "__main__":
    unittest.main()
