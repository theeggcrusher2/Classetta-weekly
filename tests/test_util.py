"""Unit tests for small shared helpers that must behave the same on
every supported platform."""

import pytest

from macrt.util import member_segments


def test_plain_name():
    assert member_segments("readme.txt") == ["readme.txt"]


def test_path_segments():
    assert member_segments("good/a.txt") == ["good", "a.txt"]
    assert member_segments("Docs/Sub/Deep/File.txt") == ["Docs", "Sub", "Deep", "File.txt"]


def test_backslashes_and_colons():
    assert member_segments("good\\a.txt") == ["good", "a.txt"]
    assert member_segments("drive:path/Sub/File") == ["drive", "path", "Sub", "File"]


def test_escape_segments_removed():
    assert member_segments("../evil.txt") == ["evil.txt"]
    assert member_segments("/abs.txt") == ["abs.txt"]
    assert member_segments("good/../../evil.txt") == ["good", "evil.txt"]


def test_trailing_dots_removed():
    assert member_segments("abc...") == ["abc"]
    assert member_segments("path./dir/file...") == ["path", "dir", "file"]
    assert member_segments("path/file.") == ["path", "file"]


def test_empty_segments_removed():
    assert member_segments("a//b") == ["a", "b"]
    assert member_segments("") == []
    assert member_segments("///") == []


def test_nul_bytes_removed():
    assert member_segments("good/\0file.txt") == ["good", "file.txt"]
    assert member_segments("\0") == []


def test_segments_match_macextract_sanitiser():
    """The Filer's rows must match the paths extraction writes."""
    try:
        from macextract._core import _sanitize_name
    except ImportError:
        pytest.skip("macextract not installed")
    names = [
        "readme.txt",
        "good/a.txt",
        "../evil.txt",
        "/abs.txt",
        "good/../../evil.txt",
        "path\\to:file",
        "a//b",
        "abc...",
        "path./dir/file...",
        "path/file.",
        "good/\0file.txt",
        "\0",
    ]
    import posixpath
    for name in names:
        try:
            want = _sanitize_name(name) or None   # "" from a nul-only name means nothing usable
        except Exception:
            want = None
        segments = member_segments(name)
        got = posixpath.join(*segments) if segments else None
        assert got == want, f"{name!r}: got {got!r}, want {want!r}"
