"""Command line: python -m algotrader <command>

  check      verify API keys, account status and market-data access
  doctor     check every data source, the dashboard files and the running server
  fetch      download & cache 5 years of daily bars from Alpaca
  train      fetch (if needed) -> features -> random forest -> OOS backtest
  backtest   re-run the backtest on saved OOS predictions (try new strategy settings fast)
  trade      run one trading cycle now (--dry-run to place no orders)
  bot        run forever, one cycle per trading day after the close
  serve      start the dashboard API (+ built React app)
  demo       offline demo: synthetic market + stand-in broker + dashboard
  users      list accounts, make someone admin, reset a password, delete an account
  db         where the data is stored (status), copy local data into DATABASE_URL (copy-from-sqlite)
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import replace

from .config import load_settings, setup_logging

log = logging.getLogger("algotrader")


def _client(settings):
    from .alpaca_client import AlpacaClient
    if not settings.has_credentials:
        sys.exit("ALPACA_API_KEY and ALPACA_SECRET_KEY are not set. Copy .env.example to backend/.env and fill them in.")
    return AlpacaClient.from_settings(settings)


def cmd_check(settings, args):
    from .alpaca_client import AlpacaError, num
    client = _client(settings)
    print(f"Mode: {settings.mode.upper()}  trading API: {settings.alpaca_trading_url}  data feed: {settings.alpaca_data_feed}")
    try:
        acct = client.get_account()
        print(f"Account {acct.get('account_number')}: status {acct.get('status')}, "
              f"equity ${num(acct.get('equity')):,.2f}, cash ${num(acct.get('cash')):,.2f}")
        clock = client.get_clock()
        print(f"Market is {'OPEN' if clock.get('is_open') else 'closed'}; next open {clock.get('next_open')}")
        from datetime import date, timedelta
        bars = client.get_bars([settings.benchmark], start=(date.today() - timedelta(days=10)).isoformat())
        print(f"Market data OK: {len(bars)} recent daily bars for {settings.benchmark}")
    except AlpacaError as exc:
        sys.exit(f"FAILED: {exc}")
    print("All checks passed.")


def _macro(settings, refresh: bool):
    """FRED data for the market-condition features, or (None, reason) if it can't be had."""
    from .context import SourceUnavailable, load_macro
    if not settings.use_market_context:
        return None, "Market conditions are switched off (USE_MARKET_CONTEXT=false)."
    try:
        macro = load_macro(settings, refresh=refresh)
        log.info("FRED data: %d days, %s -> %s", len(macro), f"{macro.index.min():%Y-%m-%d}", f"{macro.index.max():%Y-%m-%d}")
        return macro, None
    except SourceUnavailable as exc:
        log.warning("Couldn't download FRED data (%s) - training on price data only", exc)
        return None, f"FRED couldn't be reached during training ({exc}), so the model uses price data only."


def cmd_fetch(settings, args):
    from .data import load_history
    df = load_history(settings, _client(settings), refresh=True)
    print(f"Cached {len(df)} rows for {df['symbol'].nunique()} symbols "
          f"({df['date'].min():%Y-%m-%d} -> {df['date'].max():%Y-%m-%d})")
    macro, note = _macro(settings, refresh=True)
    print(note or f"Cached FRED data: {', '.join(macro.columns)} ({macro.index.min():%Y-%m-%d} -> {macro.index.max():%Y-%m-%d})")


def cmd_train(settings, args):
    from .data import load_history
    from .pipeline import report, train_from_bars
    client = _client(settings) if (args.refresh or settings.has_credentials) else None
    bars = load_history(settings, client, refresh=args.refresh)
    macro, note = _macro(settings, refresh=args.refresh)
    result, bt = train_from_bars(settings, bars, macro=macro, refit_all=not args.no_refit, context_note=note)
    print(report(result, bt))
    print(f"\nSaved model to {settings.model_dir}")


def cmd_doctor(settings, args):
    from .doctor import run_doctor
    sys.exit(run_doctor(settings))


def cmd_backtest(settings, args):
    from .data import load_cached_bars
    from .model import load_oos_predictions
    from .pipeline import backtest_from_predictions, report
    bt = backtest_from_predictions(settings, load_oos_predictions(settings.model_dir), load_cached_bars(settings.data_dir))
    print(report(None, bt))
    if args.json:
        print(json.dumps(bt.metrics, indent=2))


def cmd_trade(settings, args):
    from .trader import Trader
    rep = Trader(settings, _client(settings)).run_cycle(force=args.force, dry_run=True if args.dry_run else None)
    print(f"\nStatus: {rep['status']} - {rep.get('message', '')}")
    if rep.get("signals"):
        print(f"\nSignals as of {rep.get('as_of')} (market filter {'ON' if rep.get('market_ok') else 'blocking new entries'}):")
        for s in sorted(rep["signals"], key=lambda r: -(r["proba"] or 0)):
            p = "  n/a" if s["proba"] is None else f"{s['proba']:.3f}"
            print(f"  {s['symbol']:<6} p={p}  {s['action']:<8} {s['reason']}")
    for o in rep.get("orders", []):
        print(f"  ORDER {o['side'].upper():4} {o['qty']:>6} {o['symbol']:<6} {o['status']:<10} {o.get('error') or ''}")
    if rep["status"] == "error":
        sys.exit(1)


def cmd_bot(settings, args):
    from .trader import Trader
    Trader(settings, _client(settings)).run_forever(run_now=args.now)


def cmd_serve(settings, args):
    from . import __version__
    from .api import STALE_BUILD_FIX, create_app, frontend_version
    settings.validate()
    host, port = args.host or settings.api_host, args.port or settings.api_port
    settings = replace(settings, api_host=host)
    fv = frontend_version(settings.frontend_dist)
    if fv and fv.startswith("__"):
        print("WARNING: the dashboard files were rebuilt by an old copy of the build script that is still "
              f"running. To fix: {STALE_BUILD_FIX}.")
    elif fv and fv != __version__:
        print(f"WARNING: the dashboard files are version {fv} but this code is {__version__}. "
              "Rebuild the dashboard: cd frontend && npm run build.")
    settings = settings.guard_exposed_dashboard(exposed=host not in ("127.0.0.1", "localhost", "::1"))
    if settings.manual_trading_note:
        print(settings.manual_trading_note)
    users = None
    if settings.user_accounts:
        from .users import SecretBox, UserStore
        try:
            users = UserStore(settings.users_db, SecretBox.from_settings(settings), settings.session_days)
        except RuntimeError as exc:      # the cryptography package isn't installed
            sys.exit(str(exc))
        if users.count() == 0:
            print("User accounts are on. Open the dashboard and create your account"
                  + (": the first one becomes the admin." if settings.is_local_only else
                     ", then make it the admin with: python -m algotrader users make-admin YOUR_EMAIL"))
    app = create_app(settings, users=users)
    browse = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    print(f"AlgoTrader {__version__} dashboard: http://{browse}:{port}")
    if host in ("0.0.0.0", "::") and not settings.dashboard_password:
        print("WARNING: reachable from other devices on your network with no password. "
              "Set DASHBOARD_PASSWORD, or API_HOST=127.0.0.1 to keep it on this computer.")
    app.run(host=host, port=port, threaded=True)


def cmd_users(settings, args):
    from getpass import getpass

    from .users import AccountError, SecretBox, UserStore
    try:
        store = UserStore(settings.users_db, SecretBox.from_settings(settings), settings.session_days)
    except RuntimeError as exc:
        sys.exit(str(exc))
    try:
        if args.action == "list":
            people = store.list_users()
            if not people:
                print(f"No accounts yet ({store.db.describe()}).")
            for u in people:
                broker = f"connected {u['broker']['account'] or ''}".strip() if u["broker"]["linked"] else "not connected"
                print(f"{u['id']:>4}  {u['email']:<34} {u['name']:<22} {'admin' if u['is_admin'] else 'member':<7} "
                      f"Alpaca {broker:<17} joined {u['created_at'][:10]}")
        elif args.action in ("make-admin", "remove-admin"):
            u = store.set_admin(args.email, args.action == "make-admin")
            print(f"{u['email']} is {'now an admin' if u['is_admin'] else 'no longer an admin'} (takes effect right away).")
        elif args.action == "reset-password":
            new = args.password
            if not new:
                new = getpass("New password: ")
                if getpass("Type it again: ") != new:
                    sys.exit("The two passwords didn't match. Nothing was changed.")
            u = store.reset_password(args.email, new)
            print(f"Password changed for {u['email']}. They are logged out everywhere.")
        elif args.action == "delete":
            u = store.by_email(args.email)
            if u is None:
                sys.exit(f"No account with the email {args.email}.")
            if not args.yes and input(f"Delete {u['email']}, their saved Alpaca keys and purchase history? "
                                      "Type yes: ").strip().lower() != "yes":
                sys.exit("Nothing was deleted.")
            store.delete_user(u["id"])
            print(f"Deleted {u['email']}. Their Alpaca account itself is untouched.")
    except AccountError as exc:
        sys.exit(exc.message)


def cmd_db(settings, args):
    from .db import Database, describe
    from .journal import TABLES as JOURNAL_TABLES
    from .journal import Journal
    from .users import TABLES as USER_TABLES
    from .users import SecretBox, UserStore

    def open_stores(users_target, journal_target):
        return UserStore(users_target, SecretBox.from_settings(settings), settings.session_days), Journal(journal_target)

    if args.action == "status":
        users, journal = open_stores(settings.users_db, settings.journal_db)
        print(f"Accounts: {describe(settings.users_db)}")
        print(f"Bot journal: {describe(settings.journal_db)}")
        for name, db, tables in (("accounts", users.db, USER_TABLES), ("journal", journal.db, JOURNAL_TABLES)):
            print(f"  {name}: " + ", ".join(f"{t} {db.count(t)}" for t in tables))
        print("Connected." if settings.database_url else "Using the local files (set DATABASE_URL to use PostgreSQL/Supabase).")
        return

    # copy-from-sqlite: local files -> DATABASE_URL, only into empty tables
    if not settings.database_url:
        sys.exit("Set DATABASE_URL in backend/.env first (your Supabase connection string), then run this again.")
    target_users, target_journal = open_stores(settings.database_url, settings.database_url)
    busy = [t for t in USER_TABLES if target_users.db.count(t)] + [t for t in JOURNAL_TABLES if target_journal.db.count(t)]
    if busy:
        sys.exit(f"The database already has data ({', '.join(busy)}), so nothing was copied. "
                 "This command only fills an empty database.")
    copied = []
    for path, tables, target in ((settings.users_db_path, USER_TABLES, target_users.db),
                                 (settings.db_path, JOURNAL_TABLES, target_journal.db)):
        if not path.exists():
            continue
        source = Database(path)
        for table in tables:
            with source.connect() as c:
                rows = c.execute(f"SELECT * FROM {table}").fetchall()
            if rows:
                cols = list(rows[0].keys())
                with target.connect() as c:
                    c.executemany(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
                                  [tuple(r[k] for k in cols) for r in rows])
                if "id" in cols:
                    target.reset_ids(table)
            copied.append(f"{table}: {len(rows)}")
    print(f"Copied into {describe(settings.database_url)}: " + (", ".join(copied) or "nothing (no local data)"))
    print("Saved Alpaca keys stay readable only with the same SECRET_KEY / backend/data/secret.key on the server.")


def cmd_demo(settings, args):
    from .demo import run_demo
    run_demo(settings, host=args.host, port=args.port, sessions_to_simulate=args.days, seed=args.seed)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m algotrader", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env-file", help="path to a .env file (default: backend/.env)")
    parser.add_argument("--log-level", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("doctor", help="check every data source and the dashboard, and say how to fix problems")
    sub.add_parser("fetch")
    p = sub.add_parser("train")
    p.add_argument("--refresh", action="store_true", help="re-download data even if cached")
    p.add_argument("--no-refit", action="store_true", help="keep the train-period model instead of refitting on all data")
    p = sub.add_parser("backtest")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("trade")
    p.add_argument("--dry-run", action="store_true", help="compute signals but place no orders")
    p.add_argument("--force", action="store_true", help="run even if today is not a trading day / market open")
    p = sub.add_parser("bot")
    p.add_argument("--now", action="store_true", help="also run one cycle immediately at start-up")
    p = sub.add_parser("serve")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p = sub.add_parser("demo")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--days", type=int, default=63, help="sessions of paper trading to simulate (default 63 = ~3 months)")
    p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("users", help="manage user accounts")
    usub = p.add_subparsers(dest="action", required=True)
    usub.add_parser("list", help="every account, who is admin, who connected Alpaca")
    for name, text in (("make-admin", "let this person see and trade the bot's account"),
                       ("remove-admin", "make this person a normal member again")):
        usub.add_parser(name, help=text).add_argument("email")
    q = usub.add_parser("reset-password", help="set a new password (asks for it)")
    q.add_argument("email")
    q.add_argument("--password", help=argparse.SUPPRESS)
    q = usub.add_parser("delete", help="delete an account and its saved data")
    q.add_argument("email")
    q.add_argument("--yes", action="store_true", help="don't ask for confirmation")
    p = sub.add_parser("db", help="where the data is stored; copy local data into DATABASE_URL")
    dsub = p.add_subparsers(dest="action", required=True)
    dsub.add_parser("status", help="which database is used, and how many rows each table has")
    dsub.add_parser("copy-from-sqlite", help="copy backend/data/*.db into the (empty) DATABASE_URL database")

    args = parser.parse_args(argv)
    setup_logging(args.log_level)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    settings = load_settings(args.env_file)
    try:
        settings.validate()
    except ValueError as exc:
        sys.exit(f"Configuration error: {exc}")
    from .db import DatabaseError
    try:
        {"check": cmd_check, "doctor": cmd_doctor, "fetch": cmd_fetch, "train": cmd_train, "backtest": cmd_backtest,
         "trade": cmd_trade, "bot": cmd_bot, "serve": cmd_serve, "demo": cmd_demo, "users": cmd_users,
         "db": cmd_db}[args.command](settings, args)
    except DatabaseError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
