"""Stock search, the stock panel's data, the market clock, and buying/selling from the dashboard."""
import pandas as pd

from algotrader.alpaca_client import AlpacaClient
from algotrader.api import create_app
from algotrader.journal import Journal
from algotrader.trader import Trader
from tests.helpers import FakeBrokerTest, StubModel

HDR = {"X-AlgoTrader": "1"}


class StockApiTests(FakeBrokerTest):
    symbols = ["AAPL", "MSFT", "NVDA"]
    signal = 0.0

    def setUp(self):
        super().setUp()
        self.journal = Journal(self.settings.db_path)
        self.app = create_app(self.settings, client=self.client, journal=self.journal)
        self.http = self.app.test_client()

    def buy(self, symbol="AAPL", amount=500, **kw):
        return self.http.post("/api/trade", json={"symbol": symbol, "side": "buy", "amount": amount},
                              headers={**HDR, **kw.pop("headers", {})}, **kw)

    # ------------------------------------------------------------ read side
    def test_clock(self):
        c = self.http.get("/api/clock").get_json()
        self.assertFalse(c["is_open"])
        self.assertTrue(c["next_open"].startswith(self.fake.dates[self.fake.i + 1].strftime("%Y-%m-%d")))

    def test_search_by_symbol_and_company_name(self):
        q = lambda text: [a["symbol"] for a in self.http.get(f"/api/assets/search?q={text}").get_json()]  # noqa: E731
        self.assertEqual(q("msft"), ["MSFT"])
        self.assertEqual(q("micro"), ["MSFT"])          # company name word
        self.assertEqual(q("NVIDIA")[0], "NVDA")
        self.assertEqual(q("spdr"), ["SPY"])
        self.assertEqual(q(""), [])
        self.assertEqual(q("zzzz"), [])
        first = self.http.get("/api/assets/search?q=AAPL").get_json()[0]
        self.assertEqual((first["name"], first["in_universe"]), ("Apple Inc. Common Stock", True))

    def test_stock_ranges_use_regular_hours_only(self):
        expected = {"1D": ("5Min", 78), "1W": ("30Min", None), "1M": ("1Hour", None),
                    "3M": ("1Day", None), "1Y": ("1Day", None)}
        for rng, (tf, n) in expected.items():
            d = self.http.get(f"/api/stock/AAPL?range={rng}").get_json()
            self.assertEqual(d["timeframe"], tf)
            times = [pd.Timestamp(b["t"]) for b in d["bars"]]
            self.assertTrue(times == sorted(times))
            if tf != "1Day":
                mins = {t.hour * 60 + t.minute for t in times}
                self.assertGreaterEqual(min(mins), 540 if tf == "1Hour" else 570)
                self.assertLess(max(mins), 960)          # nothing from after-hours trading
            if n:
                self.assertEqual(len(times), n)          # one full session: 9:30 to 15:55
                self.assertEqual({t.date() for t in times}, {self.fake.today.date()})

    def test_stock_payload(self):
        d = self.http.get("/api/stock/aapl").get_json()
        self.assertEqual((d["symbol"], d["name"], d["range"]), ("AAPL", "Apple Inc. Common Stock", "1D"))
        self.assertTrue(d["in_universe"] and d["fractionable"])
        q = d["quote"]
        self.assertAlmostEqual(q["price"], self.fake.price("AAPL"))
        self.assertAlmostEqual(q["change_pct"], q["price"] / q["prev_close"] - 1)
        self.assertIsNone(d["position"])
        self.assertEqual(d["trading"], {"enabled": True, "mode": "paper", "note": None, "needs_broker": False})
        self.assertEqual(d["account_kind"], "bot")
        live = self.http.get("/api/stock/AAPL/quote").get_json()
        self.assertEqual(live["symbol"], "AAPL")

    def test_panel_survives_failing_sources(self):
        """One broken data source must not leave the chart stuck on "Loading"."""
        from algotrader.alpaca_client import AlpacaError
        real_bars = self.client.get_bars

        def no_intraday(symbols, start, end=None, timeframe="1Day", **kw):
            if timeframe != "1Day":
                raise AlpacaError(403, "subscription does not permit querying recent SIP data")
            return real_bars(symbols, start, end, timeframe, **kw)

        self.client.get_bars = no_intraday
        d = self.http.get("/api/stock/AAPL?range=1D").get_json()
        self.assertEqual(d["timeframe"], "1Day")                 # daily closes instead of nothing
        self.assertGreater(len(d["bars"]), 20)
        self.assertIn("daily closes", d["chart_note"])
        self.client.get_bars = real_bars

        def boom(*a, **kw):
            raise AlpacaError(500, "snapshot service down")

        self.client.get_snapshots = boom
        self.client.get_assets = boom
        fresh = create_app(self.settings, client=self.client, journal=self.journal).test_client()  # empty caches
        r = fresh.get("/api/stock/MSFT?range=1W")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertGreater(len(d["bars"]), 50)                   # chart still there
        self.assertIsNone(d["quote"])
        self.assertTrue(any("latest price" in p for p in d["problems"]))
        self.assertTrue(any("company details" in p for p in d["problems"]))

        def no_bars(*a, **kw):
            raise AlpacaError(429, "rate limit exceeded")

        self.client.get_bars = no_bars
        self.client.backoff_scale = 0
        d = self.http.get("/api/stock/NVDA?range=3M").get_json()
        self.assertIsNone(d["bars"])
        self.assertIn("rate limit", d["chart_error"])           # the panel can say why

    def test_unexpected_errors_come_back_as_json(self):
        def crash(*a, **kw):
            raise RuntimeError("disk full")

        self.client.get_clock = crash
        r = self.http.get("/api/clock")
        self.assertEqual(r.status_code, 500)
        self.assertIn("RuntimeError: disk full", r.get_json()["error"])
        r = self.http.get("/api/does-not-exist")
        self.assertEqual(r.status_code, 404)
        self.assertTrue(r.is_json)

    def test_stock_errors(self):
        self.assertEqual(self.http.get("/api/stock/A$B").status_code, 400)
        self.assertEqual(self.http.get("/api/stock/AAPL?range=5Y").status_code, 400)
        r = self.http.get("/api/stock/ZZZZ")
        self.assertEqual(r.status_code, 404)
        self.assertIn("No US stock", r.get_json()["error"])

    # ------------------------------------------------------- trade security
    def test_trade_requires_dashboard_headers(self):
        body = {"symbol": "AAPL", "side": "buy", "amount": 500}
        self.assertEqual(self.http.post("/api/trade", json=body).status_code, 403)              # no header
        self.assertEqual(self.http.post("/api/trade", data=body, headers=HDR).status_code, 403)  # HTML form
        evil = self.buy(headers={"Origin": "https://evil.example"})
        self.assertEqual(evil.status_code, 403)
        self.assertEqual(self.fake.orders, [])
        self.assertEqual(self.buy(headers={"Origin": "http://localhost"}).status_code, 200)       # same origin

    def test_trade_can_be_switched_off(self):
        from dataclasses import replace
        app = create_app(replace(self.settings, manual_trading=False), client=self.client, journal=self.journal)
        r = app.test_client().post("/api/trade", json={"symbol": "AAPL", "side": "buy", "amount": 50}, headers=HDR)
        self.assertEqual(r.status_code, 403)
        self.assertIn("MANUAL_TRADING", r.get_json()["error"])

    def test_exposed_dashboard_without_password_cannot_trade(self):
        guarded = self.settings.guard_exposed_dashboard(exposed=True)
        http = create_app(guarded, client=self.client, journal=self.journal).test_client()
        r = http.post("/api/trade", json={"symbol": "AAPL", "side": "buy", "amount": 50}, headers=HDR)
        self.assertEqual(r.status_code, 403)
        self.assertIn("DASHBOARD_PASSWORD", r.get_json()["error"])
        trading = http.get("/api/stock/AAPL").get_json()["trading"]
        self.assertFalse(trading["enabled"])
        self.assertIn("other devices", trading["note"])
        self.assertEqual(self.fake.orders, [])

    # -------------------------------------------------------------- buying
    def test_buy_dollar_amount_buys_fractional_shares(self):
        r = self.buy(amount=500)
        self.assertEqual(r.status_code, 200, r.get_json())
        body = r.get_json()
        self.assertIn("$500.00 of AAPL", body["message"])
        self.assertIn("market is closed", body["message"])
        order = self.fake.orders[-1]
        self.assertEqual((order["notional"], order["time_in_force"], order["type"]), ("500.00", "day", "market"))
        self.assertTrue(order["client_order_id"].startswith("manual-"))
        self.fake.advance()
        pos = self.fake.positions["AAPL"]["qty"]
        self.assertAlmostEqual(pos * self.fake.bar("AAPL")["open"], 500, delta=0.01)
        orders = self.http.get("/api/orders").get_json()
        self.assertTrue(orders[0]["manual"] and not orders[0]["bot"])
        self.assertEqual(orders[0]["notional"], 500.0)
        pos = self.http.get("/api/positions").get_json()[0]
        self.assertEqual((pos["owner"], pos["managed"]), ("you", False))

    def test_buy_whole_shares_when_not_fractionable(self):
        real = self.client.get_assets

        def no_fractions():
            return [{**a, "fractionable": False} for a in real()]

        self.client.get_assets = no_fractions
        price = self.fake.price("AAPL")
        r = self.buy(amount=price * 2.5)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertIn("2 shares of AAPL", r.get_json()["message"])
        self.assertEqual(float(self.fake.orders[-1]["qty"]), 2.0)
        small = self.buy(amount=price / 2).get_json()["error"]
        self.assertIn("can't be bought in fractions", small)

    def test_buy_validation_and_broker_rejections(self):
        self.assertIn("smallest order", self.buy(amount=0.5).get_json()["error"])
        self.assertIn("dollar amount", self.buy(amount="lots").get_json()["error"])
        self.assertIn("limited", self.buy(amount=5_000_000).get_json()["error"])
        self.assertIn("valid stock symbol", self.buy(symbol="<script>").get_json()["error"])
        r = self.buy(amount=900_000)                         # more than the $100k account holds
        self.assertEqual(r.status_code, 400)
        self.assertIn("insufficient buying power", r.get_json()["error"])

    # ------------------------------------------------------------- selling
    def test_sell_all_cancels_bot_protection_then_closes(self):
        bot = Trader(self.settings.with_overrides(strategy=self.settings.strategy.__class__(use_market_filter=False)),
                     self.client, self.journal, model=StubModel({"AAPL": 0.95}))
        bot.close_retry_delays = (0.01,)
        bot.run_cycle()
        self.fake.advance()
        self.assertIn("AAPL", self.fake.positions)
        legs = [leg for o in self.fake.orders for leg in (o["legs"] or [])]
        self.assertTrue(all(leg["status"] in ("new", "held") for leg in legs))
        pos = self.http.get("/api/positions").get_json()[0]
        self.assertEqual((pos["owner"], pos["managed"]), ("bot", True))

        r = self.http.post("/api/trade", json={"symbol": "AAPL", "side": "sell_all"}, headers=HDR)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(all(leg["status"] == "canceled" for leg in legs))
        self.fake.advance()
        self.assertNotIn("AAPL", self.fake.positions)
        sell = self.http.get("/api/orders").get_json()[0]
        self.assertEqual(sell["side"], "sell")
        self.assertTrue(sell["manual"] and not sell["bot"])

    def test_sell_without_position(self):
        r = self.http.post("/api/trade", json={"symbol": "MSFT", "side": "sell_all"}, headers=HDR)
        self.assertEqual(r.status_code, 400)
        self.assertIn("don't own any MSFT", r.get_json()["error"])
        r = self.http.post("/api/trade", json={"symbol": "MSFT", "side": "short"}, headers=HDR)
        self.assertEqual(r.status_code, 400)


class BotLeavesManualPositionsAlone(FakeBrokerTest):
    symbols = ["AAPL", "MSFT", "NVDA"]
    signal = 0.0

    def test_manual_buy_is_never_sold_or_added_to_by_the_bot(self):
        journal = Journal(self.settings.db_path)
        app = create_app(self.settings, client=self.client, journal=journal)
        app.test_client().post("/api/trade", json={"symbol": "AAPL", "side": "buy", "amount": 1000}, headers=HDR)
        self.fake.advance()
        self.assertIn("AAPL", self.fake.positions)

        # model hates AAPL (would trigger a model exit) and loves MSFT
        bot = Trader(self.settings.with_overrides(strategy=self.settings.strategy.__class__(use_market_filter=False)),
                     self.client, journal, model=StubModel({"AAPL": 0.05, "MSFT": 0.95}))
        rep = bot.run_cycle()
        self.assertEqual(rep["status"], "ok", rep.get("message"))
        sides = {(o["symbol"], o["side"]) for o in rep["orders"]}
        self.assertNotIn(("AAPL", "sell"), sides)
        self.assertIn(("MSFT", "buy"), sides)
        aapl = next(s for s in rep["signals"] if s["symbol"] == "AAPL")
        self.assertEqual((aapl["action"], aapl["held"]), ("MANUAL", 1))

        # even when the model loves it, the bot doesn't buy more of the user's stock
        bot._model = StubModel({"AAPL": 0.99})
        rep = bot.run_cycle()
        self.assertNotIn(("AAPL", "buy"), {(o["symbol"], o["side"]) for o in rep["orders"]})

        # sold by hand and later bought by the bot -> the bot manages it again
        app.test_client().post("/api/trade", json={"symbol": "AAPL", "side": "sell_all"}, headers=HDR)
        self.fake.advance()
        bot.run_cycle()
        self.fake.advance()
        self.assertIn("AAPL", self.fake.positions)
        bot._model = StubModel({"AAPL": 0.05})
        rep = bot.run_cycle()
        self.assertIn(("AAPL", "sell"), {(o["symbol"], o["side"]) for o in rep["orders"]})

    def test_client_order_ids_mark_who_placed_an_order(self):
        self.assertIsInstance(self.client, AlpacaClient)
        self.assertEqual(Journal(self.settings.db_path).last_buy("AAPL"), None)
