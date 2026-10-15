"""The account selector (schwab_accounts.py, schwab_select_account.py, the step after schwab_login.py), 2026-10-15."""

from __future__ import annotations

import json
import logging
from datetime import datetime

import pytest

import schwab_accounts as sa
import schwab_select_account as tool
from broker import Broker
from test_broker_schwab import FakeClient, _resp, make_config

NUMBERS = [{"accountNumber": "12345678", "hashValue": "HASH0"}, {"accountNumber": "87654321", "hashValue": "HASH1"}]
DETAILS = [
    {"securitiesAccount": {"accountNumber": "12345678", "type": "MARGIN",
                           "currentBalances": {"liquidationValue": 25000.5, "cashBalance": 12000.0, "buyingPower": 50000.0}}},
    {"securitiesAccount": {"accountNumber": "87654321", "type": "CASH",
                           "currentBalances": {"liquidationValue": 4100.0, "cashBalance": 4100.0, "availableFunds": 4100.0}}},
]


def answers(*given):
    """An input() that returns the given answers in order and records the prompts."""
    queue = list(given)
    asked: list[str] = []

    def ask(prompt: str = "") -> str:
        asked.append(prompt)
        return queue.pop(0)
    ask.asked = asked
    return ask


def test_merge_joins_the_hash_with_the_type_and_balances():
    rows = sa.merge_accounts(NUMBERS, DETAILS)
    assert [(r["index"], r["hash_value"], r["last4"], r["type"]) for r in rows] == [(1, "HASH0", "5678", "MARGIN"), (2, "HASH1", "4321", "CASH")]
    assert rows[0]["equity"] == 25000.5 and rows[0]["cash"] == 12000.0 and rows[0]["buying_power"] == 50000.0
    assert rows[1]["buying_power"] == 4100.0                                       # availableFunds when there is no buyingPower
    bare = sa.merge_accounts(NUMBERS, [])                                         # no balances: the accounts can still be chosen
    assert [(r["last4"], r["type"], r["equity"]) for r in bare] == [("5678", "", None), ("4321", "", None)]


def test_the_table_shows_only_the_last_four_digits():
    table = sa.format_table(sa.merge_accounts(NUMBERS, DETAILS))
    assert "...5678" in table and "...4321" in table and "$25,000.50" in table and "MARGIN" in table
    assert "12345678" not in table and "87654321" not in table


def test_choosing_needs_the_number_and_then_the_last_four_digits():
    rows = sa.merge_accounts(NUMBERS, DETAILS)
    ask = answers("2", "4321")
    assert sa.choose(rows, ask, lambda *a, **k: None)["hash_value"] == "HASH1"
    assert "4321" not in ask.asked[1] and "#2" in ask.asked[1]                    # it must be read from the table, not retyped from the prompt
    for given in (("2", "9999"), ("2", ""), ("3",), ("0",), ("x",), ("",), ("-1",), ("\u00b2",), ("\u0662",), ("1" * 5000,), ("00002",)):
        assert sa.choose(rows, answers(*given), lambda *a, **k: None) is None, given
    assert sa.choose([], answers(), lambda *a, **k: None) is None


def test_the_selection_file_roundtrip_and_what_counts_as_missing(tmp_path):
    path = sa.selection_path(tmp_path)
    assert path.name == "selected_account.json" and sa.read_selection(path) is None
    sa.write_selection(path, {"hash_value": "H", "last4": "1234", "type": "MARGIN", "selected_at": "t"})
    assert sa.read_selection(path)["hash_value"] == "H"
    for bad in ("{not json", "[]", json.dumps({"hash_value": "H"}), json.dumps({"last4": "1234"}), json.dumps({"hash_value": "", "last4": "1"})):
        path.write_text(bad)
        assert sa.read_selection(path) is None, bad
    assert sa.describe({"last4": "1234", "type": "MARGIN"}) == "...1234 MARGIN" and sa.describe({"last4": "1234"}) == "...1234"


def test_select_interactively_saves_and_a_declined_choice_saves_nothing(tmp_path):
    out: list[str] = []
    entry = sa.select_interactively(NUMBERS, DETAILS, [tmp_path], answers("1", "5678"), lambda *a, **k: out.append(" ".join(map(str, a))),
                                    now=datetime(2026, 10, 15, 8, 30, 0))
    saved = sa.read_selection(sa.selection_path(tmp_path))
    assert entry == saved == {"hash_value": "HASH0", "last4": "5678", "type": "MARGIN", "selected_at": "2026-10-15T08:30:00"}
    assert any("Selected ...5678 MARGIN" in line for line in out) and any("restarted" in line for line in out)
    assert not list(tmp_path.glob("*.tmp"))                                       # written atomically, nothing left behind
    other = tmp_path / "other"
    other.mkdir()
    said: list[str] = []
    assert sa.select_interactively(NUMBERS, DETAILS, [other], answers(""), lambda *a, **k: said.append(" ".join(map(str, a)))) is None
    assert sa.read_selection(sa.selection_path(other)) is None
    assert any("no account is selected, so LIVE orders stay refused" in line for line in said)
    said.clear()
    assert sa.select_interactively(NUMBERS, DETAILS, [tmp_path], answers("2", "9999"), lambda *a, **k: said.append(" ".join(map(str, a)))) is None
    assert any("the saved choice stays ...5678 MARGIN" in line for line in said)             # the old choice, named, stays in force
    assert sa.read_selection(sa.selection_path(tmp_path))["last4"] == "5678"


class LoginClient:
    def __init__(self, status=200, details_status=200):
        self.status, self.details_status = status, details_status

    def get_account_numbers(self):
        return _resp(self.status, NUMBERS if self.status == 200 else {"message": "no"})

    def get_accounts(self):
        if self.details_status == "raise":
            raise RuntimeError("balances unavailable")
        return _resp(self.details_status, DETAILS)


def test_after_login_offers_the_choice_in_a_terminal(tmp_path):
    out: list[str] = []
    code = sa.after_login(LoginClient(), tmp_path, interactive=True, input_fn=answers("2", "4321"), print_fn=lambda *a, **k: out.append(" ".join(map(str, a))))
    assert code == 0 and sa.read_selection(sa.selection_path(tmp_path))["last4"] == "4321"
    assert any("2 linked account(s)" in line for line in out)


@pytest.mark.parametrize("kwargs", [{"interactive": False}, {"interactive": True, "no_select": True}])
def test_after_login_without_a_terminal_or_with_no_select_only_points_to_the_selector(tmp_path, kwargs):
    out: list[str] = []
    code = sa.after_login(LoginClient(), tmp_path, input_fn=answers(), print_fn=lambda *a, **k: out.append(" ".join(map(str, a))), **kwargs)
    assert code == 0 and sa.read_selection(sa.selection_path(tmp_path)) is None
    assert any("schwab_select_account.py" in line for line in out)


def test_after_login_survives_missing_balances_and_reports_a_failed_lookup(tmp_path):
    assert sa.after_login(LoginClient(details_status="raise"), tmp_path, interactive=True, input_fn=answers("1", "5678"), print_fn=lambda *a, **k: None) == 0
    assert sa.read_selection(sa.selection_path(tmp_path))["last4"] == "5678"
    out: list[str] = []
    assert sa.after_login(LoginClient(status=401), tmp_path / "x", interactive=True, input_fn=answers(), print_fn=lambda *a, **k: out.append(" ".join(map(str, a)))) == 1
    assert any("HTTP 401" in line for line in out)


class Listing(FakeClient):
    def get_accounts(self, *, fields=None):
        self._rec("get_accounts")
        return _resp(200, DETAILS)


def test_the_selector_tool_lists_the_accounts_and_saves_the_choice_here_only(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "HERE", tmp_path / "0dte")
    (tmp_path / "0dte").mkdir()
    (tmp_path / "45DTE").mkdir()
    (tmp_path / "45DTE" / "schwab_login.py").write_text("")                          # a sibling bot folder: it is left alone
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")
    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("test.sel"))
    b._client = Listing()
    out: list[str] = []
    assert tool.main([], broker_factory=lambda: b, input_fn=answers("2", "4321"), print_fn=lambda *a, **k: out.append(" ".join(map(str, a)))) == 0
    assert sa.read_selection(sa.selection_path(tmp_path / "0dte"))["hash_value"] == "HASH1"
    assert not sa.selection_path(tmp_path / "45DTE").exists()
    assert any("MARGIN" in line for line in out)
    show: list[str] = []
    assert tool.main(["--show"], print_fn=lambda *a, **k: show.append(" ".join(map(str, a)))) == 0
    assert "...4321" in show[0]


def test_the_selector_tool_declined_or_failing_changes_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "HERE", tmp_path)
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")
    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("test.sel"))
    b._client = Listing()
    assert tool.main([], broker_factory=lambda: b, input_fn=answers("2", "0000"), print_fn=lambda *a, **k: None) == 1
    assert sa.read_selection(sa.selection_path(tmp_path)) is None
    out: list[str] = []

    def broken():
        raise RuntimeError("token rejected")
    assert tool.main([], broker_factory=broken, input_fn=answers(), print_fn=lambda *a, **k: out.append(" ".join(map(str, a)))) == 1
    assert "Could not list the Schwab accounts: token rejected" in out[0]
    show: list[str] = []
    assert tool.main(["--show"], print_fn=lambda *a, **k: show.append(a[0])) == 0
    assert "No account selected yet" in show[0]


def test_the_selection_file_is_git_ignored():
    import subprocess
    root = tool.HERE.parents[1] if (tool.HERE.parents[1] / ".git").exists() else None
    if root is None:
        pytest.skip("not inside the git checkout")
    try:
        out = subprocess.run(["git", "check-ignore", "-q", "schwab/0dte/selected_account.json"], cwd=str(root), capture_output=True)
    except FileNotFoundError:
        pytest.skip("git is not installed")
    if out.returncode not in (0, 1):
        pytest.skip("git could not answer here")                                      # e.g. 128: 'dubious ownership' on a copied checkout
    assert out.returncode == 0                                                       # it holds Schwab's account hash


def _login_module(tmp_path, monkeypatch, client):
    """schwab_login.py with its token flow replaced by `client` and every path inside tmp_path (it must never touch the real folder)."""
    import importlib.util
    from pathlib import Path
    import schwab.auth
    spec = importlib.util.spec_from_file_location("schwab_login_golive_test", Path(tool.__file__).resolve().parent / "schwab_login.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "HERE", tmp_path)
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", str(tmp_path / "token.json"))
    monkeypatch.setattr(schwab.auth, "client_from_manual_flow", lambda **kw: client)
    return module


def test_the_login_script_offers_the_account_choice_right_after_the_login(tmp_path, monkeypatch, capsys):
    module = _login_module(tmp_path, monkeypatch, LoginClient())
    monkeypatch.setattr(module.sys, "stdin", type("Tty", (), {"isatty": lambda self: True})())
    ask = answers("1", "5678")
    monkeypatch.setattr("builtins.input", ask)
    assert module.main(["--force"]) == 0
    assert sa.read_selection(sa.selection_path(tmp_path))["last4"] == "5678"
    assert "Success. Token saved" in capsys.readouterr().out


def test_the_login_script_skips_the_choice_with_no_select_or_without_a_terminal(tmp_path, monkeypatch, capsys):
    module = _login_module(tmp_path, monkeypatch, LoginClient())
    monkeypatch.setattr(module.sys, "stdin", type("Tty", (), {"isatty": lambda self: True})())
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("no question wanted"))
    assert module.main(["--force", "--no-select"]) == 0
    monkeypatch.setattr(module.sys, "stdin", type("Pipe", (), {"isatty": lambda self: False})())
    assert module.main(["--force"]) == 0
    assert sa.read_selection(sa.selection_path(tmp_path)) is None
    assert "schwab_select_account.py" in capsys.readouterr().out


def test_the_login_script_reports_a_failed_account_lookup_as_a_failure(tmp_path, monkeypatch):
    module = _login_module(tmp_path, monkeypatch, LoginClient(status=401))
    assert module.main(["--force"]) == 1



def test_a_prompt_that_dies_after_the_login_is_not_a_failed_login(tmp_path):
    def eof(prompt=""):
        raise EOFError

    out: list[str] = []
    assert sa.after_login(LoginClient(), tmp_path, interactive=True, input_fn=eof, print_fn=lambda *a, **k: out.append(" ".join(map(str, a)))) == 0
    assert any("Account choice skipped (EOFError)" in line for line in out) and sa.read_selection(sa.selection_path(tmp_path)) is None

    def interrupt(prompt=""):
        raise KeyboardInterrupt

    out.clear()
    assert sa.after_login(LoginClient(), tmp_path, interactive=True, input_fn=interrupt, print_fn=lambda *a, **k: out.append(" ".join(map(str, a)))) == 0
    assert any("KeyboardInterrupt" in line for line in out)


def test_the_selector_tool_cancelled_or_without_balances(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "HERE", tmp_path)
    monkeypatch.setenv("SCHWAB_LIVE_ORDERS", "false")

    class NoBalances(FakeClient):
        def get_accounts(self, *, fields=None):
            return _resp(500, {"message": "down"})

    b = Broker(make_config(tmp_path), dry_run=True, log=logging.getLogger("test.sel"))
    b._client = NoBalances()
    b._sleep = lambda s: None
    assert tool.main([], broker_factory=lambda: b, input_fn=answers("1", "5678"), print_fn=lambda *a, **k: None) == 0       # the accounts alone suffice
    assert sa.read_selection(sa.selection_path(tmp_path))["last4"] == "5678"

    def interrupt(prompt=""):
        raise KeyboardInterrupt

    out: list[str] = []
    assert tool.main([], broker_factory=lambda: b, input_fn=interrupt, print_fn=lambda *a, **k: out.append(" ".join(map(str, a)))) == 1
    assert any("Cancelled: nothing was changed" in line for line in out)
    assert sa.read_selection(sa.selection_path(tmp_path))["last4"] == "5678"


def test_the_selection_is_written_through_a_temp_file_and_one_atomic_replace(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    calls = []
    real = os.replace

    def spy(src, dst):
        calls.append((Path(src).name, Path(dst).name))
        return real(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    sa.write_selection(tmp_path / "selected_account.json", {"hash_value": "H", "last4": "1234", "type": "", "selected_at": "t"})
    assert calls == [("selected_account.json.tmp", "selected_account.json")]
    assert sa.read_selection(tmp_path / "selected_account.json")["hash_value"] == "H"
