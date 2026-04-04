"""
Ollama client for job scoring (local LLM fallback).

Uses the OpenAI-compatible /v1/chat/completions endpoint that Ollama
exposes by default. No API key, no rate limits, no cost.

Shares ScoreResult / _SCORE_PROMPT_TEMPLATE from gemini_client.py
so all three clients are drop-in replacements.
"""
from __future__ import annotations

import json
import re

import httpx

from src.matching.gemini_client import (
    ScoreResult,
    _SCORE_PROMPT_TEMPLATE,
)
from src.logger import audit, get_logger

log = get_logger(__name__)


class OllamaClient:
    """
    Local Ollama client with the same interface as GeminiClient/GroqClient.
    Exposes score_job(job, cv_summary) -> ScoreResult.
    """

    def __init__(
        self,
        endpoint: str = "http://localhost:11434/v1",
        model: str = "qwen3:8b",
    ):
        self._endpoint = endpoint.rstrip("/")
        self._model = model
        self._http = httpx.Client(timeout=120.0)  # local models can be slow

    def _call(self, prompt: str) -> str:
        resp = self._http.post(
            f"{self._endpoint}/chat/completions",
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.1,
                "max_tokens": 512,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def score_job(self, job: dict, cv_summary: str, user_preferences: str = "") -> ScoreResult:
        prompt = _SCORE_PROMPT_TEMPLATE.format(
            cv_summary=cv_summary,
            title=job.get("title", ""),
            company=job.get("company", ""),
            country=job.get("country", ""),
            description=(job.get("description") or "")[:3000],
            user_preferences=user_preferences,
        )

        try:
            raw = self._call(prompt)
        except Exception as exc:
            log.error("Ollama scoring failed for job %s: %s", job.get("id", "?"), exc)
            raise

        result = _parse_response(raw)
        audit(
            "ollama_score",
            job_id=job.get("id", "?"),
            score=result.score,
        )
        return result

    @property
    def daily_remaining(self) -> int:
        return 999999  # no limit


def _parse_response(raw: str) -> ScoreResult:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        log.warning("Ollama returned non-JSON; attempting regex extraction. Error: %s", exc)
        score_match = re.search(r'"score"\s*:\s*([0-9.]+)', cleaned)
        score = float(score_match.group(1)) if score_match else 0.0
        return ScoreResult(
            score=score,
            reasoning="Parse error — see raw_response",
            must_haves_met=[],
            gaps=[],
            raw_response=raw,
        )

    return ScoreResult(
        score=float(data.get("score", 0.0)),
        reasoning=str(data.get("reasoning", "")),
        must_haves_met=list(data.get("must_haves_met", [])),
        gaps=list(data.get("gaps", [])),
        raw_response=raw,
    )
