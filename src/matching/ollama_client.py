"""
Ollama client for job scoring (local LLM fallback).

Uses the OpenAI-compatible /v1/chat/completions endpoint that Ollama
exposes by default. No API key, no rate limits, no cost.

Shares ScoreResult / build_score_prompt from gemini_client.py
so all three clients are drop-in replacements.
"""
from __future__ import annotations

import httpx

from src.matching.gemini_client import (
    ScoreResult,
    build_score_prompt,
)
from src.matching.response_parser import parse_score_response
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
                "max_tokens": 1024,  # qwen3 emits <think> blocks before the JSON
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    def score_job(self, job: dict, cv_summary: str, user_preferences: str = "") -> ScoreResult:
        prompt = build_score_prompt(job, cv_summary, user_preferences)

        try:
            raw = self._call(prompt)
        except Exception as exc:
            log.error("Ollama scoring failed for job %s: %s", job.get("id", "?"), exc)
            raise

        result = parse_score_response(raw)
        audit(
            "ollama_score",
            job_id=job.get("id", "?"),
            score=result.score,
        )
        return result

    @property
    def daily_remaining(self) -> int:
        return 999999  # no limit
