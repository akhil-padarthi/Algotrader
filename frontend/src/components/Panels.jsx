import { useEffect, useState } from 'react';
import { usePrevious } from '../hooks.js';
import {
  DASH, companyName, compactNumber, dateTime, money, prob, relativeTime, shares, shortDate, signedMoney, signedPct, tone,
} from '../lib/format.js';
import { BracketBar, ProbabilityRail, Sparkline } from './Charts.jsx';

export function Panel({ title, note, action, className = '', children, id }) {
  return (
    <section className={`panel ${className}`} aria-labelledby={id}>
      <header className="panel-head">
        <div>
          <h2 id={id}>{title}</h2>
          {note && <p className="panel-note">{note}</p>}
        </div>
        {action}
      </header>
      {children}
    </section>
  );
}

export function InlineError({ error, what }) {
  if (!error) return null;
  return <p className="inline-error" role="alert">Couldn't load {what}: {error.message}</p>;
}

/** Ticker symbol that opens the stock panel. */
export function SymbolButton({ symbol, onSelect, children }) {
  if (!onSelect) return <>{symbol}{children}</>;
  return (
    <button type="button" className="sym-link" onClick={(e) => { e.stopPropagation(); onSelect(symbol); }}
      aria-label={`${symbol}: open chart and details`}>
      {symbol}{children}
    </button>
  );
}

/* ------------------------------------------------------------ positions */
export function PositionsTable({ positions, onSelect, member = false }) {
  if (!positions) return <p className="empty">Loading positions…</p>;
  if (!positions.length && member) {
    return <p className="empty">You don&apos;t own any stocks yet. Open any stock and use Buy to place your first order.</p>;
  }
  if (!positions.length) {
    return <p className="empty">No open positions. The bot buys at the daily run when a stock's probability reaches the entry threshold.</p>;
  }
  const total = positions.reduce((s, p) => s + p.unrealized_pl, 0);
  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col" className="num">Shares</th>
            <th scope="col" className="num hide-sm">Avg cost</th>
            <th scope="col" className="num">Price</th>
            <th scope="col" className="num hide-sm">Value</th>
            <th scope="col" className="num"><span className="hide-sm">Unrealized </span>P&amp;L</th>
            <th scope="col" className="bracket-col">Stop → target</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => (
            <tr key={p.symbol} className="clickable" onClick={() => onSelect?.(p.symbol)}>
              <th scope="row">
                <SymbolButton symbol={p.symbol} onSelect={onSelect} />
                {!p.managed && <span className="tag" title="You bought this; the bot leaves it alone">yours</span>}
              </th>
              <td className="num">{shares(p.qty)}</td>
              <td className="num hide-sm">{money(p.avg_entry_price)}</td>
              <td className="num"><FlashValue value={p.current_price}>{money(p.current_price)}</FlashValue></td>
              <td className="num hide-sm">{money(p.market_value, { whole: true })}</td>
              <td className={`num ${tone(p.unrealized_pl)}`}>
                {signedMoney(p.unrealized_pl)} <span className="sub">{signedPct(p.unrealized_plpc)}</span>
              </td>
              <td className="bracket-col">
                {p.pending_exit
                  ? <span className="pending-exit">Selling at next open</span>
                  : <BracketBar stop={p.stop_price} entry={p.avg_entry_price} target={p.take_profit_price} price={p.current_price} />}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr>
            <th scope="row" colSpan={2}>Total unrealized</th>
            <td className="hide-sm" />
            <td />
            <td className="hide-sm" />
            <td className={`num ${tone(total)}`}>{signedMoney(total)}</td>
            <td />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

/** Briefly highlights a value when it changes (live prices). */
export function FlashValue({ value, children }) {
  const prev = usePrevious(value);
  const [flash, setFlash] = useState('');
  useEffect(() => {
    if (prev == null || value == null || prev === value) return undefined;
    setFlash(value > prev ? 'flash-up' : 'flash-down');
    const id = setTimeout(() => setFlash(''), 900);
    return () => clearTimeout(id);
  }, [value, prev]);
  return <span className={`flash ${flash}`}>{children}</span>;
}

/* -------------------------------------------------------------- signals */
const EXPLAIN = new Set(['BUY', 'SELL', 'SKIP', 'PENDING', 'MANUAL']);
const reasonText = (r) => r.charAt(0).toUpperCase() + r.slice(1);
const ACTION_TEXT = { BUY: 'Buy', SELL: 'Sell', HOLD: 'Hold', WATCH: 'Watch', SKIP: 'Skipped', PENDING: 'Pending', MANUAL: 'Yours' };
// for people other than the admin: the model's view, not what the bot is doing with its own account
const ZONE_TEXT = { BUY: 'Buy zone', HOLD: 'Neutral', SELL: 'Sell zone' };

export function SignalsList({ data, onSelect }) {
  if (!data) return <p className="empty">Loading signals…</p>;
  if (!data.signals?.length) {
    return <p className="empty">No signals yet. They appear after the bot's first daily run (or run <code>python -m algotrader trade --dry-run</code>).</p>;
  }
  const { entry_threshold: entry, exit_threshold: exit } = data;
  const member = data.audience === 'member';
  const label = (a) => (member ? ZONE_TEXT[a] : ACTION_TEXT[a]) || a;
  return (
    <>
      <ul className="signals">
        {data.signals.map((s) => (
          <li key={s.symbol} className={s.held ? 'held' : ''}>
            <button type="button" className="sig-row" onClick={() => onSelect?.(s.symbol)}
              aria-label={`${s.symbol}: ${prob(s.proba)}, ${label(s.action)}. Open chart and details`}>
              <span className="sig-symbol">{s.symbol}{s.held ? <span className="tag">{member ? 'yours' : 'held'}</span> : null}</span>
              <ProbabilityRail value={s.proba} entry={entry} exit={exit} action={s.action} />
              <span className="sig-prob">{prob(s.proba)}</span>
              <span className={`sig-action ${s.action?.toLowerCase()}`} title={s.reason}>{label(s.action)}</span>
              {!member && EXPLAIN.has(s.action) && s.reason && <span className="sig-reason">{reasonText(s.reason)}</span>}
            </button>
          </li>
        ))}
      </ul>
      <p className="legend-line">
        {member ? (
          <>
            <span><i className="zone-key sell" />Sell zone below {prob(exit)}</span>
            <span><i className="zone-key hold" />Neutral between</span>
            <span><i className="zone-key buy" />Buy zone at {prob(entry)} or higher</span>
          </>
        ) : (
          <>
            <span><i className="zone-key sell" />Sells below {prob(exit)}</span>
            <span><i className="zone-key hold" />Holds between</span>
            <span><i className="zone-key buy" />Buys at {prob(entry)} or higher</span>
          </>
        )}
      </p>
    </>
  );
}

/* --------------------------------------------------------------- market */
export function MarketWatch({ items, quotes, onSelect, feed }) {
  if (!items) return <p className="empty">Loading prices…</p>;
  return (
    <div className="table-wrap">
      <table className="data market">
        <thead>
          <tr>
            <th scope="col">Symbol</th>
            <th scope="col" className="num">Price</th>
            <th scope="col" className="num">Today</th>
            <th scope="col" className="spark-col">30 days</th>
            <th scope="col" className="num hide-sm" title={feed === 'iex' ? 'Shares traded on the IEX exchange only (the free data feed), a small slice of total volume' : undefined}>
              {feed === 'iex' ? 'Volume (IEX)' : 'Volume'}
            </th>
            <th scope="col" className="num">Model</th>
          </tr>
        </thead>
        <tbody>
          {items.map((it) => {
            const live = quotes?.[it.symbol];
            const price = live?.price ?? it.price;
            const change = live?.change_pct ?? it.change_pct;
            return (
              <tr key={it.symbol} className={`clickable ${it.is_benchmark ? 'benchmark-row' : ''}`} onClick={() => onSelect?.(it.symbol)}>
                <th scope="row">
                  <SymbolButton symbol={it.symbol} onSelect={onSelect} />
                  {it.is_benchmark
                    ? <span className="company hide-sm">S&amp;P 500, the benchmark</span>
                    : it.name && <span className="company hide-sm">{companyName(it.name)}</span>}
                </th>
                <td className="num"><FlashValue value={price}>{money(price)}</FlashValue></td>
                <td className={`num ${tone(change)}`}>{signedPct(change)}</td>
                <td className="spark-col"><Sparkline values={it.sparkline} /></td>
                <td className="num muted hide-sm">{compactNumber(it.volume)}</td>
                <td className="num">{it.signal ? prob(it.signal.proba) : DASH}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/* --------------------------------------------------------------- orders */
const STATUS_TONE = { filled: 'gain', canceled: 'flat', expired: 'flat', rejected: 'loss', new: 'model', accepted: 'model', held: 'model', partially_filled: 'model', pending_new: 'model' };

const capitalize = (t) => (t ? t.charAt(0).toUpperCase() + t.slice(1) : t);
const ORDERS_FOLDED = 12;

export function OrdersTable({ orders, timeZone, onSelect }) {
  const [showAll, setShowAll] = useState(false);
  if (!orders) return <p className="empty">Loading orders…</p>;
  if (!orders.length) return <p className="empty">No orders yet.</p>;
  const shown = showAll ? orders : orders.slice(0, ORDERS_FOLDED);
  return (
    <>
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th scope="col">Submitted</th>
            <th scope="col">Symbol</th>
            <th scope="col">Side</th>
            <th scope="col" className="num">Qty</th>
            <th scope="col" className="hide-sm">Type</th>
            <th scope="col">Status</th>
            <th scope="col" className="num">Fill price</th>
            <th scope="col" className="hide-sm">Exit legs</th>
          </tr>
        </thead>
        <tbody>
          {shown.map((o) => (
            <tr key={o.id}>
              <td className="muted nowrap">{dateTime(o.submitted_at, timeZone)}</td>
              <th scope="row">
                <SymbolButton symbol={o.symbol} onSelect={onSelect} />
                {o.bot && <span className="tag model">bot</span>}
                {o.manual && <span className="tag">you</span>}
              </th>
              <td className={o.side === 'buy' ? 'gain' : 'loss'}>{o.side === 'buy' ? 'Buy' : 'Sell'}</td>
              <td className="num">{o.notional && !o.filled_qty ? money(o.notional) : shares(o.filled_qty || o.qty)}</td>
              <td className="muted hide-sm">{o.order_class === 'bracket' ? 'Bracket' : capitalize(o.type)}</td>
              <td><span className={`status ${STATUS_TONE[o.status] || 'flat'}`}>{o.status.replace('_', ' ')}</span></td>
              <td className="num">{o.filled_avg_price ? money(o.filled_avg_price) : DASH}</td>
              <td className="muted small hide-sm">
                {o.legs?.length
                  ? o.legs.map((l) => `${l.type === 'stop' ? 'stop' : 'target'} ${money(l.stop_price ?? l.limit_price)} (${l.status})`).join(', ')
                  : DASH}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
    {orders.length > ORDERS_FOLDED && (
      <button type="button" className="text-button" aria-expanded={showAll} onClick={() => setShowAll((v) => !v)}>
        {showAll ? 'Show fewer orders' : `Show all ${orders.length} orders`}
      </button>
    )}
    </>
  );
}

/* ------------------------------------------------------------------ bot */
const RUN_TEXT = { ok: 'Ran', skipped: 'Skipped', error: 'Failed', partial: 'Partly failed', running: 'Running' };

export function BotActivity({ bot }) {
  if (!bot) return <p className="empty">Loading bot status…</p>;
  const runs = bot.runs || [];
  return (
    <div className="bot">
      <dl className="bot-facts">
        <div><dt>Next run</dt><dd>{dateTime(bot.next_run, bot.timezone)} <span className="muted">({relativeTime(bot.next_run)})</span></dd></div>
        <div><dt>Orders</dt><dd>{bot.trading_enabled ? 'Placed automatically' : 'Dry run: signals only, no orders'}</dd></div>
      </dl>
      {runs.length === 0 ? (
        <p className="empty">The bot hasn't run yet. Start it with <code>python -m algotrader bot</code>.</p>
      ) : (
        <ol className="runs">
          {runs.slice(0, 8).map((r) => (
            <li key={r.id} className={`run ${r.status}`}>
              <span className="run-status">{RUN_TEXT[r.status] || r.status}</span>
              <span className="run-date">{r.as_of ? shortDate(r.as_of) : dateTime(r.started_at)}</span>
              <span className="run-msg">{r.message}</span>
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}
