"""Which Schwab token file does each bot use, and how old is that login? Run this when a bot says "token rejected".

    python tools/schwab_token_check.py            # ASTRA + both Schwab sim bots: the file each reads, the login's age, the verdict
    python tools/schwab_token_check.py --strict   # exit code 1 for a WARN (5+ days old) too
    python tools/schwab_token_check.py --path "C:/somewhere/token.json" [--path ...]    # just these files

Schwab refresh tokens die exactly 7 days after the login that made them, whatever the bots do. A NEW login also appears to cancel the
older one for the same app: when two bots read two different token files (two separate logins), logging in for one kills the other
("token rejected" again, right after you re-logged in). All bots should read ONE file -- schwab/0dte/token.json -- and be logged in
once; this prints which file each one reads (SCHWAB_TOKEN_PATH: the Windows environment, then the folder's .env, then the default).
Exit code 1 when a login needs doing today or the bots do not share one file. Run it from Windows Task Scheduler at ~08:30 ET too.

Only each token file's own creation_timestamp is read -- nothing secret is printed.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "schwab" / "0dte"))

import schwab_health  # noqa: E402

BOTS = (
    ("ASTRA (Alpaca/0DTE)", ROOT / "Alpaca" / "0DTE", ROOT / "schwab" / "0dte" / "token.json"),
    ("Schwab 0DTE sim", ROOT / "schwab" / "0dte", ROOT / "schwab" / "0dte" / "token.json"),
    ("Schwab 45DTE sim", ROOT / "schwab" / "45DTE", ROOT / "schwab" / "45DTE" / "token.json"),
)


def resolve(folder: Path, default: Path, environ=None) -> tuple[Path, str]:
    """(the token file this bot folder reads, where that setting came from) -- the way the bots resolve SCHWAB_TOKEN_PATH: the
    environment beats the folder's .env beats the default; a relative path is relative to the folder; a FOLDER means its token.json."""
    environ = os.environ if environ is None else environ
    from_env = str(environ.get("SCHWAB_TOKEN_PATH", "") or "").strip()
    from_dotenv = ""
    env_file = folder / ".env"
    if env_file.is_file():
        try:
            from dotenv import dotenv_values
            from_dotenv = str(dotenv_values(env_file).get("SCHWAB_TOKEN_PATH") or "").strip()
        except Exception:                                # noqa: BLE001
            from_dotenv = ""
    if from_env:
        raw, source = from_env, "Windows environment variable"
    elif from_dotenv:
        raw, source = from_dotenv, ".env"
    else:
        raw, source = str(default), "default"
    path = Path(raw)
    path = path if path.is_absolute() else folder / path
    if path.is_dir():
        path = path / "token.json"
    return path, source


def main(argv: list[str] | None = None, bots=BOTS, environ=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", action="append", help="a token file to check (repeatable); default: ASTRA and both Schwab sim bots")
    parser.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as EXPIRING / DEAD / MISSING")
    args = parser.parse_args(argv)
    now = datetime.now(schwab_health.ET)
    print(f"Schwab token check at {now:%Y-%m-%d %H:%M ET}")
    if args.path:
        targets = [(f"{Path(p).name}", Path(p), "--path") for p in args.path]
    else:
        targets = [(name, *resolve(folder, default, environ)) for name, folder, default in bots]
    bad = False
    files: dict[str, list[str]] = {}
    for name, path, source in targets:
        st = schwab_health.assess(path, now, hard_stop=schwab_health.DEFAULT_HARD_STOP)
        flag = {"OK": "  ok  ", "WARN": " WARN ", "EXPIRING": "EXPIRE", "DEAD": " DEAD ", "MISSING": "MISSING"}[st["state"]]
        print(f"  [{flag}] {name}: {path}  ({source})")
        print(f"           {st['message']}")
        if st["state"] != "OK":
            folder = path.parent if path.suffix else path
            print(f"           re-login:  cd \"{folder}\" && python schwab_login.py --force")
        if st["state"] in ("EXPIRING", "DEAD", "MISSING") or (args.strict and st["state"] == "WARN"):
            bad = True
        files.setdefault(os.path.normcase(str(path)), []).append(name)
    existing = [k for k in files if Path(k).is_file()]
    if len(existing) > 1 and not args.path:
        bad = True
        print("\nPROBLEM: these bots read DIFFERENT token files, i.e. separate logins:")
        for key in files:
            print(f"    {key}\n        used by: {', '.join(files[key])}")
        print("  A new Schwab login appears to cancel the older one, so each re-login kills the other bot's token. Point every bot at\n"
              "  ONE file: add  SCHWAB_TOKEN_PATH=<full path to that token.json>  to each bot's .env (and remove any Windows environment\n"
              "  variable of that name), restart the bots, and log in once (never separately per bot).")
    elif not args.path:
        print(f"\nAll bots read one token file ({len(files)} distinct): one login serves them all.")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
