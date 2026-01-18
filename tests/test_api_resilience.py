"""Tests for API error handling and resilience."""

import pytest
from unittest.mock import Mock, patch

from nextdnsctl.api import APIClient, RateLimitStillActiveError


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
