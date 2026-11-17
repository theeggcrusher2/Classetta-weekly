"""The Filer window a disk image opens in when no --app names what to run."""

import machfs
import os
import pytest
import struct
import sys
import zlib
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFontMetrics
from PyQt6.QtTest import QTest

from conftest import LB, NO_MODS, Recorder
from macrt.demo import finish, open_targets, parse_args
from macrt.macfile import (ArchiveFolder, ContainerFolder, HostFolder, LazyArchiveFile,
                           MacFile, image_kind,
                           is_alias, load_volume, open_disks)
from macrt.resfork import build_fork, Resource, resPurgeable
from macrt.toolbox import constants as k
from macrt.ui import theme
from macrt.ui.filer import (ARCHIVE_ICON, FILE_MENU_ID, SPECIAL_MENU_ID, FilerOptions,
                             GetInfoWindow, _container_icon)
from macrt.ui.stdfile import DISK_ICON, DOCUMENT_ICON, FOLDER_ICON
from builders import binhex, vers
from test_files import FORK, apple_single, macbinary


def _volume(games: bool = True) -> machfs.Volume:
    v = machfs.Volume()
    v.name = "Browse"
    app = machfs.File()
    app.type, app.creator, app.rsrc = b"APPL", b"TEST", bytearray(FORK)
    v["Widget"] = app
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    v["Letter"] = doc
    if games:
        games_ = machfs.Folder()
        game = machfs.File()
        game.type, game.creator, game.rsrc = b"APPL", b"TEST", bytearray(FORK)
        games_["Game"] = game
        v["Games"] = games_
    return v


def _alias_volume() -> machfs.Volume:
    """A disk with an application, and an alias file pointing at it."""
    from macrt.toolbox import aliases as al
    from macrt.toolbox import files as fm
    v = machfs.Volume()
    v.name = "Browse"
    app = machfs.File()
    app.type, app.creator, app.data, app.rsrc = b"APPL", b"TEST", bytearray(b"CODE"), bytearray(
        FORK)
    v["Widget"] = app
    record = al.Alias(volume="Browse", parid=fm.ROOT, name="Widget",
                      type=b"APPL", creator=b"TEST")
    link = machfs.File()
    link.type, link.creator = b"alis", b"MACS"
    link.flags = 0x8000
    link.rsrc = bytearray(build_fork([Resource(b"alis", 0, data=record.pack())]))
    v["Link"] = link
    return v


def _spacesaver_blob(payload: bytes) -> bytes:
    """A minimal version 3 SpaceSaver file, fork 0 stored."""
    import struct
    hdr = bytearray(84)
    hdr[:8] = b"SIT!DFD3"
    struct.pack_into(">I", hdr, 8, 3)
    struct.pack_into(">I", hdr, 12, 84)
    struct.pack_into(">III", hdr, 20, len(payload), len(payload), 84)
    hdr[52:56] = b"TEXT"
    hdr[56:60] = b"ttxt"
    return bytes(hdr) + payload


def _superdisk_blob(payload: bytes) -> bytes:
    """A minimal SuperDisk! single-file container, data fork stored."""
    import struct
    name = b"Packed"
    head = bytearray(31)
    struct.pack_into(">I", head, 0, len(payload))
    struct.pack_into(">I", head, 4, len(payload))
    head[12:16] = b"TEXT"
    head[16:20] = b"ttxt"
    head[30] = len(name)
    head += name
    if len(head) & 1:
        head += b"\x00"
    body = struct.pack(">H", 0x8000 | (len(payload) + 2)) + payload
    return bytes(head) + body


def _pf_blob() -> bytes:
    """A minimal Aladdin Private File: the clear header and footer the
    probe reads, around an empty file the kind column never opens."""
    import struct
    return (struct.pack(">4s5HI", b"CSIT", 2, 1, 1, 5, 0, 0) + bytes(308)
            + struct.pack(">4sII", b"CSIT", 0, 0))


def _archive_kinds_volume() -> machfs.Volume:
    """A disk with a zip, a gzip, a SpaceSaver, a SuperDisk!, a Private
    File and a plain document."""
    import gzip as gz
    import io
    import zipfile
    v = machfs.Volume()
    v.name = "Kinds"
    zipbuf = io.BytesIO()
    with zipfile.ZipFile(zipbuf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("Inner.txt", b"payload")
    zipped = machfs.File()
    zipped.type, zipped.creator, zipped.data = b"TEXT", b"ttxt", bytearray(zipbuf.getvalue())
    v["Packed.zip"] = zipped
    gzipped = machfs.File()
    gzipped.type, gzipped.creator, gzipped.data = b"TEXT", b"ttxt", bytearray(gz.compress(b"x"))
    v["Packed.gz"] = gzipped
    ssaved = machfs.File()
    ssaved.type, ssaved.creator, ssaved.data = b"TEXT", b"ttxt", bytearray(
        _spacesaver_blob(b"in situ, the file keeps its name. "))
    v["Packed.sit"] = ssaved
    sdisk = machfs.File()
    sdisk.type, sdisk.creator, sdisk.data = b"TEXT", b"ttxt", bytearray(
        _superdisk_blob(b"an auto-expanding file. "))
    v["Packed.x"] = sdisk
    privf = machfs.File()
    privf.type, privf.creator, privf.data = b"TEXT", b"ttxt", bytearray(_pf_blob())
    v["Packed.pf"] = privf
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"ttxt", b"plain"
    v["Plain"] = doc
    return v


def _disk(tmp_path):
    p = tmp_path / "browse.dsk"
    p.write_bytes(_volume().write(800 * 1024))
    return p


def _icon_folder_volume() -> machfs.Volume:
    """A disk whose folder keeps its own icon: all black at 32 by 32."""
    v = machfs.Volume()
    v.name = "Custom"
    folder = machfs.Folder()
    icon = machfs.File()
    icon.rsrc = bytearray(build_fork([Resource(b"ICN#", 128, bytes([0xFF]) * 256)]))
    folder["Icon\r"] = icon
    v["Mystery"] = folder
    return v


def _n_volume() -> machfs.Volume:
    """A disk whose only file's name begins as the header's Name does."""
    v = machfs.Volume()
    v.name = "Browse"
    note = machfs.File()
    note.data = b"Hello."
    v["Nimbus"] = note
    return v


# -- what the command line asks for ---------------------------------------
def test_browse_leaves_every_application_to_the_window(tmp_path):
    apps, disks = open_disks([_disk(tmp_path)], browse=True)
    assert apps == []
    assert [v.name for v in disks] == ["Browse"]


def test_browse_still_takes_loose_files(tmp_path):
    lone = tmp_path / "Lone.bin"
    lone.write_bytes(macbinary("Lone", b"", FORK))
    apps, disks = open_disks([lone, _disk(tmp_path)], browse=True)
    assert [f.name for f in apps] == ["Lone"]
    assert len(disks) == 1


def test_without_browse_the_only_application_still_runs(tmp_path):
    v = _volume(games=False)
    p = tmp_path / "one.dsk"
    p.write_bytes(v.write(800 * 1024))
    apps, disks = open_disks([p])
    assert [f.name for f in apps] == ["Widget"]
    assert len(disks) == 1


def test_open_targets_shows_the_disk_in_a_finder_window(tmp_path):
    args = parse_args([str(_disk(tmp_path))])
    targets, disks, finder, error = open_targets(args)
    assert error is None
    assert targets == []
    assert finder == disks and len(disks) == 1


def test_open_targets_with_app_picks_the_application(tmp_path):
    args = parse_args([str(_disk(tmp_path)), "--app", "Widget"])
    targets, disks, finder, error = open_targets(args)
    assert error is None
    assert [f.name for f, _ in targets] == ["Widget"]
    assert finder == []


def test_list_keeps_the_old_pick(tmp_path):
    v = _volume(games=False)
    p = tmp_path / "one.dsk"
    p.write_bytes(v.write(800 * 1024))
    args = parse_args([str(p), "--list"])
    targets, disks, finder, error = open_targets(args)
    assert error is None
    assert targets == []
    assert finder == []
    assert len(disks) == 1


# -- the window itself ------------------------------------------------------
def _rows(win):
    return [(win.tree.topLevelItem(i).text(0), win.tree.topLevelItem(i).text(2))
            for i in range(win.tree.topLevelItemCount())]


def _item(win, name):
    return next(win.tree.topLevelItem(i) for i in range(win.tree.topLevelItemCount())
                if win.tree.topLevelItem(i).text(0) == name)


def _ink(img, y, x0, x1):
    """The first black pixel of a row between x0 and x1, or None."""
    for x in range(x0, x1):
        r, g, b, _ = img.pixelColor(x, y).getRgb()
        if r < 128 and g < 128 and b < 128:
            return x
    return None


def _label_lefts(win, name, after):
    """The x of the header's Name label and of a row of the same first letter."""
    img = win.tree.grab().toImage()
    hdr = win.tree.header()
    rows = range(hdr.geometry().top() + 1, hdr.geometry().bottom())
    head = min((x for y in rows
                if (x := _ink(img, y, after, 170)) is not None), default=None)
    rect = win.tree.visualItemRect(_item(win, name))
    y = win.tree.viewport().geometry().top() + rect.y() + rect.height() // 2
    return head, _ink(img, y, 21, 170)


def _lock_left(win):
    """The x of the header's padlock at the header's middle row."""
    img = win.tree.grab().toImage()
    hdr = win.tree.header()
    return _ink(img, hdr.geometry().top() + hdr.height() // 2, 0, 17)


def _dblclick(win, name, settle):
    """A real double-click: click, then click again on the same spot."""
    _dblclick_row(win, _item(win, name), settle)


def _dblclick_row(win, item, settle):
    """The same double-click on a row found any way — inside a folder
    of members, where _item cannot reach."""
    c = win.tree.visualItemRect(item).center()
    vp = win.tree.viewport()
    QTest.mousePress(vp, LB, NO_MODS, c)
    QTest.mouseRelease(vp, LB, NO_MODS, c)
    settle(30)
    QTest.mouseDClick(vp, LB, NO_MODS, c)
    settle(150)


def _front_the_filer(h, win):
    """Click in a Filer window's list, so its own menus come back to the bar."""
    c = win.tree.visualItemRect(_item(win, "Games")).center()
    h.click(*h.mac(win.tree.viewport(), c))
    h.settle()
    assert h.d.front_filer is win


def test_the_window_lists_the_folder(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert win.isVisible()
    assert _rows(win) == [("Games", "folder"), ("Letter", "Widget document"),
                          ("Widget", "application")]
    assert win in h.d.filer_windows


def test_the_name_column_fits_32_Ws_without_truncating(harness):
    h = harness(disks=[_n_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    fm = QFontMetrics(win.tree.font())
    assert win.tree.columnWidth(0) >= fm.horizontalAdvance("W" * 32)


def test_the_header_wears_the_lock_while_the_window_is_view_only(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert not win.tree.headerItem().icon(0).isNull()
    assert win.tree.headerItem().text(0) == "Name"      # beside it, not instead of it


def test_a_window_that_can_be_modified_would_wear_no_lock(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0], options=FilerOptions(writable=True))
    h.settle()
    assert win.tree.headerItem().icon(0).isNull()


def test_the_name_label_begins_where_the_file_names_begin(harness):
    h = harness(disks=[_n_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _lock_left(win) == 8
    head, nimbus = _label_lefts(win, "Nimbus", after=17)
    assert abs(head - nimbus) <= 2


def test_the_name_label_begins_where_the_file_names_begin_without_the_lock(harness):
    h = harness(disks=[_n_volume()])
    win = h.d.open_filer(h.d.config.disks[0], options=FilerOptions(writable=True))
    h.settle()
    assert _lock_left(win) is None
    head, nimbus = _label_lefts(win, "Nimbus", after=0)
    assert abs(head - nimbus) <= 2


# -- what the title bar wears -----------------------------------------------
def test_the_title_bar_wears_the_disk_icon_at_the_root(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert win.frame().title_icon == DISK_ICON


def test_the_title_bar_wears_the_folder_icon_inside(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0], ("Games",))
    h.settle()
    assert win.frame().title_icon == FOLDER_ICON


def test_a_folder_with_its_own_icon_wears_it_in_the_title_bar(harness):
    h = harness(disks=[_icon_folder_volume()])
    win = h.d.open_filer(h.d.config.disks[0], ("Mystery",))
    h.settle()
    assert win.frame().title_icon == ["#" * 16] * 16


def test_a_file_container_wears_what_the_file_is():
    v = machfs.Volume()
    v.name = "Files"
    packed = machfs.File()
    packed.data = b"SIT!" + bytes(6) + b"rLau"
    v["Pack.sit"] = packed
    text = machfs.File()
    text.data = b"Hello."
    v["Letter"] = text
    assert _container_icon(v, ("Pack.sit",)) == ARCHIVE_ICON
    assert _container_icon(v, ("Letter",)) == DOCUMENT_ICON


def test_a_file_wears_its_own_icon_only_when_marked_as_having_one():
    v = machfs.Volume()
    v.name = "Files"
    icon_rsrc = bytearray(build_fork([Resource(b"ICON", 128, bytes([0xFF]) * 128)]))
    marked = machfs.File()
    marked.data, marked.flags = b"Text.", 0x0400      # kHasCustomIcon
    marked.rsrc = icon_rsrc
    v["Marked"] = marked
    plain = machfs.File()
    plain.data, plain.rsrc = b"Text.", bytearray(icon_rsrc)
    v["Plain"] = plain
    assert _container_icon(v, ("Marked",)) == ["#" * 16] * 16
    assert _container_icon(v, ("Plain",)) == DOCUMENT_ICON


def test_the_alias_file_is_kind_alias(harness):
    h = harness(disks=[_alias_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _rows(win) == [("Link", "alias"), ("Widget", "application")]


def test_opening_an_alias_opens_its_original(harness, monkeypatch):
    h = harness(disks=[_alias_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw["app"].name)))
    _dblclick(win, "Link", h.settle)
    h.settle()
    assert launched == [("Widget", "Widget")]


def _alias_image(tmp_path) -> machfs.Volume:
    """A disk image with an application, and an alias file pointing at it,
    read off the image so its files are the OnDisk kind an ISO's are."""
    from macrt.toolbox import aliases as al
    from macrt.toolbox import files as fm
    v = machfs.Volume()
    v.name = "Browse"
    app = machfs.File()
    app.type, app.creator, app.data, app.rsrc = b"APPL", b"TEST", bytearray(b"CODE"), bytearray(
        FORK)
    v["Widget"] = app
    record = al.Alias(volume="Browse", parid=fm.ROOT, name="Widget",
                      type=b"APPL", creator=b"TEST")
    link = machfs.File()
    link.type, link.creator = b"alis", b"MACS"
    link.flags = 0x8000
    link.rsrc = bytearray(build_fork([Resource(b"alis", 0, data=record.pack())]))
    v["Link"] = link
    p = tmp_path / "Alias.dsk"
    p.write_bytes(v.write(size=800 * 1024))
    return load_volume(str(p))


def test_opening_an_alias_on_a_disk_image_reaches_its_original(harness, monkeypatch,
                                                              tmp_path):
    h = harness(disks=[_alias_image(tmp_path)])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert sorted((n, f.type) for n, f in win.volume.items()) == [
        ("Link", b"alis"), ("Widget", b"APPL")]
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw["app"].name)))
    _dblclick(win, "Link", h.settle)
    h.settle()
    assert launched == [("Widget", "Widget")]


def test_get_info_on_an_alias_shows_its_original(harness):
    h = harness(disks=[_alias_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    link = win.entries["Link"]
    info = GetInfoWindow(h.d, link)
    h.settle()
    text = "\n".join(info._lines)
    assert "Kind: alias" in text
    assert "Original: Browse:Widget" in text


def test_the_kind_column_names_known_archives(harness):
    h = harness(disks=[_archive_kinds_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _rows(win) == [("Packed.gz", "gzip"), ("Packed.pf", "Private File"),
                          ("Packed.sit", "SpaceSaver"),
                          ("Packed.x", "SuperDisk!"), ("Packed.zip", "ZIP"),
                          ("Plain", "document")]


def test_the_kind_column_names_known_images(harness):
    import machfs
    from macrt.macfile import MacFile
    image = machfs.Volume()
    image.name = "Tiny"
    f = MacFile("Disk", image.write(800 * 1024), b"", b"disk", b"disk")
    assert image_kind(f) == "HFS disk image"
    assert MacFile("Cat.dtc", b"x", b"", b"TEXT", b"ttxt").name and \
        image_kind(MacFile("Cat.dtc", b"x", b"", b"TEXT", b"ttxt")) == "Disk Tracker catalog"
    assert image_kind(MacFile("No", b"plain", b"", b"TEXT", b"ttxt")) is None


def test_the_same_folder_is_one_window(harness):
    h = harness(disks=[_volume()])
    volume = h.d.config.disks[0]
    win = h.d.open_filer(volume)
    assert h.d.open_filer(volume) is win
    assert h.d.filer_windows == [win]


def test_double_click_launches_the_application(harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, main, kw)))
    _dblclick(win, "Widget", h.settle)
    h.settle()
    assert [(n, kw["app"].name) for n, _, kw in launched] == [("Widget", "Widget")]
    assert win in h.d.filer_windows       # the window carries on


def test_a_document_opens_with_its_creator(harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    _dblclick(win, "Letter", h.settle)
    h.settle()
    assert [n for n, _ in launched] == ["Widget"]
    assert [d.name for d in launched[0][1]["app"].documents] == ["Letter"]


def test_double_click_a_folder_opens_its_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    _dblclick(win, "Games", h.settle)
    h.settle()
    assert len(h.d.filer_windows) == 2
    child = h.d.filer_windows[1]
    assert child.path == ("Games",)
    assert [name for name, _ in _rows(child)] == ["Game"]
    assert child.frame().title == "Games"


def test_the_close_box_closes_the_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    o = win.mac_origin()
    box = win.box_rect(4)                   # wInGoAway
    assert box is not None
    h.click(o.h + box.x() + 5, o.v + box.y() + 5)
    h.wait_until(lambda: win not in h.d.filer_windows, what="the finder to close")
    assert h.d.shutting_down                # it was all that was up: macrt quits


def test_a_second_window_survives_the_first_closing(harness):
    h = harness(disks=[_volume()])
    volume = h.d.config.disks[0]
    win = h.d.open_filer(volume)
    child = h.d.open_filer(volume, ("Games",))
    h.settle()
    child.close()                           # closing its box goes through closeEvent
    h.wait_until(lambda: child not in h.d.filer_windows, what="the folder window to close")
    assert win in h.d.filer_windows
    assert not h.d.shutting_down


def _file_menu_open_enabled(h) -> bool:
    """File > Open as the bar itself would draw it: through bar_menus."""
    for menu, _cell in h.d.global_bar.cells():
        if menu.title == "File":
            return menu.items[0].enabled
    raise AssertionError("the menu bar is showing no File menu")


def test_file_open_menu_item_enabled_state(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _file_menu_open_enabled(h) is False       # nothing is selected
    win.tree.setCurrentItem(_item(win, "Games"))
    h.settle()
    assert _file_menu_open_enabled(h) is True        # a folder is selected


def test_file_open_opens_selected_item(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Games"))
    h.settle()
    win.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 2
    assert h.d.front_filer.path == ("Games",)


def test_the_file_menu_pulls_down_and_opens(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Games"))
    h.settle()
    h.choose(h.d.global_bar, "File", 1)
    h.wait_until(lambda: len(h.d.filer_windows) == 2, what="the folder window")
    assert h.d.front_filer.path == ("Games",)


def test_the_apple_menu_opens_about_classetta(harness):
    h = harness(disks=[_volume()])
    h.d.open_filer(h.d.config.disks[0])
    h.settle()
    h.choose(h.d.global_bar, k.APPLE_MENU_TITLE, 1)
    h.wait_until(lambda: len(h.d.about_windows) == 1, what="the About window")
    assert h.d.front_filer is not None             # the Filer is still front
    h.d.about_windows[0].close()
    h.settle()
    assert h.d.about_windows == []


def test_about_classetta_appears(harness):
    h = harness(disks=[_volume()])
    h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert len(h.d.about_windows) == 0
    h.d.show_about_classetta()
    h.settle()
    assert len(h.d.about_windows) == 1
    about = h.d.about_windows[0]
    assert about.windowTitle() == "About Classetta"
    assert any("Classetta" in line for line in about._lines)
    assert any(line.startswith("Total Memory") for line in about._lines)
    assert any(line.startswith("Largest Unused Block") for line in about._lines)
    assert about._logo_icon in (theme.MAC_II_ICON, theme.MAC_PLUS_ICON)
    assert about._processes[0][0] == "System"      # the System heap is listed
    about.close()
    h.settle()


def test_about_window_can_be_dragged(harness):
    h = harness(disks=[_volume()])
    h.d.open_filer(h.d.config.disks[0])
    h.settle()
    h.d.show_about_classetta()
    h.settle()
    about = h.d.about_windows[0]
    start_pos = about.pos()
    o = about.mac_origin()
    h.press(o.h + 40, o.v + 10)
    h.move(o.h + 80, o.v + 30)
    h.release(o.h + 80, o.v + 30)
    h.settle()
    assert about.pos() != start_pos
    about.close()
    h.settle()


def test_about_window_close_box_closes_it(harness):
    h = harness(disks=[_volume()])
    h.d.open_filer(h.d.config.disks[0])
    h.settle()
    h.d.show_about_classetta()
    h.settle()
    about = h.d.about_windows[0]
    o = about.mac_origin()
    box = about.box_rect(4)                   # wInGoAway
    assert box is not None
    h.click(o.h + box.x() + 5, o.v + box.y() + 5)
    h.wait_until(lambda: about not in h.d.about_windows,
                 what="the About window to close")


def test_opening_get_info_makes_it_the_auxiliary_front_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    about = h.d.show_about_classetta()
    h.settle()
    assert h.d.front_aux_window is about
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    win.get_info()
    h.settle()
    assert h.d.front_aux_window is h.d.info_windows[0]


def test_opening_display_contents_makes_it_the_auxiliary_front_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    about = h.d.show_about_classetta()
    h.settle()
    assert h.d.front_aux_window is about
    win.tree.setCurrentItem(_item(win, "Letter"))
    h.settle()
    win.display_contents()
    h.settle()
    assert h.d.front_aux_window is h.d.viewer_windows[0]


def test_opening_about_makes_it_the_auxiliary_front_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    win.get_info()
    h.settle()
    about = h.d.show_about_classetta()
    h.settle()
    assert h.d.front_aux_window is about


def test_switching_to_an_application_drops_the_auxiliary_front_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    about = h.d.show_about_classetta()
    h.settle()
    assert h.d.front_aux_window is about
    app = h.launch(Recorder("Alpha", at=(30, 60)))
    h.wait_until(lambda: h.d.front_process is app, what="Alpha to come forward")
    assert h.d.front_aux_window is None
    h.d.close_filer(win)


def test_focusing_the_filer_drops_the_auxiliary_front_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    about = h.d.show_about_classetta()
    h.settle()
    assert h.d.front_aux_window is about
    h.d.filer_front(win)
    assert h.d.front_aux_window is None


@pytest.mark.parametrize("host", ["screen", "desktop"])
def test_control_opens_the_selection(harness, host):
    h = harness(disks=[_volume()], host=host)
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setFocus()
    win.tree.setCurrentItem(_item(win, "Games"))
    h.settle()
    QTest.keyClick(win.tree, Qt.Key.Key_O, Qt.KeyboardModifier.ControlModifier)
    h.settle()
    assert len(h.d.filer_windows) == 2
    assert h.d.front_filer.path == ("Games",)


def test_the_bar_goes_to_an_application_and_back(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert h.d.global_bar.process is h.d.filer_menus
    alpha = h.launch(Recorder("Alpha", at=(30, 60)))
    h.wait_until(lambda: h.d.front_process is alpha, what="Alpha to come forward")
    assert h.d.front_filer is None
    assert h.d.global_bar.process is alpha
    c = win.tree.visualItemRect(_item(win, "Games")).center()
    h.click(*h.mac(win.tree.viewport(), c))         # click back into the Filer
    h.settle()
    assert h.d.front_filer is win
    assert h.d.global_bar.process is h.d.filer_menus


def test_keys_typed_in_the_filer_are_not_left_for_an_application(harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    alpha = Recorder("Alpha", at=(30, 60))
    proc = h.launch(alpha)
    h.wait_until(lambda: h.d.front_process is proc, what="Alpha to come forward")
    c = win.tree.visualItemRect(_item(win, "Games")).center()
    h.click(*h.mac(win.tree.viewport(), c))         # the Filer is front again
    h.settle()
    posted = []
    post = proc.events.post

    def spy(ev):
        posted.append(ev.what)
        return post(ev)

    monkeypatch.setattr(proc.events, "post", spy)
    QTest.keyClick(win, Qt.Key.Key_A)               # a keystroke in the Filer
    h.settle()
    assert posted == []                             # Alpha behind it gets none


def test_control_w_closes_the_front_filer_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setFocus()
    QTest.keyClick(win.tree, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    h.settle()
    assert win not in h.d.filer_windows
    assert h.d.front_filer is None


def _file_menu_info_enabled(h) -> bool:
    """File > Get Info as the bar itself would draw it: through bar_menus."""
    for menu, _cell in h.d.global_bar.cells():
        if menu.title == "File":
            return menu.items[2].enabled
    raise AssertionError("the menu bar is showing no File menu")


def test_file_info_menu_item_enabled_state(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _file_menu_info_enabled(h) is False       # nothing is selected
    win.tree.setCurrentItem(_item(win, "Games"))
    h.settle()
    assert _file_menu_info_enabled(h) is True        # an item is selected


def test_control_i_opens_get_info_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setFocus()
    win.tree.setCurrentItem(_item(win, "Games"))
    h.settle()
    QTest.keyClick(win.tree, Qt.Key.Key_I, Qt.KeyboardModifier.ControlModifier)
    h.settle()
    assert len(h.d.info_windows) == 1
    assert "Games Info" in h.d.info_windows[0].windowTitle()
    h.d.info_windows[0].close()
    h.settle()
    assert h.d.info_windows == []


def test_file_menu_get_info_opens_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    h.choose(h.d.global_bar, "File", 3)
    h.wait_until(lambda: len(h.d.info_windows) == 1, what="the Get Info window")
    assert "Widget Info" in h.d.info_windows[0].windowTitle()
    h.d.info_windows[0].close()
    h.settle()
    assert h.d.info_windows == []


def test_control_w_closes_get_info_window_when_front(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    win.get_info()
    h.settle()
    assert len(h.d.info_windows) == 1
    info_win = h.d.info_windows[0]
    info_win.setFocus()
    info_win.activateWindow()
    h.settle()
    QTest.keyClick(info_win, Qt.Key.Key_W, Qt.KeyboardModifier.ControlModifier)
    h.settle()
    assert len(h.d.info_windows) == 0
    assert win in h.d.filer_windows



def test_get_info_displays_minimum_and_desired_heap(harness):
    v = _volume()
    app = machfs.File()
    app.type, app.creator = b"APPL", b"TEST"
    app.rsrc = build_fork([Resource(b"SIZE", 0, struct.pack(">HII", 0, 2000000, 1000000))])
    v["SizedApp"] = app
    h = harness(disks=[v])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "SizedApp"))
    h.settle()
    win.get_info()
    h.settle()
    assert len(h.d.info_windows) == 1
    info_win = h.d.info_windows[0]
    assert any("Minimum heap:" in line for line in info_win._lines)
    assert any("Desired heap:" in line for line in info_win._lines)
    info_win.close()
    h.settle()


def _file_menu_display_contents_enabled(h) -> bool:
    for menu, _cell in h.d.global_bar.cells():
        if menu.title == "File":
            return menu.items[3].enabled
    raise AssertionError("the menu bar is showing no File menu")


def test_file_display_contents_menu_item_enabled_state(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _file_menu_display_contents_enabled(h) is False
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    assert _file_menu_display_contents_enabled(h) is True
    win.tree.setCurrentItem(_item(win, "Letter"))
    h.settle()
    assert _file_menu_display_contents_enabled(h) is True


def test_file_menu_display_contents_opens_window(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Letter"))
    h.settle()
    h.choose(h.d.global_bar, "File", 4)
    h.wait_until(lambda: len(h.d.viewer_windows) == 1, what="the Content Viewer window")
    assert "Letter" in h.d.viewer_windows[0].windowTitle()
    h.d.viewer_windows[0].close()
    h.settle()
    assert h.d.viewer_windows == []


def test_open_host_folder(harness, tmp_path):
    folder = tmp_path / "host"
    folder.mkdir()
    (folder / "test.txt").write_bytes(b"hello")
    subfolder = folder / "sub"
    subfolder.mkdir()
    (subfolder / "inner.txt").write_bytes(b"world")

    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(folder))
    h.settle()
    assert win.windowTitle() == "host"
    assert _rows(win) == [("sub", "folder"), ("test.txt", "document")]
    win.tree.setCurrentItem(_item(win, "sub"))
    h.settle()
    win.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 2
    sub_win = h.d.filer_windows[1]
    assert sub_win.windowTitle() == "sub"
    assert _rows(sub_win) == [("inner.txt", "document")]
    sub_win.tree.setCurrentItem(_item(sub_win, "inner.txt"))
    h.settle()
    assert sub_win.selected_actions() == {"open", "info", "display_contents"}

    empty = tmp_path / "empty"
    empty.mkdir()
    h.d.open_filer(HostFolder(empty))
    h.settle()
    assert _rows(h.d.filer_windows[-1]) == []

    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "ok.txt").write_bytes(b"ok")
    (bad / "bad.bin").write_bytes(b"not a mac file")
    bad_win = h.d.open_filer(HostFolder(bad))
    h.settle()
    assert _rows(bad_win) == [("bad.bin", "document"), ("ok.txt", "document")]

    inaccessible = tmp_path / "inaccessible"
    inaccessible.mkdir()
    inaccessible.chmod(0o000)
    h.d.open_filer(HostFolder(inaccessible))
    h.settle()
    assert _rows(h.d.filer_windows[-1]) == []
    inaccessible.chmod(0o755)


def test_control_space_opens_content_viewer(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setFocus()
    win.tree.setCurrentItem(_item(win, "Letter"))
    h.settle()
    for menu, _cell in h.d.global_bar.cells():          # the item draws ⌘ over a space
        if menu.title == "File":
            assert menu.items[3].key == " "
    QTest.keyClick(win.tree, Qt.Key.Key_Space, Qt.KeyboardModifier.ControlModifier)
    h.wait_until(lambda: len(h.d.viewer_windows) == 1, what="the Content Viewer window")
    assert "Letter" in h.d.viewer_windows[0].windowTitle()
    h.d.viewer_windows[0].close()
    h.settle()
    assert h.d.viewer_windows == []


def test_content_viewer_ordinary_and_text_only_fork(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Letter"))
    h.settle()
    win.display_contents()
    h.settle()
    assert len(h.d.viewer_windows) == 1
    viewer = h.d.viewer_windows[0]
    assert "Hello" in viewer.text_edit.toPlainText()
    viewer.checkbox.setChecked(True)
    h.settle()
    assert "Hello" in viewer.text_edit.toPlainText()
    viewer.close()
    h.settle()


def test_content_viewer_resource_only_file(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    assert _file_menu_display_contents_enabled(h) is True
    win.display_contents()
    h.settle()
    viewer = h.d.viewer_windows[-1]
    rows = [viewer.list_widget.item(i).text() for i in range(viewer.list_widget.count())]
    assert not any(row.startswith("DATA") for row in rows)
    assert any("Greeting" in row for row in rows)
    greeting_row = next(i for i in range(viewer.list_widget.count())
                        if "Greeting" in viewer.list_widget.item(i).text())
    viewer.list_widget.setCurrentRow(greeting_row)
    h.settle()
    assert "05 48 65 6C 6C 6F" in viewer.text_edit.toPlainText()
    viewer.checkbox.setChecked(True)
    h.settle()
    assert "Hello" in viewer.text_edit.toPlainText()
    viewer.close()
    h.settle()
    assert h.d.viewer_windows == []


def test_content_viewer_resource_and_data_file(harness):
    v = _volume()
    mixed = machfs.File()
    mixed.type, mixed.creator = b"APPL", b"TEST"
    mixed.data = b"A data fork."
    mixed.rsrc = bytearray(FORK)
    v["Mixed"] = mixed

    h = harness(disks=[v])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Mixed"))
    h.settle()
    assert _file_menu_display_contents_enabled(h) is True
    win.display_contents()
    h.settle()
    viewer = h.d.viewer_windows[-1]
    rows = [viewer.list_widget.item(i).text() for i in range(viewer.list_widget.count())]
    assert rows[0].startswith("DATA")
    assert f"({len(b'A data fork.'):,} bytes)" in rows[0]
    assert any("Greeting" in row for row in rows)
    assert viewer.list_widget.currentRow() == 0     # DATA selected first
    assert "41 20 64 61 74 61" in viewer.text_edit.toPlainText()     # "A data"
    greeting_row = next(i for i in range(viewer.list_widget.count())
                        if "Greeting" in viewer.list_widget.item(i).text())
    viewer.list_widget.setCurrentRow(greeting_row)
    h.settle()
    assert "05 48 65 6C 6C 6F" in viewer.text_edit.toPlainText()
    viewer.close()
    h.settle()


def test_content_viewer_corrupt_resource_fork(harness):
    v = _volume()
    bad = machfs.File()
    bad.type, bad.creator = b"APPL", b"TEST"
    bad.data = b""
    bad.rsrc = b"not a resource fork"
    v["BadRes"] = bad

    h = harness(disks=[v])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "BadRes"))
    h.settle()
    win.display_contents()
    h.settle()
    viewer = h.d.viewer_windows[-1]
    assert viewer.list_widget.count() == 0
    assert "(empty)" in viewer.text_edit.toPlainText()
    viewer.close()
    h.settle()


def test_content_viewer_empty_and_unreadable_fork(harness, monkeypatch):
    v = _volume(games=False)
    empty_file = machfs.File()
    empty_file.data = b""
    v["Empty"] = empty_file

    bad_file = machfs.File()
    bad_file.data = b"some data"
    v["Bad"] = bad_file

    h = harness(disks=[v])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()

    win.tree.setCurrentItem(_item(win, "Empty"))
    h.settle()
    win.display_contents()
    h.settle()
    assert "(empty)" in h.d.viewer_windows[-1].text_edit.toPlainText()
    h.d.viewer_windows[-1].close()
    h.settle()

    bad_item = win.entries["Bad"]
    monkeypatch.setattr(bad_item.__class__, "data", property(lambda self: (_ for _ in ()).throw(RuntimeError("Corrupt"))))

    win.tree.setCurrentItem(_item(win, "Bad"))
    h.settle()
    win.display_contents()
    h.settle()
    assert "(unreadable data fork)" in h.d.viewer_windows[-1].text_edit.toPlainText()
    h.d.viewer_windows[-1].close()
    h.settle()





def test_item_icons(harness):
    v = machfs.Volume()
    # 1. Item with custom icon (marked 0x0400)
    marked = machfs.File()
    marked.data, marked.flags = b"Text.", 0x0400
    marked.rsrc = bytearray(build_fork([Resource(b"ICN#", 128, bytes([0xFF]) * 256)]))
    v["Marked"] = marked
    # 2. Item with unreadable resources
    bad = machfs.File()
    bad.data, bad.flags = b"Bad.", 0x0400
    bad.rsrc = b"corrupt"
    v["Bad"] = bad
    # 3. Item with generic icon (folder)
    v["Folder"] = machfs.Folder()
    h = harness(disks=[v])
    win = h.d.open_filer(v)
    h.settle()
    
    # Assertions
    assert not _item(win, "Marked").icon(0).isNull()
    assert not _item(win, "Bad").icon(0).isNull() # Should fall back to generic
    assert not _item(win, "Folder").icon(0).isNull()


def test_open_target_with_filer_flag(tmp_path):
    p = tmp_path / "App.bin"
    p.write_bytes(macbinary("App", b"data", FORK))
    # Default launch action: runs the app, target in targets
    args = parse_args([str(p)])
    targets, disks, filers, error = open_targets(args)
    assert error is None
    assert len(targets) == 1
    assert targets[0][0].name == "App"
    assert filers == []

    # With --filer flag on a plain file: opens its folder in a Filer window
    q = tmp_path / "Letter.txt"
    q.write_bytes(b"Hello.")
    args_filer = parse_args([str(q), "--filer"])
    targets_f, disks_f, filers_f, error_f = open_targets(args_filer)
    assert error_f is None
    assert targets_f == []
    assert len(filers_f) == 1
    assert isinstance(filers_f[0], HostFolder)
    assert filers_f[0].path == tmp_path


def test_filer_opens_container_contents(tmp_path):
    # MacBinary, BinHex and AppleSingle each decode to the file inside.
    mb = tmp_path / "App.bin"
    mb.write_bytes(macbinary("Inner", b"data", FORK))
    hqx = tmp_path / "Doc.hqx"
    hqx.write_bytes(b"From: a list\r\r" + binhex("HexDoc", b"hi", b"", b"TEXT", b"ttxt"))
    single = tmp_path / "Note.as"
    single.write_bytes(apple_single({1: b"data!", 2: FORK, 3: b"Real Name"}))
    args = parse_args([str(mb), str(hqx), str(single), "--filer"])
    targets, disks, filers, error = open_targets(args)
    assert error is None
    assert targets == []
    assert [type(v).__name__ for v in filers] == ["ContainerFolder"] * 3
    assert [v.name for v in filers] == ["App.bin", "Doc.hqx", "Note.as"]
    assert [[f.name for f in v.files()] for v in filers] == \
        [["Inner"], ["HexDoc"], ["Real Name"]]


def test_filer_reports_a_damaged_container(tmp_path):
    hqx = bytearray(binhex("Hex App", b"data", FORK))
    at = hqx.index(b":") + 40
    hqx[at] = ord("!") if hqx[at] != ord("!") else ord("#")
    p = tmp_path / "app.hqx"
    p.write_bytes(hqx)
    args = parse_args([str(p), "--filer"])
    targets, disks, filers, error = open_targets(args)
    assert targets is None and filers is None
    assert error and "BinHex" in error


def test_the_window_lists_a_containers_contents(harness, tmp_path):
    p = tmp_path / "App.bin"
    p.write_bytes(macbinary("Inner", b"data", FORK))
    args = parse_args([str(p), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    assert win.isVisible()
    assert win.windowTitle() == "App.bin"
    rows = _rows(win)
    assert [name for name, _ in rows] == ["Inner"]
    assert rows[0][1] == "application"
    assert win in h.d.filer_windows


def test_a_container_in_a_window_drills_into_its_contents(harness, tmp_path):
    core = macbinary("Core", b"payload", FORK)
    outer = tmp_path / "Outer.bin"
    outer.write_bytes(macbinary("Packet", core, FORK))
    args = parse_args([str(outer), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    assert _rows(win) == [("Packet", "application")]
    win.tree.setCurrentItem(_item(win, "Packet"))
    h.settle()
    assert win.selected_actions() == {"open", "info", "display_contents"}
    assert _file_menu_open_enabled(h) is True     # File > Open can drill into it
    win.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 2
    inner = h.d.filer_windows[1]
    assert inner.windowTitle() == "Packet"
    assert _rows(inner) == [("Core", "application")]
    assert not h.d._alerts


def test_a_damaged_nested_container_says_so_without_crashing(harness, tmp_path):
    cut = bytes(bytearray(macbinary("Core", b"payload", FORK))[:150])   # header, no forks
    outer = tmp_path / "Outer.bin"
    outer.write_bytes(macbinary("Packet", cut, FORK))
    args = parse_args([str(outer), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Packet"))
    h.settle()
    win.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 1          # no drill window
    assert h.d._alerts and "cut short" in h.d._alerts[0].text()


def test_an_unsupported_nested_item_does_not_drill(harness):
    zipped = MacFile("Packet", b"PK\x03\x04" + b"payload" * 4, FORK, b"TEXT", b"ttxt")
    h = harness(disks=[])
    win = h.d.open_filer(ContainerFolder(zipped, "Outer.bin"))
    h.settle()
    assert _rows(win) == [("Packet", "document")]
    win.tree.setCurrentItem(_item(win, "Packet"))
    h.settle()
    win.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 1          # it did not drill into the zip
    assert h.d._alerts                          # and it said why it could not open


def test_a_contained_application_launches(harness, tmp_path, monkeypatch):
    p = tmp_path / "App.bin"
    p.write_bytes(macbinary("Inner", b"payload", FORK))
    args = parse_args([str(p), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    win.tree.setCurrentItem(_item(win, "Inner"))
    h.settle()
    win.open_selected()
    h.settle()
    assert len(launched) == 1
    assert launched[0][0] == "Inner"
    assert launched[0][1]["app"].name == "Inner"


def test_a_multi_nested_application_launches(harness, tmp_path, monkeypatch):
    inner = macbinary("Core", b"payload", FORK)
    outer_bytes = macbinary("Packet", inner, FORK)
    outer = tmp_path / "Outer.bin"
    outer.write_bytes(outer_bytes)
    args = parse_args([str(outer), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win1 = h.d.open_filer(filers[0])
    h.settle()
    # Drill into Packet container
    win1.tree.setCurrentItem(_item(win1, "Packet"))
    h.settle()
    win1.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 2
    win2 = h.d.filer_windows[1]
    assert win2.windowTitle() == "Packet"
    assert _rows(win2) == [("Core", "application")]

    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    win2.tree.setCurrentItem(_item(win2, "Core"))
    h.settle()
    win2.open_selected()
    h.settle()
    assert len(launched) == 1
    assert launched[0][0] == "Core"
    assert launched[0][1]["app"].name == "Core"


# -- Special > Shutdown -----------------------------------------------------
def test_special_shutdown_closes_the_windows_before_the_qt_app(harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    special = h.d.filer_menus.menus[SPECIAL_MENU_ID]
    assert special.items[6].enabled              # Special > Shutdown is choosable
    at_quit = []
    monkeypatch.setattr(h.app, "quit", lambda: at_quit.append(win.isVisible()))
    h.choose(h.d.global_bar, "Special", 7)
    h.wait_until(lambda: h.d.shutting_down, what="the display to shut down")
    assert at_quit == [False]                    # the windows are shut, then Qt goes


def _file_menu_save_enabled(h) -> bool:
    for menu, _cell in h.d.global_bar.cells():
        if menu.title == "File":
            return menu.items[8].enabled
    raise AssertionError("the menu bar is showing no File menu")


def _save_dialog(monkeypatch, out):
    class _SaveDialog:
        @staticmethod
        def getSaveFileName(*_args, **_kwargs):
            return (str(out), "") if out is not None else ("", "")
    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _SaveDialog)


def test_file_save_menu_item_enabled_state(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _file_menu_save_enabled(h) is True


def test_file_save_catalog_from_volume(harness, tmp_path, monkeypatch):
    from disktracker import parse_dtc
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    out = tmp_path / "Browse.dtc"
    _save_dialog(monkeypatch, out)
    win.save_catalog()
    h.settle()
    items = parse_dtc(out.read_bytes())
    disk = items[0]
    assert disk.kind == "disk" and disk.name == "Browse"
    assert disk.items == len(items) - 1
    by_name = {i.name: i for i in items[1:]}
    assert by_name["Widget"].kind == "file" and by_name["Widget"].type == b"APPL"
    assert by_name["Letter"].kind == "file"
    games = by_name["Games"]
    assert games.kind == "folder" and games.items == 1
    assert games.descendants == 1 and games.size == by_name["Game"].size
    assert by_name["Game"].depth == games.depth + 1


def test_file_save_catalog_from_host_folder(harness, tmp_path, monkeypatch):
    from disktracker import parse_dtc
    folder = tmp_path / "host"
    folder.mkdir()
    (folder / "doc.txt").write_bytes(b"hello")
    sub = folder / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_bytes(b"world!")

    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(folder))
    h.settle()
    out = tmp_path / "host.dtc"
    _save_dialog(monkeypatch, out)
    win.save_catalog()
    h.settle()
    items = parse_dtc(out.read_bytes())
    assert items[0].kind == "disk" and items[0].name == "host"
    by_name = {i.name: i for i in items[1:]}
    assert by_name["doc.txt"].size == 5 and by_name["doc.txt"].created != 0
    sub_item = by_name["sub"]
    assert sub_item.kind == "folder" and sub_item.descendants == 1
    assert sub_item.size == 6 == by_name["inner.txt"].size
    assert by_name["inner.txt"].depth == sub_item.depth + 1


def test_file_save_catalog_disabled_for_disk_tracker_views(harness, tmp_path):
    from disktracker import build_dtc
    from disktracker.filer_adapter import DiskTrackerVolume
    alpha = machfs.Volume()
    alpha.name = "Alpha CD"
    f = machfs.File()
    f.data = b"alpha"
    alpha["a.txt"] = f
    img = tmp_path / "Alpha CD.hfs"
    img.write_bytes(alpha.write(1024 * 1024))
    cat = tmp_path / "catalog"
    cat.write_bytes(build_dtc([str(img)]))

    h = harness(disks=[])
    win = h.d.open_filer(DiskTrackerVolume(cat))
    h.settle()
    assert _file_menu_save_enabled(h) is False


def test_file_save_catalog_cancelled_dialog_writes_nothing(harness, tmp_path, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    _save_dialog(monkeypatch, None)             # the dialog is cancelled
    win.save_catalog()
    h.settle()
    assert list(tmp_path.iterdir()) == []


def test_file_save_catalog_from_container(harness, tmp_path, monkeypatch):
    from disktracker import parse_dtc
    p = tmp_path / "App.bin"
    p.write_bytes(macbinary("Inner", b"payload", FORK))
    args = parse_args([str(p), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    assert _file_menu_save_enabled(h) is True
    out = tmp_path / "app.dtc"
    _save_dialog(monkeypatch, out)
    win.save_catalog()
    h.settle()
    items = parse_dtc(out.read_bytes())
    assert items[0].kind == "disk" and items[0].name == "App.bin"
    assert len(items) == 2
    assert items[1].name == "Inner" and items[1].type == b"APPL"


def test_shutdown_asks_each_running_application_to_quit(harness):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    alpha = h.launch(Recorder("Alpha", at=(30, 60)))
    _front_the_filer(h, win)
    h.choose(h.d.global_bar, "Special", 7)
    h.wait_until(lambda: h.d.shutting_down, what="the display to shut down")
    assert alpha.events.killed                   # its quit was asked for
    h.wait_until(lambda: not alpha.thread.is_alive(), what="Alpha's thread to end")


def test_a_quit_that_raises_does_not_stop_the_shutdown(harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    alpha = h.launch(Recorder("Alpha", at=(30, 60)))
    beta = h.launch(Recorder("Beta", at=(30, 120)))
    _front_the_filer(h, win)

    def boom():
        raise RuntimeError("Alpha refuses to quit")

    alpha.quit = boom                            # the first application's quit fails
    order = []
    monkeypatch.setattr(h.app, "quit",
                        lambda: order.append(("app.quit", beta.events.killed)))
    h.choose(h.d.global_bar, "Special", 7)
    h.wait_until(lambda: h.d.shutting_down, what="the display to shut down")
    assert order == [("app.quit", True)]         # the rest still quit, root last
    alpha.events.kill()       # only its shutdown call raised: let it stop for teardown
    h.wait_until(lambda: not alpha.thread.is_alive() and not beta.thread.is_alive(),
                  what="both threads to end")


# -- Special > Restart ------------------------------------------------------
def test_special_restart_reruns_the_original_command_after_the_shutdown(
        harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    alpha = h.launch(Recorder("Alpha", at=(30, 60)))
    _front_the_filer(h, win)
    special = h.d.filer_menus.menus[SPECIAL_MENU_ID]
    assert special.items[5].enabled              # Special > Restart is choosable
    command = ["python3", "-m", "macrt", "Browse.dsk", "--filer"]
    monkeypatch.setattr(sys, "orig_argv", command)
    runs = []
    monkeypatch.setattr(os, "execvp", lambda prog, argv: runs.append((prog, argv)))
    h.choose(h.d.global_bar, "Special", 6)
    h.wait_until(lambda: h.d.shutting_down, what="the display to shut down")
    assert h.d.restarting
    assert runs == []                            # nothing relaunches yet
    finish(h.d, 0)
    assert runs == [("python3", command)]        # the original command, same parameters
    assert not alpha.thread.is_alive()           # after the applications have gone


def test_shutdown_leaves_the_command_alone(harness, monkeypatch):
    h = harness(disks=[_volume()])
    h.d.open_filer(h.d.config.disks[0])
    runs = []
    monkeypatch.setattr(os, "execvp", lambda prog, argv: runs.append(argv))
    h.choose(h.d.global_bar, "Special", 7)
    h.wait_until(lambda: h.d.shutting_down, what="the display to shut down")
    finish(h.d, 0)
    assert not h.d.restarting
    assert runs == []                            # only Restart runs anything again


def _bundle_volume() -> machfs.Volume:
    """A volume with an application declaring its bundle and a document
    it created: the Filer should know the document's icon and kind."""
    v = machfs.Volume()
    v.name = "SurfDisk"
    image, mask = bytes([0xF0]) * 128, b"\xff" * 128
    small, small_mask = b"\xcc" * 32, b"\xff" * 32
    def _pstring(s):
        encoded = s.encode("mac_roman")
        return bytes([len(encoded)]) + encoded

    kind = (b"WAVE" + struct.pack(">H", 0)
            + b"apnm" + _pstring("SurfSoft Works")
            + b"SWTD" + _pstring("SurfSoft Works text doc"))
    resources = [
        Resource(b"ICN#", 200, image + mask),
        Resource(b"ics#", 200, small + small_mask),
        Resource(b"FREF", 150, b"SWTD" + struct.pack(">H", 3) + b"\x00"),
        Resource(b"BNDL", 128,
                 b"WAVE" + b"\x00" * 2 + struct.pack(">H", 2)
                 + b"ICN#" + struct.pack(">H", 1) + struct.pack(">HH", 3, 200)
                 + b"FREF" + struct.pack(">H", 1) + struct.pack(">HH", 10, 150)),
        Resource(b"kind", 1000, kind),
    ]
    app = machfs.File()
    app.type, app.creator = b"APPL", b"WAVE"
    app.rsrc = build_fork(resources)
    v["SurfWorks"] = app
    doc = machfs.File()
    doc.type, doc.creator = b"SWTD", b"WAVE"
    doc.data = b"dear sir"
    v["Letter"] = doc
    lone = machfs.File()
    lone.type, lone.creator = b"TEXT", b"NOPE"
    lone.data = b"lone"
    v["Lone"] = lone
    return v


def _filer_rows(win):
    out = {}
    for i in range(win.tree.topLevelItemCount()):
        item = win.tree.topLevelItem(i)
        out[item.text(0)] = item.text(2)
    return out


def test_a_document_shows_its_apps_kind_string_and_icon(harness):
    from macrt.ui.filer import item_icon
    h = harness(disks=[_bundle_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    rows = _filer_rows(win)
    assert rows["Letter"] == "SurfSoft Works text doc"
    assert rows["Lone"] == "document"
    doc = win.entries["Letter"]
    icon = item_icon(doc)
    assert icon == ["##..##..##..##.."] * 16
    assert icon != DOCUMENT_ICON


def test_get_info_shows_the_document_kind(harness):
    h = harness(disks=[_bundle_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Letter"))
    h.settle()
    win.get_info()
    h.settle()
    assert any("Kind: SurfSoft Works text doc" in line
               for line in h.d.info_windows[0]._lines)
    for w in h.d.info_windows:
        w.close()
    h.settle()


def _system_volume(name, ftype, resources, creator=b"TEST", flags=0):
    """A volume holding one system file (a control panel, an extension,
    an accessory or a font) with the given resources in its fork."""
    v = machfs.Volume()
    v.name = name
    f = machfs.File()
    f.type, f.creator, f.flags = ftype, creator, flags
    f.rsrc = bytearray(build_fork(resources)) if resources else b""
    f.data = b"body"
    v["The Thing"] = f
    return v


def _icon_family_resources(resid, ftype, local_id):
    small = b"\xcc" * 32
    image = bytes([0xF0]) * 128
    return [
        Resource(b"ICN#", resid, image + b"\xff" * 128),
        Resource(b"ics#", resid, small + b"\xff" * 32),
        Resource(b"FREF", resid, ftype + struct.pack(">H", local_id) + b"\x00"),
        Resource(b"BNDL", resid,
                 b"TEST" + b"\x00" * 2 + struct.pack(">H", 2)
                 + b"ICN#" + struct.pack(">H", 1) + struct.pack(">hh", local_id, resid)
                 + b"FREF" + struct.pack(">H", 1) + struct.pack(">hh", 10, resid)),
    ]


def _listed_file(win, name):
    return win.entries[name]


def test_a_control_panel_wears_its_own_icon_from_its_fork():
    from macrt.ui.filer import item_icon
    v = _system_volume("Control Panels", b"cdev",
                       _icon_family_resources(-4064, b"cdev", 0))
    from macrt.macfile import list_folder
    doc = next(f for f in list_folder(v) if f.name == "The Thing")
    rows = item_icon(doc)
    assert rows is not None
    assert rows == ["##..##..##..##.."] * 16


def test_an_extension_wears_the_icon_its_bundle_declares():
    from macrt.ui.filer import item_icon
    from macrt.macfile import list_folder
    # INITs keep their family wherever they please; the bundle says where.
    v = _system_volume("Extensions", b"INIT",
                       _icon_family_resources(5000, b"INIT", 1))
    doc = next(f for f in list_folder(v) if f.name == "The Thing")
    assert item_icon(doc) == ["##..##..##..##.."] * 16


def test_a_control_panel_without_an_icon_uses_the_type_default():
    from macrt.ui.filer import item_icon, _icon_for, _generic_icon, SYSTEM_FILE_BITMAPS
    from macrt.macfile import list_folder
    v = _system_volume("Control Panels", b"cdev", [])
    doc = next(f for f in list_folder(v) if f.name == "The Thing")
    assert item_icon(doc) is None          # no icon of its own
    assert _icon_for(doc) == "control_panel"
    assert _generic_icon(doc) is SYSTEM_FILE_BITMAPS["control_panel"]


def test_a_desk_accessory_without_an_icon_uses_the_type_default():
    from macrt.ui.filer import _icon_for, _generic_icon, SYSTEM_FILE_BITMAPS
    from macrt.macfile import list_folder
    v = _system_volume("Apple Menu Items", b"dmov", [])
    doc = next(f for f in list_folder(v) if f.name == "The Thing")
    assert _icon_for(doc) == "desk_accessory"
    assert _generic_icon(doc) is SYSTEM_FILE_BITMAPS["desk_accessory"]


def test_a_font_without_an_icon_uses_the_type_default():
    from macrt.ui.filer import _icon_for, _generic_icon, SYSTEM_FILE_BITMAPS
    from macrt.macfile import list_folder
    for ftype in (b"FFIL", b"FONT"):
        v = _system_volume("Fonts", ftype, [])
        doc = next(f for f in list_folder(v) if f.name == "The Thing")
        assert _icon_for(doc) == "font"
        assert _generic_icon(doc) is SYSTEM_FILE_BITMAPS["font"]


def test_system_files_have_kind_strings(harness):
    for ftype, kind in ((b"cdev", "Control Panel"), (b"INIT", "Extension"),
                        (b"dmov", "Desk Accessory"), (b"FFIL", "Font"),
                        (b"FONT", "Font")):
        v = _system_volume("System Folder", ftype, [])
        h = harness(disks=[v])
        win = h.d.open_filer(h.d.config.disks[0])
        h.settle()
        rows = dict(_rows(win))
        assert rows["The Thing"] == kind
        win.tree.setCurrentItem(_item(win, "The Thing"))
        h.settle()
        win.get_info()
        h.settle()
        assert any(f"Kind: {kind}" in line for line in h.d.info_windows[0]._lines)
        for w in h.d.info_windows:
            w.close()
        h.settle()


def _colour_icon_family_resources(resid, ftype, local_id):
    """An icon family with its colour members too, for --depth above 2."""
    return _icon_family_resources(resid, ftype, local_id) + [
        Resource(b"ics4", resid, b"\x11" * 128),       # every pixel colour 1
        Resource(b"ics8", resid, b"\xd7" * 256),      # every pixel colour 215
        Resource(b"icl4", resid, b"\x11" * 512),
        Resource(b"icl8", resid, b"\xd7" * 1024),
    ]


def _colour_system_volume():
    return _system_volume("Control Panels", b"cdev",
                          _colour_icon_family_resources(-4064, b"cdev", 0))


def test_colour_icons_at_each_depth(harness):
    from macrt.macfile import list_folder
    from macrt.ui.filer import item_icon_qicon
    v = _colour_system_volume()
    doc = next(f for f in list_folder(v) if f.name == "The Thing")
    want = {1: QColor(0, 0, 0), 2: QColor(0, 0, 0),
            4: QColor(252, 243, 5), 8: QColor(238, 0, 0)}
    for depth, colour in want.items():
        q = item_icon_qicon(doc, depth=depth)
        assert q is not None and not q.isNull()
        img = q.pixmap(16, 16).toImage()
        assert img.pixelColor(0, 0) == colour, f"depth {depth}"


def test_get_info_shows_colour_icons_at_depth_8(harness):
    v = _colour_system_volume()
    h = harness(disks=[v], model="ii")
    win = h.d.open_filer(v)
    h.settle()
    win.tree.setCurrentItem(_item(win, "The Thing"))
    h.settle()
    win.get_info()
    h.settle()
    assert len(h.d.info_windows) == 1
    info = h.d.info_windows[0]
    assert info._own_pixmap is not None
    assert info._own_pixmap.toImage().pixelColor(0, 0) == QColor(238, 0, 0)
    assert info.frame().title_pixmap is not None
    info.close()
    h.settle()


def test_get_info_shows_the_16_colour_icon_at_depth_4(harness):
    v = _colour_system_volume()
    h = harness(disks=[v], model="ii", depth=4)
    win = h.d.open_filer(v)
    h.settle()
    win.tree.setCurrentItem(_item(win, "The Thing"))
    h.settle()
    win.get_info()
    h.settle()
    assert len(h.d.info_windows) == 1
    info = h.d.info_windows[0]
    assert info._own_pixmap is not None
    assert info._own_pixmap.toImage().pixelColor(0, 0) == QColor(252, 243, 5)
    info.close()
    h.settle()


def test_get_info_falls_back_to_bitmaps_at_depth_1(harness):
    v = _colour_system_volume()
    h = harness(disks=[v])
    win = h.d.open_filer(v)
    h.settle()
    win.tree.setCurrentItem(_item(win, "The Thing"))
    h.settle()
    win.get_info()
    h.settle()
    assert len(h.d.info_windows) == 1
    info = h.d.info_windows[0]
    assert info._own_pixmap is None
    assert info.frame().title_pixmap is None
    info.close()
    h.settle()


def test_the_apple_menu_wears_the_rainbow_at_colour_depth(harness):
    green = QColor("#61BB46").rgb() & 0xFFFFFF
    for cfg in ({"model": "ii"}, {"model": "ii", "depth": 4}):
        v = _volume()
        h = harness(disks=[v], **cfg)
        win = h.d.open_filer(v)
        h.settle()
        assert any(m.is_apple for m, _ in h.d.global_bar.cells()), cfg
        img = h.d.global_bar.image()
        found = any(img.pixelColor(x, y).rgb() & 0xFFFFFF == green
                    for y in range(img.height()) for x in range(img.width()))
        assert found, cfg


def test_the_apple_menu_stays_black_and_white_at_depth_1(harness):
    v = _volume()
    h = harness(disks=[v])
    win = h.d.open_filer(v)
    h.settle()
    img = h.d.global_bar.image()
    green = QColor("#61BB46").rgb() & 0xFFFFFF
    found = any(img.pixelColor(x, y).rgb() & 0xFFFFFF == green
                for y in range(img.height()) for x in range(img.width()))
    assert not found


def _packit(files):
    """A stored PackIt image: (name, data, rsrc, type, creator) entries."""
    out = b""
    for name, data, rsrc, ftype, creator in files:
        name_bytes = name.encode("mac_roman")
        header = bytearray(94)
        header[0] = len(name_bytes)
        header[1:64] = name_bytes.ljust(63, b"\0")
        header[64:68], header[68:72] = ftype, creator
        header[72] = 0x01                       # flags >> 8
        struct.pack_into(">II", header, 76, len(data), len(rsrc))
        struct.pack_into(">II", header, 84, 400000000, 400000100)
        out += b"PMag" + bytes(header) + data + rsrc + b"\xde\xad"
    return out + b"PEnd"


def test_an_archive_opens_as_a_listing(harness, tmp_path):
    # Any format in macextract's registry works the same way; PackIt is
    # the one that isn't MacBinary, BinHex or AppleSingle.
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt"),
                           ("Inner App", b"data two", FORK, b"APPL", b"TEST")]))
    args = parse_args([str(p), "--filer"])
    targets, disks, filers, error = open_targets(args)
    assert error is None
    assert [type(v).__name__ for v in filers] == ["ArchiveFolder"]
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    assert win.isVisible() and win.windowTitle() == "Outer.pit"
    assert _rows(win) == [("Inner App", "application"), ("Inner One", "document")]
    assert h.d.filer_windows[-1] is win


def test_a_loose_archive_shows_its_listing_by_double_click(harness, tmp_path):
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    win.tree.setCurrentItem(_item(win, "Outer.pit"))
    h.settle()
    win.open_selected()
    h.settle()
    assert [w.windowTitle() for w in h.d.filer_windows] == [tmp_path.name, "Outer.pit"]
    assert _rows(h.d.filer_windows[1]) == [("Inner One", "document")]


def test_an_archive_on_a_volume_drills_into_its_listing(harness, tmp_path):
    v = _volume()
    pit = machfs.File()
    pit.type, pit.creator = b"????", b"????"
    pit.data = bytearray(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt")]))
    v["Outer.pit"] = pit
    h = harness(disks=[v])
    win = h.d.open_filer(v)
    h.settle()
    win.tree.setCurrentItem(_item(win, "Outer.pit"))
    h.settle()
    assert win.selected_actions() == {"open", "info", "display_contents"}
    win.open_selected()
    h.settle()
    inner = h.d.filer_windows[-1]
    assert inner.windowTitle() == "Outer.pit"
    assert _rows(inner) == [("Inner One", "document")]
    assert not h.d._alerts


def test_an_archive_entry_launches(harness, tmp_path, monkeypatch):
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner App", b"data two", FORK, b"APPL", b"TEST")]))
    args = parse_args([str(p), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    win.tree.setCurrentItem(_item(win, "Inner App"))
    h.settle()
    win.open_selected()
    h.settle()
    assert [name for name, _ in launched] == ["Inner App"]


def test_a_document_in_an_archive_offers_to_extract_and_open(harness, tmp_path,
                                                             monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner App", b"data two", FORK, b"APPL", b"TEST"),
                           ("Inner One", b"data one", b"", b"TEXT", b"TEST")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.pit", h.settle)
    h.settle()
    inner = h.d.filer_windows[-1]
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    _dblclick(inner, "Inner One", h.settle)
    h.settle()
    # An offer, not the old "no application on here made it" refusal.
    assert h.d._alerts, "double-clicking the document offered nothing"
    offer = h.d._alerts[-1]
    assert "Extract the archive and open it?" in offer.text()
    assert not any("no application on here" in a.text() for a in h.d._alerts)
    dest = tmp_path / "out"
    dest.mkdir()
    _dir_dialog(monkeypatch, dest)
    offer.button(QMessageBox.StandardButton.Yes).click()
    h.settle()
    h.wait_until(lambda: launched, what="the launch after extracting")
    assert (dest / "Inner One").read_bytes() == b"data one"
    name, kw = launched[0]
    assert name == "Inner App"
    assert [d.name for d in kw["app"].documents] == ["Inner One"]
    assert not h.d._alerts


def test_declining_the_extract_offer_says_why_the_document_wont_open(harness,
                                                                    tmp_path,
                                                                    monkeypatch):
    from PyQt6.QtWidgets import QMessageBox
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.pit", h.settle)
    h.settle()
    inner = h.d.filer_windows[-1]
    _dblclick(inner, "Inner One", h.settle)
    h.settle()
    offer = h.d._alerts[-1]
    offer.button(QMessageBox.StandardButton.No).click()
    h.settle()
    assert h.d._alerts, "declining said nothing"
    refusal = h.d._alerts[-1]
    assert refusal.windowTitle() == "Could Not Open"
    assert "no application on here made it" in refusal.text()
    assert h.d.filer_windows[-1] is inner      # no window opened for it


def test_a_document_in_an_archive_opens_with_an_application_on_a_volume(harness,
                                                                       tmp_path,
                                                                       monkeypatch):
    v = _volume()
    raw = _packit([("Archived", b"payload", b"", b"TEXT", b"TEST")])
    pit = machfs.File()
    pit.type, pit.creator = b"????", b"????"
    pit.data = bytearray(raw)
    v["Outer.pit"] = pit
    h = harness(disks=[v])
    win = h.d.open_filer(v)
    h.settle()
    _dblclick(win, "Outer.pit", h.settle)
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    inner = h.d.filer_windows[-1]
    _dblclick(inner, "Archived", h.settle)
    h.settle()
    # Widget on the mounted volume made it: it opens at once, with no
    # offer to extract anything.
    assert [name for name, _ in launched] == ["Widget"]
    assert [d.name for d in launched[0][1]["app"].documents] == ["Archived"]
    assert not h.d._alerts


def test_selecting_an_archive_leaves_its_body_unread_for_the_menus(harness,
                                                                  tmp_path,
                                                                  monkeypatch):
    """The menu bar asks the selected file what it is every time it is
    drawn; with a big archive on a disk image that must read a sample,
    never the body."""
    inner = machfs.Volume()
    inner.name = "Inner"
    pit = machfs.File()
    pit.type, pit.creator = b"????", b"????"
    pit.data = bytearray(_packit([("X", b"data", b"", b"TEXT", b"TTxt")]))
    inner["Outer.pit"] = pit
    img = tmp_path / "Outer.dsk"
    img.write_bytes(inner.write(size=800 * 1024))
    h = harness(disks=[])
    win = h.d.open_filer(load_volume(str(img)))
    h.settle()
    win.tree.setCurrentItem(_item(win, "Outer.pit"))
    h.settle()

    def boom(self):
        raise AssertionError("the archive's body was read to draw the menus")

    item = win.entries["Outer.pit"]
    monkeypatch.setattr(type(item), "data", property(boom))
    menus = h.d.filer_menus.bar_menus()          # must not read the body
    file_menu = next(m for m in menus if m.id == FILE_MENU_ID)
    assert file_menu.items[9].enabled is True    # Extract… still knows it is one


def test_listing_an_archive_does_not_decode_its_members(harness, tmp_path):
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one" * 1000, b"", b"TEXT", b"TTxt")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.pit", h.settle)
    h.settle()
    inner = h.d.filer_windows[-1]
    assert _rows(inner) == [("Inner One", "document")]
    member = inner.volume.entries[0]
    assert member.forks_pending          # the Kind column asked without decoding
    assert not is_alias(member)          # identification still works: declared metadata only
    assert member.forks_pending          # still pending after the test: no fork was read for it


def test_display_contents_of_a_large_file_shows_its_head(harness, tmp_path):
    p = tmp_path / "Big.pit"
    p.write_bytes(_packit([("Big", b"x" * (1024 * 1024), b"", b"TEXT", b"TTxt")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    win.tree.setCurrentItem(_item(win, "Big.pit"))
    h.settle()
    h.choose(h.d.global_bar, "File", 4)         # Display Contents
    h.wait_until(lambda: len(h.d.viewer_windows) == 1, what="the Content Viewer")
    text = h.d.viewer_windows[0].text_edit.toPlainText()
    assert f"(showing the first 65,536 bytes of {p.stat().st_size:,})" in text
    assert len(text.splitlines()) <= 4096 + 2   # the head, not the whole fork
    assert not h.d._alerts


def test_a_damaged_archive_is_refused_by_the_command_line(tmp_path):
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt")])[:50])
    args = parse_args([str(p), "--filer"])
    targets, disks, filers, error = open_targets(args)
    assert targets is None and filers is None
    assert error and "Outer.pit" in error


def test_a_damaged_archive_in_a_window_says_so_without_crashing(harness, tmp_path):
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt")])[:50])
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    win.tree.setCurrentItem(_item(win, "Outer.pit"))
    h.settle()
    win.open_selected()
    h.settle()
    assert len(h.d.filer_windows) == 1          # no drill window
    assert h.d._alerts and "Outer.pit" in h.d._alerts[0].text()


def _dir_dialog(monkeypatch, out):
    class _DirDialog:
        @staticmethod
        def getExistingDirectory(*_args, **_kwargs):
            return str(out) if out is not None else ""
    monkeypatch.setattr("macrt.ui.filer.QFileDialog", _DirDialog)


def test_extract_menu_extracts_the_browsed_archive(harness, tmp_path, monkeypatch):
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt"),
                           ("Inner App", b"data two", FORK, b"APPL", b"TEST")]))
    args = parse_args([str(p), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win = h.d.open_filer(filers[0])
    h.settle()
    assert h.d.filer_menu_choice is not None
    dest = tmp_path / "out"
    dest.mkdir()
    _dir_dialog(monkeypatch, dest)
    h.d.filer_menu_choice(FILE_MENU_ID, 10)         # File > Extract…
    h.settle()
    assert (dest / "Inner One").read_bytes() == b"data one"
    assert (dest / "Inner App").read_bytes() == b"data two"
    assert (dest / "._Inner App").exists()          # AppleDouble: resource fork and Finder info
    assert (dest / "._Inner One").exists()          # Finder info alone pairs too


def test_extract_menu_item_enabled_state(harness, tmp_path):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()

    def extract_enabled():
        for menu, _cell in h.d.global_bar.cells():
            if menu.title == "File":
                return menu.items[9].enabled, menu.items[8].enabled
        raise AssertionError("the menu bar is showing no File menu")

    assert extract_enabled()[0] is False            # a volume with nothing selected
    p = tmp_path / "Outer.pit"
    p.write_bytes(_packit([("Inner One", b"data one", b"", b"TEXT", b"TTxt")]))
    host = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    host.tree.setCurrentItem(_item(host, "Outer.pit"))
    h.settle()
    assert extract_enabled()[0] is True             # an archive is selected
    _, _, filers, error = open_targets(parse_args([str(p), "--filer"]))
    assert error is None
    h.d.open_filer(filers[0])
    h.settle()
    extract, save = extract_enabled()
    assert extract is True                          # browsing an archive
    assert save is False                            # Save Catalog is for browse views


def test_about_classetta_shows_the_command_line_environment(harness):
    h = harness(disks=[_volume()], model="ii", depth=8, speed=1.5)
    h.d.show_about_classetta()
    h.settle()
    about = h.d.about_windows[0]
    env = dict(about._env)
    assert env["Model"] == h.d.system.model.name
    assert env["Depth"] == "8 bits"
    assert env["Speed"] == "1.5× a Mac Plus"
    assert env["Host"] == "screen"                    # the harness's host
    assert env["Sound"] in ("on", "off")
    assert env["Sticky menus"] in ("on", "classic")
    assert env["System file"] == "the built-in empty one"
    about.close()
    h.settle()


def test_view_menu_item_opens_from_resources(harness, monkeypatch):
    from macrt.viewer import viewer_main
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, main, kw)))
    win.view_selected()
    h.settle()
    assert len(launched) == 1
    assert launched[0][1] is viewer_main              # resources, not its code


def test_trace_traps_menu_item_passes_trace(harness, monkeypatch):
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_item(win, "Widget"))
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, main, kw)))
    win.trace_selected()
    h.settle()
    assert len(launched) == 1
    assert launched[0][2]["trace"] is True


def test_memory_menu_item_sets_the_window_default(harness, monkeypatch):
    from PyQt6.QtWidgets import QInputDialog
    h = harness(disks=[_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    monkeypatch.setattr(QInputDialog, "getInt",
                        staticmethod(lambda *a, **k: (512, True)))
    win.set_memory()
    assert win.options.memory == 512 * 1024
    monkeypatch.setattr(QInputDialog, "getInt",
                        staticmethod(lambda *a, **k: (0, True)))
    win.set_memory()
    assert win.options.memory is None                # 0: what the SIZE resource asks for


def test_record_sound_menu_item_starts_and_stops_recording(harness, monkeypatch, tmp_path):
    from PyQt6.QtWidgets import QFileDialog
    from macrt.m68k.sound import WaveFile
    h = harness(disks=[_volume()])
    out = tmp_path / "out.wav"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))
    h.d.toggle_record_sound()
    assert h.d.config.record_sound == str(out)
    assert any(isinstance(o, WaveFile) for o in h.d.sound)
    assert out.exists()
    h.d.toggle_record_sound()
    assert h.d.config.record_sound is None
    assert not any(isinstance(o, WaveFile) for o in h.d.sound)


def _image_volume() -> machfs.Volume:
    """A disk with a disk image on it, and an alias file pointing at
    that image — the shapes a double-click must not take for a file
    that lost its resource fork."""
    from macrt.toolbox import aliases as al
    from macrt.toolbox import files as fm
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    v = machfs.Volume()
    v.name = "Browse"
    img = machfs.File()
    img.data = inner.write(size=800 * 1024)
    v["Outer.dsk"] = img
    record = al.Alias(volume="Browse", parid=fm.ROOT, name="Outer.dsk")
    link = machfs.File()
    link.type, link.creator = b"alis", b"MACS"
    link.flags = 0x8000
    link.rsrc = bytearray(build_fork([Resource(b"alis", 0, data=record.pack())]))
    v["Link"] = link
    return v


def test_a_disk_image_in_the_filer_opens_as_a_volume(harness):
    h = harness(disks=[_image_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    _dblclick(win, "Outer.dsk", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Inner"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["Letter"]
    assert not h.d._alerts
    # mounted, so an alias into the image resolves, as for --disk
    assert any(vol.name == "Inner" for vol in h.d.system.files.volumes)


def test_a_loose_disk_image_opens_as_a_volume(harness, tmp_path):
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    (tmp_path / "Outer.dsk").write_bytes(inner.write(size=800 * 1024))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.dsk", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Inner"
    assert not h.d._alerts


def test_an_archive_on_a_lazy_volume_opens(harness, tmp_path):
    """An archive whose forks are still on the disk image — the shape
    every file on an opened ISO has — lists and opens like any other."""
    import zipfile
    z = tmp_path / "t.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("Inner", bytes(range(256)) * 40)      # spans several extents
    inner = machfs.Volume()
    inner.name = "Inner"
    inner["Thing.zip"] = (lambda f: (setattr(f, "data", z.read_bytes()), f)[1])(
        machfs.File())
    img = tmp_path / "Outer.dsk"
    img.write_bytes(inner.write(size=800 * 1024))
    h = harness(disks=[])
    win = h.d.open_filer(load_volume(str(img)))
    h.settle()
    _dblclick(win, "Thing.zip", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, ArchiveFolder)
    assert not h.d._alerts


def test_an_alias_to_a_disk_image_opens_the_image(harness):
    h = harness(disks=[_image_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    _dblclick(win, "Link", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Inner"
    assert not h.d._alerts


def test_a_corrupt_rar_archive_says_what_is_wrong(harness):
    v = machfs.Volume()
    v.name = "Browse"
    rar = machfs.File()
    rar.data = b"Rar!\x1a\x07\x00" + bytes(64)
    v["Game.rar"] = rar
    h = harness(disks=[v])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    _dblclick(win, "Game.rar", h.settle)
    h.settle()
    assert h.d._alerts
    assert "RAR" in h.d._alerts[0].text()


def test_an_alias_that_lost_its_flag_is_still_an_alias():
    from macrt.toolbox import aliases as al
    from macrt.toolbox import files as fm
    record = al.Alias(volume="Browse", parid=fm.ROOT, name="Widget",
                      type=b"APPL", creator=b"TEST")
    f = MacFile("Link", b"", build_fork([Resource(b"alis", 0, data=record.pack())]))
    assert is_alias(f)              # no flag and no forks apart: an alias all the same
    assert not is_alias(MacFile("Letter", b"Hello."))


def test_an_alias_pointing_at_an_alias_opens_the_final_original(harness, monkeypatch):
    from macrt.toolbox import aliases as al
    from macrt.toolbox import files as fm
    v = _alias_volume()
    chain = al.Alias(volume="Browse", parid=fm.ROOT, name="Link",
                     type=b"alis", creator=b"MACS")
    outer = machfs.File()
    outer.type, outer.creator = b"alis", b"MACS"
    outer.flags = 0x8000
    outer.rsrc = bytearray(build_fork([Resource(b"alis", 0, data=chain.pack())]))
    v["Chain"] = outer
    h = harness(disks=[v])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw["app"].name)))
    _dblclick(win, "Chain", h.settle)
    h.settle()
    assert launched == [("Widget", "Widget")]


def test_a_chd_disk_image_in_the_filer_opens_as_a_volume(tmp_path, harness):
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    raw = inner.write(size=800 * 1024)
    hunks = [raw[i:i + 16384] for i in range(0, len(raw), 16384)]
    (tmp_path / "Outer.chd").write_bytes(_chd_file(hunks))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.chd", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Inner"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["Letter"]
    assert not h.d._alerts


def _chd_file(hunks: list[bytes]) -> bytes:
    """A v5 zlib CHD holding the hunks, map and header per chd.h."""
    entries, blocks = [], b""
    for raw in hunks:
        block = zlib.compress(raw, 9)
        entries.append([0, len(block), 0, _crc16(raw)])
        blocks += block
    blob, lengthbits, selfbits = _encode_v5_map(entries)
    at = 124 + 16 + len(blob)              # where the blocks begin
    for e in entries:
        e[2] = at
        at += e[1] if e[0] <= 3 else 16384
    rawmap = b"".join(struct.pack(">B", t) + length.to_bytes(3, "big")
                      + offset.to_bytes(6, "big") + struct.pack(">H", crc)
                      for t, length, offset, crc in entries)
    hdr = bytearray(124)
    hdr[0:8] = b"MComprHD"
    struct.pack_into(">II", hdr, 8, 124, 5)
    struct.pack_into(">I", hdr, 16, 0x7A6C6962)      # 'zlib'
    struct.pack_into(">Q", hdr, 32, 16384 * len(hunks))
    struct.pack_into(">Q", hdr, 40, 124)
    struct.pack_into(">II", hdr, 56, 16384, 2048)
    maphdr = struct.pack(">I", len(blob)) + (124 + 16 + len(blob)).to_bytes(6, "big") \
        + struct.pack(">H", _crc16(rawmap)) + bytes([lengthbits, selfbits, 0, 0])
    return bytes(hdr) + maphdr + blob + blocks


def _crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _encode_v5_map(entries):
    """The map blob: a uniform 16-symbol Huffman tree over the types,
    then each entry's fields.  Returns it and the field widths."""
    lengthbits = max((e[1] for e in entries if e[0] <= 3), default=0).bit_length()
    selfbits = 16
    bits = []

    def write(value, n):
        for i in range(n - 1, -1, -1):
            bits.append((value >> i) & 1)

    for _ in range(16):
        write(4, 4)                          # every symbol is 4 bits
    # the type stream comes first, then the per-entry fields, as the
    # CHD map layout demands
    for e in entries:
        write(e[0], 4)
    for e in entries:
        t, length, offset, crc = e
        if t <= 3:
            write(length, lengthbits)
            write(crc, 16)
        elif t == 4:
            write(crc, 16)
        elif t == 5:
            write(offset, selfbits)
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i + 8]:
            b = (b << 1) | bit
        b <<= 8 - len(bits[i:i + 8])        # pad zeros on the right
        out.append(b)
    return bytes(out), lengthbits, selfbits


def test_a_qcow2_disk_image_in_the_filer_opens_as_a_volume(tmp_path, harness):
    from test_qcow2 import CLUSTER, _qcow2_file
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    raw = inner.write(size=800 * 1024)
    (tmp_path / "Outer.qcow2").write_bytes(
        _qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.qcow2", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Inner"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["Letter"]
    assert not h.d._alerts


def test_a_vmdk_disk_image_in_the_filer_opens_as_a_volume(tmp_path, harness):
    from test_vmdk import _grains, _v4, _volume_raw
    raw = _volume_raw()
    (tmp_path / "Outer.vmdk").write_bytes(_v4(_grains(raw), 2048))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.vmdk", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "VMDK Disk"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["readme.txt"]
    assert not h.d._alerts


def test_a_udif_disk_image_in_the_filer_opens_as_a_volume(tmp_path, harness):
    from test_dmg import _udif, _volume_raw
    raw = _volume_raw()
    (tmp_path / "Oracle.dmg").write_bytes(_udif(raw))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Oracle.dmg", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Apple Disk"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["readme.txt"]
    assert not h.d._alerts


def test_an_mdf_mds_pair_in_the_filer_opens_as_a_volume(tmp_path, harness):
    from test_mdf import _cd, _volume_raw
    mds, mdf = _cd(_volume_raw())
    (tmp_path / "ADC.mds").write_bytes(mds)
    (tmp_path / "ADC.mdf").write_bytes(mdf)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "ADC.mds", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Apple Disk"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["readme.txt"]
    assert not h.d._alerts


def test_a_dart_image_double_click_opens_the_volume(tmp_path, harness):
    from test_dart import _dart, _volume_raw
    (tmp_path / "Tour of the Macintosh").write_bytes(_dart(_volume_raw()))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Tour of the Macintosh", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Apple Disk"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["readme.txt"]
    assert not h.d._alerts


def test_an_mdf_mds_pair_in_a_zip_opens_from_either_member(tmp_path, harness):
    import zipfile
    from test_mdf import _cd, _volume_raw
    mds, mdf = _cd(_volume_raw())
    with zipfile.ZipFile(tmp_path / "pair.zip", "w") as z:
        z.writestr("ADC.mds", mds)
        z.writestr("ADC.mdf", mdf)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "pair.zip", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, ArchiveFolder)
    _dblclick(opened[0], "ADC.mds", h.settle)
    h.settle()
    vol_win = [w for w in h.d.filer_windows if w is not win and w is not opened[0]]
    assert len(vol_win) == 1
    assert vol_win[0].volume.name == "Apple Disk"
    assert not h.d._alerts
    _dblclick(opened[0], "ADC.mdf", h.settle)
    h.settle()
    vol_win = [w for w in h.d.filer_windows if w is not win and w is not opened[0]]
    assert len(vol_win) == 2
    assert all(w.volume.name == "Apple Disk" for w in vol_win)


def test_an_mdf_without_its_sidecar_says_so_in_an_alert(tmp_path, harness):
    from test_mdf import _cd, _volume_raw
    mds, mdf = _cd(_volume_raw())
    (tmp_path / "ADC.mdf").write_bytes(mdf)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "ADC.mdf", h.settle)
    h.settle()
    assert h.d._alerts and "sidecar" in h.d._alerts[0].text()
    assert [w for w in h.d.filer_windows if w is not win] == []


def test_a_macbinary_wrapped_ndif_in_the_filer_opens_as_a_volume(tmp_path, harness):
    from test_dmg import NDIF_RAW, _fork, _macbinary, _ndif, _volume_raw
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    (tmp_path / "Disk.img.bin").write_bytes(_macbinary("Disk.img", data_fork, _fork(bcem)))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Disk.img", h.settle)      # the image, as the Filer lists it
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "Apple Disk"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["readme.txt"]
    assert not h.d._alerts


def test_a_vhd_disk_image_in_the_filer_opens_as_a_volume(tmp_path, harness):
    from test_vhd import _fixed_vhd, _volume_raw
    raw = _volume_raw()
    (tmp_path / "Outer.vhd").write_bytes(_fixed_vhd(raw))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.vhd", h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows if w is not win]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == "VHD Disk"
    assert sorted(n for n, _ in opened[0].volume.items()) == ["readme.txt"]
    assert not h.d._alerts


# -- drill-down through the newly supported formats (task 28) ------

def _drill_into_an_archived_image(tmp_path, harness, member: str, body: bytes,
                                  volume: str, files: list[str]):
    """A zip holding body as member opens from the host folder, the member
    drills into its volume, and both parent windows stay open.  The
    harness used, for a caller with more to do with it."""
    import zipfile
    with zipfile.ZipFile(tmp_path / "Nest.zip", "w") as z:
        z.writestr(member, body)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Nest.zip", h.settle)
    h.settle()
    archive_win = [w for w in h.d.filer_windows if w is not win]
    assert len(archive_win) == 1
    _dblclick(archive_win[0], member, h.settle)
    h.settle()
    opened = [w for w in h.d.filer_windows
              if w is not win and w is not archive_win[0]]
    assert len(opened) == 1
    assert isinstance(opened[0].volume, machfs.Volume)
    assert opened[0].volume.name == volume
    assert sorted(n for n, _ in opened[0].volume.items()) == files
    assert not h.d._alerts
    # the archive window and the window it was opened from are not lost
    assert win.isVisible() and archive_win[0].isVisible()
    return h


def test_a_raw_hfs_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    _drill_into_an_archived_image(tmp_path, harness, "Outer.dsk",
                                  inner.write(size=800 * 1024),
                                  "Inner", ["Letter"])


def test_a_dart_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    from test_dart import _dart, _volume_raw
    _drill_into_an_archived_image(tmp_path, harness, "Tour of the Macintosh",
                                  _dart(_volume_raw()),
                                  "Apple Disk", ["readme.txt"])


def test_a_vhd_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    from test_vhd import _fixed_vhd, _volume_raw
    _drill_into_an_archived_image(tmp_path, harness, "Outer.vhd",
                                  _fixed_vhd(_volume_raw()),
                                  "VHD Disk", ["readme.txt"])


def test_a_qcow2_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    from test_qcow2 import CLUSTER, _qcow2_file
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    raw = inner.write(size=800 * 1024)
    _drill_into_an_archived_image(
        tmp_path, harness, "Outer.qcow2",
        _qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]),
        "Inner", ["Letter"])


def test_a_chd_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    raw = inner.write(size=800 * 1024)
    hunks = [raw[i:i + 16384] for i in range(0, len(raw), 16384)]
    _drill_into_an_archived_image(tmp_path, harness, "Outer.chd",
                                  _chd_file(hunks), "Inner", ["Letter"])


def test_a_vmdk_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    from test_vmdk import _grains, _v4, _volume_raw
    _drill_into_an_archived_image(tmp_path, harness, "Outer.vmdk",
                                  _v4(_grains(_volume_raw()), 2048),
                                  "VMDK Disk", ["readme.txt"])


def test_a_udif_image_inside_an_archive_opens_as_a_volume(tmp_path, harness):
    from test_dmg import _udif, _volume_raw
    _drill_into_an_archived_image(tmp_path, harness, "Oracle.dmg",
                                  _udif(_volume_raw()),
                                  "Apple Disk", ["readme.txt"])


def test_a_damaged_image_inside_an_archive_says_so_and_keeps_its_parents(
        tmp_path, harness):
    import zipfile
    with zipfile.ZipFile(tmp_path / "Nest.zip", "w") as z:
        # the .vhd extension says what it is; the body under the cookie is
        # where the damage shows
        z.writestr("Broken.vhd", b"conectix" + b"\x00" * 400)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Nest.zip", h.settle)
    h.settle()
    archive_win = [w for w in h.d.filer_windows if w is not win]
    assert len(archive_win) == 1
    _dblclick(archive_win[0], "Broken.vhd", h.settle)
    h.settle()
    assert h.d._alerts and "damaged" in h.d._alerts[0].text()
    # no volume window opened, and both parents are still up
    assert [w for w in h.d.filer_windows
            if w is not win and w is not archive_win[0]] == []
    assert win.isVisible() and archive_win[0].isVisible()


def test_an_archive_inside_an_archive_drills_into_its_listing(tmp_path, harness):
    import io
    import zipfile
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, "w") as z:
        z.writestr("Note.txt", b"hello")
    with zipfile.ZipFile(tmp_path / "Nest.zip", "w") as z:
        z.writestr("Inner.zip", inner.getvalue())
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Nest.zip", h.settle)
    h.settle()
    outer_win = [w for w in h.d.filer_windows if w is not win]
    assert len(outer_win) == 1
    _dblclick(outer_win[0], "Inner.zip", h.settle)
    h.settle()
    inner_wins = [w for w in h.d.filer_windows
                  if w is not win and w is not outer_win[0]]
    assert len(inner_wins) == 1
    assert isinstance(inner_wins[0].volume, ArchiveFolder)
    assert [f.name for f in inner_wins[0].volume.files()] == ["Note.txt"]
    assert not h.d._alerts
    assert win.isVisible() and outer_win[0].isVisible()


def test_an_image_inside_an_image_drills_down(tmp_path, harness):
    """Two volumes deep: the host folder's image opens a volume window,
    whose own image opens the next one."""
    inner = machfs.Volume()
    inner.name = "Inner"
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"Hello."
    inner["Letter"] = doc
    outer = machfs.Volume()
    outer.name = "Outer"
    held = machfs.File()
    held.data = inner.write(size=800 * 1024)
    outer["Inner.dsk"] = held
    (tmp_path / "Outer.dsk").write_bytes(outer.write(size=2 * 1024 * 1024))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.dsk", h.settle)
    h.settle()
    mid_wins = [w for w in h.d.filer_windows if w is not win]
    assert len(mid_wins) == 1
    assert mid_wins[0].volume.name == "Outer"
    _dblclick(mid_wins[0], "Inner.dsk", h.settle)
    h.settle()
    deep_wins = [w for w in h.d.filer_windows
                 if w is not win and w is not mid_wins[0]]
    assert len(deep_wins) == 1
    assert isinstance(deep_wins[0].volume, machfs.Volume)
    assert deep_wins[0].volume.name == "Inner"
    assert sorted(n for n, _ in deep_wins[0].volume.items()) == ["Letter"]
    assert not h.d._alerts
    assert win.isVisible() and mid_wins[0].isVisible()


# -- launching at depth (task 29) --------------------------------------

def _macbinary_holding_zip(tmp_path, members: dict) -> None:
    """Outer.bin in tmp_path: a MacBinary whose data fork is a zip
    holding each name's bytes, so a drill goes encoding → archive."""
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, body in members.items():
            z.writestr(name, body)
    (tmp_path / "Outer.bin").write_bytes(
        macbinary("Nest.zip", buf.getvalue(), FORK))


def test_an_application_inside_an_archive_launches(harness, tmp_path,
                                                  monkeypatch):
    (tmp_path / "Nest.pit").write_bytes(
        _packit([("Hello", b"code", FORK, b"APPL", b"????")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    _dblclick(win, "Nest.pit", h.settle)          # the archive's window
    h.settle()
    archive_win = [w for w in h.d.filer_windows if w is not win]
    assert len(archive_win) == 1
    _dblclick(archive_win[0], "Hello", h.settle)
    h.settle()
    assert len(launched) == 1
    assert launched[0][0] == "Hello"
    assert launched[0][1]["app"].name == "Hello"
    assert not h.d._alerts
    assert win.isVisible() and archive_win[0].isVisible()


def test_an_application_three_container_levels_deep_launches(harness, tmp_path,
                                                            monkeypatch):
    """A MacBinary encoding a zip encoding a disk image holding the
    application: an encoding, an archive and a volume deep, then launch."""
    inner = machfs.Volume()
    inner.name = "Inner"
    app = machfs.File()
    app.type, app.data, app.rsrc = b"APPL", b"code", FORK
    inner["Hello"] = app
    _macbinary_holding_zip(tmp_path, {"Disk.dsk": inner.write(size=800 * 1024)})
    args = parse_args([str(tmp_path / "Outer.bin"), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    launched = []
    monkeypatch.setattr(h.d, "launch",
                        lambda name, main, **kw: launched.append((name, kw)))
    win1 = h.d.open_filer(filers[0])              # the encoding's window
    h.settle()
    _dblclick(win1, "Nest.zip", h.settle)          # ... to the archive
    h.settle()
    win2 = [w for w in h.d.filer_windows if w is not win1]
    assert len(win2) == 1
    assert isinstance(win2[0].volume, ArchiveFolder)
    _dblclick(win2[0], "Disk.dsk", h.settle)       # ... to the volume
    h.settle()
    win3 = [w for w in h.d.filer_windows
            if w is not win1 and w is not win2[0]]
    assert len(win3) == 1
    assert isinstance(win3[0].volume, machfs.Volume)
    assert win3[0].volume.name == "Inner"
    _dblclick(win3[0], "Hello", h.settle)          # ... and launch
    h.settle()
    assert len(launched) == 1
    assert launched[0][0] == "Hello"
    assert launched[0][1]["app"].name == "Hello"
    assert not h.d._alerts
    assert all(w.isVisible() for w in (win1, win2[0], win3[0]))


def test_a_failure_three_levels_deep_names_the_item_that_failed(harness,
                                                                tmp_path):
    _macbinary_holding_zip(tmp_path,
                           {"Broken.vhd": b"conectix" + b"\x00" * 400})
    args = parse_args([str(tmp_path / "Outer.bin"), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    win1 = h.d.open_filer(filers[0])
    h.settle()
    _dblclick(win1, "Nest.zip", h.settle)
    h.settle()
    win2 = [w for w in h.d.filer_windows if w is not win1]
    assert len(win2) == 1
    _dblclick(win2[0], "Broken.vhd", h.settle)
    h.settle()
    # the alert says which item, three levels down, not just that
    # something failed
    assert h.d._alerts and "Broken.vhd" in h.d._alerts[0].text()
    assert [w for w in h.d.filer_windows
            if w is not win1 and w is not win2[0]] == []
    assert win1.isVisible() and win2[0].isVisible()


# -- version resources in Get Info (task 30) --------------------------

def _versioned_volume() -> machfs.Volume:
    """A volume whose files carry 'vers' resources in every shape the
    Get Info window meets: both IDs, one, none, and two broken."""
    v = machfs.Volume()
    v.name = "Browse"
    both = machfs.File()
    both.type, both.creator = b"APPL", b"TEST"
    both.rsrc = bytearray(build_fork([
        Resource(b"vers", 1, vers("1.0", "1.0, a test app")),
        Resource(b"vers", 2, vers("1.0", "Second edition, for System 7"))]))
    v["Versioned"] = both
    one = machfs.File()
    one.type, one.creator = b"APPL", b"TEST"
    one.rsrc = bytearray(build_fork([Resource(b"vers", 1, vers("2.1", "Build 7"))]))
    v["Single"] = one
    plain = machfs.File()
    plain.type, plain.creator = b"TEXT", b"TEST"
    plain.data = b"Hello."
    v["Plain"] = plain
    cut = machfs.File()
    cut.type, cut.creator = b"APPL", b"TEST"
    cut.rsrc = bytearray(build_fork([Resource(b"vers", 1, b"\x01\x00")]))
    v["Cut Short"] = cut
    junk = machfs.File()
    junk.type, junk.creator = b"APPL", b"TEST"
    junk.rsrc = bytearray(b"not a resource fork")
    v["Junk"] = junk
    return v


def _get_info_lines(h, win, name) -> list[str]:
    """Get Info opened for name, its lines, and the window closed
    again so the next call starts clean."""
    win.tree.setCurrentItem(_item(win, name))
    h.settle()
    win.get_info()
    h.settle()
    info = h.d.info_windows[-1]
    lines = list(info._lines)
    info.close()
    h.settle()
    return lines


def test_get_info_shows_both_version_messages(harness):
    h = harness(disks=[_versioned_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    lines = _get_info_lines(h, win, "Versioned")
    assert lines[0] == "Name: Versioned"
    # version resource 2's message sits directly under the file's name
    assert lines[1] == "Second edition, for System 7"
    assert lines[2].startswith("Kind:")
    # version resource 1's message is the window's version field
    assert "Version: 1.0, a test app" in lines


def test_get_info_shows_only_the_version_field_for_one_version_resource(harness):
    h = harness(disks=[_versioned_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    lines = _get_info_lines(h, win, "Single")
    assert lines[1].startswith("Kind:")          # no message under the name
    assert "Version: Build 7" in lines


def test_a_file_without_a_version_resource_gets_the_same_window(harness):
    h = harness(disks=[_versioned_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    lines = _get_info_lines(h, win, "Plain")
    assert not any(line.startswith("Version:") for line in lines)
    assert lines[1].startswith("Kind:")
    # and the window is the size it has always been
    win.tree.setCurrentItem(_item(win, "Plain"))
    h.settle()
    win.get_info()
    h.settle()
    assert h.d.info_windows[-1]._content_size == (320, 236)
    h.d.info_windows[-1].close()
    h.settle()


def test_a_malformed_version_resource_never_crashes_get_info(harness):
    h = harness(disks=[_versioned_volume()])
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    for name in ("Cut Short", "Junk"):
        lines = _get_info_lines(h, win, name)
        assert lines[0] == f"Name: {name}"
        assert not any(line.startswith("Version:") for line in lines)
        assert lines[1].startswith("Kind:")
    assert not h.d._alerts


def test_a_version_resource_in_a_disk_image_file_shows_in_get_info(tmp_path,
                                                                   harness):
    inner = _versioned_volume()
    (tmp_path / "Outer.dsk").write_bytes(inner.write(size=800 * 1024))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Outer.dsk", h.settle)
    h.settle()
    image_wins = [w for w in h.d.filer_windows if w is not win]
    assert len(image_wins) == 1
    lines = _get_info_lines(h, image_wins[0], "Versioned")
    assert lines[1] == "Second edition, for System 7"
    assert "Version: 1.0, a test app" in lines


def test_a_version_resource_in_an_archive_member_shows_when_get_info_reads_it(
        harness, tmp_path):
    fork = build_fork([
        Resource(b"vers", 1, vers("3.2", "Third pass")),
        Resource(b"vers", 2, vers("3.2", "Third pass, inside an archive"))])
    (tmp_path / "Packed.pit").write_bytes(
        _packit([("Versioned", b"code", fork, b"TEXT", b"TEST")]))
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    _dblclick(win, "Packed.pit", h.settle)
    h.settle()
    archive_win = [w for w in h.d.filer_windows if w is not win]
    assert len(archive_win) == 1
    member = archive_win[0].entries["Versioned"]
    assert isinstance(member, LazyArchiveFile) and member.forks_pending
    # a window built without decoding: no version lines, fork untouched
    pending = GetInfoWindow(h.d, member)
    assert pending._lines[0] == "Name: Versioned"
    assert not any(line.startswith("Version:") for line in pending._lines)
    assert member.forks_pending
    pending.close()
    # through the menu: Get Info decodes the member first, so both show
    lines = _get_info_lines(h, archive_win[0], "Versioned")
    assert lines[1] == "Third pass, inside an archive"
    assert "Version: Third pass" in lines
    assert not member.forks_pending


# -- archive contents as nested folders (task 31) -----------------------

def _tree_zip(tmp_path) -> None:
    """Tree.zip: a plain member, and members under folders two deep."""
    import zipfile
    with zipfile.ZipFile(tmp_path / "Tree.zip", "w") as z:
        z.writestr("Top", b"top")
        z.writestr("Docs/Letter", b"letter")
        z.writestr("Docs/Deep/Note", b"note")


def _archive_window(h, win, archive: str):
    """The Filer window a double-click on archive in win opens — a new
    one, or the one already open on that folder, which the Filer brings
    forward instead, as for a folder of a disk image's."""
    before = set(map(id, h.d.filer_windows))
    _dblclick(win, archive, h.settle)
    h.settle()
    new = [w for w in h.d.filer_windows if id(w) not in before]
    if new:
        assert len(new) == 1
        return new[0]
    same = [w for w in h.d.filer_windows
            if w is not win and w.volume is win.volume]
    assert same, "the double-click opened no window"
    return same[-1]


def test_an_archive_with_paths_lists_folders_instead_of_flat_paths(tmp_path,
                                                                   harness):
    _tree_zip(tmp_path)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    arch = _archive_window(h, win, "Tree.zip")
    # folders first at the top, then the plain member: no unix paths
    assert [(name, kind) for name, kind in _rows(arch)] == \
        [("Docs", "folder"), ("Top", "document")]
    assert all("/" not in name for name, _ in _rows(arch))
    # each row carries the member itself, under its whole raw name
    assert _item(arch, "Top").data(0, Qt.ItemDataRole.UserRole).name == "Top"
    assert arch.entries["Top"].name == "Top"
    # a folder of members opens as a window of its own, over the path
    docs = _item(arch, "Docs")
    docs_win = _archive_window(h, arch, "Docs")
    assert [(name, kind) for name, kind in _rows(docs_win)] == \
        [("Deep", "folder"), ("Letter", "document")]
    assert docs_win.volume is arch.volume and docs_win.path == ("Docs",)
    deep_win = _archive_window(h, docs_win, "Deep")
    assert [name for name, _ in _rows(deep_win)] == ["Note"]
    assert deep_win.path == ("Docs", "Deep")
    note = _item(deep_win, "Note")
    assert note.data(0, Qt.ItemDataRole.UserRole).name == "Note"
    assert deep_win.entries["Note"].name == "Note"
    assert not h.d._alerts


def test_double_clicking_a_folder_of_members_opens_its_own_window(tmp_path,
                                                                   harness):
    _tree_zip(tmp_path)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    arch = _archive_window(h, win, "Tree.zip")
    docs = _item(arch, "Docs")
    docs_win = _archive_window(h, arch, "Docs")
    assert docs_win is not arch
    _dblclick_row(arch, docs, h.settle)              # already open: brings it forward
    assert [w for w in h.d.filer_windows
            if w.volume is arch.volume and w.path == ("Docs",)] == [docs_win]
    assert [(name, kind) for name, kind in _rows(docs_win)] == \
        [("Deep", "folder"), ("Letter", "document")]
    assert not h.d._alerts


def test_a_container_inside_a_folder_of_members_drills(tmp_path, harness):
    import zipfile
    packed = _packit([("Inside", b"payload", b"", b"TEXT", b"TEST")])
    with zipfile.ZipFile(tmp_path / "Tree.zip", "w") as z:
        z.writestr("Sub/Packed.pit", packed)
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    arch = _archive_window(h, win, "Tree.zip")
    sub = _archive_window(h, arch, "Sub")          # the folder's own window
    assert sub.path == ("Sub",)
    assert _rows(sub) == [("Packed.pit", "document")]
    _dblclick_row(sub, _item(sub, "Packed.pit"), h.settle)  # ... then drills
    h.settle()
    inner = [w for w in h.d.filer_windows if w is not win and w is not arch
             and w is not sub]
    assert len(inner) == 1
    assert _rows(inner[0]) == [("Inside", "document")]
    assert not h.d._alerts
    assert win.isVisible() and arch.isVisible() and inner[0].isVisible()


def test_get_info_reads_a_member_inside_a_folder(tmp_path, harness):
    import zipfile
    with zipfile.ZipFile(tmp_path / "Tree.zip", "w") as z:
        z.writestr("Docs/Letter", b"letter")
    h = harness(disks=[])
    win = h.d.open_filer(HostFolder(tmp_path))
    h.settle()
    arch = _archive_window(h, win, "Tree.zip")
    docs_win = _archive_window(h, arch, "Docs")
    docs_win.tree.setCurrentItem(_item(docs_win, "Letter"))
    h.settle()
    docs_win.get_info()
    h.settle()
    assert h.d.info_windows[-1]._lines[0] == "Name: Letter"
    h.d.info_windows[-1].close()
    h.settle()


def test_a_member_that_would_escape_shows_inside_and_extracts_inside(
        tmp_path, harness, monkeypatch):
    import zipfile
    with zipfile.ZipFile(tmp_path / "Escape.zip", "w") as z:
        z.writestr("../evil.txt", b"evil")
        z.writestr("/abs.txt", b"absolute")
        z.writestr("good/a.txt", b"kept")
    args = parse_args([str(tmp_path / "Escape.zip"), "--filer"])
    _, _, filers, error = open_targets(args)
    assert error is None
    h = harness(disks=[])
    arch = h.d.open_filer(filers[0])
    h.settle()
    # the sanitiser's names draw; ".." never appears as a folder
    assert [name for name, _ in _rows(arch)] == ["good", "abs.txt", "evil.txt"]
    assert all("/" not in name for name, _ in _rows(arch))
    # the members keep their raw names under it
    assert arch.entries["../evil.txt"].name == "../evil.txt"
    assert arch.entries["/abs.txt"].name == "/abs.txt"
    dest = tmp_path / "out"
    dest.mkdir()
    _dir_dialog(monkeypatch, dest)
    h.d.filer_menu_choice(FILE_MENU_ID, 10)         # File > Extract…
    h.settle()
    assert (dest / "evil.txt").read_bytes() == b"evil"
    assert (dest / "abs.txt").read_bytes() == b"absolute"
    assert (dest / "good" / "a.txt").read_bytes() == b"kept"
    # nothing was written outside the destination: the escape stayed refused
    assert sorted(p.name for p in tmp_path.iterdir()) == \
        ["Escape.zip", "out"]
