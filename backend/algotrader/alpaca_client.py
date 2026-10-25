"""Thin, well-tested client for the Alpaca REST API (Trading v2 + Market Data v2).

Only the endpoints the bot needs are implemented.  Responses are returned as
plain dicts/lists exactly as Alpaca sends them (numbers in trading responses
arrive as strings, e.g. ``"equity": "100000.00"``), with a couple of helpers
that convert market data to Pandas.

Docs: https://docs.alpaca.markets/reference
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Iterable

import pandas as pd
import requests

log = logging.getLogger(__name__)

RETRY_STATUSES = {429, 500, 502, 503, 504}


class AlpacaError(RuntimeError):
    def __init__(self, status: int, message: str, payload: Any = None):
        super().__init__(f"Alpaca API error {status}: {message}")
        self.status = status
        self.message = message
        self.payload = payload


def _iso(value: date | datetime | str | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.strftime("%Y-%m-%dT%H:%M:%SZ")
        return value.isoformat()
    return value.isoformat()


class AlpacaClient:
    """Alpaca REST client.

    Parameters
    ----------
    trading_url: ``https://paper-api.alpaca.markets`` for paper trading.
    data_url:    ``https://data.alpaca.markets``.
    feed:        ``iex`` (free) or ``sip`` (all exchanges).
    """

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        trading_url: str,
        data_url: str,
        feed: str = "iex",
        timeout: float = 20.0,
        max_retries: int = 4,
        session: requests.Session | None = None,
    ):
        if not api_key or not secret_key:
            raise ValueError("Alpaca API key and secret are required")
        self.trading_url = trading_url.rstrip("/")
        self.data_url = data_url.rstrip("/")
        self.feed = feed
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = session or requests.Session()
        self.session.headers.update({
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
            "Accept": "application/json",
            "User-Agent": "AlgoTrader/1.0",
        })

    @classmethod
    def from_settings(cls, settings) -> "AlpacaClient":
        return cls(
            settings.alpaca_api_key,
            settings.alpaca_secret_key,
            settings.alpaca_trading_url,
            settings.alpaca_data_url,
            settings.alpaca_data_feed,
        )

    # ------------------------------------------------------------------ core
    def _request(self, method: str, url: str, params: dict | None = None, json: dict | None = None):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            try:
                resp = self.session.request(method, url, params=params, json=json, timeout=self.timeout)
            except requests.RequestException as exc:
                if attempt > self.max_retries:
                    raise AlpacaError(0, f"network error: {exc}") from exc
                self._sleep_backoff(attempt, None)
                continue

            if resp.status_code in RETRY_STATUSES and attempt <= self.max_retries:
                log.warning("Alpaca %s %s -> %s, retrying (%d)", method, url, resp.status_code, attempt)
                self._sleep_backoff(attempt, resp.headers.get("Retry-After"))
                continue

            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                    message = payload.get("message", resp.text) if isinstance(payload, dict) else resp.text
                except ValueError:
                    payload, message = None, resp.text
                raise AlpacaError(resp.status_code, message or resp.reason, payload)

            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    def _sleep_backoff(self, attempt: int, retry_after: str | None) -> None:
        delay = min(2 ** (attempt - 1), 30)
        if retry_after:
            try:
                delay = max(delay, float(retry_after))
            except ValueError:
                pass
        time.sleep(delay * self.backoff_scale)

    backoff_scale = 1.0  # tests set this to 0 to avoid sleeping

    def _trading(self, method: str, path: str, **kw):
        return self._request(method, f"{self.trading_url}{path}", **kw)

    def _data(self, method: str, path: str, **kw):
        return self._request(method, f"{self.data_url}{path}", **kw)

    # --------------------------------------------------------------- trading
    def get_account(self) -> dict:
        return self._trading("GET", "/v2/account")

    def get_clock(self) -> dict:
        return self._trading("GET", "/v2/clock")

    def get_calendar(self, start: date | str, end: date | str) -> list[dict]:
        return self._trading("GET", "/v2/calendar", params={"start": _iso(start), "end": _iso(end)})

    def get_assets(self, status: str = "active", asset_class: str = "us_equity") -> list[dict]:
        """Every tradable instrument (~12k for US equities) - cache the result."""
        return self._trading("GET", "/v2/assets", params={"status": status, "asset_class": asset_class}) or []

    def get_asset(self, symbol: str) -> dict:
        return self._trading("GET", f"/v2/assets/{symbol}")

    def get_positions(self) -> list[dict]:
        return self._trading("GET", "/v2/positions")

    def list_orders(
        self,
        status: str = "all",
        limit: int = 50,
        symbols: Iterable[str] | None = None,
        after: datetime | str | None = None,
        direction: str = "desc",
        nested: bool = True,
    ) -> list[dict]:
        params = {
            "status": status,
            "limit": min(int(limit), 500),
            "direction": direction,
            "nested": str(nested).lower(),
            "after": _iso(after),
            "symbols": ",".join(symbols) if symbols else None,
        }
        return self._trading("GET", "/v2/orders", params=params)

    def submit_order(
        self,
        symbol: str,
        side: str,
        qty: int | float | None = None,
        notional: float | None = None,
        type: str = "market",
        time_in_force: str = "day",
        limit_price: float | None = None,
        stop_price: float | None = None,
        order_class: str | None = None,
        take_profit: float | None = None,
        stop_loss: float | None = None,
        client_order_id: str | None = None,
    ) -> dict:
        if (qty is None) == (notional is None):
            raise ValueError("Specify exactly one of qty or notional")
        body: dict[str, Any] = {
            "symbol": symbol,
            "side": side,
            "type": type,
            "time_in_force": time_in_force,
        }
        if qty is not None:
            body["qty"] = str(qty)
        if notional is not None:
            body["notional"] = str(round(float(notional), 2))
        if limit_price is not None:
            body["limit_price"] = str(round_price(limit_price))
        if stop_price is not None:
            body["stop_price"] = str(round_price(stop_price))
        if order_class:
            body["order_class"] = order_class
        if take_profit is not None:
            body["take_profit"] = {"limit_price": str(round_price(take_profit))}
        if stop_loss is not None:
            body["stop_loss"] = {"stop_price": str(round_price(stop_loss))}
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._trading("POST", "/v2/orders", json=body)

    def cancel_order(self, order_id: str) -> None:
        self._trading("DELETE", f"/v2/orders/{order_id}")

    def close_position(self, symbol: str) -> dict:
        return self._trading("DELETE", f"/v2/positions/{symbol}")

    def get_portfolio_history(
        self,
        period: str | None = "3M",
        timeframe: str = "1D",
        start: date | datetime | str | None = None,
        end: date | datetime | str | None = None,
    ) -> dict:
        """Account equity over time. Alpaca rejects `period` together with both start and end,
        and only accepts intraday timeframes (1Min/5Min/15Min/1H) for spans under 30 days."""
        params = {"timeframe": timeframe, "start": _iso(start), "end": _iso(end)}
        if not (start and end):
            params["period"] = period
        return self._trading("GET", "/v2/account/portfolio/history", params=params)

    # ----------------------------------------------------------- market data
    def get_bars(
        self,
        symbols: Iterable[str],
        start: date | datetime | str,
        end: date | datetime | str | None = None,
        timeframe: str = "1Day",
        adjustment: str = "all",
        page_limit: int = 10000,
    ) -> pd.DataFrame:
        """Historical bars for several symbols, following pagination.

        Returns a long DataFrame: symbol, timestamp (UTC), open, high, low,
        close, volume, trade_count, vwap.
        """
        symbols = [s.upper() for s in symbols]
        rows: list[dict] = []
        page_token = None
        pages = 0
        while True:
            payload = self._data(
                "GET",
                "/v2/stocks/bars",
                params={
                    "symbols": ",".join(symbols),
                    "timeframe": timeframe,
                    "start": _iso(start),
                    "end": _iso(end),
                    "adjustment": adjustment,
                    "feed": self.feed,
                    "limit": page_limit,
                    "sort": "asc",
                    "page_token": page_token,
                },
            ) or {}
            pages += 1
            for symbol, bars in (payload.get("bars") or {}).items():
                for b in bars or []:
                    rows.append({
                        "symbol": symbol,
                        "timestamp": b.get("t"),
                        "open": b.get("o"),
                        "high": b.get("h"),
                        "low": b.get("l"),
                        "close": b.get("c"),
                        "volume": b.get("v"),
                        "trade_count": b.get("n"),
                        "vwap": b.get("vw"),
                    })
            page_token = payload.get("next_page_token")
            if not page_token:
                break
            if pages > 10_000:  # defensive: never loop forever
                raise AlpacaError(0, "pagination did not terminate")
        log.info("Fetched %d bars for %d symbols in %d page(s)", len(rows), len(symbols), pages)
        df = pd.DataFrame(rows, columns=[
            "symbol", "timestamp", "open", "high", "low", "close", "volume", "trade_count", "vwap",
        ])
        if not df.empty:
            df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, format="ISO8601")
        return df

    def get_snapshots(self, symbols: Iterable[str]) -> dict:
        symbols = [s.upper() for s in symbols]
        return self._data(
            "GET", "/v2/stocks/snapshots", params={"symbols": ",".join(symbols), "feed": self.feed}
        ) or {}


    def get_news(self, symbols: Iterable[str] | None = None, start: date | datetime | str | None = None,
                 end: date | datetime | str | None = None, max_items: int = 150) -> list[dict]:
        """Recent news articles (Benzinga via Alpaca), newest first, following pagination."""
        out: list[dict] = []
        page_token = None
        while len(out) < max_items:
            payload = self._data("GET", "/v1beta1/news", params={
                "symbols": ",".join(s.upper() for s in symbols) if symbols else None,
                "start": _iso(start), "end": _iso(end), "sort": "desc",
                "limit": min(50, max_items - len(out)), "include_content": "false",
                "page_token": page_token,
            }) or {}
            out.extend(payload.get("news") or [])
            page_token = payload.get("next_page_token")
            if not page_token:
                break
        return out[:max_items]


def round_price(price: float) -> float:
    """Alpaca rejects sub-penny prices >= $1 (max 2 decimals) and allows 4 below $1."""
    price = float(price)
    return round(price, 2) if price >= 1 else round(price, 4)


def num(value: Any, default: float = 0.0) -> float:
    """Alpaca sends most numbers as strings."""
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
