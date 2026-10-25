import { useEffect, useRef, useState } from 'react';
import { api } from '../api.js';
import { useDialog } from '../hooks.js';
import { money, relativeTime, shortDate } from '../lib/format.js';

/* ----------------------------------------------------------------- fields */
function Field({ id, label, value, onChange, type = 'text', autoComplete, hint, ...rest }) {
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <input id={id} type={type} value={value} autoComplete={autoComplete} onChange={(e) => onChange(e.target.value)}
        aria-describedby={hint ? `${id}-hint` : undefined} {...rest} />
      {hint && <p id={`${id}-hint`} className="field-hint">{hint}</p>}
    </div>
  );
}

function PasswordField({ id, label, value, onChange, autoComplete, hint }) {
  const [shown, setShown] = useState(false);
  return (
    <div className="field">
      <label htmlFor={id}>{label}</label>
      <div className="password-input">
        <input id={id} type={shown ? 'text' : 'password'} value={value} autoComplete={autoComplete}
          spellCheck="false" onChange={(e) => onChange(e.target.value)} aria-describedby={hint ? `${id}-hint` : undefined} />
        <button type="button" className="reveal" onClick={() => setShown((s) => !s)}
          aria-label={shown ? `Hide ${label.toLowerCase()}` : `Show ${label.toLowerCase()}`} aria-pressed={shown}>
          {shown ? 'Hide' : 'Show'}
        </button>
      </div>
      {hint && <p id={`${id}-hint`} className="field-hint">{hint}</p>}
    </div>
  );
}

/** Runs an async action with a busy flag and a readable error. */
function useAction() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      return await fn();
    } catch (err) {
      setError(err.message);
      return undefined;
    } finally {
      setBusy(false);
    }
  };
  return { busy, error, run, setError };
}

/* ------------------------------------------------------- sign up / log in */
export function AuthScreen({ status, onAuthed }) {
  const canSignUp = Boolean(status?.signups);
  const [mode, setMode] = useState(!status?.has_users && canSignUp ? 'signup' : 'login');
  const [name, setName] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const { busy, error, run, setError } = useAction();
  const signup = mode === 'signup';

  const submit = async (e) => {
    e.preventDefault();
    const res = await run(() => (signup ? api.signup(name, email, password) : api.login(email, password)));
    if (res?.user) onAuthed(res.user);
  };
  const switchTo = (m) => { setMode(m); setError(null); setPassword(''); };

  return (
    <main className="auth">
      <section className="auth-card" aria-labelledby="auth-title">
        <h1 id="auth-title">{signup ? 'Create your account' : 'Log in'}</h1>
        <p className="auth-lead">
          {signup
            ? 'Follow the market, see what the model thinks, and buy stocks in your own Alpaca paper account.'
            : 'Log in to see your portfolio and trade.'}
        </p>
        {signup && status?.first_account_is_admin && (
          <p className="auth-note">
            You&apos;re the first person here, so this account becomes the admin. The admin sees and trades the bot&apos;s account.
          </p>
        )}
        <form className="auth-form" onSubmit={submit} noValidate>
          {signup && <Field id="auth-name" label="Name" value={name} onChange={setName} autoComplete="name" />}
          <Field id="auth-email" label="Email" type="email" value={email} onChange={setEmail} autoComplete="email"
            inputMode="email" />
          <PasswordField id="auth-password" label="Password" value={password} onChange={setPassword}
            autoComplete={signup ? 'new-password' : 'current-password'} hint={signup ? 'At least 8 characters.' : null} />
          {error && <p className="inline-error" role="alert">{error}</p>}
          <button type="submit" className="button primary" disabled={busy}>
            {busy ? (signup ? 'Creating account…' : 'Logging in…') : (signup ? 'Create account' : 'Log in')}
          </button>
        </form>
        <p className="auth-switch">
          {signup && <>Already have an account? <button type="button" className="link-button" onClick={() => switchTo('login')}>Log in</button></>}
          {!signup && canSignUp && <>New here? <button type="button" className="link-button" onClick={() => switchTo('signup')}>Create an account</button></>}
          {!signup && !canSignUp && 'New sign-ups are closed on this site. Ask the admin for an account.'}
        </p>
      </section>
      <p className="auth-foot">
        Passwords are stored as secure hashes, never as text. This site only connects Alpaca paper accounts, which trade with pretend money.
      </p>
    </main>
  );
}

/* ------------------------------------------------ connect Alpaca account */
export function ConnectBroker({ demo, onLinked, onSkip, standalone = false }) {
  const [keyId, setKeyId] = useState('');
  const [secret, setSecret] = useState('');
  const { busy, error, run } = useAction();

  const submit = async (e) => {
    e.preventDefault();
    const res = await run(() => api.connectAlpaca(keyId, secret));
    if (res?.user) onLinked(res.user);
  };
  const useDemo = async () => {
    const res = await run(() => api.connectDemo());
    if (res?.user) onLinked(res.user);
  };

  return (
    <div className={`connect ${standalone ? 'standalone' : ''}`}>
      {standalone && (
        <>
          <h1>Connect your Alpaca paper account</h1>
          <p className="auth-lead">
            Your purchases go into your own Alpaca paper account. Alpaca gives every paper account $100,000 of pretend
            money to practise with, so nothing here uses real money.
          </p>
        </>
      )}
      <ol className="connect-steps">
        <li>Sign in, or create a free account, at <a href="https://app.alpaca.markets" target="_blank" rel="noreferrer">app.alpaca.markets</a>.</li>
        <li>Check that the account switcher at the top says <strong>Paper</strong>.</li>
        <li>Find <strong>API Keys</strong> on the home page and generate new keys.</li>
        <li>Copy the <strong>API Key ID</strong> and the <strong>Secret Key</strong> into the boxes below. Alpaca shows the secret only once.</li>
      </ol>
      <form className="auth-form" onSubmit={submit} noValidate>
        <Field id="alpaca-key" label="API Key ID" value={keyId} onChange={(v) => setKeyId(v.trim())} autoComplete="off"
          spellCheck="false" placeholder="PK…" />
        <PasswordField id="alpaca-secret" label="Secret Key" value={secret} onChange={(v) => setSecret(v.trim())} autoComplete="off" />
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="form-actions">
          <button type="submit" className="button primary" disabled={busy || !keyId || !secret}>
            {busy ? 'Checking with Alpaca…' : 'Connect account'}
          </button>
          {onSkip && <button type="button" className="button secondary" onClick={onSkip}>Do this later</button>}
        </div>
      </form>
      {demo && (
        <div className="demo-option">
          <p>This is the demo, so you can also use a stand-in account with $100,000 of pretend money.</p>
          <button type="button" className="button secondary" onClick={useDemo} disabled={busy}>Use a demo account</button>
        </div>
      )}
      <p className="muted small">
        Your keys are encrypted before they&apos;re saved. They&apos;re only used to show your account and place the orders you confirm.
      </p>
    </div>
  );
}

/** Shown in place of the portfolio until the person connects their Alpaca account. */
export function ConnectPrompt({ onConnect }) {
  return (
    <div className="connect-prompt">
      <p>
        <strong>Connect your Alpaca paper account</strong> to see your portfolio here and buy stocks. It takes about two
        minutes and uses pretend money.
      </p>
      <button type="button" className="button primary" onClick={onConnect}>Connect account</button>
    </div>
  );
}

/* --------------------------------------------------------- top bar button */
export function initials(name = '') {
  const parts = name.trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return '?';
  return (parts[0][0] + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
}

export function AccountButton({ user, onOpen }) {
  return (
    <button type="button" className="account-button" onClick={onOpen} aria-label={`Your account: ${user.name}`}>
      <span className="avatar" aria-hidden="true">{initials(user.name)}</span>
      <span className="account-button-name">{user.name.split(' ')[0]}</span>
    </button>
  );
}

/* ------------------------------------------------------------ profile sheet */
export function ProfileSheet({ user, demo, onClose, onUser, onLoggedOut }) {
  const sheetRef = useRef(null);
  const closeRef = useRef(null);
  useDialog(sheetRef, closeRef, onClose);
  const logout = async () => {
    try { await api.logout(); } catch { /* the session ends either way */ }
    onLoggedOut();
  };

  return (
    <>
      <div className="sheet-backdrop" onClick={onClose} aria-hidden="true" />
      <aside ref={sheetRef} className="sheet" role="dialog" aria-modal="true" aria-labelledby="profile-title">
        <ProfileView user={user} demo={demo} onUser={onUser} onLoggedOut={onLoggedOut} onLogout={logout}
          closeRef={closeRef} onClose={onClose} />
      </aside>
    </>
  );
}

/** Everything inside the profile sheet; plain props so it can be rendered in tests. */
export function ProfileView({ user, demo, onUser, onLoggedOut, onLogout, closeRef, onClose, trades: givenTrades }) {
  const admin = user.account_kind === 'bot';
  return (
    <div className="sheet-inner">
      <header className="sheet-head">
        <div>
          <h2 id="profile-title" className="sheet-symbol">{user.name}</h2>
          <p className="sheet-name">{user.email}</p>
        </div>
        <button ref={closeRef} type="button" className="icon-button" onClick={onClose} aria-label="Close">
          <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true"><path d="M4 4l10 10M14 4L4 14" /></svg>
        </button>
      </header>
      <p className="profile-meta">
        <span className="account-tag">{admin ? 'Admin' : 'Member'}</span>
        <span>Joined {shortDate(user.created_at, { year: true })}</span>
        <button type="button" className="button secondary small" onClick={onLogout}>Log out</button>
      </p>
      <BrokerSection user={user} demo={demo} onUser={onUser} />
      {!admin && <TradesSection linked={user.broker.linked} given={givenTrades} />}
      <NameSection user={user} onUser={onUser} />
      <PasswordSection />
      <DeleteSection onDeleted={onLoggedOut} />
    </div>
  );
}

function BrokerSection({ user, demo, onUser }) {
  const [replacing, setReplacing] = useState(false);
  const { busy, error, run } = useAction();
  if (user.account_kind === 'bot') {
    return (
      <section className="sheet-section">
        <h3>Alpaca account</h3>
        <p className="muted">As the admin you see and trade the bot&apos;s Alpaca account, the one set in <code>backend/.env</code>.</p>
      </section>
    );
  }
  const linked = user.broker.linked;
  const disconnect = async () => {
    const res = await run(() => api.disconnectAlpaca());
    if (res?.user) onUser(res.user);
  };
  return (
    <section className="sheet-section" aria-labelledby="broker-title">
      <h3 id="broker-title">Alpaca account</h3>
      {linked && !replacing ? (
        <>
          <p>
            Connected: paper account <strong>{user.broker.account || ''}</strong>
            {user.broker.linked_at && <span className="muted">, since {shortDate(user.broker.linked_at, { year: true })}</span>}
          </p>
          {error && <p className="inline-error" role="alert">{error}</p>}
          <div className="form-actions">
            <button type="button" className="button secondary small" onClick={() => setReplacing(true)}>Use different keys</button>
            <button type="button" className="button secondary small" onClick={disconnect} disabled={busy}>Disconnect</button>
          </div>
          <p className="muted small">Disconnecting removes your keys from this site. Your Alpaca account and its positions stay as they are.</p>
        </>
      ) : (
        <ConnectBroker demo={demo} onLinked={(u) => { setReplacing(false); onUser(u); }}
          onSkip={replacing ? () => setReplacing(false) : null} />
      )}
    </section>
  );
}

const SIDE_TEXT = { buy: 'Bought', sell: 'Sold' };

function TradesSection({ linked, given }) {
  const [trades, setTrades] = useState(given ?? null);
  useEffect(() => {
    if (given) return undefined;
    const ctrl = new AbortController();
    api.myTrades({ signal: ctrl.signal }).then((d) => setTrades(d.trades)).catch(() => setTrades([]));
    return () => ctrl.abort();
  }, [linked, given]);
  return (
    <section className="sheet-section">
      <h3>Your purchases</h3>
      {!trades && <p className="muted">Loading…</p>}
      {trades && !trades.length && <p className="muted">Nothing yet. Open any stock and use Buy to place your first order.</p>}
      {trades && trades.length > 0 && (
        <ul className="trade-history">
          {trades.map((t) => (
            <li key={t.id}>
              <span className={`trade-side ${t.side}`}>{SIDE_TEXT[t.side] || t.side}</span>
              <strong>{t.symbol}</strong>
              <span>{t.amount != null ? money(t.amount) : ''}{t.qty ? ` (${t.qty} shares)` : ''}</span>
              <span className="muted">{relativeTime(t.created_at)}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function NameSection({ user, onUser }) {
  const [name, setName] = useState(user.name);
  const [saved, setSaved] = useState(false);
  const { busy, error, run } = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const res = await run(() => api.updateProfile(name));
    if (res?.user) { onUser(res.user); setSaved(true); }
  };
  return (
    <section className="sheet-section">
      <h3>Profile</h3>
      <form className="auth-form" onSubmit={submit} noValidate>
        <Field id="profile-name" label="Name" value={name} onChange={(v) => { setName(v); setSaved(false); }} autoComplete="name" />
        <Field id="profile-email" label="Email" value={user.email} onChange={() => {}} readOnly hint="Your email is how you log in. It can't be changed here." />
        {error && <p className="inline-error" role="alert">{error}</p>}
        <div className="form-actions">
          <button type="submit" className="button secondary small" disabled={busy || name.trim() === user.name}>Save name</button>
          {saved && <span className="saved" role="status">Saved</span>}
        </div>
      </form>
    </section>
  );
}

function PasswordSection() {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [done, setDone] = useState(null);
  const { busy, error, run } = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const res = await run(() => api.changePassword(current, next));
    if (res) { setDone(res.message); setCurrent(''); setNext(''); }
  };
  return (
    <section className="sheet-section">
      <h3>Password</h3>
      <form className="auth-form" onSubmit={submit} noValidate>
        <PasswordField id="pw-current" label="Current password" value={current} onChange={setCurrent} autoComplete="current-password" />
        <PasswordField id="pw-new" label="New password" value={next} onChange={(v) => { setNext(v); setDone(null); }}
          autoComplete="new-password" hint="At least 8 characters. Other browsers are logged out when you change it." />
        {error && <p className="inline-error" role="alert">{error}</p>}
        {done && <p className="saved" role="status">{done}</p>}
        <div className="form-actions">
          <button type="submit" className="button secondary small" disabled={busy || !current || !next}>Change password</button>
        </div>
      </form>
    </section>
  );
}

function DeleteSection({ onDeleted }) {
  const [open, setOpen] = useState(false);
  const [password, setPassword] = useState('');
  const { busy, error, run } = useAction();
  const submit = async (e) => {
    e.preventDefault();
    const res = await run(() => api.deleteAccount(password));
    if (res) onDeleted();
  };
  return (
    <section className="sheet-section">
      <h3>Delete account</h3>
      <p className="muted small">
        Deletes your profile, your saved Alpaca keys and your purchase history from this site. Your Alpaca account and
        its positions are not affected.
      </p>
      {!open ? (
        <button type="button" className="button secondary small danger-text" onClick={() => setOpen(true)}>Delete my account…</button>
      ) : (
        <form className="auth-form" onSubmit={submit} noValidate>
          <PasswordField id="delete-password" label="Type your password to confirm" value={password} onChange={setPassword}
            autoComplete="current-password" />
          {error && <p className="inline-error" role="alert">{error}</p>}
          <div className="form-actions">
            <button type="submit" className="button sell small" disabled={busy || !password}>Delete my account</button>
            <button type="button" className="button secondary small" onClick={() => { setOpen(false); setPassword(''); }}>Cancel</button>
          </div>
        </form>
      )}
    </section>
  );
}
