"""Check the age of every Schwab token the bots use, before the market opens.

    python tools/schwab_token_check.py            # table of both Schwab bots; exit code 1 if any needs a re-login today
    python tools/schwab_token_check.py --strict   # exit code 1 for a WARN (5+ days old) too
    python tools/schwab_token_check.py --path "C:/somewhere/token.json" [--path ...]

Schwab refresh tokens die exactly 7 days after the login that made them, whatever the bots do; the bots now refuse new
entries when a token would expire before the session ends, but a re-login before the open is what keeps them trading. Run
this from Windows Task Scheduler at ~08:30 ET on trading days (the exit code and the printed re-login lines are the alert).

Only the token file's own creation_timestamp is read -- nothing secret is printed.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "schwab" / "0dte"))

import schwab_health  # noqa: E402

DEFAULT_TOKENS = {
    "schwab/0dte": ROOT / "schwab" / "0dte" / "token.json",
    "schwab/45DTE": ROOT / "schwab" / "45DTE" / "token.json",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", action="append", help="a token file to check (repeatable); default: both Schwab bots")
    parser.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as EXPIRING / DEAD / MISSING")
    args = parser.parse_args(argv)
    targets = {Path(p).name + f" ({p})": Path(p) for p in args.path} if args.path else DEFAULT_TOKENS
    now = datetime.now(schwab_health.ET)
    bad = False
    print(f"Schwab token check at {now:%Y-%m-%d %H:%M ET}")
    for label, path in targets.items():
        st = schwab_health.assess(path, now, hard_stop=schwab_health.DEFAULT_HARD_STOP)
        flag = {"OK": "  ok  ", "WARN": " WARN ", "EXPIRING": "EXPIRE", "DEAD": " DEAD ", "MISSING": "MISSING"}[st["state"]]
        print(f"  [{flag}] {label}: {st['message']}")
        if st["state"] != "OK":
            folder = path.parent
            print(f"           re-login:  cd \"{folder}\" && python schwab_login.py --force")
        if st["state"] in ("EXPIRING", "DEAD", "MISSING") or (args.strict and st["state"] == "WARN"):
            bad = True
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
