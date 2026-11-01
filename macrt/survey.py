"""
Run Mac applications headless, many at a time, and report what they did:
whether each is still running, quit, or stopped with a system error;
which traps it called that macrt doesn't implement; and screenshots
along the way.  Comparing two surveys' reports and screenshots shows what
a change to macrt changed (see docs/survey.md).

    python -m macrt.survey run titles.json --out OUT [--jobs N] [--resume]
    python -m macrt.survey one Disk.dsk --app NAME --steps "w 5;c 100,200;s a" --out OUT/x
    python -m macrt.survey report OUT
    python -m macrt.survey compare OLD NEW

run takes a list of titles as JSON: a list of runs, or an object with a
"runs" list and "defaults" for each run's fields.  A run's fields are

    name     what to call it: its folder in OUT (default: from app or file)
    file     the application, or a disk image, or a list of disk images
    app      which application on the disk image, or a document of one
    model    "plus" (the default), "ii" or "iix"
    depth    the screen's bits a pixel, with model "ii" or "iix"
    size     the screen's size, "640x480" (the default)
    row_bytes
             how many bytes apart the screen's rows are, as macrt's
             --row-bytes
    memory   the application's memory, in K, in place of its SIZE's
    system   a System file, or a disk image holding one
    speed    run 68k code at about this many times a Mac Plus's speed
    wait     seconds to wait after launching, before the steps (default 6)
    settle   true to end that wait sooner, once the screen has settled (W)
    steps    what to do then (below)
    timeout  seconds the run may take in all (default: its wait and steps
             take, and two minutes more)
    trace    log every trap the application calls
    outside_port_rect
             true to note drawing that lands outside a port's portRect,
             which macrt doesn't clip to (result.json's outside_port_rect)

Paths are relative to the list's folder.  Each run is a process of its own,
so that one that crashes Python can't stop the others.  Steps, separated
by ";", are

    w SECS          wait
    W SECS          wait until the screen has settled, or SECS have gone
                    by: until every process has been asking for an event
                    for 1.5 seconds with no more than a caret's blink on
                    the screen
    c H,V           click at (h, v), in the Mac's global coordinates
    d H,V H,V ...   press at the first point, drag through the others,
                    let go at the last
    k TEXT          type TEXT a key at a time: \\n is Return, \\t Tab,
                    \\; a semicolon and \\\\ a backslash
    K NAME[,SECS]   press a key by its name (Return, Enter, Escape, Tab,
                    Space, Backspace, Left, Up, F1...), holding it SECS
    C X             Command-X
    s NAME          a screenshot, NAME.png
    O               OK every host alert that's up (a system error's)

Each run's folder has result.json, log.txt with what macrt printed, and
its screenshots, ending with end.png, the screen when the steps are done.
result.json lists each process's windows, and the text of any dialog or
alert left up, so that one waiting at its own alert shows what it asked;
with outside_port_rect, the ports drawn in outside their portRects;
and first_window, how many seconds after launching the first window came;
with settle, settled, how many seconds after launching the wait settled
(null if it didn't); and busy, the share of one of the host's processors
the run took in the last BUSY_WINDOW seconds of that wait, all its
threads': near 1 for a title that keeps asking or drawing while it waits.
A run that takes too long has its threads' stacks written to log.txt
before it's stopped.
OUT/report.json sums up the runs: how many had each outcome, the
unimplemented traps by how many titles call them, the traps titles asked
GetTrapAddress for and the Gestalt selectors they asked about that macrt
hasn't, by how many titles ask (missing_asked: one that looks first takes
another way, or stops, without calling them), and of the titles still
running, those with no window to show and those waiting at a dialog or
alert, the application's or macrt's own (Standard File's), with what it
says; the runs slowest to show a window; the runs still running that
kept the host busiest at the end of their wait; and, of the runs with
outside_port_rect, those that drew outside a portRect.  --resume keeps the
runs already finished in OUT, those with a result.json, and runs the rest,
so that a long survey that was stopped can carry on.
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import faulthandler
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from array import array
from pathlib import Path
from types import SimpleNamespace

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QImage, QPainter
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel

from .demo import _screen_size, open_targets
from .macfile import open_application
from .memory import ADDRESS_MASK
from .toolbox import dialogs, lowmem
from .toolbox.constants import dialogKind
from .toolbox.system import MODELS
from .toolbox.templates import statText
from .toolbox.types import Point
from .ui.display import ROW_BYTES, Display, DisplayConfig, row_bytes_ok

OUTCOMES = ("running", "quit", "system error", "internal error", "faulted", "no code",
            "can't open", "timeout", "crashed")
FIELDS = {"name", "file", "app", "model", "depth", "size", "row_bytes", "memory", "system",
          "speed", "wait", "settle", "steps", "timeout", "trace", "outside_port_rect"}
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*")
DEFAULT_WAIT = 6.0
DIALOG_TEXT = 100           # characters of a dialog's text a report shows
SLOWEST = 10                # runs a report names as slowest to a first window
BUSY_WINDOW = 1.0           # seconds at a wait's end it measures the processor taken over
BUSY = 0.5                  # a run's busy share, from which a report names it as busy
SPARE_TIME = 120.0          # seconds a run may take beyond its wait and steps
TYPING_PACE = 0.06          # seconds between keys typed
CLICK_HOLD = 0.06           # seconds a click holds the button down
DRAG_PACE = 0.08            # seconds between a drag's points
END_SHOT = "end"
# A settled wait (W) looks at the screen every SETTLE_LOOK seconds: it's
# settled once every process has been asking for input all along, and
# what's shown in the last SETTLE_QUIET seconds is all that was in the
# SETTLE_QUIET before, two pictures at most, for a caret that blinks.
SETTLE_LOOK = 0.1
SETTLE_QUIET = 0.75
ASKED = 0.25                # how lately a process asked (or waited), to be asking still


# =========================================================================
# Steps
# =========================================================================
class StepError(ValueError):
    pass


def split_steps(text: str) -> list[str]:
    """text's steps: separated by ";", except a "\\;"."""
    steps, cur, i = [], [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\" and i + 1 < len(text):
            cur.append(text[i:i + 2])
            i += 2
            continue
        if ch == ";":
            steps.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    steps.append("".join(cur))
    return [s.strip() for s in steps if s.strip()]


_ESCAPES = {"n": "\n", "t": "\t", ";": ";", "\\": "\\"}


def unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch == "\\":
            if i + 1 >= len(text) or text[i + 1] not in _ESCAPES:
                raise StepError(f"{text!r}: \\ starts \\n, \\t, \\; or \\\\")
            out.append(_ESCAPES[text[i + 1]])
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _point(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(-?\d+)\s*,\s*(-?\d+)\s*", text)
    if m is None:
        raise StepError(f"{text!r} isn't a point h,v")
    return int(m[1]), int(m[2])


def _secs(text: str) -> float:
    try:
        secs = float(text)
    except ValueError:
        raise StepError(f"{text!r} isn't a number of seconds") from None
    if not 0 <= secs < 3600:
        raise StepError(f"{text!r}: seconds are from 0 to 3600")
    return secs


def key_named(name: str):
    key = getattr(Qt.Key, f"Key_{name}", None)
    if key is None:
        raise StepError(f"{name!r} isn't a key's name")
    return key


def parse_steps(text: str) -> list[tuple]:
    """text's steps as (op, args...), checked."""
    steps = []
    for step in split_steps(text):
        op, _, arg = step.partition(" ")
        arg = arg.strip()
        if op in ("w", "W"):
            steps.append((op, _secs(arg)))
        elif op == "c":
            steps.append(("c", _point(arg)))
        elif op == "d":
            points = [_point(p) for p in arg.split()]
            if len(points) < 2:
                raise StepError(f"{step!r}: a drag goes through two points or more")
            steps.append(("d", points))
        elif op == "k":
            if not arg:
                raise StepError(f"{step!r}: type what?")
            steps.append(("k", unescape(arg)))
        elif op == "K":
            name, _, secs = arg.partition(",")
            key_named(name.strip())
            steps.append(("K", name.strip(), _secs(secs) if secs else None))
        elif op == "C":
            if len(arg) != 1 or arg.isspace():
                raise StepError(f"{step!r}: Command and one key")
            steps.append(("C", arg))
        elif op == "s":
            if not NAME_RE.fullmatch(arg) or arg == END_SHOT:
                raise StepError(f"{step!r}: a screenshot's name is letters, digits, "
                                f"_ . + -, and not {END_SHOT!r}")
            steps.append(("s", arg))
        elif op == "O" and not arg:
            steps.append(("O",))
        else:
            raise StepError(f"{step!r} isn't a step")
    return steps


def steps_time(steps: list[tuple]) -> float:
    """About how long steps take."""
    total = 0.0
    for step in steps:
        op = step[0]
        if op in ("w", "W"):
            total += step[1]
        elif op == "k":
            total += TYPING_PACE * len(step[1])
        elif op == "d":
            total += DRAG_PACE * len(step[1])
        elif op == "K":
            total += step[2] or 0
        total += 0.1
    return total


# =========================================================================
# Lists of runs
# =========================================================================
def default_name(entry: dict) -> str:
    base = entry.get("app") or Path(_files(entry)[0]).stem
    base = base.rsplit(":", 1)[-1]
    name = re.sub(r"[^A-Za-z0-9_.+-]+", "_", base).strip("_.") or "run"
    return name if NAME_RE.fullmatch(name) else "run_" + name


def _files(entry: dict) -> list[str]:
    files = entry.get("file")
    return [files] if isinstance(files, str) else list(files or [])


def load_list(path: str | Path) -> list[dict]:
    """The runs a list file names, each with every field filled in and its
    paths made absolute; a ValueError says what's wrong with it."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"{path}: {e}") from None
    return check_runs(data, path.parent)


def check_runs(data, base: Path) -> list[dict]:
    defaults = {}
    if isinstance(data, dict):
        defaults = data.get("defaults", {})
        unknown = set(data) - {"defaults", "runs"}
        if unknown:
            raise ValueError(f"unknown keys {sorted(unknown)}: a list has runs and defaults")
        data = data.get("runs")
    if not isinstance(data, list) or not isinstance(defaults, dict):
        raise ValueError("a list of runs is a JSON list, or an object with a \"runs\" list")
    runs, names = [], set()
    for i, raw in enumerate(data):
        if not isinstance(raw, dict):
            raise ValueError(f"run {i + 1} isn't an object")
        entry = {**defaults, **raw}
        where = f"run {i + 1} ({entry.get('name') or entry.get('app') or entry.get('file')})"
        unknown = set(entry) - FIELDS
        if unknown:
            raise ValueError(f"{where}: unknown fields {sorted(unknown)}")
        for key in ("name", "app", "model", "size", "system", "steps"):
            if entry.get(key) is not None and not isinstance(entry[key], str):
                raise ValueError(f"{where}: {key} is a string")
        for key in ("trace", "settle", "outside_port_rect"):
            if not isinstance(entry.get(key, False), bool):
                raise ValueError(f"{where}: {key} is true or false")
        if entry.get("size") is not None:
            try:
                _size(entry["size"])
            except ValueError as e:
                raise ValueError(f"{where}: {e}") from None
        files = _files(entry)
        if not files or not all(isinstance(f, str) and f for f in files):
            raise ValueError(f"{where}: no file")
        entry["file"] = [str(base / f) for f in files]
        if entry.get("system"):
            entry["system"] = str(base / entry["system"])
        entry.setdefault("name", default_name(entry))
        if not NAME_RE.fullmatch(entry["name"]):
            raise ValueError(f"{where}: a name is letters, digits, _ . + -")
        if entry["name"] in names:
            raise ValueError(f"{where}: there's another run named {entry['name']!r}")
        names.add(entry["name"])
        if entry.get("model") is None:
            entry["model"] = "plus"
        if entry["model"] not in MODELS:
            raise ValueError(f"{where}: model is \"plus\", \"ii\" or \"iix\"")
        for key, kind in (("wait", (int, float)), ("timeout", (int, float)),
                          ("memory", int), ("depth", int), ("row_bytes", int),
                          ("speed", (int, float))):
            value = entry.get(key)
            zero = key == "wait"
            if value is not None and (not isinstance(value, kind) or isinstance(value, bool)
                                      or value < 0 or (value == 0 and not zero)):
                raise ValueError(f"{where}: {key} is a number, "
                                 + ("0 or more" if zero else "more than 0"))
        if entry.get("depth") not in (None, 1, 2, 4, 8, 16, 32):
            raise ValueError(f"{where}: depth is 1, 2, 4, 8, 16 or 32")
        if entry.get("depth") not in (None, 1) and not MODELS[entry["model"]].color:
            raise ValueError(f"{where}: a Mac Plus's screen is black and white: "
                             "depth needs model \"ii\" or \"iix\"")
        if entry.get("row_bytes") is not None and not row_bytes_ok(entry["row_bytes"]):
            raise ValueError(f"{where}: {ROW_BYTES}")
        try:
            parse_steps(entry.get("steps") or "")
        except StepError as e:
            raise ValueError(f"{where}: {e}") from None
        runs.append(entry)
    return runs


def one_argv(entry: dict, out: Path) -> list[str]:
    """The command line of `one` for entry, writing to out."""
    argv = [*entry["file"], "--out", str(out), "--model", entry["model"]]
    for key in ("app", "depth", "size", "row_bytes", "memory", "system", "speed", "wait",
                "steps"):
        if entry.get(key) not in (None, ""):
            argv += [f"--{key.replace('_', '-')}", str(entry[key])]
    if entry.get("settle"):
        argv.append("--settle")
    if entry.get("trace"):
        argv.append("--trace-traps")
    if entry.get("outside_port_rect"):
        argv.append("--outside-port-rect")
    return argv


# =========================================================================
# One run, in this process
# =========================================================================
class Run:
    """One run of an application, on a Display of its own."""

    def __init__(self, args):
        self.args = args
        self.out = Path(args.out)
        self.steps = parse_steps(args.steps or "")
        self.started = time.monotonic()
        self.seen = []                  # every process launched, in order
        self.launched = None            # when the first was
        self.gui_errors: list[str] = []
        self.step_errors: list[str] = []
        self.shots: dict[str, str] = {}
        self._held = None               # the widget the mouse button went down in
        self.result = {
            "name": self.out.name, "file": args.file, "app": args.app, "model": args.model,
            "outcome": None, "elapsed": None, "first_window": None, "settled": None,
            "busy": None, "processes": [],
            "system_errors": [],
            "internal_errors": [], "gui_errors": self.gui_errors,
            "step_errors": self.step_errors, "steps_done": 0, "screenshots": self.shots,
        }
        self.d = None
        self.busy = None                # the last wait's busy share

    # -- the record ------------------------------------------------------
    def finish(self, outcome: str | None = None, error: str | None = None) -> dict:
        r = self.result
        if error:
            r["error"] = error
        if self.d is not None:
            r["processes"] = [self._process(p) for p in self.seen]
            r["system_errors"] = [{"app": a, "id": i, "message": m}
                                  for a, i, m in self.d.system_errors]
            r["internal_errors"] = [{"app": p["name"], "error": p["internal_error"]}
                                    for p in r["processes"] if p["internal_error"]]
            r["host_dialogs"] = self._host_dialogs()
            seen = self.d.system.outside_port_rect
            if seen is not None:
                r["outside_port_rect"] = sorted(
                    list(seen.values()), key=lambda n: (-n["times"], n["port"], n["via"]))
        r["outcome"] = outcome or self._outcome()
        r["elapsed"] = round(time.monotonic() - self.started, 2)
        return r

    def _outcome(self) -> str:
        r = self.result
        if r["internal_errors"]:
            return "internal error"
        if r["system_errors"]:
            return "system error"
        if any(p["faulted"] for p in r["processes"]):
            return "faulted"
        return "running" if any(p["running"] for p in r["processes"]) else "quit"

    def _process(self, proc) -> dict:
        running = proc in self.d.processes
        runner = proc.runner
        rec = {"name": proc.name, "running": running, "faulted": proc.faulted,
               "windows": self._windows(proc) if running else [],
               "unimplemented": dict(runner.unimplemented_calls) if runner else {},
               "missing_asked": dict(runner.missing_asked) if runner else {},
               "internal_error": runner.internal_error if runner else None}
        return rec

    def _windows(self, proc) -> list[dict]:
        out = []
        try:
            with self.d.system.lock:
                for rec in proc.windows:
                    w = {"title": rec.title, "visible": bool(rec.visible)}
                    if proc.runner and rec.window_kind == dialogKind:
                        w["text"] = self._dialog_text(proc, rec)
                    out.append(w)
        except Exception as e:  # noqa: BLE001 - records an application damaged
            out.append({"error": repr(e)})
        return out

    @staticmethod
    def _host_dialogs() -> list[dict]:
        """macrt's own dialogs that are up (Standard File's, alerts), which
        aren't the Mac's windows: each one's title and what it says."""
        return [{"title": w.windowTitle(),
                 "text": [label.text() for label in w.findChildren(QLabel)
                          if label.isVisible() and label.text()]}
                for w in QApplication.topLevelWidgets()
                if w.isVisible() and w.windowType() == Qt.WindowType.Dialog]

    @staticmethod
    def _dialog_text(proc, rec) -> list[str]:
        """A dialog's static text, with ParamText's strings put in: an
        alert's message, such as what the application needs to run."""
        mem = proc.system.mem
        params = []
        for i in range(4):
            h = proc.world.r32(lowmem.DAStrings + 4 * i) & ADDRESS_MASK
            params.append(mem.r_pstr(mem.r32(h) & ADDRESS_MASK) if h and mem.r32(h) else b"")
        out = []
        for item in dialogs.items(mem, dialogs.Dialog(mem, rec.addr).items):
            h = item.handle & ADDRESS_MASK
            if item.type == statText and not item.hidden and h and mem.r32(h) & ADDRESS_MASK:
                text = dialogs.substitute(proc.runner.tb.contents(h), params)
                out.append(text.decode("mac_roman").replace("\r", "\n"))
        return out

    # -- running ---------------------------------------------------------
    def go(self) -> dict:
        args = self.args
        targets, disks, error = open_targets(SimpleNamespace(file=args.file, app=args.app,
                                                             list=False, view=False))
        if error:
            return self.finish("can't open", error)
        if not targets:
            return self.finish("can't open", "no application to run")
        if all(view for _, view in targets):
            return self.finish("no code")
        system_file = None
        if args.system:
            try:
                system_file = open_application(args.system, "System")
            except (OSError, ValueError) as e:
                return self.finish("can't open", str(e))
        self.app = QApplication.instance() or QApplication(["macrt-survey"])
        sys.excepthook = self._gui_error        # before the Display, which keeps it
        size = _size(args.size) if args.size else (640, 480)
        try:
            self.d = d = SurveyDisplay(self.seen, DisplayConfig(
                host="screen", screen_size=size, system_file=system_file, speed=args.speed,
                model=args.model, depth=args.depth, row_bytes=args.row_bytes, disks=disks,
                sound=False))
        except (OSError, ValueError) as e:
            return self.finish("can't open", str(e))
        if args.outside_port_rect:
            d.system.outside_port_rect = {}
        d.show()
        logging.basicConfig(format="macrt: %(message)s")
        logging.getLogger("macrt").setLevel(logging.DEBUG if args.trace_traps else logging.INFO)
        self.launched = time.monotonic()
        try:
            for f, view in targets:
                if not view:
                    d.launch(f.name, None, app=f, trace=args.trace_traps,
                             memory=args.memory * 1024 if args.memory else None)
        except (MemoryError, ValueError) as e:
            d.shutdown(quit_app=False)
            return self.finish("can't open", str(e))
        if self.wait(args.wait, args.settle):
            self.result["settled"] = round(time.monotonic() - self.launched, 2)
        self.result["busy"] = self.busy
        for step in self.steps:
            if not d.processes:
                break
            try:
                self.do(step)
            except Exception as e:  # noqa: BLE001 - a step that can't be done here
                self.step_errors.append(f"{step}: {e!r}")
            self.result["steps_done"] += 1
        self.wait(0.3)
        if not d.shutting_down:
            self.shoot(END_SHOT)
        result = self.finish()
        d.shutdown(quit_app=False)
        d.join()
        return result

    def _gui_error(self, etype, value, tb) -> None:
        text = "".join(traceback.format_exception(etype, value, tb))
        self.gui_errors.append(text)
        print("macrt: error on the GUI thread\n" + text, file=sys.stderr, end="")

    def wait(self, secs: float, settle: bool = False) -> bool:
        """Wait secs; with settle, only until the screen has settled, if
        that's sooner (see SETTLE_QUIET).  Whether it had.  self.busy is
        the share of a processor taken in its last BUSY_WINDOW."""
        deadline = time.monotonic() + secs
        looks: collections.deque = collections.deque()
        look = 0.0
        load = collections.deque([(time.monotonic(), time.process_time())])
        try:
            while self.d.processes and time.monotonic() < deadline:
                QTest.qWait(20)
                if not self.d.processes:        # the last has quit: its screen may be gone
                    break
                if self.result["first_window"] is None and any(
                        rec.visible for p in list(self.d.processes) for rec in list(p.windows)):
                    self.result["first_window"] = round(time.monotonic() - self.launched, 2)
                now = time.monotonic()
                load.append((now, time.process_time()))
                while now - load[1][0] >= BUSY_WINDOW:
                    load.popleft()
                if settle and now >= look:
                    look = now + SETTLE_LOOK
                    picture = hash(self._picture_bytes()) if self._asking(now) else None
                    if settled(looks, now, picture):
                        return True
            return False
        finally:
            (t0, p0), (t1, p1) = load[0], load[-1]
            self.busy = round((p1 - p0) / (t1 - t0), 2) if t1 > t0 else None

    def _asking(self, now: float) -> bool:
        """Whether every process is asking for input: or macrt's own dialog
        is up, waiting for the user."""
        if self._host_dialogs():
            return True
        return all(asking(p.events, now) for p in list(self.d.processes))

    def _picture_bytes(self) -> bytes:
        image = self.picture().toImage()
        return image.constBits().asstring(image.sizeInBytes())

    def do(self, step: tuple) -> None:
        op, d = step[0], self.d
        screen = self._key_target() if op in "kKC" else d.screen_widget
        if op in ("w", "W"):
            self.wait(step[1], op == "W")
        elif op == "c":
            self.press(*step[1])
            self.wait(CLICK_HOLD)
            self.release(*step[1])
        elif op == "d":
            first, *rest = step[1]
            self.press(*first)
            for pt in rest:
                self.wait(DRAG_PACE)
                self.move(*pt)
            self.wait(DRAG_PACE)
            self.release(*rest[-1])
        elif op == "k":
            for ch in step[1]:
                if ch == "\n":
                    QTest.keyClick(screen, Qt.Key.Key_Return)
                elif ch == "\t":
                    QTest.keyClick(screen, Qt.Key.Key_Tab)
                else:
                    QTest.keyClicks(screen, ch)
                self.wait(TYPING_PACE)
        elif op == "K":
            key = key_named(step[1])
            if step[2] is None:
                QTest.keyClick(screen, key)
            else:
                QTest.keyPress(screen, key)
                self.wait(step[2])
                QTest.keyRelease(screen, key)
        elif op == "C":
            ch = step[1]
            key = key_named(ch.upper()) if ch.isascii() and ch.isalnum() else ch
            QTest.keyClick(screen, key, Qt.KeyboardModifier.ControlModifier)
        elif op == "s":
            self.shoot(step[1])
        elif op == "O":
            d.dismiss_alerts()

    def _key_target(self):
        """Where typing goes: into macrt's own dialog (Standard File's, an
        alert) while one is up, as the host's keyboard would; else to the
        Mac's screen."""
        for w in reversed(QApplication.topLevelWidgets()):
            if w.isVisible() and w.windowType() == Qt.WindowType.Dialog:
                return w.focusWidget() or w
        return self.d.screen_widget

    # -- the mouse, at Mac global coordinates ----------------------------
    def _target(self, h: int, v: int):
        g = self.d.global_from_mac(Point(v, h))
        w = QApplication.activePopupWidget() or self._held or QApplication.widgetAt(g)
        if w is None:
            w = self.d.screen_widget
        return w, g

    def press(self, h: int, v: int) -> None:
        w, g = self._target(h, v)
        self._held = w
        QTest.mousePress(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                         w.mapFromGlobal(g))

    def move(self, h: int, v: int) -> None:
        w, g = self._target(h, v)
        QTest.mouseMove(w, w.mapFromGlobal(g))

    def release(self, h: int, v: int) -> None:
        w, g = self._target(h, v)
        self._held = None
        try:
            QTest.mouseRelease(w, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier,
                               w.mapFromGlobal(g))
        except RuntimeError:            # the press closed the window it was in
            screen = self.d.screen_widget
            QTest.mouseRelease(screen, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier, screen.mapFromGlobal(g))

    def shoot(self, name: str) -> None:
        """A screenshot, name.png, of the picture."""
        self.picture().save(str(self.out / f"{name}.png"))
        self.shots[name] = f"{name}.png"

    def picture(self):
        """The Mac's screen as it looks, with macrt's own dialogs over it
        (Standard File's, alerts), kept on it, and the menu pulled down if
        one is."""
        screen = self.d.screen_widget
        pixmap = screen.grab()
        painter = QPainter(pixmap)
        shown = [w for w in QApplication.topLevelWidgets() if w.isVisible()]
        for kind in (Qt.WindowType.Dialog, Qt.WindowType.Popup):
            for w in shown:
                if w.windowType() != kind:
                    continue
                at = screen.mapFromGlobal(w.mapToGlobal(w.rect().topLeft()))
                if kind == Qt.WindowType.Dialog:
                    at.setX(max(0, min(at.x(), pixmap.width() - w.width())))
                    at.setY(max(0, min(at.y(), pixmap.height() - w.height())))
                painter.drawPixmap(at, w.grab())
        painter.end()
        return pixmap


def asking(events, now: float) -> bool:
    """Whether a process is asking for input, by its event queue: waiting
    for an event, or asking over and over whether there's one (or whether
    the button or a key is down), but not reading the clock (TickCount or
    Delay) as it does, as a splash screen that goes on by itself after a
    while does, whether it waits for events between or not."""
    if now - events.timed_at < ASKED:
        return False
    return (events.waiting or now - events.waited_at < ASKED
            or now - events.asked_at < ASKED)


def settled(looks: collections.deque, now: float, picture) -> bool:
    """Whether the screen has settled, at a look at it now: picture is
    what it shows (a hash of it), or None if not every process is asking
    for an event.  looks has the looks before, since they all were."""
    if picture is None:
        looks.clear()
        return False
    looks.append((now, picture))
    while now - looks[0][0] > 2 * SETTLE_QUIET:
        looks.popleft()
    if now - looks[0][0] < 2 * SETTLE_QUIET - SETTLE_LOOK:
        return False
    before = {p for t, p in looks if now - t > SETTLE_QUIET}
    since = {p for t, p in looks if now - t <= SETTLE_QUIET}
    return len(since) <= 2 and since == before


def _size(text: str) -> tuple[int, int]:
    size = _screen_size(text)
    if size is None:
        raise ValueError(f"{text!r} isn't a screen size, e.g. 640x480")
    return size


class SurveyDisplay(Display):
    """A Display that remembers every process launched on it, the ones
    that have quit too."""

    def __init__(self, seen: list, config: DisplayConfig):
        super().__init__(config)
        self.seen = seen

    def launch(self, *args, **kwargs):
        proc = super().launch(*args, **kwargs)
        self.seen.append(proc)
        return proc


def run_one(args) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    faulthandler.enable()               # a crash's Python stack, into the log
    if hasattr(signal, "SIGUSR1"):      # and every thread's, when it takes too long
        faulthandler.register(signal.SIGUSR1, all_threads=True)
    run = Run(args)
    try:
        result = run.go()
    except Exception as e:  # noqa: BLE001 - macrt itself failed; say so in the result
        traceback.print_exc()
        result = run.finish("internal error", f"{type(e).__name__}: {e}")
    (out / "result.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(f"{result['name']}: {result['outcome']}")
    sys.stdout.flush()
    sys.stderr.flush()
    # An application's thread can still be in its CPU's native code: leave
    # without the interpreter's tidying up, which could crash with it.
    os._exit(0 if result["outcome"] in ("running", "quit") else 1)


# =========================================================================
# Many runs, each in a process of its own
# =========================================================================
def _child_env() -> dict:
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = os.pathsep.join([root, *filter(None, [env.get("PYTHONPATH")])])
    return env


def time_limit(entry: dict, timeout: float | None = None) -> float:
    """How long entry may take in all: its own timeout, or timeout, or
    as long as its wait and steps take and SPARE_TIME more."""
    if entry.get("timeout"):
        return entry["timeout"]
    if timeout:
        return timeout
    wait = entry.get("wait")
    return (DEFAULT_WAIT if wait is None else wait) \
        + steps_time(parse_steps(entry.get("steps") or "")) + SPARE_TIME


def run_entry(entry: dict, out: Path, timeout: float | None = None) -> dict:
    """Run entry in a process of its own, into out/its name; its result."""
    folder = out / entry["name"]
    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    argv = [sys.executable, "-m", "macrt.survey", "one", *one_argv(entry, folder)]
    started = time.monotonic()
    with open(folder / "log.txt", "wb") as log:
        child = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=_child_env())
        try:
            status = child.wait(timeout=time_limit(entry, timeout))
        except subprocess.TimeoutExpired:
            status = None
            if hasattr(signal, "SIGUSR1"):
                child.send_signal(signal.SIGUSR1)       # its stacks, into the log
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            child.kill()
            child.wait()
    elapsed = round(time.monotonic() - started, 2)
    path = folder / "result.json"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        result = {"name": entry["name"], "file": entry["file"],
                  "app": [entry["app"]] if entry.get("app") else [],
                  "model": entry["model"], "processes": [], "system_errors": [],
                  "internal_errors": [], "screenshots": {
                      p.stem: p.name for p in sorted(folder.glob("*.png"))},
                  "outcome": "timeout" if status is None else "crashed",
                  "exit_status": status}
        if status is not None and status < 0:
            try:
                result["signal"] = signal.Signals(-status).name
            except ValueError:
                pass
    result["elapsed"] = elapsed
    path.write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def run_list(entries: list[dict], out: Path, jobs: int, timeout: float | None = None,
             echo=print, resume: bool = False) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    order = {e["name"]: i for i, e in enumerate(entries)}
    results = []
    if resume:
        results = [r for r in read_results(out) if r.get("name") in order]
        done = {r["name"] for r in results}
        entries = [e for e in entries if e["name"] not in done]
        echo(f"{len(done)} runs done before, {len(entries)} to run")
    with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {pool.submit(run_entry, e, out, timeout): e for e in entries}
        for n, future in enumerate(concurrent.futures.as_completed(futures), 1):
            result = future.result()
            results.append(result)
            echo(f"[{n}/{len(entries)}] {result['name']}: {result['outcome']} "
                 f"({result['elapsed']:.0f}s)")
    results.sort(key=lambda r: order.get(r["name"], len(order)))
    report = summarize(results)
    (out / "report.json").write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


# =========================================================================
# Reports
# =========================================================================
def read_results(out: Path) -> list[dict]:
    results = []
    for path in sorted(out.glob("*/result.json")):
        try:
            results.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
    return results


def _ranked(results: list[dict], key: str) -> list[dict]:
    """The names in each process's key counts, by how many runs have
    them, then how many times in all."""
    calls: dict[str, dict] = {}
    for r in results:
        made = collections.Counter()
        for p in r.get("processes", []):
            made.update(p.get(key, {}))
        for name, n in made.items():
            c = calls.setdefault(name, {"name": name, "runs": 0, "calls": 0, "examples": []})
            c["runs"] += 1
            c["calls"] += n
            if len(c["examples"]) < 5:
                c["examples"].append(r["name"])
    return sorted(calls.values(), key=lambda c: (-c["runs"], -c["calls"], c["name"]))


def summarize(results: list[dict]) -> dict:
    """What the runs did, in all: how many had each outcome, the
    unimplemented calls by how many runs made them, and the traps and
    Gestalt selectors asked for that macrt hasn't by how many runs asked."""
    outcomes = collections.Counter(r.get("outcome") for r in results)
    errors = collections.Counter(e["id"] for r in results for e in r.get("system_errors", [])
                                 if r.get("outcome") == "system error")
    no_window, at_a_dialog = [], {}
    for r in results:
        if r.get("outcome") != "running":
            continue
        shown = [[w for w in p.get("windows", []) if w.get("visible")]
                 for p in r.get("processes", [])]
        fronts = [windows[0] for windows in shown if windows]
        host = r.get("host_dialogs")
        if host:                                        # in front of every window
            at_a_dialog[r["name"]] = [host[-1]["title"], *host[-1]["text"]]
        elif not fronts:
            no_window.append(r["name"])
        elif fronts and fronts[0].get("text"):
            at_a_dialog[r["name"]] = fronts[0]["text"]
    timed = sorted((r for r in results if r.get("first_window") is not None),
                   key=lambda r: (-r["first_window"], r["name"]))
    busy = sorted((r for r in results
                   if r.get("outcome") == "running" and (r.get("busy") or 0) >= BUSY),
                  key=lambda r: (-r["busy"], r["name"]))
    outside = [[r["name"], len(r["outside_port_rect"])] for r in results
               if r.get("outside_port_rect")]
    return {
        "runs": len(results),
        "outcomes": {k: outcomes[k] for k in OUTCOMES if outcomes[k]},
        "by_outcome": {k: [r["name"] for r in results if r.get("outcome") == k]
                       for k in OUTCOMES if outcomes[k] and k not in ("running", "quit")},
        "unimplemented": _ranked(results, "unimplemented"),
        "missing_asked": _ranked(results, "missing_asked"),
        "system_error_ids": {str(k): n for k, n in sorted(errors.items())},
        "no_window": no_window,
        "at_a_dialog": at_a_dialog,
        "slowest_to_a_window": [[r["name"], r["first_window"]] for r in timed[:SLOWEST]],
        "busy": [[r["name"], r["busy"]] for r in busy],
        "outside_port_rect": outside,
    }


def format_report(report: dict, top: int = 20) -> str:
    lines = [f"{report['runs']} runs: " + ", ".join(
        f"{n} {k}" for k, n in report["outcomes"].items())]
    for outcome, names in report["by_outcome"].items():
        lines.append(f"  {outcome}: {', '.join(names)}")
    if report["system_error_ids"]:
        lines.append("system error IDs: " + ", ".join(
            f"{k} ({n})" for k, n in report["system_error_ids"].items()))
    for key, heading in (("unimplemented", "unimplemented, by how many runs call it:"),
                         ("missing_asked", "asked for but missing, by how many runs ask:")):
        if report.get(key):
            lines.append(heading)
            for c in report[key][:top]:
                lines.append(f"  {c['runs']:4} runs {c['calls']:8} calls  {c['name']}"
                             f"  ({', '.join(c['examples'])})")
    if report.get("no_window"):
        lines.append(f"running with no window: {', '.join(report['no_window'])}")
    if report.get("at_a_dialog"):
        lines.append("running at a dialog or alert:")
        for name, text in report["at_a_dialog"].items():
            said = " / ".join(text).replace("\n", " ")
            if len(said) > DIALOG_TEXT:
                said = said[:DIALOG_TEXT] + "…"
            lines.append(f"  {name}: {said}")
    if report.get("slowest_to_a_window"):
        lines.append("slowest to show a window: " + ", ".join(
            f"{name} ({secs:.1f} s)" for name, secs in report["slowest_to_a_window"]))
    if report.get("busy"):
        lines.append(f"busy at the end of their wait ({len(report['busy'])}): " + ", ".join(
            f"{name} ({share:.0%})" for name, share in report["busy"][:top]))
    if report.get("outside_port_rect"):
        lines.append(f"drew outside a portRect ({len(report['outside_port_rect'])}): "
                     + ", ".join(f"{name} ({n})" for name, n in report["outside_port_rect"]))
    return "\n".join(lines)


# =========================================================================
# Comparing two surveys
# =========================================================================
def image_difference(a, b) -> tuple[tuple[int, int, int, int], int] | str | None:
    """Where two QImages differ: the box (left, top, right, bottom) round
    the pixels that differ and how many do; a description if their sizes
    do; None if they're the same."""
    if a.size() != b.size():
        return f"size {a.width()}x{a.height()} -> {b.width()}x{b.height()}"
    a = a.convertToFormat(QImage.Format.Format_RGB32)
    b = b.convertToFormat(QImage.Format.Format_RGB32)
    w, h = a.width(), a.height()
    left, right, top, bottom, count = w, -1, h, -1, 0
    for y in range(h):
        ra, rb = _row(a, y, w), _row(b, y, w)
        if ra == rb:
            continue
        x = int.from_bytes(ra, "big") ^ int.from_bytes(rb, "big")
        n = 4 * w
        first = n - (x.bit_length() + 7) // 8           # the first byte that differs
        last = n - 1 - ((x & -x).bit_length() - 1) // 8  # and the last
        left, right = min(left, first // 4), max(right, last // 4)
        top, bottom = min(top, y), y
        pixels = array("I", x.to_bytes(n, "big"))
        count += len(pixels) - pixels.count(0)
    if bottom < 0:
        return None
    return (left, top, right + 1, bottom + 1), count


def _row(img, y: int, w: int) -> bytes:
    ptr = img.constScanLine(y)
    ptr.setsize(4 * w)
    return bytes(ptr)


def compare(old: Path, new: Path) -> list[str]:
    """How the runs in new differ from those of the same name in old."""
    a = {r["name"]: r for r in read_results(old)}
    b = {r["name"]: r for r in read_results(new)}
    lines = []
    for name in sorted(set(a) | set(b)):
        if name not in b:
            lines.append(f"{name}: only in {old}")
            continue
        if name not in a:
            lines.append(f"{name}: only in {new}")
            continue
        ra, rb = a[name], b[name]
        if ra.get("outcome") != rb.get("outcome"):
            lines.append(f"{name}: {ra.get('outcome')} -> {rb.get('outcome')}")
        ua, ub = _calls(ra), _calls(rb)
        if ua != ub:
            gone, added = sorted(ua - ub), sorted(ub - ua)
            lines.append(f"{name}: unimplemented" + (f" -{', -'.join(gone)}" if gone else "")
                         + (f" +{', +'.join(added)}" if added else ""))
        sa, sb = ra.get("screenshots", {}), rb.get("screenshots", {})
        for shot in sorted(set(sa) | set(sb)):
            if shot not in sb or shot not in sa:
                lines.append(f"{name}/{shot}.png: only in {old if shot in sa else new}")
                continue
            ia, ib = QImage(str(old / name / sa[shot])), QImage(str(new / name / sb[shot]))
            if ia.isNull() or ib.isNull():
                lines.append(f"{name}/{shot}.png: can't be read")
                continue
            diff = image_difference(ia, ib)
            if isinstance(diff, str):
                lines.append(f"{name}/{shot}.png: {diff}")
            elif diff is not None:
                box, count = diff
                lines.append(f"{name}/{shot}.png: {count} pixels differ in {box}")
    return lines


def _calls(result: dict) -> set[str]:
    return {n for p in result.get("processes", []) for n in p.get("unimplemented", {})}


# =========================================================================
# The command line
# =========================================================================
def parse_args(argv=None):
    ap = argparse.ArgumentParser(prog="python -m macrt.survey",
                                 description="Run Mac applications headless and report "
                                             "what they did.")
    sub = ap.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run each title a list names, several at a time")
    run.add_argument("list", help="the list of runs, JSON")
    run.add_argument("--out", required=True, help="the folder for the runs' results")
    run.add_argument("--jobs", type=int, default=max(1, (os.cpu_count() or 2) // 2),
                     help="how many to run at a time (default: half the processors)")
    run.add_argument("--timeout", type=float, metavar="SECS",
                     help="seconds each run may take in all, unless it says (default: "
                          f"its wait and steps take, and {SPARE_TIME:.0f} more)")
    run.add_argument("--only", action="append", default=[], metavar="NAME",
                     help="run only the runs of this name; repeat it for several")
    run.add_argument("--resume", action="store_true",
                     help="keep the runs already finished in OUT and run the rest")
    one = sub.add_parser("one", help="run one application, in this process")
    one.add_argument("file", nargs="+", help="the application, or disk images")
    one.add_argument("--out", required=True, help="the folder for its results")
    one.add_argument("--app", action="append", default=[],
                     help="which application on the disk image, or a document of one")
    one.add_argument("--model", choices=list(MODELS), default="plus")
    one.add_argument("--depth", type=int, choices=[1, 2, 4, 8, 16, 32])
    one.add_argument("--size", help="the screen's size, e.g. 640x480")
    one.add_argument("--row-bytes", type=int, metavar="N",
                     help="how many bytes apart the screen's rows are, as macrt's --row-bytes")
    one.add_argument("--memory", type=int, metavar="K")
    one.add_argument("--system", metavar="FILE")
    one.add_argument("--speed", type=float)
    one.add_argument("--wait", type=float, default=DEFAULT_WAIT,
                     help=f"seconds to wait before the steps (default {DEFAULT_WAIT:.0f})")
    one.add_argument("--settle", action="store_true",
                     help="end that wait sooner, once the screen has settled (W)")
    one.add_argument("--steps", default="", help="what to do then, as in a list")
    one.add_argument("--trace-traps", action="store_true")
    one.add_argument("--outside-port-rect", action="store_true",
                     help="note drawing that lands outside a port's portRect")
    report = sub.add_parser("report", help="sum up the runs in a folder again")
    report.add_argument("out")
    report.add_argument("--top", type=int, default=20,
                        help="how many unimplemented calls to show (default 20)")
    cmp = sub.add_parser("compare", help="what differs between two surveys' runs")
    cmp.add_argument("old")
    cmp.add_argument("new")
    args = ap.parse_args(argv)
    if args.command == "one":
        try:
            parse_steps(args.steps)
            if args.size:
                _size(args.size)
        except ValueError as e:
            ap.error(str(e))
        if args.depth not in (None, 1) and not MODELS[args.model].color:
            ap.error("a Mac Plus's screen is black and white: --depth needs --model ii or iix")
        if args.row_bytes is not None and not row_bytes_ok(args.row_bytes):
            ap.error(f"--{ROW_BYTES}")
    if args.command == "run" and args.jobs < 1:
        ap.error("--jobs is 1 or more")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.command == "one":
        return run_one(args)
    if args.command == "run":
        try:
            entries = load_list(args.list)
        except ValueError as e:
            print(f"macrt.survey: {e}", file=sys.stderr)
            return 2
        if args.only:
            missing = set(args.only) - {e["name"] for e in entries}
            if missing:
                print(f"macrt.survey: no runs named {', '.join(sorted(missing))}",
                      file=sys.stderr)
                return 2
            entries = [e for e in entries if e["name"] in args.only]
        report = run_list(entries, Path(args.out), args.jobs, args.timeout,
                          resume=args.resume)
        print(format_report(report))
        return 0
    if args.command == "report":
        report = summarize(read_results(Path(args.out)))
        print(format_report(report, args.top))
        return 0
    lines = compare(Path(args.old), Path(args.new))
    print("\n".join(lines) if lines else "no differences")
    return 1 if lines else 0


if __name__ == "__main__":
    raise SystemExit(main())
