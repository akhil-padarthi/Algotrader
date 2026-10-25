// Number and date formatting used across the dashboard.

const usd = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });
const usdWhole = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
const num = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 });
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

const isNum = (x) => typeof x === 'number' && Number.isFinite(x);

export const DASH = '–';

export function money(x, { whole = false } = {}) {
  if (!isNum(x)) return DASH;
  return (whole ? usdWhole : usd).format(x);
}

export function signedMoney(x) {
  if (!isNum(x)) return DASH;
  const s = usd.format(Math.abs(x));
  return x > 0 ? `+${s}` : x < 0 ? `−${s}` : s;
}

export function pct(x, digits = 2) {
  if (!isNum(x)) return DASH;
  return `${(x * 100).toFixed(digits)}%`;
}

export function signedPct(x, digits = 2) {
  if (!isNum(x)) return DASH;
  const v = (x * 100).toFixed(digits);
  if (Number(v) === 0) return `${(0).toFixed(digits)}%`;
  return x > 0 ? `+${v}%` : `−${Math.abs(Number(v)).toFixed(digits)}%`;
}

export function number(x) {
  return isNum(x) ? num.format(x) : DASH;
}

export function compactNumber(x) {
  return isNum(x) ? compact.format(x) : DASH;
}

export function prob(x) {
  return isNum(x) ? x.toFixed(2) : DASH;
}

/** 'gain' | 'loss' | 'flat' - drives colour classes. */
export function tone(x) {
  if (!isNum(x) || x === 0) return 'flat';
  return x > 0 ? 'gain' : 'loss';
}

function toDate(iso) {
  if (!iso) return null;
  // a bare date (2026-10-13) is a calendar day: read it at noon so no time zone moves it
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

export function shortDate(iso, { year = false } = {}) {
  const d = toDate(iso);
  if (!d) return DASH;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', ...(year ? { year: 'numeric' } : {}) });
}

export function monthYear(iso) {
  const d = toDate(iso);
  return d ? d.toLocaleDateString('en-US', { month: 'short', year: 'numeric' }) : DASH;
}

/** Hour, minute and calendar day of a timestamp in a given time zone (default: the browser's). */
export function localParts(iso, timeZone) {
  const parts = new Intl.DateTimeFormat('en-US', {
    year: 'numeric', month: '2-digit', day: '2-digit', hour: 'numeric', minute: 'numeric', hourCycle: 'h23', timeZone,
  }).formatToParts(new Date(iso));
  const get = (type) => parts.find((x) => x.type === type)?.value;
  return { hour: Number(get('hour')), minute: Number(get('minute')), day: `${get('year')}-${get('month')}-${get('day')}` };
}

export function longDate(iso) {
  if (!iso) return DASH;
  const d = new Date(iso.length === 10 ? `${iso}T12:00:00` : iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

export function dateTime(iso, timeZone) {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit', timeZone });
}

export function relativeTime(iso, now = Date.now()) {
  if (!iso) return DASH;
  const t = new Date(iso).getTime();
  if (Number.isNaN(t)) return DASH;
  const s = Math.round((now - t) / 1000);
  if (Math.abs(s) < 5) return 'just now';
  const future = s < 0;
  const a = Math.abs(s);
  const unit = a < 60 ? [a, 'second'] : a < 3600 ? [Math.round(a / 60), 'minute'] : a < 86400 ? [Math.round(a / 3600), 'hour'] : [Math.round(a / 86400), 'day'];
  const label = `${unit[0]} ${unit[1]}${unit[0] === 1 ? '' : 's'}`;
  return future ? `in ${label}` : `${label} ago`;
}

export const PERIOD_LABEL = { '1W': 'week', '1M': 'month', '3M': '3 months', '6M': '6 months', '1A': 'year' };

/** 3725000 ms -> "1:02:05"; anything a day or more -> "2d 4h". */
export function countdown(ms) {
  if (!Number.isFinite(ms)) return DASH;
  const total = Math.max(0, Math.floor(ms / 1000));
  const d = Math.floor(total / 86400);
  const h = Math.floor((total % 86400) / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  if (d > 0) return `${d}d ${h}h`;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/** Market clock sentence from Alpaca's clock and the current (broker) time. */
export function marketStatus(clock, nowMs) {
  if (!clock) return null;
  const open = Boolean(clock.is_open);
  const remaining = Date.parse(open ? clock.next_close : clock.next_open) - nowMs;
  return {
    open, remaining, text: open ? 'Market open' : 'Market closed',
    until: `${open ? 'closes' : 'opens'} in ${countdown(remaining)}`,
  };
}

/** Rough share count for a dollar amount (Alpaca works out the exact number at the fill). */
export function estimateShares(amount, price) {
  const a = Number(amount);
  if (!(a > 0) || !(price > 0)) return null;
  return a / price;
}

export function shares(x) {
  if (!Number.isFinite(x)) return DASH;
  return x >= 100 ? x.toFixed(0) : x >= 1 ? x.toFixed(3).replace(/\.?0+$/, '') : x.toFixed(4).replace(/0+$/, '');
}

export function clockTime(iso, timeZone) {
  if (!iso) return DASH;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return DASH;
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit', timeZone });
}

/** "Apple Inc. Common Stock" -> "Apple Inc." for compact lists. */
export function companyName(name) {
  if (!name) return '';
  return name
    .replace(/\s+(Class [A-Z] )?(Common|Ordinary|Capital) (Stock|Shares)(,? .*)?$/i, '')
    .replace(/\s+Common Stock$/i, '')
    .trim();
}
