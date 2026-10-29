"""
Parse and validate the Tier-1 scoring LLM's reply (shared by all clients).
An unusable reply raises ScoreParseError rather than producing a score.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ScoreResult:
    score: float
    reasoning: str
    must_haves_met: list[str]
    gaps: list[str]
    raw_response: str
    is_job_posting: bool = True


class ScoreParseError(ValueError):
    """The model reply contained no usable score."""


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_SCORE_FIELD = re.compile(r'"score"\s*:\s*"?(-?[0-9]+(?:\.[0-9]+)?)')
_JOB_FLAG_FIELD = re.compile(r'"is_job_posting"\s*:\s*"?(true|false)', re.IGNORECASE)


def parse_score_response(raw: Optional[str]) -> ScoreResult:
    """Turn a raw model reply into a validated ScoreResult, or raise ScoreParseError."""
    if not raw or not raw.strip():
        raise ScoreParseError("empty model response")

    text = _FENCE.sub("", _THINK_BLOCK.sub("", raw)).strip()

    data = _load_json_object(text)
    if data is not None:
        return ScoreResult(
            score=_valid_score(data.get("score")),
            reasoning=str(data.get("reasoning", "")),
            must_haves_met=_str_list(data.get("must_haves_met")),
            gaps=_str_list(data.get("gaps")),
            raw_response=raw,
            is_job_posting=_not_explicitly_false(data.get("is_job_posting")),
        )

    # Malformed or truncated JSON: accept only an explicit "score" field
    match = _SCORE_FIELD.search(text)
    if not match:
        raise ScoreParseError(f"no score in model response: {raw[:200]!r}")
    flag = _JOB_FLAG_FIELD.search(text)
    return ScoreResult(
        score=_valid_score(match.group(1)),
        reasoning="Partial model response (malformed JSON); score recovered",
        must_haves_met=[],
        gaps=[],
        raw_response=raw,
        is_job_posting=flag is None or flag.group(1).lower() != "false",
    )


def _load_json_object(text: str) -> Optional[dict[str, Any]]:
    """Parse the reply as JSON, falling back to the outermost {...} span."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _valid_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        raise ScoreParseError(f"score is not a number: {value!r}") from None
    if math.isnan(score) or not 0.0 <= score <= 10.0:
        raise ScoreParseError(f"score out of range 0-10: {score}")
    return score


def _not_explicitly_false(value: Any) -> bool:
    """Default to True: only an explicit false/"false" marks a non-job."""
    return not (value is False or (isinstance(value, str) and value.strip().lower() == "false"))


def _str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    return [str(value)] if value else []
