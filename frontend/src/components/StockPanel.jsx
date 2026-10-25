import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { useDialog } from '../hooks.js';
import {
  DASH, clockTime, compactNumber, estimateShares, money, pct, prob, shares, signedMoney, signedPct, tone,
} from '../lib/format.js';
import { ProbabilityRail } from './Charts.jsx';
import { NewsList, newsSentence } from './News.jsx';
import { FlashValue } from './Panels.jsx';
import PriceChart from './PriceChart.jsx';

export const RANGES = ['1D', '1W', '1M', '3M', '1Y'];
const QUICK_AMOUNTS = [100, 500, 1000];
const QUOTE_POLL_MS = 4000;
const DEFAULT_RULES = { entry: 0.58, exit: 0.45, label: 0.01, horizon: 5 };

/** Slide-out panel for one stock: live price, chart, model view, position, buy/sell. */
export default function StockPanel({ symbol, onClose, timeZone, feed, rules, sectors, onSelect, onTraded, onConnect }) {
  const [range, setRange] = useState('1D');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [live, setLive] = useState(null);
  const [reload, setReload] = useState(0);
  const sheetRef = useRef(null);
  const closeRef = useRef(null);

  // chart + details: on open, on range change, after a trade, and every 30-60 s
  useEffect(() => {
    const ctrl = new AbortController();
    setError(null);
    const load = () => api.stock(symbol, range, { signal: ctrl.signal })
      .then((d) => { setData(d); setError(null); })
      .catch((e) => { if (e.name !== 'AbortError') setError(Object.assign(e, { range })); });
    load();
    const id = setInterval(load, range === '1D' ? 30000 : 60000);
    return () => { ctrl.abort(); clearInterval(id); };
  }, [symbol, range, reload]);

  // live price every few seconds
  useEffect(() => {
    setLive(null);
    const ctrl = new AbortController();
    const poll = () => api.quote(symbol, { signal: ctrl.signal }).then(setLive).catch(() => {});
    poll();
    const id = setInterval(() => { if (!document.hidden) poll(); }, QUOTE_POLL_MS);
    return () => { ctrl.abort(); clearInterval(id); };
  }, [symbol]);

  useEffect(() => { setData(null); setError(null); }, [symbol]);

  useDialog(sheetRef, closeRef, onClose);

  const quote = live?.quote || data?.quote || null;
  const marketOpen = live?.market_open ?? data?.market_open ?? false;
  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} aria-hidden="true" />
      <aside ref={sheetRef} className="sheet" role="dialog" aria-modal="true" aria-labelledby="sheet-title">
        <StockView
          symbol={symbol}
          data={data}
          error={error}
          quote={quote}
          marketOpen={marketOpen}
          range={range}
          onRange={setRange}
          timeZone={timeZone}
          feed={feed}
          rules={rules}
          sectors={sectors}
          onSelect={onSelect}
          closeRef={closeRef}
          onClose={onClose}
          onRetry={() => setReload((n) => n + 1)}
          onTraded={() => { setReload((n) => n + 1); onTraded?.(); }}
          onConnect={onConnect}
        />
      </aside>
    </>
  );
}

/** Everything inside the panel; takes plain data so it can be rendered in tests. */
export function StockView({
  symbol, data, error, quote, marketOpen, range, onRange, timeZone, feed, rules = DEFAULT_RULES, closeRef, onClose,
  onRetry, onTraded, sectors, onSelect, onConnect,
}) {
  const sectorMove = data?.sector && (sectors || []).find((s) => s.symbol === data.sector.etf);
  const price = quote?.price;
  const liveTick = marketOpen && quote?.time && Number.isFinite(price) ? { t: quote.time, price } : null;
  const current = data && data.range === range ? data : null;
  // the chart is "loading" only while nothing has come back for this range yet
  const chartError = (error && (!current || error.range === range) ? error.message : null)
    || (current && !current.bars ? current.chart_error || 'Alpaca did not return any prices.' : null);
  return (
    <div className="sheet-inner">
      <header className="sheet-head">
        <div>
          <h2 id="sheet-title" className="sheet-symbol">{symbol}</h2>
          <p className="sheet-name">{data?.name || ' '}</p>
        </div>
        <button ref={closeRef} type="button" className="icon-button" onClick={onClose} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M4 4l10 10M14 4L4 14" /></svg>
        </button>
      </header>

      <div className="sheet-price">
        <span className="sheet-last"><FlashValue value={price}>{money(price)}</FlashValue></span>
        <span className={`sheet-change ${tone(quote?.change)}`}>
          {signedMoney(quote?.change)} ({signedPct(quote?.change_pct)}) today
        </span>
        <span className="sheet-status">
          <span className={`status-dot ${marketOpen ? 'open' : ''}`} aria-hidden="true" />
          {marketOpen
            ? `Live, last trade ${clockTime(quote?.time, timeZone)}`
            : `Market closed, last trade ${clockTime(quote?.time, timeZone)}`}
        </span>
      </div>

      <div className="period-switch" role="group" aria-label="Chart range">
        {RANGES.map((r) => (
          <button key={r} type="button" aria-pressed={r === range} onClick={() => onRange(r)}>{r}</button>
        ))}
      </div>
      <PriceChart
        bars={current?.bars || null}
        loading={!current && !chartError}
        error={chartError}
        note={current?.chart_note}
        onRetry={onRetry}
        range={range}
        daily={current?.timeframe === '1Day'}
        prevClose={quote?.prev_close}
        live={range === '1D' && current?.timeframe !== '1Day' ? liveTick : null}
        timeZone={timeZone}
      />

      <dl className="sheet-stats">
        <div><dt>Open</dt><dd>{money(quote?.open)}</dd></div>
        <div><dt>High</dt><dd>{money(quote?.high)}</dd></div>
        <div><dt>Low</dt><dd>{money(quote?.low)}</dd></div>
        <div><dt>Previous close</dt><dd>{money(quote?.prev_close)}</dd></div>
        <div><dt>{feed === 'iex' ? 'Volume (IEX)' : 'Volume'}</dt><dd>{compactNumber(quote?.volume)}</dd></div>
        <div><dt>Exchange</dt><dd>{data?.exchange || DASH}</dd></div>
        {data?.sector && (
          <div className="wide">
            <dt>Sector</dt>
            <dd>
              {data.sector.name}
              {sectorMove && (
                <span className="sector-move">
                  {' '}(sector fund {sectorMove.symbol}:{' '}
                  <span className={tone(sectorMove.change_1m)}>{signedPct(sectorMove.change_1m, 1)}</span> over the last month)
                </span>
              )}
            </dd>
          </div>
        )}
      </dl>

      {data && <ModelView data={data} rules={rules} />}
      {data?.news && (
        <section className="sheet-section">
          <h3>News</h3>
          <p className="news-summary">{newsSentence(data.news.summary, data.news.lookback_days, data.news.articles.length > 0)}</p>
          <NewsList articles={data.news.articles} onSelect={onSelect} showSymbols={false} limit={5} />
          <p className="muted small">
            From Benzinga via Alpaca. Positive and negative are a rough reading of each headline&apos;s words.
          </p>
        </section>
      )}
      {data?.position && <PositionView position={data.position} data={data} />}
      {data && (
        <TradeBox
          symbol={symbol}
          price={price}
          position={data.position}
          trading={data.trading}
          fractionable={data.fractionable !== false}
          botManaged={Boolean(data.account_kind !== 'member' && data.position && data.in_universe
            && data.signal?.action !== 'MANUAL')}
          marketOpen={marketOpen}
          onDone={onTraded}
          onConnect={onConnect}
        />
      )}
    </div>
  );
}

function ModelView({ data, rules }) {
  const s = data.signal;
  if (!data.in_universe) {
    return (
      <section className="sheet-section">
        <h3>What the model thinks</h3>
        <p className="muted">{data.is_benchmark
          ? `${data.symbol} is the benchmark the bot compares itself with; it isn't traded.`
          : `${data.symbol} isn't one of the bot's stocks, so the model doesn't score it. You can still buy it yourself below.`}</p>
      </section>
    );
  }
  return (
    <section className="sheet-section">
      <h3>What the model thinks</h3>
      {s && Number.isFinite(s.proba) ? (
        <>
          <ProbabilityRail value={s.proba} entry={rules.entry} exit={rules.exit} action={s.action} />
          <p className="sheet-model-line">
            <strong>{prob(s.proba)}</strong> chance of gaining more than {pct(rules.label, 0)} over the next {rules.horizon} trading days.{' '}
            <span className="muted">{data.signal_audience === 'member' ? modelView(s, rules) : actionSentence(s, rules)}</span>
          </p>
        </>
      ) : (
        <p className="muted">No score yet. It appears after the bot's next daily run.</p>
      )}
    </section>
  );
}

function skipReason(reason = '') {
  const r = reason.toLowerCase();
  if (r.startsWith('market filter')) return "the S&P 500 is below its 50-day average, so it isn't opening new positions";
  if (r.startsWith('max ')) return 'it already holds as many stocks as it is allowed';
  if (r.startsWith('not enough cash')) return "there isn't enough cash for one share";
  if (r.startsWith('daily loss limit')) return "today's loss limit was hit, so it isn't buying more today";
  return r || 'something is stopping it today';
}

/** What the bot will do with this stock, in plain words. */
/** The model's view for someone who isn't the bot: the bot's buy and sell levels as a reference. */
export function modelView(s, rules) {
  switch (s.action) {
    case 'BUY': return `That's above the bot's buy level of ${prob(rules.entry)}, so the model expects a rise.`;
    case 'SELL': return `That's below the bot's sell level of ${prob(rules.exit)}, so the model expects weakness.`;
    default: return `That's between the bot's sell level (${prob(rules.exit)}) and buy level (${prob(rules.entry)}): no strong view.`;
  }
}

export function actionSentence(s, rules) {
  switch (s.action) {
    case 'BUY': return `That's above the ${prob(rules.entry)} the bot needs, so it's buying at the next open.`;
    case 'WATCH': return `The bot buys at ${prob(rules.entry)} or higher, so for now it's only watching.`;
    case 'HOLD': return `The bot keeps holding while this stays above ${prob(rules.exit)}.`;
    case 'SELL': return s.reason?.startsWith('held')
      ? 'The bot is selling because it has held it for its maximum number of days.'
      : `That's below ${prob(rules.exit)}, so the bot is selling at the next open.`;
    case 'MANUAL': return 'You bought this yourself, so the bot leaves it alone.';
    case 'PENDING': return 'An order for it is already waiting to fill.';
    case 'SKIP': return `The bot would buy, but ${skipReason(s.reason)}.`;
    default: return '';
  }
}

function PositionView({ position: p, data }) {
  let who;
  if (data.account_kind === 'member') who = 'This is in your own Alpaca paper account.';
  else if (data.signal?.action === 'MANUAL' || !data.in_universe) who = 'You bought this, so the bot leaves it alone.';
  else who = 'The bot manages this position.';
  return (
    <section className="sheet-section">
      <h3>Your position</h3>
      <dl className="sheet-stats">
        <div><dt>Shares</dt><dd>{shares(p.qty)}</dd></div>
        <div><dt>Value</dt><dd>{money(p.market_value)}</dd></div>
        <div><dt>Average cost</dt><dd>{money(p.avg_entry_price)}</dd></div>
        <div><dt>Gain or loss</dt><dd className={tone(p.unrealized_pl)}>{signedMoney(p.unrealized_pl)} <span className="sub">{signedPct(p.unrealized_plpc)}</span></dd></div>
      </dl>
      <p className="muted small">{who}</p>
    </section>
  );
}

/** The points the confirmation step spells out before an order is placed. */
export function reviewNotes({ side, symbol, marketOpen, botManaged, live }) {
  const notes = [];
  if (!marketOpen) notes.push({ text: 'The market is closed, so the order waits and fills when it opens.' });
  if (side === 'buy' && botManaged) {
    notes.push({ text: `The bot owns ${symbol} right now. Buying more makes the whole position yours, so the bot stops `
      + 'managing it (its stop-loss and take-profit stay on the shares it bought).' });
  } else if (side === 'buy') {
    notes.push({ text: "The bot won't sell stocks you buy yourself, and they have no automatic stop-loss." });
  }
  if (side === 'sell' && botManaged) notes.push({ text: `This also cancels the bot's stop-loss and take-profit for ${symbol}.` });
  if (live) notes.push({ text: 'This is your live account. The order uses real money.', warn: true });
  return notes;
}

export function TradeBox({
  symbol, price, position, trading, fractionable = true, botManaged, marketOpen, onDone, initialSide = 'buy', onConnect,
}) {
  const [side, setSide] = useState(initialSide);
  const [amount, setAmount] = useState('');
  const [step, setStep] = useState('edit');        // edit -> review -> sending -> done
  const [message, setMessage] = useState(null);
  const [err, setErr] = useState(null);
  const owned = position && position.qty > 0;
  const live = trading?.mode === 'live';

  if (trading?.needs_broker) {
    return (
      <section className="sheet-section">
        <h3>Buy or sell</h3>
        <p className="muted">Connect your Alpaca paper account to buy {symbol}. Your orders go into your own account, with pretend money.</p>
        {onConnect && <button type="button" className="button primary" onClick={onConnect}>Connect Alpaca account</button>}
      </section>
    );
  }
  if (!trading?.enabled) {
    return (
      <section className="sheet-section">
        <h3>Buy or sell</h3>
        <p className="muted">{trading?.note || 'Buying and selling from the dashboard is turned off (MANUAL_TRADING=false in backend/.env).'}</p>
      </section>
    );
  }

  const value = Number(amount);
  const est = estimateShares(value, price);
  const valid = side === 'buy' ? value >= 1 : owned;
  const reset = () => { setStep('edit'); setMessage(null); setErr(null); setAmount(''); };

  const submit = async () => {
    setStep('sending');
    setErr(null);
    try {
      const res = side === 'buy' ? await api.buy(symbol, Math.round(value * 100) / 100) : await api.sellAll(symbol);
      setMessage(res.message);
      setStep('done');
      onDone?.();
    } catch (e) {
      setErr(e.message);
      setStep('review');
    }
  };

  return (
    <section className={`sheet-section trade ${live ? 'is-live' : ''}`} aria-labelledby="trade-title">
      <div className="trade-head">
        <h3 id="trade-title">Buy or sell</h3>
        <span className={`account-tag ${live ? 'live' : ''}`}>{live ? 'Live account: real money' : trading?.mode === 'demo' ? 'Demo account: pretend money' : 'Paper account: pretend money'}</span>
      </div>

      {step === 'done' ? (
        <div className="trade-done" role="status">
          <p>{message}</p>
          <button type="button" className="button secondary" onClick={reset}>Place another order</button>
        </div>
      ) : (
        <>
          <div className="segmented" role="group" aria-label="Order side">
            <button type="button" aria-pressed={side === 'buy'} onClick={() => { setSide('buy'); setStep('edit'); setErr(null); }}>Buy</button>
            <button type="button" aria-pressed={side === 'sell'} disabled={!owned}
              onClick={() => { setSide('sell'); setStep('edit'); setErr(null); }}>Sell</button>
          </div>

          {step === 'edit' && side === 'buy' && (
            <div className="trade-form">
              <label htmlFor="trade-amount">Amount to invest</label>
              <div className="money-input">
                <span aria-hidden="true">$</span>
                <input id="trade-amount" inputMode="decimal" autoComplete="off" placeholder="0.00" value={amount}
                  onChange={(e) => setAmount(e.target.value.replace(/[^0-9.]/g, ''))}
                  onKeyDown={(e) => { if (e.key === 'Enter' && valid) setStep('review'); }} />
              </div>
              <div className="chips">
                {QUICK_AMOUNTS.map((a) => (
                  <button key={a} type="button" className="chip" onClick={() => setAmount(String(a))}>{money(a, { whole: true })}</button>
                ))}
              </div>
              <p className="muted small">
                {!fractionable
                  ? `${symbol} trades in whole shares only${est ? `: this buys ${Math.floor(est)} at about ${money(price)} each` : ''}.`
                  : est ? `About ${shares(est)} shares at ${money(price)}. Alpaca works out the exact number when the order fills.`
                    : 'Fractional shares are fine: any amount from $1.'}
              </p>
              <button type="button" className="button primary" disabled={!valid} onClick={() => setStep('review')}>Review order</button>
            </div>
          )}

          {step === 'edit' && side === 'sell' && (
            <div className="trade-form">
              <p>You own <strong>{shares(position.qty)} shares</strong> of {symbol}, worth about <strong>{money(position.market_value)}</strong>.</p>
              <button type="button" className="button primary" onClick={() => setStep('review')}>Review sale</button>
            </div>
          )}

          {(step === 'review' || step === 'sending') && (
            <div className="trade-review">
              <p className="trade-summary">
                {side === 'buy'
                  ? <>Buy <strong>{money(value)}</strong> of {symbol}{est ? <> (about {shares(est)} shares)</> : null} at the market price.</>
                  : <>Sell all <strong>{shares(position.qty)} shares</strong> of {symbol} at the market price.</>}
              </p>
              <ul className="trade-notes">
                {reviewNotes({ side, symbol, marketOpen, botManaged, live }).map((n) => (
                  <li key={n.text} className={n.warn ? 'loss' : undefined}>{n.text}</li>
                ))}
              </ul>
              {err && <p className="inline-error" role="alert">{err}</p>}
              <div className="trade-actions">
                <button type="button" className="button secondary" onClick={() => setStep('edit')} disabled={step === 'sending'}>Back</button>
                <button type="button" className={`button ${side === 'buy' ? 'buy' : 'sell'}`} onClick={submit} disabled={step === 'sending'}>
                  {step === 'sending' ? 'Placing order…' : side === 'buy' ? `Buy ${money(value)} of ${symbol}` : `Sell all ${symbol}`}
                </button>
              </div>
            </div>
          )}
        </>
      )}
    </section>
  );
}
