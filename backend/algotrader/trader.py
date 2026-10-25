"""Live (paper) trading engine.

One *cycle* = what the bot does once per trading day, after the close:
    1. confirm today was a trading session (Alpaca calendar)
    2. download recent bars, rebuild features exactly like training
    3. score every symbol with the random forest
    4. read account, positions and working orders from Alpaca
    5. strategy.decide() -> sells (cancel bracket legs, close position) and
       bracket buys (market entry + take-profit limit + stop-loss stop)
    6. journal everything to SQLite for the dashboard

Orders submitted after the close queue for the next open - the same timing
the backtest assumes.
"""
from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

from . import data as data_mod
from . import model as model_mod
from .alpaca_client import AlpacaError, num
from .context import FredClient, SourceUnavailable, article_view, load_macro, news_by_symbol, news_vetoes
from .features import MACRO_FEATURES, SECTOR_FEATURES, build_features, latest_rows
from .journal import Journal
from .strategy import Holding, Signal, decide

log = logging.getLogger(__name__)

OPEN_STATUSES = {"new", "accepted", "pending_new", "accepted_for_bidding", "partially_filled", "held",
                 "pending_replace", "calculated"}
MAX_DATA_AGE_DAYS = 5
BOT_ORDER_PREFIX = "algo-"      # client order ids of orders the bot placed


def next_run_time(trade_time: str, tz: ZoneInfo, now: datetime | None = None) -> datetime:
    """Next weekday at ``trade_time`` (HH:MM, in ``tz``). Holidays are skipped by the calendar check."""
    now = (now or datetime.now(tz)).astimezone(tz)
    hh, mm = (int(x) for x in trade_time.split(":"))
    candidate = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate += timedelta(days=1)
    return candidate


def cancel_and_close(client, symbol: str, open_orders: list[dict], retry_delays=(0.5, 1.0, 2.0, 4.0)) -> dict:
    """Sell a whole position: cancel its working sell orders (bracket stop/target legs) first,
    then close it. Cancelling is asynchronous at Alpaca, so the shares can stay 'held' for a
    moment - retry the close while that clears."""
    for o in open_orders:
        if o["symbol"] == symbol and o.get("side") == "sell":
            try:
                client.cancel_order(o["id"])
            except AlpacaError as exc:
                if exc.status not in (404, 422):  # already gone / not cancelable
                    raise
    last = None
    for delay in (0.0, *retry_delays):
        if delay:
            time.sleep(delay)
        try:
            return client.close_position(symbol)
        except AlpacaError as exc:
            last = exc
            if exc.status not in (403, 422) or "qty" not in (exc.message or "").lower():
                raise
    raise last


def flatten_orders(orders: list[dict]) -> list[dict]:
    """Bracket legs come nested under their parent when nested=true."""
    out = []
    for o in orders or []:
        out.append(o)
        out.extend(o.get("legs") or [])
    return out


class Trader:
    def __init__(self, settings, client, journal: Journal | None = None, model=None, fred: FredClient | None = None):
        settings.validate()
        self.settings = settings
        self.client = client
        self.fred = fred or FredClient.from_settings(settings)
        self.journal = journal or Journal(settings.journal_db)
        self._model = model
        self.tz = ZoneInfo(settings.timezone)
        self.close_retry_delays = (0.5, 1.0, 2.0, 4.0)

    @property
    def model(self):
        if self._model is None:
            self._model = model_mod.load(self.settings.model_dir)
        return self._model

    def reload_model(self):
        self._model = None
        return self.model

    # ------------------------------------------------------------------ cycle
    def run_cycle(self, force: bool = False, dry_run: bool | None = None) -> dict:
        s = self.settings
        dry = (not s.trading_enabled) if dry_run is None else dry_run
        run_id = self.journal.start_run(dry)
        report: dict = {"run_id": run_id, "dry_run": dry, "orders": [], "signals": [], "status": "running"}
        try:
            clock = self.client.get_clock()
            # Alpaca sends nanosecond precision; Python datetimes stop at microseconds
            now = pd.Timestamp(clock["timestamp"]).tz_convert(self.tz).floor("s")
            today = now.date()
            report["broker_time"] = now.isoformat()

            if not force:
                cal = self.client.get_calendar(today, today) or []
                if not any(str(c.get("date")) == today.isoformat() for c in cal):
                    return self._finish(report, "skipped", f"{today} is not a trading day")
                if clock.get("is_open"):
                    return self._finish(report, "skipped",
                                        "market is still open - the cycle runs after the close (use --force to override)")

            account = self.client.get_account()
            if account.get("trading_blocked") or account.get("account_blocked"):
                return self._finish(report, "error", "Alpaca account is blocked from trading")
            equity, cash, last_equity = (num(account.get(k)) for k in ("equity", "cash", "last_equity"))
            report["equity"] = equity

            mdl = self.model
            cols = mdl.feature_cols
            use_market = "mkt_ret_5" in cols
            sectors = (mdl.meta.get("sectors") or s.sector_map) if any(c in SECTOR_FEATURES for c in cols) else None
            macro = None
            if any(c in MACRO_FEATURES for c in cols):
                try:
                    macro = load_macro(s, self.fred, years=2)
                except SourceUnavailable as exc:
                    return self._finish(report, "error", f"the model needs market data from FRED, which isn't "
                                                         f"available ({exc}); no trades today")
            needed = s.all_symbols + sorted({e for e in (sectors or {}).values() if e not in s.all_symbols})
            bars = data_mod.fetch_recent(self.client, needed, sessions=260, end=now.to_pydatetime())
            if bars.empty:
                return self._finish(report, "error", "no market data returned")
            symbols = [x for x in s.symbols if x in set(bars["symbol"])]
            frame = build_features(bars, s.benchmark, use_market=use_market, symbols=symbols,
                                   sectors=sectors, macro=macro)
            latest = latest_rows(frame, cols)
            if latest.empty:
                return self._finish(report, "error", "not enough history to compute features")

            as_of = latest["date"].max()
            report["as_of"] = as_of.strftime("%Y-%m-%d")
            age = (pd.Timestamp(today) - as_of).days
            if age > MAX_DATA_AGE_DAYS:
                return self._finish(report, "error", f"latest bar is {age} days old - data feed problem?")
            stale = latest[latest["date"] < as_of]["symbol"].tolist()
            if stale:
                log.warning("Stale data for %s - no new decisions for them", stale)
            fresh = latest[latest["date"] == as_of].copy()
            fresh["proba"] = mdl.predict_proba(fresh)
            market_ok = bool(fresh["market_ok"].iloc[0])
            signals = {r.symbol: Signal(r.symbol, float(r.proba), float(r.close)) for r in fresh.itertuples()}

            # --- portfolio state (only symbols in our universe are managed)
            positions = {p["symbol"]: p for p in self.client.get_positions() if p["symbol"] in s.symbols}
            open_orders = [o for o in flatten_orders(self.client.list_orders(status="open", limit=500))
                           if o.get("status") in OPEN_STATUSES]
            blocked = self._blocked_symbols(open_orders)
            holdings, manual = self._holdings(positions, frame, as_of)

            allow_entries = True
            if last_equity > 0 and (equity / last_equity - 1) <= -s.strategy.max_daily_loss_pct:
                allow_entries = False
                log.warning("Daily loss limit hit (%.2f%%) - no new entries", 100 * (equity / last_equity - 1))

            vetoes = self._news_vetoes(now) if s.news_check else {}
            plan = decide(signals, holdings, equity, cash, s.strategy, market_ok=market_ok,
                          allow_entries=allow_entries, blocked=blocked, manual=manual, vetoes=vetoes)

            sig_rows = []
            for sym in sorted(set(signals) | set(holdings) | manual):
                action, why = plan.actions.get(sym, ("WATCH", ""))
                sg = signals.get(sym)
                sig_rows.append({"symbol": sym, "proba": sg.proba if sg else None, "price": sg.price if sg else None,
                                 "action": action, "reason": why, "held": int(sym in holdings or sym in manual)})
            self.journal.add_signals(run_id, report["as_of"], sig_rows)
            report["signals"] = sig_rows
            report["market_ok"] = market_ok

            for dec in plan.sells + plan.buys:
                report["orders"].append(self._execute(run_id, dec, report["as_of"], open_orders, dry))

            n_ok = sum(1 for o in report["orders"] if o["status"] not in ("error",))
            msg = f"{len(plan.buys)} buy / {len(plan.sells)} sell decisions" + (" (dry run)" if dry else "")
            if any(o["status"] == "error" for o in report["orders"]):
                return self._finish(report, "partial", msg + " - some orders failed", n_ok)
            return self._finish(report, "ok", msg, n_ok)
        except Exception as exc:  # never crash the scheduler; record why
            log.exception("Trade cycle failed")
            return self._finish(report, "error", f"{type(exc).__name__}: {exc}")

    def _finish(self, report: dict, status: str, message: str, n_orders: int = 0) -> dict:
        report.update(status=status, message=message)
        self.journal.finish_run(report["run_id"], status, message, report.get("as_of"),
                                report.get("equity"), n_orders)
        (log.error if status == "error" else log.info)("Cycle %s: %s", status, message)
        return report

    # --------------------------------------------------------------- helpers
    def _news_vetoes(self, now: pd.Timestamp) -> dict[str, str]:
        """Live safety rule: don't open new positions into clearly negative news (Benzinga via
        Alpaca). If the news can't be fetched, trading carries on without this check."""
        s = self.settings
        since = now - pd.Timedelta(days=s.news_lookback_days)
        try:
            raw = self.client.get_news(s.symbols, start=since.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
                                       max_items=200)
        except AlpacaError as exc:
            log.warning("News check skipped - couldn't fetch news: %s", exc.message)
            return {}
        summary = news_by_symbol([article_view(a) for a in raw], s.symbols, since=since)
        vetoes = news_vetoes(summary, s.news_min_articles, s.news_block_score)
        for sym, why in vetoes.items():
            log.info("News check: %s - %s", sym, why)
        return vetoes

    @staticmethod
    def _blocked_symbols(open_orders: list[dict]) -> set[str]:
        """Symbols with an entry or exit already working.

        Protective bracket legs (sell limit / sell stop) don't block: they're
        how every position is held.
        """
        blocked = set()
        for o in open_orders:
            protective = o.get("side") == "sell" and o.get("type") in ("limit", "stop", "stop_limit", "trailing_stop")
            if not protective:
                blocked.add(o["symbol"])
        return blocked

    def _holdings(self, positions: dict, frame: pd.DataFrame, as_of: pd.Timestamp) -> tuple[dict[str, Holding], set[str]]:
        """Split positions into the ones the bot manages and the ones the user bought by hand.

        A position belongs to the bot only if its most recent buy was a bot order (client order
        id ``algo-...``). Anything bought from the dashboard, the Alpaca app, or that the bot
        cannot attribute is left alone: never sold by the model, never added to.
        """
        if not positions:
            return {}, set()
        entries = self._entries(list(positions))
        sessions = sorted(frame["date"].unique())
        out, manual = {}, set()
        for sym, p in positions.items():
            qty = num(p.get("qty"))
            if qty <= 0:
                continue  # never touch short positions we didn't open
            entry = entries.get(sym)
            if entry is None or not entry[1]:
                manual.add(sym)
                continue
            days = sum(1 for d in sessions if pd.Timestamp(entry[0]) <= d <= as_of)
            out[sym] = Holding(sym, qty, num(p.get("avg_entry_price")), days)
        return out, manual

    def _entries(self, symbols: list[str]) -> dict[str, tuple[pd.Timestamp, bool]]:
        """symbol -> (date of the most recent filled buy, whether the bot placed it)."""
        found: dict[str, tuple[pd.Timestamp, bool]] = {}
        try:
            closed = flatten_orders(self.client.list_orders(status="closed", limit=500, symbols=symbols))
        except AlpacaError as exc:
            log.warning("Could not read order history: %s", exc)
            closed = []
        for o in closed:  # newest first
            sym = o.get("symbol")
            if sym in found or o.get("side") != "buy" or not o.get("filled_at"):
                continue
            when = pd.Timestamp(o["filled_at"]).tz_convert(self.tz).tz_localize(None).normalize()
            found[sym] = (when, str(o.get("client_order_id", "")).startswith(BOT_ORDER_PREFIX))
        for sym in symbols:
            if sym not in found:
                row = self.journal.last_buy(sym)
                if row and row.get("as_of"):
                    # submitted after the close of as_of -> filled next session
                    found[sym] = (pd.Timestamp(row["as_of"]) + pd.offsets.BDay(1), True)
        return found

    def _execute(self, run_id: int, dec, as_of: str, open_orders: list[dict], dry: bool) -> dict:
        rec = {"symbol": dec.symbol, "side": dec.side, "qty": dec.qty, "reason": dec.reason,
               "proba": dec.proba, "price": dec.price, "stop_price": dec.stop_price,
               "take_profit_price": dec.take_profit_price, "as_of": as_of}
        coid = f"{BOT_ORDER_PREFIX}{as_of.replace('-', '')}-{dec.symbol}-{dec.side}"
        rec["client_order_id"] = coid
        if dry:
            rec["status"] = "dry_run"
            self.journal.add_order(run_id, **rec)
            return rec
        try:
            if dec.side == "sell":
                order = cancel_and_close(self.client, dec.symbol, open_orders, self.close_retry_delays)
            else:
                order = self.client.submit_order(
                    dec.symbol, "buy", qty=int(dec.qty), type="market", time_in_force="gtc",
                    order_class="bracket", take_profit=dec.take_profit_price, stop_loss=dec.stop_price,
                    client_order_id=coid,
                )
            rec.update(alpaca_order_id=order.get("id"), status=order.get("status", "submitted"))
        except AlpacaError as exc:
            if exc.status == 422 and "client_order_id" in (exc.message or "").lower():
                rec.update(status="duplicate", error="already submitted earlier today")
            else:
                rec.update(status="error", error=exc.message)
                log.error("Order failed %s %s: %s", dec.side, dec.symbol, exc)
        self.journal.add_order(run_id, **rec)
        return rec

    def _close_with_retry(self, symbol: str) -> dict:
        return cancel_and_close(self.client, symbol, [], self.close_retry_delays)

    # ------------------------------------------------------------- scheduler
    def next_run_time(self, now: datetime | None = None) -> datetime:
        return next_run_time(self.settings.trade_time, self.tz, now)

    def run_forever(self, run_now: bool = False, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, lambda *_: stop.set())
        log.info("Bot started (%s mode, trading %s). Daily cycle at %s %s.",
                 self.settings.mode, "ENABLED" if self.settings.trading_enabled else "DISABLED (dry run)",
                 self.settings.trade_time, self.settings.timezone)
        if run_now:
            self.run_cycle()
        while not stop.is_set():
            nxt = self.next_run_time()
            log.info("Next cycle at %s", nxt.isoformat())
            while not stop.is_set() and datetime.now(self.tz) < nxt:
                stop.wait(min(60.0, max(0.0, (nxt - datetime.now(self.tz)).total_seconds())))
            if stop.is_set():
                break
            try:
                self.reload_model()  # pick up a model retrained since the last cycle
            except FileNotFoundError as exc:
                log.error("%s", exc)
                continue
            self.run_cycle()
        log.info("Bot stopped")
