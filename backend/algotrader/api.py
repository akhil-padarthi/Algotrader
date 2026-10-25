"""Flask API that powers the React dashboard (and serves its built files)."""
from __future__ import annotations

import hmac
import ipaddress
import json
import logging
import math
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from flask import Flask, Response, g, jsonify, request, send_from_directory, stream_with_context

from . import __version__
from . import model as model_mod
from .alpaca_client import AlpacaClient, AlpacaError, num
from .context import (
    DEFAULT_SECTORS, FRED_SERIES, SECTOR_ETFS, FredClient, SourceUnavailable, article_view, load_macro,
    macro_snapshot, news_by_symbol, sector_table,
)
from .journal import Journal
from .performance import PERIOD_SPECS, build_performance, normalize_period
from .pipeline import BACKTEST_FILE
from .trader import BOT_ORDER_PREFIX, OPEN_STATUSES, Trader, cancel_and_close, flatten_orders, next_run_time
from .users import SESSION_COOKIE, AccountError, SecretBox, UserStore

log = logging.getLogger(__name__)
SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")   # AAPL, BRK.B
MANUAL_ORDER_PREFIX = "manual-"
MAX_MANUAL_ORDER = 1_000_000.0
# Stock panel chart ranges: bar size, how far back to fetch, regular-hours window (NY minutes)
STOCK_RANGES = {
    "1D": {"timeframe": "5Min", "days": 6, "hours": (570, 960), "last_session": True},
    "1W": {"timeframe": "30Min", "days": 9, "hours": (570, 960)},
    "1M": {"timeframe": "1Hour", "days": 32, "hours": (540, 960)},
    "3M": {"timeframe": "1Day", "days": 95},
    "1Y": {"timeframe": "1Day", "days": 370},
}


STALE_BUILD_FIX = ("close every window running 'npm run dev' (it says 'Watching src/'), "
                   "then rebuild the dashboard: cd frontend && npm run build")


def frontend_version(dist: Path) -> str | None:
    """Version stamped into the built dashboard (frontend/dist/index.html), if any."""
    try:
        html = (Path(dist) / "index.html").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'<meta name="algotrader-version" content="([^"]+)"', html)
    return m.group(1) if m else None


def context_summary(indicators: list[dict], sectors: list[dict], trend: dict | None) -> str:
    """One plain sentence about today's market, built only from the numbers shown."""
    parts = []
    vix = next((i for i in indicators if i["id"] == "VIXCLS"), None)
    if vix:
        parts.append({"Calm": "Markets are calm", "Normal": "Volatility is normal", "Nervous": "Markets are nervous",
                      "Fearful": "Markets are fearful"}[vix["status"]] + f" (VIX {vix['value']:.1f})")
    if trend:
        parts.append({"Uptrend": "the S&P 500 is in an uptrend", "Downtrend": "the S&P 500 is in a downtrend",
                      "Mixed": "the S&P 500's trend is mixed"}[trend["label"]])
    known = [x for x in sectors if x["above_50d"] is not None]
    if known:
        parts.append(f"{sum(x['above_50d'] for x in known)} of {len(known)} sectors are above their 50-day average")
    if not parts:
        return ""
    text = "; ".join(parts) + "."
    return text[0].upper() + text[1:]


class TTLCache:
    def __init__(self):
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str, ttl: float, fn):
        now = time.monotonic()
        with self._lock:
            hit = self._data.get(key)
            if hit and now - hit[0] < ttl:
                return hit[1]
        value = fn()
        with self._lock:
            self._data[key] = (time.monotonic(), value)
        return value

    def clear(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._data.pop(key, None)


class SetupRequired(Exception):
    pass


class NeedsBroker(Exception):
    """The person is logged in but hasn't connected their Alpaca paper account yet."""


@dataclass
class Acct:
    """The brokerage account a request reads and trades: the bot's (admin) or a person's own."""
    client: AlpacaClient
    ns: str                      # cache namespace
    is_bot: bool
    user_id: int | None = None
    mode: str = "paper"


# reachable without logging in
OPEN_PATHS = {"/api/health", "/api/auth/status", "/api/auth/signup", "/api/auth/login", "/api/auth/logout",
              "/api/cron/trade"}   # the cron route checks its own secret


def _is_loopback(addr: str | None) -> bool:
    try:
        return ipaddress.ip_address(addr or "").is_loopback
    except ValueError:
        return False


def create_app(settings, client: AlpacaClient | None = None, journal: Journal | None = None,
               fred: FredClient | None = None, users: UserStore | None = None, user_broker=None,
               demo_accounts=None, trader_factory=None) -> Flask:
    """The dashboard API.

    users:          the account database (created from settings when user accounts are on)
    user_broker:    (key, secret, url) -> AlpacaClient for a person's own account (tests swap this)
    demo_accounts:  demo only: () -> (key, secret, url) of a fresh stand-in Alpaca account
    trader_factory: () -> Trader for /api/cron/trade (tests swap this)
    """
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = 64 * 1024      # every request body here is a small JSON object
    if client is None and settings.has_credentials:
        client = AlpacaClient.from_settings(settings)
    journal = journal or Journal(settings.journal_db)
    fred = fred or FredClient.from_settings(settings)
    cache = TTLCache()
    tz = ZoneInfo(settings.timezone)
    dist = Path(settings.frontend_dist)
    accounts_on = settings.user_accounts
    if accounts_on and users is None:
        users = UserStore(settings.users_db, SecretBox.from_settings(settings), settings.session_days)
    make_user_client = user_broker or (lambda key, secret, url: AlpacaClient(
        key, secret, url, settings.alpaca_data_url, settings.alpaca_data_feed))
    user_clients: dict[int, tuple[tuple, AlpacaClient]] = {}
    clients_lock = threading.Lock()
    member_mode = "demo" if settings.demo_mode else "paper"

    def broker() -> AlpacaClient:
        """The server's own Alpaca account: the bot's account, and the source of market data."""
        if client is None:
            raise SetupRequired()
        return client

    def current_acct() -> Acct:
        user = g.get("user")
        if not accounts_on or (user and user["is_admin"]):
            return Acct(broker(), "bot", True, user["id"] if user else None, settings.mode)
        creds = users.alpaca_credentials(user["id"])
        if creds is None:
            raise NeedsBroker()
        with clients_lock:
            hit = user_clients.get(user["id"])
            if hit is None or hit[0] != creds:
                hit = (creds, make_user_client(*creds))
                user_clients[user["id"]] = hit
        return Acct(hit[1], f"u{user['id']}", False, user["id"], member_mode)

    def forget_account(ns: str, user_id: int | None = None) -> None:
        cache.clear(*(f"{ns}:{k}" for k in ("account", "positions", "open_orders", "owners")),
                    *(f"{ns}:perf:{p}" for p in PERIOD_SPECS))
        if user_id is not None:
            with clients_lock:
                user_clients.pop(user_id, None)

    def is_admin() -> bool:
        return not accounts_on or bool(g.get("user") and g.user["is_admin"])

    def user_view(user: dict) -> dict:
        admin = bool(user["is_admin"])
        return {**user, "account_kind": "bot" if admin else "member", "mode": settings.mode if admin else member_mode}

    # ------------------------------------------------------------ middleware
    @app.before_request
    def basic_auth():
        if not settings.dashboard_password or request.path == "/api/health" or request.method == "OPTIONS":
            return None
        auth = request.authorization
        if auth and auth.username == settings.dashboard_user and auth.password == settings.dashboard_password:
            return None
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="AlgoTrader"'})

    @app.before_request
    def only_from_dashboard():
        """Browsers let any website send simple POSTs to this server, so every change must be JSON with a
        custom header (which forces a CORS preflight other sites fail) and must not come from a foreign Origin."""
        if request.method != "POST" or not request.path.startswith("/api/"):
            return None
        origin = request.headers.get("Origin")
        foreign = origin and origin.rstrip("/") != request.host_url.rstrip("/") and origin not in settings.cors_origins
        if request.headers.get("X-AlgoTrader") != "1" or not request.is_json or foreign:
            return jsonify({"error": "Trades must come from the AlgoTrader dashboard"
                            if request.path == "/api/trade" else "This must come from the AlgoTrader dashboard"}), 403
        return None

    @app.before_request
    def login_gate():
        g.user, g.token = None, None
        if not accounts_on or not request.path.startswith("/api/") or request.method == "OPTIONS":
            return None
        token = request.cookies.get(SESSION_COOKIE)
        user = users.user_for_session(token)
        if user:
            g.user, g.token = user, token
        if user or request.path in OPEN_PATHS:
            return None
        return jsonify({"error": "Please log in to continue.", "login_required": True}), 401

    @app.after_request
    def cors(resp):
        origin = request.headers.get("Origin")
        if origin and ("*" in settings.cors_origins or origin in settings.cors_origins):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, X-AlgoTrader"
            resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            resp.headers["Vary"] = "Origin"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(SetupRequired)
    def setup_required(_):
        return jsonify({"error": "Alpaca API keys are not configured. Add ALPACA_API_KEY and "
                                 "ALPACA_SECRET_KEY to backend/.env and restart.", "setup_required": True}), 503

    @app.errorhandler(Exception)
    def unexpected_error(exc: Exception):
        """Never answer an /api request with an HTML error page: say what went wrong."""
        from werkzeug.exceptions import HTTPException
        if isinstance(exc, HTTPException):
            if request.path.startswith("/api/"):
                return jsonify({"error": exc.description or exc.name}), exc.code
            return exc
        log.exception("Error handling %s", request.path)
        return jsonify({"error": f"The server hit an error: {type(exc).__name__}: {exc}"}), 500

    @app.errorhandler(AccountError)
    def account_error(exc: AccountError):
        return jsonify({"error": exc.message}), exc.status

    @app.errorhandler(NeedsBroker)
    def needs_broker(_):
        return jsonify({"error": "Connect your Alpaca paper account first. It's where your purchases go.",
                        "needs_broker": True}), 409

    @app.errorhandler(AlpacaError)
    def alpaca_error(exc: AlpacaError):
        rejected = exc.status in (401, 403) and "auth" in (exc.message or "").lower()
        if rejected and not is_admin():
            msg = ("Alpaca didn't accept your saved keys (they may have been regenerated). Open your profile and "
                   "connect your Alpaca paper account again.")
        elif rejected:
            msg = "Alpaca rejected the API keys (check ALPACA_API_KEY / ALPACA_SECRET_KEY and ALPACA_PAPER)"
        else:
            msg = exc.message
        return jsonify({"error": msg, "alpaca_status": exc.status}), 502

    # ------------------------------------------------------------ data helpers
    def account_data(acct: Acct) -> dict:
        a = cache.get(f"{acct.ns}:account", 2, lambda: acct.client.get_account())
        equity, last = num(a.get("equity")), num(a.get("last_equity"))
        number = str(a.get("account_number", ""))
        return {
            "equity": equity, "cash": num(a.get("cash")), "buying_power": num(a.get("buying_power")),
            "last_equity": last, "day_pl": equity - last, "day_pl_pct": (equity / last - 1) if last else 0.0,
            "long_market_value": num(a.get("long_market_value")), "status": a.get("status"),
            "currency": a.get("currency", "USD"),
            "account_number": ("•••" + number[-4:]) if len(number) > 4 else number,
            "trading_blocked": bool(a.get("trading_blocked")),
        }

    def open_orders(acct: Acct) -> list[dict]:
        return cache.get(f"{acct.ns}:open_orders", 3, lambda: [
            o for o in flatten_orders(acct.client.list_orders(status="open", limit=500))
            if o.get("status") in OPEN_STATUSES
        ])

    def owners(acct: Acct) -> dict[str, str]:
        """symbol -> 'bot' or 'you', from who placed the most recent filled buy (same rule the bot uses).
        In a person's own account everything is theirs."""
        if not acct.is_bot:
            return {}

        def load():
            bot_ids, found = journal.bot_order_ids(), {}
            for o in flatten_orders(acct.client.list_orders(status="closed", limit=200)):   # newest first
                sym = o.get("symbol")
                if sym in found or o.get("side") != "buy" or not o.get("filled_at"):
                    continue
                by_bot = str(o.get("client_order_id", "")).startswith(BOT_ORDER_PREFIX) or o.get("id") in bot_ids
                found[sym] = "bot" if by_bot else "you"
            return found
        return cache.get(f"{acct.ns}:owners", 15, load)

    def positions_data(acct: Acct) -> list[dict]:
        raw = cache.get(f"{acct.ns}:positions", 2, lambda: acct.client.get_positions())
        owner_of = owners(acct) if raw else {}
        protect: dict[str, dict] = {}
        for o in open_orders(acct):
            if o.get("side") == "sell":
                if o.get("type") == "market":
                    protect.setdefault(o["symbol"], {})["pending_exit"] = True
                elif o.get("type") in ("stop", "stop_limit") and o.get("stop_price"):
                    protect.setdefault(o["symbol"], {})["stop_price"] = num(o["stop_price"])
                elif o.get("type") == "limit" and o.get("limit_price"):
                    protect.setdefault(o["symbol"], {})["take_profit_price"] = num(o["limit_price"])
        out = []
        for p in raw:
            sym = p["symbol"]
            out.append({
                "symbol": sym, "qty": num(p.get("qty")), "side": p.get("side", "long"),
                "avg_entry_price": num(p.get("avg_entry_price")), "current_price": num(p.get("current_price")),
                "market_value": num(p.get("market_value")), "cost_basis": num(p.get("cost_basis")),
                "unrealized_pl": num(p.get("unrealized_pl")), "unrealized_plpc": num(p.get("unrealized_plpc")),
                "change_today": num(p.get("change_today")),
                "stop_price": protect.get(sym, {}).get("stop_price"),
                "take_profit_price": protect.get(sym, {}).get("take_profit_price"),
                "pending_exit": bool(protect.get(sym, {}).get("pending_exit")),
                "managed": acct.is_bot and sym in settings.symbols and owner_of.get(sym) == "bot",
                "owner": owner_of.get(sym, "you"),
            })
        return sorted(out, key=lambda x: -x["market_value"])

    def quotes() -> dict:
        snaps = cache.get("snapshots", 3, lambda: broker().get_snapshots(settings.all_symbols))
        out = {}
        for sym, s in snaps.items():
            if not s:
                continue
            trade = s.get("latestTrade") or {}
            daily = s.get("dailyBar") or {}
            prev = s.get("prevDailyBar") or {}
            price = trade.get("p") or daily.get("c")
            prev_close = prev.get("c")
            out[sym] = {
                "symbol": sym, "price": price, "prev_close": prev_close,
                "change": (price - prev_close) if price and prev_close else None,
                "change_pct": (price / prev_close - 1) if price and prev_close else None,
                "day_high": daily.get("h"), "day_low": daily.get("l"), "volume": daily.get("v"),
                "time": trade.get("t"),
            }
        return out

    def sparklines() -> dict:
        def load():
            # anchor on the broker clock (also correct when the demo replays history)
            now = pd.Timestamp(broker().get_clock()["timestamp"])
            start = (now - pd.Timedelta(days=60)).date().isoformat()
            bars = broker().get_bars(settings.all_symbols, start=start)
            if bars.empty:
                return {}
            return {sym: g.sort_values("timestamp")["close"].tail(30).round(2).tolist()
                    for sym, g in bars.groupby("symbol")}
        return cache.get("sparklines", 300, load)

    # ------------------------------------------------------------- endpoints
    @app.get("/api/health")
    def health():
        return jsonify({
            "status": "ok", "version": __version__, "frontend_version": frontend_version(dist),
            "mode": settings.mode,
            "alpaca_configured": client is not None,
            "accounts": accounts_on, "signups": accounts_on and settings.allow_signups,
            "live_stream": settings.live_stream,
            "model_trained": (Path(settings.model_dir) / model_mod.MODEL_FILE).exists(),
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    @app.get("/api/config")
    def config():
        s, m = settings.strategy, settings.model
        return jsonify({
            "mode": settings.mode, "symbols": settings.symbols, "benchmark": settings.benchmark,
            "trading_enabled": settings.trading_enabled, "trade_time": settings.trade_time,
            "manual_trading": settings.manual_trading,
            "news_check": settings.news_check, "news_lookback_days": settings.news_lookback_days,
            "timezone": settings.timezone, "data_feed": settings.alpaca_data_feed,
            "stream_interval_seconds": settings.stream_interval_seconds,
            "strategy": s.__dict__, "model": m.__dict__,
        })

    @app.get("/api/account")
    def account():
        return jsonify(account_data(current_acct()))

    @app.get("/api/positions")
    def positions():
        return jsonify(positions_data(current_acct()))

    @app.get("/api/orders")
    def orders():
        acct = current_acct()
        limit = min(int(request.args.get("limit", 50)), 200)
        raw = acct.client.list_orders(status="all", limit=limit)
        bot_ids, manual_ids = (journal.bot_order_ids(), journal.manual_order_ids()) if acct.is_bot else (set(), set())
        out = []
        for o in raw:
            legs = o.get("legs") or []
            out.append({
                "id": o.get("id"), "client_order_id": o.get("client_order_id"), "symbol": o.get("symbol"),
                "side": o.get("side"), "type": o.get("type"), "order_class": o.get("order_class"),
                "qty": num(o.get("qty")), "filled_qty": num(o.get("filled_qty")),
                "notional": num(o.get("notional"), None),
                "filled_avg_price": num(o.get("filled_avg_price"), None),
                "limit_price": num(o.get("limit_price"), None), "stop_price": num(o.get("stop_price"), None),
                "status": o.get("status"), "submitted_at": o.get("submitted_at"), "filled_at": o.get("filled_at"),
                "bot": acct.is_bot and (o.get("id") in bot_ids or str(o.get("client_order_id", "")).startswith("algo-")),
                "manual": acct.is_bot and (o.get("id") in manual_ids
                                           or str(o.get("client_order_id", "")).startswith(MANUAL_ORDER_PREFIX)),
                "legs": [{"type": leg.get("type"), "status": leg.get("status"),
                          "limit_price": num(leg.get("limit_price"), None),
                          "stop_price": num(leg.get("stop_price"), None),
                          "filled_avg_price": num(leg.get("filled_avg_price"), None),
                          "filled_at": leg.get("filled_at")} for leg in legs],
            })
        return jsonify(out)

    @app.get("/api/performance")
    def performance():
        period = normalize_period(request.args.get("period", "1M"))
        if period is None:
            return jsonify({"error": f"period must be one of {sorted(PERIOD_SPECS)}"}), 400
        acct = current_acct()
        return jsonify(cache.get(f"{acct.ns}:perf:{period}", 20 if period == "1D" else 60,
                                 lambda: build_performance(acct.client, period, settings.benchmark, broker_now(), tz)))

    @app.get("/api/market")
    def market():
        q = quotes()
        spark = sparklines()
        sig = {s["symbol"]: s for s in viewer_signals()["signals"]}
        try:
            names = {a["symbol"]: a["name"] for a in asset_index()}
        except AlpacaError:
            names = {}
        items = []
        for sym in settings.all_symbols:
            row = dict(q.get(sym, {"symbol": sym}))
            row["sparkline"] = spark.get(sym, [])
            row["is_benchmark"] = sym == settings.benchmark
            row["name"] = names.get(sym, "")
            s = sig.get(sym)
            row["signal"] = {"proba": s["proba"], "action": s["action"]} if s else None
            items.append(row)
        return jsonify({"items": items, "time": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    def viewer_signals() -> dict:
        """The model's latest signals. The bot's actions (hold, sell, held) are about the bot's own
        positions, so other people see the model's view instead: buy zone, neutral or sell zone."""
        out = journal.latest_signals()
        if is_admin():
            return {**out, "audience": "bot"}
        s = settings.strategy
        try:
            held = {p["symbol"] for p in positions_data(current_acct())}
        except (NeedsBroker, AlpacaError, AccountError):
            held = set()

        def zone(p):
            if p is None:
                return "HOLD"
            return "BUY" if p >= s.entry_threshold else "SELL" if p < s.exit_threshold else "HOLD"
        out["signals"] = [{"symbol": x["symbol"], "proba": x["proba"], "price": x.get("price"),
                           "action": zone(x["proba"]), "reason": "", "held": x["symbol"] in held}
                          for x in out["signals"]]
        return {**out, "audience": "member"}

    @app.get("/api/signals")
    def signals():
        out = viewer_signals()
        out["entry_threshold"] = settings.strategy.entry_threshold
        out["exit_threshold"] = settings.strategy.exit_threshold
        return jsonify(out)

    @app.get("/api/bot")
    def bot():
        if not is_admin():
            raise AccountError("Only the admin can see the bot's account.", 403)
        nxt = next_run_time(settings.trade_time, tz)
        return jsonify({
            "trading_enabled": settings.trading_enabled, "trade_time": settings.trade_time,
            "timezone": settings.timezone, "next_run": nxt.isoformat(), "mode": settings.mode,
            "runs": journal.recent_runs(15), "orders": journal.recent_orders(30),
        })

    @app.get("/api/model")
    def model_info():
        meta = model_mod.load_meta(settings.model_dir)
        if meta is None:
            return jsonify({"trained": False, "error": "No model yet - run: python -m algotrader train"}), 404
        return jsonify({"trained": True, **meta})

    @app.get("/api/backtest")
    def backtest():
        path = Path(settings.model_dir) / BACKTEST_FILE
        if not path.exists():
            return jsonify({"error": "No backtest yet - run: python -m algotrader train"}), 404
        return Response(path.read_text(), mimetype="application/json")

    @app.get("/api/stream")
    def stream():
        """Server-Sent Events: a fresh account/positions/prices snapshot every few seconds."""
        once = request.args.get("once") == "1" or not settings.live_stream   # serverless hosts: one snapshot
        interval = max(1.0, settings.stream_interval_seconds)
        try:
            acct = current_acct()
        except NeedsBroker:
            acct = None          # still stream prices for the ticker
        except SetupRequired:
            acct = False

        def snapshot() -> dict:
            try:
                if acct is False:
                    raise SetupRequired()
                return {"type": "snapshot", "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "account": account_data(acct) if acct else None,
                        "positions": positions_data(acct) if acct else None, "needs_broker": acct is None,
                        "quotes": {k: {"price": v["price"], "change_pct": v["change_pct"]} for k, v in quotes().items()}}
            except SetupRequired:
                return {"type": "error", "error": "setup_required"}
            except AlpacaError as exc:
                return {"type": "error", "error": exc.message}

        @stream_with_context
        def gen():
            yield "retry: 5000\n\n"
            while True:
                yield f"data: {json.dumps(snapshot())}\n\n"
                if once:
                    return
                time.sleep(interval)

        return Response(gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})



    # -------------------------------------------- market conditions and news
    def recent_news(symbols: list[str], days: int, max_items: int) -> list[dict]:
        key = f"news:{','.join(sorted(symbols))}:{days}:{max_items}"

        def load():
            since = (broker_now() - pd.Timedelta(days=days)).tz_convert("UTC")
            raw = broker().get_news(symbols, start=since.strftime("%Y-%m-%dT%H:%M:%SZ"), max_items=max_items)
            return [article_view(a) for a in raw]
        return cache.get(key, 180, load)

    def sector_of(symbol: str) -> dict | None:
        etf = settings.sector_map.get(symbol) or settings.sector_overrides.get(symbol) or DEFAULT_SECTORS.get(symbol)
        if not etf or etf not in SECTOR_ETFS:
            return None
        return {"etf": etf, "name": SECTOR_ETFS[etf]}

    @app.get("/api/context")
    def market_context():
        """Market conditions from FRED (St. Louis Fed) and sector ETFs, with sources."""
        def load():
            problems, indicators, macro_asof = [], [], None
            try:
                macro = load_macro(settings, fred, years=2)
                indicators = macro_snapshot(macro)
                macro_asof = max((i["date"] for i in indicators), default=None)
            except SourceUnavailable as exc:
                problems.append(f"FRED: {exc}")
            sectors, trend, bars_asof = [], None, None
            try:
                start = (broker_now() - pd.Timedelta(days=320)).date().isoformat()
                raw = broker().get_bars(list(SECTOR_ETFS) + [settings.benchmark], start=start)
                if not raw.empty:
                    from .data import clean_bars
                    bars = clean_bars(raw)
                    sectors = sector_table(bars)
                    spy = bars[bars["symbol"] == settings.benchmark].sort_values("date")["close"].astype(float)
                    if len(spy) >= 50:
                        last = float(spy.iloc[-1])
                        sma50 = float(spy.tail(50).mean())
                        sma200 = float(spy.tail(200).mean()) if len(spy) >= 200 else None
                        trend = {
                            "symbol": settings.benchmark, "price": round(last, 2),
                            "vs_50d": last / sma50 - 1, "vs_200d": (last / sma200 - 1) if sma200 else None,
                            "label": ("Uptrend" if last > sma50 and (sma200 is None or last > sma200) else
                                      "Downtrend" if last < sma50 and (sma200 is None or last < sma200) else "Mixed"),
                        }
                    bars_asof = bars["date"].max().strftime("%Y-%m-%d")
            except AlpacaError as exc:
                problems.append(f"Sector prices: {exc.message}")
            return {
                "indicators": indicators, "sectors": sectors, "trend": trend,
                "summary": context_summary(indicators, sectors, trend),
                "problems": problems,
                "sources": [
                    {"name": "Federal Reserve Bank of St. Louis (FRED)", "url": "https://fred.stlouisfed.org",
                     "used_for": "VIX, Treasury yields, yield curve, credit spread", "as_of": macro_asof,
                     "ok": bool(indicators)},
                    {"name": "Alpaca Markets", "url": "https://alpaca.markets",
                     "used_for": f"stock, S&P 500 and sector ETF prices ({settings.alpaca_data_feed.upper()} feed)",
                     "as_of": bars_asof, "ok": bars_asof is not None},
                    {"name": "Benzinga (via Alpaca)", "url": "https://www.benzinga.com",
                     "used_for": "company news", "as_of": None, "ok": True},
                ],
                "series": {k: v["name"] for k, v in FRED_SERIES.items()},
            }
        return jsonify(cache.get("context", 600, load))

    @app.get("/api/news")
    def news():
        one = request.args.get("symbol")
        try:
            symbols = [stock_symbol(one)] if one else list(settings.symbols)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        days = max(1, min(int(request.args.get("days", 7)), 30))
        articles = recent_news(symbols, days, 60 if one else 150)
        since = broker_now() - pd.Timedelta(days=settings.news_lookback_days)
        return jsonify({
            "articles": articles[: int(request.args.get("limit", 12))],
            "by_symbol": news_by_symbol(articles, symbols, since=since),
            "lookback_days": settings.news_lookback_days, "news_check": settings.news_check,
            "source": "Benzinga via Alpaca",
        })

    # ------------------------------------------------- stocks, search, trading
    def broker_now() -> pd.Timestamp:
        """The broker's clock (correct for the live API and for the demo's replayed history)."""
        return pd.Timestamp(clock_data()["timestamp"]).tz_convert(tz)

    def clock_data() -> dict:
        return cache.get("clock", 30, lambda: broker().get_clock())

    def asset_index() -> list[dict]:
        def load():
            out = []
            for a in broker().get_assets():
                if a.get("status") == "active" and a.get("tradable") and a.get("symbol"):
                    out.append({"symbol": a["symbol"], "name": a.get("name") or "", "exchange": a.get("exchange", ""),
                                "fractionable": bool(a.get("fractionable"))})
            return out
        return cache.get("assets", 6 * 3600, load)

    def asset_info(symbol: str) -> dict | None:
        for a in asset_index():
            if a["symbol"] == symbol:
                return a
        try:
            a = cache.get(f"asset:{symbol}", 3600, lambda: broker().get_asset(symbol))
        except AlpacaError as exc:
            if exc.status in (404, 422):
                return None
            raise
        return {"symbol": a["symbol"], "name": a.get("name") or "", "exchange": a.get("exchange", ""),
                "fractionable": bool(a.get("fractionable")), "tradable": bool(a.get("tradable"))}

    def quote_for(symbol: str) -> dict | None:
        snaps = cache.get(f"snap:{symbol}", 3, lambda: broker().get_snapshots([symbol]))
        s = snaps.get(symbol)
        if not s:
            return None
        trade, daily, prev = s.get("latestTrade") or {}, s.get("dailyBar") or {}, s.get("prevDailyBar") or {}
        price = trade.get("p") or daily.get("c")
        prev_close = prev.get("c")
        return {
            "price": price, "prev_close": prev_close,
            "change": (price - prev_close) if price and prev_close else None,
            "change_pct": (price / prev_close - 1) if price and prev_close else None,
            "open": daily.get("o"), "high": daily.get("h"), "low": daily.get("l"), "volume": daily.get("v"),
            "time": trade.get("t") or daily.get("t"),
        }

    def fetch_bars(symbol: str, timeframe: str, days: int, hours=None, last_session=False) -> list[dict]:
        start = (broker_now() - pd.Timedelta(days=days)).date().isoformat()
        bars = broker().get_bars([symbol], start=start, timeframe=timeframe)
        if bars.empty:
            return []
        bars = bars.assign(local=bars["timestamp"].dt.tz_convert(tz))
        if timeframe == "1Day":
            bars = bars.assign(local=bars["local"].dt.normalize())
        if hours:   # regular session only: pre/after-market prints are thin and jumpy
            minutes = bars["local"].dt.hour * 60 + bars["local"].dt.minute
            bars = bars[(minutes >= hours[0]) & (minutes < hours[1])]
        if last_session and not bars.empty:
            last_day = bars["local"].dt.date.max()
            bars = bars[bars["local"].dt.date == last_day]
        return [{"t": t.isoformat(), "c": round(float(c), 4)} for t, c in zip(bars["local"], bars["close"])]

    def stock_bars(symbol: str, rng: str) -> dict:
        """Chart data for one stock. If Alpaca refuses intraday bars, fall back to daily closes
        (and say so) instead of leaving the chart empty."""
        spec = STOCK_RANGES[rng]

        def load():
            try:
                return {"bars": fetch_bars(symbol, spec["timeframe"], spec["days"], spec.get("hours"),
                                           spec.get("last_session", False)),
                        "timeframe": spec["timeframe"], "note": None}
            except AlpacaError as exc:
                if spec["timeframe"] == "1Day":
                    raise
                log.warning("Intraday bars for %s unavailable (%s) - using daily closes", symbol, exc.message)
                return {"bars": fetch_bars(symbol, "1Day", 45), "timeframe": "1Day",
                        "note": f"Alpaca didn't return intraday prices ({exc.message}), so this shows daily closes."}
        return cache.get(f"bars:{symbol}:{rng}", 20 if rng == "1D" else 120, load)

    def stock_symbol(raw: str) -> str:
        symbol = (raw or "").strip().upper()
        if not SYMBOL_RE.match(symbol):
            raise ValueError(f"'{raw}' is not a valid stock symbol")
        return symbol

    @app.get("/api/clock")
    def market_clock():
        c = clock_data()
        return jsonify({"is_open": bool(c.get("is_open")), "next_open": c.get("next_open"),
                        "next_close": c.get("next_close"), "timestamp": c.get("timestamp")})

    @app.get("/api/assets/search")
    def asset_search():
        q = (request.args.get("q") or "").strip()
        if not q:
            return jsonify([])
        qu, ql = q.upper(), q.lower()
        ranked = []
        for a in asset_index():
            sym, name = a["symbol"], a["name"].lower()
            if sym == qu:
                rank = 0
            elif a["exchange"] == "OTC":
                continue          # over-the-counter shares only on an exact symbol match
            elif sym.startswith(qu):
                rank = 1
            elif any(word.startswith(ql) for word in re.split(r"[\s,.\-&/]+", name)):
                rank = 2
            elif len(ql) >= 3 and ql in name:
                rank = 3
            else:
                continue
            ranked.append((rank, len(sym), sym, a))
        ranked.sort(key=lambda r: r[:3])
        return jsonify([{"symbol": a["symbol"], "name": a["name"], "exchange": a["exchange"],
                         "in_universe": a["symbol"] in settings.all_symbols} for *_, a in ranked[:8]])

    @app.get("/api/stock/<raw_symbol>")
    def stock(raw_symbol):
        """Everything the stock panel shows. Each part is fetched on its own, so one failing
        source (say, the quote) still leaves the chart and the rest working."""
        try:
            symbol = stock_symbol(raw_symbol)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        rng = (request.args.get("range") or "1D").upper()
        if rng not in STOCK_RANGES:
            return jsonify({"error": f"range must be one of {list(STOCK_RANGES)}"}), 400
        problems: list[str] = []

        def attempt(what: str, fn, default=None):
            try:
                return fn()
            except SetupRequired:
                raise
            except AlpacaError as exc:
                problems.append(f"{what}: {exc.message}")
            except Exception as exc:  # keep the rest of the panel working
                log.exception("stock panel: %s for %s failed", what, symbol)
                problems.append(f"{what}: {type(exc).__name__}: {exc}")
            return default

        info = attempt("company details", lambda: asset_info(symbol), default=False)
        if info is None:
            return jsonify({"error": f"No US stock found with the symbol {symbol}"}), 404
        info = info or {"symbol": symbol, "name": "", "exchange": "", "fractionable": True}
        chart = attempt("price history", lambda: stock_bars(symbol, rng))
        try:
            acct = current_acct()
        except NeedsBroker:
            acct = None
        sig = next((x for x in viewer_signals()["signals"] if x["symbol"] == symbol), None)
        pos = attempt("your position", lambda: next((x for x in positions_data(acct) if x["symbol"] == symbol), None)) \
            if acct else None
        articles = attempt("news", lambda: recent_news([symbol], 7, 40), default=None)
        return jsonify({
            **info, "range": rng,
            "timeframe": chart["timeframe"] if chart else STOCK_RANGES[rng]["timeframe"],
            "bars": chart["bars"] if chart else None,
            "chart_note": chart["note"] if chart else None,
            "chart_error": None if chart else next((p for p in problems if p.startswith("price history")), None),
            "quote": attempt("latest price", lambda: quote_for(symbol)),
            "position": pos, "signal": sig,
            "in_universe": symbol in settings.symbols, "is_benchmark": symbol == settings.benchmark,
            "market_open": bool(attempt("market clock", lambda: clock_data().get("is_open"), default=False)),
            "trading": {"enabled": settings.manual_trading, "mode": acct.mode if acct else member_mode,
                        "note": settings.manual_trading_note or None, "needs_broker": acct is None},
            "account_kind": None if acct is None else ("bot" if acct.is_bot else "member"),
            "signal_audience": "bot" if is_admin() else "member",
            "news": None if articles is None else {
                "articles": articles[:6],
                "summary": news_by_symbol(articles, [symbol],
                                          since=broker_now() - pd.Timedelta(days=settings.news_lookback_days))[symbol],
                "source": "Benzinga via Alpaca", "lookback_days": settings.news_lookback_days,
            },
            "sector": sector_of(symbol),
            "problems": problems,
        })

    @app.get("/api/stock/<raw_symbol>/quote")
    def stock_quote(raw_symbol):
        try:
            symbol = stock_symbol(raw_symbol)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"symbol": symbol, "quote": quote_for(symbol),
                        "market_open": bool(clock_data().get("is_open"))})

    @app.post("/api/trade")
    def trade():
        """Buy a dollar amount or sell a whole position, in the logged-in person's own Alpaca account
        (the admin trades the bot's account). Only reachable from the dashboard (see only_from_dashboard)."""
        if not settings.manual_trading:
            return jsonify({"error": settings.manual_trading_note
                            or "Buying and selling from the dashboard is turned off (MANUAL_TRADING=false)"}), 403

        body = request.get_json(silent=True) or {}
        try:
            symbol = stock_symbol(str(body.get("symbol", "")))
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        side = body.get("side")
        acct = current_acct()
        c = acct.client
        market_open = bool(clock_data().get("is_open"))
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        coid = f"{MANUAL_ORDER_PREFIX}{stamp}-{symbol}-{side}"
        as_of = broker_now().strftime("%Y-%m-%d")
        try:
            if side == "buy":
                try:
                    amount = float(body.get("amount"))
                except (TypeError, ValueError):
                    return jsonify({"error": "Enter the dollar amount to invest"}), 400
                if not math.isfinite(amount) or amount < 1:
                    return jsonify({"error": "The smallest order is $1"}), 400
                if amount > MAX_MANUAL_ORDER:
                    return jsonify({"error": f"Orders are limited to ${MAX_MANUAL_ORDER:,.0f} from the dashboard"}), 400
                info = asset_info(symbol)
                if info is None or info.get("tradable") is False:
                    return jsonify({"error": f"{symbol} can't be traded"}), 400
                quote = quote_for(symbol) or {}
                price = quote.get("price")
                if info["fractionable"]:
                    order = c.submit_order(symbol, "buy", notional=round(amount, 2), type="market",
                                           time_in_force="day", client_order_id=coid)
                    qty_text, qty = f"${amount:,.2f} of {symbol}", None
                else:
                    if not price:
                        return jsonify({"error": f"No current price for {symbol}"}), 400
                    qty = math.floor(amount / price)
                    if qty < 1:
                        return jsonify({"error": f"{symbol} can't be bought in fractions. One share costs about "
                                                 f"${price:,.2f}, so enter at least that much."}), 400
                    order = c.submit_order(symbol, "buy", qty=qty, type="market", time_in_force="day",
                                           client_order_id=coid)
                    qty_text = f"{qty} share{'s' if qty != 1 else ''} of {symbol}"
                record = dict(symbol=symbol, side="buy", qty=qty, amount=round(amount, 2), price=price,
                              client_order_id=coid, order=order)
                message = f"Buy order placed: {qty_text}."
            elif side == "sell_all":
                pos = next((x for x in positions_data(acct) if x["symbol"] == symbol), None)
                if not pos or pos["qty"] <= 0:
                    return jsonify({"error": f"You don't own any {symbol}"}), 400
                order = cancel_and_close(c, symbol, open_orders(acct))
                record = dict(symbol=symbol, side="sell", qty=pos["qty"], amount=pos["market_value"],
                              price=pos["current_price"], client_order_id=order.get("client_order_id"), order=order)
                message = f"Sell order placed: all {pos['qty']:g} shares of {symbol}."
            else:
                return jsonify({"error": "side must be 'buy' or 'sell_all'"}), 400
        except AlpacaError as exc:
            return jsonify({"error": exc.message or "Alpaca rejected the order"}), 400
        finally:
            forget_account(acct.ns)
        message += " It fills in a moment." if market_open else " The market is closed, so it fills when it opens."
        order = record.pop("order")
        if acct.is_bot:
            journal.add_order(None, symbol=symbol, side=record["side"], qty=record["qty"], reason="manual",
                              price=record["price"], client_order_id=record["client_order_id"],
                              alpaca_order_id=order.get("id"), status=order.get("status"), as_of=as_of)
        else:
            users.record_trade(acct.user_id, symbol=symbol, side=record["side"], amount=record["amount"],
                               qty=record["qty"], price=record["price"], alpaca_order_id=order.get("id"),
                               status=order.get("status"), message=message)
        return jsonify({"status": order.get("status"), "order_id": order.get("id"), "symbol": symbol,
                        "side": side, "message": message, "market_open": market_open, "mode": acct.mode})

    # ------------------------------------------------------ accounts and profile
    def accounts_required():
        if not accounts_on:
            raise AccountError("User accounts are switched off on this server (USER_ACCOUNTS=false).", 404)

    def client_ip() -> str:
        """The visitor's address. Behind Vercel every request comes from the proxy, so its headers are used."""
        if settings.trust_proxy:
            for header in ("X-Real-IP", "X-Vercel-Forwarded-For", "X-Forwarded-For"):
                value = (request.headers.get(header) or "").split(",")[0].strip()
                if value:
                    return value[:64]
        return request.remote_addr or ""

    def logged_in(user: dict, status: int = 200) -> Response:
        token = users.create_session(user["id"], request.headers.get("User-Agent", ""))
        resp = jsonify({"user": user_view(user)})
        resp.status_code = status
        secure = request.is_secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"
        resp.set_cookie(SESSION_COOKIE, token, max_age=settings.session_days * 86400, httponly=True,
                        samesite="Lax", secure=secure, path="/")
        return resp

    def body_text(body: dict, key: str) -> str:
        value = body.get(key)
        return value if isinstance(value, str) else ""

    @app.get("/api/auth/status")
    def auth_status():
        n = users.count() if accounts_on else 0
        return jsonify({
            "accounts": accounts_on, "signups": accounts_on and settings.allow_signups, "has_users": n > 0,
            "first_account_is_admin": accounts_on and n == 0 and settings.is_local_only,
            "demo_accounts": demo_accounts is not None,
            "user": user_view(g.user) if g.get("user") else None,
        })

    @app.post("/api/auth/signup")
    def signup():
        accounts_required()
        if not settings.allow_signups:
            raise AccountError("New sign-ups are closed on this site. Ask the admin for an account.", 403)
        body = request.get_json(silent=True) or {}
        # the first account becomes the admin only on a dashboard no other device can reach
        first_admin = users.count() == 0 and settings.is_local_only and _is_loopback(request.remote_addr)
        user = users.create_user(body_text(body, "email"), body_text(body, "name"), body_text(body, "password"),
                                 ip=client_ip(), is_admin=first_admin)
        log.info("New account %s (%s)", user["id"], "admin" if first_admin else "member")
        return logged_in(user, 201)

    @app.post("/api/auth/login")
    def login():
        accounts_required()
        body = request.get_json(silent=True) or {}
        user = users.authenticate(body_text(body, "email"), body_text(body, "password"), ip=client_ip())
        return logged_in(user)

    @app.post("/api/auth/logout")
    def logout():
        if accounts_on:
            users.end_session(request.cookies.get(SESSION_COOKIE))
        resp = jsonify({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/me")
    def me():
        accounts_required()
        return jsonify({"user": user_view(g.user)})

    @app.post("/api/me/profile")
    def update_profile():
        accounts_required()
        body = request.get_json(silent=True) or {}
        return jsonify({"user": user_view(users.update_name(g.user["id"], body_text(body, "name")))})

    @app.post("/api/me/password")
    def change_password():
        accounts_required()
        body = request.get_json(silent=True) or {}
        users.change_password(g.user["id"], body_text(body, "current_password"), body_text(body, "new_password"),
                              keep_token=g.token)
        return jsonify({"ok": True, "message": "Password changed. Other browsers have been logged out."})

    def link_broker(key_id: str, secret_key: str, url: str) -> Response:
        if g.user["is_admin"]:
            raise AccountError("The admin's account is the bot's Alpaca account from backend/.env.", 400)
        try:
            account = make_user_client(key_id, secret_key, url).get_account()
        except AlpacaError as exc:
            if exc.status in (401, 403):
                raise AccountError(
                    "Alpaca's paper trading service didn't accept these keys. Make sure you created them while your "
                    "Paper account was selected (keys for real-money accounts don't work here) and that you copied "
                    "the whole secret key.", 400) from exc
            if not exc.status:      # no answer at all
                raise AccountError("Couldn't reach Alpaca to check the keys. Check your internet connection and "
                                   "try again.", 502) from exc
            raise AccountError(f"Alpaca couldn't check the keys right now ({exc.message}). Try again in a minute.",
                               502) from exc
        status = str(account.get("status") or "ACTIVE")
        if status != "ACTIVE":
            raise AccountError(f"That Alpaca account isn't ready for trading yet (its status is {status}).", 400)
        user = users.link_alpaca(g.user["id"], key_id, secret_key, url, account.get("account_number"))
        forget_account(f"u{user['id']}", user["id"])
        log.info("Account %s connected an Alpaca paper account", user["id"])
        return jsonify({"user": user_view(user)})

    @app.post("/api/me/alpaca")
    def connect_alpaca():
        accounts_required()
        body = request.get_json(silent=True) or {}
        key_id, secret_key = body_text(body, "key_id").strip(), body_text(body, "secret_key").strip()
        if not re.fullmatch(r"[A-Za-z0-9]{12,64}", key_id):
            raise AccountError("Paste your API Key ID from Alpaca (letters and numbers, usually starting with PK).")
        if not re.fullmatch(r"[A-Za-z0-9/+=_-]{20,128}", secret_key):
            raise AccountError("Paste your whole Secret Key from Alpaca.")
        return link_broker(key_id, secret_key, settings.user_trading_url)

    @app.post("/api/me/alpaca/demo")
    def connect_demo_account():
        accounts_required()
        if demo_accounts is None:
            raise AccountError("Demo accounts are only available in the demo.", 404)
        return link_broker(*demo_accounts())

    @app.post("/api/me/alpaca/unlink")
    def disconnect_alpaca():
        accounts_required()
        user = users.unlink_alpaca(g.user["id"])
        forget_account(f"u{user['id']}", user["id"])
        return jsonify({"user": user_view(user)})

    @app.get("/api/me/trades")
    def my_trades():
        accounts_required()
        return jsonify({"trades": users.trades(g.user["id"], min(int(request.args.get("limit", 50)), 200))})

    @app.post("/api/me/delete")
    def delete_me():
        accounts_required()
        body = request.get_json(silent=True) or {}
        users.delete_user(g.user["id"], body_text(body, "password"))
        forget_account(f"u{g.user['id']}", g.user["id"])
        resp = jsonify({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    # ------------------------------------------------------ scheduled bot run
    @app.get("/api/cron/trade")
    def cron_trade():
        """One trading cycle, started by a scheduler (Vercel Cron Jobs) instead of `python -m algotrader bot`.
        Safe to call twice: the bot skips non-trading days and never places the same order twice."""
        if not settings.cron_secret:
            return jsonify({"error": "Scheduled runs are off. Set CRON_SECRET to turn them on."}), 404
        given = request.headers.get("Authorization", "").encode("utf-8", "replace")
        if not hmac.compare_digest(given, f"Bearer {settings.cron_secret}".encode("utf-8")):
            return jsonify({"error": "Not authorized"}), 401
        trader = trader_factory() if trader_factory else Trader(settings, broker(), journal, fred=fred)
        report = trader.run_cycle()
        forget_account("bot")
        log.info("Scheduled run: %s - %s", report["status"], report.get("message", ""))
        return jsonify({"status": report["status"], "message": report.get("message", ""),
                        "as_of": report.get("as_of"), "orders": len(report.get("orders") or [])}), \
            (500 if report["status"] == "error" else 200)

    # -------------------------------------------------------- dashboard files
    @app.get("/")
    @app.get("/<path:path>")
    def frontend(path: str = ""):
        if path.startswith("api/"):
            return jsonify({"error": "not found"}), 404
        if not (dist / "index.html").exists():
            return Response(
                "<h1>AlgoTrader API is running</h1><p>Dashboard not built yet: "
                "<code>cd frontend &amp;&amp; npm install &amp;&amp; npm run build</code></p>",
                mimetype="text/html")
        target = dist / path
        if path and target.is_file() and dist.resolve() in target.resolve().parents:
            return send_from_directory(dist, path)
        return send_from_directory(dist, "index.html")

    return app
