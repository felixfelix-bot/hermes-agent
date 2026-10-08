"""Per-context-window memory scoping (fork area 6, re-applied onto v0.21.5)."""
from tools import memory_tool
from tools.memory_tool import _slugify_group, get_memory_dir


def test_slugify_group():
    assert _slugify_group("My Group / DM!") == "my-group-dm"
    assert _slugify_group("") == ""
    assert _slugify_group("  ") == ""


def test_non_messaging_returns_default(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_tool, "_current_group_slug", lambda: "")
    assert get_memory_dir() == tmp_path / "memories"


def test_group_scoped_and_seeded(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_tool, "get_hermes_home", lambda: tmp_path)
    base = tmp_path / "memories"
    base.mkdir()
    (base / "MEMORY.md").write_text("default memory")
    (base / "USER.md").write_text("default user")
    monkeypatch.setattr(memory_tool, "_current_group_slug", lambda: "group-a")

    d = get_memory_dir()
    assert d == base / "group-a"
    assert (d / "MEMORY.md").read_text() == "default memory"
    assert (d / "USER.md").read_text() == "default user"

    # Diverges independently: an existing target is never overwritten.
    (d / "MEMORY.md").write_text("group a memory")
    (base / "MEMORY.md").write_text("changed default")
    assert (get_memory_dir() / "MEMORY.md").read_text() == "group a memory"


def test_no_default_seed_when_base_has_no_content(monkeypatch, tmp_path):
    monkeypatch.setattr(memory_tool, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(memory_tool, "_current_group_slug", lambda: "solo")
    d = get_memory_dir()
    assert d == tmp_path / "memories" / "solo"
    assert not (d / "MEMORY.md").exists()
