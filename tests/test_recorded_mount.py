"""Mounting the media a recorded Disk Tracker item points at (subtask 1.4).

The catalog is a read-only record: the .dtc/.dt1 is never written, the
DiskTrackerVolume never mutated.  Image fixtures are generated with
machfs — nothing here reads from outside the repository."""

from pathlib import Path

import machfs

from test_filer import _dblclick, _get_info_lines
from test_recorded import _catalog, _choice, _open_recorded_folder
from test_recorded_resolution import (_catalog_with_suffix, _open_recorded_folder as
                                     _open_recorded_sfx_folder, dt2_suffix)

from disktracker.filer_adapter import DiskTrackerVolume


def _picker_returns(monkeypatch, result):
    class _OpenDialog:
        @staticmethod
        def getOpenFileName(*_args, **_kwargs):
            return result
    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _OpenDialog)


def _names(win):
    return [win.tree.topLevelItem(i).text(0) for i in range(win.tree.topLevelItemCount())]


def _new_volume_windows(h, folder):
    return [w for w in h.d.filer_windows if w.volume is not folder.volume]


def test_double_click_image_renamed_away_opens_via_the_picker(harness, tmp_path,
                                                              monkeypatch):
    """Generated image + catalog, image renamed away; double-click → the
    prompt appears; the picked (renamed) image → opens as that volume."""
    cat, moved = _catalog(tmp_path)
    before = cat.read_bytes()
    renamed = tmp_path / "picked-by-hand.hfs"
    moved.rename(renamed)                   # gone from where the index recorded it,
    h = harness(disks=[])                   # picked under another name
    folder = _open_recorded_folder(h, cat)
    item = folder.entries["notes.txt"]
    _picker_returns(monkeypatch, (str(renamed), ""))

    before_windows = len(h.d.filer_windows)
    _dblclick(folder, "notes.txt", h.settle)
    prompt = h.d._alerts[-1]
    assert "notes.txt" in prompt.text()     # resolution failed: prompt came up
    assert len(h.d.filer_windows) == before_windows

    _choice(prompt, "Choose Image…").click()
    h.settle()

    opened = _new_volume_windows(h, folder)
    assert len(opened) == 1
    win = opened[0]
    assert isinstance(win.volume, machfs.Volume)
    assert win.volume.name == "Alpha CD"    # the recorded disk, mounted
    assert win.path == ()
    assert "notes.txt" in _names(win)
    assert cat.read_bytes() == before       # the record is untouched


def test_picked_image_whose_volume_name_does_not_match_shows_prompt_again(harness, tmp_path,
                                                                           monkeypatch):
    cat, _ = _catalog(tmp_path)
    before = cat.read_bytes()
    bait = machfs.Volume()
    bait.name = "Not The CD"
    f2 = machfs.File()
    f2.data, f2.type, f2.creator = b"bait", b"TEXT", b"ttxt"
    f2.crdate = f2.mddate = 4000000000
    bait["decoy.txt"] = f2
    bait_path = tmp_path / "Not The CD.hfs"
    bait_path.write_bytes(bait.write(1024 * 1024))
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    before_windows = len(h.d.filer_windows)
    _picker_returns(monkeypatch, (str(bait_path), ""))

    _dblclick(folder, "notes.txt", h.settle)
    _choice(h.d._alerts[-1], "Choose Image…").click()

    assert len(h.d.filer_windows) == before_windows   # nothing mounted or opened
    prompt = h.d._alerts[-1]
    assert "notes.txt" in prompt.text()               # the prompt is shown again
    assert "Choose Image…" in [b.text() for b in prompt.buttons()]
    assert cat.read_bytes() == before


def test_picked_image_matches_recorded_basename_when_volume_names_differ(harness, tmp_path,
                                                                         monkeypatch):
    suffix = dt2_suffix(vol="Restoration CD",
                        image_rel="Restoration-CD-MSS-Vol2.iso",
                        image_mount=None,
                        classic="Macintosh HD:Images:Restoration-CD-MSS-Vol2.iso")
    cat = _catalog_with_suffix(tmp_path, suffix)
    before = cat.read_bytes()
    image = machfs.Volume()
    image.name = "Some Other Volume"        # the volume name does not match…
    f3 = machfs.File()
    f3.data, f3.type, f3.creator = b"other", b"TEXT", b"ttxt"
    f3.crdate = f3.mddate = 4000000000
    image["readme.txt"] = f3
    image_path = tmp_path / "Restoration-CD-MSS-Vol2.iso"   # …but the basename does
    image_path.write_bytes(image.write(1024 * 1024))
    h = harness(disks=[])
    folder = _open_recorded_sfx_folder(h, tmp_path, suffix)
    _picker_returns(monkeypatch, (str(image_path), ""))

    _dblclick(folder, "notes.txt", h.settle)
    _choice(h.d._alerts[-1], "Choose Image…").click()
    h.settle()

    opened = _new_volume_windows(h, folder)
    assert len(opened) == 1
    assert opened[0].volume.name == "Some Other Volume"
    assert cat.read_bytes() == before


def test_a_recorded_image_where_the_index_says_mounts_on_double_click(harness, tmp_path):
    image = machfs.Volume()
    image.name = "Alpha CD"
    f = machfs.File()
    f.data, f.type, f.creator = b"alpha", b"TEXT", b"ttxt"
    f.crdate = f.mddate = 4000000000
    image["notes.txt"] = f
    image_path = tmp_path / "Alpha CD.hfs"
    image_path.write_bytes(image.write(1024 * 1024))
    suffix = dt2_suffix(vol="Alpha CD", mount_a=str(image_path))
    cat = _catalog_with_suffix(tmp_path, suffix)
    before = cat.read_bytes()
    h = harness(disks=[])
    folder = _open_recorded_sfx_folder(h, tmp_path, suffix)

    _dblclick(folder, "notes.txt", h.settle)      # resolution succeeds: no prompt needed

    opened = _new_volume_windows(h, folder)
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Alpha CD"
    assert cat.read_bytes() == before


def test_kind_volume_resolution_opens_without_mounting(harness, tmp_path):
    vol_dir = tmp_path / "roots" / "Alpha CD"
    vol_dir.mkdir(parents=True)
    (vol_dir / "marker.txt").write_text("there")
    suffix = dt2_suffix(vol="Alpha CD", mount_a=str(vol_dir))
    cat = _catalog_with_suffix(tmp_path, suffix)
    before = cat.read_bytes()
    from macrt.macfile import HostFolder
    h = harness(disks=[])
    folder = _open_recorded_sfx_folder(h, tmp_path, suffix)

    _dblclick(folder, "notes.txt", h.settle)

    opened = _new_volume_windows(h, folder)
    assert len(opened) == 1
    assert isinstance(opened[0].volume, HostFolder)       # browsed as-is, nothing mounted
    assert Path(opened[0].volume.path) == vol_dir
    assert "marker.txt" in _names(opened[0])
    assert cat.read_bytes() == before


def test_get_info_is_unchanged_after_the_picker_mounts(harness, tmp_path, monkeypatch):
    cat, moved = _catalog(tmp_path)
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    _picker_returns(monkeypatch, (str(moved), ""))
    _dblclick(folder, "notes.txt", h.settle)
    _choice(h.d._alerts[-1], "Choose Image…").click()
    h.settle()
    text = "\n".join(_get_info_lines(h, folder, "notes.txt"))
    assert "Type: TEXT" in text


def test_catalog_bytes_unchanged_after_failed_pick(harness, tmp_path, monkeypatch):
    cat, _ = _catalog(tmp_path)
    before = cat.read_bytes()
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    garbage = tmp_path / "garbage.bin"
    garbage.write_bytes(b"definitely not an image")
    _picker_returns(monkeypatch, (str(garbage), ""))

    _dblclick(folder, "notes.txt", h.settle)
    _choice(h.d._alerts[-1], "Choose Image…").click()

    assert cat.read_bytes() == before          # nothing mounted, nothing written
    prompt = h.d._alerts[-1]
    assert "notes.txt" in prompt.text()        # the prompt is shown again
