#!/usr/bin/env python3
"""Easy entry point for the partner ride bot.

  python run.py           → open setup if needed, then start the bot
  python run.py --setup   → always open the setup page
  python run.py --check   → print whether .env is ready (no bot start)
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Partner ride Discord bot")
    parser.add_argument("--setup", action="store_true", help="Open the setup page even if already configured")
    parser.add_argument("--check", action="store_true", help="Only report whether settings are complete")
    args = parser.parse_args(argv)

    import config

    config.load()

    if args.check:
        missing = config.missing_required()
        if missing:
            print("NOT READY - missing:", ", ".join(missing))
            if config.is_hosted():
                print("On Railway: set Variables (see DEPLOY_RAILWAY.md), then redeploy.")
            else:
                print("Run:  python run.py   or  start.bat")
            return 1
        print("READY -", "Railway/hosted" if config.is_hosted() else config.ENV_PATH)
        if config.is_hosted():
            print("  host=", config.get("DASHBOARD_HOST"), "port=", config.get("DASHBOARD_PORT") or os.getenv("PORT"))
            print("  public=", config.get("PUBLIC_BASE_URL") or "(auto from RAILWAY_PUBLIC_DOMAIN)")
            print("  db=", config.get("DB_PATH") or "(default)")
        return 0

    # Railway / hosted: never open the local browser wizard — use Variables instead.
    if config.is_hosted():
        if args.setup:
            print(
                "Setup wizard is for local PCs only.\n"
                "On Railway, set Variables in the project dashboard (see DEPLOY_RAILWAY.md).",
                flush=True,
            )
            return 1
        if not config.is_configured():
            print(
                "Missing required env vars on this host:\n  "
                + ", ".join(config.missing_required())
                + "\nAdd them under Railway → Variables, then Redeploy.",
                flush=True,
            )
            return 1
    elif args.setup or not config.is_configured():
        print("Opening setup in your browser…", flush=True)
        from setup_wizard import run_wizard

        if not run_wizard(force=bool(args.setup)):
            print("Setup was not completed. Bot not started.", flush=True)
            return 1
        config.load(override=True)

    if not config.is_configured():
        print("Still missing required settings. Run again: python run.py --setup", flush=True)
        return 1

    # Import bot only after config exists so module-level config succeeds.
    import bot

    logging.getLogger("bot").info(
        "Starting bot (hosted=%s db=%s)",
        config.is_hosted(),
        bot.DB_PATH,
    )
    bot.bot.run(bot.DISCORD_TOKEN, log_handler=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
