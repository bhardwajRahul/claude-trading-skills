"""Tests for fetch_calendar.py"""

import json
import os
import sys
import urllib.error
from typing import Any

import pytest

# Add parent directory to path so we can import the script module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fetch_calendar  # noqa: E402

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Fake urlopen() context manager returning a JSON-serializable payload."""

    def __init__(self, payload: Any):
        self._body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def read(self):
        return self._body


def _events(tiers: list[Any]) -> list[dict[str, Any]]:
    return [{"event": f"event-{i}", "market_tier": tier} for i, tier in enumerate(tiers)]


# ---------------------------------------------------------------------------
# _tier_rank
# ---------------------------------------------------------------------------


class TestTierRank:
    def test_int_passthrough(self):
        assert fetch_calendar._tier_rank(1) == 1
        assert fetch_calendar._tier_rank(3) == 3

    @pytest.mark.parametrize(
        "value",
        [0, -1, 4, 99, True, "2", "HIGH", None],
    )
    def test_only_api_contract_tiers_are_accepted(self, value):
        assert fetch_calendar._tier_rank(value) is None


# ---------------------------------------------------------------------------
# fetch_calendar
# ---------------------------------------------------------------------------


class TestFetchCalendar:
    @pytest.mark.parametrize("currency", ["us", "usd\nkey", "u$d", "円円円"])
    def test_invalid_currency_is_rejected_before_request(self, monkeypatch, currency):
        def unexpected_urlopen(*args, **kwargs):
            pytest.fail("urlopen must not run for an invalid currency")

        monkeypatch.setattr(fetch_calendar.urllib.request, "urlopen", unexpected_urlopen)

        with pytest.raises(RuntimeError, match="3-letter ASCII code"):
            fetch_calendar.fetch_calendar(currency, 50, 1)

    def test_numeric_tier_filter(self, monkeypatch):
        payload = {
            "currency": "USD",
            "timezone": "UTC",
            "data_quality": "official",
            "data": _events([1, 2, 3]),
        }
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        result = fetch_calendar.fetch_calendar("usd", 50, 1)
        assert [e["market_tier"] for e in result["events"]] == [1]

    @pytest.mark.parametrize("tier", [0, -1, 4, 99, True, "2", "HIGH", None])
    def test_out_of_contract_tier_fails_closed(self, monkeypatch, tier):
        payload = {"data": _events([tier])}
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )

        with pytest.raises(RuntimeError, match="invalid market_tier"):
            fetch_calendar.fetch_calendar("usd", 50, 3)

    def test_invalid_min_tier_fails_before_request(self, monkeypatch):
        def unexpected_urlopen(*args, **kwargs):
            pytest.fail("urlopen must not run for an invalid min_tier")

        monkeypatch.setattr(fetch_calendar.urllib.request, "urlopen", unexpected_urlopen)

        for min_tier in (0, -1, 4, 99, True):
            with pytest.raises(RuntimeError, match="min_tier must be one of"):
                fetch_calendar.fetch_calendar("usd", 50, min_tier)

    def test_limit_clamped_above_100(self, monkeypatch):
        payload = {"data": _events([1] * 150)}
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        result = fetch_calendar.fetch_calendar("usd", 500, None)
        assert len(result["events"]) == 100

    def test_limit_clamped_below_1(self, monkeypatch):
        payload = {"data": _events([1] * 5)}
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        result = fetch_calendar.fetch_calendar("usd", 0, None)
        assert len(result["events"]) == 1

    def test_valid_empty_data_returns_empty_events(self, monkeypatch):
        monkeypatch.setattr(
            fetch_calendar.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse({"data": []}),
        )
        result = fetch_calendar.fetch_calendar("usd", 50, 1)
        assert result["events"] == []

    def test_malformed_payload_not_dict_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            fetch_calendar.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse(["not", "a", "dict"]),
        )

        with pytest.raises(RuntimeError, match="top-level JSON must be an object"):
            fetch_calendar.fetch_calendar("usd", 50, 1)

    def test_missing_data_fails_closed(self, monkeypatch):
        monkeypatch.setattr(
            fetch_calendar.urllib.request,
            "urlopen",
            lambda *a, **k: _FakeResponse({"currency": "USD"}),
        )

        with pytest.raises(RuntimeError, match="missing required data field"):
            fetch_calendar.fetch_calendar("usd", 50, 1)

    def test_malformed_payload_data_not_list_fails_closed(self, monkeypatch):
        payload = {"data": "not-a-list"}
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )

        with pytest.raises(RuntimeError, match="data field must be an array"):
            fetch_calendar.fetch_calendar("usd", 50, 1)

    def test_malformed_payload_data_list_of_non_dicts_fails_closed(self, monkeypatch):
        payload = {"data": ["oops", 3, None]}
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )

        with pytest.raises(RuntimeError, match=r"data\[0\] must be an object"):
            fetch_calendar.fetch_calendar("usd", 50, 1)

    @pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
    def test_non_finite_number_in_event_fails_closed(self, monkeypatch, non_finite):
        payload = {
            "data": [
                {
                    "event": "CPI",
                    "market_tier": 1,
                    "details": {"actual": non_finite},
                }
            ]
        }
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )

        with pytest.raises(RuntimeError, match="non-finite number"):
            fetch_calendar.fetch_calendar("usd", 50, 1)

    def test_overflowed_json_number_fails_closed(self, monkeypatch):
        class RawResponse:
            def __enter__(self):
                return self

            def __exit__(self, *exc_info):
                return False

            def read(self):
                return b'{"data":[{"event":"CPI","market_tier":1,"actual":1e309}]}'

        monkeypatch.setattr(fetch_calendar.urllib.request, "urlopen", lambda *a, **k: RawResponse())

        with pytest.raises(RuntimeError, match="non-finite number"):
            fetch_calendar.fetch_calendar("usd", 50, 1)

    def test_uses_canonical_api_host(self, monkeypatch):
        seen_urls = []

        def fake_urlopen(request, **kwargs):
            seen_urls.append(request.full_url)
            return _FakeResponse({"data": []})

        monkeypatch.setattr(fetch_calendar.urllib.request, "urlopen", fake_urlopen)

        fetch_calendar.fetch_calendar("usd", 50, 1)

        assert seen_urls[0].startswith("https://api.fxmacrodata.com/v1/calendar/usd?")


# ---------------------------------------------------------------------------
# main() error handling / key redaction
# ---------------------------------------------------------------------------


class TestMainErrorHandling:
    def test_cli_rejects_out_of_range_min_tier(self, monkeypatch, capsys):
        monkeypatch.setattr(
            sys,
            "argv",
            ["fetch_calendar.py", "--currency", "usd", "--min-tier", "99"],
        )

        with pytest.raises(SystemExit) as exc_info:
            fetch_calendar.main()

        assert exc_info.value.code != 0
        assert "invalid choice" in capsys.readouterr().err

    def test_invalid_currency_never_leaks_api_key_or_traceback(self, monkeypatch, capsys):
        monkeypatch.setenv("FXMACRODATA_API_KEY", "REVIEW_SECRET_123")
        monkeypatch.setattr(
            sys,
            "argv",
            ["fetch_calendar.py", "--currency", "usd\napi_key=REVIEW_SECRET_123"],
        )

        with pytest.raises(SystemExit) as exc_info:
            fetch_calendar.main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "REVIEW_SECRET_123" not in captured.err
        assert "REVIEW_SECRET_123" not in captured.out
        assert "Traceback" not in captured.err
        assert "3-letter ASCII code" in captured.err

    def test_http_error_exits_nonzero_and_never_leaks_api_key(self, monkeypatch, capsys):
        secret_url = f"{fetch_calendar.FXMACRODATA_BASE_URL}/calendar/usd?api_key=SECRET"

        def fake_urlopen(*a, **k):
            raise urllib.error.HTTPError(secret_url, 401, "Unauthorized", None, None)

        monkeypatch.setenv("FXMACRODATA_API_KEY", "SECRET")
        monkeypatch.setattr(fetch_calendar.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(sys, "argv", ["fetch_calendar.py", "--currency", "usd"])

        with pytest.raises(SystemExit) as exc_info:
            fetch_calendar.main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "SECRET" not in captured.err
        assert "SECRET" not in captured.out

    def test_url_error_exits_nonzero(self, monkeypatch, capsys):
        def fake_urlopen(*a, **k):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(fetch_calendar.urllib.request, "urlopen", fake_urlopen)
        monkeypatch.setattr(sys, "argv", ["fetch_calendar.py", "--currency", "usd"])

        with pytest.raises(SystemExit) as exc_info:
            fetch_calendar.main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert "connection refused" in captured.err

    def test_non_finite_api_value_exits_nonzero_without_invalid_json(self, monkeypatch, capsys):
        payload = {"data": [{"event": "CPI", "market_tier": 1, "actual": float("nan")}]}
        monkeypatch.setattr(
            fetch_calendar.urllib.request, "urlopen", lambda *a, **k: _FakeResponse(payload)
        )
        monkeypatch.setattr(sys, "argv", ["fetch_calendar.py", "--currency", "usd"])

        with pytest.raises(SystemExit) as exc_info:
            fetch_calendar.main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "non-finite number" in captured.err
        assert "NaN" not in captured.out
        assert "Infinity" not in captured.out

    def test_allow_nan_false_is_a_serialization_backstop(self, monkeypatch, capsys):
        monkeypatch.setattr(
            fetch_calendar,
            "fetch_calendar",
            lambda *a, **k: {"events": [], "actual": float("nan")},
        )
        monkeypatch.setattr(sys, "argv", ["fetch_calendar.py", "--currency", "usd"])

        with pytest.raises(SystemExit) as exc_info:
            fetch_calendar.main()

        assert exc_info.value.code != 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "could not be serialized" in captured.err
