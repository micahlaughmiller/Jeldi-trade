"""ASTRA owns the Schwab token: the keeper thread refreshes the access token, writes it atomically, never touches a newer login, and
backs off sensibly (2026-10-09). All HTTP is a fake handler on httpx's transport and real sockets are blocked; token values are fake."""

import base64
import json
import logging
import socket
import time
from datetime import datetime
from urllib.parse import parse_qs

import httpx
import pytest

import config
import market_data
import schwab_keeper as keeper
from conftest import et

LOGIN = int(time.time()) - 2 * 86400            # a login two days ago


class Http:
    """The fake Schwab token endpoint: `handler` answers each request; `requests` records them."""
    handler = None
    requests: list = []


def ok_handler(refresh="FAKE-R0"):
    count = [0]

    def handle(request):
        count[0] += 1
        return httpx.Response(200, json={"access_token": f"FAKE-A{count[0]}", "refresh_token": refresh, "id_token": "FAKE-ID",
                                         "expires_in": 1800, "token_type": "Bearer", "scope": "api"})
    return handle


def raising(cls):
    def handle(request):
        raise cls("boom", request=request)
    return handle


@pytest.fixture(autouse=True)
def harness(monkeypatch, tmp_path):
    keeper.reset_state()
    path = tmp_path / "token.json"
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(path))
    monkeypatch.setattr(config, "SCHWAB_APP_KEY", "KEY")
    monkeypatch.setattr(config, "SCHWAB_APP_SECRET", "SECRET")
    monkeypatch.setattr(config, "SCHWAB_TOKEN_KEEPER", True, raising=False)

    def no_net(*a, **k):
        raise AssertionError("a real network attempt was blocked")
    monkeypatch.setattr(socket, "getaddrinfo", no_net)
    monkeypatch.setattr(socket.socket, "connect", no_net)
    Http.handler, Http.requests = ok_handler(), []

    def handle_request(self, request):
        Http.requests.append(request)
        return Http.handler(request)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
    yield path
    keeper.reset_state()


def write_token(path, remaining=120, login=LOGIN, refresh="FAKE-R0", access="FAKE-A0", at=None):
    base = time.time() if at is None else at
    path.write_text(json.dumps({"creation_timestamp": login, "token": {
        "expires_in": 1800, "token_type": "Bearer", "scope": "api", "refresh_token": refresh, "access_token": access,
        "id_token": "FAKE-ID", "expires_at": int(base + remaining)}}))


def on_disk(path):
    return json.loads(path.read_text())


# --------------------------------------------------------------------------------------------------- starting

@pytest.mark.parametrize("name,value", [("SCHWAB_TOKEN_KEEPER", False), ("SCHWAB_APP_KEY", ""), ("SCHWAB_APP_SECRET", " "),
                                        ("SCHWAB_TOKEN_PATH", "")])
def test_the_keeper_does_not_start_without_the_flag_the_credentials_or_a_path(monkeypatch, harness, name, value, caplog):
    caplog.set_level("INFO")
    write_token(harness)
    monkeypatch.setattr(config, name, value)
    assert keeper.start() is False and keeper.running() is False and keeper.disabled_reason()
    said = [r for r in caplog.records if "keeper NOT started" in r.getMessage()]
    assert (len(said) == 0) if name == "SCHWAB_TOKEN_KEEPER" else (len(said) == 1 and said[0].keep is True)
    assert Http.requests == []


def test_the_shipped_flag_is_on_only_for_astra_and_never_in_the_schwab_twin():
    import os
    import subprocess
    import sys
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1]
    expected_by_persona = ({"ASTRA": "True", "ARIA": "False", "JORDAN": "False"} if hasattr(config, "PERSONA_OVERRIDES")
                           else {"ASTRA": "False", "ARIA": "False"})       # the twin refreshes its own token
    for persona, expected in expected_by_persona.items():
        env = {**os.environ, "ACTIVE_TRADER": persona}
        env.pop("SCHWAB_TOKEN_KEEPER", None)
        out = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config; "
                              "print(config.SCHWAB_TOKEN_KEEPER)"], cwd=str(folder), env=env,
                             capture_output=True, text=True, timeout=60)
        assert out.stdout.strip() == expected, (persona, out.stdout, out.stderr)


# --------------------------------------------------------------------------------------------------- one refresh

def test_a_refresh_posts_the_refresh_grant_with_basic_auth_and_writes_the_file_atomically(harness):
    write_token(harness, remaining=100)
    result = keeper.refresh_schwab_token_now()
    assert result["ok"] is True and result["kind"] == "ok" and result["expires_at"] > time.time() + 1500
    assert len(Http.requests) == 1
    request = Http.requests[0]
    assert request.method == "POST" and str(request.url) == keeper.TOKEN_ENDPOINT
    assert request.headers["authorization"] == "Basic " + base64.b64encode(b"KEY:SECRET").decode()
    form = parse_qs(request.content.decode())
    assert form == {"grant_type": ["refresh_token"], "refresh_token": ["FAKE-R0"]}
    saved = on_disk(harness)
    assert saved["creation_timestamp"] == LOGIN                            # a refresh never moves the 7-day login clock
    assert saved["token"]["access_token"] == "FAKE-A1" and saved["token"]["refresh_token"] == "FAKE-R0"
    assert saved["token"]["expires_at"] > time.time() + 1500
    assert not list(harness.parent.glob("*.keeper.token"))                   # no temp file left behind


def test_a_rotated_refresh_token_is_persisted(harness):
    write_token(harness, remaining=100)
    Http.handler = ok_handler(refresh="FAKE-R9")
    assert keeper.refresh_schwab_token_now()["ok"] is True
    assert on_disk(harness)["token"]["refresh_token"] == "FAKE-R9"


def test_a_200_without_an_access_token_is_never_saved(harness):
    write_token(harness, remaining=100)
    before = harness.read_bytes()
    Http.handler = lambda request: httpx.Response(200, json={"foo": "bar"})
    result = keeper.refresh_schwab_token_now()
    assert result["ok"] is False and result["kind"] == "file"
    assert harness.read_bytes() == before and not list(harness.parent.glob("*.keeper.token"))


def test_a_login_written_while_the_refresh_was_in_flight_is_not_overwritten(harness):
    write_token(harness, remaining=100, login=LOGIN, refresh="FAKE-OLD")

    def handler(request):
        write_token(harness, remaining=1800, login=LOGIN + 5000, refresh="FAKE-NEW-LOGIN", access="FAKE-NEW-ACCESS")   # schwab_login.py
        return httpx.Response(200, json={"access_token": "FAKE-A1", "refresh_token": "FAKE-OLD", "expires_in": 1800,
                                         "token_type": "Bearer", "scope": "api"})
    Http.handler = handler
    result = keeper.refresh_schwab_token_now()
    assert result["ok"] is False and result["kind"] == "replaced"
    saved = on_disk(harness)
    assert saved["creation_timestamp"] == LOGIN + 5000 and saved["token"]["refresh_token"] == "FAKE-NEW-LOGIN"
    Http.handler = ok_handler(refresh="FAKE-NEW-LOGIN")
    assert keeper.refresh_schwab_token_now()["ok"] is True                    # the next refresh uses the NEW login
    assert parse_qs(Http.requests[-1].content.decode())["refresh_token"] == ["FAKE-NEW-LOGIN"]


@pytest.mark.parametrize("case,handler,kind", [
    ("connect timeout", raising(httpx.ConnectTimeout), "transient"),
    ("read timeout", raising(httpx.ReadTimeout), "transient"),
    ("connection reset", raising(httpx.RemoteProtocolError), "transient"),
    ("HTTP 503", lambda r: httpx.Response(503, text="down"), "transient"),
    ("HTTP 429 html", lambda r: httpx.Response(429, text="<html>slow down</html>"), "transient"),
    ("empty 200 body", lambda r: httpx.Response(200, content=b""), "transient"),
    ("temporarily_unavailable", lambda r: httpx.Response(400, json={"error": "temporarily_unavailable"}), "transient"),
    ("server_error", lambda r: httpx.Response(400, json={"error": "server_error"}), "transient"),
    ("invalid_grant", lambda r: httpx.Response(400, json={"error": "invalid_grant", "error_description": "dead"}), "auth"),
    ("invalid_client", lambda r: httpx.Response(401, json={"error": "invalid_client", "error_description": "Unauthorized"}), "auth"),
    ("unsupported_token_type", lambda r: httpx.Response(400, json={"error": "unsupported_token_type"}), "auth"),
])
def test_failures_are_classified_as_a_human_problem_or_a_retry(harness, case, handler, kind):
    write_token(harness, remaining=100)
    before = harness.read_bytes()
    Http.handler = handler
    result = keeper.refresh_schwab_token_now()
    assert result["ok"] is False and result["kind"] == kind, (case, result)
    assert harness.read_bytes() == before                                      # a failed refresh never touches the file


def test_a_missing_or_corrupt_token_file_is_a_file_problem_not_a_network_one(harness):
    assert keeper.refresh_schwab_token_now()["kind"] == "file"
    harness.write_text("{not json")
    result = keeper.refresh_schwab_token_now()
    assert result["kind"] == "file" and "JSONDecodeError" in result["detail"]
    harness.write_text(json.dumps({"token": {}}))                              # the pre-metadata format schwab-py refuses
    assert keeper.refresh_schwab_token_now()["kind"] == "file"
    assert Http.requests == []


# --------------------------------------------------------------------------------------------------- the cycle

def test_next_action_waits_for_the_margin_and_for_a_back_off():
    st = {"next_try": 0.0}
    assert keeper._next_action(1000.0, 1000.0 + 1700, st) == (False, 20.0)         # 1700 s left, margin 600: look again in 20 s
    assert keeper._next_action(1000.0, 1000.0 + 601, st) == (False, 1.0)
    assert keeper._next_action(1000.0, 1000.0 + 600, st) == (True, 0.0)
    assert keeper._next_action(1000.0, 1000.0 - 5, st) == (True, 0.0)               # already expired
    assert keeper._next_action(1000.0, None, st) == (True, 0.0)
    st["next_try"] = 1030.0
    assert keeper._next_action(1000.0, 1000.0 - 5, st) == (False, 20.0)             # backing off


def test_a_cycle_leaves_a_token_with_plenty_of_life_alone_and_refreshes_one_inside_the_margin(harness):
    write_token(harness, remaining=1700)
    keeper._cycle()
    assert Http.requests == []
    write_token(harness, remaining=300)
    keeper._cycle()
    assert len(Http.requests) == 1 and on_disk(harness)["token"]["access_token"] == "FAKE-A1"
    keeper._cycle()
    assert len(Http.requests) == 1                                                  # freshly refreshed: nothing more to do


def test_failures_back_off_15_30_60_120_then_stay_at_120_and_a_recovery_is_reported(harness, caplog):
    caplog.set_level("INFO")
    write_token(harness, remaining=60)
    Http.handler = raising(httpx.ConnectTimeout)
    now, waits = time.time(), []
    for _ in range(6):
        keeper._cycle(now)
        waits.append(keeper._KEEPER["next_try"] - now)
        before = len(Http.requests)
        keeper._cycle(now + 1)                                                      # still backing off: no request
        assert len(Http.requests) == before
        now = keeper._KEEPER["next_try"]
    assert waits == [15, 30, 60, 120, 120, 120]
    Http.handler = ok_handler()
    keeper._cycle(now)
    events = [e["event"] for e in keeper.drain_events()]
    assert events.count("SCHWAB_TOKEN_REFRESH_FAILED") == 6 and events[-2:] == ["SCHWAB_TOKEN_REFRESH_RECOVERED", "SCHWAB_TOKEN_REFRESHED"]
    assert keeper._KEEPER["fails"] == 0 and any("RECOVERED" in r.getMessage() for r in caplog.records)
    warned = [r for r in caplog.records if "refresh failed" in r.getMessage()]
    assert 1 <= len(warned) < 6 and all(r.keep is True for r in warned)             # first failure, then at most one per 5 minutes


def test_a_rejected_refresh_token_waits_15_minutes_shouts_once_and_a_new_login_clears_it(harness, caplog):
    caplog.set_level("INFO")
    write_token(harness, remaining=60)
    Http.handler = lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "dead"})
    now = time.time()
    keeper._cycle(now)
    assert keeper._KEEPER["next_try"] - now == 900 and keeper._KEEPER["kind"] == "auth"
    keeper._cycle(now + 100)
    keeper._cycle(now + 600)
    assert len(Http.requests) == 1                                                  # not hammering a dead token
    shouts = [r for r in caplog.records if "REFRESH TOKEN REJECTED" in r.getMessage()]
    assert len(shouts) == 1 and shouts[0].levelno == logging.CRITICAL and shouts[0].keep is True
    assert "schwab_login.py --force" in shouts[0].getMessage() and str(harness.parent) in shouts[0].getMessage()
    Http.handler = ok_handler(refresh="FAKE-R-NEW")
    write_token(harness, remaining=60, login=LOGIN + 9000, refresh="FAKE-R-NEW")     # the user logged in again
    keeper._cycle(now + 650)                                                        # well inside the 900 s wait
    assert keeper._KEEPER["kind"] == "ok" and len(Http.requests) == 2
    assert "SCHWAB_LOGIN_CHANGED" in [e["event"] for e in keeper.drain_events()]


def test_an_invalid_client_on_a_young_login_points_at_the_app_credentials(harness, caplog):
    caplog.set_level("INFO")
    write_token(harness, remaining=60)
    Http.handler = lambda request: httpx.Response(401, json={"error": "invalid_client", "error_description": "Unauthorized"})
    keeper._cycle(time.time())
    assert any("SCHWAB_APP_SECRET" in r.getMessage() for r in caplog.records)


def test_a_missing_token_file_says_so_and_makes_no_request(harness, caplog):
    caplog.set_level("INFO")
    keeper._cycle(time.time())
    assert Http.requests == [] and keeper._KEEPER["kind"] == "file"
    assert any("TOKEN FILE NOT FOUND" in r.getMessage() and r.levelno == logging.CRITICAL for r in caplog.records)


def test_the_login_age_is_watched_and_a_dead_login_is_not_announced_twice(harness, caplog):
    caplog.set_level("INFO")
    now_dt = et(8, 0)
    now = now_dt.timestamp()

    def at_age(days, kind=None):
        write_token(harness, remaining=1800, login=int(now - days * 86400), at=now)
        keeper._KEEPER.update(kind=kind, health=None, sig=None)
        keeper._KEEPER["logged"].clear()
        caplog.clear()
        keeper._cycle(now)
        return [(r.levelname, r.getMessage()) for r in caplog.records]
    assert at_age(2.0) == []
    warn = at_age(6.0)
    assert len(warn) == 1 and warn[0][0] == "WARNING" and "LOGIN AGING" in warn[0][1] and "schwab_login.py --force" in warn[0][1]
    expiring = at_age(6.97)
    assert len(expiring) == 1 and expiring[0][0] == "CRITICAL" and "EXPIRES TODAY" in expiring[0][1]
    dead = at_age(7.2)
    assert len(dead) == 1 and dead[0][0] == "CRITICAL" and "LOGIN EXPIRED" in dead[0][1]
    assert at_age(7.2, kind="auth") == []                                           # the rejected refresh already says it
    states = [e["state"] for e in keeper.drain_events() if e["event"] == "TOKEN_STATUS"]
    assert "WARN" in states and "EXPIRING" in states and "DEAD" in states


def test_a_warning_is_repeated_at_most_hourly(harness, caplog):
    caplog.set_level("INFO")
    now = et(8, 0).timestamp()
    write_token(harness, remaining=1800, login=int(now - 6 * 86400), at=now)
    for later in (0, 600, 3000, 3700):
        keeper._cycle(now + later)
    assert len([r for r in caplog.records if "LOGIN AGING" in r.getMessage()]) == 2


def test_events_are_queued_for_the_main_thread_and_drained_once(harness):
    write_token(harness, remaining=60)
    keeper._cycle(time.time())
    drained = keeper.drain_events()
    assert [e["event"] for e in drained] == ["SCHWAB_TOKEN_REFRESHED"] and drained[0]["expires_at"] > time.time()
    assert keeper.drain_events() == []


# --------------------------------------------------------------------------------------------------- the thread

def test_the_thread_refreshes_before_the_first_report_and_stops_cleanly(harness, monkeypatch):
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    write_token(harness, remaining=120)
    assert keeper.start() is True and keeper.start() is True                        # idempotent: one thread
    assert keeper.wait_first(10) is True
    assert len(Http.requests) == 1 and on_disk(harness)["token"]["access_token"] == "FAKE-A1"
    time.sleep(0.3)
    assert len(Http.requests) == 1                                                  # polling with 1800 s left makes no more requests
    assert "valid until" in keeper.report_line() and keeper.report_line().startswith("Schwab token keeper: ON")
    keeper.stop()
    assert keeper.running() is False and not list(harness.parent.glob("*.keeper.token"))


def test_wait_first_does_not_block_when_the_keeper_is_not_running(harness):
    started = time.monotonic()
    assert keeper.wait_first(5) is False and time.monotonic() - started < 1


def test_a_crashing_cycle_never_ends_the_thread(harness, monkeypatch, caplog):
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    calls = []

    def boom(now=None):
        calls.append(1)
        raise RuntimeError("bug")
    monkeypatch.setattr(keeper, "_cycle", boom)
    write_token(harness)
    assert keeper.start(wait_first_sec=5) is True
    time.sleep(0.3)
    assert len(calls) >= 3 and keeper.running() is True
    keeper.stop()


# --------------------------------------------------------------------------------------------------- the file reader

def test_the_reader_retries_a_truncated_file_and_a_sharing_violation(harness, monkeypatch):
    write_token(harness)
    real = json.load
    state = {"n": 0}

    def flaky(fh, *a, **k):
        state["n"] += 1
        if state["n"] == 1:
            raise json.JSONDecodeError("truncated", "", 0)
        if state["n"] == 2:
            raise PermissionError(13, "being replaced")
        return real(fh, *a, **k)
    monkeypatch.setattr(keeper.json, "load", flaky)
    assert keeper.read_token_file(str(harness), pause=0)["creation_timestamp"] == LOGIN and state["n"] == 3


def test_the_reader_gives_up_after_its_attempts_and_never_retries_a_missing_file(harness, monkeypatch):
    harness.write_text("{broken")
    with pytest.raises(json.JSONDecodeError):
        keeper.read_token_file(str(harness), attempts=3, pause=0)
    calls = []
    monkeypatch.setattr(keeper.time, "sleep", lambda s: calls.append(s))
    with pytest.raises(FileNotFoundError):
        keeper.read_token_file(str(harness.parent / "nope.json"))
    assert calls == []


def test_market_data_reads_the_token_through_the_tolerant_reader(harness, monkeypatch):
    write_token(harness, remaining=1500)
    seen = []
    real = keeper.read_token_file
    monkeypatch.setattr(keeper, "read_token_file", lambda path, *a, **k: seen.append(path) or real(path, *a, **k))
    monkeypatch.setattr(market_data, "_ES_LIVE", {"state": None, "fail_until": 0.0})
    client, why = market_data._es_token_check()
    assert client is not None and seen == [str(harness)]


def test_market_data_names_the_keeper_when_the_access_token_is_expired(harness):
    write_token(harness, remaining=-10)
    client, why = market_data._es_token_check()
    assert client is None and "no token keeper runs in this process" in why and "Schwab bot" in why
    keeper._KEEPER["thread"] = type("T", (), {"is_alive": lambda self: True, "join": lambda self, timeout=None: None})()
    keeper._KEEPER["detail"] = "ConnectTimeout"
    _, why = market_data._es_token_check()
    assert "token keeper: last refresh failed: ConnectTimeout" in why


# --------------------------------------------------------------------------------------------------- the report line

def test_the_report_line_says_why_it_is_off_and_how_it_is_doing(harness, monkeypatch):
    monkeypatch.setattr(config, "SCHWAB_TOKEN_KEEPER", False)
    assert keeper.report_line() == "Schwab token keeper: OFF -- this process does not own the Schwab token (SCHWAB_TOKEN_KEEPER is off)"
    monkeypatch.setattr(config, "SCHWAB_TOKEN_KEEPER", True)
    monkeypatch.setattr(config, "SCHWAB_APP_SECRET", "")
    assert "SCHWAB_APP_SECRET is not set" in keeper.report_line()
    monkeypatch.setattr(config, "SCHWAB_APP_SECRET", "SECRET")
    assert keeper.report_line() == "Schwab token keeper: not running"
    write_token(harness, remaining=-30)
    Http.handler = lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "dead"})
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    keeper.start(wait_first_sec=5)
    line = keeper.report_line()
    assert line.startswith("Schwab token keeper: ON") and "LAST REFRESH FAILED (auth: OAuthError:invalid_grant" in line
    assert "access token not valid right now" in line
    keeper.stop()


def test_every_queued_event_can_be_journaled_by_the_real_journal(harness, tmp_path):
    from journal import Journal
    journal = Journal(tmp_path / "logs", "astra")
    write_token(harness, remaining=60)
    Http.handler = raising(httpx.ConnectTimeout)
    keeper._cycle(time.time())                                              # a failure event
    Http.handler = ok_handler()
    keeper._cycle(time.time() + 20)                                         # recovery + refresh events
    write_token(harness, remaining=1800, login=LOGIN - 6 * 86400 - 3600)     # and an age event
    keeper._cycle(time.time() + 40)
    queued = keeper.drain_events()
    assert {"SCHWAB_TOKEN_REFRESH_FAILED", "SCHWAB_TOKEN_REFRESH_RECOVERED", "SCHWAB_TOKEN_REFRESHED"} <= {e["event"] for e in queued}
    for event in queued:
        fields = dict(event)
        journal.event(fields.pop("event"), datetime.now(), **fields)        # raises on a clashing field name


def test_the_writer_retries_a_sharing_violation_and_gives_up_after_a_while(harness, monkeypatch):
    monkeypatch.setattr(keeper.time, "sleep", lambda s: None)
    wrapped = {"creation_timestamp": LOGIN, "token": {"access_token": "A", "refresh_token": "R", "expires_at": time.time() + 1800}}
    write_token(harness)
    real, state = keeper.os.replace, {"n": 0}

    def flaky(src, dst):
        state["n"] += 1
        if state["n"] <= 3:
            raise PermissionError(13, "a reader has it open")
        return real(src, dst)
    monkeypatch.setattr(keeper.os, "replace", flaky)
    keeper._write_token_file(str(harness), wrapped, LOGIN)
    assert state["n"] == 4 and on_disk(harness)["token"]["access_token"] == "A" and not list(harness.parent.glob("*.keeper.token"))
    monkeypatch.setattr(keeper.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError(13, "always")))
    with pytest.raises(PermissionError):
        keeper._write_token_file(str(harness), wrapped, LOGIN)
    assert not list(harness.parent.glob("*.keeper.token"))                          # no temp file left after giving up


def test_one_thread_only_however_often_start_is_called(harness, monkeypatch):
    import threading
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    write_token(harness, remaining=1700)
    for _ in range(3):
        assert keeper.start() is True
    assert len([t for t in threading.enumerate() if t.name == "schwab-token-keeper"]) == 1
    keeper.stop()


def test_the_refresh_client_has_a_short_timeout_so_a_stall_cannot_hold_the_thread(harness):
    write_token(harness)
    client = keeper._build_client(str(harness))
    try:
        assert client.session.timeout.connect == 5.0 and client.session.timeout.read == 10.0
    finally:
        client.session.close()


@pytest.mark.parametrize("token", [
    {"refresh_token": "R", "expires_at": 4_000_000_000},                          # no access token
    {"access_token": "", "refresh_token": "R", "expires_at": 4_000_000_000},      # an empty one
    {"access_token": "A", "expires_at": 4_000_000_000},                           # no refresh token
    {"access_token": "A", "refresh_token": "R"},                                  # no expiry: schwab-py would then never refresh again
    {"access_token": "A", "refresh_token": "R", "expires_at": 1_000_000_000},     # already expired
])
def test_an_incomplete_or_expired_refreshed_token_is_never_saved(token):
    with pytest.raises(ValueError):
        keeper._validate_wrapped({"creation_timestamp": LOGIN, "token": token})
    keeper._validate_wrapped({"creation_timestamp": LOGIN, "token": {"access_token": "A", "refresh_token": "R", "expires_at": 4_000_000_000}})
    with pytest.raises(ValueError):
        keeper._validate_wrapped({"token": {"access_token": "A", "refresh_token": "R", "expires_at": 4_000_000_000}})


def test_an_unreadable_file_is_not_mistaken_for_a_new_login(harness, monkeypatch):
    write_token(harness, remaining=1700)
    now = time.time()
    keeper._cycle(now)                                                      # remembers this login
    keeper._KEEPER.update(fails=3, next_try=now + 800, kind="auth")
    keeper.drain_events()

    def torn(path, *a, **k):
        raise json.JSONDecodeError("torn", "", 0)
    monkeypatch.setattr(keeper, "read_token_file", torn)
    keeper._cycle(now + 10)
    assert keeper._KEEPER["sig"] == LOGIN and keeper._KEEPER["next_try"] == now + 800 and keeper._KEEPER["fails"] == 3
    assert "SCHWAB_LOGIN_CHANGED" not in [e["event"] for e in keeper.drain_events()]
    assert Http.requests == []


def test_a_failed_import_of_schwab_py_keeps_the_keeper_from_starting(harness, monkeypatch, caplog):
    import sys
    caplog.set_level("INFO")
    write_token(harness)
    monkeypatch.setitem(sys.modules, "schwab.auth", None)                   # an import of it now raises ImportError
    assert keeper.start() is False and keeper.running() is False
    assert any("cannot be imported" in r.getMessage() and r.keep is True for r in caplog.records)


def test_errors_from_the_http_library_count_as_transient_whatever_its_name_is():
    class Timeout(Exception):
        pass
    for module in ("httpx", "httpx2", "httpcore._exceptions"):
        Timeout.__module__ = module
        assert keeper._classify(Timeout("slow")) == ("transient", "Timeout"), module
    Timeout.__module__ = "mylib"
    assert keeper._classify(Timeout("x"))[0] == "file"


# ---- review follow-ups (2026-10-09): a granted token is never lost to a failed save; an unreadable file is not a failure

def rotating_handler(state):
    """A Schwab that ROTATES refresh tokens: each grant kills the refresh token it was given."""
    def handle(request):
        sent = parse_qs(request.content.decode())["refresh_token"][0]
        if sent != state["live"]:
            return httpx.Response(400, json={"error": "invalid_grant", "error_description": "refresh token already used"})
        state["n"] += 1
        state["live"] = f"FAKE-R{state['n']}"
        return httpx.Response(200, json={"access_token": f"FAKE-A{state['n']}", "refresh_token": state["live"], "id_token": "FAKE-ID",
                                         "expires_in": 1800, "token_type": "Bearer", "scope": "api"})
    return handle


def test_a_granted_token_whose_save_failed_is_kept_and_saved_on_the_next_cycle_without_a_new_grant(harness, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(keeper.time, "sleep", lambda s: None)
    write_token(harness, remaining=60, refresh="FAKE-R0")
    state = {"live": "FAKE-R0", "n": 0}
    Http.handler = rotating_handler(state)
    real_replace = keeper.os.replace
    monkeypatch.setattr(keeper.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError(13, "a reader holds it")))
    now = time.time()
    keeper._cycle(now)                                                      # the grant works, the save does not
    assert len(Http.requests) == 1 and keeper._KEEPER["pending"] is not None and keeper._KEEPER["kind"] == "write"
    assert on_disk(harness)["token"]["refresh_token"] == "FAKE-R0"           # the file still holds the DEAD token
    assert any("could not be SAVED" in r.getMessage() and r.keep is True for r in caplog.records)
    monkeypatch.setattr(keeper.os, "replace", real_replace)
    keeper._cycle(now + 1)                                                  # still backing off
    assert keeper._KEEPER["pending"] is not None and len(Http.requests) == 1
    keeper._cycle(now + 16)                                                 # back-off over: save the kept token, no second grant
    assert len(Http.requests) == 1 and keeper._KEEPER["pending"] is None and keeper._KEEPER["kind"] == "ok"
    assert on_disk(harness)["token"]["refresh_token"] == "FAKE-R1" and on_disk(harness)["token"]["access_token"] == "FAKE-A1"
    assert on_disk(harness)["creation_timestamp"] == LOGIN
    keeper._cycle(now + 40)
    assert len(Http.requests) == 1                                          # the saved token has life left: nothing to do
    events = [e["event"] for e in keeper.drain_events()]
    assert events.count("SCHWAB_TOKEN_REFRESH_FAILED") == 1 and events[-1] == "SCHWAB_TOKEN_REFRESHED"


def test_a_pending_token_is_dropped_when_a_newer_login_arrived_meanwhile(harness, monkeypatch, caplog):
    caplog.set_level("INFO")
    monkeypatch.setattr(keeper.time, "sleep", lambda s: None)
    write_token(harness, remaining=60)
    real_replace = keeper.os.replace
    monkeypatch.setattr(keeper.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError(13, "held")))
    now = time.time()
    keeper._cycle(now)
    assert keeper._KEEPER["pending"] is not None
    monkeypatch.setattr(keeper.os, "replace", real_replace)
    write_token(harness, remaining=1800, login=LOGIN + 7000, refresh="FAKE-NEW-LOGIN")        # the user logged in again
    keeper._cycle(now + 20)
    assert keeper._KEEPER["pending"] is None and on_disk(harness)["token"]["refresh_token"] == "FAKE-NEW-LOGIN"
    assert on_disk(harness)["creation_timestamp"] == LOGIN + 7000


def test_a_pending_token_that_still_cannot_be_saved_backs_off_and_never_re_grants(harness, monkeypatch):
    monkeypatch.setattr(keeper.time, "sleep", lambda s: None)
    write_token(harness, remaining=60)
    monkeypatch.setattr(keeper.os, "replace", lambda src, dst: (_ for _ in ()).throw(PermissionError(13, "held")))
    now, waits = time.time(), []
    keeper._cycle(now)
    for _ in range(4):
        waits.append(keeper._KEEPER["next_try"] - now)
        now = keeper._KEEPER["next_try"]
        keeper._cycle(now)
    assert waits == [15, 30, 60, 120] and len(Http.requests) == 1 and keeper._KEEPER["pending"] is not None


def test_a_failed_save_leaves_no_temp_file_even_when_the_dump_itself_fails(harness, monkeypatch):
    wrapped = {"creation_timestamp": LOGIN, "token": {"access_token": "A", "refresh_token": "R", "expires_at": time.time() + 1800}}
    write_token(harness)

    def disk_full(fd):
        raise OSError(28, "no space left")
    monkeypatch.setattr(keeper.os, "fsync", disk_full)
    with pytest.raises(OSError):
        keeper._write_token_file(str(harness), wrapped, LOGIN)
    assert sorted(p.name for p in harness.parent.iterdir()) == ["token.json"]


def test_the_temp_file_name_is_covered_by_the_repo_gitignore():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    if not (root / ".git").exists():
        pytest.skip("not inside the git checkout")
    try:
        out = subprocess.run(["git", "check-ignore", "-q", "schwab/0dte/token.json.keeper.token"], cwd=str(root), capture_output=True)
    except FileNotFoundError:
        pytest.skip("git is not installed")
    assert out.returncode == 0                                              # a leftover temp file holds a live refresh token


def test_a_briefly_unreadable_file_is_neither_a_failure_nor_a_refresh_and_a_lasting_one_is_reported(harness, monkeypatch, caplog):
    caplog.set_level("INFO")
    write_token(harness, remaining=30)                                      # due for a refresh, but the file cannot be read
    now = time.time()

    real_reader = keeper.read_token_file

    def torn(path, *a, **k):
        raise json.JSONDecodeError("torn", "", 0)
    monkeypatch.setattr(keeper, "read_token_file", torn)
    keeper._cycle(now)
    keeper._cycle(now + 20)
    assert Http.requests == [] and keeper._KEEPER["fails"] == 0 and keeper.drain_events() == []
    assert not [r for r in caplog.records if "UNREADABLE" in r.getMessage()]
    keeper._cycle(now + 40)                                                 # third check in a row
    shouts = [r for r in caplog.records if "TOKEN FILE UNREADABLE" in r.getMessage()]
    assert len(shouts) == 1 and shouts[0].levelno == logging.CRITICAL and keeper._KEEPER["kind"] == "file"
    monkeypatch.setattr(keeper, "read_token_file", real_reader)
    keeper._cycle(now + 60)                                                 # readable again: the count resets and the refresh happens
    assert keeper._KEEPER["unreadable"] == 0 and len(Http.requests) == 1


def test_a_login_replaced_during_the_grant_is_retried_at_once_not_after_a_wait(harness):
    write_token(harness, remaining=100, login=LOGIN, refresh="FAKE-OLD")

    def handler(request):
        write_token(harness, remaining=1800, login=LOGIN + 5000, refresh="FAKE-NEW-LOGIN")
        return httpx.Response(200, json={"access_token": "A", "refresh_token": "FAKE-OLD", "expires_in": 1800, "token_type": "Bearer"})
    Http.handler = handler
    now = time.time()
    keeper._cycle(now)
    assert keeper._KEEPER["kind"] == "replaced" and keeper._KEEPER["next_try"] == now


def test_log_lines_that_must_reach_the_log_carry_keep(harness, caplog):
    caplog.set_level("INFO")
    write_token(harness, remaining=60)
    Http.handler = raising(httpx.ConnectTimeout)
    now = time.time()
    keeper._cycle(now)
    Http.handler = ok_handler()
    keeper._cycle(now + 20)
    lines = [r for r in caplog.records if r.name == "0dte.schwab_keeper"]
    for needle in ("refresh failed", "RECOVERED", "refreshed, valid until"):
        assert any(needle in r.getMessage() for r in lines), needle
    assert lines and all(getattr(r, "keep", False) is True for r in lines)    # the console/file filter drops everything else


def test_the_app_credential_hint_appears_only_for_a_young_login(harness, caplog):
    caplog.set_level("INFO")
    Http.handler = lambda request: httpx.Response(401, json={"error": "invalid_client", "error_description": "Unauthorized"})
    now = et(8, 0).timestamp()
    write_token(harness, remaining=-60, login=int(now - 3 * 86400), at=now)
    keeper._cycle(now)
    assert any("SCHWAB_APP_SECRET" in r.getMessage() for r in caplog.records)
    caplog.clear()
    keeper._KEEPER.update(kind=None, health=None, next_try=0.0, sig=None)
    keeper._KEEPER["logged"].clear()
    write_token(harness, remaining=-60, login=int(now - 8 * 86400), at=now)                  # an old login: the token itself is the suspect
    keeper._cycle(now)
    assert any("REFRESH TOKEN REJECTED" in r.getMessage() for r in caplog.records)
    assert not any("SCHWAB_APP_SECRET" in r.getMessage() for r in caplog.records)


def test_the_thread_is_a_daemon_so_it_can_never_hold_the_process_open(harness, monkeypatch):
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    write_token(harness, remaining=1700)
    keeper.start()
    assert keeper._KEEPER["thread"].daemon is True and keeper._KEEPER["thread"].name == "schwab-token-keeper"
    keeper.stop()


def test_the_default_token_path_is_the_schwab_folders_file_and_a_blank_setting_means_unset():
    import os
    import subprocess
    import sys
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1]
    code = ("import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config; print(config.SCHWAB_TOKEN_PATH)")
    for value in (None, "", "   "):
        env = {k: v for k, v in os.environ.items() if k != "SCHWAB_TOKEN_PATH"}
        if value is not None:
            env["SCHWAB_TOKEN_PATH"] = value
        out = subprocess.run([sys.executable, "-c", code], cwd=str(folder), env=env, capture_output=True, text=True, timeout=60)
        path = Path(out.stdout.strip())
        assert path.name == "token.json", (value, out.stdout, out.stderr)
        if hasattr(config, "PERSONA_OVERRIDES"):                         # Alpaca folder: the Schwab 0DTE folder's token next to it
            assert path.parent == folder.parents[1] / "schwab" / "0dte" and (path.parent / "schwab_login.py").exists()
        else:                                                           # the Schwab twin: its own folder
            assert path.parent == folder
    env = {**os.environ, "SCHWAB_TOKEN_PATH": str(folder / "elsewhere.json")}
    out = subprocess.run([sys.executable, "-c", code], cwd=str(folder), env=env, capture_output=True, text=True, timeout=60)
    assert out.stdout.strip() == str(folder / "elsewhere.json")          # an explicit setting still wins


# ---- 2026-10-12: SCHWAB_TOKEN_PATH set to the Schwab FOLDER (a PermissionError on Windows that looked like a locked file)

def _config_path(env_value, tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "SCHWAB_TOKEN_PATH"}
    if env_value is not None:
        env["SCHWAB_TOKEN_PATH"] = env_value
    out = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config; "
                          "print(config.SCHWAB_TOKEN_PATH)"], cwd=str(folder), env=env, capture_output=True, text=True, timeout=60)
    return out.stdout.strip(), out.stderr


def test_a_token_path_that_names_a_folder_means_the_token_json_inside_it(tmp_path):
    folder = tmp_path / "0dte"
    folder.mkdir()
    path, err = _config_path(str(folder), tmp_path)
    assert path == str(folder / "token.json"), err
    file_path = tmp_path / "somewhere" / "my-token.json"
    assert _config_path(str(file_path), tmp_path)[0] == str(file_path)              # a file path (even one that does not exist yet) is untouched


def test_an_unreadable_token_path_is_named_and_a_folder_is_called_a_folder(harness, caplog, monkeypatch):
    caplog.set_level("INFO")
    folder = harness.parent / "0dte"
    folder.mkdir()
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(folder))                  # set after config import: the check below still catches it
    now = time.time()
    for n in range(3):
        keeper._cycle(now + 20 * n)
    shouts = [r for r in caplog.records if "TOKEN FILE UNREADABLE" in r.getMessage()]
    assert len(shouts) == 1 and shouts[0].levelno == logging.CRITICAL
    text = shouts[0].getMessage()
    assert str(folder) in text and "FOLDER" in text and str(folder / "token.json") in text and "SCHWAB_TOKEN_PATH" in text


def test_an_unreadable_file_names_its_path_and_the_usual_culprits(harness, caplog, monkeypatch):
    caplog.set_level("INFO")
    write_token(harness)

    def locked(path, *a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(keeper, "read_token_file", locked)
    now = time.time()
    for n in range(3):
        keeper._cycle(now + 20 * n)
    text = next(r.getMessage() for r in caplog.records if "TOKEN FILE UNREADABLE" in r.getMessage())
    assert str(harness) in text and "PermissionError" in text and "antivirus" in text and "schwab_login.py --force" in text


def test_the_relogin_command_points_at_the_folder_holding_schwab_login(harness, monkeypatch):
    assert keeper._relogin_command() == f'cd "{harness.parent}" && python schwab_login.py --force'
    folder = harness.parent / "0dte"
    folder.mkdir()
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(folder))
    assert keeper._relogin_command() == f'cd "{folder}" && python schwab_login.py --force'    # a folder path: that folder, not its parent


def test_market_data_says_when_the_token_path_is_a_folder(harness, monkeypatch):
    folder = harness.parent / "0dte"
    folder.mkdir()
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(folder))
    client, why = market_data._es_token_check()
    assert client is None and "is a folder" in why and str(folder) in why
    assert market_data.live_es_status() == (False, why)


def test_the_report_line_shows_which_token_file_the_keeper_watches(harness, monkeypatch):
    write_token(harness, remaining=-30)
    Http.handler = lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "dead"})
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    keeper.start(wait_first_sec=5)
    assert f"Schwab token keeper: ON [{harness}]" in keeper.report_line()
    keeper.stop()


# ---- 2026-10-12: SCHWAB_TOKEN_PATH set to the Schwab FOLDER (a PermissionError on Windows that looked like a locked file)

def _config_path(env_value):
    import os
    import subprocess
    import sys
    from pathlib import Path
    folder = Path(__file__).resolve().parents[1]
    env = {k: v for k, v in os.environ.items() if k != "SCHWAB_TOKEN_PATH"}
    if env_value is not None:
        env["SCHWAB_TOKEN_PATH"] = env_value
    out = subprocess.run([sys.executable, "-c", "import dotenv; dotenv.load_dotenv = lambda *a, **k: False; import config; "
                          "print(config.SCHWAB_TOKEN_PATH)"], cwd=str(folder), env=env, capture_output=True, text=True, timeout=60)
    return out.stdout.strip(), out.stderr


def test_a_token_path_that_names_a_folder_means_the_token_json_inside_it(tmp_path):
    folder = tmp_path / "0dte"
    folder.mkdir()
    path, err = _config_path(str(folder))
    assert path == str(folder / "token.json"), err
    assert _config_path(str(folder) + "\\")[0] == str(folder / "token.json") or _config_path(str(folder) + "/")[0] == str(folder / "token.json")
    file_path = tmp_path / "somewhere" / "my-token.json"
    assert _config_path(str(file_path))[0] == str(file_path)                        # a file path (even one that does not exist yet) is untouched


def test_an_unreadable_token_path_is_named_and_a_folder_is_called_a_folder(harness, caplog, monkeypatch):
    caplog.set_level("INFO")
    folder = harness.parent / "0dte"
    folder.mkdir()
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(folder))                  # set after config import: the check below still catches it
    now = time.time()
    for n in range(3):
        keeper._cycle(now + 20 * n)
    shouts = [r for r in caplog.records if "TOKEN FILE UNREADABLE" in r.getMessage()]
    assert len(shouts) == 1 and shouts[0].levelno == logging.CRITICAL
    text = shouts[0].getMessage()
    assert str(folder) in text and "FOLDER" in text and str(folder / "token.json") in text and "SCHWAB_TOKEN_PATH" in text


def test_an_unreadable_file_names_its_path_and_the_usual_culprits(harness, caplog, monkeypatch):
    caplog.set_level("INFO")
    write_token(harness)

    def locked(path, *a, **k):
        raise PermissionError(13, "denied")
    monkeypatch.setattr(keeper, "read_token_file", locked)
    now = time.time()
    for n in range(3):
        keeper._cycle(now + 20 * n)
    text = next(r.getMessage() for r in caplog.records if "TOKEN FILE UNREADABLE" in r.getMessage())
    assert str(harness) in text and "PermissionError" in text and "antivirus" in text and "schwab_login.py --force" in text


def test_the_relogin_command_points_at_the_folder_holding_schwab_login(harness, monkeypatch):
    assert keeper._relogin_command() == f'cd "{harness.parent}" && python schwab_login.py --force'
    folder = harness.parent / "0dte"
    folder.mkdir()
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(folder))
    assert keeper._relogin_command() == f'cd "{folder}" && python schwab_login.py --force'    # a folder path: that folder, not its parent


def test_market_data_says_when_the_token_path_is_a_folder(harness, monkeypatch):
    folder = harness.parent / "0dte"
    folder.mkdir()
    monkeypatch.setattr(config, "SCHWAB_TOKEN_PATH", str(folder))
    client, why = market_data._es_token_check()
    assert client is None and "is a folder" in why and str(folder) in why
    assert market_data.live_es_status() == (False, why)


def test_the_report_line_shows_which_token_file_the_keeper_watches(harness, monkeypatch):
    write_token(harness, remaining=-30)
    Http.handler = lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "dead"})
    monkeypatch.setattr(config, "SCHWAB_KEEPER_POLL_SEC", 0.05)
    keeper.start(wait_first_sec=5)
    assert f"Schwab token keeper: ON [{harness}]" in keeper.report_line()
    keeper.stop()
