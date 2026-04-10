"""Single, shared LLM client.

Goals:

* Replace the three near-identical ``llm_client.py`` modules currently scattered
  across MSARN, Companies_News and Idee_Scraping.
* Provide a primary -> fallback chain (Claude -> Gemini).
* Always return a structured ``LLMResult`` so callers can log token cost +
  model used + which retry succeeded.
* Keep the surface MINIMAL -- one ``generate(...)`` method, plus a
  ``classify_json(...)`` convenience for the common JSON-schema case.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Literal

from research_commons.config import get_settings

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
        s = get_settings()
        self._primary_model = primary_model or s.llm_primary_model
        self._fallback_model = fallback_model or s.llm_fallback_model
        self._anthropic_key = anthropic_api_key or s.anthropic_api_key
        self._gemini_key = gemini_api_key or s.gemini_api_key

        self._anthropic_client = None
        self._gemini_model = None

    def _get_anthropic(self):
        if self._anthropic_client is None and self._anthropic_key:
            import anthropic
            self._anthropic_client = anthropic.Anthropic(api_key=self._anthropic_key)
        return self._anthropic_client

    def _get_gemini(self):
        if self._gemini_model is None and self._gemini_key:
            import google.generativeai as genai
            genai.configure(api_key=self._gemini_key)
            self._gemini_model = genai.GenerativeModel(self._fallback_model)
        return self._gemini_model

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
        attempts = 0
        last_error: Exception | None = None

        # Try primary (Anthropic)
        if self._anthropic_key:
            for retry in range(max_retries):
                attempts += 1
                try:
                    result = self._call_anthropic(
                        prompt, system=system,
                        response_format=response_format,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
                    result.attempts = attempts
                    return result
                except Exception as e:
                    last_error = e
                    wait = min(2 ** retry, 8)
                    logger.warning(
                        "Anthropic attempt %d failed: %s. Retrying in %ds",
                        attempts, e, wait,
                    )
                    time.sleep(wait)

        # Try fallback (Gemini)
        if self._gemini_key:
            for retry in range(max_retries):
                attempts += 1
                try:
                    result = self._call_gemini(
                        prompt, system=system,
                        response_format=response_format,
                        temperature=temperature,
                        max_output_tokens=max_output_tokens,
                    )
                    result.attempts = attempts
                    return result
                except Exception as e:
                    last_error = e
                    wait = min(2 ** retry, 8)
                    logger.warning(
                        "Gemini attempt %d failed: %s. Retrying in %ds",
                        attempts, e, wait,
                    )
                    time.sleep(wait)

        raise RuntimeError(
            f"All LLM providers exhausted after {attempts} attempts. "
            f"Last error: {last_error}"
        )

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
        result = self.generate(
            prompt, system=system, response_format="json", max_retries=max_retries,
        )
        if json_schema is not None:
            try:
                import jsonschema
                jsonschema.validate(result.content, json_schema)
            except ImportError:
                pass  # jsonschema not installed; skip validation
            except jsonschema.ValidationError as e:
                raise ValueError(f"LLM output failed schema validation: {e.message}") from e
        return result

    # ---------- internals -----------------------------------------------------

    def _call_anthropic(
        self,
        prompt: str,
        *,
        system: str = "",
        response_format: ResponseFormat = "json",
        temperature: float = 0.0,
        max_output_tokens: int = 2048,
    ) -> LLMResult:
        client = self._get_anthropic()
        if client is None:
            raise RuntimeError("Anthropic API key not configured")

        t0 = time.monotonic_ns()
        messages = [{"role": "user", "content": prompt}]
        kwargs: dict[str, Any] = {
            "model": self._primary_model,
            "max_tokens": max_output_tokens,
            "messages": messages,
            "temperature": temperature,
        }
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)
        raw_text = response.content[0].text
        latency_ms = int((time.monotonic_ns() - t0) / 1_000_000)

        content: Any = raw_text
        if response_format == "json":
            content = _extract_json(raw_text)

        return LLMResult(
            content=content,
            raw_text=raw_text,
            model_used=self._primary_model,
            provider="anthropic",
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            attempts=1,
            latency_ms=latency_ms,
        )

    def _call_gemini(
        self,
        prompt: str,
        *,
        system: str = "",
        response_format: ResponseFormat = "json",
        temperature: float = 0.0,
        max_output_tokens: int = 2048,
    ) -> LLMResult:
        model = self._get_gemini()
        if model is None:
            raise RuntimeError("Gemini API key not configured")

        import google.generativeai as genai

        t0 = time.monotonic_ns()
        full_prompt = f"{system}\n\n{prompt}" if system else prompt

        config = genai.types.GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        if response_format == "json":
            config.response_mime_type = "application/json"

        response = model.generate_content(full_prompt, generation_config=config)
        raw_text = response.text
        latency_ms = int((time.monotonic_ns() - t0) / 1_000_000)

        content: Any = raw_text
        if response_format == "json":
            content = _extract_json(raw_text)

        input_tokens = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
        output_tokens = getattr(response.usage_metadata, "candidates_token_count", 0) or 0

        return LLMResult(
            content=content,
            raw_text=raw_text,
            model_used=self._fallback_model,
            provider="gemini",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            attempts=1,
            latency_ms=latency_ms,
        )


def _extract_json(text: str) -> Any:
    """Extract JSON from LLM output, handling markdown code fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (code fence markers)
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    return json.loads(text)
