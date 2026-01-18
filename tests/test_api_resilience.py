"""Tests for API error handling and resilience."""

import pytest
from unittest.mock import Mock

from nextdnsctl.api import api_call, RateLimitStillActiveError


class TestRetryOn500:
    """Tests for retry behavior on server errors."""

    def test_retries_on_500_then_succeeds(self, mocker):
        """Should retry on 500 errors and succeed when server recovers."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")
        mocker.patch("nextdnsctl.api.time.sleep")  # Don't actually sleep

        # First two calls fail with 500, third succeeds
        mock_response_fail = Mock()
        mock_response_fail.status_code = 500

        mock_response_ok = Mock()
        mock_response_ok.status_code = 200
        mock_response_ok.json.return_value = {"success": True}

        mock_req.side_effect = [mock_response_fail, mock_response_fail, mock_response_ok]

        result = api_call("GET", "test", retries=3)

        assert result == {"success": True}
        assert mock_req.call_count == 3

    def test_fails_after_exhausting_retries(self, mocker):
        """Should raise exception after all retries exhausted."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")
        mocker.patch("nextdnsctl.api.time.sleep")

        mock_response = Mock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"errors": [{"detail": "Internal error"}]}
        mock_req.return_value = mock_response

        with pytest.raises(Exception, match="Internal error"):
            api_call("GET", "test", retries=2)

        assert mock_req.call_count == 3  # Initial + 2 retries


class TestRateLimiting:
    """Tests for rate limit (429) handling."""

    def test_respects_retry_after_header(self, mocker):
        """Should sleep for Retry-After seconds when rate limited."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")

        # First call: 429 with Retry-After, second call: success
        mock_rate_limited = Mock()
        mock_rate_limited.status_code = 429
        mock_rate_limited.headers = {"Retry-After": "5"}

        mock_ok = Mock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"data": []}

        mock_req.side_effect = [mock_rate_limited, mock_ok]

        api_call("GET", "test", retries=1)

        mock_sleep.assert_called_with(5)

    def test_uses_default_pause_without_retry_after(self, mocker):
        """Should use default pause when no Retry-After header."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")
        mock_sleep = mocker.patch("nextdnsctl.api.time.sleep")

        mock_rate_limited = Mock()
        mock_rate_limited.status_code = 429
        mock_rate_limited.headers = {}  # No Retry-After

        mock_ok = Mock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"data": []}

        mock_req.side_effect = [mock_rate_limited, mock_ok]

        api_call("GET", "test", retries=1)

        # Default pause is 60 seconds (DEFAULT_PATIENT_RETRY_PAUSE_SECONDS)
        mock_sleep.assert_called_with(60)

    def test_raises_rate_limit_error_after_exhaustion(self, mocker):
        """Should raise RateLimitStillActiveError when rate limit persists."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")
        mocker.patch("nextdnsctl.api.time.sleep")

        mock_response = Mock()
        mock_response.status_code = 429
        mock_response.headers = {}  # No Retry-After
        mock_req.return_value = mock_response

        with pytest.raises(RateLimitStillActiveError):
            api_call("GET", "test", retries=2)


class TestNetworkErrors:
    """Tests for network error handling."""

    def test_retries_on_network_error(self, mocker):
        """Should retry on network exceptions."""
        from requests.exceptions import ConnectionError

        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")
        mocker.patch("nextdnsctl.api.time.sleep")

        mock_ok = Mock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = {"success": True}

        # First two calls fail with network error, third succeeds
        mock_req.side_effect = [
            ConnectionError("Connection refused"),
            ConnectionError("Connection refused"),
            mock_ok,
        ]

        result = api_call("GET", "test", retries=3)

        assert result == {"success": True}
        assert mock_req.call_count == 3


class TestSuccessResponses:
    """Tests for successful response handling."""

    def test_handles_204_no_content(self, mocker):
        """Should return None for 204 responses."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")

        mock_response = Mock()
        mock_response.status_code = 204
        mock_req.return_value = mock_response

        result = api_call("DELETE", "test/resource")

        assert result is None

    def test_handles_201_created(self, mocker):
        """Should handle 201 Created responses."""
        mock_req = mocker.patch("nextdnsctl.api.requests.request")
        mocker.patch("nextdnsctl.api.load_api_key", return_value="fake-key")

        mock_response = Mock()
        mock_response.status_code = 201
        mock_response.json.return_value = {"id": "new-resource"}
        mock_req.return_value = mock_response

        result = api_call("POST", "test")

        assert result == {"id": "new-resource"}
