"""Tests for the data client's rate-limit handling and snapshot validation."""

import data_client
import pytest
from data_client import DataClient, load_snapshot_from_json


class _FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload or {}
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _client(tmp_path):
    return DataClient(str(tmp_path), quiet=True)


def test_get_retries_on_429_then_succeeds(tmp_path, monkeypatch):
    responses = [_FakeResponse(429), _FakeResponse(429), _FakeResponse(200, {"ok": 1})]
    monkeypatch.setattr(
        data_client,
        "requests",
        type(
            "R",
            (),
            {
                "get": staticmethod(lambda url, params=None, timeout=None: responses.pop(0)),
            },
        ),
    )
    sleeps = []
    monkeypatch.setattr(data_client.time, "sleep", sleeps.append)
    assert _client(tmp_path)._get("http://x") == {"ok": 1}
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]  # exponential backoff


def test_get_respects_retry_after_header(tmp_path, monkeypatch):
    responses = [
        _FakeResponse(429, headers={"Retry-After": "3"}),
        _FakeResponse(200, {"ok": 1}),
    ]
    monkeypatch.setattr(
        data_client,
        "requests",
        type(
            "R",
            (),
            {
                "get": staticmethod(lambda url, params=None, timeout=None: responses.pop(0)),
            },
        ),
    )
    sleeps = []
    monkeypatch.setattr(data_client.time, "sleep", sleeps.append)
    _client(tmp_path)._get("http://x")
    assert sleeps == [3.0]


def test_get_raises_after_max_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(
        data_client,
        "requests",
        type(
            "R",
            (),
            {
                "get": staticmethod(lambda url, params=None, timeout=None: _FakeResponse(429)),
            },
        ),
    )
    monkeypatch.setattr(data_client.time, "sleep", lambda _s: None)
    with pytest.raises(RuntimeError):
        _client(tmp_path)._get("http://x")


def test_snapshot_validation_requires_btc(tmp_path):
    bad = tmp_path / "snap.json"
    bad.write_text('{"series": {"ETH": [1, 2]}, "dominance_series": [], "funding": {}}')
    with pytest.raises(ValueError):
        load_snapshot_from_json(str(bad))
