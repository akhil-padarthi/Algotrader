"""Current-conditions sources: FRED, sector ETFs, news and how the model and bot use them."""
import io
import os
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from unittest import mock

import numpy as np
import pandas as pd

from algotrader.api import create_app
from algotrader.config import Settings, StrategyParams, load_settings
from algotrader.context import (
    FRED_SERIES, MACRO_FEATURES, FredClient, SourceUnavailable, align_macro, article_view, load_macro,
    macro_features, macro_snapshot, news_by_symbol, news_vetoes, sector_map, sector_table, text_sentiment,
)
from algotrader.doctor import run_doctor
from algotrader.fake_alpaca import ServerThread
from algotrader.fake_sources import FakeFred
from algotrader.features import add_labels, build_features, feature_columns
from algotrader.journal import Journal
from algotrader.pipeline import train_from_bars
from algotrader.strategy import Signal, decide
from algotrader.synthetic import make_synthetic_macro, make_synthetic_news
from algotrader.trader import Trader
from tests.helpers import FAST_MODEL, FakeBrokerTest, StubModel, TempDirTest, synthetic


def macro_frame():
    idx = pd.bdate_range("2024-01-01", "2026-10-09")
    rng = np.random.default_rng(1)
    return pd.DataFrame({"VIXCLS": 15 + np.cumsum(rng.normal(0, 0.3, len(idx))).clip(-5, 20),
                         "DGS10": 4 + np.cumsum(rng.normal(0, 0.02, len(idx))),
                         "T10Y2Y": 0.2 + np.cumsum(rng.normal(0, 0.01, len(idx))),
                         "BAA10Y": 1.8 + np.cumsum(rng.normal(0, 0.01, len(idx)))}, index=idx).round(2)


class FredTests(TempDirTest):
    def serve(self, **kw):
        self.ff = FakeFred(self.macro, **kw)
        self.server = ServerThread(self.ff.app)
        self.server.start()
        self.addCleanup(self.server.stop)
        return self.server.url

    def setUp(self):
        super().setUp()
        self.macro = macro_frame()
        self.macro.iloc[5, 1] = np.nan                  # a holiday gap, like real FRED files

    def test_csv_download_both_header_styles(self):
        for old in (False, True):
            url = self.serve(old_style=old)
            s = FredClient(url).series("DGS10", "2024-01-01")
            self.assertEqual(len(s), len(self.macro) - 1)         # "" or "." = no value
            self.assertAlmostEqual(s.iloc[-1], self.macro["DGS10"].iloc[-1])
            self.assertTrue(any("cosd=2024-01-01" in r for r in self.ff.requests))

    def test_official_api_with_key(self):
        url = self.serve()
        s = FredClient(api_key="fred-test-key", api_url=url).series("VIXCLS", "2026-01-01")
        self.assertEqual(s.index.min(), pd.Timestamp("2026-01-01"))
        with self.assertRaises(SourceUnavailable):
            FredClient(api_key="wrong", api_url=url).series("VIXCLS", "2026-01-01")

    def test_missing_series_and_outage(self):
        url = self.serve()
        with self.assertRaises(SourceUnavailable):
            FredClient(url).series("NOPE", "2026-01-01")
        self.ff.down = True
        with self.assertRaises(SourceUnavailable):
            FredClient(url).series("VIXCLS", "2026-01-01")

    def test_load_macro_caches_and_falls_back(self):
        url = self.serve()
        s = Settings(data_dir=self.tmp, fred_url=url, history_years=1)
        fred = FredClient(url)
        first = load_macro(s, fred, years=1)
        self.assertEqual(set(first.columns), set(FRED_SERIES))
        n = len(self.ff.requests)
        load_macro(s, fred, years=1)                              # fresh cache: no new downloads
        self.assertEqual(len(self.ff.requests), n)
        self.ff.down = True
        cached = load_macro(s, fred, years=1, refresh=True)       # outage: the saved copy is used
        pd.testing.assert_frame_equal(cached, first, check_freq=False)
        (self.tmp / "macro.csv").unlink()
        with self.assertRaises(SourceUnavailable):                # outage and nothing saved
            load_macro(s, fred, years=1, refresh=True)

    def test_features_are_lagged_one_day(self):
        feats = macro_features(self.macro)
        trading = pd.bdate_range("2026-03-02", "2026-03-20")
        aligned = align_macro(feats, trading)
        for d in trading:
            known = feats[feats.index <= d - pd.Timedelta(days=1)].ffill().iloc[-1]
            pd.testing.assert_series_equal(aligned.loc[d], known, check_names=False)
        self.assertEqual(list(aligned.columns), MACRO_FEATURES)

    def test_snapshot_statuses(self):
        m = self.macro.copy()
        m.loc[m.index[-1], "VIXCLS"] = 35
        m.loc[m.index[-1], "T10Y2Y"] = -0.3
        snap = {x["id"]: x for x in macro_snapshot(m)}
        self.assertEqual(snap["VIXCLS"]["status"], "Fearful")
        self.assertEqual(snap["T10Y2Y"]["status"], "Inverted")
        self.assertTrue(all(x["url"].startswith("https://fred.stlouisfed.org/series/") for x in snap.values()))
        self.assertEqual(snap["DGS10"]["date"], "2026-10-09")


class NewsAndSectorTests(unittest.TestCase):
    def test_sentiment(self):
        self.assertGreater(text_sentiment("Apple beats estimates, raises guidance")[0], 0.5)
        self.assertLess(text_sentiment("Exxon misses estimates as shares slide")[0], -0.5)
        self.assertEqual(text_sentiment("Microsoft to present at conference")[0], 0.0)
        score, pos, neg = text_sentiment("Strong growth but analysts raise concerns")
        self.assertEqual((pos, neg), (2, 1))

    def test_article_view_cleans_input(self):
        a = article_view({"id": 1, "headline": " NVIDIA soars ", "summary": "<p>Record <b>revenue</b></p>",
                          "url": "javascript:alert(1)", "source": "benzinga", "created_at": "2026-10-09T12:00:00Z",
                          "symbols": ["NVDA"]})
        self.assertEqual(a["headline"], "NVIDIA soars")
        self.assertEqual(a["summary"], "Record  revenue")
        self.assertIsNone(a["url"])                              # only http(s) links are shown
        self.assertEqual((a["source"], a["tone"]), ("Benzinga", "positive"))

    def test_summary_and_vetoes(self):
        now = pd.Timestamp("2026-10-09T20:00:00Z")
        arts = [article_view(a) for a in make_synthetic_news(["AAA", "BBB"], now, days=10, seed=3)]
        summary = news_by_symbol(arts, ["AAA", "BBB", "CCC"], since=now - pd.Timedelta(days=10))
        self.assertEqual(summary["CCC"]["count"], 0)
        self.assertEqual(summary["CCC"]["tone"], "none")
        total = summary["AAA"]["count"] + summary["BBB"]["count"]
        self.assertEqual(total, len(arts))
        bad = {"XYZ": {"count": 5, "negative": 4, "positive": 0, "score": -0.6, "tone": "negative"},
               "FEW": {"count": 2, "negative": 2, "positive": 0, "score": -1.0, "tone": "negative"}}
        vetoes = news_vetoes(bad, min_articles=3, block_score=-0.4)
        self.assertEqual(list(vetoes), ["XYZ"])                  # too few articles to judge FEW
        self.assertIn("4 of 5 headlines", vetoes["XYZ"])

    def test_veto_blocks_new_buys_only(self):
        p = StrategyParams(use_market_filter=False)
        sigs = {"AAA": Signal("AAA", 0.9, 100.0), "BBB": Signal("BBB", 0.9, 100.0)}
        plan = decide(sigs, {}, 100_000, 100_000, p, vetoes={"AAA": "recent news is mostly negative"})
        self.assertEqual([d.symbol for d in plan.buys], ["BBB"])
        self.assertEqual(plan.actions["AAA"], ("SKIP", "recent news is mostly negative"))

    def test_sector_map_and_table(self):
        m = sector_map(["AAPL", "JPM", "ZZZZ"], "SPY", {"JPM": "XLK"})
        self.assertEqual(m, {"AAPL": "XLK", "JPM": "XLK", "ZZZZ": "SPY"})
        bars = synthetic(80, seed=4, symbols=["XLK", "XLE"])
        table = sector_table(bars)
        self.assertEqual({r["symbol"] for r in table}, {"XLK", "XLE"})
        self.assertEqual([r["change_1m"] for r in table], sorted((r["change_1m"] for r in table), reverse=True))
        xlk = bars[bars["symbol"] == "XLK"].sort_values("date")["close"].to_numpy()
        row = next(r for r in table if r["symbol"] == "XLK")
        self.assertAlmostEqual(row["change_1d"], xlk[-1] / xlk[-2] - 1)

    def test_settings(self):
        s = Settings(symbols=["AAPL", "XOM", "ZZZZ"])
        self.assertEqual(s.sector_etfs, ["XLE", "XLK"])
        self.assertEqual(s.data_symbols, ["AAPL", "XOM", "ZZZZ", "SPY", "XLE", "XLK"])
        self.assertEqual(replace(s, use_market_context=False).data_symbols, ["AAPL", "XOM", "ZZZZ", "SPY"])
        with mock.patch.dict(os.environ, {"SECTORS": "ZZZZ:XLU", "NEWS_CHECK": "false", "FRED_API_KEY": "k"}, clear=True):
            s = load_settings(env_file="/nonexistent")
        self.assertEqual((s.sector_overrides, s.news_check, s.fred_api_key), ({"ZZZZ": "XLU"}, False, "k"))


class ModelWithContextTests(TempDirTest):
    def bars_and_macro(self, plant: bool):
        syms = ["AAA", "BBB", "CCC", "DDD"]
        bars = synthetic(900, seed=7, symbols=syms + ["XLK"])
        macro = make_synthetic_macro(bars, seed=7)
        if plant:
            # VIX dated d-1 carries information about the 5-day return that starts at the next open
            # (legitimately known before the decision on day d) -> market conditions should help
            labels = add_labels(build_features(bars, "SPY", symbols=syms), 5, 0.01)
            avg = labels.groupby("date")["fwd_return"].mean()
            prev = pd.Series(avg.to_numpy(), index=avg.index).shift(-1)      # value for d-1 describes d
            vix = (20 + 400 * prev).clip(9, 80)
            macro.loc[vix.dropna().index, "VIXCLS"] = vix.dropna().round(2).to_numpy()
        return syms, bars, macro

    def test_context_is_used_only_when_it_helps(self):
        for plant, expected in ((True, "context"), (False, "base")):
            syms, bars, macro = self.bars_and_macro(plant)
            s = Settings(symbols=syms, data_dir=self.tmp, model_dir=self.tmp / str(plant), model=FAST_MODEL,
                         sector_overrides={s: "XLK" for s in syms})
            result, _ = train_from_bars(s, bars, macro=macro)
            sel = result.meta["feature_selection"]
            self.assertTrue(sel["offered"])
            self.assertEqual(sel["chosen"], expected, sel)
            uses_macro = any(c in MACRO_FEATURES for c in result.feature_cols)
            self.assertEqual(uses_macro, expected == "context")
            if plant:
                self.assertGreater(sel["context"]["oos_auc"], sel["base"]["oos_auc"] + 0.05)

    def test_choice_rule_needs_a_clear_and_consistent_gain(self):
        from algotrader.pipeline import context_wins

        def meta(folds):
            return {"cv_folds": [{"fold": i, "auc": a} for i, a in enumerate(folds)],
                    "cv_mean": {"auc": sum(folds) / len(folds)}}
        base = meta([0.55, 0.55, 0.55])
        self.assertTrue(context_wins(base, meta([0.57, 0.57, 0.56])))
        self.assertFalse(context_wins(base, meta([0.556, 0.556, 0.556])))   # too small a gain
        self.assertFalse(context_wins(base, meta([0.65, 0.54, 0.54])))      # one lucky fold

    def test_without_fred_data_trains_on_prices(self):
        syms, bars, _ = self.bars_and_macro(False)
        s = Settings(symbols=syms, data_dir=self.tmp, model_dir=self.tmp, model=FAST_MODEL)
        result, _ = train_from_bars(s, bars, macro=None, context_note="FRED couldn't be reached")
        self.assertFalse(any(c in MACRO_FEATURES for c in result.feature_cols))
        self.assertIn("FRED couldn't be reached", result.meta["feature_selection"]["note"])
        frame = build_features(bars, "SPY", symbols=syms, sectors={x: "XLK" for x in syms})
        self.assertEqual(len(feature_columns(frame, context=True)), len(feature_columns(frame)) + 4)


class BotAndApiWithContext(FakeBrokerTest):
    symbols = ["AAPL", "MSFT", "XOM", "XLK", "XLE"]     # the stand-in also prices the two sector ETFs
    signal = 0.0
    sessions = 600

    def setUp(self):
        super().setUp()
        self.settings = replace(self.settings, symbols=["AAPL", "MSFT", "XOM"])
        self.with_strategy(use_market_filter=False)
        self.macro = make_synthetic_macro(self.bars, seed=2)
        self.ff = FakeFred(self.macro)
        self.fred_server = ServerThread(self.ff.app)
        self.fred_server.start()
        self.addCleanup(self.fred_server.stop)
        self.settings = replace(self.settings, fred_url=self.fred_server.url)
        self.fred = FredClient(self.fred_server.url)
        now = self.fake.now()
        self.fake.news = sorted(
            [{"id": i, "headline": h, "summary": "", "source": "benzinga", "author": "", "url": f"https://example.com/{i}",
              "images": [], "symbols": [sym], "created_at": (pd.Timestamp(now) - pd.Timedelta(hours=i + 1)).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
              "updated_at": ""}
             for i, (sym, h) in enumerate([("XOM", "Exxon misses estimates, shares slide"),
                                           ("XOM", "Exxon downgraded to sell on weakness"),
                                           ("XOM", "Exxon cuts guidance as demand falls"),
                                           ("AAPL", "Apple beats estimates on strong demand")])],
            key=lambda a: a["created_at"], reverse=True)

    def test_news_check_skips_new_buys(self):
        bot = Trader(self.settings, self.client, Journal(self.tmp / "j.db"), fred=self.fred,
                     model=StubModel({"AAPL": 0.9, "XOM": 0.9}))
        rep = bot.run_cycle()
        self.assertEqual(rep["status"], "ok", rep.get("message"))
        self.assertEqual(sorted(o["symbol"] for o in rep["orders"]), ["AAPL"])
        xom = next(s for s in rep["signals"] if s["symbol"] == "XOM")
        self.assertEqual(xom["action"], "SKIP")
        self.assertIn("news is mostly negative (3 of 3", xom["reason"])
        bot2 = Trader(replace(self.settings, news_check=False), self.client, Journal(self.tmp / "j2.db"),
                      fred=self.fred, model=StubModel({"XOM": 0.9}))
        self.assertIn("XOM", [o["symbol"] for o in bot2.run_cycle()["orders"]])

    def test_model_with_market_conditions_trades_and_fred_outage_stops_it(self):
        from algotrader.features import MACRO_FEATURES as MF, MARKET_FEATURES, SECTOR_FEATURES, STOCK_FEATURES
        cols = STOCK_FEATURES + MARKET_FEATURES + SECTOR_FEATURES + MF
        model = StubModel({"AAPL": 0.9}, feature_cols=cols)
        model.meta = {"sectors": {"AAPL": "XLK", "MSFT": "XLK", "XOM": "XLE"}}
        bot = Trader(self.settings, self.client, Journal(self.tmp / "j.db"), fred=self.fred, model=model)
        asked = []
        real = self.client.get_bars
        self.client.get_bars = lambda symbols, *a, **kw: asked.append(list(symbols)) or real(symbols, *a, **kw)
        rep = bot.run_cycle(dry_run=True)
        self.assertEqual(rep["status"], "ok", rep.get("message"))
        self.assertEqual([s["symbol"] for s in rep["signals"] if s["action"] == "BUY"], ["AAPL"])
        self.assertTrue(any("/graph/fredgraph.csv" in r for r in self.ff.requests))
        self.assertTrue({"XLK", "XLE"} <= set(asked[0]))              # sector ETFs fetched for the features
        (self.settings.data_dir / "macro.csv").unlink()
        self.ff.down = True
        rep = bot.run_cycle(dry_run=True)
        self.assertEqual(rep["status"], "error")
        self.assertIn("FRED", rep["message"])

    def test_context_and_news_endpoints(self):
        app = create_app(self.settings, client=self.client, journal=Journal(self.tmp / "j.db"), fred=self.fred)
        http = app.test_client()
        c = http.get("/api/context").get_json()
        self.assertEqual(c["problems"], [])
        self.assertEqual({i["id"] for i in c["indicators"]}, set(FRED_SERIES))
        self.assertEqual(c["trend"]["symbol"], "SPY")
        self.assertTrue(c["summary"].endswith("."))
        self.assertTrue(all(src["ok"] for src in c["sources"]))
        n = http.get("/api/news").get_json()
        self.assertEqual(n["by_symbol"]["XOM"]["negative"], 3)
        self.assertEqual(n["articles"][0]["source"], "Benzinga")
        one = http.get("/api/news?symbol=aapl").get_json()
        self.assertEqual([a["symbols"] for a in one["articles"]], [["AAPL"]])
        st = http.get("/api/stock/XOM?range=1D").get_json()
        self.assertEqual(st["news"]["summary"]["tone"], "negative")
        self.assertEqual(st["sector"], {"etf": "XLE", "name": "Energy"})
        self.ff.down = True
        (self.settings.data_dir / "macro.csv").unlink()
        fresh = create_app(self.settings, client=self.client, journal=Journal(self.tmp / "j.db"), fred=self.fred)
        c = fresh.test_client().get("/api/context").get_json()
        self.assertEqual(c["indicators"], [])
        self.assertTrue(c["problems"][0].startswith("FRED"))
        self.assertFalse(c["sources"][0]["ok"])
        self.assertIsNotNone(c["trend"])                          # prices still work without FRED

    def test_doctor(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = run_doctor(self.settings, client=self.client, fred=self.fred, out=out,
                              server_url="http://127.0.0.1:9")
        text = out.getvalue()
        self.assertIn("[ OK ] Alpaca account", text)
        self.assertIn("[ OK ] News (Benzinga via Alpaca)", text)
        self.assertIn("[ OK ] FRED Volatility index (VIX)", text)
        self.assertIn("[FAIL] Trained model", text)              # none trained in this test
        self.assertEqual(code, 1)
        self.ff.down = True
        out = io.StringIO()
        run_doctor(self.settings, client=self.client, fred=self.fred, out=out, server_url="http://127.0.0.1:9")
        self.assertIn("[WARN] FRED", out.getvalue())

    def test_doctor_spots_dashboard_rebuilt_by_an_old_dev_watcher(self):
        # seen on a real update: an "npm run dev" window from the old version rebuilt the dashboard from the
        # new index.html without stamping the version into it
        dist = self.tmp / "dist"
        dist.mkdir()
        (dist / "index.html").write_text('<meta name="algotrader-version" content="__VERSION__" />', encoding="utf-8")
        out = io.StringIO()
        run_doctor(replace(self.settings, frontend_dist=dist), client=self.client, fred=self.fred, out=out,
                   server_url="http://127.0.0.1:9")
        text = out.getvalue()
        self.assertIn("[FAIL] Dashboard files: they were rebuilt by an old copy of the build script", text)
        self.assertIn("npm run dev", text)
        self.assertNotIn("version __VERSION__", text)

    def test_doctor_spots_a_server_running_old_code(self):
        old = ServerThread(_old_health_app())
        old.start()
        self.addCleanup(old.stop)
        out = io.StringIO()
        run_doctor(self.settings, client=self.client, fred=self.fred, out=out, server_url=old.url)
        self.assertIn("is running version 1.0.0", out.getvalue())
        self.assertIn("Ctrl+C", out.getvalue())


def _old_health_app():
    from flask import Flask, jsonify
    app = Flask("old")
    app.get("/api/health")(lambda: jsonify({"status": "ok", "version": "1.0.0"}))
    return app


if __name__ == "__main__":
    unittest.main()
