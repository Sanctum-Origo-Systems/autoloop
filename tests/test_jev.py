from __future__ import annotations

import io
import json
import urllib.error

import pytest

from autoloop.jev import (
    BACKOFF_SCHEDULE,
    DEFAULT_API_KEY_ENV,
    DEFAULT_MODEL,
    GATE_HIGH,
    GATE_LOW,
    JEV_ENDPOINT,
    JevError,
    JevResult,
    _extract_cost,
    MAX_RETRIES,
    evaluate,
    probability,
    should_auto_merge,
    triage,
)

SAMPLE_RESPONSE = {
    "answers": {
        "well_formed": {
            "type": "noul",
            "noul": 0.5,
        }
    },
    "usage": {"cost": 0.000014154},
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


_MOCK_RAW_WITH_COST = {
    "providerMetadata": {"gateway": {"marketCost": "0.000014"}},
}


def _mock_evaluate(monkeypatch, answers):
    """Patch evaluate() to return a JevResult with the given answers."""

    def fake_evaluate(
        state, questions, *, api_key=None, api_key_env=None, model=None, timeout=10.0
    ):
        raw = {"answers": answers, **_MOCK_RAW_WITH_COST}
        return JevResult(answers=answers, latency_seconds=0.05, raw=raw)

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

        result = evaluate("some state", {"well_formed": {"type": "noul"}})

        assert isinstance(result, JevResult)
        assert result.answers == SAMPLE_RESPONSE["answers"]
        assert result.raw == SAMPLE_RESPONSE
        assert result.latency_seconds > 0
        assert result.cost_usd == 0.000014154

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

        questions = {"well_formed": {"type": "noul"}}
        evaluate("my state", questions)

        req = calls[0][0]
        assert req.full_url == JEV_ENDPOINT
        assert req.get_header("Content-type") == "application/json"
        assert req.get_header("Authorization") == "Bearer test-key"
        assert req.get_method() == "POST"
        for header in (
            "Ai-model-id",
            "Ai-gateway-auth-method",
            "Ai-gateway-protocol-version",
            "Ai-evaluation-model-specification-version",
        ):
            assert req.get_header(header) is None
        body = json.loads(req.data)
        assert body == {
            "model": DEFAULT_MODEL,
            "state": "my state",
            "questions": questions,
        }

    def test_timeout_passed_to_urlopen(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        calls = _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        evaluate("state", {}, timeout=5.0)

        assert calls[0][1] == pytest.approx(5.0, abs=0.1)

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

    def test_custom_model_in_body(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        calls = _mock_urlopen(monkeypatch, json.dumps(SAMPLE_RESPONSE).encode())

        evaluate("state", {"q": {}}, model="custom/model-2.0")

        body = json.loads(calls[0][0].data)
        assert body["model"] == "custom/model-2.0"

    def test_cost_usd_from_usage(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        resp = {"answers": {"q": {"noul": 0.5}}, "usage": {"cost": 0.0042}}
        _mock_urlopen(monkeypatch, json.dumps(resp).encode())

        result = evaluate("state", {"q": {}})

        assert result.cost_usd == 0.0042

    def test_cost_usd_none_when_no_usage(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        resp = {"answers": {"q": {"noul": 0.5}}}
        _mock_urlopen(monkeypatch, json.dumps(resp).encode())

        result = evaluate("state", {"q": {}})

        assert result.cost_usd is None


class TestEvaluateErrors:
    def test_http_error_with_openrouter_body(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        error_body = json.dumps({"error": {"code": 403, "message": "Invalid API key"}}).encode()

        def fake_urlopen(req, *, timeout=None):
            raise urllib.error.HTTPError(JEV_ENDPOINT, 403, "Forbidden", {}, io.BytesIO(error_body))

        monkeypatch.setattr("autoloop.jev.urllib.request.urlopen", fake_urlopen)

        with pytest.raises(JevError, match="HTTP 403: Invalid API key"):
            evaluate("state", {})

    def test_http_error_without_json_body(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        monkeypatch.setattr("autoloop.jev.time.sleep", lambda s: None)

        def fake_urlopen(req, *, timeout=None):
            raise urllib.error.HTTPError(JEV_ENDPOINT, 500, "Server Error", {}, io.BytesIO(b""))

        monkeypatch.setattr("autoloop.jev.urllib.request.urlopen", fake_urlopen)

        with pytest.raises(JevError, match="HTTP 500"):
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


def _mock_urlopen_sequence(monkeypatch, responses):
    """Patch urlopen to return a sequence of responses.

    Each response is either:
    - bytes: successful response body
    - (code, reason, headers): HTTPError to raise
    """
    call_count = [0]

    class FakeResponse:
        def __init__(self, data):
            self._data = data

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def fake_urlopen(req, *, timeout=None):
        idx = call_count[0]
        call_count[0] += 1
        resp = responses[idx]
        if isinstance(resp, bytes):
            return FakeResponse(resp)
        code, reason, headers = resp
        raise urllib.error.HTTPError(JEV_ENDPOINT, code, reason, headers, io.BytesIO(b""))

    monkeypatch.setattr("autoloop.jev.urllib.request.urlopen", fake_urlopen)
    return call_count


class TestEvaluateRetry:
    def test_429_then_success(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        sleeps = []
        monkeypatch.setattr("autoloop.jev.time.sleep", lambda s: sleeps.append(s))
        success_body = json.dumps(SAMPLE_RESPONSE).encode()
        call_count = _mock_urlopen_sequence(
            monkeypatch,
            [(429, "Too Many Requests", {}), success_body],
        )

        result = evaluate("state", {"q": {}})

        assert call_count[0] == 2
        assert result.answers == SAMPLE_RESPONSE["answers"]
        assert sleeps == [BACKOFF_SCHEDULE[0]]

    def test_500_then_success(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        sleeps = []
        monkeypatch.setattr("autoloop.jev.time.sleep", lambda s: sleeps.append(s))
        success_body = json.dumps(SAMPLE_RESPONSE).encode()
        call_count = _mock_urlopen_sequence(
            monkeypatch,
            [(500, "Internal Server Error", {}), success_body],
        )

        result = evaluate("state", {"q": {}})

        assert call_count[0] == 2
        assert result.answers == SAMPLE_RESPONSE["answers"]
        assert sleeps == [BACKOFF_SCHEDULE[0]]

    def test_429_exhausted_raises(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        sleeps = []
        monkeypatch.setattr("autoloop.jev.time.sleep", lambda s: sleeps.append(s))
        errors = [(429, "Too Many Requests", {})] * (1 + MAX_RETRIES)
        call_count = _mock_urlopen_sequence(monkeypatch, errors)

        with pytest.raises(JevError, match="HTTP 429"):
            evaluate("state", {"q": {}}, timeout=30.0)

        assert call_count[0] == 1 + MAX_RETRIES
        assert sleeps == list(BACKOFF_SCHEDULE)

    def test_non_retryable_4xx_raises_immediately(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        sleeps = []
        monkeypatch.setattr("autoloop.jev.time.sleep", lambda s: sleeps.append(s))
        call_count = _mock_urlopen_sequence(
            monkeypatch,
            [(403, "Forbidden", {})],
        )

        with pytest.raises(JevError, match="HTTP 403"):
            evaluate("state", {"q": {}})

        assert call_count[0] == 1
        assert sleeps == []

    def test_respects_retry_after_header(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        sleeps = []
        monkeypatch.setattr("autoloop.jev.time.sleep", lambda s: sleeps.append(s))
        success_body = json.dumps(SAMPLE_RESPONSE).encode()
        _mock_urlopen_sequence(
            monkeypatch,
            [(429, "Too Many Requests", {"Retry-After": "5"}), success_body],
        )

        result = evaluate("state", {"q": {}}, timeout=30.0)

        assert result.answers == SAMPLE_RESPONSE["answers"]
        assert sleeps == [5.0]

    def test_timeout_bounds_retries(self, monkeypatch):
        monkeypatch.setenv(DEFAULT_API_KEY_ENV, "test-key")
        clock = [1000.0]

        def fake_monotonic():
            return clock[0]

        def fake_sleep(s):
            clock[0] += s

        monkeypatch.setattr("autoloop.jev.time.monotonic", fake_monotonic)
        monkeypatch.setattr("autoloop.jev.time.sleep", fake_sleep)
        errors = [(429, "Too Many Requests", {})] * (1 + MAX_RETRIES)
        call_count = _mock_urlopen_sequence(monkeypatch, errors)

        with pytest.raises(JevError, match="HTTP 429"):
            evaluate("state", {"q": {}}, timeout=2.5)

        assert call_count[0] == 2


class TestProbability:
    def test_returns_noul_for_valid_key(self):
        result = JevResult(
            answers={"well_formed": {"type": "noul", "noul": 0.5}},
            latency_seconds=0.1,
            raw={},
        )
        assert probability(result, "well_formed") == 0.5

    def test_returns_none_for_missing_key(self):
        result = JevResult(
            answers={"well_formed": {"type": "noul", "noul": 0.5}},
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
                    "well_formed": {"type": "noul", "noul": 0.91},
                    "needs_decomposition": {"type": "noul", "noul": 0.08},
                },
                latency_seconds=0.05,
                raw={},
            )

        monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)

        triage("some issue text")

        assert "well_formed" in captured["questions"]
        assert captured["questions"]["well_formed"]["type"] == "noul"
        assert "instructions" in captured["questions"]["well_formed"]
        assert "criteria" in captured["questions"]["well_formed"]
        assert "needs_decomposition" in captured["questions"]
        assert captured["questions"]["needs_decomposition"]["type"] == "noul"
        assert "instructions" in captured["questions"]["needs_decomposition"]

    def test_passes_issue_text_as_state(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["state"] = state
            return JevResult(
                answers={
                    "well_formed": {"type": "noul", "noul": 0.91},
                    "needs_decomposition": {"type": "noul", "noul": 0.08},
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
                "well_formed": {"type": "noul", "noul": 0.91},
                "needs_decomposition": {"type": "noul", "noul": 0.08},
            },
        )

        result = triage("some issue text")

        assert result["well_formed"] == 0.91
        assert result["needs_decomposition"] == 0.08
        assert result["latency"] == 0.05
        assert result["cost"] == 0.000014
        assert "fallback" not in result

    def test_high_confidence_reject(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.12},
                "needs_decomposition": {"type": "noul", "noul": 0.05},
            },
        )

        result = triage("vague issue")

        assert result["well_formed"] == 0.12


class TestTriageFallback:
    def test_jev_error_returns_fallback(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "HTTP 500: Internal Server Error")

        result = triage("some issue")

        assert result["fallback"] is True
        assert "HTTP 500" in result["reason"]
        assert result["fallback_cause"] == "outage"

    def test_well_formed_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.50},
                "needs_decomposition": {"type": "noul", "noul": 0.08},
            },
        )

        result = triage("ambiguous issue")

        assert result["fallback"] is True
        assert "well_formed" in result["reason"]
        assert "uncertain" in result["reason"]
        assert result["fallback_cause"] == "uncertain"
        assert "latency" in result
        assert "cost" in result

    def test_needs_decomposition_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.90},
                "needs_decomposition": {"type": "noul", "noul": 0.50},
            },
        )

        result = triage("issue text")

        assert result["fallback"] is True
        assert "needs_decomposition" in result["reason"]
        assert result["fallback_cause"] == "uncertain"

    def test_boundary_values_not_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": GATE_LOW},
                "needs_decomposition": {"type": "noul", "noul": GATE_HIGH},
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
                "well_formed": {"type": "noul", "noul": 0.45},
                "needs_decomposition": {"type": "noul", "noul": 0.10},
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
        assert result["fallback_cause"] == "outage"


# --- should_auto_merge() tests ---


class TestShouldAutoMergeQuestionSchema:
    def test_builds_correct_questions(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["questions"] = questions
            captured["state"] = state
            return JevResult(
                answers={
                    "meets_acceptance_criteria": {"type": "noul", "noul": 0.95},
                },
                latency_seconds=0.05,
                raw={},
            )

        monkeypatch.setattr("autoloop.jev.evaluate", fake_evaluate)

        should_auto_merge("issue body", "diff content")

        assert "meets_acceptance_criteria" in captured["questions"]
        assert captured["questions"]["meets_acceptance_criteria"]["type"] == "noul"
        assert "instructions" in captured["questions"]["meets_acceptance_criteria"]
        assert "criteria" in captured["questions"]["meets_acceptance_criteria"]

    def test_state_includes_issue_and_diff(self, monkeypatch):
        captured = {}

        def fake_evaluate(state, questions, **kwargs):
            captured["state"] = state
            return JevResult(
                answers={
                    "meets_acceptance_criteria": {"type": "noul", "noul": 0.95},
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
                "meets_acceptance_criteria": {"type": "noul", "noul": 0.95},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert result["meets_acceptance_criteria"] == 0.95
        assert result["latency"] == 0.05
        assert result["cost"] == 0.000014
        assert "fallback" not in result

    def test_ignores_score_field_on_noul_question(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {
                    "type": "noul",
                    "noul": 0.88,
                    "score": 0.92,
                },
            },
        )

        result = should_auto_merge("issue", "diff")

        assert result["meets_acceptance_criteria"] == 0.88
        assert result["latency"] == 0.05
        assert result["cost"] == 0.000014
        assert result["readiness_score"] == 0.92


class TestShouldAutoMergeFallback:
    def test_jev_error_returns_fallback(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "Request failed: timed out")

        result = should_auto_merge("issue", "diff")

        assert result["fallback"] is True
        assert "timed out" in result["reason"]
        assert result["fallback_cause"] == "outage"

    def test_middle_band_returns_fallback(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "noul", "noul": 0.50},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert result["fallback"] is True
        assert "meets_acceptance_criteria" in result["reason"]
        assert "uncertain" in result["reason"]
        assert result["fallback_cause"] == "uncertain"
        assert "latency" in result
        assert "cost" in result

    def test_boundary_values_not_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "noul", "noul": GATE_HIGH},
            },
        )

        result = should_auto_merge("issue", "diff")

        assert "fallback" not in result
        assert result["meets_acceptance_criteria"] == GATE_HIGH

    def test_custom_gate_thresholds(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "noul", "noul": 0.50},
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
        assert result["fallback_cause"] == "outage"


# --- _extract_cost tests ---


class TestExtractCost:
    def test_extracts_market_cost(self):
        raw = {"providerMetadata": {"gateway": {"marketCost": "0.000014154"}}}
        assert _extract_cost(raw) == 0.000014154

    def test_missing_provider_metadata(self):
        assert _extract_cost({}) == 0.0

    def test_missing_gateway(self):
        assert _extract_cost({"providerMetadata": {}}) == 0.0

    def test_missing_market_cost(self):
        assert _extract_cost({"providerMetadata": {"gateway": {}}}) == 0.0

    def test_non_numeric_cost(self):
        raw = {"providerMetadata": {"gateway": {"marketCost": "not-a-number"}}}
        assert _extract_cost(raw) == 0.0

    def test_integer_cost(self):
        raw = {"providerMetadata": {"gateway": {"marketCost": "0"}}}
        assert _extract_cost(raw) == 0.0


# --- Shadow mode tests ---


class TestTriageShadowMode:
    def test_returns_raw_probabilities_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.50},
                "needs_decomposition": {"type": "noul", "noul": 0.08},
            },
        )

        result = triage("ambiguous issue", mode="shadow")

        assert result["well_formed"] == 0.50
        assert result["needs_decomposition"] == 0.08
        assert result["would_fallback"] is True
        assert "well_formed" in result["fallback_reason"]
        assert result["fallback_cause"] == "uncertain"
        assert "fallback" not in result

    def test_both_probabilities_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.50},
                "needs_decomposition": {"type": "noul", "noul": 0.55},
            },
        )

        result = triage("issue", mode="shadow")

        assert result["well_formed"] == 0.50
        assert result["needs_decomposition"] == 0.55
        assert result["would_fallback"] is True
        assert "well_formed" in result["fallback_reason"]
        assert "needs_decomposition" in result["fallback_reason"]

    def test_high_confidence_no_fallback_flag(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.91},
                "needs_decomposition": {"type": "noul", "noul": 0.08},
            },
        )

        result = triage("clear issue", mode="shadow")

        assert result["well_formed"] == 0.91
        assert result["needs_decomposition"] == 0.08
        assert result["would_fallback"] is False
        assert "fallback_reason" not in result
        assert "fallback_cause" not in result

    def test_includes_latency_and_cost(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "well_formed": {"type": "noul", "noul": 0.50},
                "needs_decomposition": {"type": "noul", "noul": 0.50},
            },
        )

        result = triage("issue", mode="shadow")

        assert result["latency"] == 0.05
        assert result["cost"] == 0.000014

    def test_error_still_returns_fallback(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "HTTP 429: Too Many Requests")

        result = triage("issue", mode="shadow")

        assert result["fallback"] is True
        assert result["fallback_cause"] == "outage"
        assert "429" in result["reason"]


class TestShouldAutoMergeShadowMode:
    def test_returns_raw_probability_in_middle_band(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "noul", "noul": 0.50},
            },
        )

        result = should_auto_merge("issue", "diff", mode="shadow")

        assert result["meets_acceptance_criteria"] == 0.50
        assert result["would_fallback"] is True
        assert "meets_acceptance_criteria" in result["fallback_reason"]
        assert result["fallback_cause"] == "uncertain"
        assert "fallback" not in result

    def test_high_confidence_no_fallback_flag(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "noul", "noul": 0.95},
            },
        )

        result = should_auto_merge("issue", "diff", mode="shadow")

        assert result["meets_acceptance_criteria"] == 0.95
        assert result["would_fallback"] is False
        assert "fallback_reason" not in result

    def test_includes_latency_and_cost(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {"type": "noul", "noul": 0.50},
            },
        )

        result = should_auto_merge("issue", "diff", mode="shadow")

        assert result["latency"] == 0.05
        assert result["cost"] == 0.000014

    def test_includes_readiness_score(self, monkeypatch):
        _mock_evaluate(
            monkeypatch,
            {
                "meets_acceptance_criteria": {
                    "type": "noul",
                    "noul": 0.50,
                    "score": 0.72,
                },
            },
        )

        result = should_auto_merge("issue", "diff", mode="shadow")

        assert result["meets_acceptance_criteria"] == 0.50
        assert result["readiness_score"] == 0.72
        assert result["would_fallback"] is True

    def test_error_still_returns_fallback(self, monkeypatch):
        _mock_evaluate_raises(monkeypatch, "HTTP 503: Service Unavailable")

        result = should_auto_merge("issue", "diff", mode="shadow")

        assert result["fallback"] is True
        assert result["fallback_cause"] == "outage"
