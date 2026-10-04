"""Client transport: pacing, rate limits, retries and error mapping."""

import pytest
import requests

from nextdnsctl.client import (
    MAX_BODY_BYTES,
    APIError,
    Client,
    NetworkError,
    PayloadTooLargeError,
    RateLimitError,
)

from .fake_api import API_KEY, FakeResponse


class TestPacing:
    def test_writes_are_spaced_one_second_apart(self, fake, client, clock):
        profile = fake.add_profile("home")
        start = clock.now
        for i in range(5):
            client.add_item(profile["id"], "denylist", {"id": f"d{i}.com"})
        assert clock.now - start == pytest.approx(4.0)

    def test_sixty_writes_per_minute_never_hit_the_limit(self, fake, client, clock):
        profile = fake.add_profile("home")
        for i in range(150):
            client.add_item(profile["id"], "denylist", {"id": f"d{i}.com"})
        statuses = [r for r in fake.requests if r[0] == "POST"]
        assert len(statuses) == 150  # no retries were needed
        assert len(fake.profiles[profile["id"]]["denylist"]) == 150

    def test_reads_are_paced_separately_from_writes(self, fake, client, clock):
        profile = fake.add_profile("home")
        client.add_item(profile["id"], "denylist", {"id": "a.com"})
        before = clock.now
        client.get_profile(profile["id"])
        assert clock.now == before  # a read doesn't wait for the write pacer


class TestRateLimits:
    def test_waits_out_a_rate_limit_from_another_client(self, fake, client, clock):
        profile = fake.add_profile("home")
        # Someone else used the whole write budget for this window.
        fake._windows["write"] = [clock.now, 60]
        client.add_item(profile["id"], "denylist", {"id": "a.com"})
        assert fake.profiles[profile["id"]]["denylist"][0]["id"] == "a.com"
        assert clock.slept >= 60

    def test_gives_up_after_a_full_window(self, fake, client):
        profile = fake.add_profile("home")
        fake.fail_next = [FakeResponse(429, {"errors": [{"code": "tooManyRequests"}]})] * 20
        with pytest.raises(RateLimitError, match="another program"):
            client.add_item(profile["id"], "denylist", {"id": "a.com"})


class TestErrors:
    def test_body_over_100_kib_is_refused_before_sending(self, fake, client):
        profile = fake.add_profile("home")
        body = {"denylist": [{"id": f"domain-number-{i}.example.com"} for i in range(5000)]}
        with pytest.raises(PayloadTooLargeError, match=str(MAX_BODY_BYTES)):
            client.patch_profile(profile["id"], body)
        assert not fake.writes()

    def test_200_with_errors_is_an_error(self, fake, client):
        profile = fake.add_profile("home", denylist=[{"id": "a.com", "active": True}])
        with pytest.raises(APIError) as info:
            client.add_item(profile["id"], "denylist", {"id": "a.com"})
        assert info.value.status == 200
        assert info.value.has_code("duplicate")

    def test_error_pointer_is_exposed(self, fake, client):
        profile = fake.add_profile("home")
        with pytest.raises(APIError) as info:
            client.patch_profile(profile["id"], {"denylist": [{"id": "ok.com"}, {"id": "BAD"}]})
        assert info.value.pointers == ["/denylist/1/id"]

    def test_retries_server_errors(self, fake, client):
        fake.add_profile("home")
        fake.fail_next = [FakeResponse(502), FakeResponse(503)]
        assert len(client.list_profiles()) == 1

    def test_network_errors_are_retried_then_raised(self, clock):
        class Broken:
            headers: dict = {}

            def request(self, *args, **kwargs):
                raise requests.ConnectionError("down")

        client = Client(API_KEY, session=Broken(), clock=clock, sleep=clock.sleep, retries=2)
        with pytest.raises(NetworkError, match="down"):
            client.list_profiles()

    def test_wrong_key(self, fake, clock):
        client = Client("wrong", session=fake, clock=clock, sleep=clock.sleep)
        with pytest.raises(APIError) as info:
            client.list_profiles()
        assert info.value.status == 403


class TestLogs:
    def test_follows_the_cursor(self, fake, client):
        profile = fake.add_profile("home")
        fake.logs[profile["id"]] = [{"domain": f"d{i}.com", "status": "default"} for i in range(250)]
        entries = list(client.iter_logs(profile["id"]))
        assert len(entries) == 250
