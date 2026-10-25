"""`python -m algotrader doctor`: check every piece the bot and dashboard depend on, in order,
and say in plain words what to do about anything that fails."""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import timedelta

import pandas as pd

from . import __version__
from . import model as model_mod

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"


class Report:
    def __init__(self, out=sys.stdout):
        self.out = out
        self.failed = 0
        self.warned = 0

    def line(self, status: str, title: str, detail: str = "", fix: str = "") -> None:
        if status == FAIL:
            self.failed += 1
        elif status == WARN:
            self.warned += 1
        print(f"{status} {title}" + (f": {detail}" if detail else ""), file=self.out)
        if fix and status != OK:
            print(f"       -> {fix}", file=self.out)


def run_doctor(settings, client=None, fred=None, out=sys.stdout, server_url: str | None = None) -> int:
    from .alpaca_client import AlpacaClient, AlpacaError
    from .api import STALE_BUILD_FIX, frontend_version
    from .context import FRED_SERIES, FredClient, SourceUnavailable

    r = Report(out)
    print(f"AlgoTrader {__version__} - checking your setup\n", file=out)

    # 1. code and dashboard files
    fv = frontend_version(settings.frontend_dist)
    if fv is None:
        r.line(FAIL, "Dashboard files", f"not found in {settings.frontend_dist}",
               "build them: cd frontend && npm install && npm run build")
    elif fv.startswith("__"):
        # the new index.html went through an old build script: an "npm run dev" watcher from before the
        # update was still running and rebuilt the dashboard when the files changed
        r.line(FAIL, "Dashboard files", "they were rebuilt by an old copy of the build script that is still running",
               STALE_BUILD_FIX)
    elif fv != __version__:
        r.line(FAIL, "Dashboard files", f"version {fv}, but the Python code is {__version__}",
               "rebuild the dashboard (cd frontend && npm run build), then restart serve")
    else:
        r.line(OK, "Dashboard files", f"version {fv}")

    # 2. a dashboard server that is still running old code
    url = server_url or f"http://127.0.0.1:{settings.api_port}"
    try:
        with urllib.request.urlopen(f"{url}/api/health", timeout=3) as resp:
            running = json.loads(resp.read().decode()).get("version")
        if running != __version__:
            r.line(FAIL, "Running dashboard server", f"{url} is running version {running}, not {__version__}",
                   "press Ctrl+C in the window running 'python -m algotrader serve', then start it again")
        else:
            r.line(OK, "Running dashboard server", f"{url} is up to date")
    except Exception:
        r.line(OK, "Running dashboard server", f"none on {url} right now (that's fine)")

    # 3. where the data is stored
    from .db import DatabaseError, describe
    if settings.database_url:
        try:
            from .journal import Journal
            Journal(settings.database_url)
            r.line(OK, "Database", f"{describe(settings.database_url)}: connected")
        except DatabaseError as exc:
            r.line(FAIL, "Database", str(exc), "fix DATABASE_URL in backend/.env (or empty it to use the local files)")
            return _finish(r, out)
    else:
        r.line(OK, "Database", f"local files in {settings.data_dir} (set DATABASE_URL to use Supabase)")

    # 4. user accounts
    if settings.user_accounts:
        try:
            from .users import SecretBox, UserStore
            store = UserStore(settings.users_db, SecretBox.from_settings(settings), settings.session_days)
            people = store.list_users()
            admins = [u["email"] for u in people if u["is_admin"]]
            if not people:
                r.line(OK, "User accounts", "none yet. " + (
                    "The first account you create on this computer becomes the admin" if settings.is_local_only else
                    "After signing up, run: python -m algotrader users make-admin YOUR_EMAIL"))
            elif not admins:
                r.line(WARN, "User accounts", f"{len(people)} account(s) but no admin, so nobody sees the bot's account",
                       "python -m algotrader users make-admin YOUR_EMAIL")
            else:
                linked = sum(u["broker"]["linked"] for u in people)
                r.line(OK, "User accounts", f"{len(people)} account(s), {linked} with Alpaca connected; "
                                            f"admin: {', '.join(admins)}")
        except DatabaseError as exc:
            r.line(FAIL, "User accounts", str(exc), "fix DATABASE_URL in backend/.env")
        except RuntimeError:             # the cryptography package isn't installed
            r.line(FAIL, "User accounts", "the 'cryptography' package is missing",
                   "in the backend folder run: pip install -r requirements.txt")

    # 5. Alpaca
    if not settings.has_credentials:
        r.line(FAIL, "Alpaca API keys", "not set",
               "copy .env.example to backend/.env and add ALPACA_API_KEY and ALPACA_SECRET_KEY")
        return _finish(r, out)
    client = client or AlpacaClient.from_settings(settings)
    try:
        acct = client.get_account()
        r.line(OK, "Alpaca account", f"{acct.get('status')} ({settings.mode} trading)")
    except AlpacaError as exc:
        r.line(FAIL, "Alpaca account", exc.message,
               "check the keys in backend/.env; paper and live accounts have different keys")
        return _finish(r, out)

    now = pd.Timestamp(client.get_clock()["timestamp"])
    checks = [
        ("Stock list (for search)", lambda: f"{len(client.get_assets())} tradable assets"),
        (f"Daily prices ({settings.benchmark})",
         lambda: f"{len(client.get_bars([settings.benchmark], start=(now - timedelta(days=10)).date().isoformat()))} days"),
        ("Intraday prices (stock charts)",
         lambda: f"{len(client.get_bars([settings.benchmark], start=(now - timedelta(days=5)).date().isoformat(), timeframe='5Min'))} five-minute bars"),
        ("Latest quotes", lambda: f"{settings.benchmark} last trade ${client.get_snapshots([settings.benchmark])[settings.benchmark]['latestTrade']['p']}"),
        ("Account history (dashboard chart)",
         lambda: f"{len(client.get_portfolio_history(timeframe='1D', start=(now - timedelta(days=30)).date().isoformat())['timestamp'])} daily points"),
        ("News (Benzinga via Alpaca)",
         lambda: f"{len(client.get_news(settings.symbols[:3], start=(now - timedelta(days=3)).tz_convert('UTC').strftime('%Y-%m-%dT%H:%M:%SZ'), max_items=20))} recent articles"),
    ]
    for title, fn in checks:
        try:
            r.line(OK, title, fn())
        except (AlpacaError, KeyError, TypeError) as exc:
            msg = exc.message if isinstance(exc, AlpacaError) else f"unexpected reply ({exc})"
            fix = "keep ALPACA_DATA_FEED=iex on the free plan" if "SIP" in str(msg) else \
                "try again in a minute; if it keeps failing, send this output for help"
            r.line(FAIL, title, msg, fix)

    # 6. FRED
    fred = fred or FredClient.from_settings(settings)
    start = (now - timedelta(days=30)).date().isoformat()
    for sid, meta in FRED_SERIES.items():
        try:
            s = fred.series(sid, start)
            if s.empty:
                raise SourceUnavailable("no recent values")
            r.line(OK, f"FRED {meta['name']}", f"{s.iloc[-1]:.2f} on {s.index[-1]:%Y-%m-%d}")
        except SourceUnavailable as exc:
            r.line(WARN, f"FRED {meta['name']}", str(exc),
                   "market conditions will be missing until fred.stlouisfed.org can be reached")

    # 7. model
    meta = model_mod.load_meta(settings.model_dir)
    if meta is None:
        r.line(FAIL, "Trained model", "none yet", "run: python -m algotrader train")
    else:
        sel = meta.get("feature_selection") or {}
        uses = "uses market conditions" if sel.get("chosen") == "context" else "price data only"
        if not sel:
            r.line(WARN, "Trained model", f"trained {meta.get('trained_at', '?')[:10]} by an older version",
                   "retrain to add market conditions: python -m algotrader train --refresh")
        else:
            r.line(OK, "Trained model", f"trained {meta.get('trained_at', '?')[:10]}, {uses}")
    return _finish(r, out)


def _finish(r: Report, out) -> int:
    print("", file=out)
    if r.failed:
        print(f"{r.failed} problem(s) found. Fix the [FAIL] lines above, then run this again.", file=out)
    elif r.warned:
        print(f"Everything essential works ({r.warned} warning(s) above).", file=out)
    else:
        print("Everything checks out.", file=out)
    return 1 if r.failed else 0
