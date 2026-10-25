import { useEffect, useId, useMemo, useState } from 'react';
import { extent, linearScale, linePath, nearestIndex, ticks } from '../lib/chart.js';
import { clockTime, dateTime, localParts, money, shortDate, tone } from '../lib/format.js';
import { useWidth } from '../hooks.js';

const INTRADAY = new Set(['1D', '1W', '1M']);

/**
 * One stock's price, drawn like a broker app: green/red line by result, soft area, and on the
 * 1-day view a dashed line at yesterday's close. `live` adds the latest trade as a pulsing point.
 * bars: [{ t, c }]   live: { t, price } | null
 */
export default function PriceChart({
  bars, range, prevClose, live, timeZone, height = 260, loading = false, error = null, note = null, onRetry, daily = false,
}) {
  const [wrapRef, width] = useWidth(560);
  const [hover, setHover] = useState(null);
  const slow = useSlow(loading, 12000);
  const gradientId = `px-area-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const pad = { top: 14, right: 58, bottom: 26, left: 4 };

  const points = useMemo(() => {
    const pts = (bars || []).map((b) => ({ t: b.t, price: b.c }));
    if (live && Number.isFinite(live.price) && pts.length && Date.parse(live.t) > Date.parse(pts[pts.length - 1].t)) {
      pts.push({ t: live.t, price: live.price, live: true });
    }
    return pts;
  }, [bars, live]);

  const intradayDay = range === '1D' && !daily;
  const baseline = intradayDay && Number.isFinite(prevClose) ? prevClose : points[0]?.price;

  const geo = useMemo(() => {
    if (points.length < 2) return null;
    let [lo, hi] = extent([...points.map((p) => p.price), baseline]);
    const padY = (hi - lo) * 0.1;
    lo -= padY;
    hi += padY;
    const bottom = height - pad.bottom;
    const x = linearScale([0, points.length - 1], [pad.left, width - pad.right]);
    const y = linearScale([lo, hi], [bottom, pad.top]);
    const xs = points.map((_, i) => x(i));
    const ys = points.map((p) => y(p.price));
    const line = linePath(xs, ys);
    const area = `${line}L${xs[xs.length - 1].toFixed(2)},${bottom}L${xs[0].toFixed(2)},${bottom}Z`;

    // x labels: hours on the 1-day view, the start of each day for 1W, dates otherwise
    let labels;
    if (intradayDay) {
      labels = points.map((p, i) => i).filter((i) => {
        const { hour, minute } = localParts(points[i].t, timeZone);
        return minute === 0 && hour % 2 === 0 && !points[i].live;
      });
    } else if (range === '1W') {
      labels = points.map((p, i) => i).filter((i) => i === 0 || dayKey(points[i].t, timeZone) !== dayKey(points[i - 1].t, timeZone));
    } else {
      labels = [0, Math.floor((points.length - 1) / 3), Math.floor((2 * (points.length - 1)) / 3), points.length - 1];
    }
    return { y, xs, ys, bottom, line, area, labels: [...new Set(labels)], yTicks: ticks(lo, hi, 4) };
  }, [points, baseline, width, height, range, timeZone, intradayDay]);

  if (!geo) {
    let body;
    if (error) {
      body = (
        <div className="chart-problem" role="alert">
          <strong>Couldn&apos;t load the chart.</strong>
          <span>{error}</span>
          {onRetry && <button type="button" className="button secondary small" onClick={onRetry}>Try again</button>}
        </div>
      );
    } else if (loading) {
      body = slow ? 'Still loading… the first chart of the day can take a few seconds.' : 'Loading chart…';
    } else {
      body = 'No trades in this period yet.';
    }
    return <div ref={wrapRef} className="chart-empty" style={{ height }}>{body}</div>;
  }

  const last = points[points.length - 1];
  const lineTone = tone(last.price - baseline);
  const h = hover != null ? points[hover] : null;
  const isDaily = daily || !INTRADAY.has(range);
  // daily bars are calendar days: label them by their date, whatever the viewer's time zone
  const fmtLabel = (p) => (intradayDay ? clockTime(p.t, timeZone)
    : shortDate(isDaily ? p.t.slice(0, 10) : localParts(p.t, timeZone).day));
  const fmtTip = (p) => (isDaily ? shortDate(p.t.slice(0, 10), { year: true }) : dateTime(p.t, timeZone));
  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    setHover(nearestIndex(geo.xs, ((e.clientX - rect.left) / rect.width) * width));
  };
  const lastX = geo.xs[geo.xs.length - 1];
  const lastY = geo.ys[geo.ys.length - 1];

  return (
    <div ref={wrapRef} className={`price-chart tone-${lineTone}`}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        width="100%"
        height={height}
        role="img"
        aria-label={`Price from ${money(points[0].price)} to ${money(last.price)}`}
        onPointerMove={onMove}
        onPointerLeave={() => setHover(null)}
      >
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" className="area-top" />
            <stop offset="100%" className="area-bottom" />
          </linearGradient>
        </defs>
        {geo.yTicks.map((t) => (
          <g key={t}>
            <line className="grid" x1={pad.left} x2={width - pad.right} y1={geo.y(t)} y2={geo.y(t)} />
            <text className="axis" x={width - pad.right + 8} y={geo.y(t) + 4}>{money(t, { whole: t >= 1000 })}</text>
          </g>
        ))}
        {intradayDay && Number.isFinite(prevClose) && (
          <line className="prev-close" x1={pad.left} x2={width - pad.right} y1={geo.y(prevClose)} y2={geo.y(prevClose)} />
        )}
        <path d={geo.area} className="area" fill={`url(#${gradientId})`} />
        <path d={geo.line} className="line price" />
        {last.live && <circle cx={lastX} cy={lastY} r="9" className="live-ring" />}
        <circle cx={lastX} cy={lastY} r="3.5" className="end-dot" />
        {geo.labels.map((i) => (
          <text key={i} className="axis" x={geo.xs[i]} y={height - 7}
            textAnchor={i === 0 ? 'start' : geo.xs[i] > width - pad.right - 30 ? 'end' : 'middle'}>
            {fmtLabel(points[i])}
          </text>
        ))}
        {h && (
          <g className="crosshair">
            <line x1={geo.xs[hover]} x2={geo.xs[hover]} y1={pad.top} y2={geo.bottom} />
            <circle cx={geo.xs[hover]} cy={geo.ys[hover]} r="4" className="dot price" />
          </g>
        )}
      </svg>
      {h && (
        <div className="chart-tip" style={{ left: `${(geo.xs[hover] / width) * 100}%` }}>
          <strong>{money(h.price)}</strong>
          <span className="muted">{h.live ? 'Latest trade' : fmtTip(h)}</span>
        </div>
      )}
      {intradayDay && Number.isFinite(prevClose) && (
        <p className="chart-note"><i className="key prev" aria-hidden="true" />Previous close {money(prevClose)}</p>
      )}
      {note && <p className="chart-note">{note}</p>}
    </div>
  );
}

/** true once `active` has stayed true for `ms` (for "still loading" messages). */
function useSlow(active, ms) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    setSlow(false);
    if (!active) return undefined;
    const id = setTimeout(() => setSlow(true), ms);
    return () => clearTimeout(id);
  }, [active, ms]);
  return slow;
}

function dayKey(iso, timeZone) {
  return localParts(iso, timeZone).day;
}
