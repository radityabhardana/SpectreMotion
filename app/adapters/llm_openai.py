"""LLMPort adapter for any OpenAI-compatible /chat/completions endpoint."""

from __future__ import annotations

import logging

import requests

from app import config
from app.errors import AnalyzerError

log = logging.getLogger("spectre.llm")


class OpenAICompatLLM:
    """Sends chat completions with retry/backoff + response_format fallback."""

    def __init__(self, base_url: str | None = None, model: str | None = None) -> None:
        self.base_url = (base_url or config.LLM_BASE_URL).rstrip("/")
        self.model = model or config.LLM_MODEL

    def chat(self, messages, *, json_mode: bool = True) -> str:
        import time

        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if config.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {config.LLM_API_KEY}"
        payload = {"model": self.model, "messages": messages, "temperature": 0.4}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        last_err: Exception | None = None
        for attempt in range(1, config.LLM_MAX_RETRIES + 1):
            try:
                resp = requests.post(url, headers=headers, json=payload,
                                     timeout=config.LLM_TIMEOUT_S)
                if resp.status_code == 400 and "response_format" in resp.text:
                    payload.pop("response_format", None)
                    resp = requests.post(url, headers=headers, json=payload,
                                         timeout=config.LLM_TIMEOUT_S)
                if resp.status_code in (401, 403, 404):
                    raise AnalyzerError(f"LLM auth/endpoint error {resp.status_code}")
                if resp.status_code in (429, 500, 502, 503, 504):
                    raise AnalyzerError(f"LLM HTTP {resp.status_code}")
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except AnalyzerError as exc:
                if "auth" in str(exc):
                    raise
                last_err = exc
            except (requests.RequestException, KeyError) as exc:
                last_err = exc
            wait = config.LLM_RETRY_BACKOFF_S * (2 ** (attempt - 1))
            log.warning("LLM attempt %d/%d failed: %s", attempt, config.LLM_MAX_RETRIES, last_err)
            if attempt < config.LLM_MAX_RETRIES:
                time.sleep(wait)
        raise AnalyzerError(f"LLM gagal setelah {config.LLM_MAX_RETRIES} percobaan: {last_err}")
