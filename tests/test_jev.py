from __future__ import annotations

import io
import json
import urllib.error

import pytest

from autoloop.jev import (
    DEFAULT_API_KEY_ENV,
    GATE_HIGH,
    GATE_LOW,
    JEV_ENDPOINT,
    JevError,
    JevResult,
    evaluate,
    probability,
    should_auto_merge,
    triage,
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


def _mock_evaluate(monkeypatch, answers):
    """Patch evaluate() to return a JevResult with the given answers."""

    def fake_evaluate(state, questions, *, api_key=None, api_key_env=None, timeout=10.0):
        return JevResult(answers=answers, latency_seconds=0.05, raw={"answers": answers})

    monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)


def _mock_evaluate_raises(monkeypatch, error_msg="boom"):
    """Patch evaluate() to raise JevError."""

    def fake_evaluate(state, questions, **kwargs):
        raise JevError(error_msg)

    monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)


# --- evaluate() tests (from #211) ---


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
        assert req.get_header("Ai-model-id") == "typesafe-ai/jev"
        assert req.get_header("Ai-gateway-auth-method") == "api-key"
        assert req.get_header("Ai-gateway-protocol-version") == "0.0.1"
        assert req.get_header("Ai-evaluation-model-specification-version") == "4"
        body = json.loads(req.data)
        assert body == {
            "state": "my state",
            "questions": questions,
            "providerOptions": {},
        }

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


# --- triage() tests ---


class TestTriageQuestionSchema:
    def test_builds_correct_questions(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["questions"] = questions
            return JevResult(
                answers={
                    "well_formed": {"type": "boolean", "probability": 0.91},
                    "needs_decomposition": {"type": "boolean", "probability": 0.08},
                    "route": {
                        "type": "choice",
                        "distribution": {
                            "implement": 0.87,
                            "decompose": 0.10,
                            "reject": 0.03,
                        },
                    },
                },
                latency_seconds=0.05,
                raw={},
            )

        monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)

        triage("some issue text")

        assert captured["questions"] == {
            "well_formed": {"type": "boolean"},
            "needs_decomposition": {"type": "boolean"},
            "route": {
                "type": "choice",
                "choices": ["implement", "decompose", "reject"],
            },
        }

    def test_passes_issue_text_as_state(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["state"] = state
            return JevResult(
                answers={
                    "well_formed": {"type": "boolean", "probability": 0.91},
                    "needs_decomposition": {"type": "boolean", "probability": 0.08},
                    "route": {"type": "choice", "distribution": {}},
                },
                latency_seconds=0.05,
                raw={},
            )

        monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)

        triage("fix the login bug")

        assert captured["state"] == "fix the login bug"


class TestTriageSuccess:
    def test_returns_structured_dict(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "boolean", "probability": 0.91},
                "needs_decomposition": {"type": "boolean", "probability": 0.08},
                "route": {
                    "type": "choice",
                    "distribution": {"implement": 0.87, "decompose": 0.10, "reject": 0.03},
                },
            },
        )

        result = triage("some issue text")

        assert result == {
            "well_formed": 0.91,
            "needs_decomposition": 0.08,
            "route": {"implement": 0.87, "decompose": 0.10, "reject": 0.03},
        }
        assert "fallback" not in result

    def test_high_confidence_reject(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "boolean", "probability": 0.12},
                "needs_decomposition": {"type": "boolean", "probability": 0.05},
                "route": {
                    "type": "choice",
                    "distribution": {"implement": 0.05, "decompose": 0.05, "reject": 0.90},
                },
            },
        )

        result = triage("vague issue")

        assert result["well_formed"] == 0.12
        assert result["route"]["reject"] == 0.90


class TestTriageFallback:
    def test_jev_error_returns_fallback(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "HTTP 500: Internal Server Error")

        result = triage("some issue")

        assert result["fallback"] is True
        assert "HTTP 500" in result["reason"]

    def test_well_formed_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "boolean", "probability": 0.50},
                "needs_decomposition": {"type": "boolean", "probability": 0.08},
                "route": {"type": "choice", "distribution": {}},
            },
        )

        result = triage("ambiguous issue")

        assert result["fallback"] is True
        assert "well_formed" in result["reason"]
        assert "uncertain" in result["reason"]

    def test_needs_decomposition_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "boolean", "probability": 0.90},
                "needs_decomposition": {"type": "boolean", "probability": 0.50},
                "route": {"type": "choice", "distribution": {}},
            },
        )

        result = triage("issue text")

        assert result["fallback"] is True
        assert "needs_decomposition" in result["reason"]

    def test_boundary_values_not_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "boolean", "probability": GATE_LOW},
                "needs_decomposition": {"type": "boolean", "probability": GATE_HIGH},
                "route": {"type": "choice", "distribution": {"implement": 1.0}},
            },
        )

        result = triage("issue text")

        assert "fallback" not in result
        assert result["well_formed"] == GATE_LOW
        assert result["needs_decomposition"] == GATE_HIGH

    def test_custom_gate_thresholds(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "boolean", "probability": 0.45},
                "needs_decomposition": {"type": "boolean", "probability": 0.10},
                "route": {"type": "choice", "distribution": {}},
            },
        )

        result_default = triage("issue text")
        assert result_default["fallback"] is True

        result_custom = triage("issue text", gate_low=0.20, gate_high=0.40)
        assert "fallback" not in result_custom
        assert result_custom["well_formed"] == 0.45

    def test_no_exception_propagates(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "connection refused")

        result = triage("issue text")

        assert isinstance(result, dict)
        assert result["fallback"] is True


# --- should_auto_merge() tests ---


class TestShouldAutoMergeQuestionSchema:
    def test_builds_correct_questions(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["questions"] = questions
            captured["state"] = state
            return JevResult(
                answers={
                    "meets_acceptance_criteria": {"type": "boolean", "probability": 0.95},
                },
                latency_seconds=0.05,
                raw={},
            )

        monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)

        should_auto_merge("issue body", "diff content")

        assert captured["questions"] == {
            "meets_acceptance_criteria": {"type": "boolean"},
        }

    def test_state_includes_issue_and_diff(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["state"] = state
            return JevResult(
                answers={
                    "meets_acceptance_criteria": {"type": "boolean", "probability": 0.95},
                },
                latency_seconds=0.05,
                raw={},
            )

        monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)

        should_auto_merge("fix login", "+def login():")

        assert "fix login" in captured["state"]
        assert "+def login():" in captured["state"]


class TestShouldAutoMergeSuccess:
    def test_returns_structured_dict(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "boolean", "probability": 0.95},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert result == {"meets_acceptance_criteria": 0.95}
        assert "fallback" not in result

    def test_includes_readiness_score_when_present(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {
                    "type": "boolean",
                    "probability": 0.88,
                    "score": 0.92,
                },
            },
        )

        result = should_auto_merge("issue", "diff")

        assert result["meets_acceptance_criteria"] == 0.88
        assert result["readiness_score"] == 0.92

    def test_omits_readiness_score_when_absent(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "boolean", "probability": 0.88},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert "readiness_score" not in result


class TestShouldAutoMergeFallback:
    def test_jev_error_returns_fallback(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "Request failed: timed out")

        result = should_auto_merge("issue", "diff")

        assert result["fallback"] is True
        assert "timed out" in result["reason"]

    def test_middle_band_returns_fallback(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "boolean", "probability": 0.50},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert result["fallback"] is True
        assert "meets_acceptance_criteria" in result["reason"]
        assert "uncertain" in result["reason"]

    def test_boundary_values_not_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "boolean", "probability": GATE_HIGH},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert "fallback" not in result
        assert result["meets_acceptance_criteria"] == GATE_HIGH

    def test_custom_gate_thresholds(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "boolean", "probability": 0.50},
            },
        )

        result_default = should_auto_merge("issue", "diff")
        assert result_default["fallback"] is True

        result_custom = should_auto_merge("issue", "diff", gate_low=0.10, gate_high=0.30)
        assert "fallback" not in result_custom
        assert result_custom["meets_acceptance_criteria"] == 0.50

    def test_no_exception_propagates(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "connection refused")

        result = should_auto_merge("issue", "diff")

        assert isinstance(result, dict)
        assert result["fallback"] is True
