import { relativeTime } from '../lib/format.js';

const TONE_WORD = { positive: 'Positive', negative: 'Negative', neutral: 'Neutral' };

/** Headlines (Benzinga via Alpaca), each with a rough positive/negative reading of its words. */
export function NewsList({ articles, onSelect, showSymbols = true, limit = 8 }) {
  if (!articles) return <p className="empty">Loading news…</p>;
  if (!articles.length) return <p className="empty">No news in the last few days.</p>;
  return (
    <ul className="news-list">
      {articles.slice(0, limit).map((a) => (
        <li key={a.id} className="news-item">
          {a.url
            ? <a className="news-headline" href={a.url} target="_blank" rel="noreferrer">{a.headline}</a>
            : <span className="news-headline">{a.headline}</span>}
          <div className="news-meta">
            {showSymbols && a.symbols.slice(0, 3).map((s) => (
              <button key={s} type="button" className="sym-link" onClick={() => onSelect?.(s)}
                aria-label={`${s}: open chart`}>{s}</button>
            ))}
            <span>{a.source}</span>
            <span>{relativeTime(a.created_at)}</span>
            <span className={`tone-word ${a.tone}`}>{TONE_WORD[a.tone] || ''}</span>
          </div>
        </li>
      ))}
    </ul>
  );
}

/** "3 positive and 2 negative headlines in the last 3 days" */
export function newsSentence(s, days, olderShown = false) {
  if (!s || !s.count) {
    return `No headlines in the last ${days} days.${olderShown ? ' The most recent ones are below.' : ''}`;
  }
  const parts = [];
  if (s.positive) parts.push(`${s.positive} positive`);
  if (s.negative) parts.push(`${s.negative} negative`);
  const other = s.count - s.positive - s.negative;
  if (other) parts.push(`${other} neutral`);
  const mood = { positive: 'Mostly positive', negative: 'Mostly negative', neutral: 'Mixed' }[s.tone] || 'Mixed';
  return `${mood}: ${joinWords(parts)} headline${s.count === 1 ? '' : 's'} in the last ${days} days.`;
}

function joinWords(parts) {
  if (parts.length <= 1) return parts.join('');
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}

export default function NewsPanelBody({ data, onSelect }) {
  if (!data) return <p className="empty">Loading news…</p>;
  const counts = Object.values(data.by_symbol || {}).reduce(
    (t, s) => ({ count: t.count + s.count, positive: t.positive + s.positive, negative: t.negative + s.negative }),
    { count: 0, positive: 0, negative: 0 },
  );
  const avgTone = counts.count ? (counts.positive - counts.negative) / counts.count : 0;
  const tone = avgTone >= 0.2 ? 'positive' : avgTone <= -0.2 ? 'negative' : 'neutral';
  return (
    <>
      <p className="news-summary">{newsSentence({ ...counts, tone }, data.lookback_days, (data.articles || []).length > 0)}</p>
      <NewsList articles={data.articles} onSelect={onSelect} />
    </>
  );
}
