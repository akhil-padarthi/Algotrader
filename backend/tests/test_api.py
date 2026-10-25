"""Dashboard API against the stand-in broker after a few days of real bot activity."""
import base64
import json
from dataclasses import replace

from algotrader.alpaca_client import AlpacaClient
from algotrader.api import create_app
from algotrader.journal import Journal
from algotrader.pipeline import train_from_bars
from algotrader.trader import Trader
from tests.helpers import FakeBrokerTest, StubModel


class ApiTests(FakeBrokerTest):
    def setUp(self):
        super().setUp()
        self.with_strategy(use_market_filter=False)
        self.journal = Journal(self.settings.db_path)
        self.settings = replace(self.settings, frontend_dist=self.tmp / "dist")
        trader = Trader(self.settings, self.client, self.journal, model=StubModel({"AAA": 0.9, "BBB": 0.85}))
        for _ in range(3):
            trader.run_cycle()
            self.fake.advance()
        trader.run_cycle()
        self.app = create_app(self.settings, client=self.client, journal=self.journal)
        self.http = self.app.test_client()

    def get(self, path, **kw):
        r = self.http.get(path, **kw)
        return r, (r.get_json(silent=True) if r.is_json else None)

    def test_health_and_config(self):
        r, body = self.get("/api/health")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(body["alpaca_configured"])
        self.assertFalse(body["model_trained"])
        _, cfg = self.get("/api/config")
        self.assertEqual(cfg["symbols"], self.settings.symbols)
        self.assertIn("entry_threshold", cfg["strategy"])

    def test_account_and_positions_match_broker(self):
        _, acct = self.get("/api/account")
        self.assertAlmostEqual(acct["equity"], self.fake.equity(), places=1)
        self.assertAlmostEqual(acct["day_pl"], self.fake.equity() - self.fake.last_equity, places=1)
        _, pos = self.get("/api/positions")
        self.assertEqual({p["symbol"] for p in pos}, set(self.fake.positions))
        for p in pos:
            self.assertLess(p["stop_price"], p["avg_entry_price"])
            self.assertGreater(p["take_profit_price"], p["avg_entry_price"])
            self.assertFalse(p["pending_exit"])

    def test_orders_flag_bot_orders(self):
        _, orders = self.get("/api/orders")
        self.assertTrue(orders)
        self.assertTrue(all(o["bot"] for o in orders))
        self.assertEqual(orders[0]["legs"][0]["type"] in ("limit", "stop"), True)

    def test_performance_endpoint(self):
        r, perf = self.get("/api/performance?period=3M")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(perf["points"][0]["benchmark_pct"], 0.0)
        self.assertTrue(perf["partial"])          # the account is 4 sessions old, the window 3 months
        self.assertAlmostEqual(perf["points"][-1]["equity"], round(self.fake.equity_history[-1][1], 2), places=1)
        self.assertIn("excess_return", perf["stats"])
        self.assertEqual(self.get("/api/performance?period=1Y")[1]["period"], "1A")   # 1Y and 1A both work
        r, _ = self.get("/api/performance?period=bogus")
        self.assertEqual(r.status_code, 400)

    def test_market_includes_signals_and_sparklines(self):
        _, m = self.get("/api/market")
        by = {i["symbol"]: i for i in m["items"]}
        self.assertEqual(set(by), set(self.settings.all_symbols))
        self.assertTrue(by["SPY"]["is_benchmark"])
        self.assertEqual(len(by["AAA"]["sparkline"]), 30)
        self.assertAlmostEqual(by["AAA"]["signal"]["proba"], 0.9)

    def test_signals_bot_and_missing_model(self):
        _, s = self.get("/api/signals")
        self.assertEqual(s["entry_threshold"], self.settings.strategy.entry_threshold)
        self.assertEqual(s["signals"][0]["symbol"], "AAA")
        _, bot = self.get("/api/bot")
        self.assertEqual(len(bot["runs"]), 4)
        r, _ = self.get("/api/model")
        self.assertEqual(r.status_code, 404)
        r, _ = self.get("/api/backtest")
        self.assertEqual(r.status_code, 404)

    def test_model_and_backtest_after_training(self):
        hist = self.bars[self.bars["date"] <= self.fake.today]
        train_from_bars(self.settings, hist)
        _, m = self.get("/api/model")
        self.assertTrue(m["trained"])
        self.assertEqual(len(m["feature_importance"]), len(m["features"]))
        r = self.http.get("/api/backtest")
        bt = json.loads(r.get_data(as_text=True))
        self.assertIn("total_return", bt["metrics"])
        self.assertGreater(len(bt["equity"]), 10)

    def test_stream_emits_snapshot(self):
        r = self.http.get("/api/stream?once=1")
        self.assertEqual(r.mimetype, "text/event-stream")
        text = r.get_data(as_text=True)
        payload = json.loads(next(line[6:] for line in text.splitlines() if line.startswith("data: ")))
        self.assertEqual(payload["type"], "snapshot")
        self.assertIn("AAA", payload["quotes"])

    def test_setup_required_without_keys(self):
        app = create_app(replace(self.settings, alpaca_api_key=None, alpaca_secret_key=None), journal=self.journal)
        r = app.test_client().get("/api/account")
        self.assertEqual(r.status_code, 503)
        self.assertTrue(r.get_json()["setup_required"])
        self.assertFalse(app.test_client().get("/api/health").get_json()["alpaca_configured"])

    def test_alpaca_errors_become_502(self):
        bad_client = AlpacaClient("bad", "keys", self.server.url, self.server.url)
        app = create_app(self.settings, client=bad_client, journal=self.journal)
        r = app.test_client().get("/api/account")
        self.assertEqual(r.status_code, 502)
        self.assertIn("error", r.get_json())

    def test_basic_auth(self):
        app = create_app(replace(self.settings, dashboard_password="s3cret"), client=self.client, journal=self.journal)
        c = app.test_client()
        self.assertEqual(c.get("/api/account").status_code, 401)
        self.assertEqual(c.get("/").status_code, 401)
        self.assertEqual(c.get("/api/health").status_code, 200)  # load-balancer health checks stay open
        token = base64.b64encode(b"admin:s3cret").decode()
        self.assertEqual(c.get("/api/account", headers={"Authorization": f"Basic {token}"}).status_code, 200)
        bad = base64.b64encode(b"admin:nope").decode()
        self.assertEqual(c.get("/api/account", headers={"Authorization": f"Basic {bad}"}).status_code, 401)

    def test_cors_only_for_allowed_origins(self):
        app = create_app(replace(self.settings, cors_origins=["https://dash.example.com"]), client=self.client,
                         journal=self.journal)
        c = app.test_client()
        ok = c.get("/api/health", headers={"Origin": "https://dash.example.com"})
        self.assertEqual(ok.headers.get("Access-Control-Allow-Origin"), "https://dash.example.com")
        no = c.get("/api/health", headers={"Origin": "https://evil.example.com"})
        self.assertIsNone(no.headers.get("Access-Control-Allow-Origin"))

    def test_serves_spa_and_blocks_traversal(self):
        r = self.http.get("/")
        self.assertIn(b"Dashboard not built yet", r.data)
        dist = self.tmp / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>INDEX</html>")
        (dist / "assets" / "app.js").write_text("console.log(1)")
        (self.tmp / "secret.txt").write_text("nope")
        self.assertIn(b"INDEX", self.http.get("/").data)
        self.assertIn(b"INDEX", self.http.get("/some/client/route").data)
        self.assertIn(b"console.log", self.http.get("/assets/app.js").data)
        self.assertNotIn(b"nope", self.http.get("/../secret.txt").data)
        self.assertNotIn(b"nope", self.http.get("/%2e%2e/secret.txt").data)
        self.assertEqual(self.http.get("/api/unknown").status_code, 404)
