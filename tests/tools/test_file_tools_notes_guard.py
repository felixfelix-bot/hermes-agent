"""Tests for the shared-notes full-replace guard (D-164).

`write_file` must refuse to full-replace a per-context-window notes file
(`state/notes/*.md`) or the `state/session-notes.md` index — those are
patch-only. Appends go through `append_note.py`.
"""
from tools.file_tools import _check_shared_notes_write


def test_blocks_notes_group_file():
    err = _check_shared_notes_write(
        "/home/u/.hermes/profiles/manager/state/notes/art-project-bot.md")
    assert err and "BLOCKED" in err


def test_blocks_session_notes_index():
    err = _check_shared_notes_write(
        "/home/u/.hermes/profiles/manager/state/session-notes.md")
    assert err and "BLOCKED" in err


def test_blocks_windows_separators():
    assert _check_shared_notes_write(
        r"C:\Users\u\.hermes\profiles\manager\state\notes\x.md")


def test_blocks_default_sink_too():
    # The non-messaging sink is also patch-only.
    assert _check_shared_notes_write(
        "/home/u/.hermes/profiles/manager/state/notes/_default.md")


def test_allows_ordinary_files():
    for p in ["/home/u/notes.md", "/home/u/state/notes/notes.txt",
              "/home/u/state/other/x.md", "/tmp/state/session-notes.txt"]:
        assert _check_shared_notes_write(p) is None, p
