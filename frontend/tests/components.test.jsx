import { test } from 'node:test';
import assert from 'node:assert/strict';
import { renderToStaticMarkup } from 'react-dom/server';
import EquityChart, { labelsFor, pickLabels } from '../src/components/EquityChart.jsx';
import MarketToday from '../src/components/MarketToday.jsx';
import NewsPanelBody, { NewsList, newsSentence } from '../src/components/News.jsx';
import ModelReport from '../src/components/ModelReport.jsx';
import { BotActivity, MarketWatch, OrdersTable, PositionsTable, SignalsList } from '../src/components/Panels.jsx';
import { BracketBar, Heartbeat, ProbabilityRail } from '../src/components/Charts.jsx';
import { TickerTape } from '../src/components/Live.jsx';
import PriceChart from '../src/components/PriceChart.jsx';
import SearchBar from '../src/components/SearchBar.jsx';
import { StockView, TradeBox, actionSentence, reviewNotes } from '../src/components/StockPanel.jsx';
import App, { DeployProblems } from '../src/App.jsx';
import {
  AccountButton, AuthScreen, ConnectBroker, ConnectPrompt, ProfileView, initials,
} from '../src/components/Account.jsx';
import { modelView } from '../src/components/StockPanel.jsx';

const html = (el) => renderToStaticMarkup(el);
const count = (s, needle) => s.split(needle).length - 1;

const position = (over = {}) => ({
  symbol: 'AAPL', qty: 10, avg_entry_price: 100, current_price: 105, market_value: 1050, cost_basis: 1000,
  unrealized_pl: 50, unrealized_plpc: 0.05, change_today: 0.01, stop_price: 95, take_profit_price: 110,
  pending_exit: false, managed: true, ...over,
});

test('positions table: rows, totals, bracket and queued exits', () => {
  const out = html(<PositionsTable positions={[position(), position({ symbol: 'MSFT', unrealized_pl: -20, pending_exit: true, managed: false })]} onSelect={() => {}} />);
  assert.equal(count(out, '<tr'), 4); // head + 2 rows + total
  assert.equal(count(out, 'class="clickable"'), 2);
  assert.match(out, /aria-label="AAPL: open chart and details"/);
  assert.equal(count(out, '>yours<'), 1); // only the position the user bought
  assert.match(out, /\+\$50\.00/);
  assert.match(out, /−\$20\.00/);
  assert.match(out, /\+\$30\.00/); // total
  assert.match(out, /Selling at next open/);
  assert.match(out, /class="bracket"/);
});

test('positions empty state explains what happens next', () => {
  assert.match(html(<PositionsTable positions={[]} />), /No open positions/);
});

test('signals list draws one rail per symbol and explains actions', () => {
  const data = {
    entry_threshold: 0.58, exit_threshold: 0.45,
    signals: [
      { symbol: 'XOM', proba: 0.67, action: 'BUY', reason: 'probability 0.67 >= entry 0.58', held: 0 },
      { symbol: 'UNH', proba: 0.59, action: 'SELL', reason: 'held 10 sessions (max 10)', held: 1 },
      { symbol: 'JPM', proba: 0.37, action: 'WATCH', reason: 'probability 0.37 < entry 0.58', held: 0 },
    ],
  };
  const out = html(<SignalsList data={data} onSelect={() => {}} />);
  assert.equal(count(out, 'class="rail"'), 3);
  assert.equal(count(out, 'class="sig-row"'), 3);      // each row is a button that opens the stock
  assert.doesNotMatch(out, /<div/);                     // valid HTML: no blocks inside buttons
  assert.match(out, /Held 10 sessions/);
  assert.doesNotMatch(out, /Probability 0\.37/); // WATCH rows stay quiet
  assert.match(out, /Buys at 0\.58 or higher/);
});

test('probability rail places the dot and the zones', () => {
  const out = html(<ProbabilityRail value={0.7} entry={0.6} exit={0.4} action="BUY" />);
  assert.match(out, /left:70%/);
  assert.match(out, /width:40%/);
  assert.match(out, /rail-dot buy/);
});

test('bracket bar positions price between stop and target', () => {
  const out = html(<BracketBar stop={90} entry={100} target={110} price={105} />);
  assert.match(out, /left:75%/);
  assert.match(html(<BracketBar stop={null} entry={100} target={110} price={105} />), /No bracket/);
});

test('orders table folds long lists', () => {
  const orders = Array.from({ length: 20 }, (_, i) => ({
    id: String(i), symbol: 'AAPL', side: i % 2 ? 'sell' : 'buy', type: 'market', order_class: 'bracket', qty: 1,
    status: 'filled', submitted_at: '2026-10-12T20:30:00Z', filled_avg_price: 100, legs: [], bot: true,
  }));
  const out = html(<OrdersTable orders={orders} />);
  assert.equal(count(out, '<tr>'), 13); // head + 12
  assert.match(out, /Show all 20 orders/);
});

test('market watch marks the benchmark', () => {
  const items = [
    { symbol: 'AAPL', price: 100, change_pct: 0.01, sparkline: [1, 2, 3], volume: 1e6, signal: { proba: 0.6 } },
    { symbol: 'SPY', price: 500, change_pct: -0.01, sparkline: [3, 2, 1], volume: 1e7, is_benchmark: true },
  ];
  const out = html(<MarketWatch items={items} quotes={{ AAPL: { price: 101, change_pct: 0.02 } }} onSelect={() => {}} feed="iex" />);
  assert.match(out, /Volume \(IEX\)/);
  assert.equal(count(out, 'class="sym-link"'), 2);
  assert.match(out, /\$101\.00/); // live quote wins over the snapshot
  assert.match(out, /benchmark/);
  assert.match(out, /gain-stroke/);
  assert.match(out, /loss-stroke/);
});

test('bot activity lists runs', () => {
  const bot = { next_run: '2026-10-14T20:20:00Z', trading_enabled: true, timezone: 'America/New_York',
    runs: [{ id: 1, status: 'ok', as_of: '2026-10-12', message: '1 buy / 0 sell decisions' }] };
  const out = html(<BotActivity bot={bot} />);
  assert.match(out, /Placed automatically/);
  assert.match(out, /1 buy \/ 0 sell decisions/);
});

test('equity chart draws a stock-style line coloured by result', () => {
  const points = [0, 0.01, -0.005, 0.03].map((v, i) => ({
    date: `2026-10-0${i + 1}`, equity_pct: v, benchmark_pct: 0.005 * i, equity: 100000 * (1 + v),
  }));
  const up = html(<EquityChart points={points} />);
  assert.match(up, /class="equity-chart tone-gain"/);
  assert.match(up, /class="line portfolio"/);
  assert.match(up, /class="line bench"/);
  assert.match(up, /<linearGradient id="eq-area-[\w-]+"/);
  assert.match(up, /class="area" fill="url\(#eq-area-[\w-]+\)"/);
  assert.match(up, /class="end-dot"/);
  assert.match(up, /aria-label="S&amp;P 500 \+1\.50% over the period; your account \+3\.00%"/);
  assert.match(up, /Your account<\/span>/);
  assert.doesNotMatch(up, /spread/);
  assert.doesNotMatch(up, /start-marker/);              // the account existed for the whole period

  const down = html(<EquityChart points={points.map((p, i) => ({ ...p, equity_pct: -0.01 * i, equity: 100000 * (1 - 0.01 * i) }))} />);
  assert.match(down, /class="equity-chart tone-loss"/);
  const flat = html(<EquityChart points={points.map((p) => ({ ...p, equity_pct: 0, equity: 100000 }))} />);
  assert.match(flat, /class="equity-chart tone-flat"/);
  assert.match(html(<EquityChart points={[]} />), /No data for this period yet/);
});

test('equity chart: a new account starts part-way, the S&P 500 covers the whole period', () => {
  const points = ['2026-09-15', '2026-09-22', '2026-10-01', '2026-10-08', '2026-10-15'].map((date, i) => ({
    date, benchmark_pct: 0.01 * i, equity: i >= 3 ? 100000 + 500 * (i - 3) : null, equity_pct: i >= 3 ? 0.03 + 0.005 * (i - 3) : null,
  }));
  const out = html(<EquityChart points={points} period="1M" partial />);
  assert.match(out, /class="start-marker"/);
  assert.match(out, />You started Oct 8</);
  assert.match(out, /your account \+0\.50% since it started/);
  // the S&P 500 line has a point for every day; the account line only from Oct 8
  const bench = out.match(/class="line bench"/) && out.match(/<path d="([^"]+)" class="line bench"/)[1];
  const mine = out.match(/<path d="([^"]+)" class="line portfolio"/)[1];
  assert.equal(count(bench, 'L') + 1, 5);
  assert.equal(count(mine, 'L') + 1, 2);
});

test('equity chart x-axis: fewer labels on narrow screens, months on the 1-year view', () => {
  assert.equal(labelsFor(320), 3);
  assert.equal(labelsFor(760), 5);
  const points = Array.from({ length: 250 }, (_, i) => ({
    date: new Date(Date.UTC(2025, 9, 16) + i * 86400000 * 1.46).toISOString().slice(0, 10), benchmark_pct: 0, equity_pct: 0, equity: 1,
  }));
  assert.equal(pickLabels(points, '1A', false, undefined, 3).length, 3);
  assert.equal(pickLabels(points, '1A', false, undefined, 5).length, 5);
  const out = html(<EquityChart points={points} period="1A" />);
  assert.match(out, />Oct 2025</);
  assert.doesNotMatch(out, />Oct 16</);
});

test('intraday chart labels each trading day once', () => {
  const days = ['2026-10-12', '2026-10-13', '2026-10-14'];
  const points = days.flatMap((d) => [0, 1, 2, 3].map((k) => ({
    date: d, t: `${d}T${String(10 + k).padStart(2, '0')}:00:00-04:00`, equity_pct: 0.001 * k, benchmark_pct: -0.001 * k,
  })));
  const out = html(<EquityChart points={points} intraday timeZone="America/New_York" />);
  assert.equal(count(out, '>Oct 12<'), 1);
  assert.equal(count(out, '>Oct 13<'), 1);
  assert.equal(count(out, '>Oct 14<'), 1);
});

test('heartbeat beats while live and flatlines when disconnected', () => {
  const live = html(<Heartbeat status="live" beat="2026-10-15T20:00:05Z" />);
  assert.match(live, /class="heartbeat live"/);
  assert.match(live, /class="hb-trace"/);
  assert.match(live, /pathLength="100"/);
  assert.match(html(<Heartbeat status="polling" beat="x" />), /hb-trace/);
  const lost = html(<Heartbeat status="error" />);
  assert.match(lost, /class="heartbeat error"/);
  assert.match(lost, /class="hb-flat"/);
  assert.doesNotMatch(lost, /hb-trace/);
  assert.doesNotMatch(html(<Heartbeat status="connecting" />), /hb-trace/);
});

test('model report shows out-of-sample metrics and backtest', () => {
  const model = {
    train_period: ['2021-01-04', '2024-06-28'], test_period: ['2024-07-10', '2025-10-10'],
    oos: { auc: 0.61, accuracy: 0.6, base_rate: 0.42, precision_at_entry: 0.57 },
    cv_mean: { auc: 0.6 }, cv_folds: [{}, {}, {}], params: { label_threshold: 0.01 },
    feature_importance: [['ret_5', 0.1], ['rsi_14', 0.05]],
  };
  const backtest = {
    metrics: { total_return: 0.25, benchmark_return: 0.06, max_drawdown: -0.08, benchmark_max_drawdown: -0.1,
      sharpe: 1.9, benchmark_sharpe: 0.5, trades: 40, win_rate: 0.6, avg_days_held: 6, exposure: 0.4 },
    equity: [{ date: '2024-07-10', equity: 100000, benchmark: 100000 }, { date: '2025-10-10', equity: 125000, benchmark: 106000 }],
  };
  const out = html(<ModelReport model={model} backtest={backtest} />);
  assert.match(out, /0\.610/);
  assert.match(out, /\+25\.0%/);
  assert.match(out, /SPY \+6\.0%/);
  assert.match(out, /5-day return/);
  assert.match(out, /never saw/);
  assert.match(html(<ModelReport model={null} />), /No trained model yet/);
});

test('app renders its loading shell without crashing', () => {
  assert.match(html(<App />), /AlgoTrader/);
});

/* ------------------------------------------------------------ new features */
const stockData = (over = {}) => ({
  symbol: 'AAPL', name: 'Apple Inc. Common Stock', exchange: 'NASDAQ', fractionable: true, tradable: true,
  range: '1D', timeframe: '5Min', in_universe: true, is_benchmark: false, market_open: false,
  bars: [0, 1, 2, 3, 4].map((i) => ({ t: `2026-10-15T${10 + i}:00:00-04:00`, c: 340 + i })),
  quote: { price: 344.5, prev_close: 341, change: 3.5, change_pct: 3.5 / 341, open: 340, high: 345, low: 339, volume: 1e6, time: '2026-10-15T16:00:00-04:00' },
  position: null,
  signal: { symbol: 'AAPL', proba: 0.46, action: 'WATCH', reason: 'probability 0.46 < entry 0.58' },
  trading: { enabled: true, mode: 'paper' },
  ...over,
});

const view = (data, extra = {}) => html(
  <StockView symbol={data.symbol} data={data} quote={data.quote} marketOpen={data.market_open} range="1D"
    onRange={() => {}} timeZone="America/New_York" feed="iex" onClose={() => {}} {...extra} />,
);

test('stock panel shows price, chart, stats, model view and a paper trade box', () => {
  const out = view(stockData());
  assert.match(out, /role="dialog"|id="sheet-title"/);
  assert.match(out, />AAPL</);
  assert.match(out, /Apple Inc\. Common Stock/);
  assert.match(out, /\$344\.50/);
  assert.match(out, /\+\$3\.50 \(\+1\.03%\) today/);
  assert.match(out, /Market closed, last trade/);
  assert.match(out, /class="line price"/);
  assert.match(out, /class="prev-close"/);              // dashed line at yesterday's close
  assert.match(out, /Volume \(IEX\)/);
  assert.match(out, /only watching/);                    // plain-words model explanation
  assert.match(out, /Paper account: pretend money/);
  assert.match(out, /Amount to invest/);
  assert.doesNotMatch(out, /Your position/);
});

test('stock panel: position, who manages it, benchmark and switched-off trading', () => {
  const pos = { qty: 2.5, market_value: 861.25, avg_entry_price: 330, unrealized_pl: 36.25, unrealized_plpc: 0.044 };
  const mine = view(stockData({ position: pos, signal: { proba: 0.5, action: 'MANUAL', reason: 'x' } }));
  assert.match(mine, /Your position/);
  assert.match(mine, /You bought this, so the bot leaves it alone/);
  const bots = view(stockData({ position: pos, signal: { proba: 0.6, action: 'HOLD', reason: 'x' } }));
  assert.match(bots, /The bot manages this position/);
  const other = view(stockData({ symbol: 'KO', in_universe: false, signal: null }));
  assert.match(other, /isn&#x27;t one of the bot&#x27;s stocks|isn't one of the bot's stocks/);
  const off = view(stockData({ trading: { enabled: false, mode: 'paper' } }));
  assert.match(off, /MANUAL_TRADING=false/);
  assert.doesNotMatch(off, /Amount to invest/);
});

test('stock panel: the chart explains a failed load instead of spinning forever', () => {
  const failed = view(stockData({ bars: null, chart_error: 'Alpaca said: forbidden (403).' }), { onRetry: () => {} });
  assert.match(failed, /Couldn&#x27;t load the chart/);
  assert.match(failed, /Alpaca said: forbidden \(403\)\./);
  assert.match(failed, />Try again</);
  assert.doesNotMatch(failed, /Loading chart/);
  assert.equal(count(failed, 'forbidden (403)'), 1);    // said once, not twice
  // switching to 1Y: the old 1D data is not shown as if it were 1Y
  const switching = html(<StockView symbol="AAPL" data={stockData()} quote={stockData().quote} range="1Y"
    onRange={() => {}} timeZone="America/New_York" feed="iex" onClose={() => {}} />);
  assert.match(switching, /Loading chart/);
  assert.doesNotMatch(switching, /class="line price"/);
  const switchFailed = html(<StockView symbol="AAPL" data={stockData()} quote={stockData().quote} range="1Y"
    error={{ range: '1Y', message: 'The server is still running older code.' }}
    onRange={() => {}} timeZone="America/New_York" feed="iex" onClose={() => {}} />);
  assert.match(switchFailed, /The server is still running older code\./);
  // first load failed completely
  const nothing = html(<StockView symbol="AAPL" data={null} range="1D" error={{ range: '1D', message: 'Network error.' }}
    onRange={() => {}} timeZone="America/New_York" onClose={() => {}} />);
  assert.equal(count(nothing, 'Network error.'), 1);
});

test('stock panel: sector and recent news', () => {
  const news = { lookback_days: 3, summary: { count: 2, positive: 1, negative: 1, tone: 'neutral' }, articles: [
    { id: 9, headline: 'Apple wins contract', url: 'https://example.com/n', symbols: ['AAPL'], source: 'Benzinga',
      created_at: '2026-10-14T12:00:00Z', tone: 'positive' }] };
  const out = view(stockData({ sector: { name: 'Technology', etf: 'XLK' }, news }),
    { sectors: [{ symbol: 'XLK', name: 'Technology', change_1m: -0.162 }] });
  assert.match(out, /<dt>Sector<\/dt><dd>Technology/);
  assert.match(out, /sector fund XLK/);
  assert.match(out, /−16\.2%/);
  assert.match(out, /Mixed: 1 positive and 1 negative headlines in the last 3 days\./);
  assert.match(out, /Apple wins contract/);
  assert.match(out, /From Benzinga via Alpaca/);
  const quiet = view(stockData({ news: { ...news, summary: { count: 0, positive: 0, negative: 0 } } }));
  assert.match(quiet, /No headlines in the last 3 days\. The most recent ones are below\./);
});

test('live account is labelled as real money', () => {
  const out = html(<TradeBox symbol="AAPL" price={344.5} trading={{ enabled: true, mode: 'live' }} />);
  assert.match(out, /Live account: real money/);
  assert.match(out, /trade is-live/);
});

test('sell is only possible when you own the stock', () => {
  const none = html(<TradeBox symbol="AAPL" price={344.5} trading={{ enabled: true, mode: 'paper' }} />);
  assert.match(none, /<button type="button" aria-pressed="false" disabled="">Sell<\/button>/);
  const owned = html(<TradeBox symbol="AAPL" price={344.5} trading={{ enabled: true, mode: 'paper' }}
    position={{ qty: 2.5, market_value: 861.25 }} initialSide="sell" />);
  assert.match(owned, /You own <strong>2\.5 shares<\/strong>/);
  const whole = html(<TradeBox symbol="BRK.A" price={700000} fractionable={false} trading={{ enabled: true, mode: 'paper' }} />);
  assert.match(whole, /trades in whole shares only/);
});

test('confirmation notes for every kind of order', () => {
  const texts = (o) => reviewNotes({ symbol: 'AAPL', marketOpen: true, botManaged: false, live: false, ...o }).map((n) => n.text);
  assert.deepEqual(texts({ side: 'buy' }), ["The bot won't sell stocks you buy yourself, and they have no automatic stop-loss."]);
  assert.match(texts({ side: 'buy', botManaged: true })[0], /bot owns AAPL right now\. Buying more makes the whole position yours/);
  assert.match(texts({ side: 'buy', marketOpen: false })[0], /market is closed/);
  assert.deepEqual(texts({ side: 'sell' }), []);
  assert.match(texts({ side: 'sell', botManaged: true })[0], /cancels the bot's stop-loss and take-profit for AAPL/);
  const live = reviewNotes({ side: 'buy', symbol: 'AAPL', marketOpen: true, live: true });
  assert.deepEqual(live.at(-1), { text: 'This is your live account. The order uses real money.', warn: true });
});

test('plain-words model explanations', () => {
  const rules = { entry: 0.58, exit: 0.45, label: 0.01, horizon: 5 };
  assert.match(actionSentence({ action: 'BUY' }, rules), /buying at the next open/);
  assert.match(actionSentence({ action: 'SELL', reason: 'held 10 sessions (max 10)' }, rules), /maximum number of days/);
  assert.match(actionSentence({ action: 'SELL', reason: 'probability 0.3 < exit 0.45' }, rules), /below 0\.45/);
  assert.equal(actionSentence({ action: 'SKIP', reason: 'market filter: benchmark below 50-day average' }, rules),
    "The bot would buy, but the S&P 500 is below its 50-day average, so it isn't opening new positions.");
  assert.match(actionSentence({ action: 'SKIP', reason: 'max 5 positions reached' }, rules), /as many stocks as it is allowed/);
  assert.match(actionSentence({ action: 'SKIP', reason: 'not enough cash for one share' }, rules), /enough cash for one share\.$/);
  assert.equal(actionSentence({ action: 'NEW' }, rules), '');
});

test('price chart: colour by result, live point, empty states', () => {
  const bars = [0, 1, 2].map((i) => ({ t: `2026-10-15T1${i}:00:00-04:00`, c: 100 + i }));
  const up = html(<PriceChart bars={bars} range="1D" prevClose={99} timeZone="America/New_York" />);
  assert.match(up, /class="price-chart tone-gain"/);
  assert.match(up, />10:00 AM</);
  assert.doesNotMatch(up, /live-ring/);
  const down = html(<PriceChart bars={bars} range="1Y" prevClose={150} timeZone="America/New_York" />);
  assert.match(down, /tone-gain/);                        // 1Y compares with the first bar, not yesterday
  const live = html(<PriceChart bars={bars} range="1D" prevClose={101.5} live={{ t: '2026-10-15T12:30:00-04:00', price: 101 }} timeZone="America/New_York" />);
  assert.match(live, /class="live-ring"/);
  assert.match(live, /tone-loss/);                        // below yesterday's close
  const stale = html(<PriceChart bars={bars} range="1D" live={{ t: '2026-10-15T09:00:00-04:00', price: 1 }} timeZone="America/New_York" />);
  assert.doesNotMatch(stale, /live-ring/);                // an older quote never goes after the last bar
  assert.match(html(<PriceChart bars={null} range="1D" loading />), /Loading chart/);
  assert.match(html(<PriceChart bars={[]} range="1D" />), /No trades in this period yet/);
});

test('price chart never gets stuck on "Loading": a failed load says why and offers a retry', () => {
  const failed = html(<PriceChart bars={null} range="1D" error="Alpaca rejected the API key." onRetry={() => {}} />);
  assert.match(failed, /role="alert"/);
  assert.match(failed, /Couldn&#x27;t load the chart\./);
  assert.match(failed, /Alpaca rejected the API key\./);
  assert.match(failed, />Try again</);
  assert.doesNotMatch(failed, /Loading chart/);
  const bars = [0, 1, 2].map((i) => ({ t: `2026-10-1${i}T04:00:00Z`, c: 100 + i }));
  const fallback = html(<PriceChart bars={bars} range="1D" daily note="Alpaca didn't return intraday prices, so this shows daily closes." />);
  assert.match(fallback, /class="chart-note">Alpaca didn&#x27;t return intraday prices/);
  assert.doesNotMatch(fallback, /Previous close/);       // daily fallback: no yesterday line
});

const CONTEXT = {
  summary: 'Markets are calm (VIX 14.2); the S&P 500 is in an uptrend; 8 of 11 sectors are above their 50-day average.',
  indicators: [
    { id: 'vix', name: 'Volatility index (VIX)', value: 14.2, unit: '', status: 'Calm', tone: 'gain', change: -1.1,
      change_label: 'vs a week ago', explain: 'Expected size of S&P 500 moves.', source: 'Cboe via FRED',
      url: 'https://fred.stlouisfed.org/series/VIXCLS', date: '2026-10-15' },
    { id: 'dgs10', name: '10-year Treasury yield', value: 4.1, unit: '%', status: 'Falling', tone: 'gain', change: -0.2,
      change_label: 'vs a month ago', explain: 'What the US government pays.', source: 'Federal Reserve via FRED',
      url: 'https://fred.stlouisfed.org/series/DGS10', date: '2026-10-15' },
  ],
  trend: { label: 'Uptrend', vs_50d: 0.021, vs_200d: 0.08 },
  sectors: [{ symbol: 'XLK', name: 'Technology', change_1d: 0.012, change_1m: -0.03 }],
  sources: [{ name: 'FRED (Federal Reserve Bank of St. Louis)', url: 'https://fred.stlouisfed.org', used_for: 'volatility, rates and credit', as_of: '2026-10-15', ok: true },
    { name: 'Benzinga via Alpaca', url: 'https://alpaca.markets', used_for: 'news', ok: false }],
  problems: [],
};

test('market today: indicators with source and date, trend sentence, sectors', () => {
  const out = html(<MarketToday data={CONTEXT} onSelect={() => {}} />);
  assert.match(out, /Markets are calm/);
  assert.equal(count(out, 'class="indicator"'), 2);
  assert.match(out, />14\.20</);
  assert.match(out, />4\.10%</);
  assert.match(out, /−0\.20 pts vs a month ago/);       // a change in a % rate is in points
  assert.match(out, /href="https:\/\/fred\.stlouisfed\.org\/series\/VIXCLS"[^>]*>Cboe via FRED, Oct 15</);
  assert.match(out, /2\.1% above<\/strong> its 50-day average/);
  assert.match(out, /so it is in an uptrend/);
  assert.match(out, /aria-label="XLK: open chart"/);
  assert.match(out, /−3\.0%/);
  assert.match(out, /\(not available right now\)/);    // a source that is down says so
  assert.match(html(<MarketToday data={null} />), /Loading market conditions/);
  const down = html(<MarketToday data={{ ...CONTEXT, indicators: [], problems: ['FRED: timed out'] }} />);
  assert.match(down, /aren&#x27;t available right now/);
});

test('news: headline list, tone words and a plain summary sentence', () => {
  const articles = [
    { id: 1, headline: 'Apple beats estimates', url: 'https://example.com/a', symbols: ['AAPL'], source: 'Benzinga',
      created_at: new Date(Date.now() - 3600e3).toISOString(), tone: 'positive' },
    { id: 2, headline: 'Visa faces probe', url: '', symbols: ['V'], source: 'Benzinga',
      created_at: new Date(Date.now() - 7200e3).toISOString(), tone: 'negative' },
  ];
  const list = html(<NewsList articles={articles} onSelect={() => {}} />);
  assert.match(list, /<a class="news-headline" href="https:\/\/example\.com\/a"/);
  assert.match(list, /<span class="news-headline">Visa faces probe/);
  assert.match(list, /class="tone-word positive">Positive/);
  assert.match(list, /class="tone-word negative">Negative/);
  assert.match(html(<NewsList articles={[]} />), /No news in the last few days/);

  assert.equal(newsSentence({ count: 3, positive: 2, negative: 1, tone: 'positive' }, 3),
    'Mostly positive: 2 positive and 1 negative headlines in the last 3 days.');
  assert.equal(newsSentence({ count: 1, positive: 0, negative: 0, tone: 'neutral' }, 3),
    'Mixed: 1 neutral headline in the last 3 days.');
  assert.equal(newsSentence({ count: 0 }, 3), 'No headlines in the last 3 days.');
  assert.equal(newsSentence({ count: 0 }, 3, true), 'No headlines in the last 3 days. The most recent ones are below.');

  const panel = html(<NewsPanelBody data={{ articles, lookback_days: 3,
    by_symbol: { AAPL: { count: 1, positive: 1, negative: 0 }, V: { count: 1, positive: 0, negative: 1 } } }} />);
  assert.match(panel, /Mixed: 1 positive and 1 negative headlines in the last 3 days\./);
});

test('ticker repeats the list once for a seamless loop, and only one copy is focusable', () => {
  const items = [{ symbol: 'AAPL', price: 1, change_pct: 0.01 }, { symbol: 'MSFT', price: 2, change_pct: -0.01 }];
  const out = html(<TickerTape items={items} quotes={{ AAPL: { price: 5, change_pct: 0.02 } }} onSelect={() => {}} />);
  assert.equal(count(out, 'class="ticker-copy"'), 2);
  assert.equal(count(out, 'aria-hidden="true"><li>'), 1);
  assert.equal(count(out, 'tabindex="-1"'), 2);
  assert.match(out, /\$5\.00/);                          // live quote wins
  assert.match(out, /animation-duration:24s/);
  assert.match(html(<TickerTape items={[]} />), /ticker-empty/);
});

test('search box is an accessible combobox', () => {
  const out = html(<SearchBar watchlist={[{ symbol: 'AAPL', name: 'Apple' }]} onSelect={() => {}} />);
  assert.match(out, /role="combobox"/);
  assert.match(out, /aria-label="Search stocks"/);
  assert.match(out, /aria-expanded="false"/);
  assert.match(out, /Search any stock/);
});

/* ------------------------------------------------------------ accounts */
const member = (over = {}) => ({
  id: 2, name: 'Ana Ruiz', email: 'ana@example.com', is_admin: false, account_kind: 'member', mode: 'paper',
  created_at: '2026-10-17T12:00:00+00:00', last_login_at: null,
  broker: { linked: false, account: null, linked_at: null }, ...over,
});

test('sign-up screen: the first account is told it becomes the admin', () => {
  const first = html(<AuthScreen status={{ signups: true, has_users: false, first_account_is_admin: true }} onAuthed={() => {}} />);
  assert.match(first, />Create your account</);
  assert.match(first, /this account becomes the admin/);
  assert.match(first, /id="auth-name"/);
  assert.match(first, /autocomplete="new-password"/i);
  assert.match(first, /At least 8 characters/);
  assert.match(first, /stored as secure hashes/);
  const later = html(<AuthScreen status={{ signups: true, has_users: true }} onAuthed={() => {}} />);
  assert.match(later, />Log in</);
  assert.doesNotMatch(later, /id="auth-name"/);
  assert.match(later, /autocomplete="current-password"/i);
  assert.match(later, /Create an account/);
  const closed = html(<AuthScreen status={{ signups: false, has_users: true }} onAuthed={() => {}} />);
  assert.match(closed, /New sign-ups are closed/);
  assert.doesNotMatch(closed, /Create an account/);
});

test('connect screen explains where to get paper keys, with a demo shortcut in the demo', () => {
  const out = html(<ConnectBroker standalone onLinked={() => {}} onSkip={() => {}} />);
  assert.match(out, />Connect your Alpaca paper account</);
  assert.match(out, /href="https:\/\/app\.alpaca\.markets"/);
  assert.match(out, /says <strong>Paper<\/strong>/);
  assert.match(out, /id="alpaca-key"/);
  assert.match(out, /id="alpaca-secret" type="password"/);
  assert.match(out, />Do this later</);
  assert.match(out, /encrypted before they&#x27;re saved/);
  assert.doesNotMatch(out, /Use a demo account/);
  assert.match(html(<ConnectBroker demo onLinked={() => {}} />), />Use a demo account</);
  assert.match(html(<ConnectPrompt onConnect={() => {}} />), />Connect account</);
});

test('account button and profile: member vs admin', () => {
  assert.equal(initials('Ana María Ruiz'), 'AR');
  assert.equal(initials('alex'), 'A');
  assert.equal(initials(''), '?');
  const btn = html(<AccountButton user={member()} onOpen={() => {}} />);
  assert.match(btn, /aria-label="Your account: Ana Ruiz"/);
  assert.match(btn, />AR</);

  const notLinked = html(<ProfileView user={member()} onUser={() => {}} onLoggedOut={() => {}} onLogout={() => {}}
    onClose={() => {}} trades={[]} />);
  assert.match(notLinked, />Ana Ruiz</);
  assert.match(notLinked, />Member</);
  assert.match(notLinked, /id="alpaca-key"/);                 // connect form inside the profile
  assert.match(notLinked, /Nothing yet/);
  assert.match(notLinked, />Log out</);
  assert.match(notLinked, /Delete my account/);
  assert.match(notLinked, /Your Alpaca account and\s+its positions are not affected/);

  const trades = [{ id: 1, side: 'buy', symbol: 'AAPL', amount: 500, qty: null, created_at: new Date().toISOString() }];
  const linked = html(<ProfileView user={member({ broker: { linked: true, account: '•••6789', linked_at: '2026-10-17T12:00:00+00:00' } })}
    onUser={() => {}} onLoggedOut={() => {}} onLogout={() => {}} onClose={() => {}} trades={trades} />);
  assert.match(linked, /Connected: paper account <strong>•••6789<\/strong>/);
  assert.match(linked, />Disconnect</);
  assert.doesNotMatch(linked, /id="alpaca-key"/);
  assert.match(linked, /class="trade-side buy">Bought</);
  assert.match(linked, /\$500\.00/);

  const admin = html(<ProfileView user={member({ is_admin: true, account_kind: 'bot', name: 'Owner' })} onUser={() => {}}
    onLoggedOut={() => {}} onLogout={() => {}} onClose={() => {}} />);
  assert.match(admin, />Admin</);
  assert.match(admin, /the bot&#x27;s Alpaca account/);
  assert.doesNotMatch(admin, /Your purchases/);
  assert.doesNotMatch(admin, /id="alpaca-key"/);
});

test('members see the model view, not the bot actions', () => {
  const data = {
    as_of: '2026-10-16', audience: 'member', entry_threshold: 0.58, exit_threshold: 0.45,
    signals: [{ symbol: 'AAPL', proba: 0.62, action: 'BUY', reason: '', held: true },
      { symbol: 'XOM', proba: 0.40, action: 'SELL', reason: '', held: false }],
  };
  const out = html(<SignalsList data={data} onSelect={() => {}} />);
  assert.match(out, />Buy zone</);
  assert.match(out, />Sell zone</);
  assert.match(out, /class="tag">yours</);
  assert.match(out, /Buy zone at 0\.58 or higher/);
  assert.doesNotMatch(out, /Sells below/);
  const rules = { entry: 0.58, exit: 0.45, label: 0.01, horizon: 5 };
  assert.match(modelView({ action: 'BUY' }, rules), /above the bot's buy level of 0\.58/);
  assert.match(modelView({ action: 'SELL' }, rules), /below the bot's sell level of 0\.45/);
  assert.match(modelView({ action: 'HOLD' }, rules), /no strong view/);
  assert.match(html(<PositionsTable positions={[]} member />), /You don&#x27;t own any stocks yet/);
});

test('stock panel for a member: connect first, then their own account', () => {
  const needs = view(stockData({ trading: { enabled: true, mode: 'paper', needs_broker: true }, account_kind: null,
    signal_audience: 'member', signal: { proba: 0.62, action: 'BUY' } }), { onConnect: () => {} });
  assert.match(needs, /Connect your Alpaca paper account to buy AAPL/);
  assert.match(needs, />Connect Alpaca account</);
  assert.doesNotMatch(needs, /Amount to invest/);
  assert.match(needs, /above the bot&#x27;s buy level/);
  const pos = { qty: 2, market_value: 689, avg_entry_price: 330, unrealized_pl: 29, unrealized_plpc: 0.044 };
  const mine = view(stockData({ position: pos, account_kind: 'member', signal_audience: 'member',
    signal: { proba: 0.5, action: 'HOLD' }, trading: { enabled: true, mode: 'demo', needs_broker: false } }));
  assert.match(mine, /This is in your own Alpaca paper account/);
  assert.doesNotMatch(mine, /The bot manages this position/);
  assert.match(mine, /Demo account: pretend money/);
  assert.match(mine, /Amount to invest/);
});

test('a hosted site with missing settings says what to add and where', () => {
  const out = html(<DeployProblems problems={["SECRET_KEY isn't set: add a long random text.", "CRON_SECRET isn't set."]} />);
  assert.match(out, /This site isn&#x27;t set up yet/);
  assert.equal(count(out, '<li>'), 2);
  assert.match(out, /SECRET_KEY isn&#x27;t set/);
  assert.match(out, /Settings → Environment Variables/);
  assert.match(out, /vercel --prod/);
});
