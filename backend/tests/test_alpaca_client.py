"""AlpacaClient over real HTTP against the stand-in server (+ a misbehaving server for retries)."""
import json
import unittest

from flask import Flask, jsonify, request

from algotrader.alpaca_client import AlpacaClient, AlpacaError, num, round_price
from algotrader.fake_alpaca import ServerThread
from tests.helpers import FakeBrokerTest


class AlpacaClientTests(FakeBrokerTest):
    def test_bars_follow_pagination(self):
        expected = self.bars[self.bars["date"] <= self.fake.today]
        df = self.client.get_bars(self.settings.all_symbols, start="2000-01-01", end=self.fake.now(), page_limit=500)
        self.assertEqual(len(df), len(expected))
        pages = sum(1 for m, p in self.fake.request_log if p == "/v2/stocks/bars")
        self.assertEqual(pages, -(-len(expected) // 500))
        self.assertEqual(str(df["timestamp"].dt.tz), "UTC")
        self.assertListEqual(list(df.columns[:7]), ["symbol", "timestamp", "open", "high", "low", "close", "volume"])

    def test_bad_keys_raise_401(self):
        bad = AlpacaClient("wrong", "keys", self.server.url, self.server.url)
        with self.assertRaises(AlpacaError) as ctx:
            bad.get_account()
        self.assertEqual(ctx.exception.status, 401)

    def test_trading_endpoints_shapes(self):
        acct = self.client.get_account()
        self.assertEqual(num(acct["equity"]), 100_000.0)
        clock = self.client.get_clock()
        self.assertIn("is_open", clock)
        cal = self.client.get_calendar(self.fake.today.date(), self.fake.today.date())
        self.assertEqual(cal[0]["date"], self.fake.today.strftime("%Y-%m-%d"))
        snap = self.client.get_snapshots(["AAA", "SPY"])
        self.assertEqual(set(snap), {"AAA", "SPY"})
        self.assertIn("p", snap["AAA"]["latestTrade"])

    def test_bracket_order_validation_is_enforced(self):
        price = self.fake.price("AAA")
        ok = self.client.submit_order("AAA", "buy", qty=10, time_in_force="gtc", order_class="bracket",
                                      take_profit=price * 1.1, stop_loss=price * 0.95, client_order_id="t-1")
        self.assertEqual(ok["status"], "accepted")
        self.assertEqual(len(ok["legs"]), 2)
        with self.assertRaises(AlpacaError) as ctx:  # duplicate client_order_id
            self.client.submit_order("AAA", "buy", qty=1, time_in_force="gtc", order_class="bracket",
                                     take_profit=price * 1.1, stop_loss=price * 0.95, client_order_id="t-1")
        self.assertEqual(ctx.exception.status, 422)
        with self.assertRaises(AlpacaError):          # stop above the current price
            self.client.submit_order("AAA", "buy", qty=1, time_in_force="gtc", order_class="bracket",
                                     take_profit=price * 1.1, stop_loss=price * 1.01)
        with self.assertRaises(AlpacaError) as ctx:   # way more than the account can afford
            self.client.submit_order("AAA", "buy", qty=10_000_000)
        self.assertEqual(ctx.exception.status, 403)

    def test_submit_requires_exactly_one_of_qty_notional(self):
        with self.assertRaises(ValueError):
            self.client.submit_order("AAA", "buy")
        with self.assertRaises(ValueError):
            self.client.submit_order("AAA", "buy", qty=1, notional=100)

    def test_round_price(self):
        self.assertEqual(round_price(10.12345), 10.12)
        self.assertEqual(round_price(0.123456), 0.1235)
        self.assertEqual(num("12.5"), 12.5)
        self.assertEqual(num(None, 3.0), 3.0)
        self.assertEqual(num("bad", 1.0), 1.0)


class RetryTests(unittest.TestCase):
    def setUp(self):
        app = Flask("flaky")
        self.calls = {"n": 0}
        self.bodies = []

        @app.get("/v2/account")
        def account():
            self.calls["n"] += 1
            if self.calls["n"] < 3:
                return jsonify({"message": "rate limit exceeded"}), 429
            return jsonify({"equity": "1"})

        @app.get("/v2/clock")
        def clock():
            return jsonify({"code": 40010000, "message": "bad request"}), 400

        @app.post("/v2/orders")
        def orders():
            self.bodies.append(request.get_json())
            return jsonify({"id": "x", "status": "accepted"})

        self.server = ServerThread(app)
        self.server.start()
        self.client = AlpacaClient("k", "s", self.server.url, self.server.url)
        self.client.backoff_scale = 0

    def tearDown(self):
        self.server.stop()

    def test_retries_on_429_then_succeeds(self):
        self.assertEqual(self.client.get_account()["equity"], "1")
        self.assertEqual(self.calls["n"], 3)

    def test_4xx_is_not_retried_and_carries_message(self):
        with self.assertRaises(AlpacaError) as ctx:
            self.client.get_clock()
        self.assertEqual((ctx.exception.status, ctx.exception.message), (400, "bad request"))

    def test_order_body_matches_alpaca_schema(self):
        self.client.submit_order("AAPL", "buy", qty=3, time_in_force="gtc", order_class="bracket",
                                 take_profit=110.123, stop_loss=95.456, client_order_id="algo-x")
        self.assertEqual(self.bodies[0], {
            "symbol": "AAPL", "side": "buy", "type": "market", "time_in_force": "gtc", "qty": "3",
            "order_class": "bracket", "take_profit": {"limit_price": "110.12"},
            "stop_loss": {"stop_price": "95.46"}, "client_order_id": "algo-x",
        })
        json.dumps(self.bodies[0])


if __name__ == "__main__":
    unittest.main()
