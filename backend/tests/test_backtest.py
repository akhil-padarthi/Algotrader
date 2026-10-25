"""Backtest accounting checked against hand-computed numbers."""
import unittest

import numpy as np
import pandas as pd

from algotrader.backtest import perf_stats, run_backtest
from algotrader.config import StrategyParams

P = StrategyParams(entry_threshold=0.6, exit_threshold=0.45, max_positions=1, position_size_pct=0.5,
                   stop_loss_pct=0.05, take_profit_pct=0.10, max_holding_days=3, use_market_filter=True,
                   slippage_bps=0.0)


def pred(rows):
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "proba"])
    df["date"] = pd.bdate_range("2024-01-02", periods=len(df))
    df["symbol"] = "AAA"
    df["market_ok"] = True
    return df


class BacktestTests(unittest.TestCase):
    def test_take_profit_exact_accounting(self):
        # day0 close 100 p=0.9 -> buy at day1 open 100; target 110 hit on day2 high
        df = pred([
            [100, 100, 100, 100, 0.9],
            [100, 102, 99, 101, 0.5],
            [105, 111, 104, 108, 0.5],
            [108, 108, 108, 108, 0.5],
        ])
        bt = run_backtest(df, P, initial_cash=10_000)
        t = bt.trades.iloc[0]
        qty = int(5_000 / 1.01 // 100)                      # 49 shares
        self.assertEqual((t["qty"], t["entry_price"], t["exit_price"], t["reason"]), (qty, 100.0, 110.0, "take_profit"))
        self.assertAlmostEqual(bt.metrics["final_equity"], 10_000 + qty * 10)
        self.assertEqual(bt.equity["positions"].tolist(), [0, 1, 0, 0])

    def test_gap_through_stop_fills_at_open(self):
        df = pred([
            [100, 100, 100, 100, 0.9],
            [100, 100, 99, 99, 0.5],
            [90, 92, 88, 91, 0.5],       # opens below the 95 stop
            [91, 91, 91, 91, 0.5],
        ])
        t = run_backtest(df, P, initial_cash=10_000).trades.iloc[0]
        self.assertEqual((t["exit_price"], t["reason"]), (90.0, "stop_loss"))

    def test_stop_wins_when_stop_and_target_hit_same_day(self):
        df = pred([
            [100, 100, 100, 100, 0.9],
            [100, 111, 94, 100, 0.5],    # range covers both 95 and 110
            [100, 100, 100, 100, 0.5],
        ])
        t = run_backtest(df, P, initial_cash=10_000).trades.iloc[0]
        self.assertEqual((t["exit_price"], t["reason"]), (95.0, "stop_loss"))

    def test_model_exit_and_max_hold(self):
        flat = [100, 100, 100, 100]
        df = pred([flat + [0.9], flat + [0.5], flat + [0.3], flat + [0.5], flat + [0.5]])
        t = run_backtest(df, P, initial_cash=10_000).trades.iloc[0]
        self.assertEqual(t["reason"], "model_exit")
        self.assertEqual(t["exit_date"], df["date"].iloc[3])   # decided at close of day2, filled day3 open

        df = pred([flat + [0.9]] + [flat + [0.5]] * 6)
        t = run_backtest(df, P, initial_cash=10_000).trades.iloc[0]
        self.assertEqual(t["reason"], "max_hold")
        self.assertEqual(t["days_held"], P.max_holding_days)

    def test_slippage_costs_both_sides(self):
        flat = [100, 100, 100, 100]
        df = pred([flat + [0.9], flat + [0.3], flat + [0.5], flat + [0.5]])
        from dataclasses import replace
        bt = run_backtest(df, replace(P, slippage_bps=10), initial_cash=10_000)
        t = bt.trades.iloc[0]
        self.assertAlmostEqual(t["entry_price"], 100.1)
        self.assertAlmostEqual(t["exit_price"], 99.9)
        self.assertLess(bt.metrics["final_equity"], 10_000)

    def test_no_signal_means_no_trades_and_flat_equity(self):
        df = pred([[100 + i, 101 + i, 99 + i, 100 + i, 0.5] for i in range(10)])
        bt = run_backtest(df, P, initial_cash=10_000)
        self.assertEqual(bt.metrics["trades"], 0)
        self.assertTrue(np.allclose(bt.equity["equity"], 10_000))

    def test_last_day_signal_is_not_traded(self):
        df = pred([[100, 100, 100, 100, 0.5]] * 3 + [[100, 100, 100, 100, 0.99]])
        self.assertEqual(len(run_backtest(df, P).trades), 0)

    def test_market_filter_blocks_entries(self):
        df = pred([[100, 100, 100, 100, 0.9]] * 4)
        df["market_ok"] = False
        self.assertEqual(len(run_backtest(df, P).trades), 0)

    def test_benchmark_and_metrics(self):
        df = pred([[100, 100, 100, 100, 0.5]] * 3)
        bench = pd.DataFrame({"date": df["date"], "close": [400.0, 440.0, 420.0]})
        bt = run_backtest(df, P, initial_cash=1_000, benchmark=bench)
        self.assertEqual(bt.equity["benchmark"].round(2).tolist(), [1000.0, 1100.0, 1050.0])
        self.assertAlmostEqual(bt.metrics["benchmark_return"], 0.05)
        self.assertAlmostEqual(bt.metrics["excess_return"], -0.05)
        self.assertIsInstance(bt.to_dict()["equity"][0]["date"], str)

    def test_perf_stats_drawdown(self):
        s = perf_stats(pd.Series([100, 120, 90, 110, 130]))
        self.assertAlmostEqual(s["max_drawdown"], 90 / 120 - 1)
        self.assertAlmostEqual(s["total_return"], 0.3)


if __name__ == "__main__":
    unittest.main()
