# Build notes

Notes on how AlgoTrader is put together, the decisions behind it, how it's tested and the bugs I ran
into along the way.

## Design decisions

**Alpaca over plain REST.** `alpaca_client.py` talks to Alpaca's REST API with `requests` instead of using
the `alpaca-py` SDK. It's only a few endpoints (bars, snapshots, orders, positions, portfolio history,
clock, calendar, news), it keeps the dependency list short, and it made it easy to write a local fake of
those endpoints (`fake_alpaca.py`) so every trading path can be tested offline. The client follows
`next_page_token` pagination, retries 429/5xx with exponential backoff (honouring `Retry-After`), never
retries other 4xx errors, and rounds prices to Alpaca's penny rules.

**One strategy function.** `strategy.decide()` is the only place the trading rules live. The backtest and
the live bot both call it, so what gets backtested is exactly what trades.

**Labels match how orders fill.** The bot decides after the close and its orders fill at the next open, so
the label is the return from the next open to the open five sessions later, and the backtest fills at the
next open too.

**Chronological validation only.** The newest 25% of dates is held out. Inside the older 75% I use
walk-forward CV with an expanding window and an embargo of horizon + 1 sessions, because labels overlap
for five days and would otherwise leak across the split. The live model is refit on everything after
testing.

**Market conditions have to earn their place.** `train` fits the model twice, with and without the sector
and FRED features, on the same rows, and keeps the extra features only if they add at least 0.01 AUC on
average and win most folds. A looser rule picked them up by luck on pure noise.

**No peeking at FRED.** A FRED value dated day *d* is published the next morning, so every value is
shifted one day before it's used.

**The bot leaves manual trades alone.** It reads Alpaca's closed orders and only manages a position if
its most recent buy has a client order ID starting with `algo-`. Anything else is treated as the user's.

**Dashboard.** React 19 with hand-drawn SVG charts and no chart library, bundled with esbuild. The look
is deliberately plain: white page, one grey, green/red only for gains and losses, blue only for things you
can click, system font with tabular figures, and a charcoal dark mode. Live data comes over Server-Sent
Events every 5 seconds, with polling as a fallback.

## Build order

1. `config.py`: every setting from `.env`, paper trading by default, refuses live trading without a
   confirmation phrase and refuses leverage (`POSITION_SIZE_PCT x MAX_POSITIONS > 100%`).
2. `alpaca_client.py`
3. `data.py`: cleaning (duplicates, missing and non-positive prices, high/low repair, isolated spikes,
   UTC to New York trading dates) and a CSV cache.
4. `features.py`: 25 stock features and 6 market features, all backward-looking.
5. `model.py`: random forest with balanced class weights, purged walk-forward CV, hold-out test.
6. `strategy.py`
7. `backtest.py`: event-driven, next-open fills with slippage, bracket exits from daily high/low,
   gap-through-stop fills at the open, stop assumed first when both are touched.
8. `journal.py`, `trader.py`: daily cycle, bracket orders, idempotent client order IDs, exits that cancel
   the bracket legs and retry while Alpaca releases the shares, guards for stale data, blocked accounts
   and daily losses, and the scheduler.
9. `api.py`: Flask API and SSE stream, serves the built dashboard.
10. `frontend/`
11. Tests, Docker, docs.

## Versions

- **1.0.0** (Oct 13, 2026): the bot, the backtest, the dashboard, Docker and the demo. Shortly after:
  stock search, a stock panel with live price and charts, buying any dollar amount and selling from the
  dashboard, and the ticker and market countdown.
- **1.3.0** (Oct 16): market conditions from FRED, the 11 sector ETFs and Benzinga news; every chart period
  gets its own window and resolution (5-minute for 1D up to daily for 1Y); a cleaner redesign; charts that
  explain errors with a "Try again" button; the `doctor` command; a version check between dashboard and
  server.
- **1.4.0** (Oct 18): user accounts. Each person connects their own Alpaca paper account; only the admin
  sees the bot's account.
- **1.5.0** (Oct 18): `DATABASE_URL` support for PostgreSQL/Supabase, in a private `algotrader` schema with
  row-level security; `db status` and `db copy-from-sqlite`.
- **1.5.1** (Oct 18): connection strings with symbols in the password work as pasted.
- **1.6.0** (Oct 18): runs on Vercel's free plan (`app.py`, `vercel.json`, `.vercelignore`, a setup page
  for missing settings, the bot as a daily cron job behind `CRON_SECRET`, polling instead of SSE, client
  IPs from the proxy headers).

### Accounts (1.4.0)

`users.py` keeps users, sessions, login attempts and trades. Passwords are scrypt hashes (werkzeug).
Session tokens are random, sent in an HttpOnly SameSite=Lax cookie, and only their SHA-256 hash is
stored. Members' Alpaca keys are encrypted with Fernet using a key from `SECRET_KEY` or
`backend/data/secret.key`, never stored next to the data. Other rules: keys are checked against the paper
API before saving, one Alpaca account per person, 8 wrong passwords per account or 30 per IP in 15 minutes
pause logins, 10 sign-ups per IP per hour, every POST needs JSON plus a custom header from the same
origin, and request bodies are capped at 64 KB. The first account only becomes admin automatically on a
dashboard that's bound to localhost.

### Supabase (1.5.0)

`db.py` sits under the journal and the account store. Queries are written once with `?` placeholders and
translated for PostgreSQL, along with `AUTOINCREMENT` and `REAL` in the schema. A small pool keeps a few
connections open. Some choices: psycopg 3; tables in their own schema with RLS on and no grants for
Supabase's `anon`/`authenticated` roles (the default `public` schema is exposed through Supabase's REST
API); `sslmode=require` unless set; the transaction pooler (port 6543) is refused because it drops the
`search_path`; the encryption key never goes in the database. I tested the PostgreSQL path against a local
PostgreSQL 16 with psycopg 3.3.6; the account, API and trading tests run a second time on it.

### Vercel (1.6.0)

Vercel runs Python as short-lived functions, so: nothing can be saved to disk except `/tmp` (accounts and
the journal must be in Supabase, `SECRET_KEY` must be set, the model is uploaded with the site), a
request lasts at most 5 minutes (the bot becomes a cron job hitting `/api/cron/trade`, training stays
local), long-lived connections are cut (the dashboard polls), and every request comes through a proxy
(client IPs come from its headers so rate limits work per visitor). The Python bundle limit is 500 MB and
the app's packages come to about 360 MB. On the free plan a cron job only fires somewhere inside its hour,
so `30 21 * * 1-5` runs between 21:00 and 21:59 UTC: 5-6 pm New York in summer, 4-5 pm in winter.

Things worth knowing from the first deploy (CLI 64.5): env vars only reach deployments made after they're
saved, so run `vercel --prod` again after adding them; the dashboard's variable form rejects a key with a
stray space ("contains invalid characters"); and `vercel env add` asks for a type (Secret or Config) before
the value.

## Tests

`python -m unittest discover -s tests -t .` in `backend/`, `npm test` in `frontend/`.
Currently 215 backend tests (38 also run on PostgreSQL when `POSTGRES_TEST_URL` is set) and 45 frontend
tests, all passing.

| File | Tests | What it covers |
|---|---|---|
| `test_config.py` | 7 | defaults, env overrides, live-mode confirmation, leverage and threshold checks, `.env.example` |
| `test_data.py` | 6 | trading dates across DST, cleaning rules, cache round trip |
| `test_features.py` | 7 | no look-ahead (appending future bars never changes past features), RSI bounds, labels |
| `test_model.py` | 6 | AUC ~0.5 on a random walk, >0.65 on a planted pattern, embargo, save/load |
| `test_strategy.py` | 11 | every entry and exit rule, sizing, cash never double-spent |
| `test_backtest.py` | 10 | hand-computed P&L, gaps, same-day stop/target, slippage, drawdown |
| `test_alpaca_client.py` | 9 | pagination, retries, rejections, order JSON, over real HTTP |
| `test_trader.py` | 13 | full cycle against the fake Alpaca, no double buys, exits, guards, scheduler |
| `test_api.py` | 13 | every endpoint, chart data, errors, Basic auth, CORS, path traversal |
| `test_end_to_end.py` | 4 | the real CLI commands in order, plus the pipeline on real GOOG prices |
| `test_performance.py` | 10 | each chart period gets its own window and resolution |
| `test_context.py` | 21 | FRED formats and outages, the one-day delay, sectors, news check, feature selection |
| `test_stocks.py` | 17 | search, stock panel for every range, buying and selling, partial failures |
| `test_accounts.py` | 22 | hashing, rate limits, sessions, key encryption, member isolation, admin rules |
| `test_postgres.py` | 47 | the above on PostgreSQL, schema permissions, pooler handling, translation |
| `test_vercel.py` | 11 | setup page, `app.py` loading, cron secret, proxy IPs, `vercel.json`, `.vercelignore` |
| `test_browser.py` | 1 | headless Chromium: sign up, every panel and chart period, connect a demo account, buy, log out and in |
| `frontend/tests` | 45 | formatting, chart maths, every component with real and empty data |

One test runs a whole cycle with all warnings turned into errors, to make sure Alpaca's nanosecond
timestamps are handled cleanly. For real data, the end-to-end test uses the GOOG daily sample that ships
with matplotlib (2004-2008).

## Bugs the tests (or real runs) caught

1. The live cycle sent New York time labelled as UTC in the bars `end` parameter.
2. Sells created by "close position" don't carry the bot's client order ID; they're matched through the
   journal now.
3. The connection badge's base CSS class equalled its "connected" state class.
4. The fake Alpaca returned NaN rows for empty filters (pandas `assign` on an empty frame).
5. Sparklines used the server clock instead of the broker clock.
6. The `.env.example` test was self-fulfilling: loading the file changed the environment it compared against.
7. `serve` bound to `0.0.0.0` by default, exposing the dashboard on the LAN. Now `127.0.0.1`.
8. Alpaca's nanosecond timestamps caused a pandas warning; they're rounded to the second now.
9. A new account's chart was three straight segments (three daily points). It uses intraday history while
   the account is young.
10. The dollar input pushed the buy box 77 px past a 375 px screen.
11. The fake broker priced dollar orders when placed instead of when filled ($500 became $503.79).
12. "Sell all" orders were labelled as the bot's.
13. The stock panel showed raw reasons like "probability 0.49 < entry 0.58"; now plain sentences.
14. The market clock could sit at 0:00:00 for a minute at the bell.
15. "Loading chart" forever after updating: new dashboard files talking to an old server got 404s and the
    chart had no error state. Fixed with the version banner, error + retry, and a sturdier endpoint.
16. Period buttons didn't change the chart for a young account.
17. Market-condition features were sometimes kept by luck on noise (see the selection rule above).
18. FRED look-ahead (see above).
19. After the redesign, three tables made the page 113 px wider than a 390 px phone.
20. A failed stock load showed its error twice.
21. `doctor` reported the dashboard version as `__VERSION__`: an `npm run dev` watcher from the old version
    rebuilt the new files with its old build script. `doctor` and `serve` now detect it.
22. Failed logins weren't counted: the error was raised inside the transaction and rolled back the record.
23. A demo member's chart asked the bot's fake broker for prices with the member's keys.
24. A browser test only failed at weekends (the countdown shows days, not seconds).
25. On the login screen the theme button drifted to the middle of the top bar.
26. A heading went missing from this file during an edit.
27. A Supabase password with `/` or `&` broke URL parsing with a raw `ValueError`. Passwords are encoded
    automatically now.
28. Comparing the cron secret as text raised an error on non-ASCII input; it compares bytes now.

## What the results mean

- On synthetic data with a planted pattern the model finds it and the backtest makes money. That shows
  the machinery works, not that markets behave like that.
- On pure noise it finds nothing (AUC ~0.5) and barely trades.
- On real GOOG prices 2004-2008 (single stock, no market context) it got an out-of-sample AUC of 0.46 and
  a -2.4% backtest over Oct 2007 to Oct 2008. No edge, which is a realistic outcome.
- News tone is a simple word list. It only holds back new buys and never triggers a trade.
