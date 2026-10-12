"""The bot refuses to start on a PC whose clock disagrees with internet time (2026-10-12). All sources are fakes; no network."""

import sys
import time

import pytest
import requests

import clock_check
import config
import scheduler


class FakeClock:
    """A PC clock that is `offset` seconds ahead of true time, advancing `step` per read (a request takes `step` seconds)."""

    def __init__(self, true_now=1_800_000_000.0, offset=0.0, step=0.0):
        self.true_now, self.offset, self.step = true_now, offset, step
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.true_now + self.offset + self.step * (self.reads - 1)


def sources_returning(*times):
    """A fetch function answering each source with the next value (None = unreachable)."""
    answers = dict(zip(clock_check.SOURCES, times))
    return lambda url: answers[url]


def test_a_clock_that_matches_internet_time_is_fine():
    clock = FakeClock(offset=3.0)
    verdict = clock_check.check(fetch=sources_returning(*[clock.true_now] * 3), clock=clock)
    assert verdict["ok"] is True and verdict["sources"] == 3 and round(verdict["skew"]) == 3


@pytest.mark.parametrize("offset,word", [(-2 * 86400 - 3600, "BEHIND"), (500.0, "AHEAD of"), (-500.0, "BEHIND")])
def test_a_clock_off_by_more_than_the_limit_is_refused_and_described(offset, word):
    clock = FakeClock(offset=offset)
    verdict = clock_check.check(max_skew=120, fetch=sources_returning(*[clock.true_now] * 3), clock=clock)
    assert verdict["ok"] is False and word in verdict["detail"]


def test_the_limit_is_inclusive_and_configurable():
    clock = FakeClock(offset=120.0)
    assert clock_check.check(max_skew=120, fetch=sources_returning(*[clock.true_now] * 3), clock=clock)["ok"] is True
    clock = FakeClock(offset=121.0)
    assert clock_check.check(max_skew=120, fetch=sources_returning(*[clock.true_now] * 3), clock=clock)["ok"] is False
    assert clock_check.check(max_skew=600, fetch=sources_returning(*[clock.true_now] * 3), clock=clock)["ok"] is True


def test_one_wrong_source_cannot_decide():
    clock = FakeClock(offset=1.0)
    good = clock.true_now
    verdict = clock_check.check(fetch=sources_returning(good, good + 5 * 86400, good), clock=clock)
    assert verdict["ok"] is True and verdict["sources"] == 3
    verdict = clock_check.check(fetch=sources_returning(good + 5 * 86400, good, good + 5 * 86400), clock=clock)
    assert verdict["ok"] is False                                             # a wrong MAJORITY still decides (median)


def test_unreachable_sources_are_skipped_and_none_at_all_is_unknown_not_wrong():
    clock = FakeClock(offset=0.0)
    verdict = clock_check.check(fetch=sources_returning(None, clock.true_now, None), clock=clock)
    assert verdict["ok"] is True and verdict["sources"] == 1
    verdict = clock_check.check(fetch=sources_returning(None, None, None), clock=clock)
    assert verdict["ok"] is None and verdict["skew"] is None and "no time source" in verdict["detail"]


def test_a_slow_network_is_not_mistaken_for_a_wrong_clock():
    clock = FakeClock(offset=0.0, step=10.0)                                   # each request "takes" 10 s of the PC's clock
    # the server stamps its answer mid-request: 5 s after the request started, i.e. the midpoint of before/after
    verdict = clock_check.check(fetch=lambda url: clock.true_now + 5.0 + 10.0 * (clock.reads - 1) / 1.0 * 0, clock=clock, sources=("a",))
    assert abs(verdict["skew"]) <= 5.01 and verdict["ok"] is True


def test_human_durations():
    assert clock_check.human(45) == "45 s"
    assert clock_check.human(3 * 60 + 5) == "3 min"
    assert clock_check.human(2 * 3600 + 20 * 60) == "2 h 20 min"
    assert clock_check.human(2 * 86400 + 3 * 3600 + 600) == "2 days 3 h"
    assert clock_check.human(86400) == "1 day"
    assert clock_check.human(-45) == "45 s"


# ---------------------------------------------------------------------------------------- reading a Date header

class Response:
    def __init__(self, date):
        self.headers = {"Date": date} if date else {}


def test_the_date_header_is_parsed(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda url, **k: Response("Mon, 12 Oct 2026 14:36:30 GMT"))
    assert clock_check.server_time("https://x") == 1791815790.0


def test_a_missing_or_garbled_header_or_a_dead_source_is_none(monkeypatch):
    monkeypatch.setattr(requests, "head", lambda url, **k: Response(None))
    assert clock_check.server_time("https://x") is None
    monkeypatch.setattr(requests, "head", lambda url, **k: Response("not a date"))
    assert clock_check.server_time("https://x") is None
    monkeypatch.setattr(requests, "head", lambda url, **k: (_ for _ in ()).throw(requests.exceptions.ConnectTimeout("slow")))
    assert clock_check.server_time("https://x") is None


def test_a_certificate_error_caused_by_the_wrong_clock_retries_without_verification(monkeypatch):
    seen = []

    def head(url, **kwargs):
        seen.append(kwargs["verify"])
        if kwargs["verify"]:
            raise requests.exceptions.SSLError("certificate is not yet valid")      # what a PC years in the past sees
        return Response("Mon, 12 Oct 2026 14:36:30 GMT")
    monkeypatch.setattr(requests, "head", head)
    assert clock_check.server_time("https://x") == 1791815790.0 and seen == [True, False]
    monkeypatch.setattr(requests, "head", lambda url, **k: (_ for _ in ()).throw(requests.exceptions.SSLError("bad")))
    assert clock_check.server_time("https://x") is None                              # unreadable even unverified: unknown


# ---------------------------------------------------------------------------------------- main()

def run_main(monkeypatch, verdict, enabled=True):
    monkeypatch.setattr(sys, "argv", ["scheduler.py", "--once"])
    monkeypatch.setattr(scheduler, "setup_logging", lambda: None)
    monkeypatch.setattr(scheduler, "install_signal_handlers", lambda: None)
    monkeypatch.setattr(config, "CLOCK_CHECK_ENABLED", enabled, raising=False)
    monkeypatch.setattr(scheduler.clock_check, "check", lambda **kwargs: verdict)
    locks = []
    monkeypatch.setattr(scheduler.instance_lock, "acquire", lambda directory, name: locks.append(name) or (True, ""))

    class Built(Exception):
        pass

    def bot(*a, **k):
        raise Built
    monkeypatch.setattr(scheduler, "Bot", bot)
    return Built, locks


def test_main_refuses_to_start_on_a_wrong_clock_and_explains(monkeypatch, caplog):
    built, locks = run_main(monkeypatch, {"ok": False, "skew": -2 * 86400.0, "sources": 3,
                                          "detail": "PC clock is 2 days BEHIND internet time (3 source(s))"})
    with pytest.raises(SystemExit) as exc:
        scheduler.main()
    assert exc.value.code == 3 and locks == []                                       # nothing started, no lock taken
    message = next(r for r in caplog.records if "CLOCK IS WRONG" in r.getMessage())
    assert message.levelname == "CRITICAL" and message.keep is True
    text = message.getMessage()
    assert "2 days BEHIND" in text and "Sync now" in text and "w32tm /resync" in text and "will NOT start" in text


def test_main_starts_when_the_clock_is_right_or_unknown(monkeypatch, caplog):
    caplog.set_level("INFO")
    built, locks = run_main(monkeypatch, {"ok": True, "skew": 2.0, "sources": 3, "detail": "PC clock is 2 s AHEAD of internet time (3 source(s))"})
    with pytest.raises(built):
        scheduler.main()
    assert locks and any("Clock check" in r.getMessage() and r.keep is True for r in caplog.records)
    caplog.clear()
    built, locks = run_main(monkeypatch, {"ok": None, "skew": None, "sources": 0, "detail": "no time source could be reached"})
    with pytest.raises(built):
        scheduler.main()
    assert any("Clock check skipped" in r.getMessage() and r.levelname == "WARNING" for r in caplog.records)


def test_main_skips_the_check_when_it_is_switched_off(monkeypatch):
    built, locks = run_main(monkeypatch, {"ok": False, "skew": 9e9, "sources": 3, "detail": "x"}, enabled=False)
    with pytest.raises(built):
        scheduler.main()


def test_main_uses_the_configured_limit(monkeypatch):
    seen = []
    built, locks = run_main(monkeypatch, {"ok": True, "skew": 0.0, "sources": 3, "detail": "ok"})
    monkeypatch.setattr(config, "CLOCK_MAX_SKEW_SEC", 45, raising=False)
    monkeypatch.setattr(scheduler.clock_check, "check", lambda **kwargs: seen.append(kwargs) or {"ok": True, "skew": 0.0, "sources": 3, "detail": "ok"})
    with pytest.raises(built):
        scheduler.main()
    assert seen == [{"max_skew": 45.0}]


def test_the_shipped_settings():
    assert config.CLOCK_MAX_SKEW_SEC == 120
    import ast
    from pathlib import Path
    tree = ast.parse(Path(config.__file__).read_text(encoding="utf-8"))
    shipped = {n.targets[0].id: ast.literal_eval(n.value) for n in tree.body
               if isinstance(n, ast.Assign) and isinstance(n.targets[0], ast.Name) and n.targets[0].id == "CLOCK_CHECK_ENABLED"}
    assert shipped == {"CLOCK_CHECK_ENABLED": True}                                # the conftest turns it off for the other tests only


def test_a_crashing_clock_check_never_stops_the_bot_from_starting(monkeypatch, caplog):
    built, locks = run_main(monkeypatch, {"ok": True, "skew": 0.0, "sources": 3, "detail": "ok"})
    monkeypatch.setattr(scheduler.clock_check, "check", lambda **kwargs: 1 / 0)
    with pytest.raises(built):
        scheduler.main()
    assert any("Clock check skipped" in r.getMessage() and "ZeroDivisionError" in r.getMessage() for r in caplog.records)
