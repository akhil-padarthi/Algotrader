import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { useAnimatedNumber, useNow } from '../hooks.js';
import { marketStatus, money, signedPct, tone } from '../lib/format.js';
import { FlashValue } from './Panels.jsx';

/**
 * Scrolling price strip under the header. The list is rendered twice and slid left by
 * exactly one copy, so the loop is seamless. Hover or keyboard focus pauses it; with
 * reduced motion it becomes a still, scrollable row.
 */
export function TickerTape({ items, quotes, onSelect }) {
  if (!items?.length) return <div className="ticker ticker-empty" aria-hidden="true" />;
  const rows = items.map((it) => {
    const q = quotes?.[it.symbol];
    return { ...it, price: q?.price ?? it.price, change_pct: q?.change_pct ?? it.change_pct };
  });
  const seconds = Math.max(24, rows.length * 4);
  const copy = (hidden) => (
    <ul className="ticker-copy" aria-hidden={hidden || undefined}>
      {rows.map((r) => (
        <li key={r.symbol}>
          <button type="button" tabIndex={hidden ? -1 : 0} onClick={() => onSelect(r.symbol)}
            aria-label={hidden ? undefined : `${r.symbol} ${money(r.price)}, ${signedPct(r.change_pct)} today. Open chart`}>
            <span className="ticker-symbol">{r.symbol}</span>
            <FlashValue value={r.price}><span className="ticker-price">{money(r.price)}</span></FlashValue>
            <span className={`ticker-change ${tone(r.change_pct)}`}>
              <span aria-hidden="true">{r.change_pct > 0 ? '▲' : r.change_pct < 0 ? '▼' : '•'}</span> {signedPct(r.change_pct)}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
  return (
    <nav className="ticker" aria-label="Prices">
      <div className="ticker-track" style={{ animationDuration: `${seconds}s` }}>
        {copy(false)}
        {copy(true)}
      </div>
    </nav>
  );
}

/** "Market open, closes in 1:02:05" counting down every second (uses the broker's clock). */
export function MarketClock() {
  const [clock, setClock] = useState(null);
  const now = useNow(1000);
  const lastLoad = useRef(0);
  const load = useCallback(() => {
    lastLoad.current = Date.now();
    return api.clock()
      .then((c) => setClock({ ...c, offset: Date.parse(c.timestamp) - Date.now() }))
      .catch(() => {});
  }, []);
  useEffect(() => {
    load();
    const id = setInterval(load, 60000);
    return () => clearInterval(id);
  }, [load]);
  const status = marketStatus(clock, now + (clock?.offset || 0));
  // the bell just rang: ask Alpaca again instead of showing 0:00:00 until the next refresh
  useEffect(() => {
    if (status && status.remaining <= 0 && Date.now() - lastLoad.current > 5000) load();
  }, [status?.remaining, load]);  // eslint-disable-line react-hooks/exhaustive-deps
  if (!status) return null;
  return (
    <div className={`market-clock ${status.open ? 'open' : 'closed'}`}>
      <span className="status-dot" aria-hidden="true" />
      <span>{status.text}, <span className="tabular">{status.until}</span></span>
    </div>
  );
}

/** Dollar amount that counts smoothly to its new value when it changes. */
export function AnimatedMoney({ value, className }) {
  const shown = useAnimatedNumber(value);
  return <span className={className}>{Number.isFinite(value) ? money(shown) : '—'}</span>;
}
