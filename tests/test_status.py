"""Progress feedback: status line, request indicator, prefetching."""

import importlib
import io
import logging
import time

import pytest

from nextdnsctl.cli.status import RequestIndicator, StatusAwareHandler, StatusLine
from nextdnsctl.planner import Planner

from .fake_api import FakeResponse


class FakeTTY(io.StringIO):
    def isatty(self):
        return True


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class TestStatusLine:
    def test_draws_in_a_terminal_and_clears(self):
        stream = FakeTTY()
        line = StatusLine(stream)
        line.start("Fetching profiles")
        assert wait_for(lambda: "Fetching profiles" in stream.getvalue())
        line.stop()
        assert stream.getvalue().endswith("\r\033[K")

    def test_silent_when_not_a_terminal(self):
        stream = io.StringIO()
        line = StatusLine(stream)
        line.start("Fetching profiles")
        time.sleep(0.3)
        line.stop()
        assert stream.getvalue() == ""

    def test_delay(self):
        stream = FakeTTY()
        line = StatusLine(stream)
        line.start("Waiting", delay=10)
        time.sleep(0.3)
        line.stop()
        assert "Waiting" not in stream.getvalue()

    def test_note_is_shown(self):
        stream = FakeTTY()
        line = StatusLine(stream)
        line.start("Fetching")
        line.note("Rate limited by NextDNS; retrying in 4s")
        assert wait_for(lambda: "retrying in 4s" in stream.getvalue())
        line.stop()


class TestRequestIndicator:
    def test_owns_the_line_only_when_nothing_else_does(self):
        line = StatusLine(FakeTTY())
        indicator = RequestIndicator(line)
        indicator.started("GET", "profiles")
        assert line.active
        indicator.finished()
        assert not line.active

        line.start("Pulling")
        indicator.started("GET", "profiles")
        indicator.finished()
        assert line.active  # the specific status stays
        line.stop()

    def test_wraps_rate_limit_waits(self, fake, client):
        events = []

        class Hooks:
            def started(self, method, path):
                events.append("started")

            def finished(self):
                events.append("finished")

        fake.add_profile("home")
        client.hooks = Hooks()
        fake.fail_next = [FakeResponse(429, {"errors": [{"code": "tooManyRequests"}]})] * 2
        client.list_profiles()
        assert events == ["started", "finished"]  # one span around the retries, not one per attempt


def test_handler_routes_info_to_the_status_line(monkeypatch):
    status_mod = importlib.import_module("nextdnsctl.cli.status")  # the package's `status` is the line itself

    line = StatusLine(FakeTTY())
    monkeypatch.setattr(status_mod, "status", line)
    handler = StatusAwareHandler(verbose=False)
    record = logging.LogRecord("nextdnsctl.client", logging.INFO, __file__, 1, "retrying in 2s", None, None)
    line.start("Fetching")
    handler.emit(record)
    assert line._note == "retrying in 2s"
    line.stop()


class TestPrefetch:
    def test_parallel_fetch_is_used_once(self, fake, client):
        for name in ("a", "b", "c"):
            fake.add_profile(name, name * 3)
        messages = []
        planner = Planner(client, progress=messages.append)
        planner.prefetch(planner.profiles())
        gets = [r for r in fake.requests if r[0] == "GET" and r[1].startswith("profiles/")]
        assert len(gets) == 3
        assert messages[-1] == "Fetching 3 profiles (3/3 done)"

        planner.live("aaa")
        planner.live("aaa")  # the second call fetches fresh data
        gets = [r for r in fake.requests if r[0] == "GET" and r[1] == "profiles/aaa"]
        assert len(gets) == 2

    def test_errors_propagate(self, fake, client):
        planner = Planner(client)
        with pytest.raises(Exception):
            planner.prefetch([{"id": "missing", "name": "missing"}])
