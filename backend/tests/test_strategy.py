import unittest
from dataclasses import replace

from algotrader.config import StrategyParams
from algotrader.strategy import Holding, Signal, bracket_prices, decide

P = StrategyParams(entry_threshold=0.6, exit_threshold=0.45, max_positions=2, position_size_pct=0.25,
                   stop_loss_pct=0.05, take_profit_pct=0.10, max_holding_days=10)


def sigs(**probas):
    return {s: Signal(s, p, 100.0) for s, p in probas.items()}


class StrategyTests(unittest.TestCase):
    def test_buys_highest_probabilities_up_to_max_positions(self):
        plan = decide(sigs(A=0.7, B=0.9, C=0.65, D=0.5), {}, 100_000, 100_000, P)
        self.assertEqual([d.symbol for d in plan.buys], ["B", "A"])
        self.assertEqual(plan.actions["C"][0], "SKIP")
        self.assertEqual(plan.actions["D"][0], "WATCH")

    def test_position_size_whole_shares_with_slippage_buffer(self):
        plan = decide(sigs(A=0.7), {}, 100_000, 100_000, P)
        b = plan.buys[0]
        self.assertEqual(b.qty, int(25_000 / 1.01 // 100))   # 247 shares, never more than 25% of equity
        self.assertLessEqual(b.qty * 100, 25_000)
        self.assertEqual((b.stop_price, b.take_profit_price), (95.0, 110.0))

    def test_cash_limits_size_and_is_not_double_spent(self):
        plan = decide(sigs(A=0.9, B=0.8), {}, 100_000, 30_000, P)
        spent = sum(d.qty * 100 * 1.01 for d in plan.buys)
        self.assertLessEqual(spent, 30_000 + 1e-6)
        self.assertEqual(len(plan.buys), 2)

    def test_exit_on_low_probability_and_max_hold(self):
        holdings = {"A": Holding("A", 10, 90, 3), "B": Holding("B", 10, 90, 10), "C": Holding("C", 10, 90, 2)}
        plan = decide(sigs(A=0.3, B=0.7, C=0.5), holdings, 100_000, 50_000, P)
        reasons = {d.symbol: d.reason for d in plan.sells}
        self.assertEqual(reasons, {"A": "model_exit", "B": "max_hold"})
        self.assertEqual(plan.actions["C"][0], "HOLD")

    def test_sold_slots_are_reusable_but_sale_cash_is_not(self):
        holdings = {"A": Holding("A", 10, 90, 3), "B": Holding("B", 10, 90, 3)}
        plan = decide(sigs(A=0.3, B=0.5, C=0.9), holdings, 100_000, 0.0, P)
        self.assertEqual([d.symbol for d in plan.sells], ["A"])
        self.assertEqual(plan.buys, [])                     # one slot free, but no settled cash
        self.assertEqual(plan.actions["C"], ("SKIP", "not enough cash for one share"))

    def test_market_filter_and_daily_loss_block_entries_only(self):
        holdings = {"A": Holding("A", 10, 90, 3)}
        plan = decide(sigs(A=0.2, C=0.9), holdings, 100_000, 100_000, P, market_ok=False)
        self.assertEqual([d.symbol for d in plan.sells], ["A"])   # exits still happen
        self.assertEqual(plan.buys, [])
        plan = decide(sigs(C=0.9), {}, 100_000, 100_000, P, allow_entries=False)
        self.assertEqual(plan.buys, [])
        no_filter = replace(P, use_market_filter=False)
        self.assertEqual(len(decide(sigs(C=0.9), {}, 100_000, 100_000, no_filter, market_ok=False).buys), 1)

    def test_blocked_symbols_are_left_alone(self):
        holdings = {"A": Holding("A", 10, 90, 30)}
        plan = decide(sigs(A=0.1, B=0.9), holdings, 100_000, 100_000, P, blocked={"A", "B"})
        self.assertEqual(plan.decisions, [])
        self.assertEqual(plan.actions["A"][0], "PENDING")

    def test_holding_without_fresh_signal_is_kept(self):
        plan = decide({}, {"A": Holding("A", 10, 90, 2)}, 100_000, 0, P)
        self.assertEqual(plan.decisions, [])
        self.assertEqual(plan.actions["A"], ("HOLD", "no fresh data"))

    def test_bracket_prices_respect_penny_increments(self):
        stop, target = bracket_prices(123.4567, P)
        self.assertEqual(stop, round(123.4567 * 0.95, 2))
        self.assertEqual(target, round(123.4567 * 1.10, 2))
        stop, _ = bracket_prices(0.8123, P)
        self.assertEqual(stop, round(0.8123 * 0.95, 4))


class ManualPositionTests(unittest.TestCase):
    def test_manual_symbols_are_never_traded(self):
        holdings = {"A": Holding("A", 10, 90, 30)}
        plan = decide(sigs(A=0.01, B=0.95), holdings, 100_000, 100_000, P, manual={"A", "B"})
        self.assertEqual(plan.decisions, [])
        self.assertEqual(plan.actions["A"][0], "MANUAL")
        self.assertEqual(plan.actions["B"][0], "MANUAL")

    def test_manual_positions_do_not_use_bot_slots(self):
        plan = decide(sigs(B=0.9, C=0.8), {}, 100_000, 100_000, P, manual={"X", "Y", "Z"})
        self.assertEqual([d.symbol for d in plan.buys], ["B", "C"])


if __name__ == "__main__":
    unittest.main()
