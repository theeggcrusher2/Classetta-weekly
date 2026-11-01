"""Opening a real application from the command line: the resource listing,
the errors, and ResourceViewer standing in for the app's code."""

import gc
import struct
import threading

import machfs
import pytest

from builders import alrt, ditl, dlog, menu, pstr, size_res, vers, wind
from macrt.demo import main, open_targets, parse_args
from macrt.m68k.segload import has_powerpc_code, why_no_code
from macrt.macfile import MacFile, load_volume
from macrt.resfork import Resource, build_fork
from macrt.toolbox import constants as k
from macrt.toolbox.templates import btnCtrl, ctrlItem, editText, statText
from macrt.toolbox.types import Rect
from macrt.ui.display import InputRouter, ScreenWindow
from macrt.viewer import ResourceViewer, describe
from test_files import macbinary

FIND_ITEMS = ditl((ctrlItem + btnCtrl, (70, 200, 90, 260), b"Find"),
                  (statText, (10, 10, 26, 80), b"Find what:"),
                  (editText, (10, 90, 26, 260), b""),
                  (64, (40, 10, 60, 60), struct.pack(">h", 128)))

APP_FORK = build_fork([
    Resource(b"MBAR", 128, struct.pack(">3h", 2, 129, 130)),
    Resource(b"MENU", 129, menu(129, "File", [("New", "N"), ("(-", ""), ("Quit", "Q")])),
    Resource(b"MENU", 130, menu(130, "Edit", [("Undo", "Z"), ("Copy", "C")])),
    Resource(b"WIND", 128, wind(Rect(60, 40, 220, 340), "Main", k.zoomDocProc)),
    Resource(b"WIND", 129, wind(Rect(0, 0, 100, 200), "Tucked", k.documentProc, visible=False)),
    Resource(b"DLOG", 200, dlog(Rect(80, 80, 180, 360), "Find", 200, k.movableDBoxProc)),
    Resource(b"DITL", 200, FIND_ITEMS),
    Resource(b"ALRT", 300, alrt(Rect(60, 100, 160, 400), 300)),
    Resource(b"DITL", 300, ditl((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
                                (statText, (10, 60, 50, 290), b"Something happened."))),
    Resource(b"SIZE", -1, size_res(640 * 1024, 384 * 1024)),
    Resource(b"vers", 1, vers("1.0", "1.0, a test app")),
    *[Resource(b"STR ", 128 + i, pstr(f"string {i}")) for i in range(14)],
])


def app_file():
    return MacFile("Test App", rsrc=APP_FORK, type=b"APPL", creator=b"TEST")


# -- the listing -------------------------------------------------------------
def test_describe_summary():
    lines = describe(app_file()).splitlines()
    assert lines[0] == "Test App: application, type 'APPL', creator 'TEST'"
    assert lines[1] == f"  data fork 0 bytes, resource fork {len(APP_FORK):,} bytes, 25 resources"
    assert "  WIND 128 129" in lines
    assert "  STR  128 129 130 131 132 133 134 135 136 137 138 139 … (14)" in lines


def test_describe_every_resource():
    text = describe(app_file(), full=True)
    assert "'Main', 300x160, procID 8" in text
    assert "'Find', 280x100, items in DITL 200" in text
    assert "300x100, items in DITL 300" in text
    assert "DITL    200" in text and "4 items" in text
    assert "File: New, -, Quit" in text
    assert "menus 129, 130" in text
    assert "640K preferred, 384K minimum" in text
    assert "1.0, a test app" in text
    assert "'string 0'" in text


def test_describe_a_damaged_fork():
    text = describe(MacFile("Broken", rsrc=b"not a resource fork" * 20))
    assert "resource fork unreadable" in text


# -- the command line --------------------------------------------------------
def test_list(tmp_path, capsys):
    p = tmp_path / "app.bin"
    p.write_bytes(macbinary("Test App", b"", APP_FORK))
    assert main([str(p), "--list"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("Test App: application")
    assert "640K preferred" in out


def test_list_several(tmp_path, capsys):
    paths = []
    for name in ("Test App", "Other App"):
        paths.append(tmp_path / f"{name}.bin")
        paths[-1].write_bytes(macbinary(name, b"", APP_FORK))
    assert main([str(p) for p in paths] + ["--list"]) == 0
    out = capsys.readouterr().out
    assert out.index("Test App: application") < out.index("Other App: application")


def test_command_line_errors(tmp_path, capsys):
    def fails(*argv):
        assert main([str(a) for a in argv]) == 2
        return capsys.readouterr().err

    assert "No such file" in fails(tmp_path / "nothing.bin")
    sit = tmp_path / "Game.sit"
    sit.write_bytes(b"SIT!" + bytes(6) + b"rLau" + bytes(100))
    assert "StuffIt archive: unpack it first" in fails(sit)
    bare = tmp_path / "Game"
    bare.write_bytes(b"just a data fork, the resource fork was lost on the way")
    assert "has no resource fork" in fails(bare)
    copied = tmp_path / "copied.bin"
    copied.write_bytes(macbinary("Copied", APP_FORK, b""))
    assert "resource fork is in its data fork" in fails(copied)
    broken = tmp_path / "broken.bin"
    broken.write_bytes(macbinary("Broken", b"", b"not a resource fork" * 20))
    assert "damaged" in fails(broken)
    app = tmp_path / "app.bin"
    app.write_bytes(macbinary("Test App", b"", APP_FORK))
    assert "No such file" in fails(app, "--system", tmp_path / "System")


def test_an_application_past_the_end_of_a_disk_image_cut_short(tmp_path):
    """An image that holds only part of its disk has an application whose
    resource fork goes past its end: macrt says so, rather than that the
    fork was lost in copying.  One whose forks are on what the image holds
    runs as ever."""
    v = machfs.Volume()
    v.name = "Part"
    for name in ("First", "Last"):
        f = v[name] = machfs.File()
        f.type, f.creator, f.rsrc = b"APPL", b"TEST", APP_FORK + bytes(64 * 1024)
    image = bytearray(v.write(800 * 1024, desktopdb=False, bootable=False))
    # Last's fork moved to the end of the disk, past its catalog.
    vol = load_volume(bytes(image))
    (at, end), = vol["Last"]._forks["rsrc"][1]
    blocks = struct.pack(">HH", (at - 2048) // 512, (end - at) // 512)
    assert image.count(blocks) == 1
    i, moved = image.index(blocks), 2048 + 1400 * 512
    image[i:i + 2] = struct.pack(">H", 1400)
    image[moved:moved + end - at] = image[at:end]
    part = tmp_path / "Part.img"
    part.write_bytes(image[:moved + 1024])
    _, _, error = open_targets(parse_args([str(part), "--app", "Last"]))
    assert error == ("Last's resource fork goes past the end of the disk image: "
                     "the image holds only part of its disk.")
    [(first, _)], _, error = open_targets(parse_args([str(part), "--app", "First"]))
    assert error is None and first.rsrc == APP_FORK + bytes(64 * 1024)


def test_why_an_application_has_no_68k_code():
    code = build_fork([Resource(b"CODE", 0, bytes(16))])
    ppc = build_fork([Resource(b"cfrg", 0, bytes(32))])
    assert why_no_code(MacFile("PPC", b"Joy!peffpwpc", ppc)) == "it's a PowerPC application"
    assert why_no_code(MacFile("Copied", code, b"")) == \
        "no CODE resources: its resource fork is in its data fork"
    assert why_no_code(MacFile("Plain", b"", APP_FORK)) == "no CODE resources"


def test_an_application_with_powerpc_code():
    """A 'cfrg' resource names PowerPC code, with CODE resources or not."""
    both = build_fork([Resource(b"CODE", 0, bytes(16)), Resource(b"cfrg", 0, bytes(32))])
    assert has_powerpc_code(MacFile("Fat", b"", both))
    assert not has_powerpc_code(MacFile("Plain", b"", APP_FORK))
    assert not has_powerpc_code(MacFile("Bare", b"", b""))


def test_screen_size_and_speed_options(tmp_path, capsys, qapp):
    assert parse_args(["--size", "640x480"]).size == (640, 480)
    assert parse_args(["--size", " 512 X 342 "]).size == (512, 342)
    assert parse_args(["--speed", "2.5"]).speed == 2.5
    for bad in (["--size", "abc"], ["--size", "0x0"], ["--size", "640x480x8"],
                ["--size", "40000x10"], ["--size", "9" * 5000 + "x1"],
                ["--speed", "inf"], ["--speed", "nan"], ["--speed", "0"], ["--speed", "-1"]):
        with pytest.raises(SystemExit):
            parse_args(bad)
    err = capsys.readouterr().err
    assert "--size is a width and height in pixels" in err and "--speed is a number" in err
    # What the machine can't be built with is said, not a traceback, and
    # leaves no window or input filter for the garbage collector: destroying
    # the window under the filter takes the host down.

    def left():
        return sum(isinstance(o, (ScreenWindow, InputRouter)) for o in gc.get_objects())
    before = left()
    assert main(["--size", "32000x32000", "--host", "screen", "--no-sound"]) == 2
    assert "a 32000x32000 screen leaves no room for applications" in capsys.readouterr().err
    assert left() <= before
    assert main(["--record-sound", str(tmp_path / "no" / "such.wav"),
                 "--host", "screen", "--no-sound"]) == 2
    assert "No such file" in capsys.readouterr().err


def test_an_app_too_big_for_memory(tmp_path, capsys, qapp):
    big = tmp_path / "big.bin"
    fork = build_fork([Resource(b"SIZE", -1, size_res(48 << 20, 32 << 20))])
    big.write_bytes(macbinary("Big App", b"", fork))
    assert main([str(big), "--host", "screen"]) == 2
    err = capsys.readouterr().err
    assert "Big App needs 32768K of memory and 24K more for its stack, and only" in err


# -- ResourceViewer in a running display -------------------------------------
class Viewer(ResourceViewer):
    def __init__(self):
        self.logged = []
        self.widths = None
        self.done = threading.Event()

    def __call__(self, tb):
        super().__init__(tb, log=self.logged.append)
        self.widths = tb.StringWidth("\x11\x12"), tb.StringWidth("⌘✓")
        try:
            self.run()
        finally:
            self.done.set()


def launch_viewer(h):
    viewer = Viewer()
    proc = h.d.launch("Test App", viewer, app=app_file())
    h.wait_until(lambda: len(proc.windows) == 1, timeout=5, what="the app's first window")
    h.settle()
    return viewer, proc


def test_viewer_puts_up_the_apps_menus_and_first_window(harness):
    h = harness()
    viewer, proc = launch_viewer(h)
    assert [(m.id, m.title) for m in proc.menus.own()] == [
        (129, "File"), (130, "Edit"), (viewer.menu_id, "Resources")]
    assert [i.text for i in proc.menus.own()[-1].items] == [
        "WIND 128: Main", "WIND 129: Tucked", "DLOG 200: Find", "ALRT 300", "-", "Quit Viewer"]
    (w,) = proc.windows
    assert (w.title, w.content, w.proc_id) == ("Main", Rect(60, 40, 220, 340), k.zoomDocProc)
    assert viewer.widths[0] == viewer.widths[1]         # Chicago's ⌘ and ✓ map to Unicode


def test_viewer_opens_windows_dialogs_and_alerts(harness):
    h = harness()
    viewer, proc = launch_viewer(h)
    bar = h.d.default_bar(proc)

    def opened(n):                                      # and in the viewer's own records
        return len(proc.windows) == n and proc.windows[0] in viewer.shown

    h.choose(bar, "Resources", 2)
    h.wait_until(lambda: opened(2) and proc.windows[0].visible, what="WIND 129")
    tucked = proc.windows[0]
    sw, _ = h.d.size
    assert tucked.title == "Tucked" and tucked.visible
    assert tucked.content.left == (sw - 200) // 2         # moved out from under the menu bar
    assert tucked.content.top > 20

    h.choose(bar, "Resources", 3)
    h.wait_until(lambda: opened(3), what="DLOG 200")
    find = proc.windows[0]
    assert (find.title, find.proc_id) == ("Find", k.movableDBoxProc)
    assert find.content == Rect(80, 80, 180, 360)
    assert len(viewer.shown[find][2]) == 4

    h.choose(bar, "Resources", 4)
    h.wait_until(lambda: opened(4), what="ALRT 300")
    alert = proc.windows[0]
    assert alert.proc_id == k.dBoxProc and alert.content.width == 300
    h.settle()
    assert proc in h.d.processes                          # drawing the items didn't crash it

    c = alert.content
    h.click((c.left + c.right) // 2, (c.top + c.bottom) // 2)
    h.wait_until(lambda: len(proc.windows) == 3, what="a click to dismiss the alert")
    assert alert not in list(proc.windows)


def test_viewer_menu_items_without_code(harness):
    h = harness()
    viewer, proc = launch_viewer(h)
    bar = h.d.default_bar(proc)
    h.choose(bar, "Edit", 2)
    h.wait_until(lambda: viewer.logged, what="Edit > Copy")
    assert viewer.logged == ["Edit > Copy: no code to run yet"]
    h.choose(bar, "File", 3)
    h.wait_until(viewer.done.is_set, what="File > Quit")
    h.wait_until(lambda: proc not in h.d.processes, what="the app to go")
