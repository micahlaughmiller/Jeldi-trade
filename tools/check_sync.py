"""Report drift between the Alpaca and Schwab copies of each strategy, and between the Schwab person folders.

Strategy files must be byte-identical across broker folders; only broker.py,
config, schwab_login.py and broker tests may differ. Config files are compared
on their strategy parameters (everything after the broker section).

2026-10-26: the Schwab bots live in person folders -- schwab/Micah (0dte + 45DTE, login A), schwab/Dawson (0dte, login A),
schwab/Ronnie (45DTE, login B); 2026-10-30: schwab/45DTE is back as the PAPER 45DTE bot (sim, login A). Micah's folders are the
primaries the Alpaca copies are compared with; Dawson/0dte, Ronnie/45DTE and schwab/45DTE must be byte-identical CLONES of
Micah's (every tracked file; only .env.example may differ).

    python tools/check_sync.py                  # report
    python tools/check_sync.py --apply-clones   # after editing Micah's copy: push every tracked file to Dawson/0dte and Ronnie/45DTE
    python tools/check_sync.py --apply-clones --sweep-extras   # ... and move code/data files only a clone has to an archive folder
"""

import argparse
import ast
import filecmp
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAIRS = {
    "0DTE": (ROOT / "Alpaca" / "0DTE", ROOT / "schwab" / "Micah" / "0dte"),
    "45DTE": (ROOT / "Alpaca" / "45DTE", ROOT / "schwab" / "Micah" / "45DTE"),
}
# clone label -> (primary, clone): the clone must hold exactly the primary's tracked files, byte for byte (.env.example excepted)
CLONES = {
    "Dawson/0dte": (ROOT / "schwab" / "Micah" / "0dte", ROOT / "schwab" / "Dawson" / "0dte"),
    "Ronnie/45DTE": (ROOT / "schwab" / "Micah" / "45DTE", ROOT / "schwab" / "Ronnie" / "45DTE"),
    "45DTE (paper)": (ROOT / "schwab" / "Micah" / "45DTE", ROOT / "schwab" / "45DTE"),     # 2026-10-30: the original paper bot folder, kept
}
CLONE_MAY_DIFFER = {".env.example"}

BROKER_SPECIFIC = {
    "broker.py", "config.py", "config_45dte.py", "schwab_login.py", "alpaca-reset.py",
    "README.md", "requirements.txt", ".env", ".env.example", "token.json",
}
BROKER_SPECIFIC_TESTS = {"test_broker_alpaca.py", "test_broker_schwab.py", "live_smoke_alpaca.py",
                         "test_alpaca_exit_rules.py", "test_schwab_exit_rules.py",
                         "test_alpaca_adoption_rules.py", "test_schwab_adoption_rules.py",
                         "test_live_guards.py"}

# Deliberate, documented Schwab-vs-Alpaca strategy-config differences:
# label -> {key: (alpaca value, schwab value, reason)}, values as ast.unparse() prints them. A key only
# passes when BOTH sides hold exactly these values; anything else (including a "fix" that makes them
# match, or an edit to just one side) is still reported as drift. main() lists the accepted ones.
INTENTIONAL_CONFIG_DIVERGENCE = {
    "0DTE": {
        "ADOPT_UNTRACKED_SPXW": ("True", "False", "2026-10-02: Schwab's account is shared with the 45DTE bot and the owner's "
                                 "hand trades, so it never adopts or force-closes SPXW legs it did not open"),
        "ADOPT_UNTRACKED_STOCK": ("True", "False", "2026-10-15: the same for single-stock option legs now that Schwab runs Strategy F: "
                                  "it never adopts or force-closes stock legs it did not open"),
        "STRATEGIES": ("('B',)", "('B', 'C', 'F')", "2026-10-15: Schwab 0DTE trades B and F (live go-live); 2026-11-12: C as well"),
        "DAILY_LOSS_LIMIT_PCT": ("0.1", "0.025", "2026-10-15: Schwab live go-live: acceptable daily loss 1% (Alpaca 10%); 2026-11-12: 2.5%"),
        "DAILY_LOSS_LIMIT_EXEMPT": ("('F',)", "()", "2026-10-15: on Schwab F follows the daily loss limit (Alpaca: F is exempt)"),
        "DAILY_PROFIT_TARGET_PCT": ("None", "0.05", "2026-10-15: Schwab live go-live: no new entries once the daily goal is banked "
                                   "(Alpaca: no target); 2026-11-12: 5%"),
        "DAILY_LOSS_BASIS": ("'equity'", "'bot'", "2026-10-15: Schwab's account is shared with hand trades and the 45DTE bot, so its loss limit "
                             "counts only this bot's P/L (Alpaca: account equity)"),
        "MAX_OPEN_POSITIONS_TOTAL": ("2", "1", "2026-10-15: Schwab 0DTE (live go-live) holds ONE trade open at a time across B and F"),
        "EXIT_PNL_BASIS": ("'prev_tick'", "'fill'", "2026-11-06: on Schwab (a real account) the official P/L is booked on the "
                           "broker's actual fill (Alpaca paper: the tick before the trigger)"),
        "RESTING_EXITS": ("False", "True", "2026-11-06: on Schwab the exit (profit-target limit + stop-limit) rests at the broker as "
                          "an OCO right after the entry fills; Alpaca cannot hold one for a spread"),
        "SPX_CANDLE_SOURCE": ("'yahoo'", "'schwab'", "2026-11-10: the live Schwab bots read SPX candles, the opening range, the spot "
                              "and the basis from Schwab's own $SPX price history (Alpaca paper: Yahoo)"),
        "OPENING_RANGE_MINUTES": ("30", "15", "2026-11-10: Schwab live: a 15-minute opening range, breaks tradable from 09:45 ET "
                                  "(08:45 Central); Alpaca keeps 30"),
        "OVERNIGHT_ENTRY_END_MIN": ("30", "15", "2026-11-10: Schwab live: the ORB phase starts 15 minutes after the open, with the "
                                    "15-minute opening range; Alpaca keeps 30"),
        "MAX_TRADES_PER_DAY": ("5", "3", "2026-11-12: Schwab live: 3 trades a day per strategy (Alpaca 5)"),
        "MAX_TRADES_TOTAL_PER_DAY": ("None", "3", "2026-11-12: Schwab live: 3 trades a day across every strategy together (Alpaca: off)"),
        "MAX_CONSECUTIVE_LOSSES": ("5", "2", "2026-11-12: Schwab live: 2 consecutive losses stop the day (Alpaca 5)"),
        "LIMITS_BY_STRATEGY": ("{'A': {'MAX_TRADES_PER_DAY': 5, 'MAX_CONSECUTIVE_LOSSES': 5, 'COOLDOWN_MIN': 30}, 'B': {}, 'C': {'MAX_TRADES_PER_DAY': 3}, 'D': {'MAX_TRADES_PER_DAY': 3}, 'E': {'MAX_TRADES_PER_DAY': 3}, 'F': {'MAX_TRADES_PER_DAY': 3, 'MAX_CONSECUTIVE_LOSSES': 7, 'MAX_CONCURRENT_POSITIONS': 3}}",
                               "{'A': {'MAX_TRADES_PER_DAY': 3, 'MAX_CONSECUTIVE_LOSSES': 2, 'COOLDOWN_MIN': 10}, 'B': {}, 'C': {'MAX_TRADES_PER_DAY': 3}, 'D': {'MAX_TRADES_PER_DAY': 3}, 'E': {'MAX_TRADES_PER_DAY': 3}, 'F': {'MAX_TRADES_PER_DAY': 3, 'MAX_CONSECUTIVE_LOSSES': 7, 'MAX_CONCURRENT_POSITIONS': 3}}",
                               "2026-11-12: Schwab live: A's own limits 3 trades, 2 losses, 10-minute cool-down (Alpaca 5 / 5 / 30)"),
        "STOP_LOSS": ("0.6", "0.3", "2026-11-12: Schwab live: STOP_LOSS 0.30 -- Strategy A's stop; B, C, D and E read B_STOP_LOSS (Alpaca 0.60)"),
        "MAX_TRADES_PER_DAY_SHARED": ("{}", "{('B', 'C'): 3}", "2026-11-12: Schwab live: B and C together take at most 3 trades a day "
                                      "(Micah: 'all B and C strategies to be a total of 1-3 trades a day'); Alpaca paper: off"),
    },
    "45DTE": {
        "EXIT_DTE_ENABLED": ("True", "False", "2026-10-02: Schwab never force-closes on DTE; positions ride to expiration"),
        "MAX_LOSS_EXIT": ("True", "False", "2026-10-02: Schwab does not stop out at max loss (hits still feed the circuit breaker)"),
        "PROFIT_FLOOR_MODE": ("'fixed'", "'trail'", "2026-10-02: Schwab exits on a tick trail instead of the fixed floor"),
        "PROFIT_FLOOR_ARM_PCT_OF_TARGET": ("0.85", "0.4", "2026-10-02: Schwab arms at 40% of the way to target, not 85%"),
    },
}

BROKER_CONFIG_KEYS = {
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL", "ALPACA_DATA_URL",
    "ACTIVE_TRADER", "TRADER_NAME", "TRADER_NAMES", "PAPER_TRADING", "DRY_RUN", "PERSONA_OVERRIDES",
    "CDE_STRATEGIES",   # helper tuple for PERSONA_OVERRIDES entries, Alpaca-persona-only like it
    "SCHWAB_APP_KEY", "SCHWAB_APP_SECRET", "SCHWAB_CALLBACK_URL", "SCHWAB_TOKEN_PATH",
    "SCHWAB_ACCOUNT_INDEX", "SCHWAB_ACCOUNT_LAST4", "SCHWAB_MODE", "SIM_STARTING_EQUITY", "SIM_QUOTE_CACHE_SEC",
    "SIM_FILL_START", "SIM_FILL_END", "SIM_INDEX_FILL_END",
    "LIVE_CAPITAL_CAP_ENABLED", "LIVE_CAPITAL_FLOOR_USD", "LIVE_CAPITAL_PCT", "LIVE_MAX_CONTRACTS_PER_TRADE", "HARD_MAX_CONTRACTS",
}


def config_params(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name.isupper() and name not in BROKER_CONFIG_KEYS:
                out[name] = ast.unparse(node.value)
    return out


def compare_folder(a: Path, b: Path, intentional: dict[str, tuple[str, str, str]] | None = None,
                   notes: list[str] | None = None) -> list[str]:
    problems: list[str] = []
    intentional = intentional or {}
    for src in sorted(a.glob("*.py")):
        if src.name in BROKER_SPECIFIC:
            continue
        dst = b / src.name
        if not dst.exists():
            problems.append(f"missing in {b.relative_to(ROOT)}: {src.name}")
        elif not filecmp.cmp(src, dst, shallow=False):
            problems.append(f"differs: {src.name}")
    for src in sorted((a / "tests").glob("*.py")) if (a / "tests").exists() else []:
        if src.name in BROKER_SPECIFIC_TESTS:
            continue
        dst = b / "tests" / src.name
        if not dst.exists():
            problems.append(f"missing in {b.relative_to(ROOT)}/tests: {src.name}")
        elif not filecmp.cmp(src, dst, shallow=False):
            problems.append(f"differs: tests/{src.name}")
    cfg_name = "config.py" if (a / "config.py").exists() else "config_45dte.py"
    pa, pb = config_params(a / cfg_name), config_params(b / cfg_name)
    for key, expected in intentional.items():
        if pa.get(key) == pb.get(key):
            problems.append(f"config {key}: listed as an intentional divergence but alpaca and schwab now both "
                            f"hold {pa.get(key)!r}; remove it from INTENTIONAL_CONFIG_DIVERGENCE")
    for key in sorted(set(pa) | set(pb)):
        if pa.get(key) != pb.get(key):
            line = f"config {key}: alpaca={pa.get(key)!r} schwab={pb.get(key)!r}"
            expected = intentional.get(key)
            if expected and (pa.get(key), pb.get(key)) == expected[:2]:
                if notes is not None:
                    notes.append(f"{line}  [intentional: {expected[2]}]")
            else:
                problems.append(line + ("  [NOT the sanctioned values: expected "
                                        f"alpaca={expected[0]!r} schwab={expected[1]!r}]" if expected else ""))
    return problems


RUNTIME_DIRS = {"logs", "logs_45dte", "shared_data", "__pycache__", ".pytest_cache"}
CLONE_DRIFT_SUFFIXES = {".py", ".json", ".txt", ".md", ".csv"}      # files a clone may drift on; launchers (.cmd/.bat) and backups are left alone


def git_listing(folder: Path) -> list[Path] | None:
    """Files git tracks or would add under `folder` (relative to it), or None when git is unavailable or fails. Ignored runtime files
    (.env, token.json, logs/, shared_data/ ...) are never listed."""
    try:
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", str(folder)],
                             cwd=str(ROOT), capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    try:
        return [Path(line.strip()).relative_to(folder.relative_to(ROOT)) for line in out.stdout.splitlines() if line.strip()]
    except ValueError:
        return None


def guessed_listing(folder: Path) -> list[Path]:
    """Report-mode fallback when git is unavailable: code and data files only, never runtime folders or secrets."""
    return [p.relative_to(folder) for p in folder.rglob("*")
            if p.is_file() and not (set(p.relative_to(folder).parts[:-1]) & RUNTIME_DIRS)
            and p.suffix in (".py", ".json", ".txt", ".md") and not p.name.startswith((".env", "token.json", "selected_account.json"))]


def tracked_files(folder: Path) -> list[Path]:
    listed = git_listing(folder)
    return listed if listed is not None else guessed_listing(folder)


def compare_clone(primary: Path, clone: Path, lister=tracked_files) -> list[str]:
    problems: list[str] = []
    expected = set()
    for rel in lister(primary):
        if rel.name in CLONE_MAY_DIFFER:
            continue
        expected.add(rel)
        src, dst = primary / rel, clone / rel
        if not src.is_file():
            problems.append(f"tracked in git but missing on disk in {primary.relative_to(ROOT)}: {rel} (git rm it, or restore it)")
            continue
        if not dst.exists():
            problems.append(f"missing in {clone.relative_to(ROOT)}: {rel}")
        elif not filecmp.cmp(src, dst, shallow=False):
            problems.append(f"differs: {rel}")
    for rel in lister(clone):                           # code/data the clone has that the primary does not
        if rel.name in CLONE_MAY_DIFFER or rel in expected or rel.suffix not in CLONE_DRIFT_SUFFIXES:
            continue
        problems.append(f"extra in {clone.relative_to(ROOT)}: {rel} (not in {primary.relative_to(ROOT)})")
    return problems


def apply_clone(primary: Path, clone: Path, archive_root: Path, sweep: bool = False, lister=git_listing) -> list[str]:
    """Make the clone match the primary: copy missing / differing files. With sweep=True, code/data files the primary does not have are
    MOVED to archive_root (never deleted) with a note. Refuses to touch anything unless git can list both folders."""
    primary_files, clone_files = lister(primary), lister(clone)
    if primary_files is None or clone_files is None:
        return [f"git listing unavailable for {primary.relative_to(ROOT)} / {clone.relative_to(ROOT)}: NOTHING changed (git must be on PATH)"]
    done: list[str] = []
    expected = set()
    for rel in primary_files:
        if rel.name in CLONE_MAY_DIFFER or not (primary / rel).is_file():
            continue
        expected.add(rel)
        dst = clone / rel
        if not dst.exists() or not filecmp.cmp(primary / rel, dst, shallow=False):
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(primary / rel, dst)
            done.append(f"copied {rel}")
    extras = [rel for rel in clone_files if rel.name not in CLONE_MAY_DIFFER and rel not in expected and rel.suffix in CLONE_DRIFT_SUFFIXES]
    if extras and not sweep:
        done.append("left alone (use --sweep-extras to move them to the archive): " + ", ".join(str(r) for r in extras))
    elif extras:
        archive = archive_root / clone.relative_to(ROOT)
        note = archive / "README.txt"
        archive.mkdir(parents=True, exist_ok=True)
        lines = [f"Moved out of {clone} by tools/check_sync.py --apply-clones --sweep-extras on {datetime.now():%Y-%m-%d %H:%M}:",
                 f"these files were not in {primary} (the primary the clone must match). To restore one, copy it back to the same path.", ""]
        for rel in extras:
            (archive / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(clone / rel), str(archive / rel))
            lines.append(f"  {rel}   <- {clone / rel}")
            done.append(f"moved extra {rel} to {archive / rel}")
        note.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply-clones", action="store_true",
                        help="make Dawson/0dte and Ronnie/45DTE match Micah's folders (copies files; needs git on PATH)")
    parser.add_argument("--sweep-extras", action="store_true",
                        help="with --apply-clones: also move code/data files only a clone has to <repo parent>/_archive-for-review (never deleted)")
    args = parser.parse_args(argv)
    exit_code = 0
    if args.apply_clones:
        archive_root = ROOT.parent / "_archive-for-review" / f"check_sync-{datetime.now():%Y%m%d-%H%M%S}"
        for label, (primary, clone) in CLONES.items():
            for line in apply_clone(primary, clone, archive_root, sweep=args.sweep_extras):
                print(f"{label}: {line}")
    if git_listing(ROOT / "tools") is None:
        print("note: git is not available here; the clone check is working from a guess of the file lists")
    for label, (primary, clone) in CLONES.items():
        problems = compare_clone(primary, clone)
        print(f"{label}: {'identical to ' + str(primary.relative_to(ROOT)) if not problems else str(len(problems)) + ' difference(s)'}")
        for p in problems:
            print(f"  - {p}")
        if problems:
            exit_code = 1
    for label, (a, b) in PAIRS.items():
        notes: list[str] = []
        problems = compare_folder(a, b, INTENTIONAL_CONFIG_DIVERGENCE.get(label), notes)
        status = "in sync" if not problems else f"{len(problems)} difference(s)"
        print(f"{label}: {status}")
        for p in problems:
            print(f"  - {p}")
        for n in notes:
            print(f"  ~ {n}")
        if problems:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
