"""Single, shared LLM client.

Goals:

* Replace the three near-identical ``llm_client.py`` modules currently scattered
  across MSARN, Companies_News and Idee_Scraping.
* Provide a primary → fallback chain (Claude → Gemini).
* Always return a structured ``LLMResult`` so callers can log token cost +
  model used + which retry succeeded.
* Keep the surface MINIMAL — one ``generate(...)`` method, plus a
  ``classify_json(...)`` convenience for the common JSON-schema case.

Privacy ⚠️
The full prompt body is transmitted to Anthropic and (on fallback) Google.
Never feed it data you wouldn't be comfortable seeing in a log on a third-party
server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

ResponseFormat = Literal["json", "text"]


@dataclass
class LLMResult:
    content: Any                # parsed JSON dict or raw str depending on format
    raw_text: str               # exactly what the model returned
    model_used: str             # e.g. "claude-haiku-4-5-20251001"
    provider: Literal["anthropic", "gemini"]
    input_tokens: int
    output_tokens: int
    attempts: int               # how many providers/retries it took
    latency_ms: int


class LLMClient:
    """Thin wrapper around Anthropic + Gemini SDKs.

    Construct once per process. Thread-safe.
    """

    def __init__(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        anthropic_api_key: str | None = None,
        gemini_api_key: str | None = None,
    ) -> None:
        raise NotImplementedError(
            "Stub. Read missing args from research_commons.config.get_settings(), "
            "lazily import anthropic + google.generativeai SDK clients, store on self."
        )

    # ---------- public API ----------------------------------------------------

    def generate(
        self,
        prompt: str,
        *,
        system: str = "",
        response_format: ResponseFormat = "json",
        max_retries: int = 3,
        temperature: float = 0.0,
        max_output_tokens: int = 2048,
    ) -> LLMResult:
        """Send a prompt and return a structured result.

        Tries the primary model first; on empty response, rate-limit, or schema
        violation, falls back to the secondary model. Each provider gets up to
        ``max_retries`` attempts with exponential backoff.
        """
        raise NotImplementedError("Stub. Implement primary → fallback chain.")

    def classify_json(
        self,
        prompt: str,
        *,
        json_schema: dict | None = None,
        system: str = "",
        max_retries: int = 3,
    ) -> LLMResult:
        """Convenience wrapper that forces ``response_format='json'`` and
        validates the output against an optional JSON schema before returning.
        """
        raise NotImplementedError("Stub. Call self.generate(..., response_format='json').")

    # ---------- internals -----------------------------------------------------

    def _call_anthropic(self, *args: Any, **kwargs: Any) -> LLMResult:
        raise NotImplementedError

    def _call_gemini(self, *args: Any, **kwargs: Any) -> LLMResult:
        raise NotImplementedError
