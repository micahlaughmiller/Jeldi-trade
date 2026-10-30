"""tools/schwab_login_both.py: one login, copied into both Schwab bots' folders (2026-10-14)."""

import importlib.util
import json
import time
from datetime import datetime
from pathlib import Path

import pytest

TOOL = next((p / "tools" / "schwab_login_both.py" for p in Path(__file__).resolve().parents if (p / "tools" / "schwab_login_both.py").is_file()),
            Path(__file__).resolve().parents[3] / "tools" / "schwab_login_both.py")       # the bot folders sit at different depths
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
        out = subprocess.run(["git", "check-ignore", "-q", "schwab/Micah/45DTE/token.json.replaced-20261014-093005.token"], cwd=str(root), capture_output=True)
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


# ------------------------------------------------- 2026-10-26: tools/schwab_token_check.py knows the person-folder layout

@pytest.fixture(scope="module")
def token_check():
    path = TOOL.parent / "schwab_token_check.py"
    if not path.is_file():
        pytest.skip("not inside the full repository checkout")
    spec = importlib.util.spec_from_file_location("schwab_token_check_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def person_root(tmp_path, envs: dict, tokens=()):
    """tmp/schwab/<Person>/<bot> folders with the given .env SCHWAB_TOKEN_PATH values (None = no .env) and stray token files."""
    for rel in ("Micah/0dte", "Micah/45DTE", "Dawson/0dte", "Ronnie/45DTE", "45DTE"):
        (tmp_path / "schwab" / rel).mkdir(parents=True)
        (tmp_path / "schwab" / rel / "schwab_login.py").write_text("")
    for rel, value in envs.items():
        if value is not None:
            (tmp_path / "schwab" / rel / ".env").write_text("" if value == "" else f"SCHWAB_TOKEN_PATH={value}\n")
    for rel in tokens:
        (tmp_path / "schwab" / rel / "token.json").write_text("{}")
    return tmp_path


def test_the_routing_check_accepts_the_documented_layout(token_check, tmp_path):
    root = person_root(tmp_path, {"Micah/0dte": "", "Micah/45DTE": r"..\0dte\token.json", "Dawson/0dte": r"..\..\Micah\0dte\token.json",
                                  "Ronnie/45DTE": "", "45DTE": r"..\Micah\0dte\token.json"}, tokens=("Micah/0dte", "Ronnie/45DTE"))
    report = token_check.routing_report(root)
    assert [wrong for _, wrong in report] == [False] * 5, report
    assert all("[  ok  ]" in line for line, _ in report)


def test_the_routing_check_flags_a_second_login_a_and_a_cross_login_path(token_check, tmp_path):
    root = person_root(tmp_path, {"Micah/0dte": "", "Micah/45DTE": None, "Dawson/0dte": "", "Ronnie/45DTE": r"..\..\Micah\0dte\token.json"},
                       tokens=("Micah/0dte", "Dawson/0dte"))
    report = dict(token_check.routing_report(root))
    assert any("Dawson" in line and "WRONG" in line and "second login" in line for line in report)       # blank path, no siblings
    assert any("Ronnie" in line and "WRONG" in line and "should lead to" in line for line in report)    # login B pointed at login A
    assert any("Micah\\45DTE" in line and "no .env yet" in line for line in report)
    assert any(wrong for line, wrong in report.items() if "Dawson" in line)
    stray = person_root(tmp_path / "stray", {"Micah/0dte": "", "Dawson/0dte": r"..\..\Micah\0dte\token.json"},
                        tokens=("Micah/0dte", "Dawson/0dte"))                    # the right path, but a leftover private login beside it
    lines = token_check.routing_report(stray)
    assert any("Dawson" in line and "stray token.json" in line and wrong for line, wrong in lines)


def test_a_45dte_without_the_explicit_line_is_a_note_not_an_error(token_check, tmp_path):
    root = person_root(tmp_path, {"Micah/0dte": "", "Micah/45DTE": "", "Dawson/0dte": r"..\..\Micah\0dte\token.json", "Ronnie/45DTE": ""},
                       tokens=("Micah/0dte",))
    report = token_check.routing_report(root)
    note = [line for line, _ in report if "Micah\\45DTE" in line]
    assert note and "[ note ]" in note[0] and not [w for l, w in report if "Micah\\45DTE" in l][0]


def test_a_login_another_folder_routes_to_is_never_skipped(token_check, tmp_path):
    root = person_root(tmp_path, {"Micah/0dte": None, "Dawson/0dte": r"..\\..\\Micah\\0dte\\token.json"})      # Micah/0dte empty after a bad move
    routed = token_check.routed_targets(root)
    assert (root / "schwab" / "Micah" / "0dte" / "token.json").resolve() in routed
    assert (root / "schwab" / "Ronnie" / "45DTE" / "token.json").resolve() not in routed


def test_astra_routing_blank_right_and_wrong(token_check, tmp_path):
    root = person_root(tmp_path, {"Micah/0dte": ""}, tokens=("Micah/0dte",))
    astra = root / "Alpaca" / "0DTE"
    astra.mkdir(parents=True)
    (astra / ".env").write_text("")
    assert any("Alpaca" in line and "[  ok  ]" in line for line, _ in token_check.routing_report(root))
    (astra / ".env").write_text("SCHWAB_TOKEN_PATH=" + str(root / "schwab" / "Micah" / "0dte" / "token.json") + "\n")
    assert any("Alpaca" in line and "[  ok  ]" in line for line, _ in token_check.routing_report(root))
    (astra / ".env").write_text("SCHWAB_TOKEN_PATH=" + str(root / "schwab" / "0dte") + "\n")                    # the archived old folder
    report = token_check.routing_report(root)
    assert any("Alpaca" in line and "WRONG" in line and wrong for line, wrong in report)


def test_a_quoted_value_with_a_hidden_tab_is_made_visible(token_check, tmp_path):
    root = person_root(tmp_path, {"Micah/0dte": "", "Dawson/0dte": '"..\\..\\Micah\\0dte\\token.json"'}, tokens=("Micah/0dte",))
    report = token_check.routing_report(root)
    bad = [line for line, wrong in report if "Dawson" in line and wrong]
    assert bad and "hidden character" in bad[0] and "WITHOUT quotes" in bad[0]


def test_the_paper_45dte_folder_must_point_at_login_a(token_check, tmp_path):
    """2026-10-30: schwab/45DTE (the paper bot) has no sibling 0dte folder any more, so a blank SCHWAB_TOKEN_PATH would make it a
    separate login A; the documented line sends it to Micah/0dte."""
    root = person_root(tmp_path, {"Micah/0dte": "", "45DTE": ""}, tokens=("Micah/0dte",))
    report = token_check.routing_report(root)
    assert any(line.startswith("  [WRONG ] schwab\\45DTE") and "second login" in line and wrong for line, wrong in report)
    root = person_root(tmp_path / "ok", {"Micah/0dte": "", "45DTE": r"..\\Micah\\0dte\\token.json"}, tokens=("Micah/0dte",))
    report = token_check.routing_report(root)
    assert any(line.startswith("  [  ok  ] schwab\\45DTE") and not wrong for line, wrong in report)
