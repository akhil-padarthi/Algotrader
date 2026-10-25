import { test } from 'node:test';
import assert from 'node:assert/strict';
import { extent, linePath, linearScale, nearestIndex, ticks } from '../src/lib/chart.js';
import {
  companyName, countdown, estimateShares, localParts, marketStatus, money, monthYear, prob, relativeTime, shares, shortDate,
  signedMoney, signedPct, tone,
} from '../src/lib/format.js';
import { OUTDATED_SERVER, explain } from '../src/api.js';

test('money and signed formats', () => {
  assert.equal(money(1234.5), '$1,234.50');
  assert.equal(money(1234.5, { whole: true }), '$1,235');
  assert.equal(money(null), '–');
  assert.equal(signedMoney(12.3), '+$12.30');
  assert.equal(signedMoney(-12.3), '−$12.30');
  assert.equal(signedPct(0.1234), '+12.34%');
  assert.equal(signedPct(-0.05, 1), '−5.0%');
  assert.equal(signedPct(0.00001), '0.00%');
  assert.equal(prob(0.6789), '0.68');
});

test('tone classes', () => {
  assert.equal(tone(1), 'gain');
  assert.equal(tone(-1), 'loss');
  assert.equal(tone(0), 'flat');
  assert.equal(tone(undefined), 'flat');
});

test('relative time', () => {
  const now = Date.parse('2026-10-13T12:00:00Z');
  assert.equal(relativeTime('2026-10-13T11:59:58Z', now), 'just now');
  assert.equal(relativeTime('2026-10-13T11:58:00Z', now), '2 minutes ago');
  assert.equal(relativeTime('2026-10-14T12:00:00Z', now), 'in 1 day');
});

test('scales, extent and ticks', () => {
  assert.deepEqual(extent([3, null, 1, NaN, 2]), [1, 3]);
  const s = linearScale([0, 10], [100, 0]);
  assert.equal(s(5), 50);
  assert.deepEqual(ticks(-0.02, 0.11, 4), [0, 0.05, 0.1]);
  assert.equal(nearestIndex([0, 10, 20], 14), 1);
});

test('linePath skips gaps', () => {
  assert.equal(linePath([0, 1, 2, 3], [5, NaN, 7, 8]), 'M0.00,5.00M2.00,7.00L3.00,8.00');
});

test('countdown and market clock text', () => {
  assert.equal(countdown(3725000), '1:02:05');
  assert.equal(countdown(59000), '0:00:59');
  assert.equal(countdown(-5000), '0:00:00');
  assert.equal(countdown(2 * 86400000 + 4 * 3600000), '2d 4h');
  const now = Date.parse('2026-10-15T14:00:00-04:00');
  const open = marketStatus({ is_open: true, next_close: '2026-10-15T16:00:00-04:00' }, now);
  assert.equal(`${open.text}, ${open.until}`, 'Market open, closes in 2:00:00');
  const closed = marketStatus({ is_open: false, next_open: '2026-10-16T09:30:00-04:00' }, now);
  assert.equal(closed.until, 'opens in 19:30:00');
  assert.equal(marketStatus(null, now), null);
});

test('share estimates and formatting', () => {
  assert.equal(estimateShares(500, 250), 2);
  assert.equal(estimateShares('', 250), null);
  assert.equal(estimateShares(500, 0), null);
  assert.equal(shares(2), '2');
  assert.equal(shares(1.4567), '1.457');
  assert.equal(shares(0.5), '0.5');
  assert.equal(shares(150.4), '150');
});

test('dates: short, with year, month-year, and parts in the market time zone', () => {
  assert.equal(shortDate('2026-10-15'), 'Oct 15');
  assert.equal(shortDate('2026-10-15', { year: true }), 'Oct 15, 2026');
  assert.equal(monthYear('2025-10-16'), 'Oct 2025');
  assert.equal(shortDate(null), '–');
  // 01:30 UTC on Oct 16 is still Oct 15 in New York
  assert.deepEqual(localParts('2026-10-16T01:30:00Z', 'America/New_York'), { hour: 21, minute: 30, day: '2026-10-15' });
});

test('company names drop the share-class boilerplate', () => {
  assert.equal(companyName('Apple Inc. Common Stock'), 'Apple Inc.');
  assert.equal(companyName('Alphabet Inc. Class A Common Stock'), 'Alphabet Inc.');
  assert.equal(companyName('Visa Inc.'), 'Visa Inc.');
  assert.equal(companyName(null), '');
});

test('errors become sentences a person can act on', () => {
  assert.equal(explain(404, { error: 'not found' }), OUTDATED_SERVER);   // an old server without the route
  assert.match(OUTDATED_SERVER, /Ctrl\+C/);
  assert.equal(explain(400, { error: 'Unknown symbol ZZZZ' }), 'Unknown symbol ZZZZ');
  assert.match(explain(401, null), /log in/);
  assert.equal(explain(401, { error: 'Email or password is incorrect.' }), 'Email or password is incorrect.');
  assert.match(explain(502, null), /server hit an error \(502\)/);
});
