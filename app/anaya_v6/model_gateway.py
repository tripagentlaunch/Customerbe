from __future__ import annotations
from typing import Optional
"""Model gateway for Anaya V6 — the single place every anaya_v6 module calls
an LLM through, so the underlying provider can be swapped without touching
orchestrator/planner/tool code (build brief: "DO NOT hard-code Anaya to
Claude permanently").

ClaudeProvider's client construction mirrors claude_client.py's own
(same env var, same lazy-init-with-clear-error pattern) — v1-v5 and
claude_client.py themselves are left completely untouched; this is a new,
independent client instance, not a shared one.
"""


import logging
import os
from dataclasses import dataclass
from typing import Optional, Any, Protocol

import anthropic

_log = logging.getLogger("anaya_v6.model_gateway")

DEFAULT_MODEL = "claude-haiku-4-5-20251001"


@dataclass
class ModelResponse:
    tool_name: Optional[str]
    tool_input: dict
    text: str
    stop_reason: Optional[str]
    raw: Any = None


class ModelProvider(Protocol):
    async def call_tool(
        self, *, system: str, messages: list[dict], tool: dict, max_tokens: int,
    ) -> ModelResponse:
        """Force a single named tool call and return its structured input."""
        ...


class ClaudeProvider:
    """Default provider — Anthropic Claude, forced single-tool-call pattern
    (same pattern proven in aanya_flow_v5.py's advance())."""

    def __init__(self, model: Optional[str] = None):
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is not set. Add it to backend/.env before using Anaya V6."
            )
        self._client = anthropic.AsyncAnthropic(api_key=api_key)
        self._model = model or os.environ.get("ANAYA_V6_MODEL", "").strip() or DEFAULT_MODEL

    async def call_tool(self, *, system: str, messages: list[dict], tool: dict, max_tokens: int) -> ModelResponse:
        response = await self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=messages,
        )
        tool_input: dict = {}
        tool_name: Optional[str] = None
        for block in response.content:
            if block.type == "tool_use" and block.name == tool["name"]:
                tool_name = block.name
                tool_input = dict(block.input or {})
                break
        text = "".join(b.text for b in response.content if getattr(b, "type", None) == "text").strip()
        return ModelResponse(
            tool_name=tool_name, tool_input=tool_input, text=text,
            stop_reason=response.stop_reason, raw=response,
        )


_PROVIDERS: dict[str, type] = {"claude": ClaudeProvider}

# Named roles the spec's model-gateway section requires: a reasoning model
# (analyze_turn's intent/field extraction), a conversational model
# (compose_reply), extraction (reserved for a future structured-extraction
# call), and a fallback used when the primary role's call fails. All four
# default to the same model today — only the env resolution differs per
# role, so pointing one role at a different model later is a config change,
# never a code change.
_ROLE_ENV_VAR = {
    "reasoning": "ANAYA_V6_MODEL_REASONING",
    "conversational": "ANAYA_V6_MODEL_CONVERSATIONAL",
    "extraction": "ANAYA_V6_MODEL_EXTRACTION",
    "fallback": "ANAYA_V6_MODEL_FALLBACK",
}


class ModelGateway:
    """Facade every anaya_v6 module depends on instead of a concrete
    provider class. Provider selection is env-driven (ANAYA_V6_PROVIDER,
    default "claude") per role, so swapping models/vendors later — for any
    one role, or all of them — is a config change, not a code change across
    the package.

    Pass an explicit `provider` (e.g. a test FakeModelProvider) to use that
    SAME instance for every role, bypassing env resolution and the fallback
    retry entirely — this is what every anaya_v6 test does today, so a test
    failure is never masked by a silent fallback substitution.
    """

    def __init__(self, provider: Optional[ModelProvider] = None, provider_factory=None):
        if provider is not None:
            self._provider_factory = lambda role: provider
        else:
            self._provider_factory = provider_factory or self._default_factory
        self._providers: dict[str, ModelProvider] = {}

    def _default_factory(self, role: str) -> ModelProvider:
        name = os.environ.get(_ROLE_ENV_VAR.get(role, ""), "").strip() or DEFAULT_MODEL
        provider_name = os.environ.get("ANAYA_V6_PROVIDER", "claude").strip().lower()
        provider_cls = _PROVIDERS.get(provider_name)
        if provider_cls is None:
            _log.warning("[MODEL_GATEWAY] unknown provider %r, falling back to claude", provider_name)
            provider_cls = ClaudeProvider
        return provider_cls(model=name)

    def _provider_for(self, role: str) -> ModelProvider:
        if role not in self._providers:
            self._providers[role] = self._provider_factory(role)
        return self._providers[role]

    async def call_tool(
        self, *, system: str, messages: list[dict], tool: dict, max_tokens: int = 700,
        role: str = "conversational",
    ) -> ModelResponse:
        provider = self._provider_for(role)
        try:
            return await provider.call_tool(system=system, messages=messages, tool=tool, max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001 - retry once against the fallback role, then propagate
            if role == "fallback":
                raise
            _log.warning("[MODEL_GATEWAY] role=%s failed (%s: %s), retrying once with fallback model", role, type(exc).__name__, exc)
            fallback_provider = self._provider_for("fallback")
            return await fallback_provider.call_tool(system=system, messages=messages, tool=tool, max_tokens=max_tokens)


_gateway: Optional[ModelGateway] = None


def get_model_gateway() -> ModelGateway:
    global _gateway
    if _gateway is None:
        _gateway = ModelGateway()
    return _gateway
