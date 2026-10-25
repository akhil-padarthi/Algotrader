"""Entry/exit rules shared by the backtest and the live bot.

Exits (checked first, for each holding):
  * model probability fell below EXIT_THRESHOLD        -> "model_exit"
  * held for MAX_HOLDING_DAYS sessions                  -> "max_hold"
  * stop-loss / take-profit are attached to every entry as a bracket order
    (enforced intraday by Alpaca live, simulated with daily high/low in the backtest)

Entries:
  * probability >= ENTRY_THRESHOLD, ranked highest first
  * at most MAX_POSITIONS open, POSITION_SIZE_PCT of equity each, whole shares
  * skipped when SPY is below its 50-day average (market filter) or after a
    day worse than -MAX_DAILY_LOSS_PCT
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .alpaca_client import round_price


@dataclass
class Signal:
    symbol: str
    proba: float
    price: float            # latest close


@dataclass
class Holding:
    symbol: str
    qty: float
    entry_price: float
    days_held: int          # sessions held, counting the entry session


@dataclass
class Decision:
    symbol: str
    side: str               # "buy" | "sell"
    qty: float
    reason: str
    proba: float | None = None
    price: float | None = None
    stop_price: float | None = None
    take_profit_price: float | None = None


@dataclass
class Plan:
    decisions: list[Decision] = field(default_factory=list)
    actions: dict[str, tuple[str, str]] = field(default_factory=dict)  # symbol -> (ACTION, why)

    @property
    def sells(self) -> list[Decision]:
        return [d for d in self.decisions if d.side == "sell"]

    @property
    def buys(self) -> list[Decision]:
        return [d for d in self.decisions if d.side == "buy"]


def bracket_prices(price: float, params) -> tuple[float, float]:
    stop = round_price(price * (1 - params.stop_loss_pct))
    target = round_price(price * (1 + params.take_profit_pct))
    return stop, target


def decide(
    signals: dict[str, Signal],
    holdings: dict[str, Holding],
    equity: float,
    cash: float,
    params,
    market_ok: bool = True,
    allow_entries: bool = True,
    blocked: set[str] | frozenset = frozenset(),
    manual: set[str] | frozenset = frozenset(),
    vetoes: dict[str, str] | None = None,
) -> Plan:
    """Turn model probabilities + current portfolio into orders.

    ``blocked``: symbols with an order already working (live only) - left alone.
    ``manual``: symbols the user bought themselves - never bought or sold by the bot.
    ``vetoes``: symbol -> reason not to open a new position today (e.g. clearly negative news).
    Cash from sells is NOT reused in the same cycle (it only settles after the fill).
    """
    plan = Plan()
    for symbol in sorted(manual):
        plan.actions[symbol] = ("MANUAL", "you bought this yourself - the bot leaves it alone")

    # ---------------------------------------------------------------- exits
    staying = 0
    for symbol, h in sorted(holdings.items()):
        if symbol in manual:
            continue
        if symbol in blocked:
            plan.actions[symbol] = ("PENDING", "order already working")
            staying += 1
            continue
        sig = signals.get(symbol)
        if sig is not None and sig.proba < params.exit_threshold:
            plan.decisions.append(Decision(symbol, "sell", h.qty, "model_exit", sig.proba, sig.price))
            plan.actions[symbol] = ("SELL", f"probability {sig.proba:.2f} < exit {params.exit_threshold:.2f}")
        elif h.days_held >= params.max_holding_days:
            plan.decisions.append(Decision(symbol, "sell", h.qty, "max_hold", sig.proba if sig else None,
                                           sig.price if sig else None))
            plan.actions[symbol] = ("SELL", f"held {h.days_held} sessions (max {params.max_holding_days})")
        else:
            staying += 1
            why = "no fresh data" if sig is None else f"probability {sig.proba:.2f} >= exit {params.exit_threshold:.2f}"
            plan.actions[symbol] = ("HOLD", why)

    # -------------------------------------------------------------- entries
    slots = params.max_positions - staying
    candidates = sorted(
        (s for s in signals.values() if s.symbol not in holdings and s.symbol not in blocked and s.symbol not in manual),
        key=lambda s: s.proba,
        reverse=True,
    )
    budget_cash = max(cash, 0.0)
    for sig in candidates:
        if sig.proba < params.entry_threshold:
            plan.actions[sig.symbol] = ("WATCH", f"probability {sig.proba:.2f} < entry {params.entry_threshold:.2f}")
            continue
        if not allow_entries:
            plan.actions[sig.symbol] = ("SKIP", "daily loss limit hit - no new entries")
            continue
        if vetoes and sig.symbol in vetoes:
            plan.actions[sig.symbol] = ("SKIP", vetoes[sig.symbol])
            continue
        if params.use_market_filter and not market_ok:
            plan.actions[sig.symbol] = ("SKIP", "market filter: benchmark below 50-day average")
            continue
        if slots <= 0:
            plan.actions[sig.symbol] = ("SKIP", f"max {params.max_positions} positions reached")
            continue
        if not sig.price or sig.price <= 0:
            plan.actions[sig.symbol] = ("SKIP", "no valid price")
            continue
        # leave room for slippage between the signal close and the fill
        budget = min(equity * params.position_size_pct, budget_cash) / (1 + 0.01)
        qty = math.floor(budget / sig.price)
        if qty < 1:
            plan.actions[sig.symbol] = ("SKIP", "not enough cash for one share")
            continue
        stop, target = bracket_prices(sig.price, params)
        plan.decisions.append(Decision(sig.symbol, "buy", qty, "model_entry", sig.proba, sig.price, stop, target))
        plan.actions[sig.symbol] = ("BUY", f"probability {sig.proba:.2f} >= entry {params.entry_threshold:.2f}")
        budget_cash -= qty * sig.price * 1.01
        slots -= 1
    return plan
