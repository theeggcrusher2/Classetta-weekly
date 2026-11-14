"""
Two small guest apps that drive the chrome through the Toolbox, written
the way a classic Mac program is: set up menus, open a window, then sit in
a WaitNextEvent loop handling FindWindow part codes.  Nothing on screen
reacts to a click unless the guest asks it to.

    python -m macrt                          # host windows, global menu bar
    python -m macrt --menubar window         # menu bar under each title bar
    python -m macrt --host screen            # everything inside one Mac screen

Or run a real 68k application instead (see docs/running-apps.md):

    python -m macrt App.bin                  # MacBinary, BinHex, AppleDouble…
    python -m macrt Disk.dsk                 # a Filer window; double-click to run
    python -m macrt "System 6.0.8.dsk" --app TeachText
    python -m macrt One.bin Two.hqx          # several, side by side
    python -m macrt App.hqx --list           # just list its resources
    python -m macrt App.bin --view           # show its resources; don't run its code
    python -m macrt App.bin --filer          # show it in a Filer window instead
    python -m macrt App.bin --trace-traps    # log every trap it calls
    python -m macrt App.bin --system System  # with a real System file
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import signal
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from .m68k.segload import has_code, why_no_code
from .macfile import (ArchiveFolder, ContainerFolder, HostFolder, MacFile, alias_open,
                      archive_entries, archive_file, container_kind, is_alias,
                      is_control_panel, launch_target, open_application, open_disks)
from .resfork import is_fork
from .toolbox import constants as k
from .toolbox.api import Toolbox
from .toolbox.types import Rect
from .ui.display import Display, DisplayConfig
from .ui.filer import FilerOptions
from .viewer import describe, viewer_main

# No "(" in these: AppendMenu reads it as "disable this item", as on a Mac.
WINDOW_KINDS = [
    ("Document", k.zoomDocProc, True),
    ("No-Grow Document", k.zoomNoGrow, True),
    ("Plain Document", k.documentProc, True),
    None,
    ("Modal Dialog — dBoxProc", k.dBoxProc, False),
    ("Plain Box — plainDBox", k.plainDBox, False),
    ("Shadow Box — altDBoxProc", k.altDBoxProc, False),
    ("Movable Modal", k.movableDBoxProc, False),
    None,
    ("Desk Accessory — rDocProc", k.rDocProc, True),
]


def lo_word(x: int) -> int:
    return x & 0xFFFF


def hi_word(x: int) -> int:
    v = (x >> 16) & 0xFFFF
    return v - 0x10000 if v & 0x8000 else v


class Sampler:
    """Opens one of every window kind and reports what it's sent."""

    APPLE, FILE, EDIT, WINDOWS = 128, 129, 130, 131

    def __init__(self, tb: Toolbox):
        self.tb = tb
        self.info: dict = {}      # window -> lines it displays
        self.count = 0
        self.last_key = "(none yet)"
        self.quit = False

    def run(self):
        tb = self.tb
        apple = tb.NewMenu(self.APPLE, k.APPLE_MENU_TITLE)
        tb.AppendMenu(apple, "About Sampler…;(-")
        file = tb.NewMenu(self.FILE, "File")
        tb.AppendMenu(file, "New/N;Close/W;(-;Quit/Q")
        edit = tb.NewMenu(self.EDIT, "Edit")
        tb.AppendMenu(edit, "(Undo/Z;(-;(Cut/X;(Copy/C;(Paste/V;(Clear")
        kinds = tb.NewMenu(self.WINDOWS, "Windows")
        tb.AppendMenu(kinds, ";".join(kind[0] if kind else "(-" for kind in WINDOW_KINDS))
        for m in (apple, file, edit, kinds):
            tb.InsertMenu(m, 0)
        tb.DrawMenuBar()
        self.open_window(0)
        while not self.quit:
            got, ev = tb.WaitNextEvent(k.everyEvent, 30)
            if got:
                self.handle(ev)

    def open_window(self, index: int):
        name, proc_id, go_away = WINDOW_KINDS[index]
        self.count += 1
        off = 22 * (self.count % 8)
        boxed = proc_id in (k.dBoxProc, k.plainDBox, k.altDBoxProc, k.movableDBoxProc)
        w, h = (300, 120) if boxed else (340, 200)
        bounds = Rect.from_xywh(40 + off, 70 + off, w, h)
        title = "" if proc_id in (k.dBoxProc, k.plainDBox, k.altDBoxProc) else (
            f"Untitled {self.count}" if index == 0 else name)
        win = self.tb.NewWindow(bounds, title, True, proc_id, go_away=go_away)
        lines = [f"{name}", f"procID {proc_id}"]
        if not go_away:
            lines.append("Click inside to close.")
        self.info[win] = lines

    def draw(self, win):
        tb = self.tb
        tb.SetPort(win)
        r = win.content
        tb.EraseRect(Rect(0, 0, r.height, r.width))
        v = 22
        for line in self.info.get(win, []) + [f"Last key: {self.last_key}",
                                              f"Size: {r.width} x {r.height}"]:
            tb.MoveTo(12, v)
            tb.DrawString(line)
            v += 16
        tb.DrawGrowIcon(win)

    def handle(self, ev):
        tb = self.tb
        if ev.what == k.mouseDown:
            part, win = tb.FindWindow(ev.where)
            if part == k.inMenuBar:
                self.do_menu(tb.MenuSelect(ev.where))
            elif part == k.inDrag:
                tb.DragWindow(win, ev.where, None)
            elif part == k.inGoAway:
                if tb.TrackGoAway(win, ev.where):
                    self.close(win)
            elif part in (k.inZoomIn, k.inZoomOut):
                if tb.TrackBox(win, ev.where, part):
                    tb.ZoomWindow(win, part, False)
            elif part == k.inGrow:
                size = tb.GrowWindow(win, ev.where, Rect(80, 120, 2000, 2000))
                if size:
                    tb.SizeWindow(win, lo_word(size), hi_word(size), True)
            elif part == k.inContent:
                if win is not tb.FrontWindow():
                    tb.SelectWindow(win)
                elif not win.go_away:
                    self.close(win)
        elif ev.what in (k.keyDown, k.autoKey):
            ch = chr(ev.message & 0xFF)
            if ev.modifiers & k.cmdKey:
                self.do_menu(tb.MenuKey(ch))
            else:
                self.last_key = f"{ch!r} (key code 0x{(ev.message >> 8) & 0xFF:02X})"
                front = tb.FrontWindow()
                if front is not None:
                    self.draw(front)
        elif ev.what == k.updateEvt:
            tb.BeginUpdate(ev.message)
            self.draw(ev.message)
            tb.EndUpdate(ev.message)
        elif ev.what == k.activateEvt:
            tb.SetPort(ev.message)
            tb.DrawGrowIcon(ev.message)

    def close(self, win):
        self.info.pop(win, None)
        self.tb.DisposeWindow(win)

    def do_menu(self, result: int):
        tb = self.tb
        menu, item = hi_word(result), lo_word(result)
        if menu == self.APPLE and item == 1:
            self.about()
        elif menu == self.FILE:
            if item == 1:
                self.open_window(0)
            elif item == 2:
                front = tb.FrontWindow()
                if front is not None:
                    self.close(front)
            elif item == 4:
                self.quit = True
        elif menu == self.WINDOWS:
            self.open_window(item - 1)
        tb.HiliteMenu(0)

    def about(self):
        win = self.tb.NewWindow(Rect.from_xywh(120, 120, 280, 90), "", True, k.dBoxProc,
                                go_away=False)
        self.info[win] = ["Sampler", "A macrt chrome demo.", "Click inside to close."]


class Ticker:
    """A second app, so there is something to switch to."""

    APPLE, FILE, OPTIONS = 128, 129, 130

    def __init__(self, tb: Toolbox):
        self.tb = tb
        self.seconds = True

    def run(self):
        tb = self.tb
        apple = tb.NewMenu(self.APPLE, k.APPLE_MENU_TITLE)
        tb.AppendMenu(apple, "About Ticker…;(-")
        file = tb.NewMenu(self.FILE, "File")
        tb.AppendMenu(file, "Quit/Q")
        self.options = tb.NewMenu(self.OPTIONS, "Options")
        tb.AppendMenu(self.options, "Show Seconds!\x12")
        for m in (apple, file, self.options):
            tb.InsertMenu(m, 0)
        tb.DrawMenuBar()
        sw = tb.ScreenBounds().right
        win = tb.NewWindow(Rect.from_xywh(max(sw - 230, 420), 90, 180, 60), "Ticker", True,
                           k.noGrowDocProc)
        shown = ""
        while True:
            got, ev = tb.WaitNextEvent(k.everyEvent, 20)
            now = time.strftime("%H:%M:%S" if self.seconds else "%H:%M")
            if ev.what == k.updateEvt:
                tb.BeginUpdate(ev.message)
                shown = ""
                tb.EndUpdate(ev.message)
            elif ev.what == k.mouseDown:
                part, w = tb.FindWindow(ev.where)
                if part == k.inMenuBar:
                    if self.do_menu(tb.MenuSelect(ev.where)):
                        return
                elif part == k.inDrag:
                    tb.DragWindow(w, ev.where, None)
                elif part == k.inGoAway and tb.TrackGoAway(w, ev.where):
                    return
                elif part == k.inContent and w is not tb.FrontWindow():
                    tb.SelectWindow(w)
            elif ev.what == k.keyDown and ev.modifiers & k.cmdKey:
                if self.do_menu(tb.MenuKey(chr(ev.message & 0xFF))):
                    return
            if now != shown:
                shown = now
                tb.SetPort(win)
                tb.EraseRect(Rect(0, 0, 60, 180))
                tb.MoveTo(56 if self.seconds else 70, 34)
                tb.DrawString(now)

    def do_menu(self, result: int) -> bool:
        menu, item = hi_word(result), lo_word(result)
        quit_ = menu == self.FILE and item == 1
        if menu == self.OPTIONS and item == 1:
            self.seconds = not self.seconds
            self.tb.CheckItem(self.options, 1, self.seconds)
        self.tb.HiliteMenu(0)
        return quit_


def sampler_main(tb: Toolbox):
    Sampler(tb).run()


def ticker_main(tb: Toolbox):
    Ticker(tb).run()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="macrt", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", choices=["desktop", "screen"], default="desktop",
                    help="Mac windows as host windows, or inside one emulated screen")
    ap.add_argument("--menubar", choices=["global", "window"], default="global",
                    help="one bar for the front app, or one under each window's title bar")
    ap.add_argument("--size", default=None, help="Mac screen size, e.g. 640x480")
    ap.add_argument("--speed", type=float, default=None, metavar="N",
                    help="run 68k code at about N times a Mac Plus's speed, for games that "
                         "run as fast as the processor goes (default: as fast as it goes)")
    ap.add_argument("--model", choices=["plus", "ii"], default="plus",
                    help="the Mac it says it is: a Mac Plus's 68000, or a Mac II's 68020 and "
                         "68881, for programs that need one (default: plus)")
    ap.add_argument("--depth", type=int, choices=[1, 2, 4, 8, 16, 32], default=None,
                    help="with --model ii, the screen's bits a pixel: 1 for black and white, "
                         "2 to 8 for colours by a colour table, 16 and 32 for direct colour "
                         "(default: 8)")
    ap.add_argument("--row-bytes", type=int, metavar="N", default=None,
                    help="how many bytes apart the screen's rows are at the depth it starts "
                         "at (and at another, as many pixels apart), for a program that writes "
                         "to the screen taking them to be so many rather than asking QuickDraw "
                         "(default: as few as a row needs)")
    ap.add_argument("--no-sound", action="store_true",
                    help="don't play the Mac's sound on the host's sound output")
    ap.add_argument("--record-sound", metavar="FILE.wav",
                    help="write the Mac's sound to a WAV file as it plays")
    ap.add_argument("--classic-menus", action="store_true",
                    help="press-drag-release only (no sticky menus)")
    ap.add_argument("--rainbow", action="store_true", help="System 7 color apple")
    ap.add_argument("--single", action="store_true", help="launch only the Sampler")
    ap.add_argument("file", nargs="*",
                    help="Mac applications to run instead of the demo, side by side: "
                         "MacBinary, BinHex, AppleSingle/Double, emulator side files, or "
                         "disk images (HFS or MFS; raw, DiskCopy 4.2 or MOOF); an image "
                         "with no --app opens in a Filer window to pick from, and is "
                         "in a drive as well")
    ap.add_argument("--app", metavar="NAME", action="append", default=[],
                    help="which application on a disk image (its name or Mac path), or "
                         "a document, to launch the application that made it with it; "
                         "repeat it to run several")
    ap.add_argument("--memory", type=int, default=None, metavar="K",
                    help="give the application K kilobytes of memory, in place of what its "
                         "SIZE resource asks for, as the memory size in the Finder's Get "
                         "Info does; a control panel's is for the application it launches "
                         "from its own file")
    ap.add_argument("--list", action="store_true", help="list the file's resources and exit")
    ap.add_argument("--view", action="store_true",
                    help="show the application's menus, windows and dialogs from its "
                         "resources instead of running its code")
    ap.add_argument("--filer", action="store_true",
                    help="show a file in a Filer window instead of running it")
    ap.add_argument("--trace-traps", action="store_true",
                    help="log every Toolbox and OS trap the application calls")
    ap.add_argument("--system", metavar="FILE",
                    help="a real System file, or a disk image holding one, in place of the "
                         "built-in empty one")
    args = ap.parse_args(argv)
    if args.memory is not None and args.memory <= 0:
        ap.error("--memory is a size in K, more than 0")
    if args.depth not in (None, 1) and args.model != "ii":
        ap.error("a Mac Plus's screen is black and white: --depth needs --model ii")
    if args.row_bytes is not None and (args.row_bytes & 1 or not 0 < args.row_bytes < 0x4000):
        ap.error("--row-bytes is an even number of bytes, less than 16384")
    if args.size is not None:
        args.size = _screen_size(args.size)
        if args.size is None:
            ap.error("--size is a width and height in pixels, e.g. 640x480, each 1 to 32767")
    if args.speed is not None and not 0 < args.speed < math.inf:
        ap.error("--speed is a number of times a Mac Plus's speed, more than 0")
    return args


def _screen_size(text: str) -> tuple[int, int] | None:
    """WxH as a size, or None: QuickDraw's coordinates are INTEGERs."""
    m = re.fullmatch(r"\s*(\d{1,5})\s*[xX]\s*(\d{1,5})\s*", text)
    if m is None:
        return None
    w, h = int(m[1]), int(m[2])
    return (w, h) if 0 < w <= 0x7FFF and 0 < h <= 0x7FFF else None


def _target_error(f: MacFile) -> str | None:
    """Why f can't be launched here, or None."""
    if not f.rsrc and is_fork(f.data):
        return (f"{f.name}'s resource fork is in its data fork: it was copied "
                "by something that didn't keep its two forks apart.")
    if not f.rsrc:
        return (f"{f.name} has no resource fork. A Mac file has to travel in a "
                "form that keeps it: MacBinary, BinHex, AppleDouble or a disk image.")
    if not is_fork(f.rsrc):
        return f"{f.name}'s resource fork is damaged"
    return None


def _launch_view(f: MacFile, view: bool) -> bool:
    """Whether launching f shows its menus and windows from its resources:
    asked for, or because f has no 68k code of its own."""
    if view:
        print("Showing its menus and windows from its resources, without running its code.")
        return True
    if has_code(f) or is_control_panel(f):
        return False
    print(f"{f.name} has no 68k code ({why_no_code(f)}), so macrt shows its menus "
          "and windows from its resources.")
    return True


def open_targets(args):
    """The applications named on the command line, each with whether to
    show it from its resources rather than run its code, the disk images
    for the drives, and those among them to show in Filer windows (those
    no --app picks from, and with --filer a loose file's folder); or an
    error message."""
    browse = bool(args.file) and not args.app and not args.list
    folders: list[HostFolder] = []
    archives: list[ArchiveFolder] = []
    files = []
    for path in args.file or ():
        p = Path(path)
        if p.is_dir():                        # a host folder: browse it
            folders.append(HostFolder(p))
            continue
        if not p.is_file():
            files.append(path)                # let open_disks say it's missing
            continue
        try:
            archive = archive_file(p)
        except ValueError as e:          # a recognised archive, damaged
            return None, None, None, str(e)
        if archive is not None:               # an archive macextract reads: browse it
            archives.append(archive)
            continue
        files.append(path)
    try:
        apps, disks = open_disks(files, args.app, browse=browse)
    except (OSError, ValueError) as e:
        return None, None, None, str(e)
    targets = []
    filers = list(disks) if browse else []
    filers += folders + archives
    for f in apps:
        if is_alias(f):
            # A loose alias file: the original it points at is what's
            # opened, as from the Filer.  The volumes it points through are
            # mounted only when the Display is made, so main resolves it
            # before launch.
            print(describe(f, full=args.list))
            targets.append((f, bool(args.view)))
            continue
        print(describe(f, full=args.list))
        for doc in f.documents:
            print(f"Opening {doc.name} with it.")
        if args.list:
            continue
        if getattr(args, "filer", False) and isinstance(f.folder, HostFolder):
            # A loose file asked for in Filer view: an encoding shows the
            # file it decodes to; anything else shows its folder.
            if container_kind(f.key) is not None:
                filers.append(ContainerFolder(f, Path(f.key).name))
            else:
                try:
                    entries = archive_entries(f.data, f.name) if f.data else None
                except ValueError as e:
                    return None, None, None, str(e)
                if entries is not None:
                    filers.append(ArchiveFolder(entries, Path(f.key).name, f.data))
                elif not any(isinstance(v, HostFolder) and v.path == f.folder.path
                             for v in filers):
                    filers.append(f.folder)
            continue
        err = _target_error(f)
        if err is not None:
            return None, None, None, err
        targets.append((f, _launch_view(f, args.view)))
    return targets, disks, filers, None


def main(argv=None) -> int:
    args = parse_args(argv)
    targets, disks, filers = [], [], []
    if args.file:
        targets, disks, filers, error = open_targets(args)
        if error:
            print(f"macrt: {error}", file=sys.stderr)
            return 2
        if args.list:
            return 0
    system_file = None
    if args.system:
        try:
            system_file = open_application(args.system, "System")
        except (OSError, ValueError) as e:
            print(f"macrt: {e}", file=sys.stderr)
            return 2
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from .ui import theme
    theme.RAINBOW_APPLE = args.rainbow
    try:
        display = Display(DisplayConfig(host=args.host, menu_bar=args.menubar,
                                        screen_size=args.size,
                                        sticky_menus=not args.classic_menus,
                                        system_file=system_file, speed=args.speed,
                                        model=args.model, depth=args.depth,
                                        row_bytes=args.row_bytes, disks=disks,
                                        sound=not args.no_sound, record_sound=args.record_sound))
    except (OSError, ValueError) as e:      # a screen too big, a WAV file that can't be made
        print(f"macrt: {e}", file=sys.stderr)
        return 2
    display.show()
    if targets or filers:
        logging.basicConfig(format="macrt: %(message)s")
        logging.getLogger("macrt").setLevel(logging.DEBUG if args.trace_traps else logging.INFO)
    if filers:
        options = FilerOptions(view=args.view, trace=args.trace_traps,
                                memory=args.memory * 1024 if args.memory else None)
        for volume in filers:
            display.open_filer(volume, options=options)
    if targets:
        try:
            for f, view in targets:
                if is_alias(f):
                    # A loose alias file: open the original it points at,
                    # as the Filer does — an application, or a Filer window
                    # on the folder it points at.
                    target, volume, path = alias_open(f, display.system.files)
                    if target is None and volume is None:
                        raise ValueError(f"{f.name} is an alias, but its original "
                                         "can't be found.")
                    if target is None:
                        display.open_filer(volume, path)
                        continue
                    print(describe(target, full=args.list))
                    err = _target_error(target)
                    if err is not None:
                        raise ValueError(err)
                    target = launch_target(target)
                    for doc in target.documents:
                        print(f"Opening {doc.name} with it.")
                    f, view = target, _launch_view(target, view)
                display.launch(f.name, viewer_main if view else None, app=f,
                               trace=args.trace_traps,
                               memory=args.memory * 1024 if args.memory else None)
        except (MemoryError, ValueError) as e:
            display.shutdown(quit_app=False)
            print(f"macrt: {e}", file=sys.stderr)
            return 2
    elif not filers:
        display.launch("Sampler", sampler_main)
        if not args.single:
            display.launch("Ticker", ticker_main)
    signal.signal(signal.SIGINT, lambda *_: display.shutdown())
    heartbeat = QTimer(interval=200)      # lets Python see Ctrl+C
    heartbeat.timeout.connect(lambda: None)
    heartbeat.start()
    status = app.exec()
    return finish(display, status)


def finish(display: Display, status: int) -> int:
    """After the Qt event loop ends: finish the shutdown, wait for the
    applications' threads, and, when Special > Restart asked for it,
    run the original command again with the same parameters."""
    display.shutdown(quit_app=False)
    display.join()
    if display.restarting:
        os.execvp(sys.orig_argv[0], sys.orig_argv)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
