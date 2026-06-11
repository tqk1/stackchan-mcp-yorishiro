"""Tests for the Phase D note tools (sandboxed agent memos)."""

import pytest

from stackchan_mcp import notes


@pytest.fixture(autouse=True)
def _notes_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("STACKCHAN_NOTES_DIR", str(tmp_path / "notes"))
    return tmp_path / "notes"


def test_write_read_roundtrip():
    result = notes.write_note("memo", "こんにちは")
    assert result["ok"] is True
    assert result["name"] == "memo.md"  # bare name gets .md

    read = notes.read_note("memo.md")
    assert read["content"] == "こんにちは"
    # The bare name resolves to the same file.
    assert notes.read_note("memo")["content"] == "こんにちは"


def test_write_overwrites_by_default_and_appends_on_request():
    notes.write_note("list.txt", "milk\n")
    notes.write_note("list.txt", "eggs\n")
    assert notes.read_note("list.txt")["content"] == "eggs\n"

    notes.write_note("list.txt", "bread\n", append=True)
    assert notes.read_note("list.txt")["content"] == "eggs\nbread\n"


def test_list_notes_reports_files(_notes_dir):
    assert notes.list_notes()["notes"] == []  # missing dir is fine
    notes.write_note("a", "1")
    notes.write_note("b.txt", "2")
    (_notes_dir / ".hidden.md").write_text("x")
    (_notes_dir / "binary.bin").write_text("x")

    listing = notes.list_notes()
    names = [n["name"] for n in listing["notes"]]
    assert names == ["a.md", "b.txt"]
    assert all(n["bytes"] >= 1 and n["mtime"] > 0 for n in listing["notes"])


def test_read_missing_note_raises():
    with pytest.raises(ValueError, match="not found"):
        notes.read_note("nope")


@pytest.mark.parametrize(
    "bad",
    ["", "  ", "a/b", "a\\b", "..", "..secret", ".hidden", "x" * 101],
)
def test_invalid_names_rejected(bad):
    with pytest.raises(ValueError):
        notes.write_note(bad, "x")


def test_size_cap_enforced():
    big = "x" * (notes.MAX_NOTE_BYTES + 1)
    with pytest.raises(ValueError, match="exceed"):
        notes.write_note("big", big)

    notes.write_note("grow", "x" * (notes.MAX_NOTE_BYTES - 1))
    with pytest.raises(ValueError, match="exceed"):
        notes.write_note("grow", "yy", append=True)


def test_content_must_be_string():
    with pytest.raises(ValueError, match="string"):
        notes.write_note("memo", 123)  # type: ignore[arg-type]
