"""tools/schwab_login_both.py: one login, copied into both Schwab bots' folders (2026-10-14)."""

import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[3] / "tools" / "schwab_login_both.py"
if not TOOL.is_file():
    pytest.skip("not inside the full repository checkout", allow_module_level=True)


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("schwab_login_both_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def write(folder, stamp, secret="s"):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "token.json").write_text(json.dumps({"creation_timestamp": stamp, "token": {"refresh_token": secret}}))


def layout(tmp_path, source_stamp=None, target_stamp=None):
    source, target = tmp_path / "schwab" / "0dte", tmp_path / "schwab" / "45DTE"
    source.mkdir(parents=True)
    target.mkdir(parents=True)
    if source_stamp is not None:
        write(source, source_stamp, "SOURCE-SECRET")
    if target_stamp is not None:
        write(target, target_stamp, "OLD-TARGET-SECRET")
    return source, target


def test_a_new_login_is_copied_over_the_45dte_file_and_the_old_one_is_kept(tool, tmp_path, capsys):
    source, target = layout(tmp_path, source_stamp=1_000, target_stamp=500)
    now = time.time()
    logins = []

    def login(folder):
        logins.append(folder)
        write(folder, now, "NEW-SECRET")                                         # schwab_login.py rewrote the 0dte token
        return 0
    assert tool.main([], source=source, targets=(target,), login=login) == 0
    assert logins == [source]
    assert json.loads((target / "token.json").read_text())["token"]["refresh_token"] == "NEW-SECRET"
    assert tool.created(target / "token.json") == tool.created(source / "token.json")          # the same login in both folders
    kept = list(target.glob("token.json.replaced-*.token"))
    assert len(kept) == 1 and "OLD-TARGET-SECRET" in kept[0].read_text()                          # the replaced file is kept, gitignore-shaped
    out = capsys.readouterr().out
    assert "Copied the" in out and "SECRET" not in out                                           # nothing secret is printed


def test_a_login_that_failed_or_did_not_rewrite_the_token_copies_nothing(tool, tmp_path, capsys):
    source, target = layout(tmp_path, source_stamp=1_000, target_stamp=500)
    assert tool.main([], source=source, targets=(target,), login=lambda folder: 1) == 1
    assert tool.created(target / "token.json") == 500
    assert tool.main([], source=source, targets=(target,), login=lambda folder: 0) == 1          # exit 0 but the file is unchanged
    assert tool.created(target / "token.json") == 500 and not list(target.glob("*.replaced-*"))
    assert "nothing was copied" in capsys.readouterr().err


def test_no_login_flag_only_copies_what_is_there(tool, tmp_path):
    source, target = layout(tmp_path, source_stamp=2_000, target_stamp=None)
    assert tool.main(["--no-login"], source=source, targets=(target,), login=lambda folder: pytest.fail("no login wanted")) == 0
    assert tool.created(target / "token.json") == 2_000 and not list(target.glob("*.replaced-*"))   # nothing to keep: there was no file


def test_a_missing_source_token_copies_nothing(tool, tmp_path, capsys):
    source, target = layout(tmp_path, source_stamp=None, target_stamp=500)
    assert tool.main(["--no-login"], source=source, targets=(target,)) == 1
    assert tool.created(target / "token.json") == 500 and "No usable token file" in capsys.readouterr().err


def test_helpers(tool, tmp_path):
    assert tool.created(tmp_path / "nope.json") is None
    (tmp_path / "bad.json").write_text("{not json")
    assert tool.created(tmp_path / "bad.json") is None
    assert tool.when(None) == "none" and tool.when(0).startswith("19")
    kept = tool.copy_login(layout(tmp_path / "x", 1, 2)[0].parent / "0dte", (tmp_path / "x" / "schwab" / "45DTE"), now=datetime(2026, 10, 14, 9, 30, 5))
    assert kept.name == "token.json.replaced-20261014-093005.token"


def test_the_kept_file_name_is_gitignored():
    import subprocess
    root = TOOL.parents[1]
    if not (root / ".git").exists():
        pytest.skip("not inside the git checkout")
    try:
        out = subprocess.run(["git", "check-ignore", "-q", "schwab/45DTE/token.json.replaced-20261014-093005.token"], cwd=str(root), capture_output=True)
    except FileNotFoundError:
        pytest.skip("git is not installed")
    assert out.returncode == 0                                                                    # the kept copy holds a live refresh token


def test_a_login_that_wrote_a_token_but_reported_failure_copies_nothing(tool, tmp_path, capsys):
    source, target = layout(tmp_path, source_stamp=1_000, target_stamp=500)

    def login(folder):
        write(folder, time.time(), "NEW-SECRET")          # schwab_login.py: "Token written, but account lookup returned HTTP 401" -> exit 1
        return 1
    assert tool.main([], source=source, targets=(target,), login=login) == 1
    assert tool.created(target / "token.json") == 500 and "did not finish" in capsys.readouterr().err
