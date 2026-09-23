"""Unit tests for the Z.AI / GLM provider profile's reasoning wiring.

Z.AI's GLM-4.5-and-later chat models default to thinking-mode ON when the
request omits ``thinking``.  Before the profile emitted the parameter,
``reasoning_config = {"enabled": False}`` was a silent no-op on the direct
Z.AI route — users who turned thinking off kept burning thinking tokens on
every turn (the desktop "thinking reverts to medium" report).

GLM-5.2 additionally exposes a native ``reasoning_effort`` knob with two
enabled levels (high / max) on the OpenAI-compatible ``/api/paas/v4``
endpoint; the Hermes effort scale is collapsed onto those.

These tests pin the profile's wire-shape contract so Z.AI requests stay
correctly shaped without going live.

The auxiliary-model tests at the bottom pin the *rot* contract: the cheap tier
must come from z.ai's live catalogue, never from the hardcoded id z.ai retired
(``glm-4.5-flash``), and a lookup that fails must fall through rather than
raise or pin a dead id.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


@pytest.fixture
def zai_profile():
    """Resolve the registered Z.AI profile through the real discovery path."""
    # ``model_tools`` triggers plugin discovery on import, which is what
    # registers the Z.AI profile in the global provider registry.
    import model_tools  # noqa: F401
    import providers

    profile = providers.get_provider_profile("zai")
    assert profile is not None, "zai provider profile must be registered"
    return profile


class TestZaiThinkingWireShape:
    """``build_api_kwargs_extras`` produces Z.AI's exact wire format."""

    def test_no_preference_omits_thinking(self, zai_profile):
        """No reasoning_config → omit ``thinking`` so the server default
        applies (matches prior behavior for users with no preference)."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config=None, model="glm-5"
        )
        assert extra_body == {}
        assert top_level == {}

    def test_enabled_sends_enabled_marker(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "medium"}, model="glm-5"
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {}

    def test_explicitly_disabled_sends_disabled_marker(self, zai_profile):
        """``reasoning_config.enabled=False`` → ``thinking.type=disabled``.

        The crucial bit is that the parameter is *sent* at all — GLM defaults
        to thinking-on when ``thinking`` is absent, so an unsent disable
        burns thinking tokens forever.
        """
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model="glm-5"
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}


class TestZaiGLM52ReasoningEffort:
    """GLM-5.2's native ``reasoning_effort`` knob (two enabled levels)."""

    def test_high_maps_to_high(self, zai_profile):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["low", "medium", "minimal"])
    def test_lower_efforts_clamp_up_to_high(self, zai_profile, effort):
        """GLM-5.2's minimum thinking level is high — lower Hermes levels
        clamp onto it."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "high"}

    @pytest.mark.parametrize("effort", ["xhigh", "max"])
    def test_strong_efforts_map_to_max(self, zai_profile, effort):
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": effort},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "enabled"}}
        assert top_level == {"reasoning_effort": "max"}

    def test_disabled_sends_no_effort(self, zai_profile):
        """Disabled reasoning still sends the thinking-off marker but never
        an effort level."""
        extra_body, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False, "effort": "high"},
            model="glm-5.2",
        )
        assert extra_body == {"thinking": {"type": "disabled"}}
        assert top_level == {}


    @pytest.mark.parametrize(
        "model",
        [
            "z-ai/glm-5.2",
            "glm-5-2",
            "glm-5p2",
            "accounts/fireworks/models/glm-5p2",
            "zai-org-glm-5-2",
        ],
    )
    def test_alias_spellings_recognized(self, zai_profile, model):
        _, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "max"},
            model=model,
        )
        assert top_level == {"reasoning_effort": "max"}

    @pytest.mark.parametrize(
        "model",
        ["glm-5.1", "glm-5", "glm-4.7", "glm-4-9b", "", None],
    )
    def test_non_glm_5_2_models_get_no_effort(self, zai_profile, model):
        _, top_level = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": True, "effort": "high"},
            model=model,
        )
        assert top_level == {}


class TestZaiModelGating:
    """GLM 4.5+ get thinking; earlier GLM models are left untouched."""

    @pytest.mark.parametrize(
        "model",
        [
            "glm-4.5",
            "glm-4.5-air",
            "glm-4.5-flash",
            "glm-4.6",
            "glm-5",
            "glm-5.2",
            "GLM-5",  # case-insensitive
        ],
    )
    def test_thinking_capable_models_emit_thinking(self, zai_profile, model):
        extra_body, _ = zai_profile.build_api_kwargs_extras(
            reasoning_config={"enabled": False}, model=model
        )
        assert extra_body == {"thinking": {"type": "disabled"}}


class TestZaiFullKwargsIntegration:
    """End-to-end: the transport's full kwargs carry the reasoning wiring."""


    def test_glm_5_2_effort_reaches_top_level(self, zai_profile):
        from agent.transports.chat_completions import ChatCompletionsTransport

        kwargs = ChatCompletionsTransport().build_kwargs(
            model="glm-5.2",
            messages=[{"role": "user", "content": "ping"}],
            tools=None,
            provider_profile=zai_profile,
            reasoning_config={"enabled": True, "effort": "max"},
            base_url="https://api.z.ai/api/paas/v4",
            provider_name="zai",
        )
        assert kwargs["reasoning_effort"] == "max"
        assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}


# z.ai's live ``/api/paas/v4/models`` on 2026-09-23 (all 11 ids): the retired
# ``glm-4.5-flash`` this profile used to advertise is absent, and the newest
# flash member is ``glm-5.3-flash`` (with a ``-flashx`` variant beside it).
LIVE_ZAI_CATALOG = (
    "glm-4.5",
    "glm-4.5-air",
    "glm-4.6",
    "glm-4.7",
    "glm-5",
    "glm-5-turbo",
    "glm-5.1",
    "glm-5.2",
    "glm-5.3",
    "glm-5.3-flash",
    "glm-5.3-flashx",
)


@pytest.fixture
def zai_plugin(zai_profile):
    """The bundled plugin module, with its memoized aux answer cleared.

    ``providers`` loads bundled plugin dirs under the stable synthetic name
    ``plugins.model_providers.<name>`` (the on-disk dir is ``model-providers``),
    so the module is fetched from ``sys.modules`` after discovery has run.
    """
    import sys

    import model_tools  # noqa: F401  (import triggers plugin discovery)

    zai_module = sys.modules["plugins.model_providers.zai"]
    assert zai_module.zai is zai_profile, "fixture must exercise the registered profile"
    zai_module._aux_model_cache.clear()
    yield zai_module
    zai_module._aux_model_cache.clear()


@pytest.fixture
def zai_creds():
    """Stand in for the profile's credential lookup with a real z.ai endpoint."""
    with patch(
        "hermes_cli.auth.resolve_api_key_provider_credentials",
        return_value={"api_key": "sk-zai-test", "base_url": "https://api.z.ai/api/paas/v4"},
    ):
        yield


class TestZaiAuxModelResolution:
    """``resolve_aux_model`` tracks z.ai's catalogue instead of a constant."""

    def test_picks_the_newest_flash_id_from_the_live_catalog(
        self, zai_profile, zai_plugin, zai_creds
    ):
        with patch.object(
            zai_profile, "fetch_models", return_value=list(LIVE_ZAI_CATALOG)
        ), patch.object(zai_profile, "_probe_serves", return_value=None):
            assert zai_profile.resolve_aux_model() == "glm-5.3-flash"

    def test_plain_flash_beats_the_flashx_variant(
        self, zai_profile, zai_plugin, zai_creds
    ):
        """Same generation, both listed: the documented cheap tier wins."""
        with patch.object(
            zai_profile, "fetch_models", return_value=["glm-5.3-flashx", "glm-5.3-flash"]
        ), patch.object(zai_profile, "_probe_serves", return_value=None):
            assert zai_profile.resolve_aux_model() == "glm-5.3-flash"

    def test_newer_generation_wins_even_though_it_sorts_later_as_a_string(
        self, zai_profile, zai_plugin, zai_creds
    ):
        with patch.object(
            zai_profile,
            "fetch_models",
            return_value=["glm-4.5-flash", "glm-9-flash", "glm-10-flash"],
        ), patch.object(zai_profile, "_probe_serves", return_value=None):
            assert zai_profile.resolve_aux_model() == "glm-10-flash"

    def test_non_chat_and_non_flash_ids_are_ignored(
        self, zai_profile, zai_plugin, zai_creds
    ):
        """Speech/image siblings and the non-flash tiers are not the cheap tier."""
        catalog = [
            "glm-5.3",
            "glm-4.6v",
            "glm-5.3-flash-tts",
            "glm-5.3-flash-embed",
            "glm-5v-turbo",
        ]
        with patch.object(
            zai_profile, "fetch_models", return_value=catalog
        ), patch.object(zai_profile, "_probe_serves", return_value=None):
            assert zai_profile.resolve_aux_model() == ""

    def test_probe_skips_a_candidate_zai_calls_unknown(
        self, zai_profile, zai_plugin, zai_creds
    ):
        """A listed-but-withdrawn id must not pin the aux tier."""
        with patch.object(
            zai_profile, "fetch_models", return_value=list(LIVE_ZAI_CATALOG)
        ), patch.object(
            zai_profile, "_probe_serves", side_effect=[False, None]
        ):
            assert zai_profile.resolve_aux_model() == "glm-5.3-flashx"

    def test_inconclusive_probe_never_subtracts_a_listed_model(
        self, zai_profile, zai_plugin, zai_creds
    ):
        """z.ai 429s (no package / overload) ids it still serves — keep the pick.

        Live 2026-09-23: a 1-token probe of the listed ``glm-5.3-flash`` answers
        HTTP 429 code 1113 "Insufficient balance or no resource package". That is
        the account's billing state, so it must not empty the aux tier.
        """
        with patch.object(
            zai_profile, "fetch_models", return_value=list(LIVE_ZAI_CATALOG)
        ) as fetch, patch.object(zai_profile, "_probe_serves", return_value=None):
            assert zai_profile.resolve_aux_model() == "glm-5.3-flash"
        assert fetch.call_count == 1

    def test_catalog_that_raises_returns_empty_never_raises(
        self, zai_profile, zai_plugin, zai_creds
    ):
        with patch.object(
            zai_profile, "fetch_models", side_effect=RuntimeError("network down")
        ):
            assert zai_profile.resolve_aux_model() == ""

    def test_empty_catalog_returns_empty(self, zai_profile, zai_plugin, zai_creds):
        with patch.object(zai_profile, "fetch_models", return_value=[]):
            assert zai_profile.resolve_aux_model() == ""

    def test_answer_is_memoized(self, zai_profile, zai_plugin, zai_creds):
        """This runs on client-resolution paths: one fetch, then no network."""
        with patch.object(
            zai_profile, "fetch_models", return_value=list(LIVE_ZAI_CATALOG)
        ) as fetch, patch.object(zai_profile, "_probe_serves", return_value=True):
            assert zai_profile.resolve_aux_model() == "glm-5.3-flash"
            assert zai_profile.resolve_aux_model() == "glm-5.3-flash"
        assert fetch.call_count == 1

    def test_empty_answer_is_memoized_too(self, zai_profile, zai_plugin, zai_creds):
        """A "no answer" is memoized as well (with a short TTL), so a host with
        no usable z.ai credential pays the ladder once, not per resolution."""
        with patch.object(zai_profile, "fetch_models", return_value=[]) as fetch:
            assert zai_profile.resolve_aux_model() == ""
            attempts = fetch.call_count
            assert attempts >= 1
            assert zai_profile.resolve_aux_model() == ""
        assert fetch.call_count == attempts

    def test_vision_is_left_to_the_vision_resolution_path(
        self, zai_profile, zai_plugin, zai_creds
    ):
        """The flash tier has no multimodal member — don't hand vision a text id."""
        with patch.object(zai_profile, "fetch_models") as fetch:
            assert zai_profile.resolve_aux_model(vision=True) == ""
        fetch.assert_not_called()

    def test_loopback_relay_catalog_is_not_trusted(
        self, zai_profile, zai_plugin
    ):
        """A local relay publishes a static subset — this fleet's proxy still
        advertises the retired glm-4.5-flash and omits glm-5.3-flash entirely."""
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={"api_key": "proxy-token", "base_url": "http://127.0.0.1:9099"},
        ), patch.object(
            zai_profile, "fetch_models", return_value=list(LIVE_ZAI_CATALOG)
        ) as fetch, patch.object(zai_profile, "_probe_serves", return_value=None):
            assert zai_profile.resolve_aux_model() == "glm-5.3-flash"

        assert fetch.call_args.kwargs["base_url"] == "https://api.z.ai/api/paas/v4"

    def test_a_configured_real_endpoint_keeps_the_catalog_of_that_endpoint(
        self, zai_profile, zai_plugin
    ):
        with patch(
            "hermes_cli.auth.resolve_api_key_provider_credentials",
            return_value={
                "api_key": "k",
                "base_url": "https://api.z.ai/api/paas/v4-residency-eu",
            },
        ), patch.object(
            zai_profile, "fetch_models", return_value=list(LIVE_ZAI_CATALOG)
        ) as fetch, patch.object(zai_profile, "_probe_serves", return_value=None):
            zai_profile.resolve_aux_model()

        assert (
            fetch.call_args.kwargs["base_url"]
            == "https://api.z.ai/api/paas/v4-residency-eu"
        )


class TestZaiAuxProbeVerdicts:
    """Only z.ai's definitive "unknown model" answer subtracts a candidate."""

    def test_unknown_model_400_is_definitive(self, zai_plugin):
        assert zai_plugin._is_missing_model_response(
            400, '{"error":{"code":"1211","message":"Unknown Model, please check the model code."}}'
        )

    def test_no_package_429_is_not_definitive(self, zai_plugin):
        assert not zai_plugin._is_missing_model_response(
            429,
            '{"error":{"code":"1113","message":"Insufficient balance or no resource package. Please recharge."}}',
        )

    def test_overloaded_429_is_not_definitive(self, zai_plugin):
        assert not zai_plugin._is_missing_model_response(
            429, '{"code":"1305","message":"The service may be temporarily overloaded"}'
        )

    @pytest.mark.parametrize("status", [401, 403, 500, 502, 503])
    def test_auth_and_server_errors_are_not_definitive(self, zai_plugin, status):
        assert not zai_plugin._is_missing_model_response(status, "")

    @pytest.mark.parametrize("status", [404, 410])
    def test_gone_statuses_are_definitive(self, zai_plugin, status):
        assert zai_plugin._is_missing_model_response(status, "")


class TestZaiAuxFallbackAgreement:
    """The curated fallback and the legacy dict must not disagree."""

    def test_legacy_fallback_dict_matches_the_profile(self, zai_profile):
        from agent.auxiliary_client import _API_KEY_PROVIDER_AUX_MODELS_FALLBACK

        assert (
            _API_KEY_PROVIDER_AUX_MODELS_FALLBACK["zai"]
            == zai_profile.default_aux_model
        )

    def test_default_aux_model_is_not_the_retired_id(self, zai_profile):
        """``glm-4.5-flash`` is absent from z.ai's live catalogue."""
        assert zai_profile.default_aux_model
        assert zai_profile.default_aux_model != "glm-4.5-flash"

    def test_resolver_and_caller_agree_when_there_is_no_catalog(
        self, zai_profile, zai_plugin
    ):
        """``prefer_fast`` with an unreachable catalogue lands on the same id."""
        from agent import auxiliary_client as ac

        with patch.object(
            ac, "_fast_model_from_catalog", return_value=""
        ), patch.object(zai_profile, "fetch_models", return_value=[]):
            assert (
                ac._get_aux_model_for_provider("zai", prefer_fast=True)
                == zai_profile.default_aux_model
            )

