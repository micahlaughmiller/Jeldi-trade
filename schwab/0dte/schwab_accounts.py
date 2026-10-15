"""Which Schwab account do the bots trade? -- the selection that is made right after a token renewal.

A Schwab login can see several accounts. The bots used to take "the Nth linked account" (SCHWAB_ACCOUNT_INDEX), which silently changes when the
account list changes. For LIVE orders the account is now chosen explicitly, once per login, and remembered in selected_account.json (next to the
bot; git-ignored): schwab_login.py offers the choice right after a successful login, and `python schwab_select_account.py` repeats it any time.
The broker then refuses to place live orders unless a selection exists and that account is still linked to the login.

Only the last four digits of the account number are ever shown or stored for display; the file also holds Schwab's opaque account hash (what the
API calls need), so it is private like the token.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

FILE_NAME = "selected_account.json"


def selection_path(folder: Path) -> Path:
    return Path(folder) / FILE_NAME


def read_selection(path: Path) -> dict | None:
    """The saved selection ({"hash_value", "last4", "type", "selected_at"}), or None (missing, unreadable, incomplete)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("hash_value") or not data.get("last4"):
        return None
    return data


def write_selection(path: Path, entry: dict) -> None:
    """Atomic: the bot never reads half a file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(entry, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def describe(entry: dict) -> str:
    kind = f" {entry['type']}" if entry.get("type") else ""
    return f"...{entry['last4']}{kind}"


def merge_accounts(numbers: list[dict], accounts: list[dict]) -> list[dict]:
    """One row per linked account: Schwab's account hash (from get_account_numbers) joined with its type and balances (from get_accounts)."""
    balances: dict[str, dict] = {}
    for item in accounts or []:
        acct = item.get("securitiesAccount", item) if isinstance(item, dict) else {}
        balances[str(acct.get("accountNumber", ""))] = acct
    rows = []
    for entry in numbers or []:
        number = str(entry.get("accountNumber", ""))
        acct = balances.get(number, {})
        bal = acct.get("currentBalances") or {}
        rows.append({
            "index": len(rows) + 1,
            "hash_value": entry.get("hashValue", ""),
            "last4": number[-4:],
            "type": str(acct.get("type") or ""),
            "equity": _number(bal.get("liquidationValue"), _number(bal.get("equity"))),
            "cash": _number(bal.get("cashBalance")),
            "buying_power": _number(bal.get("buyingPower"), _number(bal.get("availableFunds"))),
        })
    return rows


def _number(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:,.2f}"


def format_table(rows: list[dict]) -> str:
    lines = [f"  {'#':>2}  {'account':<10} {'type':<8} {'total value':>14} {'cash':>14} {'buying power':>14}"]
    for r in rows:
        lines.append(f"  {r['index']:>2}  ...{r['last4']:<7} {r['type'] or '?':<8} {_money(r['equity']):>14} {_money(r['cash']):>14} "
                     f"{_money(r['buying_power']):>14}")
    return "\n".join(lines)


def choose(rows: list[dict], input_fn: Callable[[str], str] | None = None, print_fn: Callable[..., None] = print) -> dict | None:
    """Show the accounts, ask which one, and make the user type its last four digits to confirm. None = nothing selected."""
    input_fn = input_fn or input                         # looked up when called (a default argument would bind the builtin early)
    if not rows:
        print_fn("No linked accounts were returned for this login.")
        return None
    print_fn("\nLinked Schwab accounts:")
    print_fn(format_table(rows))
    answer = input_fn("\nWhich account should the bots trade? Number (or Enter to skip): ").strip()
    if not answer:
        return None
    if not (answer.isascii() and answer.isdigit() and len(answer) <= 4) or not 1 <= int(answer) <= len(rows):
        print_fn(f"'{answer}' is not one of the numbers 1-{len(rows)}: nothing selected.")
        return None
    row = rows[int(answer) - 1]
    typed = input_fn(f"Type the last 4 digits of account #{row['index']} (read them from the table above) to confirm: ").strip()
    if typed != row["last4"]:
        print_fn("That does not match: nothing selected.")
        return None
    return row


def save_for_all(row: dict, folders: list[Path], now: datetime | None = None, print_fn: Callable[..., None] = print) -> dict:
    """Write the selection into every given bot folder."""
    entry = {"hash_value": row["hash_value"], "last4": row["last4"], "type": row["type"],
             "selected_at": (now or datetime.now()).isoformat(timespec="seconds")}
    for folder in folders:
        write_selection(selection_path(folder), entry)
        print_fn(f"  saved: {selection_path(folder)}")
    return entry


def select_interactively(numbers: list[dict], accounts: list[dict], folders: list[Path],
                         input_fn: Callable[[str], str] | None = None, print_fn: Callable[..., None] = print,
                         now: datetime | None = None) -> dict | None:
    """The whole flow: list, choose, confirm, save into the given folders. Returns the saved entry (None when nothing was chosen)."""
    previous = read_selection(selection_path(folders[0])) if folders else None
    row = choose(merge_accounts(numbers, accounts), input_fn, print_fn)
    if row is None:
        print_fn("\nNothing was changed: " + (f"the saved choice stays {describe(previous)}." if previous
                                                else "no account is selected, so LIVE orders stay refused."))
        return None
    entry = save_for_all(row, folders, now, print_fn)
    print_fn(f"\nSelected {describe(entry)}. Live orders from this bot (Schwab 0DTE) will go to this account only.")
    print_fn("A bot that is already running keeps the account it started with and opens nothing new until it is restarted.")
    return entry


def after_login(client: Any, here: Path, interactive: bool, no_select: bool = False, input_fn: Callable[[str], str] | None = None,
                print_fn: Callable[..., None] = print) -> int:
    """What schwab_login.py does once the token is saved: list the linked accounts and, in a terminal, let the owner pick the trading account.
    Returns the process exit code (1 when the account lookup itself failed)."""
    resp = client.get_account_numbers()
    if resp.status_code != 200:
        print_fn(f"Token written, but account lookup returned HTTP {resp.status_code}: {resp.text[:200]}")
        return 1
    numbers = resp.json()
    print_fn(f"{len(numbers)} linked account(s).")
    if no_select or not interactive:
        print_fn("Choose the account the bots trade with:  python schwab_select_account.py   (live orders need it).")
        return 0
    details: list = []
    try:
        listing = client.get_accounts()
        details = listing.json() if listing.status_code == 200 else []
    except Exception:                                    # noqa: BLE001 -- balances are a courtesy; the numbers alone are enough to choose
        details = []
    try:
        select_interactively(numbers, details, [Path(here)], input_fn, print_fn)
    except (EOFError, KeyboardInterrupt, OSError) as exc:      # the token is already saved: a failed prompt must not look like a failed login
        print_fn(f"\nAccount choice skipped ({type(exc).__name__}): nothing was changed. Run  python schwab_select_account.py  to choose.")
    return 0
