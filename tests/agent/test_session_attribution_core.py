"""Core, provider-agnostic session attribution.

The regression: the ``X-Hermes-Session`` emission lived only in the ``zai``
provider profile, so every other provider aimed at the loopback router went
unattributed — on this fleet that was ~100 % of inference, because the agents
run ``deepseek/deepseek-flash``. Measured 2026-10-04: 586 calls/h, avg 83 266
prompt tokens, ``session_id`` NULL.

These tests pin the contract at the seam that every chat-completions provider
goes through, so no single profile can be the only one that attributes.
"""

import pytest

from agent.session_attribution import (
    HERMES_SESSION_HEADER,
    apply_session_attribution,
    endpoint_is_loopback,
    session_attribution_headers,
)

LOOPBACK = "http://127.0.0.1:9099/v1"
REMOTE = "https://api.deepseek.com/v1"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from agent import auxiliary_client as aux

    aux.clear_runtime_main()
    yield
    aux.clear_runtime_main()


def _bind(sid):
    from agent import auxiliary_client as aux

    aux.set_runtime_main("deepseek", "deepseek/deepseek-flash", session_id=sid)


class TestLoopbackGuard:
    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:9099/v1",
        "http://localhost:9099/v1",
        "localhost:9099",
        "http://[::1]:9099/v1",
    ])
    def test_loopback_forms(self, url):
        assert endpoint_is_loopback(url) is True

    @pytest.mark.parametrize("url", [
        "https://api.deepseek.com/v1",
        "https://api.z.ai/api/paas/v4",
        "http://10.0.0.5:9099/v1",
        "",
        None,
        "://nonsense",
    ])
    def test_non_loopback_forms(self, url):
        assert endpoint_is_loopback(url) is False


class TestHeaderMerging:
    def test_ambient_session_reaches_a_loopback_provider(self):
        """The whole point: deepseek (not zai) gets attributed too."""
        _bind("sess-deepseek")
        assert session_attribution_headers({}, base_url=LOOPBACK) == {
            HERMES_SESSION_HEADER: "sess-deepseek"
        }

    def test_explicit_session_id_wins_over_ambient(self):
        _bind("ambient")
        got = session_attribution_headers({}, base_url=LOOPBACK, session_id="explicit")
        assert got[HERMES_SESSION_HEADER] == "explicit"

    def test_empty_explicit_session_id_suppresses_the_header(self):
        _bind("ambient")
        assert HERMES_SESSION_HEADER not in session_attribution_headers(
            {}, base_url=LOOPBACK, session_id=""
        )

    def test_existing_header_wins_and_repeat_merges_are_idempotent(self):
        _bind("ambient")
        once = session_attribution_headers({}, base_url=LOOPBACK)
        twice = session_attribution_headers(once, base_url=LOOPBACK)
        assert once == twice
        kept = session_attribution_headers(
            {HERMES_SESSION_HEADER: "from-profile"}, base_url=LOOPBACK
        )
        assert kept[HERMES_SESSION_HEADER] == "from-profile"

    def test_other_headers_are_preserved(self):
        _bind("s1")
        got = session_attribution_headers({"User-Agent": "x"}, base_url=LOOPBACK)
        assert got["User-Agent"] == "x"
        assert got[HERMES_SESSION_HEADER] == "s1"

    def test_input_is_not_mutated(self):
        _bind("s1")
        original = {"User-Agent": "x"}
        session_attribution_headers(original, base_url=LOOPBACK)
        assert original == {"User-Agent": "x"}

    def test_never_sent_off_machine(self):
        _bind("s1")
        assert session_attribution_headers({}, base_url=REMOTE) == {}

    def test_no_session_bound_is_not_an_error(self):
        assert session_attribution_headers({}, base_url=LOOPBACK) == {}


class TestApplyToRequestKwargs:
    def test_merges_into_extra_headers(self):
        _bind("s2")
        kwargs = {}
        apply_session_attribution(kwargs, base_url=LOOPBACK)
        assert kwargs["extra_headers"][HERMES_SESSION_HEADER] == "s2"

    def test_caller_extra_headers_survive(self):
        _bind("s2")
        kwargs = {"extra_headers": {"x-initiator": "user"}}
        apply_session_attribution(kwargs, base_url=LOOPBACK)
        assert kwargs["extra_headers"]["x-initiator"] == "user"
        assert kwargs["extra_headers"][HERMES_SESSION_HEADER] == "s2"

    def test_noop_when_remote_or_unbound(self):
        kwargs = {}
        apply_session_attribution(kwargs, base_url=REMOTE)
        assert "extra_headers" not in kwargs
        apply_session_attribution(kwargs, base_url=LOOPBACK)
        assert "extra_headers" not in kwargs


class TestTransportSeam:
    """The chat-completions transport must attribute for ANY provider."""

    def _transport(self):
        from agent.transports import chat_completions as cc

        for name in dir(cc):
            obj = getattr(cc, name)
            if isinstance(obj, type) and hasattr(obj, "build_kwargs"):
                return obj()
        raise AssertionError("no transport class with build_kwargs found")

    def _kwargs(self, base_url):
        from providers import get_provider_profile

        profile = get_provider_profile("deepseek")
        return self._transport().build_kwargs(
            "deepseek/deepseek-flash",
            [{"role": "user", "content": "hi"}],
            provider_profile=profile,
            base_url=base_url,
            session_id="sess-transport",
        )

    def test_deepseek_build_kwargs_carries_the_header_on_loopback(self):
        kwargs = self._kwargs(LOOPBACK)
        assert kwargs["extra_headers"][HERMES_SESSION_HEADER] == "sess-transport"

    def test_remote_endpoint_gets_no_header(self):
        kwargs = self._kwargs(REMOTE)
        assert HERMES_SESSION_HEADER not in dict(kwargs.get("extra_headers") or {})
