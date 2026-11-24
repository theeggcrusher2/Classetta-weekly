"""Tests for document/application association from app resources."""

import machfs
import pytest
import struct

from macrt import assoc
from macrt.macfile import MacFile, VolumeFolder, list_folder
from macrt.resfork import Resource, build_fork, resPurgeable
from macrt.toolbox.icons import LARGE, SMALL


def _fref(file_type: bytes, local_id: int) -> bytes:
    return file_type + struct.pack(">H", local_id) + b"\x00"


def _bndl(maps: list[tuple[bytes, list[tuple[int, int]]]]) -> bytes:
    out = bytearray(b"SFTW" + b"\x00" * 2 + struct.pack(">H", len(maps)))
    for rtype, pairs in maps:
        out += rtype + struct.pack(">H", len(pairs))
        for local_id, resid in pairs:
            out += struct.pack(">HH", local_id, resid)
    return bytes(out)


def _bndl_minus_one(maps: list[tuple[bytes, list[tuple[int, int]]]]) -> bytes:
    """A bundle as Apple's compilers write it: every count field holds
    the entries minus one, the way Rez compiles the Rez-source view
    Inside Macintosh describes."""
    out = bytearray(b"WILD" + b"\x00" * 2 + struct.pack(">H", len(maps) - 1))
    for rtype, pairs in maps:
        out += rtype + struct.pack(">H", len(pairs) - 1)
        for local_id, resid in pairs:
            out += struct.pack(">HH", local_id, resid)
    return bytes(out)


def _pstring(s: str) -> bytes:
    encoded = s.encode("mac_roman")
    return bytes([len(encoded)]) + encoded


def _kind(signature: bytes, region: int, entries: list[tuple[bytes, str]]) -> bytes:
    out = bytearray(signature + struct.pack(">H", region))
    for ftype, text in entries:
        out += ftype + _pstring(text)
    return bytes(out)


def _volume_with(app_name="SurfWorks", app_creator=b"WAVE", doc_type=b"SWTD",
                 doc_creator=b"WAVE", extra=None, app_kind=None, icon_resid=200):
    v = machfs.Volume()
    v.name = "Test Disk"
    image = bytes([0xF0]) * 128
    mask = b"\xff" * 128
    image16 = b"\xcc" * 32
    mask16 = b"\xff" * 32
    resources = [
        Resource(b"ICN#", icon_resid, image + mask),
        Resource(b"ics#", icon_resid, image16 + mask16),
        Resource(b"FREF", 150, _fref(doc_type, 3)),
        Resource(b"BNDL", 128, _bndl([(b"ICN#", [(3, icon_resid)]),
                                      (b"FREF", [(10, 150)])])),
    ]
    if app_kind is not None:
        resources.append(Resource(b"kind", 1000, app_kind))
    app = machfs.File()
    app.type, app.creator = b"APPL", app_creator
    app.rsrc = bytearray(build_fork(resources))
    v[app_name] = app
    doc = machfs.File()
    doc.type, doc.creator = doc_type, doc_creator
    doc.data = b"letter"
    v["Letter"] = doc
    if extra:
        for name, f in extra.items():
            v[name] = f
    return v


def test_parse_bundle_reads_both_arrays():
    data = _bndl([(b"ICN#", [(0, 128), (1, 129)]), (b"FREF", [(2, 150)])])
    maps = assoc.parse_bundle(data)
    assert maps[b"ICN#"] == {0: 128, 1: 129}
    assert maps[b"FREF"] == {2: 150}


def test_parse_bundle_reads_apples_compiled_counts():
    # HyperCard's real bundle, byte for byte off the ADC CD: every count
    # field stores the entries minus one (the Inside Macintosh prose
    # describes the Rez source, not the compiled bytes).
    data = bytes.fromhex(
        "57 49 4C 44 00 00 00 01 49 43 4E 23 00 01 00 00 00 80 00 01 00 81"
        " 46 52 45 46 00 01 00 00 00 80 00 01 00 81")
    maps = assoc.parse_bundle(data)
    assert maps[b"ICN#"] == {0: 128, 1: 129}
    assert maps[b"FREF"] == {0: 128, 1: 129}


def test_parse_bundle_too_short_is_empty():
    assert assoc.parse_bundle(b"\x00") == {}


def test_parse_fref_reads_type_and_local_id():
    ftype, local = assoc.parse_fref(_fref(b"SWTD", 3) + b"\x00\x00\x00\x00")
    assert ftype == b"SWTD" and local == 3


def test_parse_fref_short_data_is_none():
    assert assoc.parse_fref(b"\x00\x01") is None


def test_parse_kind_reads_entries_and_stops_on_truncation():
    data = b"WAVE" + struct.pack(">H", 0) + b"SWTD" + _pstring("Letter") + b"XXXX"
    assert assoc.parse_kind(data) == {b"SWTD": "Letter"}


def test_associated_kind_prefers_exact_type_kind_string():
    kind = _kind(b"WAVE", 0, [(b"apnm", "SurfSoft Works"),
                              (b"SWTD", "SurfSoft Works text document")])
    v = _volume_with(app_kind=kind)
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_kind(doc) == "SurfSoft Works text document"


def test_associated_kind_falls_back_to_application_name_entry():
    kind = _kind(b"WAVE", 0, [(b"apnm", "SurfSoft Works")])
    v = _volume_with(doc_type=b"XXXX", app_kind=kind)
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_kind(doc) == "SurfSoft Works document"


def test_associated_kind_falls_back_to_app_file_name():
    v = _volume_with(app_name="Minimal.bin", app_creator=b"MINI", doc_creator=b"MINI")
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_kind(doc) == "Minimal document"


def test_associated_kind_uses_non_us_kind_when_nothing_matches_region():
    kind = _kind(b"WAVE", 7, [(b"SWTD", "Document texte")])
    v = _volume_with(app_kind=kind)
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_kind(doc) == "Document texte"


def test_associated_kind_prefers_region_zero_over_other_regions():
    v = _volume_with()
    app = v["SurfWorks"]
    fork = build_fork([
        Resource(b"kind", 1000, _kind(b"WAVE", 7, [(b"SWTD", "Document texte")])),
        Resource(b"kind", 1001, _kind(b"WAVE", 0, [(b"SWTD", "US document")])),
    ])
    app.rsrc = bytearray(fork)
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_kind(doc) == "US document"


def test_associated_kind_without_an_app_is_none():
    v = machfs.Volume()
    v.name = "Empty"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"NOPE", b"hi"
    v["Lone"] = doc
    listed = next(f for f in list_folder(v) if f.name == "Lone")
    assert assoc.associated_kind(listed) is None


def test_associated_icon_rows_match_the_declared_small_icon():
    v = _volume_with()
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_icon_rows(doc) == tuple(["##..##..##..##.."] * 16)


def test_associated_icon_rows_large_match_the_declared_large_icon():
    v = _volume_with()
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    assert assoc.associated_icon_rows(doc, large=True) == tuple(["####...." * 4] * 32)


def test_associated_icon_rows_without_the_type_is_none():
    v = _volume_with()               # the FREF declares SWTD
    doc = next(f for f in list_folder(v) if f.name == "Letter")
    doc.type = b"ZZZZ"               # a type the app does not declare
    assert assoc.associated_icon_rows(doc) is None


def test_a_second_icon_type_resolves_through_the_compiled_bundle():
    # A stack typed file gets its app's second icon, through a bundle
    # laid out the way Apple's compilers write it (counts minus one).
    image, mask = b"\xff" * 128, b"\xff" * 128
    resources = [
        Resource(b"ICN#", 128, image + mask),
        Resource(b"ICN#", 129, image + mask),
        Resource(b"FREF", 128, _fref(b"APPL", 0)),
        Resource(b"FREF", 129, _fref(b"STAK", 1)),
        Resource(b"BNDL", 128, _bndl_minus_one([
            (b"ICN#", [(0, 128), (1, 129)]),
            (b"FREF", [(0, 128), (1, 129)])])),
    ]
    v = machfs.Volume()
    v.name = "Stacks"
    app = machfs.File()
    app.type, app.creator = b"APPL", b"WILD"
    app.rsrc = bytearray(build_fork(resources))
    v["HyperCard"] = app
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"STAK", b"WILD", b"stack"
    v["Home"] = doc
    stack = next(f for f in list_folder(v) if f.name == "Home")
    assert assoc.associated_app(stack).name == "HyperCard"
    assert assoc.associated_icon_rows(stack) == tuple(["#" * 16] * 16)


def test_folders_and_programmes_are_not_associated():
    v = _volume_with()
    for f in list_folder(v):
        if f.name == "SurfWorks":
            assert assoc.associated_app(f) is None
            assert assoc.associated_kind(f) is None


def test_strip_wrapper():
    assert assoc._strip_wrapper("App.bin") == "App"
    assert assoc._strip_wrapper("App.HQX") == "App"
    assert assoc._strip_wrapper("Plain") == "Plain"


def test_the_host_walk_runs_once_for_many_creators(tmp_path, monkeypatch):
    """The creating-app table is built in one pass; the per-creator
    lookups then serve from it."""
    import os
    from macrt.macfile import HostFolder

    (tmp_path / "AppOne").mkdir()
    (tmp_path / "AppOne" / "One").write_bytes(b"x")
    (tmp_path / "AppTwo").mkdir()
    (tmp_path / "AppTwo" / "Two").write_bytes(b"y")
    xattrs = {str(tmp_path / "AppOne" / "One"): b"APPLWAVE",
              str(tmp_path / "AppTwo" / "Two"): b"APPLWAV2"}
    real_walk = os.walk
    calls = {"walk": 0}

    def counting_walk(*a, **kw):
        calls["walk"] += 1
        return real_walk(*a, **kw)

    monkeypatch.setattr(assoc.os, "walk", counting_walk)
    monkeypatch.setattr(assoc.os, "getxattr",
                        lambda p, n, follow_symlinks=True: xattrs.get(p, b""))
    assoc._host_app_cache.clear()
    assoc._host_tree_cache.clear()
    try:
        assert assoc._host_app(HostFolder(tmp_path), b"WAVE") is not None
        assert assoc._host_app(HostFolder(tmp_path), b"WAV2") is not None
        assert calls["walk"] == 1
    finally:
        assoc._host_app_cache.clear()
        assoc._host_tree_cache.clear()


def test_the_fork_parse_is_cached_per_fork(monkeypatch):
    fork = build_fork([Resource(b"kind", 1, b"#SWT\x00SurfWorks document\x00", None, resPurgeable)])
    calls = []
    real = assoc._app_info

    def counting(f):
        calls.append(1)
        return real(f)

    monkeypatch.setattr(assoc, "_app_info", counting)
    assoc._info_for_fork.cache_clear()
    try:
        assoc._info_for_fork(fork)
        assoc._info_for_fork(fork)
        assert len(calls) == 1
    finally:
        assoc._info_for_fork.cache_clear()
