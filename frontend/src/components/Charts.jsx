import { extent, linePath } from '../lib/chart.js';
import { money, prob } from '../lib/format.js';

export function Sparkline({ values, width = 96, height = 28 }) {
  if (!values || values.length < 2) return <svg width={width} height={height} aria-hidden="true" />;
  const [lo, hi] = extent(values);
  const xs = values.map((_, i) => (i / (values.length - 1)) * (width - 2) + 1);
  const ys = values.map((v) => height - 2 - ((v - lo) / (hi - lo || 1)) * (height - 4));
  const up = values[values.length - 1] >= values[0];
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true" className="sparkline">
      <path d={linePath(xs, ys)} className={up ? 'gain-stroke' : 'loss-stroke'} />
    </svg>
  );
}

/**
 * Model probability on a 0-1 rail with the strategy's zones drawn in:
 * below `exit` the bot sells, at/above `entry` it buys, in between it holds.
 */
export function ProbabilityRail({ value, entry, exit, action }) {
  const v = Number.isFinite(value) ? Math.min(1, Math.max(0, value)) : null;
  return (
    <span className="rail" role="img" aria-label={`probability ${prob(value)}; sells below ${prob(exit)}, buys at ${prob(entry)} or higher`}>
      <span className="rail-zone sell" style={{ width: `${exit * 100}%` }} />
      <span className="rail-zone hold" style={{ left: `${exit * 100}%`, width: `${(entry - exit) * 100}%` }} />
      <span className="rail-zone buy" style={{ left: `${entry * 100}%`, width: `${(1 - entry) * 100}%` }} />
      {v != null && <span className={`rail-dot ${(action || '').toLowerCase()}`} style={{ left: `${v * 100}%` }} />}
    </span>
  );
}

/** Where the price sits between the bracket's stop-loss and take-profit. */
export function BracketBar({ stop, entry, target, price }) {
  if (![stop, target, price].every(Number.isFinite) || target <= stop) {
    return <span className="muted small">No bracket</span>;
  }
  const pos = (x) => `${Math.min(100, Math.max(0, ((x - stop) / (target - stop)) * 100))}%`;
  return (
    <div className="bracket" title={`Stop ${money(stop)}, entry ${money(entry)}, target ${money(target)}`}>
      <div className="bracket-track" />
      {Number.isFinite(entry) && <div className="bracket-entry" style={{ left: pos(entry) }} />}
      <div className={`bracket-price ${price >= entry ? 'gain' : 'loss'}`} style={{ left: pos(price) }} />
      <div className="bracket-labels">
        <span>{money(stop)}</span>
        <span>{money(target)}</span>
      </div>
    </div>
  );
}

export function Bars({ items, max, format = (v) => v.toFixed(3) }) {
  const top = max ?? Math.max(...items.map((i) => i.value), 1e-9);
  return (
    <ul className="bars">
      {items.map((it) => (
        <li key={it.label}>
          <span className="bars-label">{it.label}</span>
          <span className="bars-track"><span className="bars-fill" style={{ width: `${(it.value / top) * 100}%` }} /></span>
          <span className="bars-value">{format(it.value)}</span>
        </li>
      ))}
    </ul>
  );
}

const HB_FLAT = 'M0,13 H150';
// baseline, P wave, Q dip, R spike, S dip, T wave, baseline
const HB_BEAT = 'M0,13 H52 Q56,8 60,13 H64 L66,16 L70,2 L74,21 L77,13 H82 Q88,6 94,13 H150';

/**
 * ECG line under the live badge. It draws one heartbeat each time fresh data arrives
 * (`beat` changes), so it only moves when the dashboard really is receiving updates.
 * Connection lost -> flat line.
 */
export function Heartbeat({ status, beat }) {
  const alive = status === 'live' || status === 'polling';
  return (
    <svg className={`heartbeat ${status}`} width="150" height="22" viewBox="0 0 150 22" aria-hidden="true" focusable="false">
      <path d={HB_FLAT} className="hb-base" />
      {alive
        ? <path key={beat || 'first'} d={HB_BEAT} pathLength="100" className="hb-trace" />
        : <path d={HB_FLAT} className="hb-flat" />}
    </svg>
  );
}
