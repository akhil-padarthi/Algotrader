"""Full-stack browser test: demo broker + bot + API + built React app in headless Chromium.

Skipped automatically when Playwright/Chromium or the built frontend is missing.
Install: pip install playwright && playwright install chromium; cd frontend && npm run build
"""
import re
import unittest

from algotrader.api import create_app
from algotrader.config import Settings
from algotrader.demo import build_demo
from algotrader.fake_alpaca import ServerThread
from algotrader.journal import Journal
from tests.helpers import FAST_MODEL, TempDirTest

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

SYMS = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META"]
PW = "correct horse battery"


class BrowserEndToEnd(TempDirTest):
    @classmethod
    def setUpClass(cls):
        dist = Settings().frontend_dist
        if sync_playwright is None:
            raise unittest.SkipTest("playwright not installed")
        if not (dist / "index.html").exists():
            raise unittest.SkipTest("frontend not built (cd frontend && npm run build)")
        cls.dist = dist

    def setUp(self):
        super().setUp()
        base = Settings(symbols=SYMS, data_dir=self.tmp, model_dir=self.tmp / "m", model=FAST_MODEL,
                        frontend_dist=self.dist)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            self.d = build_demo(base, sessions_to_simulate=15, history_sessions=500)
        self.demo, self.client, self.fake = self.d.settings, self.d.client, self.d.fake
        self.app_server = ServerThread(create_app(self.demo, client=self.client, journal=Journal(self.demo.db_path),
                                                  fred=self.d.fred, demo_accounts=self.d.new_account,
                                                  user_broker=self.d.user_client))
        self.app_server.start()

    def tearDown(self):
        self.app_server.stop()
        self.d.stop()
        super().tearDown()

    def test_dashboard_renders_live_data(self):
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.launch()
        except Exception as exc:  # no browser binary
            self.skipTest(f"chromium unavailable: {exc}")
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            errors, perf_requests = [], []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
            page.on("request", lambda r: perf_requests.append(r.url) if "/api/performance" in r.url else None)
            page.route("**/fonts.g*/**", lambda r: r.abort())  # sandbox has no internet

            page.goto(self.app_server.url, wait_until="load")
            # first visit: create the first account, which becomes the admin and sees the bot's account
            page.wait_for_selector("#auth-title", timeout=15000)
            self.assertIn("becomes the admin", page.inner_text(".auth-card"))
            page.fill("#auth-name", "Owner Person")
            page.fill("#auth-email", "owner@example.com")
            page.fill("#auth-password", PW)
            page.click(".auth-form button[type=submit]")
            page.get_by_text("Account value").wait_for(timeout=15000)
            self.assertEqual(page.inner_text(".account-button .avatar"), "OP")
            page.wait_for_function("document.querySelector('.equity-figure').textContent.includes('$')", timeout=15000)

            equity_text = page.locator(".equity-figure").inner_text()
            shown = float(equity_text.replace("$", "").replace(",", ""))
            self.assertAlmostEqual(shown, self.fake.equity(), delta=self.fake.equity() * 0.01)

            page.locator("#positions").wait_for()
            page.wait_for_function("document.querySelectorAll('.signals li').length > 0", timeout=15000)
            pos_rows = page.locator("section[aria-labelledby=positions] tbody tr").count()
            if self.fake.positions:
                self.assertEqual(pos_rows, len(self.fake.positions))
            else:
                self.assertIn("No open positions", page.locator("section[aria-labelledby=positions]").inner_text())
            self.assertEqual(page.locator(".signals li").count(), len(SYMS))
            page.wait_for_function("document.querySelectorAll('table.market tbody tr').length > 0", timeout=15000)
            self.assertEqual(page.locator("table.market tbody tr").count(), len(SYMS) + 1)  # + benchmark
            page.wait_for_function("document.querySelector('.equity-chart path.line.portfolio')?.getAttribute('d')?.length > 20")
            self.assertIn("ROC AUC", page.locator("section[aria-labelledby=model]").inner_text())
            self.assertIn("This is the demo", page.locator(".banner").first.inner_text())
            self.assertEqual(page.locator(".banner.problem").count(), 0)   # server and dashboard versions match

            # market today: FRED indicators with their source, the S&P trend and all 11 sectors
            page.wait_for_function("document.querySelectorAll('.indicator').length === 4", timeout=15000)
            today = page.locator("section[aria-labelledby=today]")
            self.assertRegex(today.locator(".mt-summary").inner_text(), r"^Markets are \w+ \(VIX \d+\.\d\)")
            self.assertEqual(today.locator("a.indicator-source[href^='https://fred.stlouisfed.org/series/']").count(), 4)
            self.assertIn("50-day average", today.locator(".trend-line").inner_text())
            self.assertEqual(today.locator("table.sectors tbody tr").count(), 11)
            # news on the bot's stocks
            page.wait_for_function("document.querySelectorAll('#news ~ * .news-item, section[aria-labelledby=news] .news-item').length > 0",
                                   timeout=15000)
            self.assertRegex(page.locator("section[aria-labelledby=news] .news-summary").inner_text(),
                             r"headlines? in the last 3 days|No headlines")

            # live stream connected
            page.wait_for_function("document.querySelector('.live-status').classList.contains('live')", timeout=15000)

            # every chart period loads its own data: a different window, sentence and axis
            hero = page.locator(".hero")
            self.assertEqual(hero.locator(".period-switch [aria-pressed=true]").inner_text(), "1M")   # default
            seen = {}
            for label, param, axis in (("1D", "1D", r"(AM|PM)"), ("1W", "1W", r"[A-Z][a-z]{2} \d"),
                                       ("3M", "3M", r"[A-Z][a-z]{2} \d"), ("6M", "6M", r"[A-Z][a-z]{2} \d"),
                                       ("1Y", "1A", r"[A-Z][a-z]{2} 20\d\d"), ("1M", "1M", r"[A-Z][a-z]{2} \d")):
                before = len(perf_requests)
                hero.locator(".period-switch button", has_text=label).click()
                page.wait_for_function(
                    f"document.querySelector('.hero .period-switch [aria-pressed=true]').textContent === '{label}'")
                page.wait_for_function("document.querySelector('.hero .equity-chart path.line.bench')?.getAttribute('d')?.length > 20",
                                       timeout=15000)
                page.wait_for_timeout(400)
                self.assertTrue(any(f"period={param}" in u for u in perf_requests[before:]), label)
                ticks = " ".join(hero.locator(".equity-chart text.axis").all_text_contents())
                self.assertRegex(ticks, axis, label)
                seen[label] = (hero.locator(".hero-sub").inner_text(), hero.locator("path.line.bench").get_attribute("d"))
            self.assertEqual(len({d for _, d in seen.values()}), len(seen))        # six different charts
            self.assertIn("today", seen["1D"][0])
            self.assertNotEqual(seen["1Y"][0], seen["1M"][0])

            # light by default; the toggle switches to dark and is remembered
            self.assertIsNone(page.get_attribute("html", "data-theme"))
            page.click(".theme-toggle")
            self.assertEqual(page.get_attribute("html", "data-theme"), "dark")
            page.reload(wait_until="load")
            page.wait_for_selector(".theme-toggle")
            self.assertEqual(page.get_attribute("html", "data-theme"), "dark")
            page.click(".theme-toggle")
            self.assertNotEqual(page.get_attribute("html", "data-theme"), "dark")
            page.wait_for_function("document.querySelector('.live-status').classList.contains('live')", timeout=15000)

            # scrolling ticker + ticking market clock
            self.assertEqual(page.eval_on_selector(".ticker-track", "el => getComputedStyle(el).animationName"), "ticker-scroll")
            first = page.inner_text(".market-clock")
            page.wait_for_timeout(1100)
            self.assertRegex(first, r"^Market (open|closed), (opens|closes) in")
            if not re.search(r"\d+d \d+h", first):        # more than a day away it shows days and hours only
                self.assertNotEqual(first, page.inner_text(".market-clock"))

            # search -> stock panel -> buy $250 -> confirmation; the stand-in broker receives the order
            page.click("input[role=combobox]")
            page.keyboard.type("micro")
            page.wait_for_selector("li[role=option]")
            page.keyboard.press("Enter")
            page.wait_for_selector(".sheet .price-chart path.line.price", timeout=15000)
            self.assertEqual(page.inner_text(".sheet-symbol"), "MSFT")
            page.fill("#trade-amount", "250")
            page.click("text=Review order")
            page.click(".button.buy")
            page.wait_for_selector(".trade-done", timeout=10000)
            self.assertIn("$250.00 of MSFT", page.inner_text(".trade-done"))
            self.assertEqual(self.fake.orders[-1]["notional"], "250.00")
            for rng in ("1W", "1M", "3M", "1Y", "1D"):
                page.click(f".sheet .period-switch button >> text={rng}")
                page.wait_for_function(
                    f"document.querySelector('.sheet .period-switch [aria-pressed=true]').textContent === '{rng}'")
            page.wait_for_selector(".sheet .price-chart path.line.price")
            sheet = page.locator(".sheet").inner_text()
            self.assertIn("Sector", sheet)
            self.assertIn("Technology", sheet)
            self.assertIn("From Benzinga via Alpaca", sheet)
            page.keyboard.press("Escape")
            page.wait_for_selector(".sheet", state="detached")

            # the bug from the field: dashboard updated but the old server still running. The chart must
            # say why and recover with "Try again" once the server is restarted, never spin forever.
            n_errors = len(errors)
            page.route("**/api/stock/**", lambda r: r.fulfill(status=404, content_type="application/json",
                                                               body='{"error": "not found"}'))
            page.locator("table.market .sym-link").nth(1).click()
            page.wait_for_selector(".sheet .chart-problem", timeout=15000)
            problem = page.locator(".sheet .chart-problem").inner_text()
            self.assertIn("older code", problem)
            self.assertIn("python -m algotrader serve", problem)
            self.assertNotIn("Loading chart", page.locator(".sheet").inner_text())
            page.unroute("**/api/stock/**")
            page.click(".sheet .chart-problem button")
            page.wait_for_selector(".sheet .price-chart path.line.price", timeout=15000)
            errors[n_errors:] = [e for e in errors[n_errors:] if "status of 404" not in e]   # the simulated outage
            page.keyboard.press("Escape")
            page.wait_for_selector(".sheet", state="detached")

            # every list opens the same panel: a model-signal row, a market-watch symbol, the ticker
            page.locator(".sig-row").first.click()
            page.wait_for_selector(".sheet .sheet-symbol")
            page.click(".sheet .icon-button")
            page.wait_for_selector(".sheet", state="detached")
            page.locator("table.market .sym-link").first.click()
            page.wait_for_selector(".sheet .sheet-symbol")
            page.keyboard.press("Escape")
            # the strip keeps moving, so pick an item that is on screen right now
            visible = page.evaluate("""() => {
                const box = document.querySelector('.ticker').getBoundingClientRect();
                return [...document.querySelectorAll('.ticker-copy button')].findIndex(b => {
                    const r = b.getBoundingClientRect();
                    return r.left > box.left + 40 && r.right < box.right - 40;
                });
            }""")
            self.assertGreaterEqual(visible, 0)
            tick = page.locator(".ticker-copy button").nth(visible)
            tick.hover(force=True)   # hovering pauses the ticker
            self.assertEqual(page.eval_on_selector(".ticker-track", "el => getComputedStyle(el).animationPlayState"), "paused")
            tick.click()
            page.wait_for_selector(".sheet .sheet-symbol")
            page.keyboard.press("Escape")

            # a second person: sign up, connect their own (demo) Alpaca account, buy, log out and back in
            bot_orders = len(self.fake.orders)
            mctx = browser.new_context(viewport={"width": 1280, "height": 900})
            m = mctx.new_page()
            m.on("pageerror", lambda e: errors.append(str(e)))
            m.goto(self.app_server.url, wait_until="load")
            m.wait_for_selector("#auth-title")
            self.assertEqual(m.inner_text("#auth-title"), "Log in")
            m.click("text=Create an account")
            m.fill("#auth-name", "Ana Ruiz")
            m.fill("#auth-email", "ana@example.com")
            m.fill("#auth-password", PW)
            m.click(".auth-form button[type=submit]")
            m.wait_for_selector("#alpaca-key", timeout=15000)       # next step: connect Alpaca
            m.click("text=Use a demo account")
            m.wait_for_function("document.querySelector('.equity-figure')?.textContent.includes('100,000')", timeout=20000)
            self.assertEqual(m.locator("section[aria-labelledby=bot]").count(), 0)   # the bot's account is private
            self.assertIn("You don't own any stocks yet", m.locator("section[aria-labelledby=positions]").inner_text())
            m.wait_for_function("document.querySelectorAll('.signals li').length > 0", timeout=15000)
            self.assertNotIn("held", m.locator(".signals").inner_text())
            m.locator("table.market .sym-link").first.click()
            m.wait_for_selector(".sheet .price-chart path.line.price", timeout=15000)
            m.fill("#trade-amount", "300")
            m.click("text=Review order")
            m.click(".button.buy")
            m.wait_for_selector(".trade-done", timeout=10000)
            member_broker = self.d.accounts[-1][0]
            self.assertEqual(member_broker.orders[-1]["notional"], "300.00")    # went to Ana's own account
            self.assertEqual(len(self.fake.orders), bot_orders)                  # not the bot's
            m.keyboard.press("Escape")
            m.wait_for_selector(".sheet", state="detached")
            m.click(".account-button")
            m.wait_for_selector(".trade-history li", timeout=10000)
            self.assertIn("Bought", m.inner_text(".trade-history"))
            m.click("text=Log out")
            m.wait_for_selector("#auth-title")
            m.fill("#auth-email", "ana@example.com")
            m.fill("#auth-password", PW)
            m.click(".auth-form button[type=submit]")
            m.wait_for_function("document.querySelector('.equity-figure')?.textContent.includes('$')", timeout=15000)
            self.assertEqual(m.inner_text(".account-button .avatar"), "AR")
            mctx.close()

            # phone layout: no sideways page scroll
            page.set_viewport_size({"width": 375, "height": 800})
            page.wait_for_timeout(300)
            overflow = page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            self.assertLessEqual(overflow, 0)
            page.locator(".sig-row").first.click()      # stock panel fits a phone screen too
            page.wait_for_selector(".sheet .price-chart path.line.price", timeout=15000)
            fits = page.evaluate("(() => { const s = document.querySelector('.sheet'); return s.scrollWidth - s.clientWidth; })()")
            self.assertLessEqual(fits, 0)

            self.assertEqual([e for e in errors if "fonts.g" not in e and "net::ERR" not in e], [])
        finally:
            browser.close()
            pw.stop()


if __name__ == "__main__":
    unittest.main()
