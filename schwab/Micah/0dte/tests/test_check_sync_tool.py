"""tools/check_sync.py: the clone check and --apply-clones must never touch a bot's runtime files (2026-10-26)."""

import importlib.util
from pathlib import Path

import pytest

TOOL = next((p / "tools" / "check_sync.py" for p in Path(__file__).resolve().parents if (p / "tools" / "check_sync.py").is_file()), None)
if TOOL is None:
    pytest.skip("not inside the full repository checkout", allow_module_level=True)


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("check_sync_under_test", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def layout(tmp_path):
    """primary and clone folders with code, data and the runtime files a bot leaves behind."""
    primary, clone = tmp_path / "schwab" / "Micah" / "0dte", tmp_path / "schwab" / "Dawson" / "0dte"
    for folder in (primary, clone):
        (folder / "logs").mkdir(parents=True)
        (folder / "data").mkdir()
        (folder / "broker.py").write_text("print('v1')\n")
        (folder / "data" / "universe.json").write_text("{}")
        (folder / ".env.example").write_text("EXAMPLE=" + folder.name)
        (folder / ".env").write_text("SECRET=" + folder.parent.name)
        (folder / "token.json").write_text('{"token": "' + folder.parent.name + '"}')
        (folder / "selected_account.json").write_text('{"hash_value": "' + folder.parent.name + '"}')
        (folder / "logs" / "state_bot.json").write_text('{"positions": "' + folder.parent.name + '"}')
        (folder / "logs" / "live_capital.json").write_text('{"cap": "' + folder.parent.name + '"}')
    return primary, clone


def code_lister(folder):
    """What git would list: code and data only."""
    return [Path("broker.py"), Path("data") / "universe.json", Path(".env.example")]


def runtime_of(folder):
    return {name: (folder / name).read_text() for name in (".env", "token.json", "selected_account.json", "logs/state_bot.json", "logs/live_capital.json")}


def test_the_guessed_listing_never_includes_runtime_files_or_secrets(tool, tmp_path):
    primary, _ = layout(tmp_path)
    guessed = {str(p) for p in tool.guessed_listing(primary)}
    assert guessed == {"broker.py", str(Path("data") / "universe.json")}


def test_apply_refuses_to_touch_anything_without_a_git_listing(tool, tmp_path, monkeypatch):
    primary, clone = layout(tmp_path)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    (primary / "broker.py").write_text("print('v2')\n")
    before = runtime_of(clone)
    done = tool.apply_clone(primary, clone, tmp_path / "archive", sweep=True, lister=lambda folder: None)
    assert len(done) == 1 and "NOTHING changed" in done[0]
    assert (clone / "broker.py").read_text() == "print('v1')\n" and runtime_of(clone) == before
    assert not (tmp_path / "archive").exists()


def test_apply_copies_code_and_leaves_runtime_files_and_the_env_example_alone(tool, tmp_path, monkeypatch):
    primary, clone = layout(tmp_path)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    (primary / "broker.py").write_text("print('v2')\n")
    before = runtime_of(clone)
    done = tool.apply_clone(primary, clone, tmp_path / "archive", lister=code_lister)
    assert done == ["copied broker.py"]
    assert (clone / "broker.py").read_text() == "print('v2')\n"
    assert (clone / ".env.example").read_text() == "EXAMPLE=0dte" and runtime_of(clone) == before


def test_extras_are_left_alone_unless_swept_and_then_archived_with_a_note(tool, tmp_path, monkeypatch):
    primary, clone = layout(tmp_path)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    (clone / "helper.py").write_text("x = 1\n")
    (clone / "start_dawson.cmd").write_text("python scheduler.py\n")                 # a launcher: never drift, never moved

    def lister(folder):                                                                 # what git would list: only files that exist
        base = code_lister(folder) + ([Path("helper.py"), Path("start_dawson.cmd")] if folder == clone else [])
        return [rel for rel in base if (folder / rel).exists()]
    assert any("left alone" in line and "helper.py" in line for line in tool.apply_clone(primary, clone, tmp_path / "archive", lister=lister))
    assert (clone / "helper.py").exists() and not (tmp_path / "archive").exists()
    done = tool.apply_clone(primary, clone, tmp_path / "archive", sweep=True, lister=lister)
    assert any("moved extra helper.py" in line for line in done)
    moved = tmp_path / "archive" / "schwab" / "Dawson" / "0dte" / "helper.py"
    assert moved.read_text() == "x = 1\n" and not (clone / "helper.py").exists()
    assert (clone / "start_dawson.cmd").exists()                                        # not a code/data drift
    note = (tmp_path / "archive" / "schwab" / "Dawson" / "0dte" / "README.txt").read_text()
    assert "helper.py" in note and "copy it back" in note
    problems = tool.compare_clone(primary, clone, lister=lister)
    assert problems == []                                                               # the .cmd is not reported either


def test_the_report_names_a_deleted_primary_file_instead_of_crashing(tool, tmp_path, monkeypatch):
    primary, clone = layout(tmp_path)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    (primary / "broker.py").unlink()                                                   # deleted with Explorer, still tracked
    problems = tool.compare_clone(primary, clone, lister=code_lister)
    assert problems == [f"tracked in git but missing on disk in {primary.relative_to(tmp_path)}: broker.py (git rm it, or restore it)"]
