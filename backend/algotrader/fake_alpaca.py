"""An in-memory stand-in for the Alpaca Trading + Market Data REST APIs.

Used by the test-suite and by ``python -m algotrader demo`` so the whole
stack (bot, API, dashboard) can run end-to-end without API keys or network.

It replays a historical bar file one session at a time.  It reproduces the
JSON shapes and the validation rules the bot relies on (bracket price
checks, sub-penny rule, duplicate client_order_id, insufficient buying
power, shares held by open bracket legs, OCO cancellation).  It is NOT a
market simulator for research - fills are at the open / exact stop/limit.
"""
from __future__ import annotations

import base64
import itertools
import random
import threading
import uuid
import zlib
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from flask import Flask, jsonify, request

NY = ZoneInfo("America/New_York")
ASSET_NAMES = {
    "AAPL": ("Apple Inc. Common Stock", "NASDAQ"), "MSFT": ("Microsoft Corporation Common Stock", "NASDAQ"),
    "NVDA": ("NVIDIA Corporation Common Stock", "NASDAQ"), "AMZN": ("Amazon.com, Inc. Common Stock", "NASDAQ"),
    "GOOGL": ("Alphabet Inc. Class A Common Stock", "NASDAQ"),
    "META": ("Meta Platforms, Inc. Class A Common Stock", "NASDAQ"),
    "JPM": ("JPMorgan Chase & Co. Common Stock", "NYSE"), "V": ("Visa Inc.", "NYSE"),
    "UNH": ("UnitedHealth Group Incorporated Common Stock", "NYSE"),
    "XOM": ("Exxon Mobil Corporation Common Stock", "NYSE"), "SPY": ("SPDR S&P 500 ETF Trust", "ARCA"),
}
OPEN_STATUSES = {"new", "accepted", "held", "pending_new", "partially_filled"}
_period_sessions = {"D": 1, "W": 5, "M": 21, "A": 252}


def _err(status: int, message: str, code: int | None = None):
    resp = jsonify({"code": code or status * 100000, "message": message})
    resp.status_code = status
    return resp


def _fmt(x) -> str | None:
    return None if x is None else (f"{x:.2f}" if abs(x) >= 1 else f"{x:.4f}")


class FakeAlpaca:
    def __init__(self, bars: pd.DataFrame, start_index: int, api_key: str = "test-key",
                 secret_key: str = "test-secret", initial_cash: float = 100_000.0, news: list[dict] | None = None):
        self.bars = bars.sort_values(["symbol", "date"]).reset_index(drop=True)
        self.dates = sorted(self.bars["date"].unique())
        if not 0 <= start_index < len(self.dates):
            raise ValueError("start_index out of range")
        self.i = start_index
        self.by_date = {d: g.set_index("symbol") for d, g in self.bars.groupby("date")}
        self.symbols = set(self.bars["symbol"])
        self.api_key, self.secret_key = api_key, secret_key
        self.cash = float(initial_cash)
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.live_price: dict[str, float] = {}
        self.equity_history: list[tuple[pd.Timestamp, float]] = [(self.today, self.cash)]
        # end-of-day cash and holdings per session, for intraday account history
        self.day_book: dict[pd.Timestamp, tuple[float, dict[str, float]]] = {self.today: (self.cash, {})}
        self.last_equity = self.cash
        self.lock = threading.RLock()
        self._seq = itertools.count(1)
        self.request_log: list[tuple[str, str]] = []
        self.news = sorted(news or [], key=lambda a: a["created_at"], reverse=True)
        self.app = self._make_app()

    # ------------------------------------------------------------- sim state
    @property
    def today(self) -> pd.Timestamp:
        return pd.Timestamp(self.dates[self.i])

    def now(self) -> datetime:
        """Simulated wall clock: 16:30 New York on the current session (after the close)."""
        return datetime.combine(self.today.date(), dtime(16, 30), tzinfo=NY)

    def _iso(self, dt: datetime | None = None) -> str:
        return (dt or self.now()).isoformat()

    def bar(self, symbol: str, date=None):
        g = self.by_date.get(pd.Timestamp(date or self.today))
        if g is None or symbol not in g.index:
            return None
        return g.loc[symbol]

    def price(self, symbol: str) -> float | None:
        if symbol in self.live_price:
            return self.live_price[symbol]
        hist = self.bars[(self.bars["symbol"] == symbol) & (self.bars["date"] <= self.today)]
        return None if hist.empty else float(hist["close"].iloc[-1])

    def equity(self) -> float:
        return self.cash + sum(p["qty"] * (self.price(s) or p["avg"]) for s, p in self.positions.items())

    def tick(self, noise: float = 0.0015, rng: random.Random | None = None) -> None:
        """Demo only: wiggle prices around the close so the dashboard looks live."""
        rng = rng or random
        with self.lock:
            for sym in self.symbols:
                b = self.bar(sym)
                if b is None:
                    continue
                base = self.live_price.get(sym, float(b["close"]))
                drift_back = (float(b["close"]) - base) * 0.05
                self.live_price[sym] = round(max(0.01, base + drift_back + base * rng.gauss(0, noise)), 2)

    def intraday_bars(self, symbols, start, end, minutes: int) -> pd.DataFrame:
        """Deterministic intraday bars (8:00-18:00 New York, incl. thin extended hours) whose
        path runs from each day's open to its close and stays inside its high/low."""
        rows = []
        first = pd.Timestamp(start.tz_convert(NY).date()) if start is not None else None
        for sym in symbols:
            g = self.bars[(self.bars["symbol"] == sym) & (self.bars["date"] <= self.today)]
            if first is not None:
                g = g[g["date"] >= first]
            for r in g.itertuples():
                day = pd.Timestamp(r.date).tz_localize(NY)
                stamps = pd.date_range(day + pd.Timedelta(hours=8), day + pd.Timedelta(hours=18),
                                       freq=f"{minutes}min", inclusive="left")
                n = len(stamps)
                rng = np.random.default_rng(zlib.crc32(f"{sym}{r.date:%Y%m%d}{minutes}".encode()))
                walk = np.cumsum(rng.normal(0, 1, n))
                walk -= np.linspace(walk[0], walk[-1], n)          # bridge: pinned at both ends
                path = np.linspace(r.open, r.close, n)
                room = max(r.high - max(r.open, r.close), min(r.open, r.close) - r.low, 1e-9)
                path = np.clip(path + walk / (np.abs(walk).max() or 1) * room, r.low, r.high)
                live = self.live_price.get(sym) if pd.Timestamp(r.date) == self.today else None
                if live:
                    path[-1] = live
                for k, t in enumerate(stamps):
                    o = path[k - 1] if k else r.open
                    c = path[k]
                    rows.append({"symbol": sym, "t": t.tz_convert("UTC"), "open": o, "high": max(o, c),
                                 "low": min(o, c), "close": c, "volume": max(int(r.volume / n), 1)})
        df = pd.DataFrame(rows, columns=["symbol", "t", "open", "high", "low", "close", "volume"])
        if not df.empty:
            df = df[(df["t"] >= (start if start is not None else df["t"].min()))
                    & (df["t"] <= (end if end is not None else df["t"].max()))]
        return df.sort_values(["symbol", "t"]).reset_index(drop=True)

    def held_qty(self, symbol: str) -> float:
        return sum(float(o["qty"]) for o in self._all_orders()
                   if o["symbol"] == symbol and o["side"] == "sell" and o["status"] in OPEN_STATUSES)

    def reserved_cash(self) -> float:
        total = 0.0
        for o in self.orders:
            if o["side"] == "buy" and o["status"] in OPEN_STATUSES:
                total += float(o["qty"]) * (self.price(o["symbol"]) or 0)
        return total

    def _all_orders(self):
        for o in self.orders:
            yield o
            yield from o.get("legs") or []

    def _find(self, order_id: str):
        for o in self.orders:
            if o["id"] == order_id:
                return o, None
            for leg in o.get("legs") or []:
                if leg["id"] == order_id:
                    return leg, o
        return None, None

    # ----------------------------------------------------------- order model
    def _new_order(self, symbol, side, qty, otype, tif, limit=None, stop=None, order_class="simple",
                   client_order_id=None, status="accepted") -> dict:
        ts = self._iso()
        return {
            "id": str(uuid.uuid4()),
            "client_order_id": client_order_id or str(uuid.uuid4()),
            "created_at": ts, "updated_at": ts, "submitted_at": ts,
            "filled_at": None, "expired_at": None, "canceled_at": None, "failed_at": None,
            "replaced_at": None, "replaced_by": None, "replaces": None,
            "asset_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, symbol)), "symbol": symbol,
            "asset_class": "us_equity", "notional": None, "qty": f"{qty:.9g}",
            "filled_qty": "0", "filled_avg_price": None, "order_class": order_class,
            "order_type": otype, "type": otype, "side": side, "time_in_force": tif,
            "limit_price": _fmt(limit), "stop_price": _fmt(stop), "status": status,
            "extended_hours": False, "legs": None, "trail_percent": None, "trail_price": None, "hwm": None,
            "position_intent": "buy_to_open" if side == "buy" else "sell_to_close",
            "_seq": next(self._seq),
        }

    @staticmethod
    def _public(o: dict, nested: bool = True) -> dict:
        out = {k: v for k, v in o.items() if not k.startswith("_")}
        if out.get("legs"):
            out["legs"] = [FakeAlpaca._public(leg) for leg in out["legs"]] if nested else None
        return out

    def submit(self, body: dict):
        sym = str(body.get("symbol", "")).upper()
        side = body.get("side")
        otype = body.get("type", "market")
        tif = body.get("time_in_force", "day")
        oclass = body.get("order_class") or "simple"
        if sym not in self.symbols:
            return _err(422, f"asset {sym} not found")
        if side not in ("buy", "sell"):
            return _err(422, "invalid side")
        if otype not in ("market", "limit", "stop"):
            return _err(422, f"order type {otype} not supported by the fake")
        if tif not in ("day", "gtc", "opg", "cls", "ioc", "fok"):
            return _err(422, "invalid time_in_force")
        notional = body.get("notional")
        if notional is not None:
            if body.get("qty") is not None:
                return _err(422, "qty and notional are mutually exclusive")
            if otype != "market" or tif != "day" or (body.get("order_class") or "simple") != "simple":
                return _err(422, "fractional orders must be simple day market orders in the stand-in")
            base_px = self.price(sym)
            try:
                notional = float(notional)
            except (TypeError, ValueError):
                return _err(422, "invalid notional")
            if notional < 1 or not base_px:
                return _err(422, "notional must be at least $1")
            qty = round(notional / base_px, 9)
        else:
            try:
                qty = float(body["qty"])
            except (KeyError, TypeError, ValueError):
                return _err(422, "qty is required")
        if qty <= 0:
            return _err(422, "qty must be > 0")
        coid = body.get("client_order_id")
        if coid and any(o["client_order_id"] == coid for o in self._all_orders()):
            return _err(422, "client_order_id must be unique", 40010001)
        for key in ("limit_price", "stop_price"):
            v = body.get(key)
            if v is not None and float(v) >= 1 and round(float(v), 2) != float(v):
                return _err(422, "sub-penny increment does not fulfill minimum pricing criteria")
        base = self.price(sym)

        tp = sl = None
        if oclass == "bracket":
            if tif not in ("day", "gtc"):
                return _err(422, "bracket orders must be day or gtc")
            if qty != int(qty):
                return _err(422, "fractional orders must be simple orders")
            try:
                tp = float(body["take_profit"]["limit_price"])
                sl = float(body["stop_loss"]["stop_price"])
            except (KeyError, TypeError, ValueError):
                return _err(422, "bracket orders require take_profit.limit_price and stop_loss.stop_price")
            for v in (tp, sl):
                if v >= 1 and round(v, 2) != v:
                    return _err(422, "sub-penny increment does not fulfill minimum pricing criteria")
            if side == "buy":
                if tp < base + 0.01:
                    return _err(422, f"take_profit.limit_price must be >= base_price + 0.01 (base {base:.2f})")
                if sl > base - 0.01:
                    return _err(422, f"stop_loss.stop_price must be <= base_price - 0.01 (base {base:.2f})")
        elif oclass != "simple":
            return _err(422, f"order_class {oclass} not supported by the fake")

        if side == "buy":
            cost = qty * (float(body.get("limit_price") or 0) or base)
            if cost > self.cash - self.reserved_cash() + 1e-9:
                return _err(403, "insufficient buying power", 40310000)
        else:
            pos = self.positions.get(sym)
            available = (pos["qty"] if pos else 0) - self.held_qty(sym)
            if qty > available + 1e-9:
                return _err(403, f"insufficient qty available for order (requested: {qty:g}, available: {max(available, 0):g})",
                            40310000)

        limit = float(body["limit_price"]) if body.get("limit_price") else None
        stop = float(body["stop_price"]) if body.get("stop_price") else None
        order = self._new_order(sym, side, qty, otype, tif, limit, stop, oclass, coid,
                                status="accepted")
        if notional is not None:
            order["notional"] = f"{notional:.2f}"
        if oclass == "bracket":
            exit_side = "sell" if side == "buy" else "buy"
            order["legs"] = [
                self._new_order(sym, exit_side, qty, "limit", tif, limit=tp, order_class="bracket", status="held"),
                self._new_order(sym, exit_side, qty, "stop", tif, stop=sl, order_class="bracket", status="held"),
            ]
        self.orders.append(order)
        return jsonify(self._public(order))

    def cancel(self, order_id: str):
        order, parent = self._find(order_id)
        if order is None:
            return _err(404, "order not found", 40410000)
        if order["status"] not in OPEN_STATUSES:
            return _err(422, "order is not cancelable")
        group = [parent] + (parent["legs"] or []) if parent else [order] + (order.get("legs") or [])
        for o in group:
            if o["status"] in OPEN_STATUSES:
                o["status"], o["canceled_at"], o["updated_at"] = "canceled", self._iso(), self._iso()
        return "", 204

    def close_position(self, symbol: str):
        pos = self.positions.get(symbol)
        if not pos:
            return _err(404, "position not found", 40410000)
        held = self.held_qty(symbol)
        if held > 0:
            return _err(403, f"insufficient qty available for order (requested: {pos['qty']:g}, available: {pos['qty'] - held:g})",
                        40310000)
        order = self._new_order(symbol, "sell", pos["qty"], "market", "day")
        self.orders.append(order)
        return jsonify(self._public(order))

    # ------------------------------------------------------------ simulation
    def _fill(self, o: dict, price: float, when: datetime) -> None:
        qty = float(o["qty"])
        sym = o["symbol"]
        if o["side"] == "buy":
            pos = self.positions.setdefault(sym, {"qty": 0.0, "avg": 0.0})
            pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / (pos["qty"] + qty)
            pos["qty"] += qty
            self.cash -= qty * price
        else:
            pos = self.positions[sym]
            pos["qty"] -= qty
            self.cash += qty * price
            if pos["qty"] <= 1e-9:
                del self.positions[sym]
        o.update(status="filled", filled_qty=o["qty"], filled_avg_price=_fmt(price),
                 filled_at=when.isoformat(), updated_at=when.isoformat())

    def advance(self, sessions: int = 1) -> None:
        """Move to the next session: fill queued orders at the open, run bracket legs, close."""
        with self.lock:
            for _ in range(sessions):
                if self.i + 1 >= len(self.dates):
                    raise IndexError("no more historical sessions to replay")
                self.last_equity = self.equity()
                self.i += 1
                self.live_price.clear()
                open_t = datetime.combine(self.today.date(), dtime(9, 30), tzinfo=NY)

                # 1. market orders queued since the last close fill at today's open
                for o in sorted(self.orders, key=lambda x: x["_seq"]):
                    if o["status"] not in ("accepted", "new") or o["type"] != "market":
                        continue
                    b = self.bar(o["symbol"])
                    if b is None:
                        continue
                    if o.get("notional"):   # dollar orders become shares at the fill price, like Alpaca
                        o["qty"] = f"{round(float(o['notional']) / float(b['open']), 9):.9g}"
                    insufficient_shares = o["side"] == "sell" and self.positions.get(o["symbol"], {}).get("qty", 0) < float(o["qty"])
                    if insufficient_shares or (o["side"] == "buy" and float(o["qty"]) * float(b["open"]) > self.cash + 1e-6):
                        o.update(status="rejected", failed_at=open_t.isoformat())
                        for leg in o.get("legs") or []:
                            leg["status"] = "canceled"
                        continue
                    self._fill(o, float(b["open"]), open_t)
                    for leg in o.get("legs") or []:
                        leg["status"] = "new" if leg["type"] == "limit" else "held"

                # 2. working stop / limit orders (incl. bracket legs) vs today's range
                for o in list(self._all_orders()):
                    if o["status"] not in ("new", "held") or o["type"] not in ("stop", "limit"):
                        continue
                    if o["side"] != "sell" or o["symbol"] not in self.positions:
                        continue
                    _, parent = self._find(o["id"])
                    if parent is not None and parent["status"] != "filled":
                        continue  # legs only activate once the entry has filled
                    b = self.bar(o["symbol"])
                    if b is None:
                        continue
                    op, hi, lo = float(b["open"]), float(b["high"]), float(b["low"])
                    px = None
                    if o["type"] == "stop":
                        stop = float(o["stop_price"])
                        px = op if op <= stop else (stop if lo <= stop else None)
                    else:
                        lim = float(o["limit_price"])
                        siblings = self._siblings(o)
                        stop_hit = any(s["type"] == "stop" and lo <= float(s["stop_price"]) for s in siblings)
                        if not stop_hit:  # same-day tie -> the stop wins (conservative)
                            px = op if op >= lim else (lim if hi >= lim else None)
                    if px is not None:
                        self._fill(o, px, open_t)
                        for s in self._siblings(o):
                            if s["status"] in OPEN_STATUSES:
                                s.update(status="canceled", canceled_at=open_t.isoformat())

                # 3. day orders expire at the close
                for o in self._all_orders():
                    if o["time_in_force"] == "day" and o["status"] in OPEN_STATUSES and o["type"] != "market":
                        o["status"] = "expired"
                self.equity_history.append((self.today, self.equity()))
                self.day_book[self.today] = (self.cash, {k: v["qty"] for k, v in self.positions.items()})

    def _siblings(self, leg: dict) -> list[dict]:
        _, parent = self._find(leg["id"])
        if parent is None:
            return []
        return [x for x in parent["legs"] if x["id"] != leg["id"]]

    # ----------------------------------------------------------------- flask
    def _make_app(self) -> Flask:
        app = Flask("fake_alpaca")
        fa = self

        @app.before_request
        def auth():
            fa.request_log.append((request.method, request.path))
            if request.path.startswith("/__sim"):
                return None
            if (request.headers.get("APCA-API-KEY-ID") != fa.api_key
                    or request.headers.get("APCA-API-SECRET-KEY") != fa.secret_key):
                return _err(401, "request is not authorized", 40110000)
            return None

        @app.get("/v2/account")
        def account():
            with fa.lock:
                eq = fa.equity()
                lmv = eq - fa.cash
                return jsonify({
                    "id": "fake-account", "account_number": "PA000FAKE", "status": "ACTIVE",
                    "currency": "USD", "cash": _fmt(fa.cash), "portfolio_value": _fmt(eq), "equity": _fmt(eq),
                    "last_equity": _fmt(fa.last_equity), "buying_power": _fmt(max(fa.cash - fa.reserved_cash(), 0)),
                    "long_market_value": _fmt(lmv), "short_market_value": "0", "initial_margin": "0",
                    "maintenance_margin": "0", "daytrade_count": 0, "pattern_day_trader": False,
                    "trading_blocked": False, "transfers_blocked": False, "account_blocked": False,
                    "multiplier": "1", "created_at": "2024-01-01T00:00:00Z",
                })

        @app.get("/v2/clock")
        def clock():
            with fa.lock:
                nxt = fa.dates[fa.i + 1] if fa.i + 1 < len(fa.dates) else fa.today + pd.offsets.BDay(1)
                nxt = pd.Timestamp(nxt).date()
                return jsonify({
                    "timestamp": fa._iso().replace("-04:00", ".123456789-04:00").replace("-05:00", ".123456789-05:00"),
                    "is_open": False,
                    "next_open": datetime.combine(nxt, dtime(9, 30), tzinfo=NY).isoformat(),
                    "next_close": datetime.combine(nxt, dtime(16, 0), tzinfo=NY).isoformat(),
                })

        @app.get("/v2/calendar")
        def calendar():
            start = pd.Timestamp(request.args.get("start") or fa.dates[0])
            end = pd.Timestamp(request.args.get("end") or fa.dates[-1])
            return jsonify([
                {"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "open": "09:30", "close": "16:00",
                 "session_open": "0400", "session_close": "2000"}
                for d in fa.dates if start <= d <= end
            ])

        def asset(sym: str) -> dict:
            name, exchange = ASSET_NAMES.get(sym, (f"{sym} Corporation Common Stock", "NYSE"))
            return {"id": str(uuid.uuid5(uuid.NAMESPACE_DNS, sym)), "class": "us_equity", "exchange": exchange,
                    "symbol": sym, "name": name, "status": "active", "tradable": True, "marginable": True,
                    "shortable": True, "easy_to_borrow": True, "fractionable": True}

        @app.get("/v2/assets")
        def assets():
            return jsonify([asset(sym) for sym in sorted(fa.symbols)])

        @app.get("/v2/assets/<symbol>")
        def one_asset(symbol):
            symbol = symbol.upper()
            if symbol not in fa.symbols:
                return _err(404, "asset not found", 40410000)
            return jsonify(asset(symbol))

        @app.get("/v2/positions")
        def positions():
            with fa.lock:
                out = []
                for sym, p in sorted(fa.positions.items()):
                    px = fa.price(sym) or p["avg"]
                    prev_bar = fa.bar(sym, fa.dates[fa.i - 1]) if fa.i > 0 else None
                    last = float(prev_bar["close"]) if prev_bar is not None else px
                    mv, cost = p["qty"] * px, p["qty"] * p["avg"]
                    out.append({
                        "asset_id": str(uuid.uuid5(uuid.NAMESPACE_DNS, sym)), "symbol": sym, "exchange": "NASDAQ",
                        "asset_class": "us_equity", "avg_entry_price": _fmt(p["avg"]), "qty": f"{p['qty']:.9g}",
                        "qty_available": f"{p['qty'] - fa.held_qty(sym):.9g}", "side": "long",
                        "market_value": _fmt(mv), "cost_basis": _fmt(cost), "unrealized_pl": _fmt(mv - cost),
                        "unrealized_plpc": f"{(mv / cost - 1) if cost else 0:.4f}",
                        "unrealized_intraday_pl": _fmt(p["qty"] * (px - last)),
                        "unrealized_intraday_plpc": f"{(px / last - 1) if last else 0:.4f}",
                        "current_price": _fmt(px), "lastday_price": _fmt(last),
                        "change_today": f"{(px / last - 1) if last else 0:.4f}",
                    })
                return jsonify(out)

        @app.delete("/v2/positions/<symbol>")
        def close_pos(symbol):
            with fa.lock:
                return fa.close_position(symbol.upper())

        @app.get("/v2/orders")
        def list_orders():
            with fa.lock:
                status = request.args.get("status", "open")
                limit = int(request.args.get("limit", 50))
                nested = request.args.get("nested", "false") == "true"
                direction = request.args.get("direction", "desc")
                syms = set(filter(None, (request.args.get("symbols") or "").split(",")))
                pool = list(fa.orders) if nested else list(fa._all_orders())

                def keep(o):
                    if syms and o["symbol"] not in syms:
                        return False
                    if status == "open":
                        legs_open = nested and any(leg["status"] in OPEN_STATUSES for leg in (o.get("legs") or []))
                        return o["status"] in OPEN_STATUSES or legs_open
                    if status == "closed":
                        return o["status"] not in OPEN_STATUSES
                    return True

                sel = sorted((o for o in pool if keep(o)), key=lambda o: o["_seq"], reverse=direction == "desc")
                return jsonify([fa._public(o, nested) for o in sel[:limit]])

        @app.post("/v2/orders")
        def post_order():
            with fa.lock:
                return fa.submit(request.get_json(force=True) or {})

        @app.delete("/v2/orders/<order_id>")
        def delete_order(order_id):
            with fa.lock:
                return fa.cancel(order_id)

        @app.get("/v2/account/portfolio/history")
        def history():
            """Account equity like Alpaca's endpoint: daily (stamped at midnight New York, with 0 for
            days before the account was funded) or intraday for spans under 30 days."""
            tf = request.args.get("timeframe", "1D")
            minutes = {"1Min": 1, "5Min": 5, "15Min": 15, "1H": 60, "1D": 1440}.get(tf)
            if minutes is None:
                return _err(422, "invalid timeframe")
            period = request.args.get("period")
            start, end = _ts(request.args.get("start")), _ts(request.args.get("end"))
            if period and start is not None and end is not None:
                return _err(422, "period cannot be combined with both start and end")
            with fa.lock:
                last_day = fa.today.tz_localize(NY)
                if end is None:
                    end = last_day + pd.Timedelta(hours=23, minutes=59)
                if start is None:
                    n = int((period or "1M")[:-1] or 1) * _period_sessions.get((period or "1M")[-1], 21)
                    days_back = [d for d in fa.dates if d <= fa.today][-(n + 1):]
                    start = pd.Timestamp(days_back[0]).tz_localize(NY)
                if minutes < 1440 and end - start > pd.Timedelta(days=30):
                    return _err(422, "intraday timeframes are only available for periods under 30 days")
                ts_out, eq_out = [], []
                if minutes == 1440:
                    opened = fa.equity_history[0][0]
                    before = [d for d in fa.dates if d < opened][-5:]   # not funded yet -> 0
                    for d, e in [(pd.Timestamp(d), 0.0) for d in before] + fa.equity_history:
                        stamp = pd.Timestamp(d).tz_localize(NY)
                        if start.normalize() <= stamp <= end:
                            ts_out.append(int(stamp.timestamp()))
                            eq_out.append(round(e, 2))
                else:
                    for d, (cash, held) in sorted(fa.day_book.items()):
                        day = pd.Timestamp(d).tz_localize(NY)
                        lo, hi = max(start, day + pd.Timedelta(hours=9, minutes=30)), min(end, day + pd.Timedelta(hours=16))
                        if lo >= hi:
                            continue
                        stamps = pd.date_range(day + pd.Timedelta(hours=9, minutes=30), day + pd.Timedelta(hours=16),
                                               freq=f"{minutes}min", inclusive="left")
                        stamps = stamps[(stamps >= lo) & (stamps < hi)]
                        value = pd.Series(cash, index=stamps)
                        if held:
                            px = fa.intraday_bars(list(held), day.tz_convert("UTC"), (day + pd.Timedelta(hours=18)).tz_convert("UTC"),
                                                  min(minutes, 60))
                            for sym, qty in held.items():
                                g = px[px["symbol"] == sym].set_index("t")["close"]
                                g.index = g.index.tz_convert(NY)
                                value = value + qty * g.reindex(g.index.union(stamps)).ffill().reindex(stamps).bfill()
                        ts_out += [int(t.timestamp()) for t in stamps]
                        eq_out += [round(float(v), 2) for v in value]
                base = next((e for e in eq_out if e), 0.0)
                return jsonify({
                    "timestamp": ts_out, "equity": eq_out,
                    "profit_loss": [round(e - base, 2) if e else 0.0 for e in eq_out],
                    "profit_loss_pct": [round(e / base - 1, 4) if e and base else 0.0 for e in eq_out],
                    "base_value": round(base, 2), "timeframe": tf,
                })

        @app.get("/v2/stocks/bars")
        def bars():
            syms = [s for s in (request.args.get("symbols") or "").upper().split(",") if s]
            start = _ts(request.args.get("start")) or pd.Timestamp("1900-01-01", tz="UTC")
            end = _ts(request.args.get("end")) or pd.Timestamp("2100-01-01", tz="UTC")
            limit = min(int(request.args.get("limit", 1000)), 10000)
            offset = int(base64.b64decode(request.args["page_token"]).decode()) if request.args.get("page_token") else 0
            minutes = _timeframe_minutes(request.args.get("timeframe", "1Day"))
            if minutes is None:
                return _err(422, "invalid timeframe")
            if minutes < 1440:
                with fa.lock:
                    df = fa.intraday_bars(syms, start, end, minutes)
            else:
                with fa.lock:
                    df = fa.bars[(fa.bars["symbol"].isin(syms)) & (fa.bars["date"] <= fa.today)].copy()
                ts = df["date"].dt.tz_localize(NY).dt.tz_convert("UTC")
                mask = (ts >= start) & (ts <= end)
                df = df[mask].copy()
                df["t"] = ts[mask]  # (assign() on an empty frame would adopt ts's index -> NaN rows)
                df = df.sort_values(["symbol", "date"])
            page = df.iloc[offset:offset + limit]
            out: dict[str, list] = {}
            for r in page.itertuples():
                out.setdefault(r.symbol, []).append({
                    "t": r.t.strftime("%Y-%m-%dT%H:%M:%SZ"), "o": r.open, "h": r.high, "l": r.low, "c": r.close,
                    "v": int(r.volume), "n": int(r.volume // 100) or 1, "vw": round((r.high + r.low + r.close) / 3, 4),
                })
            nxt = offset + limit
            token = base64.b64encode(str(nxt).encode()).decode() if nxt < len(df) else None
            return jsonify({"bars": out, "next_page_token": token, "currency": "USD"})

        @app.get("/v1beta1/news")
        def news():
            syms = {x for x in (request.args.get("symbols") or "").upper().split(",") if x}
            start, end = _ts(request.args.get("start")), _ts(request.args.get("end"))
            now = pd.Timestamp(fa.now()).tz_convert("UTC")
            limit = int(request.args.get("limit", 10))
            if not 1 <= limit <= 50:
                return _err(422, "limit must be between 1 and 50")
            offset = int(request.args.get("page_token") or 0)
            items = [a for a in fa.news
                     if (not syms or syms & set(a["symbols"]))
                     and pd.Timestamp(a["created_at"]) <= now
                     and (start is None or pd.Timestamp(a["created_at"]) >= start)
                     and (end is None or pd.Timestamp(a["created_at"]) <= end)]
            if request.args.get("sort", "desc") == "asc":
                items = items[::-1]
            page = items[offset:offset + limit]
            token = str(offset + limit) if offset + limit < len(items) else None
            return jsonify({"news": page, "next_page_token": token})

        @app.get("/v2/stocks/snapshots")
        def snapshots():
            syms = [s for s in (request.args.get("symbols") or "").upper().split(",") if s]
            out = {}
            with fa.lock:
                prev_d = fa.dates[fa.i - 1] if fa.i > 0 else None
                for sym in syms:
                    b = fa.bar(sym)
                    if b is None:
                        continue
                    pb = fa.bar(sym, prev_d) if prev_d is not None else None
                    px = fa.price(sym)
                    t = fa._iso()

                    def as_bar(row, d):
                        return {"t": pd.Timestamp(d).tz_localize(NY).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
                                "o": float(row["open"]), "h": float(row["high"]), "l": float(row["low"]),
                                "c": float(row["close"]), "v": int(row["volume"]), "n": 1, "vw": float(row["close"])}

                    out[sym] = {
                        "latestTrade": {"t": t, "x": "V", "p": px, "s": 100, "c": ["@"], "i": 1, "z": "C"},
                        "latestQuote": {"t": t, "ax": "V", "ap": round(px + 0.01, 2), "as": 1, "bx": "V",
                                        "bp": round(px - 0.01, 2), "bs": 1, "c": ["R"], "z": "C"},
                        "minuteBar": as_bar(b, fa.today), "dailyBar": as_bar(b, fa.today),
                        "prevDailyBar": as_bar(pb, prev_d) if pb is not None else None,
                    }
            return jsonify(out)

        # ------------------------------------------------- test / demo hooks
        @app.post("/__sim/advance")
        def sim_advance():
            fa.advance(int(request.args.get("n", 1)))
            return jsonify({"today": fa.today.strftime("%Y-%m-%d"), "equity": fa.equity()})

        @app.get("/__sim/state")
        def sim_state():
            with fa.lock:
                return jsonify({"today": fa.today.strftime("%Y-%m-%d"), "cash": fa.cash, "equity": fa.equity(),
                                "positions": fa.positions, "orders": len(fa.orders)})

        return app


def _timeframe_minutes(tf: str) -> int | None:
    """Alpaca timeframe ('5Min', '5T', '1Hour', '1H', '1Day', '1D') -> minutes (1440 for a day)."""
    import re
    m = re.fullmatch(r"(\d+)(Min|T|Hour|H|Day|D)", tf or "")
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    return n * {"Min": 1, "T": 1, "Hour": 60, "H": 60, "Day": 1440, "D": 1440}[unit]


def _ts(value: str | None) -> pd.Timestamp | None:
    if not value:
        return None
    t = pd.Timestamp(value)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class ServerThread(threading.Thread):
    """Run a Flask/WSGI app on localhost in a background thread (tests + demo)."""

    def __init__(self, app, host: str = "127.0.0.1", port: int = 0):
        super().__init__(daemon=True)
        from werkzeug.serving import make_server
        self.server = make_server(host, port, app, threaded=True)
        self.port = self.server.server_port
        self.url = f"http://{host}:{self.port}"

    def run(self):
        self.server.serve_forever()

    def stop(self):
        self.server.shutdown()
