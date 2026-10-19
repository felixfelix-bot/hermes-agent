"""WS-B: a spatial child must not ride a lane with no spatial candidate.

The manager profile pins ``delegation.model: tier/delegation``. That tier's first
candidate (``hermes/workhorse``) carries operator-directed *placeholder* axes
(spatial/vision basic) but dispatches to a text-only DeepSeek model, so a naive
"some candidate declares spatial" check passes while the real child is blind.
The guard therefore counts only real models (never routing aliases), refuses the
delegation before any child is spawned, and logs a goal-hash-only JSONL record
so the volume can be measured before per-task routing exists.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import tools.delegate_tool as dt  # noqa: E402
from tools.delegate_spatial_guard import (  # noqa: E402
    declares_spatial,
    guard,
    lane_has_spatial,
)


def _yaml(d: dict) -> str:
    import yaml

    return yaml.safe_dump(d)


@pytest.fixture()
def bot(tmp_path, monkeypatch):
    tiers = {
        "tier/delegation": {
            "candidates": ["hermes/workhorse", "glm-5.3", "deepseek/deepseek-flash"],
        },
        "tier/spatial-worker": {"candidates": ["gpt-6-astra"]},
        "tier/aliased": {"candidates": ["vendor/spy-alias"]},
        "tier/bad": "malformed entry — not a dict",
    }
    models = {
        # Poisons the naive check: claims spatial, dispatches DeepSeek.
        "hermes/workhorse": {
            "spatial": "basic",
            "vision": "basic",
            "caveats": ["OPERATOR-DIRECTED: floors are a routing floor, NOT a "
                        "capability claim."],
            "evidence": "operator directive",
        },
        "glm-5.3": {"coding_class": "solid"},
        "deepseek/deepseek-flash": {"coding_class": "solid"},
        # The real id keeps operator-directed PLACEHOLDER axes in production;
        # it is still the fleet's designated spatial lane, so it must count.
        "gpt-6-astra": {
            "spatial": "basic",
            "vision": "basic",
            "caveats": ["capability axis values are floor-meeting placeholders"],
            "evidence": "operator directive + live canary",
        },
        # An alias outside the hermes/ namespace: reported_as names another id.
        "vendor/spy-alias": {
            "spatial": "basic",
            "vision": "basic",
            "reported_as": ["vendor/real-model"],
        },
    }
    b = tmp_path / "bot"
    b.mkdir()
    (b / "model_tiers.yaml").write_text(_yaml({"tiers": tiers}))
    (b / "model_benchmarks.yaml").write_text(_yaml({"models": models}))
    monkeypatch.setenv("HERMES_BOT_DIR", str(b))
    monkeypatch.setenv("HERMES_DELEGATION_GUARD_LOG", str(tmp_path / "guard.jsonl"))
    return b


# ── declaration syntax ──────────────────────────────────────────────────────


def test_detects_only_explicit_spatial_declarations():
    assert declares_spatial("spatial_tag: basic\nroute the pads")
    assert declares_spatial('spatial_tag: "basic"\nroute the pads')
    assert declares_spatial("text\nvisual_artifact: reports/map.png\n")
    assert not declares_spatial("spatial_tag: none\nordinary coding task")
    assert not declares_spatial("just fix the login bug")


def test_empty_and_comment_values_never_declare():
    assert not declares_spatial("spatial_tag:\nsome text")
    assert not declares_spatial("spatial_tag: # a comment, not a value")
    assert not declares_spatial('visual_artifact: ""')
    assert not declares_spatial("visual_artifact: <path>")


def test_quoted_values_keep_inner_whitespace():
    assert declares_spatial('spatial_tag: " basic"')
    assert declares_spatial("visual_artifact: \"reports/my map.png\"")
    assert not declares_spatial("spatial_tag: ''")
    assert not declares_spatial("spatial_tag: 'none'")


def test_every_occurrence_is_checked_so_opt_out_cannot_mask_a_declaration():
    # An earlier opt-out must not hide a later real declaration (finditer).
    assert declares_spatial("visual_artifact: none\nvisual_artifact: map.png")
    assert not declares_spatial("visual_artifact: map.png\nvisual_artifact: none"
                                .replace("map.png", "none"))


# ── lane capability ─────────────────────────────────────────────────────────


def test_placeholder_candidate_does_not_make_the_lane_spatial(bot):
    assert lane_has_spatial("tier/delegation") is False
    assert lane_has_spatial("tier/spatial-worker") is True


def test_concrete_lane_is_checked_directly(bot):
    assert lane_has_spatial("gpt-6-astra") is True
    assert lane_has_spatial("glm-5.3") is False


def test_fleet_alias_never_satisfies_the_floor(bot):
    # `hermes/workhorse` advertises spatial/vision basic but serves DeepSeek.
    assert lane_has_spatial("hermes/workhorse") is False
    # ... and an alias outside that namespace is caught via reported_as.
    assert lane_has_spatial("vendor/spy-alias") is False
    assert lane_has_spatial("tier/aliased") is False


def test_malformed_tier_entry_is_unknown_not_a_crash(bot):
    assert lane_has_spatial("tier/bad") is None
    assert lane_has_spatial("tier/does-not-exist") is None
    assert lane_has_spatial("") is None


def test_missing_capability_config_is_unknown(bot, tmp_path):
    import os
    os.environ["HERMES_BOT_DIR"] = str(tmp_path / "empty-bot")
    try:
        assert lane_has_spatial("tier/delegation") is None
    finally:
        os.environ["HERMES_BOT_DIR"] = str(bot)


# ── guard ───────────────────────────────────────────────────────────────────


def test_guard_refuses_a_spatial_child_on_the_delegation_pool(bot, tmp_path):
    msg = guard(
        [{"goal": "Measure the F.Cu pad escapes", "context": "spatial_tag: basic"}],
        {"model": "tier/delegation"},
    )
    assert msg and "tier/spatial-worker" in msg
    rows = (tmp_path / "guard.jsonl").read_text().splitlines()
    assert len(rows) == 1
    rec = json.loads(rows[0])
    assert rec["ref"] == "tier/delegation"
    assert rec["event"] == "refused"
    assert len(rec["goal_sha256"]) == 64
    assert "Measure" not in json.dumps(rec)  # prompts are not logged


def test_guard_allows_a_spatial_child_on_a_spatial_lane(bot, tmp_path):
    assert guard([{"goal": "x", "context": "spatial_tag: basic"}],
                 {"model": "tier/spatial-worker"}) is None
    assert not (tmp_path / "guard.jsonl").exists()


def test_guard_leaves_non_spatial_delegation_alone(bot, tmp_path):
    assert guard([{"goal": "fix the login bug"}], {"model": "tier/delegation"}) is None
    assert not (tmp_path / "guard.jsonl").exists()


def test_guard_checks_every_task_and_names_and_logs_each_one(bot, tmp_path):
    msg = guard(
        [
            {"goal": "read the wing tab drawing",
             "context": "visual_artifact: wing.png"},
            {"goal": "rename a variable"},
            {"goal": "map the seats", "context": "spatial_tag: basic"},
        ],
        {"model": "tier/delegation"},
    )
    assert msg and "tasks 0, 2" in msg
    rows = (tmp_path / "guard.jsonl").read_text().splitlines()
    assert len(rows) == 2
    assert [json.loads(r)["task_index"] for r in rows] == [0, 2]


def test_guard_fails_closed_when_the_lane_is_unknown(bot, tmp_path):
    msg = guard([{"goal": "x", "context": "spatial_tag: basic"}],
                {"model": "tier/bad"})
    assert msg and "unknown" in msg.lower()
    msg = guard([{"goal": "x", "context": "spatial_tag: basic"}],
                {"model": "tier/nope"})
    assert msg and "unknown" in msg.lower()


def test_guard_fails_closed_for_declared_spatial_on_internal_error(bot, monkeypatch,
                                                                   tmp_path):
    def boom(*a, **k):
        raise RuntimeError("guard bug")
    monkeypatch.setattr("tools.delegate_spatial_guard._guard_inner", boom)
    msg = guard([
        {"goal": "x", "context": "spatial_tag: basic"},
        {"goal": "y"},
        {"goal": "z", "context": "visual_artifact: m.png"},
    ], {"model": "tier/delegation"})
    assert msg and "could not be verified" in msg and "tasks 0, 2" in msg
    # every declared-spatial task is accounted for in the log
    rows = (tmp_path / "guard.jsonl").read_text().splitlines()
    assert [json.loads(r)["task_index"] for r in rows] == [0, 2]
    assert all(json.loads(r)["reason"] == "guard-internal-error" for r in rows)
    # a non-spatial task must still proceed on a guard bug
    assert guard([{"goal": "fix the login bug"}], {"model": "tier/delegation"}) is None


def _set_policy(bot, content):
    (bot / "capability_routing.yaml").write_text(content)


def test_malformed_floor_fails_closed_even_for_a_floor_meeting_model(bot):
    import yaml
    _set_policy(bot, yaml.safe_dump({
        "classes": {"spatial_required": {
            "axes": {"spatial": ["strong"], "vision": "basic"}}}}))
    # a malformed configured minimum must not become a weaker one
    assert lane_has_spatial("gpt-6-astra") is False
    assert lane_has_spatial("tier/spatial-worker") is False
    msg = guard([{"goal": "x", "context": "spatial_tag: basic"}],
                {"model": "tier/spatial-worker"})
    assert msg and "no spatial-capable candidate" in msg


@pytest.mark.parametrize("content", [
    "this is: {not: valid: yaml: at all",        # invalid YAML
    "classes: 'not-a-mapping'",                  # classes is a string
    "classes:\n  spatial_required: 'nope'",     # class is a string
    "classes:\n  spatial_required:\n    axes: [strong]",   # axes is a list
    "classes:\n  spatial_required:\n    axes: {}",        # axes is empty
    "classes:\n  spatial_required:\n    axes: {spatial: ultra}",  # unknown level
])
def test_present_but_invalid_policy_refuses_everything(bot, content):
    _set_policy(bot, content)
    assert lane_has_spatial("gpt-6-astra") is False
    assert lane_has_spatial("tier/spatial-worker") is False


def test_absent_policy_uses_the_default_floor(bot):
    assert not (bot / "capability_routing.yaml").exists()
    assert lane_has_spatial("gpt-6-astra") is True


def test_valid_policy_floor_is_used(bot):
    import yaml
    _set_policy(bot, yaml.safe_dump({
        "classes": {"spatial_required": {"axes": {"spatial": "good"}}}}))
    # gpt-6-astra declares basic < good: a VALID stronger floor must bind
    assert lane_has_spatial("gpt-6-astra") is False
    assert lane_has_spatial("tier/spatial-worker") is False


def test_fallback_detector_never_under_detects_the_guard():
    corpus = [
        "spatial_tag: basic", 'spatial_tag: " basic"', "spatial_tag: none",
        "spatial_tag: none.", 'spatial_tag: ""', "spatial_tag: # comment",
        "spatial_tag:", "visual_artifact: m.png",
        'visual_artifact: "reports/my map.png"', "visual_artifact: <path>",
        "**spatial_tag: basic**", "spatial_tag: NONE", "  - spatial_tag: good",
        "spatial_tag: none\nspatial_tag: good",
    ]
    for c in corpus:
        assert not declares_spatial(c) or dt._fallback_declares_spatial(c), c


def test_inherited_parent_model_is_checked(bot):
    # delegation.model empty -> child inherits the parent model
    assert guard([{"goal": "x", "context": "spatial_tag: basic"}],
                 {"model": ""}, parent_model="glm-5.3")
    assert guard([{"goal": "x", "context": "spatial_tag: basic"}],
                 {"model": ""}, parent_model="gpt-6-astra") is None


# ── call site: delegate_task must refuse BEFORE any child is built ──────────


class _StubParent:
    _delegate_depth = 0
    model = "glm-5.3"


def _arm_builder(monkeypatch):
    calls = []

    def _builder(**kwargs):
        calls.append(kwargs)
        raise AssertionError("BUILDER REACHED")

    monkeypatch.setattr(dt, "_build_child_preserving_parent_tools", _builder)
    monkeypatch.setattr(dt, "_load_config",
                        lambda: {"model": "tier/delegation", "max_iterations": 3})
    return calls


def test_delegate_task_refuses_a_spatial_single_goal_before_spawning(bot, monkeypatch):
    calls = _arm_builder(monkeypatch)
    result = dt.delegate_task(goal="Read the wing tab drawing",
                              context="visual_artifact: wing.png",
                              parent_agent=_StubParent())
    assert "WS-B" in result and "BUILDER REACHED" not in result
    assert calls == []


def test_delegate_task_refuses_a_spatial_batch_task_before_spawning(bot, monkeypatch):
    calls = _arm_builder(monkeypatch)
    result = dt.delegate_task(
        tasks=[
            {"goal": "Fix the login form validation", "context": "unit tests"},
            {"goal": "Read the wing tab drawing", "context": "spatial_tag: basic"},
        ],
        parent_agent=_StubParent())
    assert "WS-B" in result and "task 1" in result
    assert "BUILDER REACHED" not in result
    assert calls == []


def test_delegate_task_uses_the_inherited_parent_model_when_unpinned(bot, monkeypatch):
    calls = _arm_builder(monkeypatch)
    monkeypatch.setattr(dt, "_load_config", lambda: {"model": "", "max_iterations": 3})
    result = dt.delegate_task(goal="Read the wing tab drawing",
                              context="spatial_tag: basic",
                              parent_agent=_StubParent())
    assert "WS-B" in result and "glm-5.3" in result
    assert calls == []


def test_delegate_task_does_not_brick_ordinary_delegation_on_a_guard_bug(bot, monkeypatch):
    calls = _arm_builder(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("guard bug")

    monkeypatch.setattr("tools.delegate_spatial_guard.guard", boom)
    try:
        dt.delegate_task(goal="Fix the login form validation",
                         parent_agent=_StubParent())
        reached = True
    except Exception:
        reached = True  # any failure past the builder seam still proves it
    assert calls, "a guard bug must not stop ordinary delegation"
    assert reached


def test_delegate_task_fails_closed_on_a_guard_bug_with_spatial_declared(bot, monkeypatch):
    calls = _arm_builder(monkeypatch)

    def boom(*a, **k):
        raise RuntimeError("guard bug")

    monkeypatch.setattr("tools.delegate_spatial_guard.guard", boom)
    result = dt.delegate_task(goal="Read the wing tab drawing",
                              context="spatial_tag: basic",
                              parent_agent=_StubParent())
    assert "WS-B" in result and "BUILDER REACHED" not in result
    assert calls == []

def test_delegate_task_fallback_is_independent_of_a_broken_guard_module(bot, monkeypatch):
    # the guard module itself fails to import: the call-site fallback must
    # still refuse a declared-spatial child and never spawn it...
    calls = _arm_builder(monkeypatch)
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "tools.delegate_spatial_guard", SimpleNamespace())
    result = dt.delegate_task(goal="Read the wing tab drawing",
                              context="spatial_tag: basic",
                              parent_agent=_StubParent())
    assert "WS-B" in result and "BUILDER REACHED" not in result
    assert calls == []
    # ...while ordinary delegation proceeds past the guard seam
    try:
        dt.delegate_task(goal="Fix the login form validation",
                         parent_agent=_StubParent())
    except Exception:
        pass  # any failure past the builder seam proves the delegation ran on
    assert calls


def test_fallback_logs_and_names_every_declared_spatial_task(bot, monkeypatch, tmp_path):
    # broken module + a batch: EVERY declared-spatial task must be refused,
    # named in the message, and recorded (goal-hash-only) in the JSONL.
    calls = _arm_builder(monkeypatch)
    from types import SimpleNamespace
    monkeypatch.setitem(sys.modules, "tools.delegate_spatial_guard", SimpleNamespace())
    result = dt.delegate_task(
        tasks=[
            {"goal": "Read the wing tab drawing", "context": "spatial_tag: basic"},
            {"goal": "Fix the login form validation"},
            {"goal": "Map the seats", "context": "visual_artifact: seats.png"},
        ],
        parent_agent=_StubParent())
    assert "WS-B" in result and "tasks 0, 2" in result
    assert "BUILDER REACHED" not in result
    assert calls == []
    rows = (tmp_path / "guard.jsonl").read_text().splitlines()
    assert [json.loads(r)["task_index"] for r in rows] == [0, 2]
    assert all(json.loads(r)["reason"] == "guard-module-broken" for r in rows)
    assert "wing tab" not in (tmp_path / "guard.jsonl").read_text()