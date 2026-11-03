# Deploying AMIA (free tier, runs on its own)

Everything runs on GitHub Actions on a schedule: weekdays at 22:30 UTC, after the US close. Data lives in Supabase, and the dashboard is a GitHub Pages site. Nothing has to stay on at home.

Keys never go in the code or in chat. You add each one as an encrypted GitHub secret.

## 1. Accounts (all free)

| Service | What to get | Becomes secret |
|---|---|---|
| **Supabase** (supabase.com) | Create a project and choose a region near `us-east`. Save the database password you set. | `DATABASE_URL` |
| **Alpaca** (alpaca.markets) | Sign up and open the **Paper Trading** dashboard. Under API Keys, click Generate. | `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` |
| **FRED** (fred.stlouisfed.org) | My Account → API Keys → Request API Key | `FRED_API_KEY` |
| **Anthropic** (console.anthropic.com) | Create an API key, add about $5 of credit, and set a monthly spend limit of $10 under Limits | `ANTHROPIC_API_KEY` |
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
gh secret set ANTHROPIC_API_KEY
```

```bash
gh secret set SEC_CONTACT_EMAIL
```

Optional: to change the $10 monthly LLM cap, add a repository **variable** (not a secret) named `AMIA_LLM_MONTHLY_CAP_USD`.

## 3. Turn on Pages

Go to **Settings → Pages → Build and deployment → Source** and choose **GitHub Actions**.

## 4. First run

1. Under **Actions → backfill → Run workflow**, run it once. It loads index membership, about 20 months of prices, macro data and EDGAR fundamentals for about 1,500 companies. That takes roughly 30–60 minutes. If its output says companies are "still waiting", run it again.
2. Under **Actions → daily → Run workflow**, leave the date empty. When it finishes, the dashboard URL appears on the `deploy` job and under Settings → Pages.
3. After that it runs by itself every weekday.

## What to expect

- **Alerts** arrive as GitHub issues labelled `amia-alert`, and GitHub emails them to you. Failed workflow runs are emailed too.
- **Cash at first.** The portfolio starts in cash. Few large US companies pass Graham's defensive screen with a 25% margin of safety, so it can stay mostly in cash for a long time. That's the framework working, not a bug.
- **Learning stays locked** until about 6 months of 126-day predictions have resolved.
- **Cost:** about $2–5/month in Anthropic usage. Everything else is free tier.

## If something breaks

| Symptom | Likely cause |
|---|---|
| `subscription does not permit querying recent SIP data` | Alpaca's free-plan terms changed. Switching to a paid price feed is a small change in `amia/data/live.py`. |
| News fetch failed alert | Alpaca news may no longer be free on the Basic plan. Scoring continues with filings only. |
| `could not translate host name` / connection timeout | `DATABASE_URL` is the direct connection. Use the Session pooler URI. |
| Universe refresh failed alert | A Wikipedia table layout changed. The previous month's membership stays in use. |
| No runs for 60+ days | GitHub pauses schedules on inactive public repos. The monthly job commits a heartbeat to prevent this. If they're paused anyway, re-enable them under Actions. |
