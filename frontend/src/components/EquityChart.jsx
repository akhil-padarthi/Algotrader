import { useId, useMemo, useState } from 'react';
import { extent, linearScale, linePath, nearestIndex, ticks } from '../lib/chart.js';
import { clockTime, dateTime, localParts, money, monthYear, shortDate, signedPct, tone } from '../lib/format.js';
import { useWidth } from '../hooks.js';

const MAX_LABELS = 5;

/**
 * Your account vs the S&P 500 over the chosen period, as % change from the start of the period.
 * The S&P 500 line always covers the whole period. If the account is newer than the period, its
 * line starts where the account started (on the S&P line at that moment) and a marker shows when.
 * points: [{ t, date, equity, equity_pct, benchmark_pct }]
 */
export default function EquityChart({
  points, period = '1M', intraday = false, partial = false, timeZone, height = 280,
  benchmarkLabel = 'S&P 500', label = 'Your account',
}) {
  const [wrapRef, width] = useWidth(760);
  const [hover, setHover] = useState(null);
  const gradientId = `eq-area-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const pad = { top: 22, right: 56, bottom: 26, left: 6 };

  const geo = useMemo(() => {
    if (!points?.length) return null;
    let [lo, hi] = extent([...points.flatMap((p) => [p.equity_pct, p.benchmark_pct]), 0]);
    const padY = (hi - lo) * 0.08;
    lo -= padY;
    hi += padY;
    const bottom = height - pad.bottom;
    const x = linearScale([0, Math.max(points.length - 1, 1)], [pad.left, width - pad.right]);
    const y = linearScale([lo, hi], [bottom, pad.top]);
    const xs = points.map((_, i) => x(i));
    const ya = points.map((p) => (Number.isFinite(p.equity_pct) ? y(p.equity_pct) : NaN));
    const yb = points.map((p) => (Number.isFinite(p.benchmark_pct) ? y(p.benchmark_pct) : NaN));
    const valid = xs.map((_, i) => i).filter((i) => Number.isFinite(ya[i]));
    const first = valid[0];
    const last = valid[valid.length - 1];
    const line = linePath(valid.map((i) => xs[i]), valid.map((i) => ya[i]));
    const area = valid.length > 1 ? `${line}L${xs[last].toFixed(2)},${bottom}L${xs[first].toFixed(2)},${bottom}Z` : '';
    return { y, xs, ya, yb, bottom, line, area, first, last, bench: linePath(xs, yb), yTicks: ticks(lo, hi, 4),
      labels: pickLabels(points, period, intraday, timeZone, labelsFor(width)) };
  }, [points, width, height, period, intraday, timeZone]);

  if (!geo) return <div ref={wrapRef} className="chart-empty" style={{ height }}>No data for this period yet.</div>;

  const startEq = geo.first != null ? points[geo.first].equity : null;
  const endEq = geo.last != null ? points[geo.last].equity : null;
  const lineTone = startEq && endEq ? tone(endEq / startEq - 1) : 'flat';
  const fmtLabel = (p) => {
    if (period === '1D' && intraday) return clockTime(p.t, timeZone);
    if (intraday) return shortDate(localParts(p.t, timeZone).day);
    return period === '1A' ? monthYear(p.date) : shortDate(p.date);
  };
  const when = (p) => (intraday ? dateTime(p.t, timeZone) : shortDate(p.date, { year: true }));
  const onMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    setHover(nearestIndex(geo.xs, ((e.clientX - rect.left) / rect.width) * width));
  };
  const h = hover != null ? points[hover] : null;
  const lastPoint = points[points.length - 1];
  const summary = `${benchmarkLabel} ${signedPct(lastPoint.benchmark_pct)} over the period`
    + (startEq && endEq ? `; ${label.toLowerCase()} ${signedPct(endEq / startEq - 1)}${partial ? ' since it started' : ''}` : '');
  const markerX = partial && geo.first != null ? geo.xs[geo.first] : null;

  return (
    <div ref={wrapRef} className={`equity-chart tone-${lineTone}`}>
      <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} role="img" aria-label={summary}
        onPointerMove={onMove} onPointerLeave={() => setHover(null)}>
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" className="area-top" />
            <stop offset="100%" className="area-bottom" />
          </linearGradient>
        </defs>
        {geo.yTicks.map((t) => (
          <g key={t}>
            <line className={t === 0 ? 'grid zero' : 'grid'} x1={pad.left} x2={width - pad.right} y1={geo.y(t)} y2={geo.y(t)} />
            <text className="axis" x={width - pad.right + 8} y={geo.y(t) + 4}>{signedPct(t, Math.abs(t) < 0.1 ? 1 : 0)}</text>
          </g>
        ))}
        {markerX != null && (
          <g>
            <line className="start-marker" x1={markerX} x2={markerX} y1={pad.top - 4} y2={geo.bottom} />
            <text className="start-label" x={markerX + (markerX > width - 170 ? -6 : 6)} y={pad.top - 8}
              textAnchor={markerX > width - 170 ? 'end' : 'start'}>
              You started {shortDate(intraday ? localParts(points[geo.first].t, timeZone).day : points[geo.first].date)}
            </text>
          </g>
        )}
        {geo.area && <path d={geo.area} className="area" fill={`url(#${gradientId})`} />}
        <path d={geo.bench} className="line bench" />
        <path d={geo.line} className="line portfolio" />
        {geo.last != null && <circle cx={geo.xs[geo.last]} cy={geo.ya[geo.last]} r="4" className="end-dot" />}
        {geo.labels.map((i) => (
          <text key={i} className="axis" x={geo.xs[i]} y={height - 7}
            textAnchor={i === 0 ? 'start' : geo.xs[i] > width - pad.right - 40 ? 'end' : 'middle'}>
            {fmtLabel(points[i])}
          </text>
        ))}
        {h && (
          <g className="crosshair">
            <line x1={geo.xs[hover]} x2={geo.xs[hover]} y1={pad.top} y2={geo.bottom} />
            {Number.isFinite(geo.yb[hover]) && <circle cx={geo.xs[hover]} cy={geo.yb[hover]} r="3.5" className="dot bench" />}
            {Number.isFinite(geo.ya[hover]) && <circle cx={geo.xs[hover]} cy={geo.ya[hover]} r="4" className="dot portfolio" />}
          </g>
        )}
      </svg>
      {h && (
        <div className="chart-tip" style={{ left: `${Math.min(Math.max((geo.xs[hover] / width) * 100, 12), 88)}%` }}>
          <strong>{when(h)}</strong>
          {h.equity != null && startEq
            ? <span>{label} {money(h.equity)} ({signedPct(h.equity / startEq - 1)}{partial ? ' since you started' : ''})</span>
            : <span className="muted">{label}: not open yet</span>}
          <span className="muted">{benchmarkLabel} {signedPct(h.benchmark_pct)}</span>
        </div>
      )}
      <div className="chart-legend" aria-hidden="true">
        <span><i className="key portfolio" />{label}</span>
        <span><i className="key bench" />{benchmarkLabel}</span>
      </div>
    </div>
  );
}

/** How many x-axis labels fit: about one per 110px, between 3 and MAX_LABELS. */
export function labelsFor(width) {
  return Math.max(3, Math.min(MAX_LABELS, Math.floor(width / 110)));
}

/** Indexes of the points that get an x-axis label. */
export function pickLabels(points, period, intraday, timeZone, maxLabels = MAX_LABELS) {
  const n = points.length;
  if (n === 0) return [];
  let idx;
  if (period === '1D' && intraday) {
    idx = points.map((_, i) => i).filter((i) => {
      const { hour, minute } = localParts(points[i].t, timeZone);
      return minute === 0 && hour % 2 === 0;
    });
  } else if (intraday) {
    const day = (p) => localParts(p.t, timeZone).day;
    idx = points.map((_, i) => i).filter((i) => i === 0 || day(points[i]) !== day(points[i - 1]));
  } else {
    idx = Array.from({ length: maxLabels }, (_, k) => Math.round((k * (n - 1)) / (maxLabels - 1)));
  }
  idx = [...new Set(idx)];
  const step = Math.ceil(idx.length / maxLabels);
  return idx.filter((_, k) => k % step === 0);
}
