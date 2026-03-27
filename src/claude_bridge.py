"""
Anthropic API bridge for document generation (CV + cover letter).

Uses the official anthropic SDK. Job data is sanitised before inclusion
in prompts. Profile context (CV template, candidate summary) is passed
as the system prompt — never via env vars, shell args, or subprocess.
"""
from __future__ import annotations

import os

MAX_JD_LENGTH = 50_000
ALLOWED_COUNTRIES = {"DE", "SA", "AE", "CH", "QA", "NL"}


def validate_input(data: dict) -> dict:
    """Sanitise and validate job data before embedding in prompts."""
    required = ["title", "company", "country"]
    optional_str = ["city"]

    for field in required:
        if field not in data:
            raise ValueError(f"Missing required field: {field}")
        if isinstance(data[field], str):
            data[field] = "".join(
                c for c in data[field] if c.isprintable() or c in "\n\t"
            )

    for field in optional_str:
        if data.get(field) and isinstance(data[field], str):
            data[field] = "".join(
                c for c in data[field] if c.isprintable() or c in "\n\t"
            )
        elif not data.get(field):
            data[field] = ""

    jd = data.get("description", "")
    if len(jd) > MAX_JD_LENGTH:
        data["description"] = jd[:MAX_JD_LENGTH] + "\n[TRUNCATED]"

    company = data["company"]
    safe_company = "".join(c for c in company if c.isalnum() or c in " -.").strip()
    safe_company = safe_company.replace(" ", "_").replace("..", "").lstrip("./")
    if not safe_company:
        raise ValueError(f"Company name sanitises to empty: {company}")
    data["company_safe"] = safe_company[:100]

    if data["country"] not in ALLOWED_COUNTRIES:
        raise ValueError(f"Unknown country code: {data['country']}")

    return data


def call_api(
    system_prompt: str,
    user_prompt: str,
    max_tokens: int = 4096,
) -> dict:
    """
    Call the document generation LLM with a system + user message split.

    Provider is controlled by DOCUMENT_LLM_PROVIDER env var:
      "anthropic" (default, production) — uses ANTHROPIC_API_KEY + ANTHROPIC_MODEL
      "groq"      (testing)             — uses GROQ_API_KEY + GROQ_MODEL

    Returns:
        {"status": "success", "output": <text>}
        {"status": "error",   "error":  <message>}
    """
    provider = os.environ.get("DOCUMENT_LLM_PROVIDER", "anthropic").lower()

    if provider == "groq":
        return _call_groq(system_prompt, user_prompt, max_tokens)
    return _call_anthropic(system_prompt, user_prompt, max_tokens)


def _call_anthropic(system_prompt: str, user_prompt: str, max_tokens: int) -> dict:
    try:
        import anthropic
    except ImportError:
        return {"status": "error", "error": "anthropic SDK not installed"}

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"status": "error", "error": "ANTHROPIC_API_KEY is not set"}

    model = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-6")

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return {"status": "success", "output": message.content[0].text}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


def _call_groq(system_prompt: str, user_prompt: str, max_tokens: int) -> dict:
    try:
        from groq import Groq
    except ImportError:
        return {"status": "error", "error": "groq SDK not installed"}

    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        return {"status": "error", "error": "GROQ_API_KEY is not set"}

    model = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        return {"status": "success", "output": response.choices[0].message.content}
    except Exception as exc:
        return {"status": "error", "error": str(exc)}
