// Small, dependency-free helpers for drawing SVG charts.

export function extent(values) {
  let lo = Infinity;
  let hi = -Infinity;
  for (const v of values) {
    if (typeof v !== 'number' || !Number.isFinite(v)) continue;
    if (v < lo) lo = v;
    if (v > hi) hi = v;
  }
  if (lo === Infinity) return [0, 1];
  if (lo === hi) return [lo - 1e-3 - Math.abs(lo) * 0.01, hi + 1e-3 + Math.abs(hi) * 0.01];
  return [lo, hi];
}

export function linearScale([d0, d1], [r0, r1]) {
  const span = d1 - d0 || 1;
  const f = (v) => r0 + ((v - d0) / span) * (r1 - r0);
  f.domain = [d0, d1];
  f.range = [r0, r1];
  return f;
}

/** "Nice" tick values covering [lo, hi]. */
export function ticks(lo, hi, count = 4) {
  const span = hi - lo;
  if (!(span > 0)) return [lo];
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Number(v.toFixed(10)));
  return out;
}

export function linePath(xs, ys) {
  let d = '';
  let pen = false;
  for (let i = 0; i < xs.length; i++) {
    const y = ys[i];
    if (typeof y !== 'number' || !Number.isFinite(y)) { pen = false; continue; }
    d += `${pen ? 'L' : 'M'}${xs[i].toFixed(2)},${y.toFixed(2)}`;
    pen = true;
  }
  return d;
}

export function nearestIndex(xs, x) {
  let best = 0;
  let bestD = Infinity;
  for (let i = 0; i < xs.length; i++) {
    const d = Math.abs(xs[i] - x);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}
