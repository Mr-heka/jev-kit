"""Small fixed-route client for typed Jev decisions through OpenRouter.

Standard library only. One request per call, no retries, no redirects, and the
key never leaves this process except in the Authorization header.

Key lookup, first match wins:
  1. OPENROUTER_API_KEY in the environment
  2. JEV_KEY_COMMAND: a command that prints the key, for example
     `op read op://Private/OpenRouter/key` or
     `security find-generic-password -w -s openrouter`. It runs without a shell.
"""

from __future__ import annotations

import json
import math
import os
import shlex
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Any

MODEL = os.environ.get("JEV_MODEL", "typesafe/jev-1.13")
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"
KEY_URL = "https://openrouter.ai/api/v1/key"
MAX_INPUT_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_QUESTIONS = 32
TIMEOUT_SECONDS = 30

_key_lock = threading.Lock()
_key_cache: str | None = None


class JevError(Exception):
    """An error whose fields are safe to print (never contains the key)."""

    def __init__(self, code: str, detail: str | None = None, status: int | None = None):
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.status = status

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"error": self.code}
        if self.detail is not None:
            out["detail"] = self.detail
        if self.status is not None:
            out["status"] = self.status
        return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _probability(value: Any) -> bool:
    return _number(value) and 0 <= value <= 1


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


# ------------------------------------------------------------------ request

def validate_request(body: Any) -> dict[str, Any]:
    """Check a {state, questions} body locally before any key lookup."""
    if not isinstance(body, dict) or set(body) != {"state", "questions"}:
        raise JevError("invalid_input", "only_state_and_questions_allowed")
    try:
        raw = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
        raise JevError("invalid_input", "finite_utf8_json_required") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise JevError("invalid_input", "request_too_large")
    if not isinstance(body["state"], (str, list, dict)):
        raise JevError("invalid_input", "state_must_be_string_list_or_object")
    questions = body["questions"]
    if not isinstance(questions, dict) or not 1 <= len(questions) <= MAX_QUESTIONS:
        raise JevError("invalid_input", "questions_count_out_of_range")
    clean: dict[str, dict[str, Any]] = {}
    for name, q in questions.items():
        if not _text(name) or not isinstance(q, dict):
            raise JevError("invalid_input", "invalid_question")
        if not set(q) <= {"type", "instructions", "criteria"}:
            raise JevError("invalid_input", f"unsupported_question_field:{name}")
        kind = q.get("type")
        if kind not in {"choice", "noul", "score"}:
            raise JevError("invalid_input", f"type_must_be_choice_noul_or_score:{name}")
        if not _text(q.get("instructions")):
            raise JevError("invalid_input", f"instructions_required:{name}")
        out: dict[str, Any] = {"type": kind, "instructions": q["instructions"]}
        criteria = q.get("criteria")
        if kind == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2 or not all(
                _text(k) and _text(v) for k, v in criteria.items()
            ):
                raise JevError("invalid_input", f"choice_needs_two_or_more_described_options:{name}")
            out["criteria"] = criteria
        elif kind == "score":
            if not isinstance(criteria, list) or len(criteria) < 2 or not all(_text(v) for v in criteria):
                raise JevError("invalid_input", f"score_needs_ordered_list_of_levels:{name}")
            out["criteria"] = criteria
        elif "criteria" in q:
            if not isinstance(criteria, dict) or set(criteria) != {"true", "false"} or not all(
                _text(v) for v in criteria.values()
            ):
                raise JevError("invalid_input", f"noul_criteria_must_define_true_and_false:{name}")
            out["criteria"] = criteria
        clean[name] = out
    return {"state": body["state"], "questions": clean}


# ------------------------------------------------------------------ key

def _lookup_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return _checked(key)
    command = os.environ.get("JEV_KEY_COMMAND", "").strip()
    if not command:
        raise JevError("key_missing", "set OPENROUTER_API_KEY or JEV_KEY_COMMAND")
    try:
        result = subprocess.run(shlex.split(command), capture_output=True, text=True, timeout=40, check=False)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise JevError("key_command_failed") from None
    key = result.stdout.strip() if result.returncode == 0 else ""
    if not key or "\n" in key:
        raise JevError("key_command_failed")
    return _checked(key)


def _checked(key: str) -> str:
    """A key is printable ASCII with no spaces; anything else could end up in an error message."""
    if not all(33 <= ord(ch) <= 126 for ch in key):
        raise JevError("key_invalid", "the key contains spaces, line breaks or other unusual characters")
    return key


def _key() -> str:
    global _key_cache
    with _key_lock:
        if _key_cache is None:
            _key_cache = _lookup_key()
        return _key_cache


def key_available() -> bool:
    try:
        _key()
        return True
    except JevError:
        return False


# ------------------------------------------------------------------ http

def _request(url: str, payload: dict[str, Any] | None) -> Any:
    headers = {"Authorization": f"Bearer {_key()}", "Accept": "application/json"}
    data = None
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
    try:
        with urllib.request.build_opener(_NoRedirect()).open(req, timeout=TIMEOUT_SECONDS) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        exc.close()
        raise JevError("http_error", status=exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise JevError("network_error") from None
    except ValueError:  # e.g. a header Python refuses; its message could contain the header value
        raise JevError("request_failed") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise JevError("response_too_large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise JevError("invalid_response_json") from None


# ------------------------------------------------------------------ response

def _check_answers(raw: Any, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(questions):
        raise JevError("invalid_response", "answer_set_mismatch")
    clean: dict[str, Any] = {}
    for name, q in questions.items():
        a = raw[name]
        kind = q["type"]
        if not isinstance(a, dict) or a.get("type") != kind:
            raise JevError("invalid_response", f"answer_type_mismatch:{name}")
        if kind == "choice":
            options = set(q["criteria"])
            probs = a.get("probabilities")
            if a.get("choice") not in options or not isinstance(probs, dict) or set(probs) != options:
                raise JevError("invalid_response", f"unknown_choice:{name}")
            if not all(_probability(v) for v in probs.values()) or not math.isclose(
                sum(probs.values()), 1, abs_tol=0.001
            ) or not _probability(a.get("confidence")):
                raise JevError("invalid_response", f"invalid_probability:{name}")
            clean[name] = {"type": "choice", "choice": a["choice"], "probabilities": probs, "confidence": a["confidence"]}
        elif kind == "noul":
            if not _probability(a.get("noul")):
                raise JevError("invalid_response", f"invalid_noul:{name}")
            clean[name] = {"type": "noul", "noul": a["noul"]}
        else:
            top = len(q["criteria"]) - 1
            if not _number(a.get("score")) or not 0 <= a["score"] <= top:
                raise JevError("invalid_response", f"invalid_score:{name}")
            out: dict[str, Any] = {"type": "score", "score": a["score"]}
            probs = a.get("probabilities")
            if probs is not None:
                if not isinstance(probs, dict) or not set(probs) <= {str(i) for i in range(top + 1)} or not all(
                    _probability(v) for v in probs.values()
                ):
                    raise JevError("invalid_response", f"invalid_probability:{name}")
                out["probabilities"] = probs
            if a.get("confidence") is not None:
                if not _probability(a["confidence"]):
                    raise JevError("invalid_response", f"invalid_probability:{name}")
                out["confidence"] = a["confidence"]
            clean[name] = out
    return clean


def _usage(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {}
    out = {k: raw[k] for k in ("input_tokens", "output_tokens") if isinstance(raw.get(k), int)}
    if _number(raw.get("cost")) and raw["cost"] >= 0:
        out["cost"] = raw["cost"]
    return out


# ------------------------------------------------------------------ public

def decide(state: Any, questions: dict[str, Any]) -> dict[str, Any]:
    """One typed decision. Returns model, id, answers, usage and elapsed seconds."""
    body = validate_request({"state": state, "questions": questions})
    started = time.monotonic()
    raw = _request(DECISIONS_URL, {"model": MODEL, **body})
    elapsed = round(time.monotonic() - started, 3)
    if not isinstance(raw, dict) or not _text(raw.get("model")):
        raise JevError("invalid_response", "missing_model")
    return {
        "model": raw["model"],
        "id": raw.get("id"),
        "answers": _check_answers(raw.get("answers"), body["questions"]),
        "usage": _usage(raw.get("usage")),
        "elapsed": elapsed,
    }


def check_key() -> dict[str, Any]:
    """Read-only key check: no inference, no spend."""
    raw = _request(KEY_URL, None)
    data = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
    if not isinstance(data, dict):
        raise JevError("invalid_response", "key_response_object_required")
    out = {"ok": True, "is_free_tier": data.get("is_free_tier")}
    if _number(data.get("limit_remaining")):
        out["limit_remaining"] = data["limit_remaining"]
    return out
