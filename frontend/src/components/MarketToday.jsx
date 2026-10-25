import { pct, shortDate, signedPct, tone } from '../lib/format.js';

const UNIT_CHANGE = { '%': ' pts', ' pts': ' pts', '': '' };

function fmtValue(i) {
  const v = Number(i.value).toFixed(2);
  return i.unit === '%' ? `${v}%` : `${v}${i.unit}`;
}

function fmtChange(i) {
  const sign = i.change > 0 ? '+' : i.change < 0 ? '−' : '';
  return `${sign}${Math.abs(i.change).toFixed(2)}${UNIT_CHANGE[i.unit] ?? ''} ${i.change_label}`;
}

/**
 * Today's market conditions from FRED (St. Louis Fed) and the sector ETFs, each figure with
 * its source and date, plus one plain sentence that sums them up.
 */
export default function MarketToday({ data, onSelect }) {
  if (!data) return <p className="empty">Loading market conditions…</p>;
  const { indicators = [], sectors = [], trend, summary, problems = [], sources = [] } = data;
  return (
    <div className="market-today">
      {summary && <p className="mt-summary">{summary}</p>}
      {problems.map((p) => <p key={p} className="inline-error">{explainProblem(p)}</p>)}

      {indicators.length > 0 && (
        <ul className="indicators">
          {indicators.map((i) => (
            <li key={i.id} className="indicator">
              <span className="indicator-name">{i.name}</span>
              <span className="indicator-value">{fmtValue(i)}</span>
              <span className={`indicator-status ${i.tone}`}>{i.status}</span>
              <span className="indicator-change">{fmtChange(i)}</span>
              <p className="indicator-explain">
                {i.explain}{' '}
                <a className="indicator-source" href={i.url} target="_blank" rel="noreferrer">
                  {i.source}, {shortDate(i.date)}
                </a>
              </p>
            </li>
          ))}
        </ul>
      )}

      {trend && (
        <p className="trend-line">
          The S&amp;P 500 is {vsAverage(trend.vs_50d, 50)}
          {trend.vs_200d != null && <> and {vsAverage(trend.vs_200d, 200)}</>}
          , {trendWords(trend.label)}.
        </p>
      )}

      {sectors.length > 0 && (
        <>
          <h3 className="subhead">Sectors</h3>
          <div className="table-wrap">
            <table className="data sectors">
              <thead>
                <tr>
                  <th scope="col">Sector</th>
                  <th scope="col" className="num">Today</th>
                  <th scope="col" className="num">1 month</th>
                </tr>
              </thead>
              <tbody>
                {sectors.map((s) => (
                  <tr key={s.symbol}>
                    <th scope="row">
                      {s.name}{' '}
                      <button type="button" className="sym-link small" onClick={() => onSelect?.(s.symbol)}
                        aria-label={`${s.symbol}: open chart`}>{s.symbol}</button>
                    </th>
                    <td className={`num ${tone(s.change_1d)}`}>{signedPct(s.change_1d)}</td>
                    <td className={`num ${tone(s.change_1m)}`}>{signedPct(s.change_1m, 1)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </>
      )}

      <div className="sources">
        {sources.map((s) => (
          <p key={s.name}>
            <a href={s.url} target="_blank" rel="noreferrer">{s.name}</a>
            {`: ${s.used_for}`}
            {s.as_of ? `, as of ${shortDate(s.as_of)}` : ''}
            {s.ok ? '' : ' (not available right now)'}
          </p>
        ))}
      </div>
    </div>
  );
}

function vsAverage(v, days) {
  return (
    <>
      <strong className={tone(v)}>{pct(Math.abs(v), 1)} {v >= 0 ? 'above' : 'below'}</strong> its {days}-day average
    </>
  );
}

function trendWords(label) {
  return { Uptrend: 'so it is in an uptrend', Downtrend: 'so it is in a downtrend', Mixed: 'so its trend is mixed' }[label] || 'trend unknown';
}

function explainProblem(p) {
  if (p.startsWith('FRED')) return 'Market indicators from FRED aren\'t available right now, so they\'re missing below. Prices still work.';
  return `Some market data isn't available right now: ${p}`;
}

