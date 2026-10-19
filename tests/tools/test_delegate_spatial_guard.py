"""WS-B: a spatial child must not ride a lane with no spatial candidate.

The manager profile pins ``delegation.model: tier/delegation``. That tier's first
candidate (``hermes/workhorse``) carries operator-directed *placeholder* axes
(spatial/vision basic) but dispatches to a text-only DeepSeek model, so a naive
"some candidate declares spatial" check passes while the real child is blind.
The guard therefore counts only candidates whose benchmark row is a real
capability claim, and refuses the delegation (before any child is spawned) with
a message that names the correct lane. Every refusal is appended to a JSONL log
so the volume can be measured before per-task routing exists.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.delegate_spatial_guard import (  # noqa: E402
    declares_spatial,
    guard,
    lane_has_spatial,
)


def _write(tmp_path: Path, tiers: dict, models: dict) -> Path:
    bot = tmp_path / "bot"
    bot.mkdir()
    (bot / "model_tiers.yaml").write_text(_yaml({"tiers": tiers}))
    (bot / "model_benchmarks.yaml").write_text(_yaml({"models": models}))
    return bot


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
    }
    models = {
        # Poisons the naive check: claims spatial, is not a real claim.
        "hermes/workhorse": {
            "spatial": "basic",
            "vision": "basic",
            "coding_class": "solid",
            "caveats": [
                "OPERATOR-DIRECTED: no measured benchmark. Floors are a ROUTING "
                "floor, NOT a capability claim.",
            ],
            "evidence": "source: operator-directive",
        },
        "glm-5.3": {"coding_class": "solid"},
        "deepseek/deepseek-flash": {"coding_class": "solid"},
        # The real id keeps operator-directed PLACEHOLDER axes in production;
        # it is still the fleet's designated spatial lane, so it must count.
        "gpt-6-astra": {
            "spatial": "basic",
            "vision": "basic",
            "caveats": ["OPERATOR-DIRECTED: capability axis values are "
                        "floor-meeting placeholders."],
            "evidence": "operator directive + live canary",
        },
    }
    b = _write(tmp_path, tiers, models)
    monkeypatch.setenv("HERMES_BOT_DIR", str(b))
    monkeypatch.setenv("HERMES_DELEGATION_GUARD_LOG", str(tmp_path / "guard.jsonl"))
    monkeypatch.delenv("HERMES_ALLOW_SPATIAL_DELEGATION", raising=False)
    return b


def test_detects_only_explicit_spatial_declarations():
    assert declares_spatial("spatial_tag: basic\nroute the pads")
    assert declares_spatial("text\nvisual_artifact: reports/map.png\n")
    assert not declares_spatial("spatial_tag: none\nordinary coding task")
    assert not declares_spatial("just fix the login bug")


def test_placeholder_candidate_does_not_make_the_lane_spatial(bot):
    assert lane_has_spatial("tier/delegation") is False
    assert lane_has_spatial("tier/spatial-worker") is True


def test_concrete_lane_is_checked_directly(bot):
    assert lane_has_spatial("gpt-6-astra") is True
    assert lane_has_spatial("glm-5.3") is False


def test_fleet_alias_never_satisfies_the_floor(bot):
    # `hermes/workhorse` advertises spatial/vision basic but serves DeepSeek.
    assert lane_has_spatial("hermes/workhorse") is False


def test_unknown_lane_is_unknown_not_a_pass(bot):
    assert lane_has_spatial("tier/does-not-exist") is None
    assert lane_has_spatial("") is None


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


def test_guard_checks_every_task_in_a_batch(bot):
    msg = guard(
        [
            {"goal": "rename a variable"},
            {"goal": "read the wing tab drawing", "context": "visual_artifact: wing.png"},
        ],
        {"model": "tier/delegation"},
    )
    assert msg and "task 1" in msg


def test_guard_fails_closed_when_the_lane_is_unknown(bot):
    msg = guard([{"goal": "x", "context": "spatial_tag: basic"}],
                {"model": "tier/nope"})
    assert msg and "unknown" in msg.lower()


def test_inherited_parent_model_is_checked(bot):
    # delegation.model empty -> child inherits the parent model
    assert guard([{"goal": "x", "context": "spatial_tag: basic"}],
                 {"model": ""}, parent_model="glm-5.3")
    assert guard([{"goal": "x", "context": "spatial_tag: basic"}],
                 {"model": ""}, parent_model="gpt-6-astra") is None
