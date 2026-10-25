// API client. The dashboard is normally served by the Flask backend (same origin),
// so BASE is ''. To host the frontend elsewhere, build with API_BASE=https://your-api.

/* global __API_BASE__ */
export const BASE = typeof __API_BASE__ !== 'undefined' ? __API_BASE__ : '';

export class ApiError extends Error {
  constructor(message, { status = 0, setupRequired = false, needsBroker = false, loginRequired = false } = {}) {
    super(message);
    this.status = status;
    this.setupRequired = setupRequired;
    this.needsBroker = needsBroker;
    this.loginRequired = loginRequired;
  }
}

/** Fired when the server says the log-in has ended (expired, or logged out in another tab). */
export const LOGGED_OUT_EVENT = 'algotrader:logged-out';

function failure(status, body) {
  const err = new ApiError(explain(status, body), {
    status,
    setupRequired: Boolean(body?.setup_required),
    needsBroker: Boolean(body?.needs_broker),
    loginRequired: Boolean(body?.login_required),
  });
  if (err.loginRequired && typeof window !== 'undefined') window.dispatchEvent(new Event(LOGGED_OUT_EVENT));
  return err;
}

export async function getJSON(path, { signal } = {}) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, { signal, headers: { Accept: 'application/json' }, credentials: 'same-origin' });
  } catch (err) {
    if (err.name === 'AbortError') throw err;
    throw new ApiError('Cannot reach the AlgoTrader API. Is the backend running?');
  }
  let body = null;
  try {
    body = await res.json();
  } catch {
    // non-JSON error page
  }
  if (!res.ok) throw failure(res.status, body);
  return body;
}

export const OUTDATED_SERVER = 'The server is still running older code that doesn\'t have this feature. '
  + 'Restart it: press Ctrl+C in the window running "python -m algotrader serve", then run that command again.';

/** Turn an error response into a sentence a person can act on. */
export function explain(status, body) {
  // an older server answers unknown /api routes with exactly this
  if (status === 404 && body?.error === 'not found') return OUTDATED_SERVER;
  if (body?.error) return body.error;
  if (status === 401) return 'Please log in to continue.';
  if (status >= 500) return `The server hit an error (${status}). Check the window running the server for details.`;
  return `Request failed (${status})`;
}

/** POST for actions that change something (trades, log-in, profile). The custom header is required by
 *  the backend: other websites can't send it without a CORS preflight, which they fail. */
export async function postJSON(path, body) {
  let res;
  try {
    res = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/json', 'X-AlgoTrader': '1' },
      body: JSON.stringify(body),
      credentials: 'same-origin',
    });
  } catch {
    throw new ApiError('Cannot reach the AlgoTrader API. Is the backend running?');
  }
  let data = null;
  try { data = await res.json(); } catch { /* non-JSON */ }
  if (!res.ok) throw failure(res.status, data);
  return data;
}

const enc = encodeURIComponent;

export const api = {
  health: (o) => getJSON('/api/health', o),
  config: (o) => getJSON('/api/config', o),
  account: (o) => getJSON('/api/account', o),
  positions: (o) => getJSON('/api/positions', o),
  orders: (o) => getJSON('/api/orders?limit=40', o),
  performance: (period, o) => getJSON(`/api/performance?period=${encodeURIComponent(period)}`, o),
  market: (o) => getJSON('/api/market', o),
  signals: (o) => getJSON('/api/signals', o),
  bot: (o) => getJSON('/api/bot', o),
  model: (o) => getJSON('/api/model', o),
  backtest: (o) => getJSON('/api/backtest', o),
  streamUrl: () => `${BASE}/api/stream`,
  clock: (o) => getJSON('/api/clock', o),
  context: (o) => getJSON('/api/context', o),
  news: (o) => getJSON('/api/news?limit=10', o),
  search: (q, o) => getJSON(`/api/assets/search?q=${enc(q)}`, o),
  stock: (symbol, range, o) => getJSON(`/api/stock/${enc(symbol)}?range=${enc(range)}`, o),
  quote: (symbol, o) => getJSON(`/api/stock/${enc(symbol)}/quote`, o),
  buy: (symbol, amount) => postJSON('/api/trade', { symbol, side: 'buy', amount }),
  sellAll: (symbol) => postJSON('/api/trade', { symbol, side: 'sell_all' }),

  // accounts
  authStatus: (o) => getJSON('/api/auth/status', o),
  signup: (name, email, password) => postJSON('/api/auth/signup', { name, email, password }),
  login: (email, password) => postJSON('/api/auth/login', { email, password }),
  logout: () => postJSON('/api/auth/logout', {}),
  me: (o) => getJSON('/api/me', o),
  updateProfile: (name) => postJSON('/api/me/profile', { name }),
  changePassword: (current, next) => postJSON('/api/me/password', { current_password: current, new_password: next }),
  connectAlpaca: (keyId, secretKey) => postJSON('/api/me/alpaca', { key_id: keyId, secret_key: secretKey }),
  connectDemo: () => postJSON('/api/me/alpaca/demo', {}),
  disconnectAlpaca: () => postJSON('/api/me/alpaca/unlink', {}),
  myTrades: (o) => getJSON('/api/me/trades?limit=50', o),
  deleteAccount: (password) => postJSON('/api/me/delete', { password }),
};
