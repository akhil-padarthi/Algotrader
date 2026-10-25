"""Random forest entry/exit model (scikit-learn).

Target: P(return from next open to the open `horizon` sessions later > threshold).
The strategy buys when this probability is high and sells when it fades.

Validation is strictly chronological:
* the most recent ``test_fraction`` of dates is a hold-out the model never
  sees during development -> honest out-of-sample (OOS) metrics + backtest;
* inside the training period, purged walk-forward CV (expanding window, with
  an embargo of horizon+1 sessions so overlapping labels can't leak).
The model saved for live trading is then refit on all labelled data.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import sklearn
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, roc_auc_score

from .features import feature_columns

log = logging.getLogger(__name__)

MODEL_FILE = "model.joblib"
META_FILE = "model_meta.json"
OOS_FILE = "oos_predictions.csv"


def make_model(params) -> RandomForestClassifier:
    return RandomForestClassifier(
        n_estimators=params.n_estimators,
        max_depth=params.max_depth,
        min_samples_leaf=params.min_samples_leaf,
        max_features=params.max_features,
        class_weight="balanced_subsample",
        n_jobs=-1,
        random_state=params.random_state,
    )


def purged_walk_forward(dates: np.ndarray, n_splits: int, embargo: int):
    """Yield (train_dates, test_dates) with an expanding training window.

    ``dates`` must be sorted unique dates.  The training set for each fold
    ends ``embargo`` sessions before its test block starts.
    """
    n = len(dates)
    block = n // (n_splits + 1)
    if block < 10:
        raise ValueError(f"Not enough dates ({n}) for {n_splits} CV splits")
    for k in range(1, n_splits + 1):
        test_start = k * block
        test_end = n if k == n_splits else (k + 1) * block
        train_end = test_start - embargo
        if train_end <= 0:
            continue
        yield dates[:train_end], dates[test_start:test_end]


def score(y: np.ndarray, proba: np.ndarray, fwd: np.ndarray, threshold: float) -> dict:
    y = np.asarray(y).astype(int)
    proba = np.asarray(proba, dtype=float)
    fwd = np.asarray(fwd, dtype=float)
    picked = proba >= threshold
    out = {
        "n": int(len(y)),
        "base_rate": float(y.mean()) if len(y) else float("nan"),
        "accuracy": float(accuracy_score(y, (proba >= 0.5).astype(int))) if len(y) else float("nan"),
        "auc": float(roc_auc_score(y, proba)) if len(np.unique(y)) == 2 else float("nan"),
        "coverage": float(picked.mean()) if len(y) else 0.0,
        "precision_at_entry": float(y[picked].mean()) if picked.any() else float("nan"),
        "avg_fwd_return_all": float(np.nanmean(fwd)) if len(fwd) else float("nan"),
        "avg_fwd_return_signal": float(np.nanmean(fwd[picked])) if picked.any() else float("nan"),
    }
    return out


@dataclass
class TrainResult:
    model: RandomForestClassifier
    feature_cols: list[str]
    meta: dict
    oos_predictions: pd.DataFrame = field(repr=False)


def train_and_evaluate(frame: pd.DataFrame, params, entry_threshold: float, refit_all: bool = True,
                       cols: list[str] | None = None, rows_from: list[str] | None = None) -> TrainResult:
    """Train, cross-validate, test out-of-sample, then (optionally) refit on everything.

    ``frame`` comes from ``features.add_labels(build_features(...))``.
    ``cols``: feature columns to use (default: the base set).
    ``rows_from``: drop rows missing any of these columns (so two feature sets can be compared
    on exactly the same rows); defaults to ``cols``.
    """
    cols = cols or feature_columns(frame)
    usable = frame.dropna(subset=list(dict.fromkeys((rows_from or []) + cols))).copy()
    labelled = usable.dropna(subset=["label"])
    dates = np.array(sorted(labelled["date"].unique()))
    if len(dates) < 150:
        raise ValueError(f"Only {len(dates)} labelled sessions - need at least 150 to train")

    embargo = params.horizon_days + 1
    n_test = max(int(len(dates) * params.test_fraction), 20)
    test_start = dates[-n_test]
    train_dates = dates[: len(dates) - n_test - embargo]
    train = labelled[labelled["date"].isin(train_dates)]
    test = labelled[labelled["date"] >= test_start]
    log.info(
        "Train %s..%s (%d rows) | OOS test %s..%s (%d rows) | %d features",
        _d(train_dates[0]), _d(train_dates[-1]), len(train), _d(test_start), _d(dates[-1]), len(test), len(cols),
    )

    # ---- purged walk-forward CV inside the training period
    folds = []
    for k, (tr_d, te_d) in enumerate(purged_walk_forward(train_dates, params.cv_splits, embargo), 1):
        tr = train[train["date"].isin(tr_d)]
        te = train[train["date"].isin(te_d)]
        if tr["label"].nunique() < 2 or te.empty:
            continue
        m = make_model(params).fit(tr[cols], tr["label"].astype(int))
        p = m.predict_proba(te[cols])[:, 1]
        s = score(te["label"], p, te["fwd_return"], entry_threshold)
        s.update(fold=k, train_end=_d(tr_d[-1]), test_start=_d(te_d[0]), test_end=_d(te_d[-1]))
        folds.append(s)
        log.info("  CV fold %d: AUC %.3f  acc %.3f  precision@entry %.3f", k, s["auc"], s["accuracy"], s["precision_at_entry"])

    # ---- fit on the full training period, evaluate on the untouched hold-out
    dev_model = make_model(params).fit(train[cols], train["label"].astype(int))
    test_proba = dev_model.predict_proba(test[cols])[:, 1]
    oos = score(test["label"], test_proba, test["fwd_return"], entry_threshold)
    log.info("OOS: AUC %.3f  acc %.3f  base rate %.3f  precision@entry %.3f",
             oos["auc"], oos["accuracy"], oos["base_rate"], oos["precision_at_entry"])

    # Predictions for EVERY session in the OOS window (incl. the last few
    # without labels yet) - this is what the backtest trades on.
    window = usable[usable["date"] >= test_start].copy()
    window["proba"] = dev_model.predict_proba(window[cols])[:, 1]
    keep = ["date", "symbol", "open", "high", "low", "close", "market_ok", "proba", "label", "fwd_return"]
    oos_pred = window[keep].sort_values(["date", "symbol"]).reset_index(drop=True)

    importances = sorted(
        zip(cols, dev_model.feature_importances_.round(5).tolist()), key=lambda kv: kv[1], reverse=True
    )

    final_model = dev_model
    if refit_all:
        final_model = make_model(params).fit(labelled[cols], labelled["label"].astype(int))

    def _mean(key):
        vals = [f[key] for f in folds if not np.isnan(f[key])]
        return float(np.mean(vals)) if vals else float("nan")

    meta = {
        "trained_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sklearn_version": sklearn.__version__,
        "model": "RandomForestClassifier",
        "params": asdict(params),
        "entry_threshold": entry_threshold,
        "features": cols,
        "symbols": sorted(labelled["symbol"].unique().tolist()),
        "data_start": _d(dates[0]),
        "data_end": _d(dates[-1]),
        "train_period": [_d(train_dates[0]), _d(train_dates[-1])],
        "test_period": [_d(test_start), _d(dates[-1])],
        "n_train_rows": int(len(train)),
        "n_test_rows": int(len(test)),
        "cv_folds": folds,
        "cv_mean": {k: _mean(k) for k in ("auc", "accuracy", "precision_at_entry", "base_rate")},
        "oos": oos,
        "feature_importance": importances,
        "refit_on_all_data": refit_all,
    }
    return TrainResult(final_model, cols, _clean_json(meta), oos_pred)


def save(result: TrainResult, model_dir: Path) -> Path:
    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    joblib.dump({"model": result.model, "feature_cols": result.feature_cols, "meta": result.meta},
                model_dir / MODEL_FILE)
    (model_dir / META_FILE).write_text(json.dumps(result.meta, indent=2))
    result.oos_predictions.to_csv(model_dir / OOS_FILE, index=False, date_format="%Y-%m-%d")
    return model_dir / MODEL_FILE


@dataclass
class LoadedModel:
    model: RandomForestClassifier
    feature_cols: list[str]
    meta: dict

    def predict_proba(self, rows: pd.DataFrame) -> np.ndarray:
        missing = [c for c in self.feature_cols if c not in rows.columns]
        if missing:
            raise ValueError(f"Missing feature columns: {missing}")
        return self.model.predict_proba(rows[self.feature_cols])[:, 1]


def load(model_dir: Path) -> LoadedModel:
    path = Path(model_dir) / MODEL_FILE
    if not path.exists():
        raise FileNotFoundError(f"No trained model at {path}. Run: python -m algotrader train")
    blob = joblib.load(path)
    trained_with = blob["meta"].get("sklearn_version")
    if trained_with and trained_with != sklearn.__version__:
        log.warning("Model was trained with scikit-learn %s but %s is installed - retrain it "
                    "(python -m algotrader train) to avoid subtle differences.", trained_with, sklearn.__version__)
    return LoadedModel(blob["model"], blob["feature_cols"], blob["meta"])


def load_meta(model_dir: Path) -> dict | None:
    path = Path(model_dir) / META_FILE
    return json.loads(path.read_text()) if path.exists() else None


def load_oos_predictions(model_dir: Path) -> pd.DataFrame:
    path = Path(model_dir) / OOS_FILE
    if not path.exists():
        raise FileNotFoundError(f"{path} not found. Run: python -m algotrader train")
    df = pd.read_csv(path, parse_dates=["date"])
    df["market_ok"] = df["market_ok"].astype(str).str.lower().isin(["true", "1"])
    return df


def _d(x) -> str:
    return pd.Timestamp(x).strftime("%Y-%m-%d")


def _clean_json(obj):
    """Replace NaN/inf with None so the metadata is valid JSON."""
    if isinstance(obj, dict):
        return {k: _clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_json(v) for v in obj]
    if isinstance(obj, (float, np.floating)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    return obj
