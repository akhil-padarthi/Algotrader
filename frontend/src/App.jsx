import { useCallback, useEffect, useMemo, useState } from 'react';
import { LOGGED_OUT_EVENT, api } from './api.js';
import { useLive, usePoll, useTheme } from './hooks.js';
import { pct, relativeTime, shortDate, signedMoney, signedPct, tone } from './lib/format.js';
import { APP_VERSION } from './version.js';
import {
  AccountButton, AuthScreen, ConnectBroker, ConnectPrompt, ProfileSheet,
} from './components/Account.jsx';
import EquityChart from './components/EquityChart.jsx';
import { Heartbeat } from './components/Charts.jsx';
import { AnimatedMoney, MarketClock, TickerTape } from './components/Live.jsx';
import MarketToday from './components/MarketToday.jsx';
import ModelReport from './components/ModelReport.jsx';
import NewsPanelBody from './components/News.jsx';
import {
  BotActivity, InlineError, MarketWatch, OrdersTable, Panel, PositionsTable, SignalsList,
} from './components/Panels.jsx';
import SearchBar from './components/SearchBar.jsx';
import StockPanel from './components/StockPanel.jsx';

const PERIODS = ['1D', '1W', '1M', '3M', '6M', '1A'];
const PERIOD_BUTTON = { '1D': '1D', '1W': '1W', '1M': '1M', '3M': '3M', '6M': '6M', '1A': '1Y' };
const PERIOD_WORDS = {
  '1D': 'today', '1W': 'over the last week', '1M': 'over the last month', '3M': 'over the last 3 months',
  '6M': 'over the last 6 months', '1A': 'over the last year',
};
const MODE_TEXT = { paper: 'Paper account', live: 'Live account: real money', demo: 'Demo with made-up prices' };
const LIVE_TEXT = { live: 'Live', polling: 'Updating every 10s', connecting: 'Connecting…', error: 'Disconnected' };

export default function App() {
  const health = usePoll(api.health, 30000);
  const [theme, toggleTheme] = useTheme();
  const toggle = <ThemeToggle theme={theme} onToggle={toggleTheme} />;

  if (!health.data) {
    return (
      <Shell mode={null} status={toggle}>
        {health.error ? (
          <div className="center-note" role="alert">
            <h1>Can&apos;t reach the AlgoTrader server</h1>
            <p>{health.error.message}</p>
            <p>Start it with <code>python -m algotrader serve</code> from the <code>backend</code> folder, then reload this page.</p>
          </div>
        ) : (
          <p className="center-note">Loading…</p>
        )}
      </Shell>
    );
  }
  if (health.data.problems?.length) {
    return <Shell mode={null} status={toggle}><DeployProblems problems={health.data.problems} /></Shell>;
  }
  if (!health.data.alpaca_configured) {
    return <Shell mode={health.data.mode} status={toggle}><SetupGuide modelTrained={health.data.model_trained} /></Shell>;
  }
  if (!health.data.accounts) return <Dashboard health={health.data} themeToggle={toggle} />;
  return <AccountGate health={health.data} themeToggle={toggle} />;
}

/** Sign up or log in first; new members then connect their Alpaca paper account (or skip for now). */
function AccountGate({ health, themeToggle }) {
  const [auth, setAuth] = useState(null);
  const [error, setError] = useState(null);
  const [skipped, setSkipped] = useState(false);

  const load = useCallback(() => {
    api.authStatus().then((s) => { setAuth(s); setError(null); }).catch(setError);
  }, []);
  useEffect(() => {
    load();
    const onLoggedOut = () => load();
    window.addEventListener(LOGGED_OUT_EVENT, onLoggedOut);
    return () => window.removeEventListener(LOGGED_OUT_EVENT, onLoggedOut);
  }, [load]);

  const setUser = useCallback((user) => setAuth((a) => ({ ...a, user, has_users: true, first_account_is_admin: false })), []);
  const loggedOut = useCallback(() => { setSkipped(false); setAuth((a) => ({ ...a, user: null })); load(); }, [load]);

  if (!auth) {
    return (
      <Shell mode={null} status={themeToggle}>
        {error ? <p className="center-note inline-error" role="alert">{error.message}</p> : <p className="center-note">Loading…</p>}
      </Shell>
    );
  }
  const { user } = auth;
  if (!user) {
    return <Shell mode={null} status={themeToggle}><AuthScreen status={auth} onAuthed={setUser} /></Shell>;
  }
  if (user.account_kind === 'member' && !user.broker.linked && !skipped) {
    return (
      <Shell mode={user.mode} status={themeToggle}>
        <main className="auth wide">
          <section className="auth-card">
            <ConnectBroker standalone demo={auth.demo_accounts} onLinked={setUser} onSkip={() => setSkipped(true)} />
          </section>
        </main>
      </Shell>
    );
  }
  return (
    <Dashboard key={user.id} health={health} themeToggle={themeToggle} user={user} onUser={setUser}
      onLoggedOut={loggedOut} demoAccounts={auth.demo_accounts} />
  );
}

function Shell({ mode, status, search, ticker, children }) {
  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true" className="brand-mark">
            <path d="M2 16 L8 10 L12 13 L20 4" />
            <path d="M15 4 H20 V9" />
          </svg>
          <span>AlgoTrader</span>
          {mode && <span className={`mode-chip ${mode}`}>{MODE_TEXT[mode] || mode}</span>}
        </div>
        {search && <div className="topbar-search">{search}</div>}
        {status && <div className="topbar-status">{status}</div>}
      </header>
      {ticker}
      {children}
    </div>
  );
}

function ThemeToggle({ theme, onToggle }) {
  const dark = theme === 'dark';
  return (
    <button type="button" className="theme-toggle" onClick={onToggle} aria-pressed={dark}
      aria-label={dark ? 'Switch to light mode' : 'Switch to dark mode'} title={dark ? 'Light mode' : 'Dark mode'}>
      {dark ? (
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true">
          <circle cx="8" cy="8" r="3" />
          <path d="M8 1v1.5M8 13.5V15M1 8h1.5M13.5 8H15M3 3l1 1M12 12l1 1M3 13l1-1M12 4l1-1" />
        </svg>
      ) : (
        <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true"><path d="M13.5 9.5A6 6 0 0 1 6.5 2.5a6 6 0 1 0 7 7z" /></svg>
      )}
    </button>
  );
}

function Dashboard({ health, themeToggle, user = null, onUser, onLoggedOut, demoAccounts = false }) {
  const member = user?.account_kind === 'member';
  const needsBroker = member && !user.broker.linked;
  const [period, setPeriod] = useState('1M');
  const [selected, setSelected] = useState(null);
  const [profileOpen, setProfileOpen] = useState(false);
  const open = useCallback((symbol) => setSelected(symbol), []);
  const close = useCallback(() => setSelected(null), []);
  const openProfile = useCallback(() => { setSelected(null); setProfileOpen(true); }, []);
  const closeProfile = useCallback(() => setProfileOpen(false), []);
  const config = usePoll(api.config, 0);
  const live = useLive({ withAccount: !needsBroker, stream: health.live_stream !== false });
  const nothing = () => Promise.resolve(null);
  const perf = usePoll((o) => (needsBroker ? nothing() : api.performance(period, o)), period === '1D' ? 30000 : 60000,
    [period, needsBroker]);
  const market = usePoll(api.market, 15000);
  const signals = usePoll(api.signals, 60000, [needsBroker]);
  const context = usePoll(api.context, 600000);
  const news = usePoll(api.news, 180000);
  const bot = usePoll((o) => (member ? nothing() : api.bot(o)), 30000, [member]);
  const orders = usePoll((o) => (needsBroker ? nothing() : api.orders(o)), 15000, [needsBroker]);
  const model = usePoll(api.model, 0);
  const backtest = usePoll(api.backtest, 0);

  const tz = config.data?.timezone;
  const feed = config.data?.data_feed;
  const acct = live.account;
  const items = market.data?.items;
  const watchlist = useMemo(() => (items || []).filter((i) => !i.is_benchmark), [items]);
  const rules = config.data && {
    entry: config.data.strategy.entry_threshold, exit: config.data.strategy.exit_threshold,
    label: config.data.model.label_threshold, horizon: config.data.model.horizon_days,
  };
  const outdated = APP_VERSION !== 'dev' && health.version !== APP_VERSION;

  const status = (
    <>
      <MarketClock />
      <div className={`live-status ${live.status}`} aria-live="polite">
        <div className="live-row">
          <span className="live-dot" aria-hidden="true" />
          <span>
            {LIVE_TEXT[live.status]}
            {live.time && live.status !== 'error' && <span className="muted">, updated {relativeTime(live.time)}</span>}
          </span>
        </div>
        <Heartbeat status={live.status} beat={live.time} />
      </div>
      {themeToggle}
      {user && <AccountButton user={user} onOpen={openProfile} />}
    </>
  );

  return (
    <Shell
      mode={user ? user.mode : health.mode}
      status={status}
      search={<SearchBar watchlist={watchlist} onSelect={open} />}
      ticker={<TickerTape items={items} quotes={live.quotes} onSelect={open} />}
    >
      {outdated && (
        <p className="banner problem" role="alert">
          <strong>Restart the server to finish the update.</strong> These dashboard files are version {APP_VERSION}, but the
          server is still running {health.version ? `version ${health.version}` : 'an older version'}, so some parts won&apos;t load. Press Ctrl+C in the window running
          {' '}<code>python -m algotrader serve</code>, then run that command again and reload this page.
        </p>
      )}
      {health.mode === 'demo' && (
        <p className="banner">
          This is the demo: prices, market indicators and news are made up, and the broker is a stand-in for Alpaca.
          Use it to explore the dashboard. The numbers say nothing about real-market performance.
        </p>
      )}
      <main className="layout">
        <div className="col">
          <section className="panel hero o-1" aria-label="Account summary">
            {needsBroker ? (
              <>
                <p className="hero-label">Welcome, {user.name.split(' ')[0]}</p>
                <ConnectPrompt onConnect={openProfile} />
              </>
            ) : (
            <>
            <div className="hero-top">
              <div>
                <p className="hero-label">{user && !member ? 'The bot’s account value' : 'Account value'}</p>
                <p className="equity-figure"><AnimatedMoney value={acct?.equity} /></p>
                <PeriodLine perf={perf.data} period={period} />
              </div>
              <dl className="hero-facts">
                <div><dt>Today</dt><dd className={tone(acct?.day_pl)}>{signedMoney(acct?.day_pl)} <span className="sub">{signedPct(acct?.day_pl_pct)}</span></dd></div>
                <div><dt>Cash</dt><dd>{acct ? `$${Math.round(acct.cash).toLocaleString('en-US')}` : '–'}</dd></div>
                <div><dt>Invested</dt><dd>{acct && acct.equity ? pct(acct.long_market_value / acct.equity, 0) : '–'}</dd></div>
                <div><dt>Positions</dt><dd>{live.positions ? live.positions.length : '–'}</dd></div>
              </dl>
            </div>
            <div className="period-switch" role="group" aria-label="Chart period">
              {PERIODS.map((p) => (
                <button key={p} type="button" aria-pressed={p === period} onClick={() => setPeriod(p)}>{PERIOD_BUTTON[p]}</button>
              ))}
            </div>
            <InlineError error={perf.error} what="the chart" />
            <InlineError error={live.error} what="live account data" />
            <EquityChart
              points={perf.data?.period === period ? perf.data.points : []}
              period={period}
              intraday={Boolean(perf.data?.intraday)}
              partial={Boolean(perf.data?.partial)}
              timeZone={tz}
            />
            <ChartFoot perf={perf.data} period={period} />
            </>
            )}
          </section>

          <Panel id="positions" className="o-3" title={member ? 'Your positions' : 'Open positions'}
            note="Click any stock to see its chart, news and buy or sell.">
            {needsBroker
              ? <p className="empty">Your positions appear here once your Alpaca paper account is connected.</p>
              : <PositionsTable positions={live.positions} onSelect={open} member={member} />}
          </Panel>
          <Panel id="market" className="o-5" title="Market watch">
            <InlineError error={market.error} what="prices" />
            <MarketWatch items={items} quotes={live.quotes} onSelect={open} feed={feed} />
          </Panel>
          <Panel id="orders" className="o-8" title={member ? 'Your orders' : 'Recent orders'}>
            <InlineError error={orders.error} what="orders" />
            {needsBroker
              ? <p className="empty">Orders you place appear here.</p>
              : <OrdersTable orders={orders.data} timeZone={tz} onSelect={open} />}
          </Panel>
          <Panel id="model" className="o-9" title="How the model tested">
            <ModelReport model={model.data} backtest={backtest.data} benchmark="S&P 500" />
          </Panel>
        </div>

        <div className="col">
          <Panel id="today" className="o-2" title="Market today"
            note="Volatility, interest rates and credit from the Federal Reserve Bank of St. Louis, and how each sector is doing.">
            <InlineError error={context.error} what="market conditions" />
            <MarketToday data={context.data} onSelect={open} />
          </Panel>
          <Panel id="signals" className="o-4" title="Model signals"
            note={signals.data?.as_of
              ? `Chance each stock gains more than ${pct(config.data?.model?.label_threshold, 0)} in the next ${config.data?.model?.horizon_days ?? 5} trading days, from the ${shortDate(signals.data.as_of)} close.`
              : 'Chance each stock gains over the next few trading days.'}>
            <InlineError error={signals.error} what="signals" />
            <SignalsList data={signals.data} onSelect={open} />
          </Panel>
          <Panel id="news" className="o-6" title={member ? 'News on the model’s stocks' : 'News on your stocks'}
            note={config.data?.news_check ? 'The bot won’t open a new position when a stock’s recent news is clearly negative.' : null}>
            <InlineError error={news.error} what="news" />
            <NewsPanelBody data={news.data} onSelect={open} />
          </Panel>
          {!member && (
            <Panel id="bot" className="o-7" title="Bot activity"
              note={bot.data ? `Runs on trading days at ${formatTradeTime(bot.data.trade_time)} New York time, after the market closes.` : null}>
              <InlineError error={bot.error} what="bot status" />
              <BotActivity bot={bot.data} />
            </Panel>
          )}
        </div>
      </main>
      <footer className="foot">
        <p>Prices and your account: Alpaca Markets ({(feed || 'iex').toUpperCase()} exchange feed). Market indicators: Federal Reserve Bank of St. Louis (FRED). News: Benzinga, through Alpaca.</p>
        <p>AlgoTrader {health.version}. Not investment advice. Trading involves risk of loss.</p>
      </footer>
      {selected && (
        <StockPanel
          key={selected}
          symbol={selected}
          onClose={close}
          onSelect={open}
          timeZone={tz}
          feed={feed}
          rules={rules || undefined}
          sectors={context.data?.sectors}
          onTraded={() => { orders.reload(); market.reload(); }}
          onConnect={user ? openProfile : undefined}
        />
      )}
      {profileOpen && user && (
        <ProfileSheet user={user} demo={demoAccounts} onClose={closeProfile} onUser={onUser}
          onLoggedOut={() => { setProfileOpen(false); onLoggedOut(); }} />
      )}
    </Shell>
  );
}

function startDay(perf) {
  return perf?.account_start ? perf.account_start.slice(0, 10) : null;
}

function PeriodLine({ perf, period }) {
  if (!perf) return <p className="hero-sub">&nbsp;</p>;
  const s = perf.stats;
  if (!s) return <p className="hero-sub muted">No account history for this period yet.</p>;
  const when = perf.partial ? `since you started on ${shortDate(startDay(perf))}` : PERIOD_WORDS[period];
  const ahead = s.excess_return != null && s.excess_return >= 0;
  return (
    <p className="hero-sub">
      <span className={tone(s.return)}>{signedPct(s.return)}</span> {when}. The S&amp;P 500 moved{' '}
      <span className={tone(s.benchmark_return)}>{signedPct(s.benchmark_return)}</span> over the same time
      {s.excess_return != null && <>, so you&apos;re <span className={ahead ? 'gain' : 'loss'}>{ahead ? 'ahead' : 'behind'} by {pct(Math.abs(s.excess_return))}</span></>}.
    </p>
  );
}

function ChartFoot({ perf, period }) {
  if (!perf?.partial || !perf.stats) return null;
  return (
    <p className="chart-foot">
      The S&amp;P 500 moved {signedPct(perf.stats.benchmark_period_return)} {PERIOD_WORDS[period]}. Your account opened on{' '}
      {shortDate(startDay(perf))}, so its line starts there, level with the S&amp;P 500 at that moment.
    </p>
  );
}

function formatTradeTime(hhmm) {
  const [h, m] = (hhmm || '16:20').split(':').map(Number);
  const d = new Date(2000, 0, 1, h, m);
  return d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}

/** A hosted site (Vercel) that is missing settings: what to add, and where. */
export function DeployProblems({ problems }) {
  return (
    <main className="setup">
      <h1>This site isn&apos;t set up yet</h1>
      <p>The server is running, but it needs these settings first:</p>
      <ul className="problem-list">
        {problems.map((p) => <li key={p}>{p}</li>)}
      </ul>
      <p>
        On Vercel, add them in your project under <strong>Settings → Environment Variables</strong>, then deploy
        again (<code>vercel --prod</code>). The steps are in DEPLOYMENT.md, Option D.
      </p>
    </main>
  );
}

function SetupGuide({ modelTrained }) {
  return (
    <main className="setup">
      <h1>Connect your Alpaca paper account</h1>
      <p>The dashboard is running, but the server has no Alpaca API keys yet.</p>
      <ol>
        <li>Sign in at <a href="https://app.alpaca.markets" target="_blank" rel="noreferrer">app.alpaca.markets</a>, switch to your <strong>Paper</strong> account and generate API keys.</li>
        <li>Copy <code>.env.example</code> to <code>backend/.env</code> and paste the key and secret into <code>ALPACA_API_KEY</code> and <code>ALPACA_SECRET_KEY</code>.</li>
        <li>Check everything: <code>python -m algotrader doctor</code></li>
        {!modelTrained && <li>Train the model on 5 years of data: <code>python -m algotrader train</code></li>}
        <li>Restart <code>python -m algotrader serve</code> and reload this page.</li>
      </ol>
      <p className="muted">Want to look around first? Run <code>python -m algotrader demo</code> for a self-contained demo with made-up prices.</p>
    </main>
  );
}
