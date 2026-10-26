"""Log in to Schwab ONCE and give both Schwab bots that same login.

    python tools/schwab_login_both.py            # log in (schwab/Micah/0dte), copy the new token.json into schwab/Micah/45DTE, show both

Why: both Schwab bots use the same Schwab app, and a NEW login appears to cancel the older one -- so two separate logins (one per folder)
kill each other ("token rejected" right after you re-logged in). When nine personas were running, the 45DTE bot worked because its
token.json was a COPY of the 0dte login (identical creation time and refresh token). This does exactly that, weekly (a login lives 7 days):
it runs schwab_login.py in schwab/Micah/0dte (you paste the redirect URL as usual), checks that a fresh token.json was written, and copies it
over schwab/Micah/45DTE/token.json. The file it replaces is kept beside it as token.json.replaced-<time>.token (matched by .gitignore's *.token).

2026-10-26 person folders: Dawson/0dte uses the SAME login and reads schwab/Micah/0dte/token.json directly (SCHWAB_TOKEN_PATH in its .env), so
nothing is copied there. Ronnie/45DTE is a DIFFERENT Schwab login with its own token.json: log in from that folder; it is never copied.

Only creation timestamps are printed -- nothing secret.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "schwab" / "Micah" / "0dte"
# 2026-10-26: no targets any more. Micah/45DTE and Dawson/0dte read schwab/Micah/0dte/token.json directly (SCHWAB_TOKEN_PATH in their .env), so a
# copy in Micah/45DTE would only be a stray file the daily check flags. Running this now just logs in from Micah/0dte.
TARGETS: tuple[Path, ...] = ()


def created(path: Path) -> float | None:
    """The login's creation_timestamp of a token file, or None (missing, unreadable, no timestamp)."""
    try:
        stamp = json.loads(path.read_text(encoding="utf-8")).get("creation_timestamp")
        return float(stamp) if stamp else None
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def when(stamp: float | None) -> str:
    return "none" if stamp is None else datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def run_login(folder: Path) -> int:
    """schwab_login.py --force in `folder`, interactive (its prompts reach the terminal)."""
    return subprocess.call([sys.executable, "schwab_login.py", "--force"], cwd=str(folder))


def copy_login(source: Path, target: Path, now: datetime | None = None) -> Path | None:
    """Copy source/token.json over target/token.json, keeping the file it replaces beside it. Returns the kept copy (None if there was none)."""
    now = now or datetime.now()
    src, dst = source / "token.json", target / "token.json"
    kept = None
    if dst.is_file():
        kept = target / f"token.json.replaced-{now:%Y%m%d-%H%M%S}.token"
        shutil.copyfile(dst, kept)
    shutil.copyfile(src, dst)
    return kept


def main(argv: list[str] | None = None, source: Path = SOURCE, targets: tuple[Path, ...] = TARGETS, login=run_login) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-login", action="store_true", help="skip the login: only copy the schwab/Micah/0dte token.json that is there now")
    args = parser.parse_args(argv)
    before = created(source / "token.json")
    if not args.no_login:
        print(f"Logging in from {source} ...")
        code = login(source)
        if code != 0:
            print(f"The login did not finish (exit code {code}); nothing was copied.", file=sys.stderr)
            return 1
    after = created(source / "token.json")
    if after is None:
        print(f"No usable token file at {source / 'token.json'}: nothing was copied.", file=sys.stderr)
        return 1
    if not args.no_login and before is not None and after <= before:
        print(f"{source / 'token.json'} was not rewritten (login time still {when(after)}): nothing was copied.", file=sys.stderr)
        return 1
    for target in targets:
        kept = copy_login(source, target)
        print(f"Copied the {when(after)} login to {target / 'token.json'}" + (f" (the old file is kept as {kept.name})" if kept else ""))
    print("\nLogin A renewed. Restart the bots that were running; redo this in 7 days (before the login expires)."
          + (" The other folders on login A read this file directly." if not targets else ""))
    for folder in (source, *targets):
        print(f"  {folder / 'token.json'}: login {when(created(folder / 'token.json'))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
