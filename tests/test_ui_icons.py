"""The in-repo colour icons: well-formed art, the module's caching, and
the chrome surface that hands them out at depth > 2 — the Filer
window's title (task 5.2; the other surfaces get their own as 5.3 and
5.4 land)."""

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
