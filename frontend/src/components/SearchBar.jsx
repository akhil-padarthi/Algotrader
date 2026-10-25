import { useEffect, useId, useRef, useState } from 'react';
import { api } from '../api.js';

/**
 * Stock search in the header (combobox). Empty: suggests the bot's watchlist.
 * Typing: matches symbols and company names across all US stocks Alpaca can trade.
 * "/" anywhere focuses it.
 */
export default function SearchBar({ watchlist = [], onSelect }) {
  const [query, setQuery] = useState('');
  const [open, setOpen] = useState(false);
  const [results, setResults] = useState([]);
  const [active, setActive] = useState(0);
  const [status, setStatus] = useState('idle');   // idle | loading | done | error
  const inputRef = useRef(null);
  const listId = `search-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;

  useEffect(() => {
    const onKey = (e) => {
      const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || '');
      if (e.key === '/' && !typing && !e.metaKey && !e.ctrlKey) {
        e.preventDefault();
        inputRef.current?.focus();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  useEffect(() => {
    const q = query.trim();
    if (!q) { setResults([]); setStatus('idle'); return undefined; }
    setStatus('loading');
    const ctrl = new AbortController();
    const id = setTimeout(() => {
      api.search(q, { signal: ctrl.signal })
        .then((r) => { setResults(r); setActive(0); setStatus('done'); })
        .catch((e) => { if (e.name !== 'AbortError') setStatus('error'); });
    }, 180);
    return () => { clearTimeout(id); ctrl.abort(); };
  }, [query]);

  const items = query.trim()
    ? results
    : watchlist.map((w) => ({ symbol: w.symbol, name: w.name || '', in_universe: true }));

  const choose = (item) => {
    if (!item) return;
    onSelect(item.symbol);
    setQuery('');
    setOpen(false);
    inputRef.current?.blur();
  };

  const onKeyDown = (e) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setOpen(true); setActive((a) => Math.min(a + 1, items.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); choose(items[active]); }
    else if (e.key === 'Escape') { setOpen(false); inputRef.current?.blur(); }
  };

  const showList = open && (items.length > 0 || status === 'done' || status === 'error');
  return (
    <div className="search">
      <svg className="search-icon" width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
        <circle cx="7" cy="7" r="5" /><path d="M11 11l3.5 3.5" />
      </svg>
      <input
        ref={inputRef}
        type="search"
        role="combobox"
        aria-label="Search stocks"
        aria-expanded={showList}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={showList && items[active] ? `${listId}-${items[active].symbol}` : undefined}
        placeholder="Search any stock, like Tesla or KO"
        value={query}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => { setQuery(e.target.value); setOpen(true); }}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 120)}
        onKeyDown={onKeyDown}
      />
      <kbd className="search-key" aria-hidden="true">/</kbd>
      {showList && (
        <ul id={listId} role="listbox" className="search-list">
          {!query.trim() && <li className="search-label" role="presentation">The bot&apos;s stocks</li>}
          {items.map((it, i) => (
            <li
              key={it.symbol}
              id={`${listId}-${it.symbol}`}
              role="option"
              aria-selected={i === active}
              className={i === active ? 'active' : ''}
              onMouseDown={(e) => { e.preventDefault(); choose(it); }}
              onMouseEnter={() => setActive(i)}
            >
              <span className="search-symbol">{it.symbol}</span>
              <span className="search-name">{it.name}</span>
              {it.in_universe && query.trim() && <span className="tag model">bot</span>}
            </li>
          ))}
          {query.trim() && status === 'done' && !items.length && (
            <li className="search-empty" role="presentation">No US stock matches &ldquo;{query.trim()}&rdquo;</li>
          )}
          {status === 'error' && <li className="search-empty" role="presentation">Search isn&apos;t available right now</li>}
        </ul>
      )}
    </div>
  );
}
