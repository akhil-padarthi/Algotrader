"""User accounts: the database, sign-up/log-in, each person's own Alpaca account, admin-only bot account."""
import contextlib
import io
import os
import unittest
from dataclasses import replace
from unittest import mock

from algotrader.alpaca_client import AlpacaClient
from algotrader.api import create_app
from algotrader.fake_alpaca import FakeAlpaca, ServerThread
from algotrader.journal import Journal
from algotrader.users import (
    SESSION_COOKIE, AccountError, SecretBox, UserStore, load_or_create_key_file,
)
from tests.helpers import FakeBrokerTest, TempDirTest

PW = "correct horse battery"
POST = {"X-AlgoTrader": "1"}


class UserStoreTests(TempDirTest):
    def target(self):
        return self.tmp / "users.db"

    def setUp(self):
        super().setUp()
        self.store = UserStore(self.target(), SecretBox("test material"))

    def raw(self, sql):
        with self.store.db.connect() as c:
            return [tuple(r) if not isinstance(r, dict) else tuple(r.values()) for r in c.execute(sql).fetchall()]

    def test_create_and_log_in(self):
        u = self.store.create_user("  Ana@Example.com ", "Ana  Ruiz", PW)
        self.assertEqual((u["email"], u["name"], u["is_admin"]), ("ana@example.com", "Ana Ruiz", False))
        self.assertNotIn("password_hash", u)
        stored = self.raw("SELECT password_hash FROM users")[0][0]
        self.assertNotIn(PW, stored)
        self.assertTrue(stored.startswith("scrypt:"))
        self.assertEqual(self.store.authenticate("ANA@example.com", PW)["id"], u["id"])
        for email, pw in (("ana@example.com", "wrong password"), ("nobody@example.com", PW)):
            with self.assertRaises(AccountError) as ctx:
                self.store.authenticate(email, pw)
            self.assertEqual((ctx.exception.status, ctx.exception.message), (401, "Email or password is incorrect."))

    def test_profile_rules(self):
        bad = [("not-an-email", "Ana", PW, "valid email"), ("a@b.co", "  ", PW, "your name"),
               ("a@b.co", "Ana", "short", "at least 8"), ("a@b.co", "Ana", "password123", "too easy"),
               ("a@b.co", "Ana", "aaaaaaaaaaaa", "too easy"), ("ana@b.co", "Ana", "ana@b.co", "email address"),
               ("a@b.co", "x" * 61, PW, "60 characters")]
        for email, name, pw, words in bad:
            with self.assertRaises(AccountError) as ctx:
                self.store.create_user(email, name, pw)
            self.assertIn(words, ctx.exception.message)
        self.store.create_user("ana@b.co", "Ana", PW)
        with self.assertRaises(AccountError) as ctx:
            self.store.create_user("ANA@B.CO", "Other", PW)
        self.assertEqual(ctx.exception.status, 409)

    def test_wrong_passwords_are_slowed_down(self):
        self.store.create_user("ana@b.co", "Ana", PW)
        for _ in range(8):
            with self.assertRaises(AccountError):
                self.store.authenticate("ana@b.co", "nope nope", ip="10.0.0.1")
        with self.assertRaises(AccountError) as ctx:           # even the right password waits now
            self.store.authenticate("ana@b.co", PW, ip="10.0.0.2")
        self.assertEqual(ctx.exception.status, 429)
        self.store.create_user("bo@b.co", "Bo", PW)
        self.assertEqual(self.store.authenticate("bo@b.co", PW)["email"], "bo@b.co")   # other people unaffected
        for i in range(30):                                      # one address guessing many accounts
            with contextlib.suppress(AccountError):
                self.store.authenticate(f"guess{i}@b.co", "nope nope", ip="10.9.9.9")
        with self.assertRaises(AccountError) as ctx:
            self.store.authenticate("bo@b.co", PW, ip="10.9.9.9")
        self.assertEqual(ctx.exception.status, 429)

    def test_sessions(self):
        u = self.store.create_user("ana@b.co", "Ana", PW)
        token = self.store.create_session(u["id"], "Firefox")
        self.assertEqual(self.store.user_for_session(token)["id"], u["id"])
        self.assertIsNone(self.store.user_for_session("made-up-token"))
        self.assertIsNone(self.store.user_for_session(None))
        self.assertNotIn(token, str(self.raw("SELECT * FROM sessions")))    # only a hash of the token is stored
        with self.store.db.connect() as c:
            c.execute("UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00'")
        self.assertIsNone(self.store.user_for_session(token))
        token = self.store.create_session(u["id"])
        self.store.end_session(token)
        self.assertIsNone(self.store.user_for_session(token))

    def test_changing_the_password_logs_out_other_browsers(self):
        u = self.store.create_user("ana@b.co", "Ana", PW)
        here, there = self.store.create_session(u["id"]), self.store.create_session(u["id"])
        with self.assertRaises(AccountError):
            self.store.change_password(u["id"], "not my password", "a brand new secret", keep_token=here)
        self.store.change_password(u["id"], PW, "a brand new secret", keep_token=here)
        self.assertIsNotNone(self.store.user_for_session(here))
        self.assertIsNone(self.store.user_for_session(there))
        with self.assertRaises(AccountError):
            self.store.authenticate("ana@b.co", PW)
        self.store.authenticate("ana@b.co", "a brand new secret")

    def test_alpaca_keys_are_stored_encrypted(self):
        ana, bo = self.store.create_user("ana@b.co", "Ana", PW), self.store.create_user("bo@b.co", "Bo", PW)
        u = self.store.link_alpaca(ana["id"], "PKTESTKEY123456", "s3cretSecretSecretSecret", "https://paper", "PA123456789")
        self.assertEqual(u["broker"], {"linked": True, "account": "•••6789", "linked_at": u["broker"]["linked_at"]})
        raw = str(self.raw("SELECT * FROM users"))
        self.assertNotIn("PKTESTKEY123456", raw)
        self.assertNotIn("s3cretSecretSecretSecret", raw)
        self.assertEqual(self.store.alpaca_credentials(ana["id"]),
                         ("PKTESTKEY123456", "s3cretSecretSecretSecret", "https://paper"))
        self.assertIsNone(self.store.alpaca_credentials(bo["id"]))
        with self.assertRaises(AccountError) as ctx:             # one Alpaca account per person
            self.store.link_alpaca(bo["id"], "PKTESTKEY123456", "other", "https://paper", "PA1")
        self.assertEqual(ctx.exception.status, 409)
        other_server = UserStore(self.target(), SecretBox("a different secret"))
        with self.assertRaises(AccountError) as ctx:             # a lost secret key means reconnecting
            other_server.alpaca_credentials(ana["id"])
        self.assertIn("Connect your Alpaca paper account again", ctx.exception.message)
        self.assertFalse(self.store.unlink_alpaca(ana["id"])["broker"]["linked"])
        self.assertIsNone(self.store.alpaca_credentials(ana["id"]))

    def test_deleting_an_account_removes_everything(self):
        u = self.store.create_user("ana@b.co", "Ana", PW)
        token = self.store.create_session(u["id"])
        self.store.record_trade(u["id"], symbol="AAPL", side="buy", amount=100.0)
        with self.assertRaises(AccountError):
            self.store.delete_user(u["id"], "wrong password")
        self.store.delete_user(u["id"], PW)
        self.assertIsNone(self.store.get(u["id"]))
        self.assertIsNone(self.store.user_for_session(token))
        self.assertEqual(self.store.db.count("trades"), 0)

    def test_admin_tools(self):
        u = self.store.create_user("ana@b.co", "Ana", PW)
        token = self.store.create_session(u["id"])
        self.assertTrue(self.store.set_admin("ANA@b.co")["is_admin"])
        self.store.reset_password("ana@b.co", "a fresh password")
        self.assertIsNone(self.store.user_for_session(token))
        self.store.authenticate("ana@b.co", "a fresh password")
        with self.assertRaises(AccountError):
            self.store.set_admin("nobody@b.co")

    def test_secret_key_file(self):
        path = self.tmp / "sub" / "secret.key"
        first = load_or_create_key_file(path)
        self.assertGreaterEqual(len(first), 40)
        self.assertEqual(load_or_create_key_file(path), first)
        box = SecretBox(first)
        self.assertEqual(box.decrypt(box.encrypt("hello")), "hello")
        self.assertNotEqual(box.encrypt("hello"), "hello")


class AccountsApiTests(FakeBrokerTest):
    """The bot's account is self.fake; each member gets their own stand-in Alpaca account."""

    def target(self):
        return self.tmp / "users.db"

    def journal_target(self):
        return self.tmp / "j.db"

    def setUp(self):
        super().setUp()
        self.settings = replace(self.settings, user_accounts=True, local_only=True,
                                user_trading_url="https://paper-api.example")
        self.journal = Journal(self.journal_target())
        self.store = UserStore(self.target(), SecretBox("test material"))
        self.member_fakes, self.routes = {}, {}
        for n in (1, 2):
            key, secret = f"PKMEMBER{n:08d}", f"membersecret{n}" * 2
            fake = FakeAlpaca(self.bars, start_index=self.fake.i, api_key=key, secret_key=secret)
            server = ServerThread(fake.app)
            server.start()
            self.addCleanup(server.stop)
            self.member_fakes[n] = (key, secret, fake)
            self.routes[key] = server.url
        self.app = self.make_app()

    def make_app(self, settings=None, **kw):
        def broker(key, secret, url):        # sends each member's keys to their own stand-in account
            target = self.routes.get(key, self.routes["PKMEMBER00000001"])
            c = AlpacaClient(key, secret, target, target)
            c.backoff_scale = 0
            return c
        return create_app(settings or self.settings, client=self.client, journal=self.journal, users=self.store,
                          user_broker=broker, **kw)

    def browser(self, ip="127.0.0.1"):
        c = self.app.test_client()
        c.environ_base["REMOTE_ADDR"] = ip
        return c

    def signup(self, http, email, name="Test Person", password=PW):
        return http.post("/api/auth/signup", json={"email": email, "name": name, "password": password}, headers=POST)

    def connect(self, http, n):
        key, secret, _ = self.member_fakes[n]
        return http.post("/api/me/alpaca", json={"key_id": key, "secret_key": secret}, headers=POST)

    # ------------------------------------------------------------------ tests
    def test_everything_needs_a_log_in(self):
        http = self.browser()
        r = http.get("/api/account")
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.get_json()["login_required"])
        for path in ("/api/positions", "/api/orders", "/api/performance", "/api/market", "/api/stock/AAA",
                     "/api/signals", "/api/bot", "/api/me", "/api/stream?once=1"):
            self.assertEqual(http.get(path).status_code, 401, path)
        self.assertEqual(http.get("/api/health").get_json()["accounts"], True)
        status = http.get("/api/auth/status").get_json()
        self.assertEqual((status["has_users"], status["first_account_is_admin"], status["user"]), (False, True, None))

    def test_first_account_is_the_admin_and_sees_the_bot(self):
        http = self.browser()
        r = self.signup(http, "owner@example.com", "Owner")
        self.assertEqual(r.status_code, 201)
        cookie = r.headers["Set-Cookie"]
        self.assertIn(f"{SESSION_COOKIE}=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        me = http.get("/api/me").get_json()["user"]
        self.assertEqual((me["is_admin"], me["account_kind"]), (True, "bot"))
        self.assertAlmostEqual(http.get("/api/account").get_json()["equity"], self.fake.equity(), delta=1)
        self.assertEqual(http.get("/api/bot").status_code, 200)
        self.assertEqual(http.get("/api/signals").get_json()["audience"], "bot")

    def test_member_signs_up_connects_alpaca_and_buys(self):
        self.signup(self.browser(), "owner@example.com")
        http = self.browser()
        me = self.signup(http, "ana@example.com", "Ana").get_json()["user"]
        self.assertEqual((me["is_admin"], me["broker"]["linked"]), (False, False))

        # before connecting Alpaca: market data works, the account asks to be connected, the bot is private
        r = http.get("/api/account")
        self.assertEqual(r.status_code, 409)
        self.assertTrue(r.get_json()["needs_broker"])
        self.assertEqual(http.get("/api/bot").status_code, 403)
        self.assertEqual(http.get("/api/market").status_code, 200)
        stock = http.get("/api/stock/AAA").get_json()
        self.assertEqual((stock["trading"]["needs_broker"], stock["account_kind"], stock["position"]), (True, None, None))
        sig = http.get("/api/signals").get_json()
        self.assertEqual(sig["audience"], "member")
        self.assertTrue(all(s["action"] in ("BUY", "HOLD", "SELL") and s["reason"] == "" for s in sig["signals"]))
        r = http.post("/api/trade", json={"symbol": "AAA", "side": "buy", "amount": 500}, headers=POST)
        self.assertEqual(r.status_code, 409)

        # wrong keys are refused; the right ones connect
        r = http.post("/api/me/alpaca", json={"key_id": "PKWRONGKEY000001", "secret_key": "x" * 30}, headers=POST)
        self.assertEqual(r.status_code, 400)
        self.assertIn("didn't accept these keys", r.get_json()["error"])
        r = http.post("/api/me/alpaca", json={"key_id": "short", "secret_key": "x" * 30}, headers=POST)
        self.assertIn("API Key ID", r.get_json()["error"])
        r = self.connect(http, 1)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertTrue(r.get_json()["user"]["broker"]["linked"])
        self.assertTrue(r.get_json()["user"]["broker"]["account"].startswith("•••"))
        self.assertNotIn(self.member_fakes[1][1], r.get_data(as_text=True))     # the secret is never sent back

        # now it's their own $100,000 account, not the bot's
        self.assertAlmostEqual(http.get("/api/account").get_json()["equity"], 100_000, delta=1)
        self.assertEqual(http.get("/api/positions").get_json(), [])
        bot_orders_before = len(self.fake.orders)
        r = http.post("/api/trade", json={"symbol": "AAA", "side": "buy", "amount": 500}, headers=POST)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["mode"], "paper")
        member_fake = self.member_fakes[1][2]
        self.assertEqual(member_fake.orders[-1]["notional"], "500.00")
        self.assertEqual(len(self.fake.orders), bot_orders_before)            # the bot's account is untouched
        trades = http.get("/api/me/trades").get_json()["trades"]
        self.assertEqual((trades[0]["symbol"], trades[0]["side"], trades[0]["amount"]), ("AAA", "buy", 500.0))
        orders = http.get("/api/orders").get_json()
        self.assertEqual(orders[0]["symbol"], "AAA")
        self.assertFalse(orders[0]["bot"])
        member_fake.advance()                                                   # next open: the order fills
        pos = http.get("/api/positions").get_json()
        self.assertEqual([p["symbol"] for p in pos], ["AAA"])
        self.assertFalse(pos[0]["managed"])
        self.assertEqual(http.get("/api/stock/AAA").get_json()["account_kind"], "member")
        r = http.get("/api/stream?once=1")
        self.assertIn('"needs_broker": false', r.get_data(as_text=True))
        r = http.post("/api/trade", json={"symbol": "AAA", "side": "sell_all"}, headers=POST)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(http.get("/api/me/trades").get_json()["trades"][0]["side"], "sell")

    def test_members_only_see_their_own_account(self):
        admin, ana, bo = self.browser(), self.browser(), self.browser()
        self.signup(admin, "owner@example.com")
        self.signup(ana, "ana@example.com")
        self.signup(bo, "bo@example.com")
        self.connect(ana, 1)
        self.connect(bo, 2)
        ana.post("/api/trade", json={"symbol": "BBB", "side": "buy", "amount": 250}, headers=POST)
        self.member_fakes[1][2].advance()
        self.assertEqual([p["symbol"] for p in ana.get("/api/positions").get_json()], ["BBB"])
        self.assertEqual(bo.get("/api/positions").get_json(), [])
        self.assertEqual(bo.get("/api/orders").get_json(), [])
        self.assertEqual(len(admin.get("/api/positions").get_json()), len(self.fake.positions))
        r = bo.post("/api/me/alpaca", json={"key_id": self.member_fakes[1][0], "secret_key": self.member_fakes[1][1]},
                    headers=POST)
        self.assertEqual(r.status_code, 409)                                   # Ana's keys can't be reused
        self.assertEqual(admin.post("/api/me/alpaca", json={"key_id": "PKMEMBER00000002", "secret_key": "x" * 30},
                                    headers=POST).status_code, 400)            # the admin uses the bot's account

    def test_log_out_and_back_in(self):
        http = self.browser()
        self.signup(http, "ana@example.com")
        self.assertEqual(http.post("/api/auth/logout", json={}, headers=POST).status_code, 200)
        self.assertEqual(http.get("/api/me").status_code, 401)
        r = http.post("/api/auth/login", json={"email": "ana@example.com", "password": "wrong password"}, headers=POST)
        self.assertEqual((r.status_code, r.get_json()["error"]), (401, "Email or password is incorrect."))
        r = http.post("/api/auth/login", json={"email": "ANA@example.com", "password": PW}, headers=POST)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(http.get("/api/me").get_json()["user"]["email"], "ana@example.com")
        status = http.get("/api/auth/status").get_json()
        self.assertEqual((status["user"]["email"], status["first_account_is_admin"]), ("ana@example.com", False))

    def test_changes_must_come_from_the_dashboard(self):
        http = self.browser()
        body = {"email": "ana@example.com", "name": "Ana", "password": PW}
        self.assertEqual(http.post("/api/auth/signup", json=body).status_code, 403)               # no header
        self.assertEqual(http.post("/api/auth/signup", data="email=x", headers=POST).status_code, 403)  # not JSON
        r = http.post("/api/auth/signup", json=body, headers={**POST, "Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(self.store.count(), 0)

    def test_sign_up_rules(self):
        http = self.browser()
        r = self.signup(http, "ana@example.com", password="short")
        self.assertEqual(r.status_code, 400)
        self.assertIn("at least 8", r.get_json()["error"])
        self.signup(http, "ana@example.com")
        r = self.signup(self.browser(), "ana@example.com")
        self.assertEqual(r.status_code, 409)
        closed = self.make_app(replace(self.settings, allow_signups=False)).test_client()
        r = self.signup(closed, "bo@example.com")
        self.assertEqual(r.status_code, 403)
        self.assertIn("closed", r.get_json()["error"])

    def test_no_automatic_admin_on_a_server_others_can_reach(self):
        app = self.make_app(replace(self.settings, local_only=False))
        http = app.test_client()
        self.assertFalse(http.get("/api/auth/status").get_json()["first_account_is_admin"])
        self.assertFalse(self.signup(http, "first@example.com").get_json()["user"]["is_admin"])
        self.store.delete_user(self.store.by_email("first@example.com")["id"])
        remote = self.browser(ip="203.0.113.9")               # local-only setting, but not from this computer
        self.assertFalse(self.signup(remote, "first@example.com").get_json()["user"]["is_admin"])

    def test_profile_password_and_delete(self):
        http = self.browser()
        self.signup(http, "ana@example.com", "Ana")
        r = http.post("/api/me/profile", json={"name": "Ana María"}, headers=POST)
        self.assertEqual(r.get_json()["user"]["name"], "Ana María")
        other = self.browser()
        other.post("/api/auth/login", json={"email": "ana@example.com", "password": PW}, headers=POST)
        r = http.post("/api/me/password", json={"current_password": "nope nope", "new_password": "another secret"},
                      headers=POST)
        self.assertEqual(r.status_code, 400)
        r = http.post("/api/me/password", json={"current_password": PW, "new_password": "another secret"}, headers=POST)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(http.get("/api/me").status_code, 200)                 # this browser stays in
        self.assertEqual(other.get("/api/me").status_code, 401)                # the other one is logged out
        self.connect(http, 1)
        self.assertFalse(http.post("/api/me/alpaca/unlink", json={}, headers=POST).get_json()["user"]["broker"]["linked"])
        self.assertEqual(http.post("/api/me/delete", json={"password": "wrong"}, headers=POST).status_code, 400)
        self.assertEqual(http.post("/api/me/delete", json={"password": "another secret"}, headers=POST).status_code, 200)
        self.assertEqual(http.get("/api/me").status_code, 401)
        self.assertIsNone(self.store.by_email("ana@example.com"))

    def test_demo_accounts(self):
        made = []

        def new_account():
            key, secret, _ = self.member_fakes[2]
            made.append(key)
            return key, secret, "ignored"
        http = self.make_app(demo_accounts=new_account).test_client()
        self.signup(http, "owner@example.com")
        member = self.make_app(demo_accounts=new_account).test_client()
        self.signup(member, "demo@example.com")
        self.assertTrue(member.get("/api/auth/status").get_json()["demo_accounts"])
        r = member.post("/api/me/alpaca/demo", json={}, headers=POST)
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(made, ["PKMEMBER00000002"])
        self.assertAlmostEqual(member.get("/api/account").get_json()["equity"], 100_000, delta=1)
        plain = self.browser()
        plain.post("/api/auth/login", json={"email": "demo@example.com", "password": PW}, headers=POST)
        self.assertEqual(plain.post("/api/me/alpaca/demo", json={}, headers=POST).status_code, 404)

    def test_accounts_switched_off(self):
        http = create_app(replace(self.settings, user_accounts=False), client=self.client,
                          journal=self.journal).test_client()
        self.assertEqual(http.get("/api/account").status_code, 200)            # no log-in needed
        self.assertEqual(http.post("/api/auth/signup", json={}, headers=POST).status_code, 404)


class DoctorAccountsTests(TempDirTest):
    def test_doctor_reports_accounts(self):
        from algotrader.config import Settings
        from algotrader.doctor import run_doctor
        s = Settings(data_dir=self.tmp, secret_key="doctor material", frontend_dist=self.tmp)
        out = io.StringIO()
        run_doctor(s, out=out, server_url="http://127.0.0.1:9")
        self.assertIn("[ OK ] User accounts: none yet. The first account you create on this computer becomes the admin",
                      out.getvalue())
        store = UserStore(s.users_db_path, SecretBox("doctor material"))
        store.create_user("ana@example.com", "Ana", PW)
        out = io.StringIO()
        run_doctor(s, out=out, server_url="http://127.0.0.1:9")
        self.assertIn("[WARN] User accounts: 1 account(s) but no admin", out.getvalue())
        self.assertIn("users make-admin", out.getvalue())
        store.set_admin("ana@example.com")
        out = io.StringIO()
        run_doctor(s, out=out, server_url="http://127.0.0.1:9")
        self.assertIn("admin: ana@example.com", out.getvalue())


class UsersCommandTests(TempDirTest):
    def run_cli(self, *argv):
        from algotrader.cli import main
        out = io.StringIO()
        env = {"DATA_DIR": str(self.tmp), "SECRET_KEY": "cli test material"}
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out):
            main(["--env-file", str(self.tmp / "none.env"), "users", *argv])
        return out.getvalue()

    def test_users_commands(self):
        self.assertIn("No accounts yet", self.run_cli("list"))
        store = UserStore(self.tmp / "users.db", SecretBox("cli test material"))
        store.create_user("ana@example.com", "Ana", PW)
        self.assertIn("ana@example.com", self.run_cli("list"))
        self.assertIn("now an admin", self.run_cli("make-admin", "ana@example.com"))
        self.assertTrue(store.by_email("ana@example.com")["is_admin"])
        self.assertIn("Password changed", self.run_cli("reset-password", "ana@example.com", "--password", "fresh password"))
        store.authenticate("ana@example.com", "fresh password")
        with self.assertRaises(SystemExit) as ctx:
            self.run_cli("make-admin", "nobody@example.com")
        self.assertIn("No account", str(ctx.exception))
        self.assertIn("Deleted", self.run_cli("delete", "ana@example.com", "--yes"))
        self.assertEqual(store.count(), 0)


if __name__ == "__main__":
    unittest.main()
