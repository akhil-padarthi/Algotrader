"""Event-driven daily backtest that reuses the live strategy code.

Timeline for each session d:
  1. open:   fill orders decided at the previous close (sells first), with slippage
  2. intraday: bracket stop-loss / take-profit checked against the day's low/high
             (gap through the stop -> filled at the open; stop and target both
             touched on the same day -> assume the stop, the conservative case)
  3. close:  mark positions to market
  4. after close: strategy.decide() on today's probabilities -> orders for d+1
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .strategy import Holding, Signal, decide

TRADING_DAYS = 252


@dataclass
class BacktestResult:
    equity: pd.DataFrame     # date, equity, cash, invested, positions, benchmark
    trades: pd.DataFrame     # one row per round trip (open ones marked to market)
    metrics: dict

    def to_dict(self) -> dict:
        eq = self.equity.copy()
        eq["date"] = eq["date"].dt.strftime("%Y-%m-%d")
        tr = self.trades.copy()
        for col in ("entry_date", "exit_date"):
            if col in tr:
                tr[col] = pd.to_datetime(tr[col]).dt.strftime("%Y-%m-%d")
        return _json_safe({
            "metrics": self.metrics,
            "equity": eq.round(2).to_dict(orient="records"),
            "trades": tr.round(4).to_dict(orient="records"),
        })


def run_backtest(
    pred: pd.DataFrame,
    params,
    initial_cash: float = 100_000.0,
    benchmark: pd.DataFrame | None = None,
) -> BacktestResult:
    """``pred`` columns: date, symbol, open, high, low, close, proba, market_ok."""
    need = {"date", "symbol", "open", "high", "low", "close", "proba"}
    missing = need - set(pred.columns)
    if missing:
        raise ValueError(f"Backtest input missing columns: {sorted(missing)}")
    if pred.empty:
        raise ValueError("Backtest input is empty")

    pred = pred.sort_values(["date", "symbol"])
    if "market_ok" not in pred:
        pred = pred.assign(market_ok=True)
    slip = params.slippage_bps / 10_000.0
    dates = list(pd.to_datetime(pred["date"].unique()))
    by_date = {d: g.set_index("symbol") for d, g in pred.groupby("date", sort=True)}

    cash = float(initial_cash)
    positions: dict[str, dict] = {}
    pending = []
    last_close: dict[str, float] = {}
    eq_rows, trades = [], []
    prev_equity = cash

    def close_trade(sym, pos, price, d, reason):
        nonlocal cash
        cash += pos["qty"] * price
        trades.append({
            "symbol": sym, "qty": pos["qty"],
            "entry_date": pos["entry_date"], "entry_price": pos["entry_price"],
            "exit_date": d, "exit_price": price,
            "pnl": (price - pos["entry_price"]) * pos["qty"],
            "return": price / pos["entry_price"] - 1,
            "days_held": pos["days_held"], "reason": reason, "entry_proba": pos["proba"],
        })

    for i, d in enumerate(dates):
        bars = by_date[d]

        # 1. open: execute yesterday's decisions
        for dec in pending:
            if dec.symbol not in bars.index:
                continue  # no bar today (halt / missing data) -> order not filled
            o = float(bars.at[dec.symbol, "open"])
            if dec.side == "sell":
                pos = positions.pop(dec.symbol, None)
                if pos:
                    close_trade(dec.symbol, pos, o * (1 - slip), d, dec.reason)
            else:
                if dec.symbol in positions:
                    continue
                fill = o * (1 + slip)
                qty = int(dec.qty)
                if qty * fill > cash:
                    qty = math.floor(cash / fill)
                if qty < 1:
                    continue
                cash -= qty * fill
                positions[dec.symbol] = {
                    "qty": qty, "entry_price": fill, "entry_date": d, "days_held": 0,
                    "stop": dec.stop_price, "target": dec.take_profit_price, "proba": dec.proba,
                }
        pending = []

        # 2. intraday bracket exits
        for sym, pos in list(positions.items()):
            if sym not in bars.index:
                continue
            o, h, l = (float(bars.at[sym, c]) for c in ("open", "high", "low"))
            stop, target = pos["stop"], pos["target"]
            exit_px, reason = None, None
            if stop is not None and o <= stop:
                exit_px, reason = o * (1 - slip), "stop_loss"
            elif stop is not None and l <= stop:
                exit_px, reason = stop * (1 - slip), "stop_loss"
            elif target is not None and o >= target:
                exit_px, reason = o, "take_profit"
            elif target is not None and h >= target:
                exit_px, reason = target, "take_profit"
            if exit_px is not None:
                pos["days_held"] += 1
                close_trade(sym, positions.pop(sym), exit_px, d, reason)

        # 3. close: mark to market
        for sym, row in bars.iterrows():
            last_close[sym] = float(row["close"])
        invested = 0.0
        for sym, pos in positions.items():
            pos["days_held"] += 1
            invested += pos["qty"] * last_close.get(sym, pos["entry_price"])
        equity = cash + invested
        eq_rows.append({"date": d, "equity": equity, "cash": cash, "invested": invested,
                        "positions": len(positions)})

        # 4. after close: decide tomorrow's orders
        if i < len(dates) - 1:
            signals = {
                sym: Signal(sym, float(row["proba"]), float(row["close"]))
                for sym, row in bars.iterrows() if pd.notna(row["proba"])
            }
            holdings = {
                sym: Holding(sym, pos["qty"], pos["entry_price"], pos["days_held"])
                for sym, pos in positions.items()
            }
            market_ok = bool(bars["market_ok"].iloc[0])
            allow = (equity / prev_equity - 1) > -params.max_daily_loss_pct if prev_equity > 0 else True
            plan = decide(signals, holdings, equity, cash, params, market_ok=market_ok, allow_entries=allow)
            pending = plan.sells + plan.buys
        prev_equity = equity

    # positions still open at the end: mark to market, flagged as open
    for sym, pos in positions.items():
        px = last_close.get(sym, pos["entry_price"])
        trades.append({
            "symbol": sym, "qty": pos["qty"], "entry_date": pos["entry_date"], "entry_price": pos["entry_price"],
            "exit_date": dates[-1], "exit_price": px, "pnl": (px - pos["entry_price"]) * pos["qty"],
            "return": px / pos["entry_price"] - 1, "days_held": pos["days_held"], "reason": "open",
            "entry_proba": pos["proba"],
        })

    equity_df = pd.DataFrame(eq_rows)
    if benchmark is not None and not benchmark.empty:
        b = benchmark.sort_values("date").set_index("date")["close"].reindex(equity_df["date"]).ffill().bfill()
        equity_df["benchmark"] = initial_cash * (b.values / b.values[0])
    trades_df = pd.DataFrame(trades, columns=[
        "symbol", "qty", "entry_date", "entry_price", "exit_date", "exit_price",
        "pnl", "return", "days_held", "reason", "entry_proba",
    ])
    return BacktestResult(equity_df, trades_df, compute_metrics(equity_df, trades_df, initial_cash))


def perf_stats(values: pd.Series) -> dict:
    values = pd.Series(values, dtype=float).dropna()
    if len(values) < 2:
        return {"total_return": 0.0, "cagr": None, "volatility": None, "sharpe": None,
                "sortino": None, "max_drawdown": 0.0}
    rets = values.pct_change().dropna()
    total = values.iloc[-1] / values.iloc[0] - 1
    years = len(rets) / TRADING_DAYS
    std = rets.std()
    downside = rets[rets < 0].std()
    return {
        "total_return": float(total),
        "cagr": float((1 + total) ** (1 / years) - 1) if years > 0 and total > -1 else None,
        "volatility": float(std * np.sqrt(TRADING_DAYS)) if std > 0 else None,
        "sharpe": float(rets.mean() / std * np.sqrt(TRADING_DAYS)) if std > 0 else None,
        "sortino": float(rets.mean() / downside * np.sqrt(TRADING_DAYS)) if downside and downside > 0 else None,
        "max_drawdown": float((values / values.cummax() - 1).min()),
    }


def compute_metrics(equity_df: pd.DataFrame, trades_df: pd.DataFrame, initial_cash: float) -> dict:
    m = perf_stats(equity_df["equity"])
    closed = trades_df[trades_df["reason"] != "open"]
    wins = closed[closed["pnl"] > 0]
    losses = closed[closed["pnl"] <= 0]
    m.update({
        "start": equity_df["date"].iloc[0].strftime("%Y-%m-%d"),
        "end": equity_df["date"].iloc[-1].strftime("%Y-%m-%d"),
        "sessions": int(len(equity_df)),
        "initial_equity": float(initial_cash),
        "final_equity": float(equity_df["equity"].iloc[-1]),
        "trades": int(len(closed)),
        "open_positions": int((trades_df["reason"] == "open").sum()),
        "win_rate": float(len(wins) / len(closed)) if len(closed) else None,
        "avg_trade_return": float(closed["return"].mean()) if len(closed) else None,
        "avg_win": float(wins["return"].mean()) if len(wins) else None,
        "avg_loss": float(losses["return"].mean()) if len(losses) else None,
        "profit_factor": float(wins["pnl"].sum() / -losses["pnl"].sum()) if len(losses) and losses["pnl"].sum() < 0 else None,
        "avg_days_held": float(closed["days_held"].mean()) if len(closed) else None,
        "exit_reasons": closed["reason"].value_counts().to_dict(),
        "exposure": float((equity_df["invested"] / equity_df["equity"]).mean()),
    })
    if "benchmark" in equity_df:
        b = perf_stats(equity_df["benchmark"])
        m.update({
            "benchmark_return": b["total_return"],
            "benchmark_sharpe": b["sharpe"],
            "benchmark_max_drawdown": b["max_drawdown"],
            "excess_return": m["total_return"] - b["total_return"],
        })
    return _json_safe(m)


def _json_safe(obj):
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, pd.Timestamp):
        return obj.strftime("%Y-%m-%d")
    return obj
