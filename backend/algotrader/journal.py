"""Journal of every bot run, model signal and order (audit trail + dashboard feed).

Stored in the SQLite file backend/data/algotrader.db, or in PostgreSQL when DATABASE_URL is set (db.py).
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

from .db import Database

TABLES = ["runs", "signals", "orders"]
SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    as_of TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    message TEXT,
    dry_run INTEGER NOT NULL DEFAULT 0,
    equity REAL,
    n_orders INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS signals (
    run_id INTEGER NOT NULL,
    as_of TEXT NOT NULL,
    symbol TEXT NOT NULL,
    proba REAL,
    price REAL,
    action TEXT,
    reason TEXT,
    held INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER,
    created_at TEXT NOT NULL,
    as_of TEXT,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL,
    reason TEXT,
    proba REAL,
    price REAL,
    stop_price REAL,
    take_profit_price REAL,
    client_order_id TEXT,
    alpaca_order_id TEXT,
    status TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_run ON signals(run_id);
CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol, created_at);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plain(value):
    """numpy numbers -> plain Python numbers (PostgreSQL drivers don't take numpy types)."""
    if value is not None and hasattr(value, "item") and not isinstance(value, (str, bytes)):
        return value.item()
    return value


class Journal:
    def __init__(self, target: Path | str):
        """target: a SQLite file path, or a postgresql:// URL (DATABASE_URL)."""
        self.db = Database(target)
        self._lock = threading.Lock()
        self.db.create_tables(SCHEMA, TABLES)

    def _conn(self):
        return self.db.connect()

    # ------------------------------------------------------------------ writes
    def start_run(self, dry_run: bool) -> int:
        with self._lock, self._conn() as c:
            return c.insert("INSERT INTO runs (started_at, dry_run) VALUES (?, ?)", (_now(), int(dry_run)))

    def finish_run(self, run_id: int, status: str, message: str = "", as_of: str | None = None,
                   equity: float | None = None, n_orders: int = 0) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, message=?, as_of=?, equity=?, n_orders=? WHERE id=?",
                (_now(), status, message, as_of, equity, n_orders, run_id),
            )

    def add_signals(self, run_id: int, as_of: str, rows: list[dict]) -> None:
        with self._lock, self._conn() as c:
            c.executemany(
                "INSERT INTO signals (run_id, as_of, symbol, proba, price, action, reason, held) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(run_id, as_of, r["symbol"], _plain(r.get("proba")), _plain(r.get("price")), r.get("action"),
                  r.get("reason"), int(bool(r.get("held")))) for r in rows],
            )

    def add_order(self, run_id: int | None, **fields) -> None:
        cols = ["symbol", "side", "qty", "reason", "proba", "price", "stop_price", "take_profit_price",
                "client_order_id", "alpaca_order_id", "status", "error", "as_of"]
        with self._lock, self._conn() as c:
            c.execute(
                f"INSERT INTO orders (run_id, created_at, {', '.join(cols)}) "
                f"VALUES (?, ?, {', '.join('?' for _ in cols)})",
                (run_id, _now(), *[_plain(fields.get(k)) for k in cols]),
            )

    # ------------------------------------------------------------------- reads
    def latest_signals(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT r.id, r.as_of, r.finished_at, r.dry_run FROM runs r "
                "WHERE EXISTS (SELECT 1 FROM signals s WHERE s.run_id = r.id) ORDER BY r.id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return {"as_of": None, "run_id": None, "signals": []}
            sigs = c.execute(
                "SELECT symbol, proba, price, action, reason, held FROM signals WHERE run_id=? ORDER BY proba DESC",
                (row["id"],),
            ).fetchall()
            return {
                "run_id": row["id"], "as_of": row["as_of"], "generated_at": row["finished_at"],
                "dry_run": bool(row["dry_run"]), "signals": [dict(s) for s in sigs],
            }

    def recent_runs(self, limit: int = 20) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]

    def recent_orders(self, limit: int = 50) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,))]

    def bot_order_ids(self) -> set[str]:
        with self._conn() as c:
            return {r[0] for r in c.execute(
                "SELECT alpaca_order_id FROM orders WHERE alpaca_order_id IS NOT NULL AND reason != 'manual'")}

    def manual_order_ids(self) -> set[str]:
        with self._conn() as c:
            return {r[0] for r in c.execute(
                "SELECT alpaca_order_id FROM orders WHERE alpaca_order_id IS NOT NULL AND reason = 'manual'")}

    def last_buy(self, symbol: str) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM orders WHERE symbol=? AND side='buy' AND status NOT IN ('error','dry_run') "
                "AND client_order_id LIKE 'algo-%' ORDER BY id DESC LIMIT 1", (symbol,),
            ).fetchone()
            return dict(row) if row else None
