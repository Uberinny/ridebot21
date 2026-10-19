# Partner ride bot

Discord bot for **your** server: customers book rides with wallet credits; **you** (the server owner) manage the VCC card pool. Talks to our Ride API with your partner key.

You do **not** need to edit a `.env` file by hand.

---

## Quick start (Windows)

1. Install [Python 3.11+](https://www.python.org/downloads/) — tick **Add python.exe to PATH**.
2. Double-click **`start.bat`**.
3. A setup page opens in your browser. Paste:
   - **Discord bot token** (from the [Discord Developer Portal](https://discord.com/developers/applications) → your app → Bot)
   - **API key ID** + **API secret** (we give you these — `pk_…`)
4. Click **Test Discord token** and **Test ride API**, then **Save & continue**.
5. Leave the black window open — the bot stays online while it runs.

Invite the bot to your server with scopes **`bot`** + **`applications.commands`**, then in Discord run **`/dashboard`** (admins only).

---

## Quick start (Mac / Linux)

```bash
chmod +x start.sh
./start.sh
```

Same browser setup as Windows.

---

## Useful commands

| Command | What it does |
|--------|----------------|
| `start.bat` / `./start.sh` | Install deps, open setup if needed, start bot |
| `python run.py` | Same as above (after venv is active) |
| `python run.py --setup` | Re-open the setup page to change keys |
| `python run.py --check` | Print whether settings are complete |

Settings are saved in a local **`.env`** file on your PC (never commit it).

---

## Deploy on Railway (24/7)

For production, host the bot on Railway so it stays online without your PC.

**Full step-by-step:** see **[DEPLOY_RAILWAY.md](DEPLOY_RAILWAY.md)**

Short version:

1. Push this folder to a **private** GitHub repo (no `.env`).
2. Railway → **New Project** → Deploy from GitHub.
3. **Variables:** `DISCORD_TOKEN`, `API_KEY_ID`, `API_SECRET`, `API_BASE_URL`, `GUILD_ID`, `DASHBOARD_PASSWORD` (10+ chars).
4. **Settings → Networking → Generate Domain**.
5. **Volumes → Add Volume** mounted at `/data` (keeps wallets after redeploy).
6. Open `https://YOUR_DOMAIN/health` — should return `ok`.
7. In Discord: `/dashboard` → set channels → **Add Card**.

On Railway the bot binds `0.0.0.0:$PORT` automatically. Do **not** use the browser setup wizard there — use Variables.

---

## After the bot is online

- **`/dashboard`** — channels, fees, wallets, **Add Card** (your VCC pool)
- **`/adminhelp`** — short admin guide
- Web dashboard: local `http://127.0.0.1:8080` or your Railway `https://YOUR_DOMAIN/` (password from setup / Variables)

Customers never see or pick cards. The bot picks from **your** pool and rotates on decline.

---

## What you need from us

1. Partner **API key ID** (`pk_…`)
2. Partner **API secret** (shown once)
3. Default API URL is already filled in (change only if we tell you)

---

## Troubleshooting

| Problem | Fix |
|--------|-----|
| Setup page doesn’t open | Open `http://127.0.0.1:8765/` yourself, or set `SETUP_PORT=8877` |
| `invalid_signature` on API test | Confirm secret/key ID; ask us if `API_SIGN_ORDER` must change |
| Slash commands missing | Set **Server ID** / `GUILD_ID`, restart; wait a minute |
| Dashboard disabled | Password must be **10+ characters** and not `change-me` |
| Railway healthcheck fail | Generate a domain; logs should show web on `0.0.0.0` — see DEPLOY_RAILWAY.md |
| Balances reset on Railway | Add a Volume at `/data` |
