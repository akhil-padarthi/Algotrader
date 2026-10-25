"""Live trading cycle against the stand-in Alpaca server, with a stub model for exact control."""
import threading
import unittest
from datetime import datetime, timedelta
from unittest import mock
from zoneinfo import ZoneInfo

import pandas as pd

from algotrader.alpaca_client import AlpacaError
from algotrader.journal import Journal
from algotrader.trader import Trader, next_run_time
from tests.helpers import FakeBrokerTest, StubModel


class TraderTests(FakeBrokerTest):
    def journal_target(self):
        return self.tmp / "j.db"

    def make(self, probas, **strategy):
        if strategy:
            self.with_strategy(**strategy)
        self.model = StubModel(probas)
        t = Trader(self.settings, self.client, Journal(self.journal_target()), model=self.model)
        t.close_retry_delays = (0.01,)
        return t

    def test_entry_submits_bracket_orders_and_they_fill_next_open(self):
        t = self.make({"AAA": 0.9, "BBB": 0.8, "CCC": 0.3}, use_market_filter=False)
        rep = t.run_cycle()
        self.assertEqual(rep["status"], "ok", rep.get("message"))
        buys = [o for o in rep["orders"] if o["side"] == "buy"]
        self.assertEqual(sorted(o["symbol"] for o in buys), ["AAA", "BBB"])
        for o in buys:
            self.assertEqual(o["status"], "accepted")
            self.assertTrue(o["client_order_id"].startswith(f"algo-{rep['as_of'].replace('-', '')}-"))
            self.assertLess(o["stop_price"], o["price"])
            self.assertGreater(o["take_profit_price"], o["price"])
        self.fake.advance()
        self.assertEqual(set(self.fake.positions), {"AAA", "BBB"})
        legs = [leg for o in self.fake.orders for leg in (o["legs"] or [])]
        self.assertEqual(sorted(leg["status"] for leg in legs), ["held", "held", "new", "new"])

    def test_rerun_same_day_does_not_double_buy(self):
        t = self.make({"AAA": 0.9}, use_market_filter=False)
        t.run_cycle()
        rep = t.run_cycle()  # same session, entry still working
        self.assertEqual(rep["orders"], [])
        self.assertEqual(sum(1 for o in self.fake.orders if o["side"] == "buy"), 1)
        # Even if the bot forgets about the working order, Alpaca rejects the duplicate id
        with mock.patch.object(Trader, "_blocked_symbols", return_value=set()):
            rep = t.run_cycle()
        self.assertEqual([o["status"] for o in rep["orders"]], ["duplicate"])

    def test_exit_cancels_bracket_legs_then_closes(self):
        t = self.make({"AAA": 0.9}, use_market_filter=False)
        t.run_cycle()
        self.fake.advance()
        self.model.probas["AAA"] = 0.2
        rep = t.run_cycle()
        sell = [o for o in rep["orders"] if o["side"] == "sell"]
        self.assertEqual(len(sell), 1)
        self.assertEqual(sell[0]["reason"], "model_exit")
        legs = [leg for o in self.fake.orders for leg in (o["legs"] or [])]
        self.assertTrue(all(leg["status"] == "canceled" for leg in legs))
        self.fake.advance()
        self.assertNotIn("AAA", self.fake.positions)

    def test_max_hold_uses_fill_date_from_order_history(self):
        t = self.make({"AAA": 0.9}, use_market_filter=False, max_holding_days=3, take_profit_pct=4.0,
                      stop_loss_pct=0.9)
        t.run_cycle()
        reasons = []
        for _ in range(4):
            self.fake.advance()
            rep = t.run_cycle()
            reasons += [o["reason"] for o in rep["orders"] if o["side"] == "sell"]
            if reasons:
                break
        self.assertEqual(reasons, ["max_hold"])
        held = next(s for s in rep["signals"] if s["symbol"] == "AAA")
        self.assertIn("held 3 sessions", held["reason"])

    def test_dry_run_places_nothing_but_journals(self):
        t = self.make({"AAA": 0.9}, use_market_filter=False)
        rep = t.run_cycle(dry_run=True)
        self.assertEqual([o["status"] for o in rep["orders"]], ["dry_run"])
        self.assertEqual(self.fake.orders, [])
        self.assertEqual(t.journal.latest_signals()["signals"][0]["symbol"], "AAA")
        self.assertTrue(t.journal.recent_runs()[0]["dry_run"])

    def test_market_filter_and_daily_loss_guard(self):
        t = self.make({"AAA": 0.9}, use_market_filter=True)
        # force market_ok False through the data: make the benchmark crash 20% today
        spy = self.fake.bars["symbol"] == "SPY"
        today = self.fake.bars["date"] == self.fake.today
        self.fake.bars.loc[spy & today, ["open", "high", "low", "close"]] *= 0.8
        self.fake.by_date = {d: g.set_index("symbol") for d, g in self.fake.bars.groupby("date")}
        rep = t.run_cycle()
        self.assertFalse(rep["market_ok"])
        self.assertEqual(rep["orders"], [])

        t2 = self.make({"AAA": 0.9}, use_market_filter=False, max_daily_loss_pct=0.01)
        self.fake.last_equity = 110_000  # today looks like a -9% day
        rep = t2.run_cycle()
        self.assertEqual(rep["orders"], [])
        self.assertEqual(next(s for s in rep["signals"] if s["symbol"] == "AAA")["action"], "SKIP")

    def test_skips_non_trading_days(self):
        t = self.make({"AAA": 0.9})
        with mock.patch.object(self.client, "get_calendar", return_value=[]):
            rep = t.run_cycle()
        self.assertEqual(rep["status"], "skipped")
        self.assertEqual(self.fake.orders, [])

    def test_refuses_stale_data(self):
        t = self.make({"AAA": 0.9}, use_market_filter=False)
        fake_now = (self.fake.today + pd.Timedelta(days=12)).strftime("%Y-%m-%dT16:30:00-04:00")
        with mock.patch.object(self.client, "get_clock", return_value={"timestamp": fake_now, "is_open": False}):
            rep = t.run_cycle(force=True)
        self.assertEqual(rep["status"], "error")
        self.assertIn("days old", rep["message"])

    def test_close_retries_while_legs_release_shares(self):
        t = self.make({})
        calls = {"n": 0}

        def flaky_close(symbol):
            calls["n"] += 1
            if calls["n"] < 2:
                raise AlpacaError(403, "insufficient qty available for order (requested: 5, available: 0)")
            return {"id": "ok", "status": "accepted"}

        with mock.patch.object(self.client, "close_position", side_effect=flaky_close):
            self.assertEqual(t._close_with_retry("AAA")["id"], "ok")
        self.assertEqual(calls["n"], 2)

    def test_errors_are_journaled_not_raised(self):
        t = self.make({"AAA": 0.9})
        with mock.patch.object(self.client, "get_account", side_effect=AlpacaError(500, "boom")):
            rep = t.run_cycle()
        self.assertEqual(rep["status"], "error")
        self.assertEqual(t.journal.recent_runs()[0]["status"], "error")

    def test_scheduler_runs_cycle_on_time_and_stops_cleanly(self):
        t = self.make({"AAA": 0.9}, use_market_filter=False)
        stop = threading.Event()
        reports = []
        real_cycle = t.run_cycle

        def cycle(*a, **k):
            reports.append(real_cycle(*a, **k))
            stop.set()
            return reports[-1]

        t.run_cycle = cycle
        t.reload_model = lambda: t.model  # the stub isn't on disk
        soon = lambda self, now=None: datetime.now(t.tz) + timedelta(seconds=0.3)  # noqa: E731
        with mock.patch.object(Trader, "next_run_time", soon):
            worker = threading.Thread(target=t.run_forever, kwargs={"stop_event": stop})
            worker.start()
            worker.join(15)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["status"], "ok")
        self.assertEqual(len(self.fake.orders), 1)

    def test_nanosecond_clock_runs_without_warnings(self):
        """Real Alpaca clock timestamps carry nanoseconds; the cycle must handle them silently."""
        import warnings
        t = self.make({"AAA": 0.9}, use_market_filter=False)
        self.assertRegex(self.client.get_clock()["timestamp"], r"\.\d{9}[+-]")
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            rep = t.run_cycle(dry_run=True)
        self.assertEqual(rep["status"], "ok", rep.get("message"))

    def test_next_run_time_skips_weekends(self):
        ny = ZoneInfo("America/New_York")
        fri_evening = datetime(2026, 10, 9, 18, 0, tzinfo=ny)
        self.assertEqual(next_run_time("16:20", ny, fri_evening), datetime(2026, 10, 12, 16, 20, tzinfo=ny))
        mon_morning = datetime(2026, 10, 12, 9, 0, tzinfo=ny)
        self.assertEqual(next_run_time("16:20", ny, mon_morning), datetime(2026, 10, 12, 16, 20, tzinfo=ny))


if __name__ == "__main__":
    unittest.main()
