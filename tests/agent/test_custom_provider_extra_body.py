from types import SimpleNamespace

import pytest

from agent.agent_init import _merge_custom_provider_extra_body
from providers import get_provider_profile


@pytest.mark.parametrize("effort", ["none", "high", "ultra", None])
@pytest.mark.parametrize("capable", [False, True])
def test_custom_remote_reasoning_respects_model_capability(monkeypatch, tmp_path, effort, capable):
    from agent import models_dev

    monkeypatch.setattr(models_dev, "_get_provider_models", lambda *a, **k: None)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "model_overrides:\n  custom:\n    glm-5.2:\n"
        f"      supports_reasoning: {str(capable).lower()}\n"
    )
    profile = get_provider_profile("custom")
    assert profile is not None
    extra, top = profile.build_api_kwargs_extras(
        model="glm-5.2", base_url="https://ark.cn-beijing.volces.com/api/v3",
        reasoning_config={"enabled": effort != "none", "effort": effort},
    )
    assert extra == {}
    expected = "max" if effort == "ultra" else effort
    assert top == ({"reasoning_effort": expected} if capable and effort else {})


def test_custom_profile_omits_ollama_reasoning_fields_for_remote_endpoint():
    profile = get_provider_profile("custom")
    assert profile is not None
    extra_body, top_level = profile.build_api_kwargs_extras(
        base_url="https://api.mistral.ai/v1",
        reasoning_config={"enabled": False, "effort": "none"},
    )

    assert "think" not in extra_body
    assert "reasoning_effort" not in top_level


def test_custom_profile_keeps_ollama_reasoning_fields_for_local_endpoint():
    profile = get_provider_profile("custom")
    assert profile is not None
    extra_body, top_level = profile.build_api_kwargs_extras(
        base_url="http://127.0.0.1:11434/v1",
        reasoning_config={"enabled": False, "effort": "none"},
    )

    assert extra_body["think"] is False
    assert top_level["reasoning_effort"] == "none"




def test_custom_provider_extra_body_preserves_caller_override():
    agent = SimpleNamespace(
        provider="custom",
        model="google/gemma-4-31b-it",
        base_url="https://example.test/v1",
        request_overrides={
            "extra_body": {
                "reasoning_effort": "low",
                "caller_only": True,
            }
        },
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "name": "gemma",
                "base_url": "https://example.test/v1",
                "model": "google/gemma-4-31b-it",
                "extra_body": {
                    "enable_thinking": True,
                    "reasoning_effort": "high",
                },
            }
        ],
    )

    assert agent.request_overrides["extra_body"] == {
        "enable_thinking": True,
        "reasoning_effort": "low",
        "caller_only": True,
    }




def test_named_custom_provider_extra_body_matches_provider_key():
    agent = SimpleNamespace(
        provider="custom:zai-coding-plan",
        model="glm-5.2",
        base_url="https://api.z.ai/api/coding/paas/v4",
        request_overrides={},
    )

    _merge_custom_provider_extra_body(
        agent,
        [
            {
                "provider_key": "other-provider",
                "name": "Other Provider",
                "base_url": "https://api.z.ai/api/coding/paas/v4",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": True},
            },
            {
                "provider_key": "zai-coding-plan",
                "name": "Z.AI Coding Plan",
                "base_url": "https://api.z.ai/api/coding/paas/v4/",
                "model": "glm-5.2",
                "extra_body": {"enable_thinking": False},
            },
        ],
    )

    assert agent.request_overrides == {"extra_body": {"enable_thinking": False}}
