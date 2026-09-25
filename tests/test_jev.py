from __future__ import annotations

import io
import json
import urllib.error

import pytest

from autoloop.jev import (
    DEFAULT_API_KEY_ENV,
    JEV_ENDPOINT,
    JevError,
    JevResult,
    evaluate,
    probability,
)

SAMPLE_RESPONSE = {
    "answers": {
        "well_formed": {
            "type": "boolean",
            "probability": 0.5,
        }
    },
    "rounding": {"probabilityDecimals": 2, "scoreDecimals": 2},
    "usage": {"inputTokens": 337, "outputTokens": 23},
    "warnings": [],
    "providerMetadata": {
        "typesafe": {"confidence": {}},
        "gateway": {
            "routing": {
                "originalModelId": "typesafe-ai/jev",
                "resolvedProvider": "digitalocean",
                "fallbacksAvailable": ["typesafe-ai"],
                "canonicalSlug": "typesafe-ai/jev",
                "finalProvider": "typesafe-ai",
                "modelAttemptCount": 1,
            },
            "cost": "0",
            "marketCost": "0.000014154",
            "surchargeCost": "0",
            "gatewayCost": "0",
            "generationId": "gen_01M3BN3ACG1QA8BPPB1W2FP42P",
        },
    },
}


def _mock_urlopen(monkeypatch, response_bytes):
    """Patch urllib.request.urlopen to return a fake response."""

    class FakeResponse:
        def __init__(self):
            self._data = response_bytes

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    calls = []

    def fake_urlopen(req, *, timeout=None):
        calls.append((req, timeout))
        return FakeResponse()

    monkeypatch.setattr("autoloop.jev.urllib.request.urlopen", fake_urlopen)
    return calls


class TestEvaluateSuccess:
    def test_returns_jev_result(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        result = evaluate("some state", {"well_formed": {"type": "boolean"}})

        assert isinstance(result, JevResult)
        assert result.answers == SAMPLE_RESPONSE["answers"]
        assert result.raw == SAMPLE_RESPONSE
        assert result.latency_seconds > 0

    def test_explicit_api_key(self, monkeypatch):
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
        calls = _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        result = evaluate("state", {"q": {}}, api_key="explicit-key")

        assert result.answers == SAMPLE_RESPONSE["answers"]
        req = calls[0][0]
        assert req.get_header("Authorization") == "Bearer explicit-key"

    def test_sends_correct_headers_and_body(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        calls = _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        questions = {"well_formed": {"type": "boolean"}}
        evaluate("my state", questions)

        req = calls[0][0]
        assert req.full_url == JEV_ENDPOINT
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "Bearer test-key"
        assert req.get_method() == "POST"
        body = json.loads(req.data)
        assert body == {"state": "my state", "questions": questions}

    def test_timeout_passed_to_urlopen(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        calls = _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        evaluate("state", {}, timeout=5.0)

        assert calls[0][1] == 5.0

    def test_api_key_env_override(self, monkeypatch):
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
        monkeypatch.setenv("CUSTOM_JEV_KEY", "custom-key")
        calls = _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        result = evaluate("state", {"q": {}}, api_key_env="CUSTOM_JEV_KEY")

        assert result.answers == SAMPLE_RESPONSE["answers"]
        req = calls[0][0]
        assert req.get_header("Authorization") == "Bearer custom-key"

    def test_api_key_env_missing_raises(self, monkeypatch):
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)
        monkeypatch.delenv("CUSTOM_JEV_KEY", raising=False)

        with pytest.raises(JevError, match="CUSTOM_JEV_KEY"):
            evaluate("state", {}, api_key_env="CUSTOM_JEV_KEY")


class TestEvaluateErrors:
    def test_http_error(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")

        def fake_urlopen(req, *, timeout=None):
            raise urllib.error.HTTPError(JEV_ENDPOINT, 403, "Forbidden", {}, io.BytesIO(b""))

        monkeypatch.setattr("autoloop.jev.urllib.request.urlopen", fake_urlopen)

        with pytest.raises(JevError, match="HTTP 403"):
            evaluate("state", {})

    def test_timeout_error(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")

        def fake_urlopen(req, *, timeout=None):
            raise urllib.error.URLError("timed out")

        monkeypatch.setattr("autoloop.jev.urllib.request.urlopen", fake_urlopen)

        with pytest.raises(JevError, match="Request failed"):
            evaluate("state", {})

    def test_malformed_json(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        _mock_urlopen(monkeypatch, b"not json")

        with pytest.raises(JevError, match="Malformed response body"):
            evaluate("state", {})

    def test_missing_answers_key(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        _mock_urlopen(monkeypatch, json.dumps({"usage": {}}).encode())

        with pytest.raises(JevError, match="missing 'answers' key"):
            evaluate("state", {})

    def test_no_api_key(self, monkeypatch):
        monkeypatch.delenv(DEFAULT_API_KEY_ENV, raising=False)

        with pytest.raises(JevError, match="No API key"):
            evaluate("state", {})


class TestProbability:
    def test_returns_probability_for_valid_key(self):
        result = JevResult(
            answers={"well_formed": {"type": "boolean", "probability": 0.5}},
            latency_seconds=0.1,
            raw={},
        )
        assert probability(result, "well_formed") == 0.5

    def test_returns_none_for_missing_key(self):
        result = JevResult(
            answers={"well_formed": {"type": "boolean", "probability": 0.5}},
            latency_seconds=0.1,
            raw={},
        )
        assert probability(result, "missing_key") is None
