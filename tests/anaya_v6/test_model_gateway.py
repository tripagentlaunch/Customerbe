"""Model gateway: named roles resolve to independently-configurable models,
and a primary-role failure retries once against the fallback role before
propagating — the gateway must never hard-fail a turn just because one
role's provider construction/call broke, when a fallback is available.
"""

import pytest

from app.anaya_v6.model_gateway import ModelGateway, ModelResponse


class _RoleRecordingProvider:
    def __init__(self, role: str, fail: bool = False):
        self.role = role
        self.fail = fail
        self.calls = 0

    async def call_tool(self, *, system, messages, tool, max_tokens):
        self.calls += 1
        if self.fail:
            raise RuntimeError(f"{self.role} provider failed")
        return ModelResponse(tool_name=tool["name"], tool_input={"role_used": self.role}, text="", stop_reason="tool_use")


@pytest.mark.asyncio
async def test_different_roles_can_resolve_to_different_providers():
    providers = {"reasoning": _RoleRecordingProvider("reasoning"), "conversational": _RoleRecordingProvider("conversational")}
    gateway = ModelGateway(provider_factory=lambda role: providers[role])

    r1 = await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="reasoning")
    r2 = await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="conversational")

    assert r1.tool_input["role_used"] == "reasoning"
    assert r2.tool_input["role_used"] == "conversational"
    assert providers["reasoning"].calls == 1
    assert providers["conversational"].calls == 1


@pytest.mark.asyncio
async def test_provider_instances_are_cached_per_role_not_recreated_each_call():
    build_count = {"n": 0}

    def factory(role):
        build_count["n"] += 1
        return _RoleRecordingProvider(role)

    gateway = ModelGateway(provider_factory=factory)
    await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="reasoning")
    await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="reasoning")
    assert build_count["n"] == 1  # not rebuilt on the second call to the same role


@pytest.mark.asyncio
async def test_a_failed_primary_role_retries_once_against_fallback():
    providers = {
        "reasoning": _RoleRecordingProvider("reasoning", fail=True),
        "fallback": _RoleRecordingProvider("fallback", fail=False),
    }
    gateway = ModelGateway(provider_factory=lambda role: providers[role])

    result = await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="reasoning")

    assert result.tool_input["role_used"] == "fallback"
    assert providers["reasoning"].calls == 1
    assert providers["fallback"].calls == 1


@pytest.mark.asyncio
async def test_a_failed_fallback_itself_propagates_instead_of_looping():
    providers = {"fallback": _RoleRecordingProvider("fallback", fail=True)}
    gateway = ModelGateway(provider_factory=lambda role: providers[role])

    with pytest.raises(RuntimeError):
        await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="fallback")


@pytest.mark.asyncio
async def test_an_explicit_provider_override_serves_every_role_the_same_instance():
    provider = _RoleRecordingProvider("any")
    gateway = ModelGateway(provider=provider)
    await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="reasoning")
    await gateway.call_tool(system="s", messages=[], tool={"name": "t"}, role="conversational")
    assert provider.calls == 2  # same fake used for both roles, matching every anaya_v6 test's fixture
