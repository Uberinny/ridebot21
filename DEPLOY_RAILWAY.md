# Deploy the partner ride bot on Railway

This guide is for **server owners** who want the Discord bot online 24/7 without keeping a PC on.

Local setup (`start.bat`) is still fine for testing. Production → **Railway**.

---

## What you need before you start

| Item | Where from |
|------|------------|
| Discord **bot token** | [Discord Developer Portal](https://discord.com/developers/applications) → your app → **Bot** → Reset/Copy token |
| Bot invited to your server | OAuth2 URL Generator → scopes **`bot`** + **`applications.commands`** |
| **API_KEY_ID** (`pk_…`) | We create this for your server |
| **API_SECRET** | Shown once when the key is created — keep it private |
| Railway account | [railway.app](https://railway.app) |

Optional later: Stripe / OxaPay keys for automatic Add Funds.

---

## 1. Put the bot code on GitHub

1. Create a **private** GitHub repo.
2. Upload the contents of the `ride_bot` folder (this project), **not** your `.env` file.
3. Confirm these files are in the repo root you will deploy:
   - `run.py`, `bot.py`, `requirements.txt`
   - `railway.toml`, `Procfile`

> Never commit `.env` or real tokens.

---

## 2. Create the Railway project

1. Open [railway.app](https://railway.app) → **New Project**.
2. **Deploy from GitHub repo** → select the repo.
3. If Railway asks for the root directory and your bot lives in a subfolder, set **Root Directory** to that folder (the one that contains `run.py`).
4. Wait for the first build (it may fail until variables are set — that is OK).

---

## 3. Add Variables (required)

Railway → your service → **Variables** → add:

### Required

| Variable | Example / notes |
|----------|------------------|
| `DISCORD_TOKEN` | Your Discord bot token |
| `API_KEY_ID` | `pk_xxxxxxxxxxxx` |
| `API_SECRET` | Your partner API secret |
| `API_BASE_URL` | `https://empower-production-3f06.up.railway.app` (change only if we say so) |
| `DASHBOARD_PASSWORD` | Strong password, **10+ characters** (not `change-me`) |
| `GUILD_ID` | Your Discord **server ID** (slash commands appear faster) |

### Set automatically on Railway (you can leave blank)

| Variable | What happens |
|----------|----------------|
| `PORT` | Railway sets this — do not override |
| `DASHBOARD_HOST` | Defaults to `0.0.0.0` on Railway |
| `PUBLIC_BASE_URL` | Filled from your Railway public domain when blank |
| `DB_PATH` | Uses `/data/bot.db` if you attach a volume at `/data` |

### Optional (payments)

| Variable | Notes |
|----------|--------|
| `STRIPE_SECRET_KEY` | `sk_live_…` or `sk_test_…` |
| `STRIPE_WEBHOOK_SECRET` | `whsec_…` — endpoint: `https://YOUR_DOMAIN/stripe/webhook` |
| `OXAPAY_MERCHANT_API_KEY` | Crypto top-ups |
| `OXAPAY_SANDBOX` | Must be `false` in production |
| `OPERATOR_MEMBER_ID` | Leave empty (= `operator`) unless we tell you otherwise |

Then click **Deploy** / **Redeploy**.

---

## 4. Turn on a public URL

1. Service → **Settings** → **Networking** → **Generate Domain**  
   (or attach your own domain).
2. Copy the URL, e.g. `https://your-bot-production.up.railway.app`.
3. Optional: set Variable `PUBLIC_BASE_URL` to that exact URL (https, no trailing slash).  
   If you skip this, the bot usually picks it up from `RAILWAY_PUBLIC_DOMAIN` automatically.

Health check (after deploy): open  
`https://YOUR_DOMAIN/health`  
→ should show `{"ok":true,"service":"ride-bot"}`.

---

## 5. Keep wallets / rides after redeploys (volume)

SQLite is the bot database. Without a volume, **redeploy wipes balances**.

1. Service → **Settings** → **Volumes** → **Add Volume**.
2. Mount path: **`/data`**
3. Redeploy.
4. Confirm logs say something like `Database: /data/bot.db`.

---

## 6. Stripe / OxaPay webhooks (if you use auto top-up)

### Stripe

1. Stripe Dashboard → Developers → Webhooks → Add endpoint  
   `https://YOUR_DOMAIN/stripe/webhook`
2. Events: Checkout session completed (and related payment events you use).
3. Copy **Signing secret** → Railway Variable `STRIPE_WEBHOOK_SECRET`.

### OxaPay

Set callback / IPN to:  
`https://YOUR_DOMAIN/oxapay/webhook`  
(and `OXAPAY_MERCHANT_API_KEY` in Variables).

The bot also **polls** payments every 20s, so webhooks help speed but are not the only path.

---

## 7. Check that the bot is alive

1. Railway → **Deployments** → latest → **Logs**  
   Look for: `Logged in as YourBot#…` and `Web server on http://0.0.0.0:…`
2. In Discord: **`/dashboard`** (server admin) → set Book Ride / Add Funds channels.
3. **`/adminhelp`** for the short admin guide.
4. Web dashboard (optional): `https://YOUR_DOMAIN/` with `DASHBOARD_PASSWORD`.

---

## 8. Updating the bot later

1. Push code to GitHub.
2. Railway redeploys automatically (if connected).
3. Or **Deployments → Redeploy**.
4. Variables can be changed anytime → Redeploy for them to apply.

---

## Troubleshooting

| Problem | Fix |
|--------|-----|
| Build OK but bot exits | Missing `DISCORD_TOKEN` / `API_KEY_ID` / `API_SECRET` — check Variables |
| Healthcheck failed | Confirm public domain + logs show web on `0.0.0.0` and `/health` works |
| `invalid_signature` | Wrong `API_SECRET` or `API_KEY_ID`; ask us if `API_SIGN_ORDER` must change |
| Slash commands missing | Set `GUILD_ID`, redeploy, wait 1–2 minutes |
| Balances reset after deploy | Add Volume mounted at `/data` |
| Dashboard 404 / disabled | `DASHBOARD_PASSWORD` too short or is `change-me` |
| Setup wizard mentioned in logs | That is for local PCs only — on Railway use Variables, not `start.bat` |

---

## Local vs Railway (quick)

| | Your PC (`start.bat`) | Railway |
|--|----------------------|---------|
| Config | Browser setup page → `.env` | Project **Variables** |
| Keep online | Window must stay open | Always on |
| Public URL | Usually none | Generate Domain |
| Database | `bot.db` in folder | Volume at `/data` |

---

## Minimal variable checklist (copy/paste)

```
DISCORD_TOKEN=
API_KEY_ID=
API_SECRET=
API_BASE_URL=https://empower-production-3f06.up.railway.app
GUILD_ID=
DASHBOARD_PASSWORD=
OXAPAY_SANDBOX=false
```

After the first successful deploy, generate a domain, add a `/data` volume, then invite the bot and run `/dashboard` in Discord.
