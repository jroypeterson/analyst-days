"""Scanner fail-loud tests: EDGAR/Tavily hard failures must raise (not return an
empty list), so cmd_discover counts an error and the heartbeat goes 'partial'
instead of a silent zero-result keeping health green."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import pytest

from src.discovery import scan_edgar
from src.discovery import scan_tavily


def test_edgar_company_lookup_failure_raises(monkeypatch):
    scan_edgar._IDENTITY_SET = True  # skip real identity call

    def _boom(_ticker):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(scan_edgar, "Company", _boom)
    with pytest.raises(scan_edgar.EdgarScanError):
        scan_edgar.scan_ticker("AAPL")


def test_edgar_get_filings_failure_raises(monkeypatch):
    scan_edgar._IDENTITY_SET = True

    class _C:
        def __init__(self, _ticker):
            pass

        def get_filings(self, **_kw):
            raise RuntimeError("EDGAR 503")

    monkeypatch.setattr(scan_edgar, "Company", _C)
    with pytest.raises(scan_edgar.EdgarScanError):
        scan_edgar.scan_ticker("AAPL")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_tavily_200_error_payload_raises(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(
        scan_tavily.requests, "post",
        lambda *a, **k: _FakeResp({"error": "rate_limited",
                                   "message": "quota exceeded"}),
    )
    with pytest.raises(scan_tavily.TavilyScanError):
        scan_tavily.search_ticker("AAPL", "Apple Inc.")


def test_tavily_missing_results_key_raises(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(
        scan_tavily.requests, "post",
        lambda *a, **k: _FakeResp({"query": "x"}),  # no 'results' key
    )
    with pytest.raises(scan_tavily.TavilyScanError):
        scan_tavily.search_ticker("AAPL", "Apple Inc.")


def test_tavily_empty_results_is_ok(monkeypatch):
    """A genuine empty result set (results: []) is NOT an error."""
    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    monkeypatch.setattr(
        scan_tavily.requests, "post",
        lambda *a, **k: _FakeResp({"results": []}),
    )
    assert scan_tavily.search_ticker("AAPL", "Apple Inc.") == []
