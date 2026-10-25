"""Running on Vercel: the entry point, the setup check page, the scheduled bot run, and the upload rules."""
import json
import os
import re
import runpy
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from algotrader.api import create_app
from algotrader.config import Settings
from algotrader.journal import Journal
from algotrader.trader import Trader
from algotrader.vercel import deployment_problems, setup_app
from tests.helpers import FakeBrokerTest, StubModel, TempDirTest

ROOT = Path(__file__).resolve().parents[2]
POST = {"X-AlgoTrader": "1"}
PW = "correct horse battery"


class SetupCheckTests(TempDirTest):
    def test_missing_settings_are_listed(self):
        problems = deployment_problems(Settings())
        text = " ".join(problems)
        for name in ("ALPACA_API_KEY", "DATABASE_URL", "SECRET_KEY", "CRON_SECRET"):
            self.assertIn(name, text)
        complete = Settings(alpaca_api_key="k", alpaca_secret_key="s", secret_key="x" * 30, cron_secret="c" * 20,
                            database_url="postgresql://u:p@db.example.com:5432/postgres")
        self.assertEqual(deployment_problems(complete), [])
        self.assertIn("DATABASE_URL", " ".join(deployment_problems(replace(complete, database_url=str(self.tmp / "x.db")))))
        self.assertEqual(deployment_problems(replace(complete, database_url=None, secret_key=None, user_accounts=False)), [])

    def test_setup_page(self):
        dist = self.tmp / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<html>dashboard</html>", encoding="utf-8")
        (dist / "assets" / "app.js").write_text("js", encoding="utf-8")
        http = setup_app(Settings(frontend_dist=dist), ["SECRET_KEY isn't set: add it."]).test_client()
        health = http.get("/api/health").get_json()
        self.assertEqual(health["problems"], ["SECRET_KEY isn't set: add it."])
        r = http.get("/api/account")
        self.assertEqual(r.status_code, 503)
        self.assertIn("SECRET_KEY", r.get_json()["error"])
        self.assertIn(b"dashboard", http.get("/").data)              # the dashboard shows the list
        self.assertEqual(http.get("/assets/app.js").data, b"js")

    def test_entry_point_like_vercel_loads_it(self):
        env = {"DATA_DIR": str(self.tmp / "tmpdata"), "USER_ACCOUNTS": "false", "ALPACA_API_KEY": "k",
               "ALPACA_SECRET_KEY": "s", "CRON_SECRET": "c" * 24}
        with mock.patch.dict(os.environ, env, clear=True):
            app = runpy.run_path(str(ROOT / "app.py"))["app"]
            health = app.test_client().get("/api/health").get_json()
        self.assertEqual(health["status"], "ok")
        self.assertFalse(health["live_stream"])                       # polling instead of a held-open stream
        self.assertTrue((self.tmp / "tmpdata").exists())              # writes go to the temporary folder only
        with mock.patch.dict(os.environ, {"DATA_DIR": str(self.tmp / "t2")}, clear=True):
            stand_in = runpy.run_path(str(ROOT / "app.py"))["app"]
            self.assertTrue(stand_in.test_client().get("/api/health").get_json()["problems"])


class ScheduledRunTests(FakeBrokerTest):
    def setUp(self):
        super().setUp()
        self.with_strategy(use_market_filter=False)
        self.journal = Journal(self.tmp / "j.db")
        self.settings = replace(self.settings, cron_secret="s3cret-cron-value-123")

    def app(self, settings=None):
        s = settings or self.settings
        return create_app(s, client=self.client, journal=self.journal,
                          trader_factory=lambda: Trader(s, self.client, self.journal,
                                                        model=StubModel({"AAA": 0.9, "BBB": 0.2})))

    def test_only_the_scheduler_can_start_a_run(self):
        http = self.app().test_client()
        self.assertEqual(http.get("/api/cron/trade").status_code, 401)
        self.assertEqual(http.get("/api/cron/trade", headers={"Authorization": "Bearer wrong"}).status_code, 401)
        self.assertEqual(http.get("/api/cron/trade", headers={"Authorization": "Bearer s3cret-cron-välue"}).status_code, 401)
        self.assertEqual(len(self.fake.orders), 0)
        r = http.get("/api/cron/trade", headers={"Authorization": "Bearer s3cret-cron-value-123"})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["status"], "ok")
        self.assertEqual([o["symbol"] for o in self.fake.orders], ["AAA"])
        self.assertEqual(self.journal.recent_runs(1)[0]["status"], "ok")
        again = http.get("/api/cron/trade", headers={"Authorization": "Bearer s3cret-cron-value-123"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(len([o for o in self.fake.orders if o["side"] == "buy"]), 1)   # a repeat never buys twice

    def test_off_without_a_secret(self):
        http = self.app(replace(self.settings, cron_secret=None)).test_client()
        self.assertEqual(http.get("/api/cron/trade", headers={"Authorization": "Bearer "}).status_code, 404)

    def test_needs_no_log_in_but_everything_else_does(self):
        from algotrader.users import SecretBox, UserStore
        s = replace(self.settings, user_accounts=True)
        app = create_app(s, client=self.client, journal=self.journal,
                         users=UserStore(self.tmp / "u.db", SecretBox("m")),
                         trader_factory=lambda: Trader(s, self.client, self.journal, model=StubModel({"AAA": 0.1})))
        http = app.test_client()
        self.assertEqual(http.get("/api/account").status_code, 401)
        self.assertEqual(http.get("/api/cron/trade", headers={"Authorization": "Bearer s3cret-cron-value-123"})
                         .status_code, 200)


class ProxyAndStreamTests(FakeBrokerTest):
    def test_visitor_address_comes_from_the_proxy_on_vercel(self):
        from algotrader.users import SecretBox, UserStore

        def app(trust):
            store = UserStore(self.tmp / f"u{trust}.db", SecretBox("m"))
            return create_app(replace(self.settings, user_accounts=True, trust_proxy=trust, local_only=False),
                              client=self.client, journal=Journal(self.tmp / "j.db"), users=store).test_client()

        behind_proxy = app(True)
        for i in range(10):                                           # 10 sign-ups an hour from one address
            r = behind_proxy.post("/api/auth/signup", json={"email": f"a{i}@x.com", "name": "A", "password": PW},
                                  headers={**POST, "X-Real-IP": "198.51.100.7"})
            self.assertEqual(r.status_code, 201)
        r = behind_proxy.post("/api/auth/signup", json={"email": "a10@x.com", "name": "A", "password": PW},
                              headers={**POST, "X-Real-IP": "198.51.100.7"})
        self.assertEqual(r.status_code, 429)
        r = behind_proxy.post("/api/auth/signup", json={"email": "b@x.com", "name": "B", "password": PW},
                              headers={**POST, "X-Real-IP": "203.0.113.20"})          # someone else isn't blocked
        self.assertEqual(r.status_code, 201)
        direct = app(False)                                           # without the setting, the header is ignored
        for i in range(10):
            direct.post("/api/auth/signup", json={"email": f"c{i}@x.com", "name": "C", "password": PW},
                        headers={**POST, "X-Real-IP": f"10.0.0.{i}"})
        r = direct.post("/api/auth/signup", json={"email": "c10@x.com", "name": "C", "password": PW},
                        headers={**POST, "X-Real-IP": "10.0.0.99"})
        self.assertEqual(r.status_code, 429)

    def test_no_held_open_stream_when_live_stream_is_off(self):
        http = create_app(replace(self.settings, live_stream=False), client=self.client,
                          journal=Journal(self.tmp / "j.db")).test_client()
        self.assertFalse(http.get("/api/health").get_json()["live_stream"])
        body = http.get("/api/stream").get_data(as_text=True)         # returns at once instead of streaming forever
        self.assertEqual(body.count("data: "), 1)


class DeploymentFilesTests(unittest.TestCase):
    def test_vercel_json(self):
        cfg = json.loads((ROOT / "vercel.json").read_text())
        self.assertLessEqual(cfg["functions"]["app.py"]["maxDuration"], 300)      # the free plan's limit
        cron = cfg["crons"][0]
        self.assertEqual(cron["path"], "/api/cron/trade")
        minute, hour, dom, month, dow = cron["schedule"].split()
        self.assertTrue(minute.isdigit() and hour.isdigit())                       # once a day (free plan)
        self.assertGreaterEqual(int(hour), 21)                                     # after the 4 pm New York close,
        self.assertEqual(dow, "1-5")                                               # summer and winter, on weekdays

    def test_root_requirements_match_the_backend(self):
        def packages(path):
            names = []
            for line in Path(path).read_text().splitlines():
                line = line.split("#")[0].strip()
                if line and not line.startswith("-r"):
                    names.append(re.split(r"[;\s]", line)[0])
            return names
        backend = [p for p in packages(ROOT / "backend/requirements.txt") if not p.startswith("gunicorn")]
        self.assertEqual(packages(ROOT / "requirements.txt"), backend)
        self.assertEqual((ROOT / ".python-version").read_text().strip(), "3.13")

    @unittest.skipUnless(shutil.which("git"), "git is needed to check the ignore rules")
    def test_only_the_site_is_uploaded(self):
        uploaded = ["app.py", "vercel.json", "requirements.txt", ".python-version", "backend/algotrader/api.py",
                    "backend/algotrader/vercel.py", "backend/models/model.joblib", "backend/models/model_meta.json",
                    "frontend/dist/index.html", "frontend/dist/assets/app.js", "frontend/dist/assets/app.css"]
        kept_private = [".env", "backend/.env", "backend/data/users.db", "backend/data/secret.key",
                        "backend/.venv/Lib/site.py", "backend/tests/test_api.py", "backend/requirements-dev.txt",
                        "frontend/node_modules/react/index.js", "frontend/src/App.jsx", "frontend/package.json",
                        "frontend/dist/assets/app.js.map", "docs/PROCESS.md", "Dockerfile", ".git/config",
                        "backend/algotrader/__pycache__/api.cpython-313.pyc"]
        with tempfile.TemporaryDirectory() as repo:
            subprocess.run(["git", "init", "-q", repo], check=True)
            shutil.copy(ROOT / ".vercelignore", Path(repo) / ".gitignore")
            result = subprocess.run(["git", "-C", repo, "check-ignore", "--no-index", "--stdin"],
                                    input="\n".join(uploaded + kept_private), capture_output=True, text=True)
            ignored = set(result.stdout.split())
        self.assertEqual([p for p in uploaded if p in ignored], [])
        self.assertEqual([p for p in kept_private if p not in ignored and p != ".git/config"], [])


if __name__ == "__main__":
    unittest.main()
