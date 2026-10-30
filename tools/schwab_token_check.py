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
sys.path.insert(0, str(ROOT / "schwab" / "Micah" / "0dte"))

import schwab_health  # noqa: E402

# 2026-10-26: one file per Schwab LOGIN. Login A (Micah + Dawson, and ASTRA's read-only /ES feed) lives in schwab/Micah/0dte; Micah/45DTE and
# Dawson/0dte read that same file. Login B (Ronnie) has its own.
DEFAULT_TOKENS = {
    "login A  schwab/Micah/0dte": ROOT / "schwab" / "Micah" / "0dte" / "token.json",
    "login B  schwab/Ronnie/45DTE": ROOT / "schwab" / "Ronnie" / "45DTE" / "token.json",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--path", action="append", help="a token file to check (repeatable); default: both Schwab logins")
    parser.add_argument("--strict", action="store_true", help="exit 1 on WARN as well as EXPIRING / DEAD / MISSING")
    args = parser.parse_args(argv)
    targets = {Path(p).name + f" ({p})": Path(p) for p in args.path} if args.path else DEFAULT_TOKENS
    now = datetime.now(schwab_health.ET)
    bad = False
    checked = 0
    print(f"Schwab token check at {now:%Y-%m-%d %H:%M ET}")
    routed = routed_targets(ROOT) if not args.path else set()
    for label, path in targets.items():
        if not args.path and not path.is_file() and not (path.parent / ".env").exists() and path.resolve() not in routed:
            print(f"  [ skip ] {label}: not set up yet (no token.json and no .env in {path.parent}, and no folder routes to it)")
            continue
        checked += 1
        st = schwab_health.assess(path, now, hard_stop=schwab_health.DEFAULT_HARD_STOP)
        flag = {"OK": "  ok  ", "WARN": " WARN ", "EXPIRING": "EXPIRE", "DEAD": " DEAD ", "MISSING": "MISSING"}[st["state"]]
        print(f"  [{flag}] {label}: {st['message']}")
        if st["state"] != "OK":
            folder = path.parent
            print(f"           re-login:  cd \"{folder}\" && python schwab_login.py --force")
        if st["state"] in ("EXPIRING", "DEAD", "MISSING") or (args.strict and st["state"] == "WARN"):
            bad = True
    if not args.path:
        for line, wrong in routing_report(ROOT):
            print(line)
            bad = bad or wrong
    if checked == 0 and not args.path:
        print("  nothing checked: no Schwab folder is set up yet")
        bad = True
    return 1 if bad else 0


# 2026-10-26 person folders: folder -> the token file its .env must lead to (None = its own token.json)
LAYOUT = {
    Path("schwab") / "Micah" / "0dte": None,
    Path("schwab") / "Micah" / "45DTE": Path("schwab") / "Micah" / "0dte" / "token.json",
    Path("schwab") / "Dawson" / "0dte": Path("schwab") / "Micah" / "0dte" / "token.json",
    Path("schwab") / "Ronnie" / "45DTE": None,
    Path("schwab") / "45DTE": Path("schwab") / "Micah" / "0dte" / "token.json",          # 2026-10-30: the paper 45DTE bot, login A
}


def resolve_token_setting(folder: Path, raw: str | None) -> Path:
    """The file a bot in `folder` reads for SCHWAB_TOKEN_PATH=raw, the way the bot resolves it (blank = own token.json; relative = this
    folder; a folder = the token.json inside it)."""
    value = (raw or "").strip()
    path = Path(value) if value else Path("token.json")
    if not path.is_absolute():
        path = folder / path
    path = path.resolve()
    return path / "token.json" if path.is_dir() else path


def routed_targets(root: Path) -> set[Path]:
    """Every token file some folder's .env leads to: a login must be checked when another folder depends on it, even if its own folder
    is empty (e.g. the owner forgot to move Micah/0dte's runtime files after git pull)."""
    from dotenv import dotenv_values
    targets: set[Path] = set()
    for rel, expected_rel in list(LAYOUT.items()) + [(ALPACA, ALPACA_EXPECTED)]:
        folder = root / rel
        env_file = folder / ".env"
        if not env_file.is_file():
            continue
        raw = dotenv_values(env_file).get("SCHWAB_TOKEN_PATH")
        if (raw or "").strip():
            targets.add(resolve_token_setting(folder, raw))
        elif expected_rel is not None and rel == ALPACA:
            targets.add((root / expected_rel).resolve())             # ASTRA's blank value means the login-A file (config.py default)
    return targets


ALPACA = Path("Alpaca") / "0DTE"                                     # ASTRA reads login A read-only; blank = that file by default
ALPACA_EXPECTED = Path("schwab") / "Micah" / "0dte" / "token.json"


def shown(raw: str | None) -> str:
    """The raw value made visible: control characters (a TAB produced by '\\t' inside double quotes) are the usual cause of a wrong path."""
    value = raw or ""
    if any(ord(c) < 32 for c in value):
        return repr(value) + " -- the value contains a hidden character: write it WITHOUT quotes"
    return repr(value)


def routing_report(root: Path) -> list[tuple[str, bool]]:
    """One line per person folder saying which token file its .env leads to, flagged when that is the wrong login."""
    from dotenv import dotenv_values
    lines: list[tuple[str, bool]] = []
    astra = root / ALPACA
    if (astra / ".env").is_file():
        raw = dotenv_values(astra / ".env").get("SCHWAB_TOKEN_PATH")
        expected = (root / ALPACA_EXPECTED).resolve()
        if not (raw or "").strip():
            lines.append((f"  [  ok  ] {ALPACA}: SCHWAB_TOKEN_PATH not set: reads login A (the default)", False))
        else:
            resolved = resolve_token_setting(astra, raw)
            if resolved == expected:
                lines.append((f"  [  ok  ] {ALPACA}: reads {resolved}", False))
            else:
                lines.append((f"  [WRONG ] {ALPACA}: SCHWAB_TOKEN_PATH={shown(raw)} leads to {resolved!r} but ASTRA must read {expected} "
                              f"(or leave it blank): its live /ES feed silently drops to delayed data otherwise", True))
    for rel, expected_rel in LAYOUT.items():
        folder = root / rel
        if not folder.is_dir():
            continue
        env_file = folder / ".env"
        label = str(rel)
        if not env_file.is_file():
            if (folder / "token.json").is_file() and expected_rel is not None:
                lines.append((f"  [WRONG ] {label}: has a token.json of its own but no .env -- a second login made here cancels "
                              f"{expected_rel.parent}'s; set SCHWAB_TOKEN_PATH in its .env", True))
            else:
                lines.append((f"  [ skip ] {label}: no .env yet", False))
            continue
        raw = dotenv_values(env_file).get("SCHWAB_TOKEN_PATH")
        resolved = resolve_token_setting(folder, raw)
        expected = (root / expected_rel).resolve() if expected_rel else (folder / "token.json").resolve()
        own = (folder / "token.json").resolve()
        if resolved == expected and (expected_rel is None or (raw or "").strip()):
            lines.append((f"  [  ok  ] {label}: reads {resolved}", False))
        elif expected_rel is not None and not (raw or "").strip():
            sibling_ok = any(p != folder and p.is_dir() and (p / "schwab_login.py").is_file()      # Micah/45DTE finds Micah/0dte by the
                             for p in folder.parent.iterdir())                                      # sibling rule
            own_key = str(dotenv_values(env_file).get("SCHWAB_APP_KEY") or "").strip()
            login_env = root / expected_rel.parent / ".env"
            login_key = str(dotenv_values(login_env).get("SCHWAB_APP_KEY") or "").strip() if login_env.is_file() else ""
            if sibling_ok or (own_key and own_key == login_key):
                lines.append((f"  [  ok  ] {label}: SCHWAB_TOKEN_PATH not set: shares the newest login of the bots on the same app key "
                              f"({expected_rel.parent} among them)", False))
            else:
                lines.append((f"  [WRONG ] {label}: SCHWAB_TOKEN_PATH not set and its SCHWAB_APP_KEY does not match {expected_rel.parent}'s, so it "
                              f"expects its OWN token.json: a login made here is a separate login. Use the same app key as {expected_rel.parent} "
                              f"(or set SCHWAB_TOKEN_PATH to {expected_rel})", True))
        else:
            lines.append((f"  [WRONG ] {label}: SCHWAB_TOKEN_PATH={shown(raw)} leads to {str(resolved)!r} but should lead to {expected}", True))
        if expected_rel is not None and own.is_file() and resolved != own:
            lines.append((f"  [WRONG ] {label}: a stray token.json sits in this folder (a separate login that may have cancelled "
                          f"{expected_rel.parent}'s): move it away", True))
    return lines


if __name__ == "__main__":
    raise SystemExit(main())
