import json
import unittest

import numpy as np
import pandas as pd

from algotrader import model as model_mod
from algotrader.features import add_labels, build_features
from tests.helpers import FAST_MODEL, TempDirTest, synthetic


def labelled(signal, seed):
    return add_labels(build_features(synthetic(sessions=800, seed=seed, signal=signal), "SPY"), 5, 0.01)


class ModelTests(TempDirTest):
    @classmethod
    def setUpClass(cls):
        cls.noise = model_mod.train_and_evaluate(labelled(0.0, 11), FAST_MODEL, 0.58)
        cls.signal = model_mod.train_and_evaluate(labelled(0.3, 11), FAST_MODEL, 0.58)

    def test_no_edge_on_pure_noise(self):
        """If any feature peeked at the future, the model would 'predict' random walks."""
        self.assertLess(abs(self.noise.meta["oos"]["auc"] - 0.5), 0.06)
        self.assertLess(abs(self.noise.meta["cv_mean"]["auc"] - 0.5), 0.06)

    def test_learns_a_real_pattern(self):
        self.assertGreater(self.signal.meta["oos"]["auc"], 0.65)
        self.assertGreater(self.signal.meta["cv_mean"]["auc"], 0.65)

    def test_split_is_chronological_with_embargo(self):
        m = self.signal.meta
        train_end, test_start = pd.Timestamp(m["train_period"][1]), pd.Timestamp(m["test_period"][0])
        gap = len(pd.bdate_range(train_end, test_start)) - 2
        self.assertGreaterEqual(gap, FAST_MODEL.horizon_days)
        oos = self.signal.oos_predictions
        self.assertTrue((oos["date"] >= test_start).all())
        self.assertTrue(oos["proba"].between(0, 1).all())

    def test_walk_forward_folds_never_overlap(self):
        dates = np.array(pd.bdate_range("2020-01-01", periods=300))
        for tr, te in model_mod.purged_walk_forward(dates, 4, embargo=6):
            self.assertLess(tr[-1], te[0])
            gap = np.searchsorted(dates, te[0]) - np.searchsorted(dates, tr[-1]) - 1
            self.assertGreaterEqual(gap, 6)

    def test_save_load_roundtrip(self):
        model_mod.save(self.signal, self.tmp)
        loaded = model_mod.load(self.tmp)
        rows = self.signal.oos_predictions.head(50)
        frame = build_features(synthetic(sessions=800, seed=11, signal=0.3), "SPY")
        feats = frame.merge(rows[["date", "symbol"]], on=["date", "symbol"])
        np.testing.assert_allclose(loaded.predict_proba(feats), self.signal.model.predict_proba(feats[loaded.feature_cols])[:, 1])
        meta = json.loads((self.tmp / model_mod.META_FILE).read_text())  # strict JSON: no NaN
        self.assertEqual(meta["model"], "RandomForestClassifier")
        oos = model_mod.load_oos_predictions(self.tmp)
        self.assertEqual(len(oos), len(self.signal.oos_predictions))
        self.assertEqual(oos["market_ok"].dtype, bool)

    def test_missing_model_message(self):
        with self.assertRaises(FileNotFoundError) as ctx:
            model_mod.load(self.tmp / "nope")
        self.assertIn("algotrader train", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
