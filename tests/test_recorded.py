"""The prompt shown when a recorded Disk Tracker item's media is not
available: insert the disk, or choose the original image (subtask 1.3).

The catalog is a read-only record: cancelling the prompt, or the file
picker, changes nothing and the .dtc stays untouched."""

import machfs
from PyQt6.QtWidgets import QMessageBox

from test_filer import _dblclick, _item, _get_info_lines
from disktracker import build_dtc
from disktracker.filer_adapter import DiskTrackerVolume


def _catalog(tmp_path, files=("notes.txt",)):
    """A catalog built from a generated HFS image, with the image itself
    moved away — the recorded media is not where the index says it is."""
    alpha = machfs.Volume()
    alpha.name = "Alpha CD"
    for name in files:
        f = machfs.File()
        f.data = b"alpha"
        f.type, f.creator = b"TEXT", b"ttxt"
        f.crdate = f.mddate = 4000000000
        alpha[name] = f
    img = tmp_path / "Alpha CD.hfs"
    img.write_bytes(alpha.write(1024 * 1024))
    cat = tmp_path / "catalog.dtc"
    cat.write_bytes(build_dtc([str(img)]))
    moved = tmp_path / "Alpha CD.hfs.moved"
    img.rename(moved)          # gone from where the index recorded it
    return cat, moved


def _open_recorded_folder(h, cat):
    win = h.d.open_filer(DiskTrackerVolume(cat))
    h.settle()
    _dblclick(win, "Alpha CD", h.settle)
    return next(w for w in h.d.filer_windows
                if w is not win and w.volume is win.volume)


def _choice(box, label):
    return next(b for b in box.buttons() if b.text() == label)


def _picker(monkeypatch, result, record=None):
    class _OpenDialog:
        @staticmethod
        def getOpenFileName(*_args, **_kwargs):
            if record is not None:
                record.append((_args, _kwargs))
            return result
    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _OpenDialog)


def test_resolution_failure_shows_the_prompt(harness, tmp_path):
    cat, _ = _catalog(tmp_path)
    before = cat.read_bytes()
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    item = folder.entries["notes.txt"]
    assert folder._resolve_recorded(item) is False      # the stub never resolves
    prompt = h.d._alerts[-1]
    assert "notes.txt" in prompt.text()                 # which item
    assert "Alpha CD" in prompt.text()                  # which disk to insert
    labels = [b.text() for b in prompt.buttons()]
    assert "Insert Disk" in labels
    assert "Choose Image…" in labels
    assert cat.read_bytes() == before                   # the record is untouched


def test_insert_disk_retries_resolution(harness, tmp_path, monkeypatch):
    cat, _ = _catalog(tmp_path)
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    item = folder.entries["notes.txt"]
    retries = []
    monkeypatch.setattr(folder, "_resolve_recorded", lambda f: retries.append(f))
    folder._recorded_unavailable(item)
    prompt = h.d._alerts[-1]
    _choice(prompt, "Insert Disk").click()
    assert retries == [item]


def test_choose_image_picks_a_file_and_hands_it_to_the_mount_stub(harness, tmp_path,
                                                                  monkeypatch):
    cat, moved = _catalog(tmp_path)
    before = cat.read_bytes()
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    item = folder.entries["notes.txt"]
    _picker(monkeypatch, (str(moved), ""), record=[])
    picked = []
    monkeypatch.setattr(folder, "_open_picked_image",
                        lambda f, path: picked.append((f, path)))
    folder._recorded_unavailable(item)
    prompt = h.d._alerts[-1]
    _choice(prompt, "Choose Image…").click()
    assert picked == [(item, str(moved))]               # 1.4's call site got the path
    assert cat.read_bytes() == before


def test_cancelling_the_picker_does_nothing(harness, tmp_path, monkeypatch):
    cat, _ = _catalog(tmp_path)
    before = cat.read_bytes()
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    item = folder.entries["notes.txt"]
    _picker(monkeypatch, ("", ""), record=[])            # cancelled
    picked = []
    monkeypatch.setattr(folder, "_open_picked_image",
                        lambda f, path: picked.append((f, path)))
    windows = len(h.d.filer_windows)
    folder._recorded_unavailable(item)
    _choice(h.d._alerts[-1], "Choose Image…").click()
    assert picked == []                                  # nothing mounted, nothing opened
    assert len(h.d.filer_windows) == windows
    assert cat.read_bytes() == before


def test_cancel_button_does_nothing(harness, tmp_path, monkeypatch):
    cat, _ = _catalog(tmp_path)
    before = cat.read_bytes()
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    item = folder.entries["notes.txt"]

    class _NoFileDialog:
        @staticmethod
        def getOpenFileName(*_args, **_kwargs):
            raise AssertionError("the picker must not open on Cancel")
    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _NoFileDialog)
    retries = []
    monkeypatch.setattr(folder, "_resolve_recorded", lambda f: retries.append(f))
    windows = len(h.d.filer_windows)
    folder._recorded_unavailable(item)
    h.d._alerts[-1].button(QMessageBox.StandardButton.Cancel).click()
    assert retries == []
    assert len(h.d.filer_windows) == windows
    assert cat.read_bytes() == before
    assert h.d._alerts == []                          # dismissed; nothing else raised


def test_double_click_on_an_item_with_the_media_missing_shows_the_prompt(harness, tmp_path,
                                                                          monkeypatch):
    """Since 1.2/1.4 a double-click attempts the recorded media (and its
    1.3 prompt) rather than saying nothing opens."""
    cat, _ = _catalog(tmp_path)
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    launched = []
    monkeypatch.setattr(h.d, "launch", lambda *a, **kw: launched.append((a, kw)))
    _dblclick(folder, "notes.txt", h.settle)
    assert launched == []
    assert any("notes.txt" in a.text() and "Disk Not Available" in a.windowTitle()
               for a in h.d._alerts)


def test_get_info_is_unchanged_for_recorded_items(harness, tmp_path):
    cat, _ = _catalog(tmp_path)
    h = harness(disks=[])
    folder = _open_recorded_folder(h, cat)
    text = "\n".join(_get_info_lines(h, folder, "notes.txt"))
    assert "Type: TEXT" in text
