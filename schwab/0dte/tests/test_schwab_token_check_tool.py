"""tools/schwab_token_check.py: which token file does each bot read, and do they share one (2026-10-12)."""

import importlib.util
import json
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[3] / "tools" / "schwab_token_check.py"
if not TOOL.is_file():
    pytest.skip("not inside the full repository checkout", allow_module_level=True)


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("schwab_token_check_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def token(path, age_days=1.0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"creation_timestamp": int(time.time() - age_days * 86400), "token": {"expires_at": 1}}))


def layout(tmp_path):
    for sub in ("Alpaca/0DTE", "schwab/0dte", "schwab/45DTE"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return (("ASTRA", tmp_path / "Alpaca/0DTE", tmp_path / "schwab/0dte/token.json"),
            ("Sim 0DTE", tmp_path / "schwab/0dte", tmp_path / "schwab/0dte/token.json"),
            ("Sim 45DTE", tmp_path / "schwab/45DTE", tmp_path / "schwab/45DTE/token.json"))


def test_each_bot_resolves_its_token_the_way_the_bots_do(tool, tmp_path):
    bots = layout(tmp_path)
    env45 = tmp_path / "schwab/45DTE/.env"
    assert tool.resolve(bots[0][1], bots[0][2], {}) == (tmp_path / "schwab/0dte/token.json", "default")
    assert tool.resolve(bots[2][1], bots[2][2], {}) == (tmp_path / "schwab/45DTE/token.json", "default")
    env45.write_text(f"SCHWAB_TOKEN_PATH={tmp_path / 'schwab/0dte/token.json'}\n")
    assert tool.resolve(bots[2][1], bots[2][2], {}) == (tmp_path / "schwab/0dte/token.json", ".env")
    assert tool.resolve(bots[2][1], bots[2][2], {"SCHWAB_TOKEN_PATH": str(tmp_path / "x.json")}) == \
        (tmp_path / "x.json", "Windows environment variable")                       # the environment beats the .env
    env45.write_text(f"SCHWAB_TOKEN_PATH={tmp_path / 'schwab/0dte'}\n")              # a FOLDER: its token.json
    assert tool.resolve(bots[2][1], bots[2][2], {})[0] == tmp_path / "schwab/0dte/token.json"
    env45.write_text("SCHWAB_TOKEN_PATH=mine.json\n")                                # relative: to the bot folder
    assert tool.resolve(bots[2][1], bots[2][2], {})[0] == tmp_path / "schwab/45DTE/mine.json"
    env45.write_text("SCHWAB_TOKEN_PATH=\n")                                         # blank: the default
    assert tool.resolve(bots[2][1], bots[2][2], {}) == (tmp_path / "schwab/45DTE/token.json", "default")


def test_separate_logins_are_called_out_and_fail_the_check(tool, tmp_path, capsys):
    bots = layout(tmp_path)
    token(tmp_path / "schwab/0dte/token.json")
    token(tmp_path / "schwab/45DTE/token.json")
    assert tool.main([], bots=bots, environ={}) == 1
    out = capsys.readouterr().out
    assert "PROBLEM" in out and "DIFFERENT token files" in out and "Sim 45DTE" in out and "SCHWAB_TOKEN_PATH=<full path" in out


def test_one_shared_file_is_fine(tool, tmp_path, capsys):
    bots = layout(tmp_path)
    token(tmp_path / "schwab/0dte/token.json", age_days=0.1)
    (tmp_path / "schwab/45DTE/.env").write_text(f"SCHWAB_TOKEN_PATH={tmp_path / 'schwab/0dte'}\n")       # the folder form is accepted
    assert tool.main([], bots=bots, environ={}) == 0
    out = capsys.readouterr().out
    assert "All bots read one token file (1 distinct)" in out and "PROBLEM" not in out and "(.env)" in out


def test_a_dead_or_missing_login_fails_the_check_and_names_the_folder_to_log_in_from(tool, tmp_path, capsys):
    bots = layout(tmp_path)
    token(tmp_path / "schwab/0dte/token.json", age_days=8)
    (tmp_path / "schwab/45DTE/.env").write_text(f"SCHWAB_TOKEN_PATH={tmp_path / 'schwab/0dte/token.json'}\n")
    assert tool.main([], bots=bots, environ={}) == 1
    out = capsys.readouterr().out
    assert "DEAD" in out and f'cd "{tmp_path / "schwab/0dte"}" && python schwab_login.py --force' in out
    (tmp_path / "schwab/0dte/token.json").unlink()
    assert tool.main([], bots=bots, environ={}) == 1 and "MISSING" in capsys.readouterr().out


def test_explicit_paths_are_checked_alone(tool, tmp_path, capsys):
    token(tmp_path / "a/token.json", age_days=0.5)
    assert tool.main(["--path", str(tmp_path / "a/token.json")], bots=(), environ={}) == 0
    assert "PROBLEM" not in capsys.readouterr().out
