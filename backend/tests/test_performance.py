"""The dashboard's account-vs-S&P chart: one window and resolution per period button."""
import unittest
from zoneinfo import ZoneInfo

import pandas as pd

from algotrader.alpaca_client import AlpacaError
from algotrader.journal import Journal
from algotrader.performance import (
    PERIOD_SPECS, asof, build_performance, history_series, normalize_period,
)
from algotrader.trader import Trader
from tests.helpers import FakeBrokerTest, StubModel

NY = ZoneInfo("America/New_York")


class SeriesHelpers(unittest.TestCase):
    def test_daily_points_get_the_right_trading_date_in_either_convention(self):
        days = ["2026-10-12", "2026-10-13", "2026-10-14"]
        ny_midnight = [int(pd.Timestamp(d, tz=NY).timestamp()) for d in days]
        utc_midnight = [int(pd.Timestamp(d, tz="UTC").timestamp()) for d in days]   # 8 pm the day before in NY
        for stamps in (ny_midnight, utc_midnight):
            s = history_series({"timestamp": stamps, "equity": [1.0, 2.0, 3.0]}, NY, daily=True)
            self.assertEqual([d.strftime("%Y-%m-%d") for d in s.index], days)

    def test_unfunded_points_are_dropped(self):
        s = history_series({"timestamp": [1, 2, 3, 4], "equity": [0, None, 100.0, 101.0]}, NY)
        self.assertEqual(s.tolist(), [100.0, 101.0])

    def test_asof_never_borrows_from_the_future(self):
        values = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-01-02", "2026-01-05"]))
        idx = pd.to_datetime(["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-06"])
        out = asof(values, idx)
        self.assertTrue(pd.isna(out.iloc[0]))
        self.assertEqual(out.iloc[1:].tolist(), [1.0, 1.0, 2.0])

    def test_period_names(self):
        self.assertEqual(normalize_period("1y"), "1A")
        self.assertEqual(normalize_period("1D"), "1D")
        self.assertIsNone(normalize_period("5Y"))


class PerformanceTests(FakeBrokerTest):
    """Account opened 26 sessions ago in the stand-in broker; the bot has been trading since."""
    sessions = 420
    start_offset = 30
    signal = 0.0

    def setUp(self):
        super().setUp()
        self.with_strategy(use_market_filter=False)
        bot = Trader(self.settings, self.client, Journal(self.tmp / "j.db"), model=StubModel({"AAA": 0.9, "BBB": 0.9}))
        bot.close_retry_delays = (0.01,)
        for _ in range(26):
            bot.run_cycle()
            self.fake.advance()
        self.now = pd.Timestamp(self.client.get_clock()["timestamp"])

    def perf(self, period, client=None):
        return build_performance(client or self.client, period, "SPY", self.now, NY)

    def test_each_period_has_its_own_window_and_resolution(self):
        expected = {"1D": ("5Min", 78), "1W": ("15Min", None), "1M": ("1H", None),
                    "3M": ("1D", None), "6M": ("1D", None), "1A": ("1D", None)}
        windows = {}
        for period, (tf, n) in expected.items():
            r = self.perf(period)
            self.assertEqual(r["timeframe"], tf, period)
            stamps = [pd.Timestamp(p["t"]) for p in r["points"]]
            self.assertEqual(stamps, sorted(stamps))
            if n:
                self.assertEqual(len(stamps), n)                     # one full session, 9:30 to 15:55
            if tf != "1D":
                self.assertTrue(all(570 <= t.hour * 60 + t.minute < 960 for t in stamps if tf != "1H"))
            windows[period] = (stamps[0], len(stamps))
            self.assertEqual(r["points"][0]["benchmark_pct"], 0.0)
        # longer periods reach further back and the buttons really change the chart
        firsts = [windows[p][0] for p in ("1D", "1W", "1M", "3M", "6M", "1A")]
        self.assertEqual(firsts, sorted(firsts, reverse=True))
        self.assertGreater(windows["1A"][1], 240)
        self.assertGreater(windows["6M"][1], 120)

    def test_account_younger_than_the_period(self):
        r = self.perf("1A")
        self.assertTrue(r["partial"])
        pts = r["points"]
        first = next(i for i, p in enumerate(pts) if p["equity"] is not None)
        self.assertGreater(first, 200)                               # nothing before the account existed
        self.assertTrue(all(p["equity_pct"] is None for p in pts[:first]))
        self.assertEqual(r["account_start"][:10], pts[first]["date"])
        # the account line starts on the S&P line, so the gap is relative performance since then
        self.assertAlmostEqual(pts[first]["equity_pct"], pts[first]["benchmark_pct"], places=4)
        s = r["stats"]
        self.assertAlmostEqual(s["return"], pts[-1]["equity"] / pts[first]["equity"] - 1, places=4)
        spy = (1 + pts[-1]["benchmark_pct"]) / (1 + pts[first]["benchmark_pct"]) - 1
        self.assertAlmostEqual(s["benchmark_return"], spy, places=3)  # S&P over the same days
        self.assertAlmostEqual(s["benchmark_period_return"], pts[-1]["benchmark_pct"], places=5)
        self.assertAlmostEqual(s["excess_return"], s["return"] - s["benchmark_return"], places=9)

    def test_account_older_than_the_period(self):
        r = self.perf("1W")
        self.assertFalse(r["partial"])
        self.assertEqual(r["points"][0]["equity_pct"], 0.0)
        self.assertAlmostEqual(r["stats"]["benchmark_return"], r["stats"]["benchmark_period_return"], places=6)
        # last 15-minute window of the session, so close to (not exactly) the closing value
        self.assertAlmostEqual(r["points"][-1]["equity"], self.fake.equity(), delta=self.fake.equity() * 0.01)
        daily = self.perf("3M")
        self.assertAlmostEqual(daily["points"][-1]["equity"], self.fake.equity(), delta=0.02)

    def test_intraday_history_refused_falls_back_to_daily(self):
        real = self.client.get_portfolio_history

        def daily_only(**kw):
            if kw.get("timeframe") != "1D":
                raise AlpacaError(422, "timeframe not available")
            return real(**kw)

        self.client.get_portfolio_history = daily_only
        r = self.perf("1W")
        self.assertEqual(r["timeframe"], "1D")
        self.assertFalse(r["intraday"])
        dates = [p["date"] for p in r["points"]]
        self.assertEqual(len(dates), len(set(dates)))               # one point per day on both lines
        self.assertTrue(all(p["equity"] is not None for p in r["points"]))

    def test_no_account_history_yet(self):
        class Empty:
            def __init__(self, inner):
                self.inner = inner

            def get_portfolio_history(self, **kw):
                return {"timestamp": [], "equity": []}

            def get_bars(self, *a, **kw):
                return self.inner.get_bars(*a, **kw)

        r = self.perf("3M", client=Empty(self.client))
        self.assertIsNone(r["stats"])
        self.assertGreater(len(r["points"]), 50)                    # the S&P line still shows
        self.assertTrue(all(p["equity"] is None for p in r["points"]))

    def test_specs_respect_alpaca_limits(self):
        for period, spec in PERIOD_SPECS.items():
            if spec.history_tf != "1D":
                self.assertLess(spec.days, 30, period)               # intraday history: spans under 30 days


if __name__ == "__main__":
    unittest.main()
