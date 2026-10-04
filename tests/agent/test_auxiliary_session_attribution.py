"""Auxiliary calls must carry X-Hermes-Session so the live router can attribute them.

Measured on the fleet box 2026-10-03/04 with caller tagging live: ~2 600 calls in
6 h (213 Mtok, avg 81k-token prompts, 97 % cached) reached the proxy with NO
session header. The proxy records those rows as ``caller='ua:OpenAI/Python 2.24.0'``
with ``session_id=NULL``, so the whole auxiliary path (compression / title /
vision / memory) was invisible to per-session accounting and to the productivity
gate.

The main agent turn already sends ``X-Hermes-Session``: the zai provider profile's
``build_api_kwargs_extras`` hook adds it loopback-only (see
``plugins/model-providers/zai``). Auxiliary calls build their own client and
relied on that hook's ``HERMES_SESSION_ID`` env fallback — which is wrong in a
gateway process serving many sessions at once (absent, or attributed to whichever
session exported the var last).

These tests pin the fix: the context-local session id from ``set_runtime_main``
reaches the wire, caller-supplied ``extra_headers`` can no longer clobber it, and
the header never leaves the machine.
"""

import pytest

HEADER = "X-Hermes-Session"
LOOPBACK = "http://127.0.0.1:9099/v1"
REMOTE = "https://api.z.ai/api/paas/v4"


@pytest.fixture(autouse=True)
def _runtime(monkeypatch):
    # Neutralise the process-wide fallback: HERMES_SESSION_ID is exactly the
    # unreliable source this fix exists to stop depending on.
    monkeypatch.delenv("HERMES_SESSION_ID", raising=False)
    from agent import auxiliary_client as aux

    aux.clear_runtime_main()
    yield
    aux.clear_runtime_main()


def _set_session(sid="20261004_150244_e45916b0"):
    from agent import auxiliary_client as aux

    aux.set_runtime_main("zai", "deepseek/deepseek-flash", session_id=sid)


class TestLoopbackGuard:
    """The attribution header is internal-only — it must fail closed."""

    @pytest.mark.parametrize("url", [
        "http://127.0.0.1:9099/v1",
        "http://localhost:9099/v1",
        "localhost:9099",
        "http://[::1]:9099/v1",
    ])
    def test_loopback_forms(self, url):
        from agent import auxiliary_client as aux

        assert aux._endpoint_is_loopback(url) is True

    @pytest.mark.parametrize("url", [
        "https://api.z.ai/api/paas/v4",
        "http://10.0.0.5:9099/v1",
        "https://example.com",
        "",
        None,
        "://nonsense",
    ])
    def test_non_loopback_forms(self, url):
        from agent import auxiliary_client as aux

        assert aux._endpoint_is_loopback(url) is False


class TestSessionAttributionHeaders:
    def test_header_added_from_runtime_session(self):
        from agent import auxiliary_client as aux

        _set_session()
        assert aux._session_attribution_headers({}, LOOPBACK) == {
            HEADER: "20261004_150244_e45916b0"
        }

    def test_never_sent_off_machine(self):
        """A remote endpoint must never receive the internal session header."""
        from agent import auxiliary_client as aux

        _set_session()
        assert aux._session_attribution_headers({}, REMOTE) == {}
        assert aux._session_attribution_headers(None, REMOTE) == {}

    def test_no_session_no_header(self):
        from agent import auxiliary_client as aux

        assert aux._session_attribution_headers({}, LOOPBACK) == {}
        assert aux._session_attribution_headers(None, LOOPBACK) == {}

    def test_existing_headers_preserved(self):
        from agent import auxiliary_client as aux

        _set_session("s1")
        merged = aux._session_attribution_headers({"User-Agent": "x"}, LOOPBACK)
        assert merged["User-Agent"] == "x"
        assert merged[HEADER] == "s1"

    def test_profile_set_header_wins_and_is_not_re_evaluated(self):
        """When the provider profile already set it, keep that value as-is."""
        from agent import auxiliary_client as aux

        _set_session("context-session")
        merged = aux._session_attribution_headers({HEADER: "profile-session"}, REMOTE)
        assert merged[HEADER] == "profile-session"

    def test_blank_session_is_not_sent(self):
        from agent import auxiliary_client as aux

        _set_session("   ")
        assert HEADER not in aux._session_attribution_headers({}, LOOPBACK)

    def test_input_dict_is_not_mutated(self):
        from agent import auxiliary_client as aux

        _set_session()
        original = {"User-Agent": "x"}
        aux._session_attribution_headers(original, LOOPBACK)
        assert original == {"User-Agent": "x"}


class TestBuildCallKwargsCarriesSession:
    def _kwargs(self, base_url):
        from agent import auxiliary_client as aux

        return aux._build_call_kwargs(
            "zai",
            "deepseek/deepseek-flash",
            [{"role": "user", "content": "hi"}],
            base_url=base_url,
        )

    def test_aux_call_carries_session_header_on_loopback(self):
        """The regression this module exists for: aux calls had session NULL."""
        _set_session("sess-42")
        kwargs = self._kwargs(LOOPBACK)
        assert (kwargs.get("extra_headers") or {}).get(HEADER) == "sess-42"

    def test_no_session_header_on_remote_endpoint(self):
        _set_session("sess-42")
        kwargs = self._kwargs(REMOTE)
        assert HEADER not in (kwargs.get("extra_headers") or {})

    def test_no_session_header_without_runtime_session(self):
        kwargs = self._kwargs(LOOPBACK)
        assert HEADER not in (kwargs.get("extra_headers") or {})


class TestAttributionMergePreservesBoth:
    def test_caller_headers_do_not_drop_the_session_header(self):
        """#60293's x-initiator must survive alongside the session header.

        The old code was ``kwargs["extra_headers"] = dict(extra_headers)`` — an
        overwrite that discarded the session header the profile hook had set.
        """
        from agent import auxiliary_client as aux

        _set_session("s7")
        kwargs = {"extra_headers": {HEADER: "s7"}}
        aux._apply_attribution_headers(kwargs, {"x-initiator": "user"}, REMOTE)
        assert kwargs["extra_headers"][HEADER] == "s7"
        assert kwargs["extra_headers"]["x-initiator"] == "user"

    def test_apply_attribution_headers_without_session_is_a_noop(self):
        from agent import auxiliary_client as aux

        kwargs = {}
        aux._apply_attribution_headers(kwargs, None, LOOPBACK)
        assert "extra_headers" not in kwargs

    def test_apply_attribution_headers_omitting_base_url_is_a_noop(self):
        """Fail closed: no base_url means we cannot prove loopback."""
        from agent import auxiliary_client as aux

        _set_session("s9")
        kwargs = {}
        aux._apply_attribution_headers(kwargs)
        assert HEADER not in (kwargs.get("extra_headers") or {})


def test_header_name_matches_the_router_contract():
    """``zai_proxy.py`` reads exactly this header (``_session_id``)."""
    from agent import auxiliary_client as aux

    assert aux.HERMES_SESSION_HEADER == "X-Hermes-Session"
