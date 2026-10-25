"""Train -> evaluate -> backtest pipeline shared by the CLI and the demo."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

from . import model as model_mod
from .backtest import BacktestResult, run_backtest
from .features import add_labels, build_features, feature_columns

log = logging.getLogger(__name__)
BACKTEST_FILE = "backtest.json"


# Market-condition features have to earn their place: they're kept only if they improve the
# walk-forward CV score clearly (on average, and in most folds). Only the training period is used
# for this choice - the hold-out test plays no part in it.
MIN_CONTEXT_GAIN = 0.01


def context_wins(base_meta: dict, ctx_meta: dict) -> bool:
    b = {f["fold"]: f["auc"] for f in base_meta["cv_folds"] if f["auc"] is not None}
    c = {f["fold"]: f["auc"] for f in ctx_meta["cv_folds"] if f["auc"] is not None}
    folds = sorted(set(b) & set(c))
    if not folds:
        return False
    better = sum(c[k] > b[k] for k in folds)
    gain = (ctx_meta["cv_mean"]["auc"] or 0.0) - (base_meta["cv_mean"]["auc"] or 0.0)
    return gain >= MIN_CONTEXT_GAIN and better * 2 > len(folds)


def train_from_bars(settings, bars: pd.DataFrame, macro: pd.DataFrame | None = None, refit_all: bool = True,
                    context_note: str | None = None) -> tuple[model_mod.TrainResult, BacktestResult]:
    symbols = [s for s in settings.symbols if s in set(bars["symbol"])]
    use_ctx = settings.use_market_context
    frame = build_features(bars, settings.benchmark, symbols=symbols,
                           sectors=settings.sector_map if use_ctx else None, macro=macro if use_ctx else None)
    frame = add_labels(frame, settings.model.horizon_days, settings.model.label_threshold)
    args = (settings.model, settings.strategy.entry_threshold, refit_all)
    base_cols, ctx_cols = feature_columns(frame), feature_columns(frame, context=True)

    selection = {"offered": False, "chosen": "base", "note": context_note}
    if use_ctx and ctx_cols != base_cols:
        log.info("Comparing feature sets: price-only (%d) vs price + market conditions (%d)", len(base_cols), len(ctx_cols))
        base = model_mod.train_and_evaluate(frame, *args, cols=base_cols, rows_from=ctx_cols)
        ctx = model_mod.train_and_evaluate(frame, *args, cols=ctx_cols, rows_from=ctx_cols)
        b_auc, c_auc = base.meta["cv_mean"]["auc"] or 0.0, ctx.meta["cv_mean"]["auc"] or 0.0
        chosen = "context" if context_wins(base.meta, ctx.meta) else "base"
        result = ctx if chosen == "context" else base
        added = [c for c in ctx_cols if c not in base_cols]
        selection = {
            "offered": True, "chosen": chosen, "min_gain": MIN_CONTEXT_GAIN, "added_features": added,
            "base": {"cv_auc": b_auc, "oos_auc": base.meta["oos"]["auc"], "n_features": len(base_cols)},
            "context": {"cv_auc": c_auc, "oos_auc": ctx.meta["oos"]["auc"], "n_features": len(ctx_cols)},
            "note": " ".join(filter(None, [context_note, (
                "Market conditions improved the walk-forward test, so the model uses them."
                if chosen == "context" else
                "Market conditions didn't clearly improve the walk-forward test, so the model leaves them out "
                "(the dashboard still shows them).")])),
        }
        log.info("Walk-forward AUC: price-only %.3f vs with market conditions %.3f -> using %s",
                 b_auc, c_auc, "market conditions" if chosen == "context" else "price-only")
    else:
        result = model_mod.train_and_evaluate(frame, *args, cols=base_cols)
        if use_ctx and not context_note:
            selection["note"] = "No market-condition data was available, so the model uses price data only."
    result.meta["feature_selection"] = selection
    result.meta["sectors"] = settings.sector_map if use_ctx else {}
    model_mod.save(result, settings.model_dir)
    bt = backtest_from_predictions(settings, result.oos_predictions, bars)
    return result, bt


def backtest_from_predictions(settings, oos: pd.DataFrame, bars: pd.DataFrame | None) -> BacktestResult:
    bench = None
    if bars is not None:
        b = bars[bars["symbol"] == settings.benchmark]
        bench = b[b["date"].isin(oos["date"].unique())] if not b.empty else None
    bt = run_backtest(oos, settings.strategy, benchmark=bench)
    save_backtest(settings.model_dir, bt)
    return bt


def save_backtest(model_dir: Path, bt: BacktestResult) -> Path:
    path = Path(model_dir) / BACKTEST_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bt.to_dict()))
    return path


def pct(x) -> str:
    return "n/a" if x is None else f"{x:+.2%}"


def report(result: model_mod.TrainResult | None, bt: BacktestResult) -> str:
    lines = []
    if result is not None:
        m = result.meta
        cv, oos = m["cv_mean"], m["oos"]
        lines += [
            "",
            "MODEL (RandomForestClassifier)",
            f"  data            {m['data_start']} -> {m['data_end']}  ({len(m['symbols'])} symbols, {len(m['features'])} features)",
            f"  train period    {m['train_period'][0]} -> {m['train_period'][1]}  ({m['n_train_rows']} rows)",
            f"  test period     {m['test_period'][0]} -> {m['test_period'][1]}  ({m['n_test_rows']} rows, never trained on)",
            f"  walk-forward CV AUC {_f(cv['auc'])}   accuracy {_f(cv['accuracy'])}",
            f"  out-of-sample   AUC {_f(oos['auc'])}   accuracy {_f(oos['accuracy'])}   base rate {_f(oos['base_rate'])}",
            f"  precision when signalling (p >= {m['entry_threshold']}): {_f(oos['precision_at_entry'])}"
            f"  (coverage {_f(oos['coverage'])})",
            "  top features    " + ", ".join(f"{k} {v:.3f}" for k, v in m["feature_importance"][:6]),
        ]
        sel = m.get("feature_selection") or {}
        if sel.get("offered"):
            lines.append(f"  market conditions (FRED + sectors): walk-forward AUC {_f(sel['context']['cv_auc'])} "
                         f"vs {_f(sel['base']['cv_auc'])} without -> {'USED' if sel['chosen'] == 'context' else 'not used'}")
        elif sel.get("note"):
            lines.append(f"  market conditions: {sel['note']}")
    x = bt.metrics
    lines += [
        "",
        f"BACKTEST (out-of-sample {x['start']} -> {x['end']}, {x['sessions']} sessions, $100k start)",
        f"  strategy return {pct(x['total_return'])}   vs {pct(x.get('benchmark_return'))} buy-and-hold benchmark",
        f"  sharpe {_f(x['sharpe'])}   max drawdown {pct(x['max_drawdown'])}   exposure {_f(x['exposure'])}",
        f"  trades {x['trades']}   win rate {_f(x['win_rate'])}   avg trade {pct(x['avg_trade_return'])}"
        f"   profit factor {_f(x['profit_factor'])}",
        f"  exits {x['exit_reasons']}",
        "",
        "Past performance (especially simulated) does not guarantee future results.",
    ]
    return "\n".join(lines)


def _f(x) -> str:
    return "n/a" if x is None else f"{x:.3f}"
