"""Report drift between the Alpaca and Schwab copies of each strategy.

Strategy files must be byte-identical across broker folders; only broker.py,
config, schwab_login.py and broker tests may differ. Config files are compared
on their strategy parameters (everything after the broker section).

    python tools/check_sync.py
"""

import ast
import filecmp
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

PAIRS = {
    "0DTE": (ROOT / "Alpaca" / "0DTE", ROOT / "schwab" / "0dte"),
    "45DTE": (ROOT / "Alpaca" / "45DTE", ROOT / "schwab" / "45DTE"),
}

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
    "SCHWAB_ACCOUNT_INDEX", "SCHWAB_MODE", "SIM_STARTING_EQUITY", "SIM_QUOTE_CACHE_SEC",
    "SIM_FILL_START", "SIM_FILL_END", "SIM_INDEX_FILL_END",
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


def main() -> int:
    exit_code = 0
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
