"""One-time Schwab OAuth login. Writes token.json next to this file.

Usage (from this folder, inside the trading venv):
    python schwab_login.py            # manual copy/paste flow (works everywhere)
    python schwab_login.py --browser  # opens the browser and captures the redirect

Requires SCHWAB_APP_KEY and SCHWAB_APP_SECRET in this folder's .env. The callback
URL must match the Schwab developer-portal app setting exactly (default
https://127.0.0.1:8080). Schwab refresh tokens live 7 days; re-run when expired.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
load_dotenv(HERE / ".env")

DEFAULT_CALLBACK_URL = "https://127.0.0.1:8080"


def _token_path() -> Path:
    raw = os.getenv("SCHWAB_TOKEN_PATH", "token.json")
    path = Path(raw)
    return path if path.is_absolute() else (HERE / path).resolve()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a Schwab API token for this bot folder.")
    parser.add_argument("--browser", action="store_true",
                        help="use the browser-assisted flow (starts a local HTTPS listener on the callback port)")
    parser.add_argument("--force", action="store_true", help="overwrite an existing token without asking")
    args = parser.parse_args(argv)

    app_key = os.getenv("SCHWAB_APP_KEY", "").strip()
    app_secret = os.getenv("SCHWAB_APP_SECRET", "").strip()
    callback_url = os.getenv("SCHWAB_CALLBACK_URL", DEFAULT_CALLBACK_URL).strip() or DEFAULT_CALLBACK_URL
    if not app_key or not app_secret:
        print(f"ERROR: SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing in {HERE / '.env'}", file=sys.stderr)
        return 2

    token_path = _token_path()
    if token_path.exists() and not args.force:
        answer = input(f"{token_path} already exists. Overwrite? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Aborted; existing token kept.")
            return 0
    token_path.parent.mkdir(parents=True, exist_ok=True)

    from schwab.auth import client_from_login_flow, client_from_manual_flow

    print(f"Callback URL : {callback_url}")
    print(f"Token path   : {token_path}")
    if args.browser:
        client = client_from_login_flow(api_key=app_key, app_secret=app_secret,
                                        callback_url=callback_url, token_path=str(token_path))
    else:
        client = client_from_manual_flow(api_key=app_key, app_secret=app_secret,
                                         callback_url=callback_url, token_path=str(token_path))

    resp = client.get_account_numbers()
    if resp.status_code != 200:
        print(f"Token written, but account lookup returned HTTP {resp.status_code}: {resp.text[:200]}",
              file=sys.stderr)
        return 1
    accounts = resp.json()
    print(f"\nSuccess. Token saved to {token_path}. {len(accounts)} linked account(s):")
    for i, acct in enumerate(accounts):
        number = str(acct.get("accountNumber", ""))
        print(f"  index {i}: ...{number[-4:]}")
    print("Set SCHWAB_ACCOUNT_INDEX in .env or config to pick the trading account (default 0).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
