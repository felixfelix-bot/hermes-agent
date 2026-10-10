"""decompose_gate: free deterministic pre-gate for the kanban decomposer."""

from __future__ import annotations

from hermes_cli import decompose_gate as dg


def test_eligible_triage_card():
    ok, why = dg.should_decompose(title="ship a feature", body="")
    assert ok is True
    assert why == "eligible"


def test_not_triage():
    ok, why = dg.should_decompose(status="ready")
    assert ok is False
    assert "not in triage" in why


def test_already_specified_or_decomposed_or_has_children():
    for kwargs in ({"specified": True}, {"decomposed": True},
                   {"has_children": True}):
        ok, why = dg.should_decompose(**kwargs)
        assert ok is False
        assert why == "already specified/decomposed"


def test_no_decompose_marker():
    ok, why = dg.should_decompose(title="x", body="please no-decompose this")
    assert ok is False
    assert why == "no-decompose marker"


def test_marker_is_case_and_separator_insensitive():
    assert dg.has_no_decompose_marker("No_Decompose this")
    assert dg.has_no_decompose_marker("keep-as-one please")
    assert not dg.has_no_decompose_marker("decompose this fully")
