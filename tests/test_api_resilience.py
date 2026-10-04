"""Tests for API error handling and resilience."""

import threading
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, patch

import pytest

from nextdnsctl.api import APIClient, APIError, RateLimitStillActiveError, _parse_retry_after


class TestRetryOn500:
    """Tests for retry behavior on server errors."""

    def test_retries_on_500_then_succeeds(self, mocker):
        """Should retry on 500 errors and succeed when server recovers."""
        mocker.patch("nextdnsctl.api.time.sleep")  # Don't actually sleep

        # First two calls fail with 500, third succeeds
        mock_response_fail = Mock()
        mock_response_fail.status_code = 500

        mock_response_ok = Mock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"success": True}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.side_effect = [
                mock_response_fail,
                mock_response_fail,
                mock_response_ok,
            ]
            MockSession.return_value = mock_session

            client = APIClient("fake-key", retries=3)
            result = client.call("GET", "test")

            assert result == {"success": True}
            assert mock_session.request.call_count == 3

    def test_fails_after_exhausting_retries(self, mocker):
        """Should raise exception after all retries exhausted."""
        mocker.patch("nextdnsctl.api.time.sleep")

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"errors": [{"detail": "Internal error"}]}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.return_value = mock_response
            MockSession.return_value = mock_session

            client = APIClient("fake-key", retries=2)
            with pytest.raises(Exception, match="Internal error"):
                client.call("GET", "test")

            assert mock_session.request.call_count == 3  # Initial + 2 retries


class TestRateLimiting:
    """Tests for rate limit (429) handling."""

    def test_respects_retry_after_header(self, mocker):
        """Should sleep for Retry-After seconds when rate limited."""
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")

        # First call: 429 with Retry-After, second call: success
        mock_rate_limited = Mock()
        mock_rate_limited.status_code = 429
        mock_rate_limited.headers = {"Retry-After": "5"}

        mock_ok = Mock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"data": []}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.side_effect = [mock_rate_limited, mock_ok]
            MockSession.return_value = mock_session

            client = APIClient("fake-key", retries=1)
            client.call("GET", "test")

            mock_sleep.assert_called_with(5)

    def test_uses_default_pause_without_retry_after(self, mocker):
        """Should use default pause when no Retry-After header."""
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")

        mock_rate_limited = Mock()
        mock_rate_limited.status_code = 429
        mock_rate_limited.headers = {}  # No Retry-After

        mock_ok = Mock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"data": []}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.side_effect = [mock_rate_limited, mock_ok]
            MockSession.return_value = mock_session

            client = APIClient("fake-key", retries=1)
            client.call("GET", "test")

            # Default pause is 60 seconds (DEFAULT_PATIENT_RETRY_PAUSE_SECONDS)
            mock_sleep.assert_called_with(60)

    def test_raises_rate_limit_error_after_exhaustion(self, mocker):
        """Should raise RateLimitStillActiveError when rate limit persists."""
        mocker.patch("nextdnsctl.api.time.sleep")

        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {}  # No Retry-After

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.return_value = mock_response
            MockSession.return_value = mock_session

            client = APIClient("fake-key", retries=2)
            with pytest.raises(RateLimitStillActiveError):
                client.call("GET", "test")


class TestRetryAfterParsing:
    """Tests for Retry-After header parsing."""

    def test_seconds(self):
        assert _parse_retry_after("5") == 5.0

    def test_http_date(self):
        retry_at = datetime.now(timezone.utc) + timedelta(seconds=30)
        parsed = _parse_retry_after(format_datetime(retry_at, usegmt=True))
        assert parsed is not None and 25 <= parsed <= 31

    def test_date_in_the_past_is_zero(self):
        assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_missing_or_garbage(self):
        assert _parse_retry_after(None) is None
        assert _parse_retry_after("") is None
        assert _parse_retry_after("soon") is None

    def test_http_date_header_does_not_crash(self, mocker):
        """A Retry-After HTTP date used to crash with int(); it should be honoured instead."""
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")
        rate_limited = Mock(status_code=429, headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})
        ok = Mock(status_code=200)
        ok.json.return_value = {"data": []}

        with patch("requests.Session") as MockSession:
            MockSession.return_value.request.side_effect = [rate_limited, ok]
            APIClient("fake-key", retries=1).call("GET", "test")

        mock_sleep.assert_called_with(0.0)

    def test_exhaustion_with_retry_after_raises_rate_limit_error(self, mocker):
        """Persistent 429s must raise RateLimitStillActiveError so the CLI aborts the batch."""
        mocker.patch("nextdnsctl.api.time.sleep")
        rate_limited = Mock(status_code=429, headers={"Retry-After": "1"})

        with patch("requests.Session") as MockSession:
            MockSession.return_value.request.return_value = rate_limited
            with pytest.raises(RateLimitStillActiveError):
                APIClient("fake-key", retries=1).call("GET", "test")


class TestDiagnosticsOutput:
    """Retry messages must not pollute stdout (e.g. 'export -' output)."""

    def test_retry_messages_go_to_stderr(self, mocker, capsys):
        mocker.patch("nextdnsctl.api.time.sleep")
        server_error = Mock(status_code=500)
        ok = Mock(status_code=200)
        ok.json.return_value = {"data": []}

        with patch("requests.Session") as MockSession:
            MockSession.return_value.request.side_effect = [server_error, ok]
            APIClient("fake-key", retries=1).call("GET", "test")

        captured = capsys.readouterr()
        assert captured.out == ""
        assert "Server error" in captured.err


class TestSharedRateLimitPause:
    """A rate limit seen by one thread should hold off the other threads too."""

    def test_other_threads_wait_for_pause(self, mocker):
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")
        client = APIClient("fake-key")
        client._pause_all(60)

        result = []
        worker = threading.Thread(target=lambda: result.append(client._wait_for_pause()))
        worker.start()
        worker.join()

        assert mock_sleep.call_count == 1
        assert 59 <= mock_sleep.call_args[0][0] <= 60

    def test_pausing_thread_does_not_wait_twice(self, mocker):
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")
        client = APIClient("fake-key")
        client._pause_all(60)
        client._wait_for_pause()
        mock_sleep.assert_not_called()


class TestNetworkErrors:
    """Tests for network error handling."""

    def test_retries_on_network_error(self, mocker):
        """Should retry on network exceptions."""
        from requests.exceptions import ConnectionError

        mocker.patch("nextdnsctl.api.time.sleep")

        mock_ok = Mock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"success": True}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            # First two calls fail with network error, third succeeds
            mock_session.request.side_effect = [
                ConnectionError("Connection refused"),
                ConnectionError("Connection refused"),
                mock_ok,
            ]
            MockSession.return_value = mock_session

            client = APIClient("fake-key", retries=3)
            result = client.call("GET", "test")

            assert result == {"success": True}
            assert mock_session.request.call_count == 3


class TestSuccessResponses:
    """Tests for successful response handling."""

    def test_handles_204_no_content(self, mocker):
        """Should return None for 204 responses."""
        mock_response = Mock()
        mock_response.status_code = 204

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.return_value = mock_response
            MockSession.return_value = mock_session

            client = APIClient("fake-key")
            result = client.call("DELETE", "test/resource")

            assert result is None

    def test_handles_201_created(self, mocker):
        """Should handle 201 Created responses."""
        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "new-resource"}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.return_value = mock_response
            MockSession.return_value = mock_session

            client = APIClient("fake-key")
            result = client.call("POST", "test")

            assert result == {"id": "new-resource"}


class TestApiErrorResponses:
    """Tests for API error payload handling."""

    def test_raises_error_for_200_response_with_errors_body(self):
        """NextDNS may return error payloads with a 200 status."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"errors": [{"code": "duplicate"}]}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.return_value = mock_response
            MockSession.return_value = mock_session

            client = APIClient("fake-key")
            with pytest.raises(APIError, match="duplicate"):
                client.call("POST", "test")


class TestSessionReuse:
    """Tests for session/connection reuse."""

    def test_session_headers_set_once(self):
        """Should set headers on session creation."""
        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            MockSession.return_value = mock_session

            APIClient("test-api-key")

            # Verify headers were updated
            mock_session.headers.update.assert_called_once()
            call_args = mock_session.headers.update.call_args[0][0]
            assert call_args["X-Api-Key"] == "test-api-key"
            assert "User-Agent" in call_args

    def test_same_session_used_for_multiple_calls(self, mocker):
        """Should reuse the same session for multiple API calls."""
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"data": []}

        with patch("requests.Session") as MockSession:
            mock_session = Mock()
            mock_session.request.return_value = mock_response
            MockSession.return_value = mock_session

            client = APIClient("fake-key")
            client.call("GET", "endpoint1")
            client.call("GET", "endpoint2")
            client.call("POST", "endpoint3")

            # Session should only be created once
            assert MockSession.call_count == 1
            # But request should be called 3 times on the same session
            assert mock_session.request.call_count == 3
