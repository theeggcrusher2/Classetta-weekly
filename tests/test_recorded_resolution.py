"""Resolution of the media a Disk Tracker record points at (subtask 1.2).

Suffix blobs are hand-crafted here from bytes — the tag grammar
recorded in disktracker/recorded.py's module docstring (from the
subtask 1.1 findings).  Mount-root searches run against tmp_path
directories passed as injectable roots; nothing here touches the real
filesystem's mount points."""

from pathlib import Path

import machfs
import pytest

from disktracker import parse_dtc
from disktracker.disktracker import Item
from disktracker.filer_adapter import DiskTrackerVolume
from disktracker.recorded import (RecordedResolution, image_candidates,
                                  parse_suffix, resolve_recorded_media,
                                  source_hint)
from disktracker.writer import write_dtc

# --- hand-crafted suffix blobs ----------------------------------------
TAG_CLASSIC = b"\x02\x00"
TAG_ITEM = b"\x12\x00"
TAG_MOUNT = b"\x13\x00"
TAG_NAME = b"\x0e\x00"


def _tag(tag: bytes, payload: bytes) -> bytes:
    return tag + bytes([len(payload)]) + payload


def _name_tag(s: str) -> bytes:
    p = s.encode("utf-16-be")
    return TAG_NAME + bytes([len(p)]) + p


def dt2_suffix(vol="Developer CD", *, mount_a="/nonexistent-a/Developer CD",
               image_rel="MacTech_CD_v1-17.toast",
               image_mount="/nonexistent-b/Images",
               classic="Macintosh HD:Users:casey:Images:MacTech_CD_v1-17.toast"):
    """A DT2 image-sourced suffix: segment A (volume info) + segment B
    (the source image file's own tag stream)."""
    seg_a = (_name_tag(vol)
             + _tag(TAG_MOUNT, mount_a.encode())
             + _tag(TAG_ITEM, b""))                  # the volume alias itself
    seg_b = ((_tag(TAG_CLASSIC, classic.encode()) if classic is not None else b"")
             + (_tag(TAG_ITEM, image_rel.encode()) if image_rel is not None else b"")
             + (_tag(TAG_MOUNT, image_mount.encode()) if image_mount is not None else b""))
    return seg_a + seg_b


def dt2_cd_suffix(vol="Developer CD", mount_a="/nonexistent-a/Developer CD"):
    """A DT2 CD-sourced suffix: segment A only, no source image."""
    return _name_tag(vol) + _tag(TAG_MOUNT, mount_a.encode()) + _tag(TAG_ITEM, b"")


def fake_opener(p):
    """Directories open (a mounted volume); files open when their bytes
    start with the fixture marker.  Mirrors default_opener's shape."""
    if Path(p).is_dir():
        return True
    try:
        return Path(p).read_bytes().startswith(b"FAKEIMG")
    except OSError:
        return False


def image_file(path: Path, marker=b"FAKEIMG") -> Path:
    path.write_bytes(marker + b"\x00" * 512)
    return path





def _roots(tmp_path: Path):
    d = tmp_path / "roots"
    d.mkdir(exist_ok=True)
    return [d]


# --- suffix parsing ----------------------------------------------------

def test_parse_image_sourced_suffix():
    info = parse_suffix(dt2_suffix())
    assert info.mount_path == "/nonexistent-a/Developer CD"
    assert info.image_mount == "/nonexistent-b/Images"
    assert info.image_relpath == "MacTech_CD_v1-17.toast"
    assert info.image_basename == "MacTech_CD_v1-17.toast"
    assert info.volume_name == "Developer CD"


def test_parse_cd_sourced_suffix_has_no_segment_b():
    info = parse_suffix(dt2_cd_suffix())
    assert info.mount_path == "/nonexistent-a/Developer CD"
    assert info.image_mount is None
    assert info.image_relpath is None
    assert info.image_basename is None
    assert info.volume_name == "Developer CD"


def test_image_candidates_order_absolute_then_joined():
    info = parse_suffix(dt2_suffix(image_rel="/Volumes/Images/disk.toast",
                                   image_mount="/elsewhere"))
    assert image_candidates(info) == ["/Volumes/Images/disk.toast",
                                      "/elsewhere/Volumes/Images/disk.toast"]
    info = parse_suffix(dt2_suffix())
    assert image_candidates(info) == ["/nonexistent-b/Images/MacTech_CD_v1-17.toast"]


def test_utf16_name_with_embedded_mount_tag_bytes_is_not_a_path():
    # U+1300 encodes as 13 00 in UTF-16BE; the name tag's payload then
    # contains the mount-tag bytes.  No mount path may come out of it.
    blob = _name_tag("X\u1300Y")
    info = parse_suffix(blob)
    assert info.mount_path is None
    assert info.volume_name == "X\u1300Y"


def test_mount_tag_with_relative_payload_is_rejected():
    blob = _name_tag("V") + _tag(TAG_MOUNT, b"relative/path")
    assert parse_suffix(blob).mount_path is None


def test_item_tag_with_control_bytes_is_rejected():
    blob = _name_tag("V") + _tag(TAG_ITEM, b"ab\x00cd")
    assert parse_suffix(blob).image_relpath is None


def test_basename_falls_back_to_classic_colon_path():
    blob = dt2_suffix(image_rel=None,
                      classic="Macintosh HD:Images:Restoration-CD-MSS-Vol2.iso")
    info = parse_suffix(blob)
    assert info.image_basename == "Restoration-CD-MSS-Vol2.iso"
    assert image_candidates(info) == ["/nonexistent-b/Images/Restoration-CD-MSS-Vol2.iso"]


def test_garbage_suffix_does_not_raise():
    for blob in (b"", b"\x00" * 64, b"\x13\x00", b"\x12\x00\xff", bytes(range(256)) * 4):
        info = parse_suffix(blob)          # must not raise
        assert isinstance(info.volume_name, (str, type(None)))


def test_dt1_empty_suffix_falls_through():
    vol, base = source_hint(b"", "Alpha CD")
    assert (vol, base) == ("Alpha CD", None)
    res = resolve_recorded_media(b"", disk_name="Alpha CD",
                                 mount_roots=[], home=Path("/nonexistent-home"))
    assert res.path is None
    assert res.source is None
    assert res.volume_name == "Alpha CD"
    assert res.image_basename is None


# --- resolution order ---------------------------------------------------

def test_step1b_suffix_image_path_used(tmp_path):
    img = image_file(tmp_path / "MacTech_CD_v1-17.toast")
    suffix = dt2_suffix(image_mount=str(tmp_path))
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == img
    assert res.kind == "image"
    assert res.source == "suffix-image"
    assert res.image_basename == "MacTech_CD_v1-17.toast"
    assert res.volume_name == "Developer CD"


def test_step1a_suffix_mount_path_dir_used(tmp_path):
    mounted = tmp_path / "mnt-vol"
    mounted.mkdir()
    suffix = dt2_suffix(mount_a=str(mounted))
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == mounted
    assert res.kind == "volume"
    assert res.source == "suffix-mount"


def test_step1a_suffix_mount_path_image_file_used(tmp_path):
    img = image_file(tmp_path / "recorded.toast")
    suffix = dt2_suffix(mount_a=str(img))
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == img
    assert res.kind == "image"
    assert res.source == "suffix-mount"


def test_step1b_absolute_relpath_tried_first(tmp_path):
    img = image_file(tmp_path / "abs.toast")
    suffix = dt2_suffix(image_rel=str(img), image_mount="/nonexistent-b")
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == img
    assert res.source == "suffix-image"


def test_step1b_missing_image_falls_back_to_step2(tmp_path):
    vol_dir = _roots(tmp_path)[0] / "Developer CD"
    vol_dir.mkdir()
    suffix = dt2_suffix(image_mount=str(tmp_path / "gone"))   # image not there
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == vol_dir
    assert res.source == "volume-name"
    assert res.kind == "volume"
    # 1c keys survive the fall-through
    assert res.image_basename == "MacTech_CD_v1-17.toast"


def test_step2_name_match_normalizes_case_and_whitespace(tmp_path):
    vol_dir = _roots(tmp_path)[0] / "DEVELOPER  CD"   # double space, wrong case
    vol_dir.mkdir()
    suffix = dt2_cd_suffix()                          # no segment B at all
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == vol_dir
    assert res.source == "volume-name"


def test_step2_name_match_at_depth2(tmp_path):
    nested = _roots(tmp_path)[0] / "user" / "Developer CD"
    nested.mkdir(parents=True)
    suffix = dt2_cd_suffix()
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == nested
    assert res.source == "volume-name"


def test_step2_hidden_dirs_are_skipped(tmp_path):
    hidden = _roots(tmp_path)[0] / ".hidden" / "Developer CD"
    hidden.mkdir(parents=True)
    res = resolve_recorded_media(dt2_cd_suffix(), mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path is None


def test_step2_match_must_open(tmp_path):
    vol_dir = _roots(tmp_path)[0] / "Developer CD"
    vol_dir.mkdir()
    res = resolve_recorded_media(dt2_cd_suffix(), mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=lambda p: False)   # nothing opens
    assert res.path is None
    assert res.source is None


def test_step2_colon_normalized_in_name(tmp_path):
    vol_dir = _roots(tmp_path)[0] / "AP:Restoration"
    vol_dir.mkdir()
    res = resolve_recorded_media(dt2_cd_suffix(vol="AP/Restoration"),
                                 mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == vol_dir


def test_nothing_found_reports_unresolved(tmp_path):
    res = resolve_recorded_media(dt2_suffix(),
                                 mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path is None
    assert res.kind is None
    assert res.source is None
    assert res.volume_name == "Developer CD"           # match keys kept
    assert res.image_basename == "MacTech_CD_v1-17.toast"


def test_suffix_beats_disk_name_for_step2(tmp_path):
    vol_dir = _roots(tmp_path)[0] / "Suffix Name"
    vol_dir.mkdir()
    res = resolve_recorded_media(dt2_cd_suffix(vol="Suffix Name"),
                                 disk_name="Record Name",
                                 mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == vol_dir


def test_disk_name_used_when_suffix_silent(tmp_path):
    vol_dir = _roots(tmp_path)[0] / "Record Name"
    vol_dir.mkdir()
    res = resolve_recorded_media(b"", disk_name="Record Name",
                                 mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path == vol_dir


def test_default_opener_accepts_real_hfs_image(tmp_path):
    v = machfs.Volume()
    v.name = "Real CD"
    f = machfs.File()
    f.data, f.type, f.creator = b"real", b"TEXT", b"ttxt"
    f.crdate = f.mddate = 4000000000
    v["notes.txt"] = f
    img = tmp_path / "Real CD.hfs"
    img.write_bytes(v.write(1024 * 1024))
    suffix = dt2_suffix(image_mount=str(tmp_path),
                        image_rel="Real CD.hfs",
                        classic="Macintosh HD:Real CD.hfs")
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"))
    assert res.path == img
    assert res.source == "suffix-image"
    assert res.kind == "image"


def test_default_opener_rejects_plain_file(tmp_path):
    junk = tmp_path / "junk.bin"
    junk.write_bytes(b"definitely not an image" * 100)
    suffix = dt2_suffix(image_mount=str(tmp_path), image_rel="junk.bin")
    res = resolve_recorded_media(suffix, mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"))
    assert res.path is None


# --- catalog carries the suffix; the adapter passes it through ---------

def _catalog_with_suffix(tmp_path, suffix: bytes):
    disk = Item(kind="disk", name="Restoration CD", items=1,
                created=4000000000, modified=4000000000, suffix=suffix)
    note = Item(kind="file", name="notes.txt", type=b"TEXT", creator=b"ttxt",
                size=5, depth=1, created=4000000000, modified=4000000000)
    cat = tmp_path / "catalog.dtc"
    cat.write_bytes(write_dtc([disk, note]))
    return cat


def test_adapter_carries_suffix_on_catalog_files(tmp_path):
    suffix = dt2_suffix(vol="Restoration CD")
    cat = _catalog_with_suffix(tmp_path, suffix)
    v = DiskTrackerVolume(cat)
    item = v["Restoration CD"]["notes.txt"]
    # write_dtc's disk record has a fixed 308-byte suffix slot (real DT
    # catalogs are ≥308 too), so the roundtrip pads with zeros: the
    # suffix is a prefix of what comes back, not the whole of it.
    assert item.suffix[:len(suffix)] == suffix
    assert not item.suffix[len(suffix):].strip(b"\x00")   # padding only
    assert item.disk == "Restoration CD"


def test_catalog_roundtrip_keeps_keys_for_the_prompt(tmp_path):
    suffix = dt2_suffix(vol="Restoration CD",
                        classic="Macintosh HD:Images:Restoration-CD-MSS-Vol2.iso",
                        image_rel="Restoration-CD-MSS-Vol2.iso")
    cat = _catalog_with_suffix(tmp_path, suffix)
    v = DiskTrackerVolume(cat)
    item = v["Restoration CD"]["notes.txt"]
    items = parse_dtc(cat.read_bytes())
    disk_item = next(i for i in items if i.kind == "disk")
    assert disk_item.suffix[:len(suffix)] == suffix      # 308-byte slot, zero-padded
    res = resolve_recorded_media(item.suffix, disk_name=item.disk,
                                 mount_roots=_roots(tmp_path),
                                 home=Path("/nonexistent-home"),
                                 opener=fake_opener)
    assert res.path is None                     # media absent here
    assert res.image_basename == "Restoration-CD-MSS-Vol2.iso"
    assert res.volume_name == "Restoration CD"


# --- filer wiring: results through the stub, prompt on failure ---------

from test_filer import _dblclick


def _catalog_image(tmp_path):
    from disktracker import build_dtc
    alpha = machfs.Volume()
    alpha.name = "Alpha CD"
    f = machfs.File()
    f.data, f.type, f.creator = b"alpha", b"TEXT", b"ttxt"
    f.crdate = f.mddate = 4000000000
    alpha["notes.txt"] = f
    img = tmp_path / "Alpha CD.hfs"
    img.write_bytes(alpha.write(1024 * 1024))
    cat = tmp_path / "catalog.dtc"
    cat.write_bytes(build_dtc([str(img)]))
    img.rename(tmp_path / "Alpha CD.hfs.moved")
    return cat


def _open_recorded_folder(h, tmp_path, suffix=None):
    if suffix is None:
        cat = _catalog_image(tmp_path)
        disk = "Alpha CD"
    else:
        cat = _catalog_with_suffix(tmp_path, suffix)
        disk = "Restoration CD"
    win = h.d.open_filer(DiskTrackerVolume(cat))
    h.settle()
    _dblclick(win, disk, h.settle)
    return next(w for w in h.d.filer_windows
                if w is not win and w.volume is win.volume)


def _choice(box, label):
    return next(b for b in box.buttons() if b.text() == label)


def test_stub_reports_unresolved_and_shows_the_prompt(harness, tmp_path,
                                                      monkeypatch):
    import disktracker.recorded as recorded
    h = harness(disks=[])
    folder = _open_recorded_folder(h, tmp_path)
    item = folder.entries["notes.txt"]
    monkeypatch.setattr(recorded, "resolve_recorded_media",
                        lambda *a, **k: RecordedResolution(volume_name="Alpha CD"))
    assert folder._resolve_recorded(item) is False
    prompt = h.d._alerts[-1]
    assert "notes.txt" in prompt.text()
    assert "Alpha CD" in prompt.text()
    assert item.recorded_resolution is not None
    assert item.recorded_resolution.path is None


def test_stub_passes_a_found_resolution_through(harness, tmp_path,
                                                monkeypatch):
    import disktracker.recorded as recorded
    h = harness(disks=[])
    folder = _open_recorded_folder(h, tmp_path)
    item = folder.entries["notes.txt"]
    fake = RecordedResolution(path=Path("/somewhere/recorded.toast"),
                              kind="image", source="suffix-image",
                              volume_name="Alpha CD",
                              image_basename="recorded.toast")
    monkeypatch.setattr(recorded, "resolve_recorded_media",
                        lambda *a, **k: fake)
    prompts = []
    monkeypatch.setattr(folder, "_recorded_unavailable",
                        lambda f, **kw: prompts.append(f))
    assert folder._resolve_recorded(item) is True
    assert item.recorded_resolution is fake
    assert item.recorded_image_basename == "recorded.toast"
    assert prompts == []                        # the prompt is the failure path


def test_picker_offers_recorded_basename_as_filter(harness, tmp_path,
                                                   monkeypatch):
    suffix = dt2_suffix(vol="Restoration CD",
                        classic="Macintosh HD:Images:Restoration-CD-MSS-Vol2.iso",
                        image_rel="Restoration-CD-MSS-Vol2.iso")
    h = harness(disks=[])
    folder = _open_recorded_folder(h, tmp_path, suffix)
    item = folder.entries["notes.txt"]
    captured = []

    class _OpenDialog:
        @staticmethod
        def getOpenFileName(*args, **kwargs):
            captured.append((args, kwargs))
            return ("", "")                     # cancelled

    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _OpenDialog)
    folder._pick_recorded_image(item, "Restoration CD", lambda p: None)
    args, kwargs = captured[0]
    assert "Restoration-CD-MSS-Vol2.iso" in str(args) + str(kwargs)

    # …and through the prompt's Choose Image… button, end to end
    folder._recorded_unavailable(item)
    _choice(h.d._alerts[-1], "Choose Image…").click()
    args, kwargs = captured[-1]
    assert "Restoration-CD-MSS-Vol2.iso" in str(args) + str(kwargs)


def test_picker_without_recorded_basename_keeps_plain_filter(harness,
                                                             tmp_path,
                                                             monkeypatch):
    h = harness(disks=[])
    folder = _open_recorded_folder(h, tmp_path)          # DT1-style: empty suffix
    item = folder.entries["notes.txt"]
    captured = []

    class _OpenDialog:
        @staticmethod
        def getOpenFileName(*args, **kwargs):
            captured.append((args, kwargs))
            return ("", "")

    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _OpenDialog)
    folder._pick_recorded_image(item, "Alpha CD", lambda p: None)
    args, kwargs = captured[0]
    assert "All files (*)" in str(args) + str(kwargs)
