"""Strict paper/live selection from the environment (2026-10-08): a typo must never switch the bot to the live API."""

import pytest

import config as config


@pytest.mark.parametrize("raw, expected", [("true", True), ("TRUE", True), (" Yes ", True), ("1", True), ("on", True),
                                           ("false", False), ("False", False), ("no", False), ("0", False), ("off", False)])
def test_recognised_values(monkeypatch, raw, expected):
    monkeypatch.setenv("SOME_FLAG", raw)
    assert config._env_flag("SOME_FLAG", True) is expected


@pytest.mark.parametrize("raw", [None, "", "   "])
def test_unset_or_blank_means_the_default_which_is_paper(monkeypatch, raw):
    if raw is None:
        monkeypatch.delenv("SOME_FLAG", raising=False)
    else:
        monkeypatch.setenv("SOME_FLAG", raw)
    assert config._env_flag("SOME_FLAG", True) is True
    assert config._env_flag("SOME_FLAG", False) is False


@pytest.mark.parametrize("raw", ["ture", "paper", "live", "flase", "2", "y"])
def test_anything_else_raises_instead_of_going_live(monkeypatch, raw):
    monkeypatch.setenv("SOME_FLAG", raw)
    with pytest.raises(ValueError, match="SOME_FLAG"):
        config._env_flag("SOME_FLAG", True)


def test_the_shipped_flag_is_a_real_boolean():
    assert isinstance(config.PAPER_TRADING, bool)


def _import_config_with(value):
    """Import the shipped config in a fresh interpreter with the flag set (no module reloads in the test process)."""
    import os
    import subprocess
    import sys
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1]
    env = {**os.environ, "PAPER_TRADING": value}
    env.pop("ALPACA_BASE_URL", None)
    return subprocess.run([sys.executable, "-c", "import config; print(config.PAPER_TRADING, config.ALPACA_BASE_URL)"],
                          cwd=folder, env=env, capture_output=True, text=True, timeout=60)


def test_a_typo_in_the_flag_stops_the_bot_at_import_instead_of_going_live():
    result = _import_config_with("ture")
    assert result.returncode != 0 and "PAPER_TRADING" in result.stderr and "ValueError" in result.stderr


def test_the_flag_selects_the_matching_endpoint_when_it_is_spelled_correctly():
    paper, live = _import_config_with("true"), _import_config_with("false")
    assert paper.returncode == 0 and paper.stdout.split() == ["True", "https://paper-api.alpaca.markets"]
    assert live.returncode == 0 and live.stdout.split() == ["False", "https://api.alpaca.markets"]
