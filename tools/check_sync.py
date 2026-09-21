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
BROKER_SPECIFIC_TESTS = {"test_broker_alpaca.py", "test_broker_schwab.py", "live_smoke_alpaca.py"}

BROKER_CONFIG_KEYS = {
    "ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ALPACA_BASE_URL", "ALPACA_DATA_URL",
    "ACTIVE_TRADER", "TRADER_NAME", "TRADER_NAMES", "PAPER_TRADING", "DRY_RUN",
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


def compare_folder(a: Path, b: Path) -> list[str]:
    problems: list[str] = []
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
    for key in sorted(set(pa) | set(pb)):
        if pa.get(key) != pb.get(key):
            problems.append(f"config {key}: alpaca={pa.get(key)!r} schwab={pb.get(key)!r}")
    return problems


def main() -> int:
    exit_code = 0
    for label, (a, b) in PAIRS.items():
        problems = compare_folder(a, b)
        status = "in sync" if not problems else f"{len(problems)} difference(s)"
        print(f"{label}: {status}")
        for p in problems:
            print(f"  - {p}")
        if problems:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
