"""Custom / Ollama (local) provider profile.

Covers any endpoint registered as provider="custom", including local
Ollama instances and OpenAI-compatible reasoning endpoints (GLM-5.2 on
Volcengine ARK, vLLM, llama.cpp). Key quirks:
  - ollama_num_ctx → extra_body.options.num_ctx (local context window)
  - remote reasoning fields require offline model capability metadata or
    the existing model_overrides.custom.<model>.supports_reasoning config
  - reasoning_config disabled (capable endpoint) → reasoning_effort="none"
    (Ollama /v1/chat/completions ignores think=False — ollama#14820)
    + extra_body.think = False only on Ollama URLs (/api/chat and proxies)
  - reasoning_config enabled + effort → top-level reasoning_effort
    (the native OpenAI-compatible format GLM/ARK expect; unset omits it
    so the endpoint's server default applies)
"""

from typing import Any
from urllib.parse import urlparse

from agent.reasoning_effort import OPENAI_COMPAT_WIRE_EFFORTS, clamp_effort
from providers import register_provider
from providers.base import ProviderProfile
from utils import base_url_host_matches


def _looks_like_ollama_endpoint(base_url: str | None) -> bool:
    """True only for explicit Ollama signatures (port 11434 or an ``ollama`` host label).
    ``think`` is Ollama-native; strict hosts (Mistral, Groq) 422 on it, and
    arbitrary localhost may be llama.cpp / vLLM / LM Studio."""
    raw = (base_url or "").strip()
    if not raw:
        return False
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    try:  # urlparse raises ValueError on malformed ports ("host:99999"); treat as not-Ollama.
        if parsed.port == 11434:
            return True
    except ValueError:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    return bool(host) and (host == "ollama.com" or host.endswith(".ollama.com") or "ollama" in host.split("."))


class CustomProfile(ProviderProfile):
    """Custom/Ollama local provider — think=false and num_ctx support."""

    def supported_reasoning_efforts(self, model: str | None) -> tuple[str, ...]:
        """The OpenAI-compat wire set, mirroring this profile's own chat-completions clamp.

        Without this declaration the Responses transport clamps onto the OpenAI
        per-model ladder (``codex_supported_efforts``), where ``max`` is gpt-5.6-only —
        so a custom relay's model had a configured ``max`` silently demoted to
        ``xhigh`` while the same provider over chat-completions forwarded ``max``
        unchanged (#114249). A custom endpoint's vocabulary is undiscoverable, so
        the widest OpenAI-compat set is the honest ceiling; ``ultra`` still clamps
        to ``max`` via the shared ``clamp_effort`` policy.
        """
        return OPENAI_COMPAT_WIRE_EFFORTS

    def default_reasoning_config(self, model: str | None = None) -> dict | None:
        """Unset ``agent.reasoning_effort`` → ``medium``, as on the Nous / OpenRouter profiles.

        Leaving the field off lets the endpoint's own default apply, and for a hosted reasoning
        model that default can be its ceiling: kimi-k3 behind an OpenAI-compatible relay defaults
        to ``max`` — 3x the reasoning tokens and ~3x the latency of medium. The agent skips this
        default for models the catalog marks non-reasoning (``agent.reasoning_params``).
        """
        return {"enabled": True, "effort": "medium"}

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, ollama_num_ctx: int | None = None, **ctx: Any
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}
        if ollama_num_ctx:
            options = extra_body.get("options", {})
            options["num_ctx"] = ollama_num_ctx
            extra_body["options"] = options

        # Reasoning / thinking control for custom OpenAI-compatible endpoints
        # (GLM-5.2 on Volcengine ARK, vLLM, Ollama, llama.cpp, …).
        #
        #   - disabled  → top-level reasoning_effort="none"; extra_body.think
        #     = False only on Ollama URLs (Ollama's thinking-off flag)
        #   - enabled + effort set → TOP-LEVEL reasoning_effort string, the
        #     format GLM-5.2/ARK and other OpenAI-compatible reasoning APIs
        #     expect (GLM documents "high" and "max"; "max" is its default).
        #   - enabled + no effort  → omit both, so the endpoint applies its own
        #     server-side default (do NOT force a level the user didn't pick).
        #
        # We deliberately do NOT emit ``think=True`` on enable: it is an
        # Ollama-only flag and thinking is already server-default-on for these
        # backends, so forcing it risks a 400 on GLM/vLLM endpoints that don't
        # recognize it. Mirrors the DeepSeek/Zai profile precedent. The same
        # constraint applies to ``think=False`` on disable — Mistral/Groq
        # reject unknown fields (HTTP 422 extra_forbidden) rather than ignoring
        # them, so that flag stays Ollama-URL-gated.
        # Generic OpenAI compatibility does not imply thinking-disable support.
        # Reuse the offline capability catalog/model_overrides contract before
        # sending reasoning_effort="none" to strict or unknown endpoints.
        base_url = ctx.get("base_url")
        is_ollama = _looks_like_ollama_endpoint(base_url)
        from agent.models_dev import get_model_capabilities

        capabilities = get_model_capabilities(self.name, ctx.get("model") or "")
        reasoning_supported = False
        _effort = ""
        _enabled = True
        if reasoning_config and isinstance(reasoning_config, dict):
            _effort = (reasoning_config.get("effort") or "").strip().lower()
            _enabled = reasoning_config.get("enabled", True)
            # Explicit capability metadata is authoritative.  Unknown generic
            # OpenAI-compatible endpoints may optimistically send the field and
            # rely on the bounded rejection-retry ladder, except for known strict
            # Mistral endpoints; Ollama-local control remains explicit.
            reasoning_supported = (
                is_ollama
                or (
                    capabilities is not None
                    and capabilities.supports_reasoning
                )
                or (
                    capabilities is None
                    and not base_url_host_matches(base_url, "api.mistral.ai")
                )
            )
        if (
            reasoning_supported
            and reasoning_config
            and isinstance(reasoning_config, dict)
            and _enabled is not False
            and _effort
            and _effort != "none"
        ):
            top_level["reasoning_effort"] = (
                "default" if base_url_host_matches(str(base_url or ""), "api.groq.com")
                else clamp_effort(_effort, OPENAI_COMPAT_WIRE_EFFORTS)
            )
        elif reasoning_supported and reasoning_config and isinstance(reasoning_config, dict):
            if _effort == "none" or _enabled is False:
                # Ollama's /v1/chat/completions silently ignores
                # extra_body.think (only /api/chat honours it — ollama#14820)
                # but respects the top-level reasoning_effort field (#25758).
                # Capable endpoints receive reasoning_effort="none"; add the
                # native think=False flag only when the URL is Ollama.
                top_level["reasoning_effort"] = "none"
                if is_ollama:
                    extra_body["think"] = False
            elif _effort and base_url_host_matches(str(ctx.get("base_url") or ""), "api.groq.com"):
                # Groq's OpenAI-compatible wire accepts top-level reasoning_effort only as
                # "none" / "default"; any graded level ("medium", "high") 400s (#75089).
                top_level["reasoning_effort"] = "default"
            elif _effort:
                top_level["reasoning_effort"] = clamp_effort(_effort, OPENAI_COMPAT_WIRE_EFFORTS)
        return extra_body, top_level

    def fetch_models(
        self, *, api_key: str | None = None, base_url: str | None = None, timeout: float = 8.0
    ) -> list[str] | None:
        """base_url is user-configured; fetch only if set."""
        if not (base_url or self.base_url):
            return None
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)


custom = CustomProfile(
    name="custom", aliases=("ollama", "local", "vllm", "llamacpp", "llama.cpp", "llama-cpp"),
    env_vars=(),  # No fixed key — custom endpoint
    base_url="",  # User-configured
    # An arbitrary client ceiling can exceed a local server's actual output limit.
    # The endpoint owns its generation default.
)

register_provider(custom)
