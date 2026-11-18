"""The in-repo colour icons: well-formed art, the module's caching, and
the chrome surface that hands them out at depth > 2 — the Filer
window's title (task 5.2), the About Classetta app list's System row
(task 5.3) and the About window's machine logo (task 5.4)."""

import machfs
import pytest
from PyQt6.QtGui import QImage

from macrt.resfork import Resource, build_fork
from macrt.ui import filer as filer_mod
from macrt.ui import icons, theme
from macrt.ui.filer import _container_pixmap
from macrt.ui.stdfile import icon, standin_icon


@pytest.fixture(autouse=True)
def _app(qapp):
    """QPixmap needs a GUI application; the art tests otherwise don't care."""
    return qapp


def test_every_icon_is_square_art_in_palette():
    for kind, rows in icons.ART.items():
        assert icons._check(rows) == rows          # raises on bad art
        assert len({len(row) for row in rows}) == 1 and len(rows) == len(rows[0])
    for kind, rows in icons.ART.items():
        image = icons.image(kind)
        assert (image.width(), image.height()) == (len(rows), len(rows))
        assert icons.pixmap(kind).width() == len(rows)


def test_pixmap_and_image_are_cached():
    for kind in icons.ART:
        assert icons.pixmap(kind) is icons.pixmap(kind)
        assert icons.image(kind) is icons.image(kind)


def test_every_stand_in_has_large_art():
    assert set(icons.LARGE) == {"app", "archive", "catalog", "disk", "document",
                                "folder", "control_panel", "extension",
                                "desk_accessory", "font", "alias"}


def test_every_large_icon_is_square_art_in_palette():
    for kind, rows in icons.LARGE.items():
        assert icons._check(rows) == rows          # raises on bad art
        assert len({len(row) for row in rows}) == 1 and len(rows) == 32
        image = icons.image_large(kind)
        assert (image.width(), image.height()) == (32, 32)
        assert icons.pixmap_large(kind).width() == 32


def test_large_pixmap_and_image_are_cached():
    for kind in icons.LARGE:
        assert icons.pixmap_large(kind) is icons.pixmap_large(kind)
        assert icons.image_large(kind) is icons.image_large(kind)


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


def test_the_about_logo_is_selected_by_model_id(qapp, harness, monkeypatch):
    from macrt.toolbox.system import MODELS
    for model_id in MODELS:
        v = _volume()
        h = harness(disks=[v], model=model_id)
        monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
        about = h.d.show_about_classetta()
        h.settle()
        assert about._logo_icon is theme.MAC_LOGO_ICONS[model_id]
        assert about._logo_pixmap is icons.pixmap(model_id)
        about.close()
        h.settle()


def test_the_about_logo_keeps_1bit_at_depth_1(qapp, harness, monkeypatch):
    v = _volume()
    h = harness(disks=[v], model="iix")
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 1)
    about = h.d.show_about_classetta()
    h.settle()
    assert about._logo_pixmap is None
    assert about._logo_icon is theme.MAC_IIX_ICON
    about.close()
    h.settle()


def test_the_iix_and_iici_logos_are_their_own_art():
    logos = {model_id: theme.MAC_LOGO_ICONS[model_id]
             for model_id in ("plus", "ii", "iix", "iici")}
    assert len({tuple(icon) for icon in logos.values()}) == 4
    for model_id in ("iix", "iici"):
        assert icons.pixmap(model_id).width() == 32


def test_no_models_entry_grows_without_an_about_logo():
    # Task 5.5's rule: adding to MODELS without an About logo (the 1-bit
    # constant and its colour sibling) fails here.
    from macrt.toolbox.system import MODELS
    for model_id in MODELS:
        assert model_id in theme.MAC_LOGO_ICONS, (
            f"MODELS entry {model_id!r} has no 1-bit About logo "
            "(theme.MAC_LOGO_ICONS)")
        assert model_id in icons.ART, (
            f"MODELS entry {model_id!r} has no colour About logo (icons.ART)")


def test_about_classetta_opens_on_every_model(qapp, harness):
    from macrt.toolbox.system import MODELS
    for model_id in MODELS:
        v = _volume()
        h = harness(disks=[v], model=model_id)
        about = h.d.show_about_classetta()
        h.settle()
        assert len(h.d.about_windows) == 1
        assert about.isVisible()
        about.close()
        h.settle()


def test_the_about_window_paints_at_depth_8(qapp, harness, monkeypatch):
    for model_id in ("ii", "iix", "iici"):
        v = _volume()
        h = harness(disks=[v], model=model_id)
        monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
        about = h.d.show_about_classetta()
        h.settle()
        img = about.grab().toImage()
        assert not img.isNull()
        assert img.width() > 0 and img.height() > 0
        about.close()
        h.settle()


def test_the_filer_title_paints_at_depth_8(qapp, harness, monkeypatch):
    v = _volume()
    h = harness(disks=[v])
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    img = win.grab().toImage()
    assert not img.isNull()
    assert win.frame().title_pixmap is not None
    h.d.close_filer(win)


def test_the_colour_surfaces_keep_1bit_at_depth_1(qapp, harness, monkeypatch):
    # Depth 1 is unchanged everywhere colour art could draw.
    v = _volume()
    h = harness(disks=[v], model="iix")
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 1)
    win = h.d.open_filer(h.d.config.disks[0])
    assert win.frame().title_pixmap is None
    h.d.close_filer(win)
    about = h.d.show_about_classetta()
    h.settle()
    assert about._logo_pixmap is None
    assert all(row[4] is None for row in about._processes)
    about.close()
    h.settle()

# -- the 1-bit stand-ins draw colour at depth > 2, everywhere --

def _volume_with_items():
    v = _volume()
    doc = machfs.File()
    doc.type, doc.creator, doc.data = b"TEXT", b"TEST", b"hello"
    v["Letter"] = doc
    v["Docs"] = machfs.Folder()
    return v


def _row(win, name):
    for i in range(win.tree.topLevelItemCount()):
        item = win.tree.topLevelItem(i)
        if item.text(0) == name:
            return item
    raise KeyError(name)


def test_standin_icon_picks_colour_art_at_depth_8():
    assert standin_icon("folder", 8).pixmap(16, 16).toImage() == icons.pixmap("folder").toImage()
    assert standin_icon("document", 8).pixmap(16, 16).toImage() == icons.pixmap("document").toImage()
    assert standin_icon("volume", 8).pixmap(16, 16).toImage() == icons.pixmap("disk").toImage()
    assert standin_icon("application", 8).pixmap(16, 16).toImage() == icons.pixmap("app").toImage()


def test_standin_icon_keeps_1bit_at_depth_2_and_below():
    for depth in (1, 2):
        assert standin_icon("folder", depth).pixmap(16, 16).toImage() == icon("folder").pixmap(16, 16).toImage()


def test_standin_icon_for_an_unknown_kind_falls_back_to_1bit():
    assert standin_icon("mystery", 8).pixmap(16, 16).toImage() == icon("document").pixmap(16, 16).toImage()
    assert standin_icon("mystery", 8, large=True).pixmap(32, 32).toImage() == icon("document", large=True).pixmap(32, 32).toImage()


def test_a_plain_row_shows_the_colour_stand_in_at_depth_8(qapp, harness, monkeypatch):
    h = harness(disks=[_volume_with_items()], menu_bar="global")
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _row(win, "Docs").icon(0).pixmap(16, 16).toImage() == icons.pixmap("folder").toImage()
    assert _row(win, "Letter").icon(0).pixmap(16, 16).toImage() == icons.pixmap("document").toImage()
    h.d.close_filer(win)


def test_a_plain_row_keeps_1bit_at_depth_1(qapp, harness, monkeypatch):
    h = harness(disks=[_volume_with_items()], menu_bar="global")
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 1)
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    assert _row(win, "Docs").icon(0).pixmap(16, 16).toImage() == icon("folder").pixmap(16, 16).toImage()
    h.d.close_filer(win)


def test_get_info_title_picks_the_colour_stand_in_at_depth_8(qapp, harness, monkeypatch):
    h = harness(disks=[_volume_with_items()], menu_bar="global")
    monkeypatch.setattr(filer_mod, "_depth", lambda display: 8)
    win = h.d.open_filer(h.d.config.disks[0])
    h.settle()
    win.tree.setCurrentItem(_row(win, "Letter"))
    h.settle()
    win.get_info()
    h.settle()
    info = h.d.info_windows[0]
    assert info.frame().title_pixmap is not None
    assert info.frame().title_pixmap.toImage() == icons.pixmap("document").toImage()
    info.close()
    h.d.close_filer(win)
