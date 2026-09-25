"""Jev evaluation client — stdlib HTTP only."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

JEV_ENDPOINT = "https://ai-gateway.vercel.sh/v4/ai/evaluation-model"
DEFAULT_API_KEY_ENV = "JEV_API_KEY"


class JevError(Exception):
    """Raised when a Jev evaluation request fails."""


@dataclass
class JevResult:
    answers: dict
    latency_seconds: float
    raw: dict


def evaluate(
    state: str,
    questions: dict,
    *,
    api_key: str | None = None,
    timeout: float = 10.0,
) -> JevResult:
    if api_key is None:
        api_key = os.environ.get(DEFAULT_API_KEY_ENV)
    if not api_key:
        raise JevError(f"No API key: pass api_key or set ${DEFAULT_API_KEY_ENV}")

    body = json.dumps({"state": state, "questions": questions}).encode()
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
        raise JevError(f"HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise JevError(f"Request failed: {exc.reason}") from exc

    latency = time.monotonic() - t0

    try:
        data = json.loads(raw_bytes)
    except (json.JSONDecodeError, ValueError) as exc:
        raise JevError("Malformed response body") from exc

    if "answers" not in data:
        raise JevError("Response missing 'answers' key")

    return JevResult(answers=data["answers"], latency_seconds=latency, raw=data)


def probability(result: JevResult, question: str) -> float | None:
    entry = result.answers.get(question)
    if entry is None:
        return None
    return entry.get("probability")
