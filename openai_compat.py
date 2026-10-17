"""OpenAI-compatible chat-completions backends with tool calling: Hugging Face and Google Gemini.

    LARRE_LLM=hf      open models via Hugging Face Inference Providers; needs HF_TOKEN
                      (fine-grained, "Make calls to Inference Providers").
    LARRE_LLM=gemini  Google Gemini via its OpenAI-compatible endpoint; needs GEMINI_API_KEY
                      (free from aistudio.google.com).

Uses only the standard library, so it adds no dependencies.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from config import LLMConfig

RETRYABLE = {408, 429, 500, 502, 503, 504}
# Hugging Face's edge firewall answers Python's default "Python-urllib" User-Agent with an HTML 403.
USER_AGENT = "larr-e/0.1 (+https://github.com/mrgarbett/larr-e)"


class LLMError(Exception):
    """An LLM backend failure with a message fit to show the operator."""


@dataclass(frozen=True)
class Preset:
    name: str
    base_url: str
    model: str
    token_envs: tuple[str, ...]
    token_help: str


def preset_for(llm: LLMConfig) -> Preset:
    if llm.provider == "hf":
        return Preset("Hugging Face", llm.hf_base_url, llm.hf_model, ("HF_TOKEN",),
                      "Set HF_TOKEN to a Hugging Face token with the 'Make calls to Inference Providers' permission.")
    if llm.provider == "gemini":
        return Preset("Gemini", llm.gemini_base_url, llm.gemini_model, ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
                      "Set GEMINI_API_KEY to a key from aistudio.google.com (API keys).")
    raise ValueError(f"No OpenAI-compatible preset for provider {llm.provider!r}")


# JSON Schema keywords Gemini's function declarations reliably accept. Anything else is dropped;
# the agent enforces argument names and the tools enforce values, so nothing safety-relevant is lost.
_GEMINI_KEYS = {"type", "description", "enum", "properties", "required", "items", "minimum", "maximum",
                "format", "nullable", "minItems", "maxItems"}


def _gemini_schema(schema: Any) -> Any:
    if isinstance(schema, list):
        return [_gemini_schema(s) for s in schema]
    if not isinstance(schema, dict):
        return schema
    out = {k: v for k, v in schema.items() if k in _GEMINI_KEYS}
    if "exclusiveMinimum" in schema and "minimum" not in out:
        out["minimum"] = schema["exclusiveMinimum"]
    if "properties" in out:
        out["properties"] = {name: _gemini_schema(s) for name, s in out["properties"].items()}
    if "items" in out:
        out["items"] = _gemini_schema(out["items"])
    return out


def to_openai_tools(specs: list[dict[str, Any]], provider: str = "hf") -> list[dict[str, Any]]:
    """Convert our Anthropic-style tool specs ({name, description, input_schema})."""
    clean = _gemini_schema if provider == "gemini" else (lambda s: s)
    return [
        {"type": "function", "function": {"name": s["name"], "description": s["description"],
                                          "parameters": clean(s["input_schema"])}}
        for s in specs
    ]


class ChatBackend:
    def __init__(self, llm: LLMConfig):
        self.llm = llm
        self.preset = preset_for(llm)
        self.token = next((os.environ[e] for e in self.preset.token_envs if os.environ.get(e)), None)
        self.url = self.preset.base_url.rstrip("/") + "/chat/completions"

    def create(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> dict[str, Any]:
        p = self.preset
        if not self.token:  # checked here, not in __init__, so the CLI's error handler reports it
            raise LLMError(p.token_help)
        body: dict[str, Any] = {
            "model": p.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "max_tokens": self.llm.hf_max_tokens,
        }
        if self.llm.reasoning_effort:
            body["reasoning_effort"] = self.llm.reasoning_effort
        data = json.dumps(body).encode()
        headers = {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json", "User-Agent": USER_AGENT}
        if self.llm.provider == "hf" and self.llm.hf_bill_to:  # spend an organization's credits
            headers["X-HF-Bill-To"] = self.llm.hf_bill_to

        for attempt in range(3):
            request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    payload = json.loads(response.read())
                if not payload.get("choices"):
                    raise LLMError(f"{p.name} returned no choices: {str(payload)[:200]}")
                return payload
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:400].decode(errors="replace")
                if exc.code in RETRYABLE and attempt < 2:
                    time.sleep(2 ** attempt * (5 if exc.code == 429 else 1))  # free tiers: back off harder
                    continue
                if exc.code in (401, 403) and detail.lstrip().startswith("<"):
                    raise LLMError(f"{p.name}'s web firewall blocked the request (HTTP {exc.code} HTML page), "
                                   "not an API-key problem. Check the network or VPN.") from exc
                if exc.code in (401, 403):
                    raise LLMError(f"{p.name} rejected the API key. {p.token_help}") from exc
                if exc.code == 402:
                    raise LLMError(f"{p.name} says the account is out of credits.") from exc
                if exc.code == 429:
                    raise LLMError(f"{p.name} rate limit or free-tier quota reached for {p.model}; wait a minute "
                                   "or pick another model.") from exc
                raise LLMError(f"{p.name} error {exc.code} for model {p.model}: {detail}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise LLMError(f"Cannot reach {p.name} ({exc}). Check the network.") from exc
        raise LLMError(f"{p.name} request failed after retries.")
