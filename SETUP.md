# Deploying AMIA (free tier, runs on its own)

Everything runs on GitHub Actions on a schedule: weekdays at 22:30 UTC, after the US close, plus a monthly post-mortem job. Data lives in Supabase, and the dashboard is a GitHub Pages site. Nothing has to stay on at home.

Keys never go in the code or in chat. You add each one as an encrypted GitHub secret.

## 1. Accounts (all free)

| Service | What to get | Becomes secret |
|---|---|---|
| **Supabase** (supabase.com) | Create a project and choose a region near `us-east`. Save the database password you set. | `DATABASE_URL` |
| **Alpaca** (alpaca.markets) | Sign up and open the **Paper Trading** dashboard. Under API Keys, click Generate. | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` |
| **FRED** (fred.stlouisfed.org) | My Account → API Keys → Request API Key | `FRED_API_KEY` |
| **OpenRouter** (openrouter.ai) | Sign up, buy about $5 of credits, then create an API key under **Keys** and give it a $10 credit limit | `OPENROUTER_API_KEY` |
| **SEC EDGAR** | No account. The SEC requires a contact email in each request. | `SEC_CONTACT_EMAIL` |

### The Supabase connection string

In Supabase, click **Connect** at the top of the project and choose **Session pooler**. Don't use "Direct connection": that one is IPv6-only, and GitHub's runners can't reach it.

Copy the URI, replace `[YOUR-PASSWORD]` with your database password, and add `?sslmode=require` at the end. It looks like this:

```
postgresql://postgres.abcdefghijkl:YOUR-PASSWORD@aws-0-us-east-1.pooler.supabase.com:5432/postgres?sslmode=require
```

## 2. Add the secrets

In the GitHub repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Add all six names from the table above.

Or, from this folder, run one command per secret. Each one prompts for the value, so it never appears in your shell history:

```bash
gh secret set DATABASE_URL
```

```bash
gh secret set ALPACA_API_KEY_ID
```

```bash
gh secret set ALPACA_API_SECRET_KEY
```

```bash
gh secret set FRED_API_KEY
```

```bash
gh secret set OPENROUTER_API_KEY
```

```bash
gh secret set SEC_CONTACT_EMAIL
```

Optional: to change the $10 monthly LLM cap, add a repository **variable** (not a secret) named `AMIA_LLM_MONTHLY_CAP_USD`.

## 3. Turn on Pages

Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.

## 4. First run

1. Under **Actions → backfill → Run workflow**, run it once. It loads the SEC ticker map, 3 years of macro data and benchmark prices, then scores the last 7 days of news so there's a watchlist from day one. Scoring runs 8 calls at a time, so this takes about 15–40 minutes and costs well under $1.
2. Under **Actions → daily → Run workflow**, leave the date empty. The first run fetches prices and SEC fundamentals for the watchlist names, so it takes longer than later runs. When it finishes, the dashboard URL appears on the `deploy` job and under Settings → Pages.
3. After that it runs by itself every weekday.

## What to expect

- **Alerts** arrive as GitHub issues labelled `amia-alert`, and GitHub emails them to you. Failed workflow runs are emailed too.
- **A watchlist that moves.** It follows the news: names enter when they get material, novel coverage and fade out over about a week when the story goes quiet.
- **Buys only above the confidence bar.** The model must reach 0.60 confidence of beating SPY over six months, plus 0.05 for each regime or crowding flag, and at most 3 new positions a day. Blocked buys appear on the dashboard with the reason.
- **Lessons take time.** Post-mortems start once trades close or reach six months. A lesson needs at least 3 agreeing post-mortems in a sector.
- **Cost:** about $6/month in OpenRouter usage (Qwen models), with a hard $10 cap. Everything else is free tier.

## If something breaks

| Symptom | Likely cause |
|---|---|
| `subscription does not permit querying recent SIP data` | Alpaca's free-plan terms changed. Switching to a paid price feed is a small change in `amia/data/live.py`. |
| News fetch failed alert | Alpaca news may no longer be free on the Basic plan. Scoring continues with filings only. |
| `could not translate host name` / connection timeout | `DATABASE_URL` is the direct connection. Use the Session pooler URI. |
| Empty watchlist | No scored news yet: check the backfill ran, and that the news feed isn't failing (an alert says so). |
| "LLM budget cap reached" alert | The $10 cap stopped scoring or reviews for the month. Raise `AMIA_LLM_MONTHLY_CAP_USD` or wait for the 1st. |
| No runs for 60+ days | GitHub pauses schedules on inactive public repos. The monthly job commits a heartbeat to prevent this. If they're paused anyway, re-enable them under Actions. |
