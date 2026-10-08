"""Schwab refresh-token health: the 7-day expiry is checked up front instead of discovered mid-session."""

import json
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import schwab_health as sh  # noqa: E402

ET = ZoneInfo("US/Eastern")
LOGIN = datetime(2026, 10, 1, 10, 0, tzinfo=ET)


def token_file(tmp_path, created=LOGIN, extra=None):
    path = tmp_path / "token.json"
    body = {"creation_timestamp": created.timestamp(), "token": {"access_token": "SECRET", "refresh_token": "SECRET"}}
    path.write_text(json.dumps({**body, **(extra or {})}), encoding="utf-8")
    return path


def test_a_missing_file_is_missing(tmp_path):
    out = sh.assess(tmp_path / "nope.json", LOGIN)
    assert out["state"] == "MISSING" and "no token file" in out["message"]


def test_an_unreadable_or_timestampless_file_warns_instead_of_guessing(tmp_path):
    bad = tmp_path / "token.json"
    bad.write_text("not json", encoding="utf-8")
    assert sh.token_created_at(bad) is None and sh.assess(bad, LOGIN)["state"] == "WARN"
    bad.write_text(json.dumps({"token": {}}), encoding="utf-8")
    assert sh.assess(bad, LOGIN)["state"] == "WARN"


@pytest.mark.parametrize("days, state", [(1 / 24, "OK"), (3, "OK"), (5, "WARN"), (6.5, "WARN"), (6.97, "EXPIRING"),
                                         (7.0, "DEAD"), (9, "DEAD")])
def test_the_state_follows_the_age_of_the_token(tmp_path, days, state):
    path = token_file(tmp_path)
    now = LOGIN + timedelta(days=days)
    assert sh.assess(path, now, hard_stop=time(13, 0))["state"] == state


def test_a_token_that_dies_before_the_hard_stop_refuses_entries_that_morning(tmp_path):
    path = token_file(tmp_path)                                    # logged in Thursday 10:00, expires next Thursday 10:00
    day7_open = LOGIN + timedelta(days=7) - timedelta(hours=0, minutes=30)      # 09:30 on day 7, before the expiry minute
    out = sh.assess(path, day7_open, hard_stop=time(13, 0), margin_min=30)
    assert out["state"] == "EXPIRING" and "before today's hard stop" in out["message"]
    day6_open = LOGIN + timedelta(days=6) - timedelta(minutes=30)               # 09:30 on day 6: the token outlives 13:30
    assert sh.assess(path, day6_open, hard_stop=time(13, 0), margin_min=30)["state"] == "WARN"


def test_the_message_names_the_age_and_the_expiry_and_carries_the_relogin_command(tmp_path):
    out = sh.assess(token_file(tmp_path), LOGIN + timedelta(days=5, hours=2), relogin="cd 0dte && python schwab_login.py --force")
    assert "5.1 days old" in out["message"] and "expires Thu Oct 08 10:00 ET" in out["message"]
    assert out["relogin"] == "cd 0dte && python schwab_login.py --force" and out["age_days"] == pytest.approx(5.08, abs=0.01)


def test_only_the_creation_timestamp_is_read(tmp_path):
    out = sh.assess(token_file(tmp_path), LOGIN + timedelta(days=1))
    assert "SECRET" not in json.dumps(out)
