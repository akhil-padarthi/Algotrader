# Deploying, running and changing AlgoTrader

## Before you deploy

- [ ] `python -m algotrader check` passes with your **paper** keys
- [ ] `python -m algotrader doctor` shows no `[FAIL]` lines (it checks Alpaca, FRED, news, the model and the dashboard)
- [ ] `python -m algotrader train` has run and you've read the report
- [ ] Tests pass: `python -m unittest discover -s tests -t .` (backend) and `npm test` (frontend)
- [ ] User accounts are on (`USER_ACCOUNTS=true`, the default). On a server, sign up on the site, then make
      yourself the admin: `python -m algotrader users make-admin you@example.com` (on your own computer the
      first account becomes admin by itself). Close sign-ups with `ALLOW_SIGNUPS=false` if it's only for you.
- [ ] `backend/data/secret.key` is backed up, or `SECRET_KEY` is set. It encrypts the Alpaca keys people save;
      without it everyone has to connect their Alpaca account again (passwords are not affected).
- [ ] HTTPS in front of a server (Option B step 6): passwords and log-in cookies travel with every request.
- [ ] `ALPACA_PAPER=true`

The bot only needs to be awake once per trading day (16:20 New York time) but the dashboard is nicer
always-on, so a small always-on server is the best home for it.

---

## Option A: your own computer

Fine for trying it out. The machine must be on and awake at 16:20 New York time on trading days.

```bash
cd backend && source .venv/bin/activate
python -m algotrader serve     # terminal 1: dashboard at http://127.0.0.1:8000
python -m algotrader bot       # terminal 2: the trading bot
```

If the bot misses a day (computer asleep), nothing breaks: brackets still protect open positions at
Alpaca, and the next cycle picks up where it left off. You can also run a missed cycle by hand with
`python -m algotrader trade` any time after that day's close.

**Updating to a newer version:**

1. Stop `serve` and `bot` (Ctrl+C), and close any window running `npm run dev`. A running server keeps
   using the old code until it is restarted, and an old `npm run dev` watcher rebuilds new files with the
   old build script.
2. Get the new code: `git pull`
3. Install anything new, in the backend folder with the virtual environment active:
   `pip install -r requirements.txt`
4. Rebuild the dashboard: `cd frontend && npm install && npm run build`
5. Start `python -m algotrader serve` and `python -m algotrader bot` again, then reload the dashboard.
6. If the update changed data sources or model features, retrain: `python -m algotrader train --refresh`.
7. `python -m algotrader doctor` checks everything and says how to fix anything that isn't right.

If you forget step 1, the dashboard shows a "Restart the server to finish the update" banner.

---

## Option B (recommended): a small Linux server with Docker

Any $5–6/month VPS works (DigitalOcean, Hetzner, Lightsail, Linode). 1 GB RAM is enough.
One container runs both the bot and the dashboard; data, the model and the journal live on a Docker volume.

**1. Install Docker** on the server (Ubuntu):
```bash
curl -fsSL https://get.docker.com | sh
```

**2. Copy the project to the server** (git is easiest):
```bash
git clone https://github.com/<you>/algotrader.git && cd algotrader
```

**3. Configure:**
```bash
cp .env.example backend/.env
nano backend/.env        # add ALPACA keys; set SECRET_KEY to a long random text
```

**4. Build and train once** (downloads 5 years of data, ~1–3 minutes):
```bash
docker compose build
docker compose run --rm algotrader python -m algotrader train
```

**5. Start it:**
```bash
docker compose up -d
docker compose logs -f          # watch it; Ctrl+C stops watching, not the bot
```

The dashboard is on port 8000. Open `http://<server-ip>:8000` and create your account, then make it the admin
(on a server the first account doesn't become admin by itself, so a stranger can't claim it):
```bash
docker compose exec algotrader python -m algotrader users make-admin you@example.com
```

**6. Add HTTPS** (strongly recommended: passwords and log-in cookies are sent with every request). With a domain pointing at the server,
install [Caddy](https://caddyserver.com/docs/install) and use this `/etc/caddy/Caddyfile`:
```
trader.yourdomain.com {
    reverse_proxy 127.0.0.1:8000 {
        flush_interval -1
    }
}
```
`flush_interval -1` keeps the live price stream real-time. Then change the compose port to
`"127.0.0.1:8000:8000"` so the app is only reachable through Caddy, and `docker compose up -d` again.

**7. Retrain weekly** (the bot reloads the model before every cycle). `crontab -e` on the server:
```
0 10 * * 6  cd /home/<you>/algotrader && docker compose exec -T algotrader python -m algotrader train --refresh >> train.log 2>&1
```

**Updating to a new version of your code:**
```bash
git pull && docker compose up -d --build
```

**Backups:** everything is in the `algotrader-data` volume:
```bash
docker run --rm -v algotrader_algotrader-data:/data -v $PWD:/backup alpine tar czf /backup/algotrader-data.tgz /data
```

---

## Option C: Render, Railway or Fly.io

Use the included `Dockerfile` as a single service:

- **Service type:** web service from the repo's Dockerfile (Render: "Web Service"; Railway: deploy from repo; Fly: `fly launch`).
- **Persistent disk/volume:** mount it at **`/data`** (the model, cached data, accounts and journal live there).
  With [Supabase](#storing-data-in-supabase) the accounts and journal move out; without any disk, also set
  `TRAIN_ON_START=true` so the model is retrained at each start.
- **Environment variables:** everything from `.env.example`, at minimum `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `SECRET_KEY`. Don't upload the `.env` file itself.
- **Admin:** after signing up, run `python -m algotrader users make-admin you@example.com` from the platform's shell.
- **Instance:** must stay always-on (no free tier that sleeps), 512 MB–1 GB RAM.
- **Train once** from the platform's shell: `python -m algotrader train`.
- **Health check path:** `/api/health` (open without the password by design).

---

## Option D: Vercel (free plan) + Supabase

Vercel runs the website and the API; Supabase keeps the data; the bot runs once each weekday through a
Vercel **Cron Job** (`vercel.json`), shortly after the market closes. What's different from a normal server:

| On a server | On Vercel |
|---|---|
| `python -m algotrader bot` runs all the time | Vercel calls `/api/cron/trade` Mon–Fri some time between 21:00 and 21:59 UTC (the free plan only guarantees the hour): 5–6 pm New York in summer, 4–5 pm in winter. Orders fill at the next open either way. |
| The model can retrain on the server | Train on your computer; the model in `backend/models` is uploaded with the site. Retrain and redeploy whenever you like (every few weeks is plenty). |
| Prices update live every 5 seconds | The page refreshes them every 10–15 seconds (a Vercel request can't stay open). |
| Data on the server's disk | Accounts and the bot's history in Supabase (required); nothing is saved on Vercel itself. |

**1. Before the first deploy** (in the backend folder, with `.venv\Scripts\activate`):

- Make sure `DATABASE_URL` in `backend\.env` points to Supabase and `python -m algotrader db status` says **Connected.**
- Choose two secrets (run this twice and keep both results):
  ```
  python -c "import secrets; print(secrets.token_urlsafe(32))"
  ```
  Put the first one in `backend\.env` as `SECRET_KEY=...` so your computer and Vercel use the same one
  (it encrypts the Alpaca keys people save). The second is your `CRON_SECRET` (Vercel only).
- Train the model you'll upload: `python -m algotrader train --refresh`

**2. Install the Vercel command line tool** (once; Node.js is already installed):
```
npm install -g vercel
vercel login
```
Finish the log-in in the browser that opens; the command waits until you do. (If a later command says
`No existing credentials found`, the log-in wasn't finished: run `vercel login` again.)

**3. First deploy**, from the project folder (the one with `app.py` and `vercel.json`, not `backend`):
```
cd C:\path\to\algotrader
dir app.py vercel.json
vercel
```
Answer the questions: set up and deploy: yes; scope: your own account; link to an existing project: no;
project name: `algotrader`; directory: `./`; modify (customize) the settings: no. It prints a web address.
Opening it now shows "This site isn't set up yet" with the list of settings it needs: that's expected.
Only the files listed in `.vercelignore` are uploaded; your `.env`, local data and the virtual environment
never leave your computer.

**4. Add the settings**, each for the **Production** environment. The Key is the name; the Value is what
comes after `=` in `backend\.env` (no `=`, quotes or spaces):

| Key | Value |
|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | the bot's Alpaca paper keys (same as in `backend\.env`) |
| `DATABASE_URL` | your Supabase connection string (Session pooler) |
| `SECRET_KEY` | the first secret from step 1 (same as in `backend\.env`) |
| `CRON_SECRET` | the second secret from step 1 |
| `TRADE_TIME` | `17:30` (optional: only changes the "next run" time the dashboard shows) |

Either on the website: your project → **Settings → Environments → Production → Add Environment Variable**
(type the Key, paste the Value, **Add new variable** for the next one, then **Save**), or in cmd from the
project folder, one at a time:
```
vercel env add ALPACA_API_KEY production
```
It asks for the type (choose **Secret** for keys and passwords, **Config** for `TRADE_TIME`), then the
value (paste it and press Enter). `vercel env ls` lists what's saved.

- "The name of your Environment Variable contains invalid characters": a hidden space in the Key, usually
  from copy-paste. Type the Key by hand, or use the cmd method.
- "A variable with the name ... already exists": it's already saved. Skip it.

**5. Deploy again:** `vercel --prod`. Settings only reach deployments made after they were saved; if the
site still lists every setting as missing, you're looking at the old deployment (reload with Ctrl+F5 once
the new one says Ready). Open the address and log in with the account you already have (it's in Supabase).
Open it in a private window too: if Vercel asks you to log in, set **Settings → Deployment Protection** to
Standard Protection (or off). Check **Settings → Cron Jobs** in Vercel: `/api/cron/trade` should be listed.
Stop the bot on your computer: Vercel runs it now (running both is harmless, since the bot never places the
same order twice, but there's no need).

**Updating later:** `vercel --prod` from the project folder uploads the current version.
**Retraining:** `python -m algotrader train --refresh` in the backend folder, then `vercel --prod`.
**Running the bot now:** `vercel crons run /api/cron/trade` (a beta Vercel command), or
`python -m algotrader trade` on your computer after the close.

**If something's off:** Vercel → your project → **Logs** (or Settings → Cron Jobs → View Logs) shows every
request and each cron run. Vercel doesn't retry a failed run; a missed day can be run by hand as above.
A Python function on Vercel can be up to 500 MB; this app's packages come to about 360 MB, so a size error
means packages were added. The first visit after a quiet period takes a few seconds while Vercel starts the app.

Vercel's free Hobby plan is meant for personal, non-commercial projects.

---

## Storing data in Supabase

By default the accounts and the bot's journal are SQLite files in `backend/data`. To keep them in
[Supabase](https://supabase.com) (a hosted PostgreSQL database with a free plan) instead:

1. **Create a project** at supabase.com. Write down the **database password** you choose; if you lost it,
   reset it in the project's Database settings.
2. **Copy the connection string:** in the project, click **Connect** at the top and choose **Session pooler**
   (it works on every network). It looks like
   `postgresql://postgres.abcdefghijkl:[YOUR-PASSWORD]@aws-0-us-east-1.pooler.supabase.com:5432/postgres`.
   Replace `[YOUR-PASSWORD]` with your database password, exactly as it is: symbols such as `@ # ? & /`
   are fine, the app encodes them for you.
   Don't use the **Transaction pooler** (port 6543); the app refuses it with an explanation.
3. **Add it to `backend/.env`:**
   ```
   DATABASE_URL=postgresql://postgres.abcdefghijkl:your-password@aws-0-us-east-1.pooler.supabase.com:5432/postgres
   ```
4. **Install the driver** (once): `pip install -r requirements.txt` in the backend folder.
5. **Check the connection:** `python -m algotrader db status`. The first connection creates the tables.
6. **Optional, bring your local data along:** `python -m algotrader db copy-from-sqlite` copies your local
   accounts, purchases and bot history into Supabase. It only fills an empty database and never overwrites.
   Saved Alpaca keys stay readable only with the same `SECRET_KEY` (or the same `backend/data/secret.key`).
7. Restart `serve` and `bot`. `python -m algotrader doctor` now shows `Database: Supabase database ...: connected`.

**What Supabase keeps safe for you, and what it doesn't:**

- The tables live in their own `algotrader` schema. Supabase's automatic REST API only exposes the `public`
  schema, and on top of that the app switches on row-level security for every table and removes any access
  for Supabase's `anon` and `authenticated` roles, so nobody can reach this data with the project's public API
  key. If you don't use Supabase's API for anything else you can also turn the Data API off in the project's
  API settings.
- The connection is always encrypted (`sslmode=require` is added unless you set your own `sslmode`).
- The `DATABASE_URL` contains your database password: keep `backend/.env` private, and on a hosting platform
  put it in the environment settings, never in the code.
- `SECRET_KEY` (or `secret.key`) still lives with the app, not in Supabase. That's on purpose: someone who
  got into the database still couldn't read anyone's Alpaca keys.

**On a server:** set `DATABASE_URL` and `SECRET_KEY` in the server's environment. The accounts and journal no
longer need the server's disk. The trained model and the price cache still do; on a platform without a
persistent disk, also set `TRAIN_ON_START=true` so the container trains a fresh model each time it starts
(a few minutes), or keep a small disk for `/data` as described in Option C.

---

## Hosting the dashboard separately (optional)

The Flask app serves the dashboard itself, which is the simplest setup. If you'd rather put the React app
on Vercel/Netlify:

```bash
cd frontend
API_BASE=https://trader.yourdomain.com npm run build      # upload frontend/dist
```
and on the backend set `CORS_ORIGINS=https://your-dashboard.vercel.app`. User accounts need the dashboard
and the API on the same address (the log-in cookie isn't sent across sites), so with a separate origin set
`USER_ACCOUNTS=false` and use your host's access protection instead.

---

## Operating it

| Task | How |
|---|---|
| See what it's doing | Dashboard → *Bot activity*; or `docker compose logs -f` |
| Pause trading, keep signals | Set `TRADING_ENABLED=false`, restart (`docker compose up -d`) |
| Turn off the dashboard's buy/sell buttons | Set `MANUAL_TRADING=false`, restart |
| Buy a stock yourself | Search it (or click it), enter an amount, review, confirm. The bot leaves it alone. |
| Stop everything | `docker compose down` (open positions keep their stop-loss/take-profit at Alpaca) |
| Close all positions | Alpaca dashboard → Positions → Close all |
| Run a cycle by hand | `docker compose exec algotrader python -m algotrader trade` |
| Check what it would do today | `... python -m algotrader trade --dry-run --force` |
| Retrain now | `... python -m algotrader train --refresh` |
| Check every data source | `... python -m algotrader doctor` |
| See where the data is stored | `... python -m algotrader db status` |
| See every account | `... python -m algotrader users list` |
| Someone forgot their password | `... python -m algotrader users reset-password them@example.com` (asks for the new one) |
| Make someone admin / remove it | `... python -m algotrader users make-admin` / `remove-admin them@example.com` |
| Delete an account | `... python -m algotrader users delete them@example.com` (or they do it in their profile) |
| Stop new sign-ups | `ALLOW_SIGNUPS=false`, restart |
| See why a buy was skipped | Dashboard → *Model signals* (for example "recent news is mostly negative (4 of 5 headlines in the last few days)") |

---

## Making changes

| I want to… | Change this | Then |
|---|---|---|
| Trade different stocks | `SYMBOLS` in `backend/.env` | `train --refresh` |
| Buy more / less eagerly | `ENTRY_THRESHOLD`, `EXIT_THRESHOLD` | `backtest` (seconds) to compare |
| Change risk | `STOP_LOSS_PCT`, `TAKE_PROFIT_PCT`, `POSITION_SIZE_PCT`, `MAX_POSITIONS`, `MAX_HOLDING_DAYS` | `backtest` |
| Change the prediction target | `HORIZON_DAYS`, `LABEL_THRESHOLD` | `train` |
| Tune the forest | `N_ESTIMATORS`, `MAX_DEPTH`, `MIN_SAMPLES_LEAF` | `train` |
| Add a feature | `features.py`: compute it in `stock_features()` and add its name to `STOCK_FEATURES` | tests, then `train` |
| Try another model | `model.py` → `make_model()` (anything with `predict_proba`, e.g. `GradientBoostingClassifier`) | tests, then `train` |
| Change a trading rule | `strategy.py` → `decide()`. Backtest and live both use it, so they can't drift apart. | add a test in `tests/test_strategy.py` |
| Switch market conditions off | `USE_MARKET_CONTEXT=false` | `train` |
| Get FRED data through its official API | `FRED_API_KEY=` (free key from fred.stlouisfed.org) | `doctor` |
| Make the news check stricter / looser / off | `NEWS_BLOCK_SCORE` (−1..1), `NEWS_MIN_ARTICLES`, `NEWS_LOOKBACK_DAYS`, `NEWS_CHECK=false` | restart `bot` |
| Tell the bot a new stock's sector | `SECTORS=KO:XLP,BA:XLI` | `train` |
| Add a FRED series | `context.py`: add it to `FRED_SERIES`, derive a feature in `macro_features()`, list it in `MACRO_FEATURES` | tests, then `train --refresh` |
| Make log-ins last longer / shorter | `SESSION_DAYS` | restart `serve` |
| Turn accounts off (just you, no log-in) | `USER_ACCOUNTS=false` | restart `serve` |
| Change the dashboard | `frontend/src/` (components in `components/`, colours in the `:root` block of `styles.css`) | `npm run dev` + reload, `npm test`, `npm run build` |
| Add an API field | `backend/algotrader/api.py` | add to `tests/test_api.py` |

**A safe workflow for any change:**

1. Change one thing.
2. Run the tests (`python -m unittest discover -s tests -t .` and `npm test`).
3. `python -m algotrader backtest` (strategy change) or `train` (model/feature change) and compare
   the out-of-sample numbers with before. Be suspicious of big improvements: tuning many settings
   against the same test period overfits it.
4. Let it paper trade for a few weeks before trusting it.
5. Deploy: `git commit`, `git push`, then on the server `git pull && docker compose up -d --build`.

**Dashboard development loop:** run `python -m algotrader serve` (or `demo`) in one terminal and
`npm run dev` in another (rebuilds on every save), then refresh the browser.

---

## Real money (read this first)

Paper trading fills are optimistic, and a strategy that looks good on paper can lose money live.
If you still decide to:

1. Run on paper for at least a couple of months and compare the live paper results with the backtest.
2. Use live keys from your Alpaca live account, start with a small amount, and set:
   ```
   ALPACA_PAPER=false
   LIVE_TRADING_CONFIRM=I_UNDERSTAND_REAL_MONEY
   ```
   Without the confirmation line the bot refuses to start.
3. Watch it daily. `TRADING_ENABLED=false` is your off switch.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `check` says 401/403 | Keys are wrong, or you used live keys with `ALPACA_PAPER=true` (or vice versa). Paper and live keys differ. |
| `subscription does not permit querying recent SIP data` | Keep `ALPACA_DATA_FEED=iex`, or only use `sip` for data older than 15 minutes. |
| Dashboard shows "Connect your Alpaca paper account" | The API process can't see the keys: check `backend/.env` / the platform's env vars, then restart. |
| Dashboard says "Updating every 10s" instead of "Live" | A proxy is buffering the event stream; add `flush_interval -1` (Caddy) or `proxy_buffering off;` (nginx). It still works via polling. |
| Bot ran but "Skipped" | Not a trading day, or the market was still open (it runs after the close). |
| No buys for days | Normal when SPY is below its 50-day average (market filter) or no probability reaches `ENTRY_THRESHOLD`. The *Model signals* panel shows why for each stock. |
| `Model was trained with scikit-learn X but Y is installed` | Retrain: `python -m algotrader train`. |
| Banner "Restart the server to finish the update", or a stock chart says "The server is still running older code" | Press Ctrl+C in the window running `python -m algotrader serve`, run it again, reload the page. |
| `doctor` says the dashboard files are version `__VERSION__`, or were "rebuilt by an old copy of the build script" | A window running `npm run dev` from the old version was still open while the code changed. Close it (it shows "Watching src/"), then run `npm run build` in `frontend`. |
| A stock chart says "Couldn't load the chart" | The message says what Alpaca answered. Click *Try again*; if it repeats, run `python -m algotrader doctor`. |
| *Market today* says FRED isn't available | FRED (St. Louis Fed) couldn't be reached. Prices still work; the bot uses the last saved FRED data for up to a few days. `doctor` shows the exact error. |
| `train` says "market conditions … not used" | They didn't beat the price-only model in the walk-forward test, so the bot uses prices only. That's the safety rule working, not an error. |
| `serve` or `doctor` says the 'cryptography' package is missing | In the backend folder, with the virtual environment active: `pip install -r requirements.txt` |
| "Alpaca didn't accept your saved keys" for one person | They regenerated their keys at Alpaca. Profile → Alpaca account → Use different keys. |
| Everyone's Alpaca connection stopped working after moving servers | `backend/data/secret.key` (or `SECRET_KEY`) wasn't copied. Copy it back, or everyone reconnects in their profile. |
| Nobody sees the bot's account | No admin yet: `python -m algotrader users make-admin you@example.com` |
| `Can't connect to the database (password authentication failed ...)` | The password in `DATABASE_URL` is wrong. It's the database password, not your Supabase login. |
| `Can't connect to the database (... Network is unreachable)` | You copied the direct connection string on an IPv4-only network. Use the **Session pooler** string. |
| `Can't connect to the database (... Tenant or user not found)` | With the pooler the user name is `postgres.yourprojectref`, not just `postgres`. Copy the string again from **Connect**. |
| Data disappeared after switching `DATABASE_URL` on | The app now reads Supabase, which starts empty. Copy the local data across: `python -m algotrader db copy-from-sqlite`. |
| "Too many wrong passwords" | Wait 15 minutes, or reset it: `python -m algotrader users reset-password them@example.com` |
| Vercel shows "This site isn't set up yet" | Add the settings it lists (Settings → Environments → Production), then `vercel --prod` and reload with Ctrl+F5. If it lists all four, you're looking at a deployment made before the settings existed. |
| Vercel: "The name of your Environment Variable contains invalid characters" | A hidden space in the Key. Type it by hand, or use `vercel env add NAME production`. |
| Vercel: "A variable with the name ... already exists" | It's already saved; skip it (`vercel env ls` lists them). |
| Vercel: `No existing credentials found` | `vercel login` wasn't finished in the browser. Run it again. |
| A private window shows a Vercel log-in page instead of AlgoTrader | Settings → Deployment Protection → Standard Protection (or off). |
| Vercel bot run fails after a redeploy and the logs mention scikit-learn versions | Vercel installed a newer scikit-learn than the one that trained the model. On your computer: `pip install -U -r requirements.txt`, `python -m algotrader train`, then `vercel --prod`. |
| Supabase says the project is paused | Free projects pause after 7 days of low activity. Resume it in the Supabase dashboard. |
| Vercel cron run returns "Not authorized" | `CRON_SECRET` was changed or added after the last deploy: run `vercel --prod` again. |
| Vercel: Model signals empty, cron run says there's no model | `backend\models` was empty when you deployed. Run `python -m algotrader train`, then `vercel --prod`. |
| Bot run fails with "the model needs market data from FRED" | The model was trained with market conditions but FRED and the saved copy are both unavailable. Retry later, or set `USE_MARKET_CONTEXT=false` and `train`. |
