import { longDate, pct, signedPct } from '../lib/format.js';
import { Bars } from './Charts.jsx';
import EquityChart from './EquityChart.jsx';

const FEATURE_NAMES = {
  ret_1: '1-day return', ret_5: '5-day return', ret_10: '10-day return', ret_20: '20-day return',
  gap: 'Opening gap', range_pct: 'Day range', close_sma10: 'Price vs 10-day avg', close_sma20: 'Price vs 20-day avg',
  close_sma50: 'Price vs 50-day avg', close_sma100: 'Price vs 100-day avg', sma10_sma50: '10 vs 50-day avg',
  rsi_14: 'RSI (14)', stoch_14: 'Stochastic (14)', macd: 'MACD', macd_signal: 'MACD signal', macd_hist: 'MACD histogram',
  bb_pctb: 'Bollinger %B', bb_width: 'Bollinger width', atr_14: 'ATR (14)', vol_20: '20-day volatility',
  vol_ratio: 'Volatility ratio', volume_z: 'Volume z-score', volume_ratio: 'Volume vs average',
  dist_high_100: 'Distance from 100-day high', dist_low_100: 'Distance from 100-day low',
  mkt_ret_5: 'S&P 5-day return', mkt_ret_20: 'S&P 20-day return', mkt_close_sma50: 'S&P vs 50-day avg',
  mkt_vol_20: 'S&P volatility', rel_strength_20: 'Strength vs S&P', corr_mkt_60: 'Correlation with S&P',
  sector_ret_5: 'Sector 5-day return', sector_ret_20: 'Sector 20-day return', sector_close_sma50: 'Sector vs 50-day avg',
  rel_sector_20: 'Strength vs its sector', vix: 'VIX level', vix_chg_5: 'VIX 5-day change', vix_z_252: 'VIX vs its past year',
  yc_10y2y: 'Yield curve', dgs10_chg_20: '10-year yield change', credit_spread: 'Credit spread', credit_chg_20: 'Credit spread change',
};

/** How the market-condition data (FRED + sectors) fared in testing, in one sentence. */
export function ContextNote({ selection }) {
  if (!selection) {
    return (
      <p className="context-note">
        This model was trained before market conditions were added. Retrain it to include them:
        {' '}<code>python -m algotrader train --refresh</code>
      </p>
    );
  }
  if (!selection.offered) return selection.note ? <p className="context-note">{selection.note}</p> : null;
  const used = selection.chosen === 'context';
  return (
    <p className="context-note">
      <strong>Market conditions {used ? 'are used' : 'are not used'}.</strong>{' '}
      In the walk-forward test the model scored {f3(selection.context.cv_auc)} with VIX, interest rates, credit
      spreads and sector trends, and {f3(selection.base.cv_auc)} without them
      {used ? ', a clear enough gain to keep them.' : ', not a clear enough gain to keep them. The dashboard still shows them for you.'}
    </p>
  );
}

const f3 = (x) => (Number.isFinite(x) ? x.toFixed(3) : '–');
const f2 = (x) => (Number.isFinite(x) ? x.toFixed(2) : '–');

function Stat({ label, value, hint, toneClass = '' }) {
  return (
    <div className="stat">
      <dt>{label}</dt>
      <dd className={toneClass}>{value}</dd>
      {hint && <p className="stat-hint">{hint}</p>}
    </div>
  );
}

export default function ModelReport({ model, backtest, benchmark = 'SPY' }) {
  if (!model) return <p className="empty">No trained model yet. Run <code>python -m algotrader train</code>.</p>;
  const oos = model.oos || {};
  const m = backtest?.metrics;
  const points = (backtest?.equity || []).map((r) => ({
    date: r.date,
    equity: r.equity,
    equity_pct: r.equity / backtest.equity[0].equity - 1,
    benchmark_pct: r.benchmark != null ? r.benchmark / backtest.equity[0].benchmark - 1 : null,
  }));
  const features = (model.feature_importance || []).slice(0, 8).map(([k, v]) => ({ label: FEATURE_NAMES[k] || k, value: v }));

  return (
    <div className="report">
      <p className="report-intro">
        Random forest trained on {longDate(model.train_period?.[0])} to {longDate(model.train_period?.[1])}, then
        tested on {longDate(model.test_period?.[0])} to {longDate(model.test_period?.[1])}, a period it never saw
        during training.
      </p>
      <ContextNote selection={model.feature_selection} />

      <div className="report-grid">
        <div>
          <h3>Prediction quality (out of sample)</h3>
          <dl className="stats">
            <Stat label="ROC AUC" value={f3(oos.auc)} hint="0.5 is a coin flip" />
            <Stat label="Accuracy" value={pct(oos.accuracy, 1)} hint={`${pct(1 - (oos.base_rate ?? 0), 1)} if it always said no`} />
            <Stat label="Hit rate when buying" value={pct(oos.precision_at_entry, 1)} hint={`${pct(oos.base_rate, 1)} of all setups rose > ${pct(model.params?.label_threshold, 0)}`} />
            <Stat label="Walk-forward AUC" value={f3(model.cv_mean?.auc)} hint={`mean of ${model.cv_folds?.length ?? 0} folds`} />
          </dl>
          <h3>What the model looks at most</h3>
          <Bars items={features} />
        </div>

        <div>
          <h3>Backtest on the test period</h3>
          {m ? (
            <>
              <dl className="stats">
                <Stat label="Strategy" value={signedPct(m.total_return, 1)} toneClass={m.total_return >= 0 ? 'gain' : 'loss'} hint={`${benchmark} ${signedPct(m.benchmark_return, 1)}`} />
                <Stat label="Max drawdown" value={signedPct(m.max_drawdown, 1)} hint={`${benchmark} ${signedPct(m.benchmark_max_drawdown, 1)}`} />
                <Stat label="Sharpe ratio" value={f2(m.sharpe)} hint={`${benchmark} ${f2(m.benchmark_sharpe)}`} />
                <Stat label="Trades" value={m.trades} hint={`${pct(m.win_rate, 0)} winners, ${f2(m.avg_days_held)} days avg`} />
              </dl>
              <EquityChart points={points} height={200} benchmarkLabel={benchmark} label="Backtest" />
            </>
          ) : (
            <p className="empty">No backtest saved yet.</p>
          )}
        </div>
      </div>
      <p className="disclaimer">
        The backtest uses the live bot's exact rules: orders fill at the next open with slippage,
        stop-losses fill at the worse of the stop price or the open
        {m ? `, and the strategy was invested ${pct(m.exposure, 0)} of the time on average` : ''}.
        Simulated results are not a promise of future returns.
      </p>
    </div>
  );
}
