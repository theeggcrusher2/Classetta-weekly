"""The in-repo colour icons: well-formed art, the module's caching, and
the chrome surface that hands them out at depth > 2 — the Filer
window's title (task 5.2) and the About Classetta app list's System row
(task 5.3; the large machine logo gets its own as 5.4 lands)."""

import machfs
import pytest
from PyQt6.QtGui import QImage

from macrt.resfork import Resource, build_fork
from macrt.ui import filer as filer_mod
from macrt.ui import icons
from macrt.ui.filer import _container_pixmap


@pytest.fixture(autouse=True)
def _app(qapp):
    """QPixmap needs a GUI application; the art tests otherwise don't care."""
    return qapp


def test_every_icon_is_16_rows_by_16_columns_in_palette():
    for kind, rows in icons.ART.items():
        assert icons._check(rows) == rows          # raises on bad art
    for kind in icons.ART:
        image = icons.image(kind)
        assert (image.width(), image.height()) == (16, 16)
        assert icons.pixmap(kind).width() == 16


def test_pixmap_and_image_are_cached():
    for kind in icons.ART:
        assert icons.pixmap(kind) is icons.pixmap(kind)
        assert icons.image(kind) is icons.image(kind)


def _colours(pm):
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    return tuple(img.pixelColor(x, y).name()
                 for y in range(img.height()) for x in range(img.width()))


def _volume() -> machfs.Volume:
    v = machfs.Volume()
    v.name = 'Browse'
    return v


def test_a_plain_volume_picks_the_disk_stand_in():
    assert _container_pixmap(_volume(), (), 8) is icons.pixmap('disk')


def test_a_plain_folder_picks_the_folder_stand_in():
    v = _volume()
    v['Docs'] = machfs.Folder()
    assert _container_pixmap(v, ('Docs',), 8) is icons.pixmap('folder')


def _icon_file(v: machfs.Volume) -> None:
    rsrc = build_fork([Resource(b'ics#', 128, b'\xff' * 64),
                       Resource(b'ics8', 128, b'\xd7' * 256)])
    icn = machfs.File()
    icn.type, icn.creator = b'ICON', b'mcrt'
    icn.data = b''
    icn.rsrc = rsrc
    v['Icon\r'] = icn


def test_a_volume_with_its_own_colour_icon_uses_it():
    v = _volume()
    _icon_file(v)
    pm = _container_pixmap(v, (), 8)
    assert pm is not None and pm.width() == 16
    assert _colours(pm) != _colours(icons.pixmap('disk'))


def test_a_folder_with_its_own_colour_icon_uses_it():
    v = _volume()
    folder = machfs.Folder()
    v['Docs'] = folder
    _icon_file(folder)
    pm = _container_pixmap(v, ('Docs',), 8)
    assert pm is not None and pm.width() == 16
    assert _colours(pm) != _colours(icons.pixmap('folder'))


def test_a_disk_tracker_catalog_picks_the_catalog_stand_in(tmp_path):
    from disktracker import Item, write_dtc
    from disktracker.filer_adapter import DiskTrackerVolume
    dtc = tmp_path / 'index.dtc'
    dtc.write_bytes(write_dtc([
        Item(kind='disk', name='Indexed', created=1000000, modified=2000000),
        Item(kind='file', name='readme.txt', depth=1, size=5,
             type=b'TEXT', creator=b'ttxt'),
    ]))
    assert _container_pixmap(DiskTrackerVolume(dtc), (), 8) is icons.pixmap('catalog')


def test_an_unbuildable_container_icon_falls_back_to_1bit(monkeypatch):
    monkeypatch.setattr(filer_mod.icons, "pixmap",
                        lambda kind: (_ for _ in ()).throw(RuntimeError("no art")))
    assert _container_pixmap(_volume(), (), 8) is None


def test_the_filer_title_hands_chrome_a_pixmap_at_depth_8(qapp, harness, monkeypatch):
    v = _volume()
    h = harness(menu_bar="global", disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    win = h.d.open_filer(h.d.config.disks[0])
    frame = win.frame()
    assert frame.title_pixmap is not None
    assert frame.title_pixmap.width() == 16
    h.d.close_filer(win)


def test_the_filer_title_keeps_1bit_at_depth_1(qapp, harness, monkeypatch):
    v = _volume()
    h = harness(menu_bar="global", disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 1)
    win = h.d.open_filer(h.d.config.disks[0])
    assert win.frame().title_pixmap is None
    h.d.close_filer(win)


def _system_disk(colour: bool) -> machfs.Volume:
    """A disk whose top level holds a System file whose fork either
    carries a colour family or holds no icon at all."""
    v = machfs.Volume()
    v.name = 'Boot'
    f = machfs.File()
    f.type, f.creator, f.flags = b'zsys', b'macs', 0
    f.data = b'body'
    f.rsrc = bytearray(build_fork([
        Resource(b'ics#', 128, b'\xcc' * 32 + b'\xff' * 32),
        Resource(b'ics8', 128, b'\xd7' * 256)])) if colour else bytearray()
    v['System'] = f
    return v


def _about(h):
    about = h.d.show_about_classetta()
    h.settle()
    assert about._processes[0][0] == "System"
    return about


def test_the_about_system_row_is_the_stand_in_at_depth_8_without_a_system_file(qapp, harness, monkeypatch):
    v = _volume()                                   # the built-in empty System
    h = harness(disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    about = _about(h)
    assert about._processes[0][4] is icons.pixmap('app')
    about.close()
    h.settle()


def test_the_about_system_row_uses_the_running_system_files_own_icon_at_depth_8(qapp, harness, monkeypatch):
    v = _system_disk(colour=True)
    h = harness(disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    about = _about(h)
    row_pixmap = about._processes[0][4]
    assert row_pixmap is not None and row_pixmap.width() == 16
    assert _colours(row_pixmap) != _colours(icons.pixmap('app'))
    about.close()
    h.settle()


def test_the_about_system_row_falls_back_to_the_stand_in_at_depth_8_without_a_colour_icon(qapp, harness, monkeypatch):
    v = _system_disk(colour=False)
    h = harness(disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    about = _about(h)
    assert about._processes[0][4] is icons.pixmap('app')
    about.close()
    h.settle()


def test_the_about_system_row_keeps_1bit_at_depth_1(qapp, harness, monkeypatch):
    v = _system_disk(colour=True)
    h = harness(disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 1)
    about = _about(h)
    assert about._processes[0][4] is None
    about.close()
    h.settle()
