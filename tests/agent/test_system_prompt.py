"""Tests for agent/system_prompt.py — context-file cwd wiring."""

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.system_prompt import build_system_prompt, build_system_prompt_parts


def _make_agent(**overrides):
    base = dict(
        load_soul_identity=False,
        skip_context_files=False,
        valid_tool_names=[],
        _task_completion_guidance=False,
        _tool_use_enforcement=False,
        _environment_probe=False,
        _kanban_worker_guidance="",
        _memory_store=None,
        _memory_manager=None,
        model="",
        provider="",
        platform="",
        pass_session_id=False,
        session_id="",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _captured_context_cwd(agent):
    """The cwd build_system_prompt_parts hands to build_context_files_prompt."""
    captured = {}

    def fake_context_files(
        cwd=None, skip_soul=False, context_length=None,
        allow_install_tree_fallback=False,
    ):
        captured["cwd"] = cwd
        return ""

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", side_effect=fake_context_files),
    ):
        build_system_prompt_parts(agent)
    return captured["cwd"]


class TestContextFileCwd:
    def test_none_when_terminal_cwd_unset(self, monkeypatch):
        # Unset → None, so discovery falls back to the launch dir inside
        # build_context_files_prompt (the local-CLI #19242 contract).
        monkeypatch.delenv("TERMINAL_CWD", raising=False)
        assert _captured_context_cwd(_make_agent()) is None

    def test_configured_dir_when_terminal_cwd_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        assert _captured_context_cwd(_make_agent()) == tmp_path


def _stable_prompt(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)["stable"]


def _prompt_parts(agent):
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=""),
    ):
        return build_system_prompt_parts(agent)


def _init_code_repo(path):
    """A git repo that actually holds code — the coding posture requires a source
    file (or manifest), not a bare ``.git`` (a prose/notes repo stays general)."""
    import subprocess

    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    (path / "main.py").write_text("print('hi')\n")


class TestCodingContextBlock:
    def test_injected_when_active(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        parts = _prompt_parts(agent)
        assert "coding agent" in parts["stable"]
        assert "Workspace" in parts["context"]

    def test_absent_when_off(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=["read_file"], platform="cli")
        # Drive the real path: force the resolved mode to "off" via config.
        with patch("agent.coding_context._coding_mode", return_value="off"):
            stable = _stable_prompt(agent)
        assert "coding agent" not in stable

    def test_absent_without_tools(self, monkeypatch, tmp_path):
        _init_code_repo(tmp_path)
        monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
        agent = _make_agent(valid_tool_names=[], platform="cli")
        assert "coding agent" not in _stable_prompt(agent)


def test_build_system_prompt_records_stable_prefix():
    agent = _make_agent()
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value="context"),
    ):
        prompt = build_system_prompt(agent)

    assert prompt.startswith(agent._cached_system_prompt_static)
    assert prompt[len(agent._cached_system_prompt_static):].startswith("\n\ncontext")


def test_coding_prompt_preserves_legacy_workspace_order(monkeypatch):
    """The cache split must not reorder the stored coding prompt."""
    import agent.system_prompt as system_prompt

    agent = _make_agent(
        valid_tool_names=["read_file"],
        _parallel_tool_call_guidance=False,
    )
    monkeypatch.setattr(system_prompt, "DEFAULT_AGENT_IDENTITY", "IDENTITY")
    monkeypatch.setattr(system_prompt, "HERMES_AGENT_HELP_GUIDANCE", "HELP")
    monkeypatch.setattr(system_prompt, "STEER_CHANNEL_NOTE", "STEER")
    monkeypatch.setattr(system_prompt, "get_hermes_home", lambda: Path("/hermes"))

    expected_profile = (
        "Active Hermes profile: default. Other profiles (if any) live "
        "under /hermes/profiles/<name>/. Each profile has its own skills/, "
        "plugins/, cron/, and memories/ that affect a different session than "
        "this one. Do not modify another profile's skills/plugins/cron/memories "
        "unless the user explicitly directs you to."
    )
    expected = "\n\n".join((
        "IDENTITY",
        "HELP",
        "STEER",
        "CODING_STABLE",
        "WORKSPACE",
        "Operator instructions (from config):\nOPERATOR",
        expected_profile,
        "SYSTEM_MESSAGE",
        "CONTEXT_FILES",
        "Conversation started: Friday, January 02, 2026",
    ))

    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value="CONTEXT_FILES"),
        patch(
            "agent.coding_context.coding_system_prompt_parts",
            return_value=(
                ["CODING_STABLE"],
                ["WORKSPACE"],
                ["Operator instructions (from config):\nOPERATOR"],
            ),
        ),
        patch("agent.file_safety._resolve_active_profile_name", return_value="default"),
        patch("hermes_time.now", return_value=datetime(2026, 1, 2)),
    ):
        prompt = build_system_prompt(agent, system_message="SYSTEM_MESSAGE")

    assert prompt == expected
    assert agent._cached_system_prompt_static == "\n\n".join(expected.split("\n\n")[:4])


class TestTelegramRichMessagesHint:
    """Verify that TELEGRAM_RICH_MESSAGES_HINT is conditionally included."""

    def test_base_hint_without_rich_messages(self, monkeypatch):
        """When rich_messages is False, only the base hint is used."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": {"rich_messages": False}}}}
            }
            stable = _stable_prompt(agent)
        assert "Standard Markdown is automatically converted" in stable
        assert "lean into it" not in stable
        assert "task lists" not in stable

    def test_rich_hint_with_rich_messages_enabled(self, monkeypatch):
        """When rich_messages is True in gateway.platforms, the extension
        is appended (the canonical/primary location)."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": {"rich_messages": True}}}}
            }
            stable = _stable_prompt(agent)
        assert "lean into it" in stable
        assert "task lists" in stable
        assert "math/formulas" in stable

    def test_rich_hint_from_top_level_platforms(self):
        """Top-level ``platforms.telegram.extra.rich_messages`` is merged
        alongside gateway.platforms, so it works on its own."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "platforms": {"telegram": {"extra": {"rich_messages": True}}}
            }
            stable = _stable_prompt(agent)
        assert "lean into it" in stable
        assert "task lists" in stable

    def test_top_level_overrides_gateway_rich_messages(self):
        """Top-level ``platforms.telegram.extra`` wins over gateway.platforms
        at the leaf, matching the adapter's merge precedence."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": {"rich_messages": False}}}},
                "platforms": {"telegram": {"extra": {"rich_messages": True}}},
            }
            stable = _stable_prompt(agent)
        assert "lean into it" in stable

    def test_gateway_extra_other_keys_does_not_block_top_level_rich_messages(self):
        """When gateway.platforms.telegram.extra has other keys but not
        rich_messages, the top-level rich_messages still activates."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": {"disable_link_previews": True}}}},
                "platforms": {"telegram": {"extra": {"rich_messages": True}}},
            }
            stable = _stable_prompt(agent)
        assert "lean into it" in stable

    def test_base_hint_without_config(self, monkeypatch):
        """When config has no telegram section, only base hint is used."""
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {}
            stable = _stable_prompt(agent)
        assert "Standard Markdown is automatically converted" in stable
        assert "lean into it" not in stable


    def test_gateway_rich_messages_integration_via_real_config(self, tmp_path, monkeypatch):
        """End-to-end through the real config-resolution chain: a config.yaml
        under HERMES_HOME with ``gateway.platforms.telegram.extra.rich_messages``
        must activate the rich hint. ``load_config_readonly`` is NOT mocked here,
        so this guards against the exact path-mismatch bug this PR fixes.
        """
        config_yaml = (
            "gateway:\n"
            "  platforms:\n"
            "    telegram:\n"
            "      extra:\n"
            "        rich_messages: true\n"
        )
        home = tmp_path / "hermes_home"
        home.mkdir()
        (home / "config.yaml").write_text(config_yaml)

        monkeypatch.setenv("HERMES_HOME", str(home))
        # Point config resolution at the temp file without mocking the loader:
        # mirror the pattern used in test_config_env_expansion.py.
        from hermes_cli import config as _cfgmod
        monkeypatch.setattr(_cfgmod, "get_config_path", lambda: home / "config.yaml")

        agent = _make_agent(platform="telegram")
        stable = _stable_prompt(agent)
        assert "lean into it" in stable
        assert "task lists" in stable

    def test_malformed_extra_value_falls_back_to_base_hint(self, tmp_path, monkeypatch):
        """A truthy non-mapping ``extra`` must not crash prompt construction —
        it should fail open to the base hint (Tek's fail-open concern).
        """
        agent = _make_agent(platform="telegram")
        with patch("hermes_cli.config.load_config_readonly") as mock_cfg:
            mock_cfg.return_value = {
                "gateway": {"platforms": {"telegram": {"extra": "not-a-map"}}}
            }
            stable = _stable_prompt(agent)
        assert "Standard Markdown is automatically converted" in stable
        assert "lean into it" not in stable


_SKILLS = "SKILLS_INDEX_SENTINEL"
_CONTEXT = "CONTEXT_FILES_SENTINEL"


def _build(builder, **overrides):
    """Run a build_* function with skills + context files present."""
    agent = _make_agent(valid_tool_names=["skills_list"], **overrides)
    with (
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=_CONTEXT),
        patch("run_agent.get_toolset_for_tool", return_value=None),
        patch("run_agent.build_skills_system_prompt", return_value=_SKILLS),
    ):
        return builder(agent)


class TestSkillsInVolatileBand:
    """The skills index is runtime-mutable, so it lives in the volatile band,
    not the stable band, to keep the cached stable prefix reusable when a
    rebuild picks up a skill change."""

    def test_skills_not_in_stable_band(self):
        parts = _build(build_system_prompt_parts)
        assert _SKILLS not in parts["stable"]

    def test_skills_lead_the_volatile_band(self):
        parts = _build(build_system_prompt_parts)
        assert parts["volatile"].startswith(_SKILLS)

    def test_full_order_is_stable_context_then_skills(self):
        # build_system_prompt joins stable + context + volatile, so the skills
        # index renders after the context files and before the per-turn
        # memory/timestamp tail.
        full = _build(build_system_prompt)
        assert full.index(_CONTEXT) < full.index(_SKILLS)
        assert full.index(_SKILLS) < full.index("Conversation started:")


# ── T4 (cost-reduction-sprint): longest-prefix stability ────────────────────
#
# A provider prompt cache only reuses a prefix that is BYTE-IDENTICAL across
# turns: DeepSeek prices cached input at $0.03/M against $0.14/M uncached
# (~4.7x) and NeuralWatt charges real prefill compute, so one volatile byte
# rendered ABOVE the stable scaffold costs the whole prefix on every rebuild.
# The assembly order (stable -> context -> volatile) is the invariant that
# keeps the prefix reusable; these tests guard the ORDER, not the content.

_MEMORY = "MEMORY_SNAPSHOT_SENTINEL"
_USER = "USER_PROFILE_SENTINEL"
_EXTERNAL_MEMORY = "EXTERNAL_MEMORY_SENTINEL"
_CHANGED_SKILLS = "SKILLS_INDEX_AFTER_A_PATCH"
_DAY_ONE_EARLY = datetime(2026, 9, 19, 0, 5)        # Saturday 00:05
_DAY_ONE_LATE = datetime(2026, 9, 19, 23, 55)       # Saturday 23:55
_DAY_TWO = datetime(2026, 9, 20, 0, 5)              # Sunday


class _FakeMemoryStore:
    """Only the two blocks ``build_system_prompt_parts`` reads."""

    def __init__(self, memory="", user=""):
        self._blocks = {"memory": memory, "user": user}

    def format_for_system_prompt(self, kind):
        return self._blocks.get(kind, "")


class _FakeMemoryManager:
    def build_system_prompt(self):
        return _EXTERNAL_MEMORY


def _build_variant(builder, *, skills=_SKILLS, when=None, **overrides):
    """Rebuild the prompt with a controllable VOLATILE tail.

    ``skills`` and ``when`` are exactly the inputs that move between rebuilds
    in production (the agent patches its own skills mid-session; the day rolls
    over), so varying them is what makes the stability assertions non-vacuous.
    """
    agent = _make_agent(
        valid_tool_names=["skills_list"],
        _memory_store=_FakeMemoryStore(_MEMORY, _USER),
        _memory_enabled=True,
        _user_profile_enabled=True,
        _memory_manager=_FakeMemoryManager(),
        **overrides,
    )
    patches = [
        patch("run_agent.load_soul_md", return_value=""),
        patch("run_agent.build_nous_subscription_prompt", return_value=""),
        patch("run_agent.build_environment_hints", return_value=""),
        patch("run_agent.build_context_files_prompt", return_value=_CONTEXT),
        patch("run_agent.get_toolset_for_tool", return_value=None),
        patch("run_agent.build_skills_system_prompt", return_value=skills),
    ]
    if when is not None:
        patches.append(patch("hermes_time.now", return_value=when))
    with ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        return builder(agent)


class TestLongestPrefixStability:
    """T4: the stable tier is the prefix the upstream prompt cache reuses."""

    def test_stable_prefix_byte_identical_when_volatile_tail_moves(self):
        first = _build_variant(build_system_prompt_parts, skills=_SKILLS,
                               when=_DAY_ONE_EARLY)
        second = _build_variant(build_system_prompt_parts,
                                skills=_CHANGED_SKILLS, when=_DAY_TWO)
        # The tail genuinely moved (skills patched, day rolled) ...
        assert first["volatile"] != second["volatile"]
        # ... and the prefix the provider caches did not, byte for byte.
        assert first["stable"] == second["stable"]
        assert first["stable"].encode("utf-8") == second["stable"].encode("utf-8")

    def test_full_prompt_keeps_the_identical_leading_prefix(self):
        parts_a = _build_variant(build_system_prompt_parts, when=_DAY_ONE_EARLY)
        parts_b = _build_variant(build_system_prompt_parts,
                                 skills=_CHANGED_SKILLS, when=_DAY_TWO)
        full_a = _build_variant(build_system_prompt, when=_DAY_ONE_EARLY)
        full_b = _build_variant(build_system_prompt,
                                skills=_CHANGED_SKILLS, when=_DAY_TWO)
        n = len(parts_a["stable"])
        assert n > 0
        assert full_a.startswith(parts_a["stable"])
        assert full_b.startswith(parts_b["stable"])
        assert full_a[:n] == full_b[:n]

    def test_band_order_is_stable_then_context_then_volatile(self):
        parts = _build_variant(build_system_prompt_parts)
        full = _build_variant(build_system_prompt)
        assert parts["stable"] and parts["context"] and parts["volatile"]
        # Exactly the documented assembly: the three bands, in order, joined
        # with a blank line — stable FIRST, volatile LAST.
        assert full == "\n\n".join(
            p for p in (parts["stable"], parts["context"], parts["volatile"]) if p
        )
        assert full.startswith(parts["stable"])
        assert full.index(parts["context"]) >= len(parts["stable"])
        assert full.index(parts["volatile"]) > full.index(parts["context"])

    def test_stable_band_carries_no_volatile_state(self):
        parts = _build_variant(
            build_system_prompt_parts,
            model="deepseek/deepseek-flash",
            provider="deepseek",
            platform="cli",
            pass_session_id=True,
            session_id="sess-t4-sentinel",
        )
        stable = parts["stable"]
        for volatile_marker in (
            _SKILLS,
            _MEMORY,
            _USER,
            _EXTERNAL_MEMORY,
            "Conversation started:",
            "Model: deepseek/deepseek-flash",
            "Provider: deepseek",
            "Platform: cli",
            "Session ID: sess-t4-sentinel",
        ):
            assert volatile_marker not in stable, volatile_marker
            assert volatile_marker in parts["volatile"], volatile_marker

    def test_timestamp_is_date_only_so_one_day_is_byte_stable(self):
        early = _build_variant(build_system_prompt_parts, when=_DAY_ONE_EARLY)
        late = _build_variant(build_system_prompt_parts, when=_DAY_ONE_LATE)
        assert "Conversation started: Saturday, September 19, 2026" in early["volatile"]
        # 23h50 apart on the same day -> identical bytes, so a rebuild during
        # the day (compaction, gateway turn, session resume) still hits.
        assert early["volatile"] == late["volatile"]
        assert early["stable"] == late["stable"]

    def test_volatile_tail_moves_only_at_the_date_boundary(self):
        one = _build_variant(build_system_prompt_parts, when=_DAY_ONE_LATE)
        two = _build_variant(build_system_prompt_parts, when=_DAY_TWO)
        assert one["volatile"] != two["volatile"]   # keeps the test above honest
        assert one["stable"] == two["stable"]
