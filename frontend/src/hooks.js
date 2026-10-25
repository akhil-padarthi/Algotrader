import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from './api.js';

/** Fetch now, then every `intervalMs` (0 = once). Pauses while the tab is hidden. */
export function usePoll(fetcher, intervalMs, deps = []) {
  const [state, setState] = useState({ data: null, error: null, loading: true });
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const load = useCallback(async (signal) => {
    try {
      const data = await fetcherRef.current({ signal });
      setState({ data, error: null, loading: false });
    } catch (err) {
      if (err.name === 'AbortError') return;
      setState((s) => ({ data: s.data, error: err, loading: false }));
    }
  }, []);

  useEffect(() => {
    const ctrl = new AbortController();
    setState((s) => ({ ...s, loading: true }));
    load(ctrl.signal);
    if (!intervalMs) return () => ctrl.abort();
    const id = setInterval(() => {
      if (typeof document === 'undefined' || !document.hidden) load(ctrl.signal);
    }, intervalMs);
    return () => { ctrl.abort(); clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [intervalMs, load, ...deps]);

  return { ...state, reload: () => load() };
}

/**
 * Live account / positions / quotes via Server-Sent Events, falling back to
 * polling if the stream can't connect (some proxies buffer SSE).
 */
export function useLive({ enabled = true, fallbackMs = 10000, withAccount = true, stream = true } = {}) {
  const [snap, setSnap] = useState({ account: null, positions: null, quotes: {}, time: null });
  const [status, setStatus] = useState('connecting'); // connecting | live | polling | error
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!enabled) return undefined;
    let es = null;
    let pollId = null;
    let failures = 0;
    let closed = false;

    const poll = async () => {
      if (!withAccount) { setStatus('polling'); return; }
      try {
        const [account, positions] = await Promise.all([api.account(), api.positions()]);
        setSnap((s) => ({ ...s, account, positions, time: new Date().toISOString() }));
        setError(null);
        setStatus('polling');
      } catch (err) {
        setError(err);
        setStatus('error');
      }
    };
    const startPolling = () => {
      if (pollId || closed) return;
      poll();
      pollId = setInterval(poll, fallbackMs);
    };

    if (!stream || typeof EventSource === 'undefined') {     // no live stream on serverless hosts (Vercel)
      startPolling();
    } else {
      es = new EventSource(api.streamUrl(), { withCredentials: false });
      es.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.type === 'error') {
          setError(new Error(msg.error === 'setup_required' ? 'Alpaca API keys are not configured.' : msg.error));
          setStatus('error');
          return;
        }
        failures = 0;
        setError(null);
        setStatus('live');
        setSnap({ account: msg.account, positions: msg.positions, quotes: msg.quotes || {}, time: msg.time });
      };
      es.onerror = () => {
        failures += 1;
        if (failures >= 3) {
          es.close();
          startPolling();
        } else {
          setStatus('connecting');
        }
      };
    }
    return () => {
      closed = true;
      if (es) es.close();
      if (pollId) clearInterval(pollId);
    };
  }, [enabled, fallbackMs, withAccount, stream]);

  return { ...snap, status, error };
}

/**
 * Side-sheet dialog behaviour: focus moves inside, Esc closes, Tab stays inside, the page behind
 * doesn't scroll, and focus returns to whatever opened it.
 */
export function useDialog(sheetRef, closeRef, onClose) {
  useEffect(() => {
    const opener = document.activeElement;
    closeRef.current?.focus();
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); onClose(); return; }
      if (e.key !== 'Tab' || !sheetRef.current) return;
      const items = [...sheetRef.current.querySelectorAll('button, input, [href], [tabindex]:not([tabindex="-1"])')]
        .filter((el) => !el.disabled && el.offsetParent !== null);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    };
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
      if (opener && typeof opener.focus === 'function') opener.focus();
    };
  }, [sheetRef, closeRef, onClose]);
}

/** Width of a DOM element, tracked with ResizeObserver (for responsive SVG). */
export function useWidth(fallback = 720) {
  const ref = useRef(null);
  const [width, setWidth] = useState(fallback);
  useEffect(() => {
    if (!ref.current || typeof ResizeObserver === 'undefined') return undefined;
    const ro = new ResizeObserver(([entry]) => setWidth(Math.max(240, Math.floor(entry.contentRect.width))));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  return [ref, width];
}

/** Remember the previous value of something (for "price changed" flashes). */
export function usePrevious(value) {
  const ref = useRef();
  useEffect(() => { ref.current = value; }, [value]);
  return ref.current;
}

export function prefersReducedMotion() {
  return typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;
}

/** Re-render every `ms` (for countdowns). */
export function useNow(ms = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(id);
  }, [ms]);
  return now;
}

/** Smoothly counts from the previous value to the new one (instant for reduced motion). */
export function useAnimatedNumber(value, duration = 700) {
  const [shown, setShown] = useState(value);
  const fromRef = useRef(value);
  useEffect(() => {
    if (!Number.isFinite(value)) { setShown(value); return undefined; }
    const from = Number.isFinite(fromRef.current) ? fromRef.current : value;
    fromRef.current = value;
    if (from === value || prefersReducedMotion() || typeof requestAnimationFrame === 'undefined') {
      setShown(value);
      return undefined;
    }
    let raf;
    const start = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - start) / duration);
      const eased = 1 - (1 - k) ** 3;
      setShown(from + (value - from) * eased);
      if (k < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [value, duration]);
  return shown;
}

const THEME_KEY = 'algotrader-theme';

/** Light by default; the choice is remembered in this browser. */
export function useTheme() {
  const [theme, setTheme] = useState(() => {
    try { return localStorage.getItem(THEME_KEY) === 'dark' ? 'dark' : 'light'; } catch { return 'light'; }
  });
  useEffect(() => {
    if (typeof document === 'undefined') return;
    if (theme === 'dark') document.documentElement.dataset.theme = 'dark';
    else delete document.documentElement.dataset.theme;
    try { localStorage.setItem(THEME_KEY, theme); } catch { /* private mode */ }
  }, [theme]);
  return [theme, () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'))];
}
