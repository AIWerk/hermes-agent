"""Tests for live+curated merge in the generic profile-based provider path.

Guards two contracts:

* #46850 — when a provider's live /v1/models endpoint returns a stale or
  incomplete list, the static curated models from ``_PROVIDER_MODELS`` must
  still appear in the merged result (nothing is dropped).
* #46309 / #49129 — merge *order* is per-provider. Single providers
  (kimi, zai) stay **curated-first** so a deliberately surfaced newest model
  leads even when the live API lags. ``_LIVE_FIRST_PICKER_PROVIDERS``
  (OpenCode Zen / Go) flip to **live-first** because their live API is the
  authoritative catalog and stale curated entries must not lead the picker.
"""

from unittest.mock import MagicMock, patch

from hermes_cli.models import (
    _LIVE_FIRST_PICKER_PROVIDERS,
    provider_model_ids,
)


class TestGenericProviderLiveCuratedMerge:
    """provider_model_ids merges live + curated for generic api_key providers."""

    def _make_profile(self, models=None):
        """Create a minimal mock provider profile."""
        p = MagicMock()
        p.auth_type = "api_key"
        p.base_url = "https://api.example.com/v1"
        p.fetch_models.return_value = models
        p.fallback_models = None
        return p

    def test_curated_first_for_single_provider(self):
        """Single providers (zai) stay curated-first; live-only appended."""
        assert "zai" not in _LIVE_FIRST_PICKER_PROVIDERS
        curated = ["glm-5.2", "glm-5.1", "glm-5"]  # authoritative-intent order
        # Live API lags AND surfaces a brand-new model not yet curated.
        live = ["glm-5", "glm-6-preview"]
        profile = self._make_profile(live)

        with (
            patch("providers.get_provider_profile", return_value=profile),
            patch(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"zai": curated}),
        ):
            result = provider_model_ids("zai")

        # Curated entries lead (commit 658ac1d86, #46309).
        assert result[: len(curated)] == curated
        # Live-only entries (glm-6-preview) still surface, appended afterwards.
        assert "glm-6-preview" in result
        assert result.index("glm-6-preview") >= len(curated)
        # No duplicates for models present in both.
        assert result.count("glm-5") == 1

    def test_live_first_for_opencode_zen(self):
        """OpenCode Zen flips to live-first; curated-only models appended."""
        assert "opencode-zen" in _LIVE_FIRST_PICKER_PROVIDERS
        live = ["nemotron-3-ultra-free", "gpt-5.5", "claude-fable-5"]
        curated = ["gpt-5.5", "claude-fable-5", "big-pickle"]
        profile = self._make_profile(live)

        with (
            patch("providers.get_provider_profile", return_value=profile),
            patch(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"opencode-zen": curated}),
        ):
            result = provider_model_ids("opencode-zen")

        # Live entries lead (authoritative aggregator catalog).
        assert result[: len(live)] == list(live)
        assert result[0] == "nemotron-3-ultra-free"
        # Curated-only entries (big-pickle) appended for discovery.
        assert "big-pickle" in result
        assert result.index("big-pickle") >= len(live)
        # No duplicates.
        assert result.count("gpt-5.5") == 1

    def test_no_models_dropped_either_direction(self):
        """Every live AND curated model survives the merge for both modes."""
        live = ["a", "b"]
        # zai = curated-first
        with (
            patch("providers.get_provider_profile", return_value=self._make_profile(live)),
            patch(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"zai": ["c", "b"]}),
        ):
            zai_result = set(provider_model_ids("zai"))
        assert {"a", "b", "c"} <= zai_result

        # opencode-zen = live-first
        with (
            patch("providers.get_provider_profile", return_value=self._make_profile(live)),
            patch(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"opencode-zen": ["c", "b"]}),
        ):
            zen_result = set(provider_model_ids("opencode-zen"))
        assert {"a", "b", "c"} <= zen_result

    def test_case_insensitive_dedup(self):
        """Dedup is case-insensitive but preserves first occurrence casing."""
        live = ["GLM-5.1", "glm-5"]
        curated = ["glm-5.1", "GLM-5", "glm-4.5"]
        profile = self._make_profile(live)

        with (
            patch("providers.get_provider_profile", return_value=profile),
            patch(
                "hermes_cli.auth.resolve_api_key_provider_credentials",
                return_value={"api_key": "k", "base_url": ""},
            ),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"zai": curated}),
        ):
            result = provider_model_ids("zai")

        # zai is curated-first: curated casing wins for models present in both.
        assert result == ["glm-5.1", "GLM-5", "glm-4.5"]

class TestValidateRequestedModelCuratedFallback:
    """validate_requested_model falls back to curated catalog when live API omits model."""

    def test_model_in_curated_but_not_live_is_accepted(self):
        """When live /v1/models omits a model that exists in the curated
        catalog, validate_requested_model should accept it with a note.

        Patches the real ``_PROVIDER_MODELS`` source (not the function under
        test) so the curated-catalog fallback is genuinely exercised.
        """
        from hermes_cli.models import validate_requested_model

        # Live API returns only glm-5.1, but curated has glm-5.2
        live_models = ["glm-5.1"]
        curated = ["glm-5.2", "glm-5.1", "glm-5", "glm-4.5"]

        with (
            patch("hermes_cli.models.fetch_api_models", return_value=live_models),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"zai": curated}),
        ):
            result = validate_requested_model("glm-5.2", "zai", api_key="dummy")

        assert result["accepted"] is True
        assert result["recognized"] is True
        assert result["message"] is not None
        assert "curated catalog" in result["message"]

    def test_model_not_in_curated_nor_live_is_rejected(self):
        """When a model is in neither live nor curated, it should be rejected."""
        from hermes_cli.models import validate_requested_model

        live_models = ["glm-5.1"]
        curated = ["glm-5.1", "glm-5", "glm-4.5"]

        with (
            patch("hermes_cli.models.fetch_api_models", return_value=live_models),
            patch.dict("hermes_cli.models._PROVIDER_MODELS", {"zai": curated}),
        ):
            result = validate_requested_model("nonexistent-model", "zai", api_key="dummy")

        assert result["accepted"] is False

    def test_model_in_live_is_accepted_without_curated_check(self):
        """When the model is in the live API, it should be accepted directly."""
        from hermes_cli.models import validate_requested_model

        live_models = ["glm-5.1", "glm-5"]

        with patch("hermes_cli.models.fetch_api_models", return_value=live_models):
            result = validate_requested_model("glm-5.1", "zai", api_key="dummy")

        assert result["accepted"] is True
        assert result["recognized"] is True
        assert result["message"] is None

    def test_curated_fallback_is_scoped_to_the_current_provider(self):
        """The curated fallback must not leak models across providers.

        A model that lives in some OTHER provider's catalog (or only on an
        aggregator like OpenRouter) must still be rejected when the current
        provider neither lists it live nor ships it in its OWN curated
        catalog.  The fallback keys on ``_provider_keys(normalized)``, so
        catalog membership is checked per-provider, never globally.
        """
        from hermes_cli.models import validate_requested_model

        # `some-other-model` is known to a DIFFERENT provider, not to zai.
        # zai's live listing also omits it.  It must be rejected.
        live_models = ["glm-5.1"]

        with (
            patch("hermes_cli.models.fetch_api_models", return_value=live_models),
            patch.dict(
                "hermes_cli.models._PROVIDER_MODELS",
                {"zai": ["glm-5.2", "glm-5.1"], "openrouter": ["some-other-model"]},
            ),
        ):
            result = validate_requested_model("some-other-model", "zai", api_key="dummy")

        assert result["accepted"] is False, (
            "A model only present in another provider's catalog must not be "
            "accepted on this provider via the curated fallback."
        )

