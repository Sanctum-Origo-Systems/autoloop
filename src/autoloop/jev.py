"""Jev evaluation client — stdlib HTTP only."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

JEV_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_API_KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_MODEL = "typesafe/jev-1.13"

GATE_LOW = 0.35
GATE_HIGH = 0.65


class JevError(Exception):
    """Raised when a Jev evaluation request fails."""


@dataclass
class JevResult:
    answers: dict
    latency_seconds: float
    raw: dict
    cost_usd: float | None = None


def evaluate(
    state: str,
    questions: dict,
    *,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
) -> JevResult:
    if api_key is None:
        api_key = os.environ.get(api_key_env)
    if not api_key:
        raise JevError(f"No API key: pass api_key or set ${api_key_env}")

    body = json.dumps({"model": model, "state": state, "questions": questions}).encode()
    req = urllib.request.Request(
        JEV_ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_bytes = resp.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read()
        try:
            err = json.loads(error_body).get("error", {})
            msg = err.get("message", exc.reason)
        except (json.JSONDecodeError, ValueError, AttributeError):
            msg = exc.reason
        raise JevError(f"HTTP {exc.code}: {msg}") from exc
    except urllib.error.URLError as exc:
        raise JevError(f"Request failed: {exc.reason}") from exc

    try:
        data = json.loads(raw_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        raise JevError("Malformed response body") from exc

    latency = time.monotonic() - t0

    if "answers" not in data:
        raise JevError("Response missing 'answers' key")

    cost_usd = None
    usage = data.get("usage")
    if isinstance(usage, dict):
        cost_usd = usage.get("cost")

    return JevResult(answers=data["answers"], latency_seconds=latency, raw=data, cost_usd=cost_usd)


def probability(result: JevResult, question: str) -> float | None:
    entry = result.answers.get(question)
    if entry is None:
        return None
    return entry.get("noul")


def _in_middle_band(p: float, gate_low: float = GATE_LOW, gate_high: float = GATE_HIGH) -> bool:
    return gate_low < p < gate_high


def _fallback(reason: str) -> dict:
    return {"fallback": True, "reason": reason}


def triage(
    issue_text: str,
    *,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
) -> dict:
    questions = {
        "well_formed": {
            "type": "noul",
            "instructions": "Is this GitHub issue well-formed enough to implement?",
            "criteria": {
                "true": "Has a clear problem statement, expected behavior, and enough context to act on",
                "false": "Vague, missing context, missing expected behavior, or not actionable",
            },
        },
        "needs_decomposition": {
            "type": "noul",
            "instructions": "Does this issue need to be decomposed into smaller sub-issues?",
            "criteria": {
                "true": "Touches multiple files or concerns, estimated at more than 3 story points",
                "false": "Small and focused enough to implement in a single PR",
            },
        },
    }

    try:
        result = evaluate(
            issue_text,
            questions,
            api_key=api_key,
            api_key_env=api_key_env,
            model=model,
            timeout=timeout,
        )
    except JevError as exc:
        return _fallback(str(exc))

    wf_p = probability(result, "well_formed")
    nd_p = probability(result, "needs_decomposition")

    if wf_p is not None and _in_middle_band(wf_p, gate_low, gate_high):
        return _fallback(f"well_formed probability {wf_p} in uncertain band")
    if nd_p is not None and _in_middle_band(nd_p, gate_low, gate_high):
        return _fallback(f"needs_decomposition probability {nd_p} in uncertain band")

    return {
        "well_formed": wf_p,
        "needs_decomposition": nd_p,
    }


def should_auto_merge(
    issue_text: str,
    diff: str,
    *,
    api_key: str | None = None,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    model: str = DEFAULT_MODEL,
    timeout: float = 10.0,
    gate_low: float = GATE_LOW,
    gate_high: float = GATE_HIGH,
) -> dict:
    questions = {
        "meets_acceptance_criteria": {
            "type": "noul",
            "instructions": "Does this PR meet the acceptance criteria from the issue and is it safe to merge?",
            "criteria": {
                "true": "All acceptance criteria addressed, tests pass, no regressions, code is clean",
                "false": "Missing acceptance criteria, test failures, regressions, or code quality issues",
            },
        },
    }

    state = f"Issue:\n{issue_text}\n\nDiff:\n{diff}"

    try:
        result = evaluate(
            state,
            questions,
            api_key=api_key,
            api_key_env=api_key_env,
            model=model,
            timeout=timeout,
        )
    except JevError as exc:
        return _fallback(str(exc))

    mac_p = probability(result, "meets_acceptance_criteria")

    if mac_p is not None and _in_middle_band(mac_p, gate_low, gate_high):
        return _fallback(f"meets_acceptance_criteria probability {mac_p} in uncertain band")

    return {"meets_acceptance_criteria": mac_p}
