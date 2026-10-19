#!/usr/bin/env python3
"""Local browser setup for the partner ride bot — no .env editing required.

Opens http://127.0.0.1:8765 so the server owner can paste Discord + API keys,
test the connection, and save settings. Used by run.py / start.bat.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import threading
import webbrowser
from typing import Any

from aiohttp import ClientSession, web

import config
from dashboard import password_problem

log = logging.getLogger("setup")
HOST = "127.0.0.1"
PORT = int(os.getenv("SETUP_PORT") or 8765)

# One-time token so only the machine that opened the wizard can save.
_CSRF = secrets.token_urlsafe(24)
_DONE = threading.Event()
_SAVED_OK = False

STYLE = """
:root {
  --bg0: #0c1222;
  --bg1: #121a2b;
  --card: #172033;
  --line: #2a3548;
  --text: #e8eef8;
  --muted: #8b9bb4;
  --acc: #4f8cff;
  --acc2: #2dd4a8;
  --err: #ff6b7a;
  --ok: #3dd68c;
  --warn: #f0b429;
  --radius: 14px;
  --font: "Segoe UI", system-ui, sans-serif;
}
* { box-sizing: border-box; }
body {
  margin: 0; min-height: 100vh; color: var(--text); font-family: var(--font);
  background:
    radial-gradient(900px 500px at 10% -10%, rgba(79,140,255,.22), transparent 55%),
    radial-gradient(700px 400px at 90% 0%, rgba(45,212,168,.12), transparent 50%),
    linear-gradient(180deg, var(--bg0), var(--bg1));
}
.wrap { max-width: 760px; margin: 0 auto; padding: 40px 20px 80px; }
header h1 { margin: 0 0 8px; font-size: 1.85rem; letter-spacing: -0.02em; }
header p { margin: 0 0 28px; color: var(--muted); line-height: 1.5; }
.card {
  background: color-mix(in srgb, var(--card) 92%, transparent);
  border: 1px solid var(--line); border-radius: var(--radius);
  padding: 22px 22px 8px; margin-bottom: 18px;
  box-shadow: 0 16px 40px rgba(0,0,0,.25);
}
.card h2 {
  margin: 0 0 6px; font-size: 1.05rem; display: flex; align-items: center; gap: 10px;
}
.card h2 .n {
  width: 26px; height: 26px; border-radius: 8px; background: rgba(79,140,255,.18);
  color: var(--acc); display: grid; place-items: center; font-size: 13px; font-weight: 700;
}
.card .hint { color: var(--muted); font-size: 13px; margin: 0 0 16px; line-height: 1.45; }
.field { margin-bottom: 14px; }
label { display: block; font-size: 13px; font-weight: 600; margin-bottom: 6px; }
.help { color: var(--muted); font-size: 12px; margin: 5px 0 0; line-height: 1.4; }
input[type=text], input[type=password], input[type=url] {
  width: 100%; padding: 11px 12px; border-radius: 10px;
  border: 1px solid var(--line); background: #0e1524; color: var(--text);
  font-size: 14px; outline: none;
}
input:focus { border-color: var(--acc); box-shadow: 0 0 0 3px rgba(79,140,255,.18); }
.row-actions { display: flex; flex-wrap: wrap; gap: 10px; margin: 8px 0 18px; }
button, .btn {
  appearance: none; border: 0; border-radius: 10px; padding: 11px 16px;
  font-weight: 650; font-size: 14px; cursor: pointer; color: #061018;
  background: linear-gradient(180deg, #6aa1ff, var(--acc));
}
button.secondary { background: #243047; color: var(--text); border: 1px solid var(--line); }
button:disabled { opacity: .55; cursor: wait; }
#status {
  min-height: 22px; font-size: 13px; margin: 4px 0 12px; white-space: pre-wrap;
}
#status.ok { color: var(--ok); } #status.err { color: var(--err); } #status.warn { color: var(--warn); }
details.advanced { margin: 8px 0 16px; color: var(--muted); font-size: 13px; }
details.advanced summary { cursor: pointer; margin-bottom: 10px; }
.foot { color: var(--muted); font-size: 12px; margin-top: 10px; }
a { color: #8cbcff; }
"""


def _page_html() -> str:
    values = config.current_values(mask_secrets=True)
    sections: dict[str, list[dict[str, Any]]] = {}
    for f in config.FIELDS:
        if f.get("advanced"):
            continue
        sections.setdefault(f["section"], []).append(f)
    advanced = [f for f in config.FIELDS if f.get("advanced")]

    def field_html(f: dict[str, Any]) -> str:
        key = f["key"]
        val = values.get(key, f.get("default") or "")
        typ = "password" if f.get("secret") else ("url" if "URL" in key else "text")
        req = " required" if f.get("required") else ""
        ph = f.get("placeholder") or ""
        help_ = f.get("help") or ""
        return f"""
        <div class="field">
          <label for="{key}">{f['label']}{" *" if f.get("required") else ""}</label>
          <input id="{key}" name="{key}" type="{typ}" value="{_esc(val)}"
                 placeholder="{_esc(ph)}"{req} autocomplete="off" spellcheck="false">
          {f'<p class="help">{_esc(help_)}</p>' if help_ else ""}
        </div>"""

    blocks = []
    step = 1
    intros = {
        "Discord": (
            "Create a bot at the "
            "<a href='https://discord.com/developers/applications' target='_blank' rel='noreferrer'>"
            "Discord Developer Portal</a>, enable Message Content if needed, invite it to your server "
            "with <b>applications.commands</b> + <b>bot</b> permissions, then paste the token below."
        ),
        "Ride API": (
            "These come from us (Partner API key). Paste the <b>key ID</b> and <b>secret</b> we gave you, "
            "then press <b>Test ride API</b>."
        ),
        "Dashboard": (
            "Optional password for the local admin web page (wallets / stats) after the bot is running."
        ),
        "Payments": (
            "Optional. Leave blank to use manual Add Funds only. You can add Stripe / OxaPay later."
        ),
    }
    for section, items in sections.items():
        fields = "".join(field_html(f) for f in items)
        extra = ""
        if section == "Ride API":
            extra = '<div class="row-actions"><button type="button" class="secondary" id="btn-test-api">Test ride API</button></div>'
        if section == "Discord":
            extra = '<div class="row-actions"><button type="button" class="secondary" id="btn-test-discord">Test Discord token</button></div>'
        blocks.append(f"""
        <section class="card">
          <h2><span class="n">{step}</span>{_esc(section)}</h2>
          <p class="hint">{intros.get(section, "")}</p>
          {fields}{extra}
        </section>""")
        step += 1

    adv_fields = "".join(field_html(f) for f in advanced)
    return f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ride bot setup</title>
<style>{STYLE}</style>
</head><body>
<div class="wrap">
  <header>
    <h1>Set up your ride bot</h1>
    <p>Fill in the fields below — no need to edit a <code>.env</code> file.
      When you save, this window can close and the bot will start.</p>
  </header>
  <form id="form">
    <input type="hidden" name="csrf" value="{_CSRF}">
    {"".join(blocks)}
    <details class="advanced card"><summary>Advanced settings</summary>{adv_fields}</details>
    <div id="status"></div>
    <div class="row-actions">
      <button type="submit" id="btn-save">Save &amp; continue</button>
      <button type="button" class="secondary" id="btn-save-only">Save only</button>
    </div>
    <p class="foot">Settings are stored only on this computer in <code>.env</code> (never uploaded to Discord).
      Re-run setup anytime with <code>python run.py --setup</code>.</p>
  </form>
</div>
<script>
const statusEl = document.getElementById('status');
function setStatus(kind, msg) {{
  statusEl.className = kind || '';
  statusEl.textContent = msg || '';
}}
async function post(url, body) {{
  const r = await fetch(url, {{
    method: 'POST',
    headers: {{ 'Content-Type': 'application/json', 'X-CSRF': '{_CSRF}' }},
    body: JSON.stringify(body || {{}}),
  }});
  const j = await r.json().catch(() => ({{ ok: false, message: 'Bad response' }}));
  if (!r.ok || !j.ok) throw new Error(j.message || ('HTTP ' + r.status));
  return j;
}}
function formValues() {{
  const fd = new FormData(document.getElementById('form'));
  const out = {{}};
  for (const [k, v] of fd.entries()) out[k] = String(v);
  return out;
}}
document.getElementById('btn-test-discord').onclick = async () => {{
  setStatus('warn', 'Checking Discord token…');
  try {{
    const j = await post('/api/test-discord', formValues());
    setStatus('ok', 'Discord OK — bot user: ' + (j.username || '') + (j.id ? ' (' + j.id + ')' : ''));
  }} catch (e) {{ setStatus('err', e.message); }}
}};
document.getElementById('btn-test-api').onclick = async () => {{
  setStatus('warn', 'Checking ride API…');
  try {{
    const j = await post('/api/test-api', formValues());
    setStatus('ok', 'Ride API OK — ' + (j.caller || 'signed in'));
  }} catch (e) {{ setStatus('err', e.message); }}
}};
async function save(startBot) {{
  setStatus('warn', 'Saving…');
  try {{
    const j = await post('/api/save', {{ ...formValues(), start: !!startBot }});
    setStatus('ok', j.message || 'Saved.');
    if (j.done) setTimeout(() => {{ window.close(); }}, 800);
  }} catch (e) {{ setStatus('err', e.message); }}
}}
document.getElementById('form').onsubmit = (ev) => {{ ev.preventDefault(); save(true); }};
document.getElementById('btn-save-only').onclick = () => save(false);
</script>
</body></html>"""


def _esc(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _check_csrf(request: web.Request) -> None:
    if request.headers.get("X-CSRF") != _CSRF:
        raise web.HTTPForbidden(text='{"ok":false,"message":"Invalid session — reopen setup from start.bat"}')


async def _test_discord(token: str) -> dict[str, Any]:
    err = config.validate_discord_token(token)
    if err:
        raise ValueError(err)
    async with ClientSession() as session:
        async with session.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
        ) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200:
                msg = data.get("message") if isinstance(data, dict) else str(data)
                raise ValueError(f"Discord rejected the token ({resp.status}): {msg}")
            return {
                "ok": True,
                "username": f"{data.get('username')}#{data.get('discriminator')}"
                if data.get("discriminator") not in (None, "0")
                else data.get("username"),
                "id": str(data.get("id") or ""),
            }


async def _test_api(values: dict[str, str]) -> dict[str, Any]:
    from api import PartnerClient

    key_id = (values.get("API_KEY_ID") or "").strip()
    secret = (values.get("API_SECRET") or "").strip()
    base = (values.get("API_BASE_URL") or "").strip().rstrip("/")
    # Restore secrets if the form still has the mask
    file_vals = config.read_env_file()
    if key_id.startswith("••••"):
        key_id = file_vals.get("API_KEY_ID", "")
    if secret.startswith("••••"):
        secret = file_vals.get("API_SECRET", "")
    err = config.validate_api_key_id(key_id)
    if err:
        raise ValueError(err)
    if not secret:
        raise ValueError("Paste your API secret.")
    if not base.startswith("http"):
        raise ValueError("API URL must start with https://")
    sign = [
        p.strip()
        for p in (values.get("API_SIGN_ORDER") or "timestamp,nonce,method,path,body_sha256").split(",")
        if p.strip()
    ]
    client = PartnerClient(base, key_id, secret, sign)
    try:
        await client.start()
        ping = await client.ping()
        caller = ping.get("caller") or {}
        label = caller.get("name") or caller.get("key_id") or key_id
        return {"ok": True, "caller": str(label)}
    finally:
        await client.close()


def _merge_secrets(values: dict[str, str]) -> dict[str, str]:
    """Replace masked secret fields with the previously saved values."""
    existing = config.read_env_file()
    out = dict(values)
    for f in config.FIELDS:
        key = f["key"]
        if not f.get("secret"):
            continue
        val = (out.get(key) or "").strip()
        if val.startswith("••••") and key in existing:
            out[key] = existing[key]
    return out


async def handle_index(_request: web.Request):
    return web.Response(text=_page_html(), content_type="text/html")


async def handle_test_discord(request: web.Request):
    _check_csrf(request)
    body = await request.json()
    values = _merge_secrets(body if isinstance(body, dict) else {})
    try:
        result = await _test_discord(values.get("DISCORD_TOKEN") or "")
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=400)


async def handle_test_api(request: web.Request):
    _check_csrf(request)
    body = await request.json()
    values = _merge_secrets(body if isinstance(body, dict) else {})
    try:
        result = await _test_api(values)
        return web.json_response(result)
    except Exception as e:
        return web.json_response({"ok": False, "message": str(e)}, status=400)


async def handle_save(request: web.Request):
    global _SAVED_OK
    _check_csrf(request)
    body = await request.json()
    if not isinstance(body, dict):
        return web.json_response({"ok": False, "message": "Invalid body"}, status=400)
    start = bool(body.pop("start", True))
    body.pop("csrf", None)
    values = _merge_secrets(body)

    # Validate required + dashboard password if set
    err = config.validate_discord_token(values.get("DISCORD_TOKEN") or "")
    if err:
        return web.json_response({"ok": False, "message": err}, status=400)
    err = config.validate_api_key_id(values.get("API_KEY_ID") or "")
    if err:
        return web.json_response({"ok": False, "message": err}, status=400)
    if not (values.get("API_SECRET") or "").strip():
        return web.json_response({"ok": False, "message": "API secret is required."}, status=400)
    dash = (values.get("DASHBOARD_PASSWORD") or "").strip()
    if dash:
        problem = password_problem(dash)
        if problem:
            return web.json_response(
                {"ok": False, "message": f"Dashboard password rejected: {problem}."},
                status=400,
            )

    # Live checks before saving
    try:
        await _test_discord(values["DISCORD_TOKEN"])
        await _test_api(values)
    except Exception as e:
        return web.json_response(
            {"ok": False, "message": f"Could not verify credentials: {e}"},
            status=400,
        )

    try:
        path = config.save_env(values)
    except ValueError as e:
        return web.json_response({"ok": False, "message": str(e)}, status=400)

    _SAVED_OK = True
    msg = f"Saved to {path.name}."
    if start:
        msg += " Starting the bot…"
        # Let the response flush, then stop the wizard server
        asyncio.get_running_loop().call_later(0.4, _DONE.set)
    return web.json_response({"ok": True, "message": msg, "done": start})


async def _open_browser():
    await asyncio.sleep(0.6)
    url = f"http://{HOST}:{PORT}/"
    try:
        webbrowser.open(url)
    except Exception:
        pass
    print(f"\n  Setup page: {url}\n  (If the browser did not open, paste that link.)\n", flush=True)


async def _watch_done(runner: web.AppRunner):
    while not _DONE.is_set():
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.2)
    await runner.cleanup()


def run_wizard(*, force: bool = False) -> bool:
    """Open the setup page. Returns True when settings were saved successfully."""
    global _SAVED_OK
    _SAVED_OK = False
    _DONE.clear()

    if config.is_configured() and not force:
        return True

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_post("/api/test-discord", handle_test_discord)
    app.router.add_post("/api/test-api", handle_test_api)
    app.router.add_post("/api/save", handle_save)

    async def _amain():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        try:
            await site.start()
        except OSError as e:
            print(f"Could not start setup on {HOST}:{PORT}: {e}", flush=True)
            print("Close anything using that port, or set SETUP_PORT=8877", flush=True)
            return
        asyncio.create_task(_open_browser())
        await _watch_done(runner)

    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("\nSetup cancelled.", flush=True)
        return False

    if not _SAVED_OK:
        # User closed without saving — if already configured, allow continue
        return config.is_configured()
    return config.is_configured()


if __name__ == "__main__":
    ok = run_wizard(force=True)
    raise SystemExit(0 if ok else 1)
