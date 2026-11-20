"""
macrt.ui.display - Where Mac windows and menu bars appear on the host.

Two independent settings (DisplayConfig):

  host="desktop"     every Mac window is a frameless host window, so Mac
                     windows sit among the host's own windows.
  host="screen"      one host window holds an emulated Mac screen
                     (desktop pattern, rounded corners) with the windows
                     inside it.

  menu_bar="global"  one menu bar at the top of the Mac screen showing the
                     front app's menus.  With more than one app running it
                     switches with the front app and grows the System 7
                     Application menu at its right end.
  menu_bar="window"  each document window carries its app's menu bar under
                     its title bar, like a host app.  An app with no such
                     window open gets a small bar-only window.

The Display also routes host input.  Clicks become mouseDown events in the
owning app's queue (a click in a background app's window brings that app
forward instead), and the trackers answer blocking calls like MenuSelect.
"""

from __future__ import annotations

import sys
import time
import traceback
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable

from PyQt6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PyQt6.QtGui import QCursor, QGuiApplication, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QFileDialog, QMessageBox, QVBoxLayout, QWidget

from ..m68k.segload import Layout
from ..m68k.sound import WaveFile
from ..macfile import Folder, MacFile, VolumeFolder, fonts_beside, system_on
from ..process import GuestMain, Process
from ..toolbox.api import Toolbox
from ..toolbox import cursors
from ..toolbox.constants import (
    ASK_WDEF, WHIT_TO_PART, btnState, checkMark, convertClipboard, inDesk, inMenuBar,
    kApplicationMenuID, keyDown, keyUp, autoKey, mouseDown, mouseUp, osEvt, resumeFlag,
    suspendResumeMessage, wInMenuBar, wNoHit,
)
from ..toolbox.events import EventQueue, EventRecord
from ..toolbox.menus import Menu, MenuItem
from ..toolbox.qdraw import Region, ink, patCopy, qrect
from ..toolbox.scrap import needs_converting
from ..toolbox.screen import DESK, Screen
from ..toolbox.system import System
from ..toolbox.templates import place
from ..toolbox.types import Point, Rect
from ..toolbox.windows import WindowRecord, describe_proc
from . import chrome, theme
from .bridge import GuiBridge
from .filer import (APPLE_MENU_ID, DISK_MENU_ID, FILE_MENU_ID, SPECIAL_MENU_ID, AboutClassettaWindow,
                    AboutWindow, ContentViewerWindow, FilerMenus, FilerOptions,
                    FilerWindow, GetInfoWindow)
from .keymap import key_message, modifiers_from_qt
from .menubar import MenuBarWidget, MenuTracker, PopUpTracker
from .tracking import BoxTracker, DragTracker, GrowTracker, Tracker
from .window import AppBarWindow, MacWindowView

HOSTS = ("desktop", "screen")
MENU_BARS = ("global", "window")
ROW_BYTES = "row-bytes is an even number of bytes, less than 16384"


def row_bytes_ok(n: int) -> bool:
    """Whether n bytes will do as DisplayConfig.row_bytes: ROW_BYTES."""
    return not n & 1 and 0 < n < 0x4000


@dataclass
class DisplayConfig:
    host: str = "desktop"
    menu_bar: str = "global"
    screen_size: tuple[int, int] | None = None   # None: host screen / 640x480
    sticky_menus: bool = True
    application_menu: str = "auto"               # "auto" | "always" | "never"
    system_file: MacFile | None = None           # a real System file's resources, if wanted
    speed: float | None = None                   # times a Mac Plus; None: as fast as it goes
    model: str = "plus"                          # which Mac it says it is: system.MODELS
    depth: int | None = None                     # its screen's bits a pixel; None: the model's
    row_bytes: int | None = None                 # bytes from one row to the next; None: a row's
    disks: list = field(default_factory=list)    # disk images in the drives, in order
    sound: bool = False                          # play the Mac's sound on the host's output
    record_sound: str | None = None              # and write it to this WAV file

    def __post_init__(self):
        if self.host not in HOSTS:
            raise ValueError(f"host must be one of {HOSTS}")
        if self.menu_bar not in MENU_BARS:
            raise ValueError(f"menu_bar must be one of {MENU_BARS}")


class InputState:
    """Mouse and modifier state, written on the GUI thread, read by guests."""

    def __init__(self):
        self.down = False
        self.global_pos = QPoint(0, 0)
        self.mac_pos = Point(0, 0)
        self.press_time = 0.0
        self.presses = 0            # the mouse presses so far
        self.releases = 0           # of those, the ones whose release has been handled
        self.popup_presses = 0      # of those, the ones before the last pop-up menu closed
        self.modifiers = btnState


class MacScreen(QWidget):
    """host="screen": the emulated screen.  Windows are its children.  The
    desk is in the desk's pattern, and shows the screen buffer once a
    program draws on it."""

    def __init__(self, size: tuple[int, int], parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(*size)
        self.setProperty("macrt_role", "screen")
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.mirror: Screen | None = None

    def paintEvent(self, event):  # noqa: N802
        p = QPainter(self)
        mirror = self.mirror
        p.fillRect(self.rect(), mirror.desk_brush if mirror is not None
                   else ink(DESK, patCopy)[0])
        if mirror is not None and mirror.desk_drawn:
            r = event.rect()
            shown = mirror.desk.sect(Region.rect(Rect(r.top(), r.left(), r.bottom() + 1,
                                                      r.right() + 1)))
            if not shown.empty:
                box = shown.bbox
                p.setClipRegion(shown.to_qregion())
                p.drawImage(QPoint(box.left, box.top), mirror.shown.image(box))
                p.setClipping(False)
        # A global menu bar draws the top corners over these.
        top_left = theme.SCREEN_CORNER
        bottom_left = list(reversed(top_left))
        h, w = len(top_left), len(top_left[0])
        for x, y, bitmap in ((0, 0, top_left), (0, self.height() - h, bottom_left)):
            theme.draw_bitmap(p, x, y, bitmap)
            theme.draw_bitmap(p, self.width() - w, y, [row[::-1] for row in bitmap])


class ScreenWindow(QWidget):
    def __init__(self, display: Display, size: tuple[int, int]):
        super().__init__()
        self.display = display
        self.setWindowTitle(f"macrt — {size[0]}×{size[1]}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.screen_widget = MacScreen(size, self)
        layout.addWidget(self.screen_widget)

    def closeEvent(self, event):  # noqa: N802
        self.display.shutdown()
        event.accept()


_MOUSE = {QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick,
          QEvent.Type.MouseMove, QEvent.Type.MouseButtonRelease}
_KEYS = {QEvent.Type.KeyPress, QEvent.Type.KeyRelease}


class InputRouter(QObject):
    """Application-wide event filter: our widgets never see raw input.
    Each widget with a macrt_role tracks the mouse, so its moves come
    through with the button up too: GetMouse and low memory's Mouse
    follow it, as on a Mac."""

    def __init__(self, display: Display):
        super().__init__()
        self.display = display

    def eventFilter(self, obj, ev):  # noqa: N802
        t = ev.type()
        if t not in _MOUSE and t not in _KEYS:
            return False
        if not isinstance(obj, QWidget) or obj.property("macrt_role") is None:
            return False
        d = self.display
        if t in _KEYS:
            d.key_event(ev)
            return True
        if t != QEvent.Type.MouseMove and ev.button() != Qt.MouseButton.LeftButton:
            return True
        d.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(),
                       down={QEvent.Type.MouseButtonPress: True,
                             QEvent.Type.MouseButtonDblClick: True,
                             QEvent.Type.MouseButtonRelease: False}.get(t))
        if t == QEvent.Type.MouseMove:
            if d.tracker is not None:
                d.tracker.move(d.input.global_pos)
        elif t == QEvent.Type.MouseButtonRelease:
            d.mouse_up()
        else:
            d.mouse_down(obj)
        return True


def _report_gui_error(etype, value, tb) -> None:
    """PyQt aborts the whole runtime on an exception in a slot or event
    handler unless there's an excepthook, and every app would go with it."""
    print("macrt: error on the GUI thread", file=sys.stderr)
    traceback.print_exception(etype, value, tb)


class Display(QObject):
    def __init__(self, config: DisplayConfig | None = None):
        super().__init__()
        self.config = config or DisplayConfig()
        self.bridge = GuiBridge()
        self.input = InputState()
        self.processes: list[Process] = []       # front to back
        self.launch_order: list[Process] = []
        self.memory_sizes: dict[str, int] = {}  # files' memory sizes, as Get Info sets them
        self.views: dict[WindowRecord, MacWindowView] = {}
        self.app_bars: dict[Process, AppBarWindow] = {}
        self.filer_windows: list[FilerWindow] = []
        self.front_filer: FilerWindow | None = None
        self.filer_menus = FilerMenus(self)
        self.about_windows: list[AboutWindow] = []
        self.info_windows: list[GetInfoWindow] = []
        self.viewer_windows: list[ContentViewerWindow] = []
        self.front_aux_window: AboutWindow | GetInfoWindow | ContentViewerWindow | None = None
        self.tracker: Tracker | MenuTracker | None = None
        self.body_font = theme.body_font()
        self._swallow_release = False
        self._last_clicked: QWidget | None = None
        self._shut_down = False
        self.restarting = False    # Special > Restart: run the original command again after
        self.system_errors: list[tuple[str, int, str]] = []    # (app, ID, what happened)
        self._alerts: list[QMessageBox] = []
        self.file_dialogs: list = []              # Standard File's, open now
        self.cursors: dict[Process, object] = {}  # each one's cursor's shape (toolbox.cursors)
        self._obscured: Process | None = None     # whose cursor is hidden till the mouse moves
        self._qcursors: dict[object, QCursor] = {}
        self.print_dialogs: list = []             # Page Setup and Print, open now
        self.disk_dialogs: list = []              # which disk goes in a drive, open now
        self.color_dialogs: list = []             # the Color Picker's, open now
        self.edition_dialogs: list = []           # the Edition Manager's options, open now
        self._app_bar_timer = QTimer(self, singleShot=True, interval=250)
        self._app_bar_timer.timeout.connect(self._update_app_bars)

        self.screen_widget: MacScreen | None = None
        self.host_window: ScreenWindow | None = None
        self.origin = QPoint(0, 0)
        if self.config.host == "screen":
            self.size = self.config.screen_size or (640, 480)
        else:
            avail = QGuiApplication.primaryScreen().availableGeometry()
            self.origin = avail.topLeft()
            self.size = self.config.screen_size or (avail.width(), avail.height())
        has_global_bar = self.config.menu_bar == "global"
        disks = self.config.disks
        disk_system = system_on(disks[0]) if disks else None
        # The System file the emulator runs: the one asked for, or the
        # disk's, or none when macrt's own built-in stands in for it.
        self.running_system_file = self.config.system_file or disk_system
        self.sound = self._sound_outputs()
        self.system = System(self.size, desk_top=theme.MENU_BAR_HEIGHT if has_global_bar else 0,
                             menu_bar_height=theme.MENU_BAR_HEIGHT,
                             system_file=self.config.system_file,
                             disk_system=disk_system, disk_fonts=fonts_beside(disk_system),
                             speed=self.config.speed, model=self.config.model,
                             sound=self.sound, depth=self.config.depth,
                             row_bytes=self.config.row_bytes)
        for disk in disks:
            self.system.files.mount_folder(VolumeFolder(disk))
        # The window and the application-wide filter come once nothing
        # above can fail: left behind for the garbage collector, the window
        # is destroyed under the filter, and that takes the host down.
        if self.config.host == "screen":
            self.host_window = ScreenWindow(self, self.size)
            self.screen_widget = self.host_window.screen_widget
        self._router = InputRouter(self)
        QApplication.instance().installEventFilter(self._router)
        self.system.screen.order = lambda: self.processes
        if self.screen_widget is not None:
            self.screen_widget.mirror = self.system.screen
            self.system.screen.on_desk = lambda box: self.bridge.post(self._desk_drawn, box)
        self.system.screen.on_layout = lambda: self.bridge.post(self._laid_out)
        # Keys let go of in another application never come up here.
        QGuiApplication.instance().applicationStateChanged.connect(self._app_state)

        self.global_bar: MenuBarWidget | None = None
        if has_global_bar:
            if self.screen_widget is not None:
                self.global_bar = MenuBarWidget(self, self.screen_widget, rounded=True)
                self.global_bar.setGeometry(0, 0, self.size[0], theme.MENU_BAR_HEIGHT)
            else:
                self.global_bar = MenuBarWidget(self, top_level=True)
                self.global_bar.setGeometry(self.origin.x(), self.origin.y(),
                                            self.size[0], theme.MENU_BAR_HEIGHT)
            # The bar the Finder would have left in the screen buffer, for
            # a game that draws over it with no InitMenus (MacLanding).
            self.system.screen.bar_drawn(self.global_bar.image())
        # Only once it's built: what goes wrong building it isn't in a slot
        if sys.excepthook is sys.__excepthook__:
            sys.excepthook = _report_gui_error

    # =====================================================================
    # Lifecycle
    # =====================================================================
    def show(self) -> None:
        if self.host_window is not None:
            self.host_window.show()
            self.screen_widget.setFocus()
        if self.global_bar is not None:
            self.global_bar.show()

    def launch(self, name: str, main: GuestMain | None, app: MacFile | None = None,
               folder: Folder | None = None, trace: bool = False,
               page_option: int = 0, memory: int | None = None,
               as_panel: bool = True, code0: Layout | None = None,
               finder_info: bytes | None = None) -> Process:
        """Start a guest.  app is its application file, if it has one: the
        resource file GetNewWindow, GetMenu and friends read from.  With
        no Python main, app's own 68k code runs; trace logs its traps.
        page_option is Launch's, for CurPageOption.  memory, in bytes, is
        in place of the size its SIZE resource asks for: app's memory size,
        as Get Info sets it, so it's the size of app's later launches too
        (a control panel launching its own CODE as an application).  A
        control panel that's an application as well is hosted as a panel
        unless as_panel is False.  code0 is the layout of app's CODE 0, if
        it's been read.  finder_info is the Finder information it's
        launched with, if not its documents'."""
        if app is not None:
            if memory:
                self.memory_sizes[app.key] = memory
            memory = self.memory_sizes.get(app.key)
        proc = Process(self.system, name, main,
                       EventQueue(lambda: (self.system.input.position, self.input.modifiers)),
                       app=app, folder=folder, page_option=page_option, memory=memory,
                       as_panel=as_panel, code0=code0, finder_info=finder_info)
        if proc.runner is not None:
            proc.runner.trace = trace
        self.launch_order.append(proc)
        self.processes.append(proc)
        self.switch_to(proc)
        proc.start(Toolbox(self, proc), on_exit=lambda p: self.bridge.post(self._process_exited, p))
        return proc

    def launch_application(self, app: MacFile, page_option: int = 0,
                           code0: Layout | None = None,
                           finder_info: bytes | None = None) -> Process | str:
        """Launch an application file for another application (the Launch
        trap): the new process, or why it couldn't start.  A control panel
        with an application's code runs it.  code0 is the layout of its
        CODE 0, if the launching application has read it, and finder_info
        the Finder information it passes on."""
        try:
            return self.launch(app.name, None, app=app, page_option=page_option, as_panel=False,
                               code0=code0, finder_info=finder_info)
        except (MemoryError, ValueError) as e:
            return str(e)

    def _sound_outputs(self) -> list:
        """Where the Mac's speaker goes: the host's sound output, a WAV file."""
        outputs: list = []
        if self.config.sound:
            from .audio import HostSpeaker      # only now: QtMultimedia has its say as it loads
            speaker = HostSpeaker.open(self)
            if speaker is not None:
                outputs.append(speaker)
        if self.config.record_sound:
            outputs.append(WaveFile(self.config.record_sound))
        return outputs

    def shutdown(self, quit_app: bool = True) -> None:
        """Close every window, ask each application to quit, and only
        then let the Qt application go.  An application whose quit
        raises must not keep the rest of the sequence from happening."""
        if self._shut_down:
            return
        self._shut_down = True
        self.bridge.closed = True
        widgets = [*self.views.values(), *self.app_bars.values(), *self.filer_windows,
                   *self.about_windows, *self.info_windows, *self.viewer_windows,
                   self.global_bar, self.host_window, *self._alerts, *self.file_dialogs,
                   *self.print_dialogs, *self.disk_dialogs, *self.color_dialogs,
                   *self.edition_dialogs]
        for w in widgets:
            if w is not None:
                w.hide()
                w.deleteLater()
        self.system.screen.close()
        for out in self.sound:
            out.close()
        if self.tracker is not None:
            self.tracker.cancel()
        app = QApplication.instance()
        app.removeEventFilter(self._router)
        self._app_bar_timer.stop()
        for proc in list(self.processes):
            try:
                proc.quit()
            except Exception:  # noqa: BLE001 - one app's failure must not skip the rest
                traceback.print_exc()
        if quit_app:
            app.quit()

    def join(self, timeout: float = 2.0) -> None:
        """After shutdown, wait up to timeout seconds in all for the
        applications' threads to stop: one still in its CPU's native code
        as the interpreter exits can crash it.  Once they all have, the
        processor's clock stops too."""
        deadline = time.monotonic() + timeout
        threads = [p.thread for p in list(self.launch_order) if p.thread is not None]
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        machine = getattr(self.system, "machine", None)
        if machine is not None and not any(t.is_alive() for t in threads):
            machine.close()

    @property
    def shutting_down(self) -> bool:
        return self._shut_down

    def guest_fault(self, proc: Process, error: Exception) -> None:
        """Something in an app's records broke the GUI side (a bad handle in
        its WindowRecord, say).  Stop that app, as a system error would,
        and keep the others running."""
        if proc.faulted:
            return
        proc.faulted = True
        print(f"macrt: stopping {proc.name}: its records are damaged ({error!r})",
              file=sys.stderr)
        proc.kill()

    def report_system_error(self, proc: Process, error_id: int, message: str) -> None:
        """An application's code stopped with a system error (called on its
        thread).  Say so, as the Mac's bomb alert did; only that
        application stops."""
        self.system_errors.append((proc.name, error_id, message))
        self.bridge.post(self._show_system_error, proc.name, error_id, message)

    def _show_system_error(self, name: str, error_id: int, message: str) -> None:
        self.show_alert(QMessageBox.Icon.Critical, "System Error",
                        f"Sorry, a system error occurred in \u201c{name}\u201d, "
                        f"so it has quit.\n\nID = {error_id}", message)

    def notify(self, name: str, message: str,
               dismissed: Callable[[], None] | None = None) -> None:
        """A Notification Manager request with a message (called on the
        application's thread): an alert that doesn't stop anyone, and
        dismissed called as it's dismissed."""
        self.bridge.post(self.show_alert, QMessageBox.Icon.Information, name, message, "",
                         dismissed)

    def show_alert(self, icon: QMessageBox.Icon, title: str, text: str,
                   detail: str = "", dismissed: Callable[[], None] | None = None) -> None:
        """A host alert that leaves the applications running."""
        if self._shut_down:
            return
        box = QMessageBox(icon, title, text, QMessageBox.StandardButton.Ok, self.host_window)
        if detail:
            box.setDetailedText(detail)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.finished.connect(lambda _: self._alert_closed(box))
        if dismissed is not None:
            box.finished.connect(lambda _: dismissed())
        self._alerts.append(box)
        box.show()

    def ask_choice(self, title: str, text: str,
                   choices: list[tuple[str, Callable[[], None]]],
                   detail: str = "") -> QMessageBox:
        """A host question with one button per choice, shown as show_alert
        shows an alert and recorded beside them; the button pressed calls
        its callable, and every other way of dismissing it (Cancel, the
        close box, Escape) keeps the Cancel semantics: none of the
        callables run, so nothing happens."""
        if self._shut_down:
            return None
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Cancel, self.host_window)
        if detail:
            box.setDetailedText(detail)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.finished.connect(lambda _: self._alert_closed(box))
        for label, answered in choices:
            button = box.addButton(label, QMessageBox.ButtonRole.AcceptRole)
            button.clicked.connect(lambda _=None, a=answered: a())
        self._alerts.append(box)
        box.show()
        return box

    def ask_question(self, title: str, text: str, answered: Callable[[bool], None],
                     detail: str = "") -> QMessageBox:
        """A host question with Yes and No, shown as show_alert shows an
        alert and recorded beside them; `answered` gets which button the
        user pressed.  Dismissing it without pressing either (the
        shutdown's dismiss) answers neither way, so nothing runs."""
        if self._shut_down:
            return None
        box = QMessageBox(QMessageBox.Icon.Question, title, text,
                          QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                          self.host_window)
        if detail:
            box.setDetailedText(detail)
        box.setWindowModality(Qt.WindowModality.NonModal)
        box.finished.connect(lambda _: self._alert_closed(box))
        box.button(QMessageBox.StandardButton.Yes).clicked.connect(
            lambda _: answered(True))
        box.button(QMessageBox.StandardButton.No).clicked.connect(
            lambda _: answered(False))
        self._alerts.append(box)
        box.show()
        return box

    def dismiss_alerts(self) -> None:
        """OK every host alert that's up, as the user would."""
        for box in list(self._alerts):
            box.accept()

    def _alert_closed(self, box: QMessageBox) -> None:
        if box in self._alerts:
            self._alerts.remove(box)
        if not self._alerts and not self.processes and not self.filer_windows:
            self.shutdown()             # the last app crashed; its alert kept the display up

    def _process_exited(self, proc: Process) -> None:
        if proc not in self.processes:
            return
        if self.tracker is not None and self.tracker.owner is proc:
            self.tracker.cancel()                   # nothing waits for it now
        was_front = self.front_process is proc
        for rec in list(proc.windows):
            self.destroy_view(rec)
        with self.system.lock:
            proc.windows.clear()
            proc.close()
        bar = self.app_bars.pop(proc, None)
        if bar is not None:
            self._retire(bar)
        self.processes.remove(proc)
        self.launch_order.remove(proc)
        self.cursors.pop(proc, None)
        if self._obscured is proc:
            self._obscured = None
        if not self.processes:
            if not self._alerts and not self.filer_windows:
                self.shutdown()
            else:
                self.refresh_bars()         # the Filer windows carry on without it
            return
        if was_front:
            self.switch_to(self.processes[0], force=True)
        self.refresh_bars()

    # =====================================================================
    # Coordinates.  Mac global coordinates have (0,0) at the top-left of
    # the Mac screen, under the menu bar.
    # =====================================================================
    @property
    def screen_bounds(self) -> Rect:
        return self.system.screen_bits.bounds

    def mac_from_global(self, g: QPoint) -> Point:
        if self.screen_widget is not None:
            g = self.screen_widget.mapFromGlobal(g)
            return Point(g.y(), g.x())
        return Point(g.y() - self.origin.y(), g.x() - self.origin.x())

    def global_from_mac(self, pt: Point) -> QPoint:
        if self.screen_widget is not None:
            return self.screen_widget.mapToGlobal(QPoint(pt.h, pt.v))
        return QPoint(self.origin.x() + pt.h, self.origin.y() + pt.v)

    def parent_pos(self, pt: Point) -> QPoint:
        """Where a hosted widget's top-left goes for a Mac global point."""
        if self.screen_widget is not None:
            return QPoint(pt.h, pt.v)
        return QPoint(self.origin.x() + pt.h, self.origin.y() + pt.v)

    def mac_from_parent_pos(self, pos: QPoint) -> Point:
        if self.screen_widget is not None:
            return Point(pos.y(), pos.x())
        return Point(pos.y() - self.origin.y(), pos.x() - self.origin.x())

    @property
    def desk_top(self) -> int:
        """Where the desk starts: under the global menu bar, if there is one."""
        return theme.MENU_BAR_HEIGHT if self.global_bar is not None else 0

    def place_window(self, bounds: Rect, position: int, proc_id: int,
                     parent: Rect | None = None) -> Rect:
        """Where a WIND, DLOG or ALRT positioning word puts a window."""
        d = describe_proc(proc_id)
        bar = self.config.menu_bar == "window" and chrome.supports_menu_bar(d.kind)
        f = chrome.Frame(kind=d.kind, corner=d.corner, grow=d.grow, zoom=d.zoom, menu_bar=bar)
        top = chrome.insets(f)[1]
        w, h = self.size
        return place(bounds, position, Rect(self.desk_top, 0, h, w), parent, title_bar=top)

    def standard_state(self, rec: WindowRecord) -> Rect:
        """Default zoomed size: the desk, 3 pixels in."""
        f = chrome.Frame.for_window(rec, menu_bar=self.config.menu_bar == "window")
        l, t, r, b = chrome.insets(f)
        s = chrome.shadow(f)
        w, h = self.size
        return Rect(self.desk_top + t + 3, l + 3, h - b - s - 3, w - r - s - 3)

    def place_aux_window(self, win, over: FilerWindow | None = None) -> None:
        """Centre `win` on `over` in Mac coordinates — or on the screen when
        there is none — and keep it on the screen: its top is never behind
        the menu bar, and a window too tall for the space below the bar
        keeps its bottom while that keeps the top below the bar."""
        screen = self.screen_bounds
        if over is not None:
            o = over.mac_origin()
            v = o.v + (over.height() - win.height()) // 2
            h = o.h + (over.width() - win.width()) // 2
        else:
            v = self.desk_top + (screen.bottom - self.desk_top - win.height()) // 2
            h = (screen.right - win.width()) // 2
        bottom_keep = screen.bottom - 8 - win.height()
        # A window taller than the desk clips at the bottom; one taller than
        # the screen clips at the bottom too — never at the bar.
        v = min(max(v, self.desk_top), max(self.desk_top, bottom_keep))
        h = min(max(h, 4), max(4, screen.right - 8 - win.width()))
        win.place(Point(v, h))

    # =====================================================================
    # Processes
    # =====================================================================
    @property
    def front_process(self) -> Process | None:
        return self.processes[0] if self.processes else None

    def is_front(self, proc: Process) -> bool:
        return self.front_process is proc

    def switch_to(self, proc: Process, force: bool = False) -> None:
        """Make proc the front process, like clicking its window under MultiFinder."""
        old = self.front_process
        if proc not in self.processes or (old is proc and not force and self.front_filer is None):
            return                      # an app that has just quit can't come forward
        if self.front_filer is not None:
            self.filer_front(None)      # an application is coming forward
        else:
            # No Filer window was front; an application is coming forward.
            self.front_aux_window = None
        self.processes.remove(proc)
        self.processes.insert(0, proc)
        if proc.hidden:
            proc.hidden = False
            self._sync_process(proc)
        if old is not None and old is not proc:
            fw = old.front_window()
            if fw is not None and fw.hilited:
                fw.hilited = False
                self.sync(fw)
                old.events.post_activate(fw, False)
            old.events.post(EventRecord(osEvt, suspendResumeMessage << 24, 0,
                                        self.system.input.position, self.input.modifiers))
        fw = proc.front_window()
        if fw is not None and not fw.hilited:
            fw.hilited = True
            self.sync(fw)
            proc.events.post_activate(fw, True)
        message = (suspendResumeMessage << 24) | resumeFlag
        if needs_converting(proc.system, proc.world, self.bridge):
            message |= convertClipboard
        proc.events.post(EventRecord(osEvt, message, 0, self.system.input.position,
                                     self.input.modifiers))
        self.restack()
        self.focus(fw)
        if fw is not None:
            view = self.views.get(fw)
            if view is not None:
                view.raise_()
                if view.isWindow():
                    view.activateWindow()
        self.refresh_bars()
        self._show_cursor(proc)

    def _sync_process(self, proc: Process) -> None:
        for rec in proc.windows:
            self.sync(rec)

    def z_order(self):
        """(process, window) pairs, front to back, visible windows only."""
        for proc in self.processes:
            if proc.hidden:
                continue
            for rec in proc.windows:
                if rec.visible:
                    yield proc, rec

    def restack(self) -> None:
        self.system.screen.moved()
        for proc in reversed(self.processes):
            bar = self.app_bars.get(proc)
            if bar is not None:
                bar.raise_()
            for rec in reversed(proc.windows):
                view = self.views.get(rec)
                if view is not None:
                    view.raise_()
        if self.global_bar is not None and self.screen_widget is not None:
            self.global_bar.raise_()

    def focus(self, rec: WindowRecord | None) -> None:
        """Give the keyboard to rec's window: an application's, which takes
        the menu bar from a Filer window that had it."""
        if rec is not None and self.front_filer is not None:
            self.filer_front(None)
        if self.screen_widget is not None:
            self.screen_widget.setFocus()
        elif rec is not None and rec in self.views and self.views[rec].isVisible():
            self.views[rec].activateWindow()

    # =====================================================================
    # Window views
    # =====================================================================
    def create_view(self, rec: WindowRecord) -> MacWindowView:
        view = MacWindowView(self, rec, menu_bar=self.config.menu_bar == "window",
                             parent=self.screen_widget)
        self.views[rec] = view
        if self.screen_widget is None:
            view.setCursor(self._qcursor(self._cursor_of(rec.process)))
        view.sync()
        self._app_bar_timer.start()
        return view

    def destroy_view(self, rec: WindowRecord) -> None:
        view = self.views.pop(rec, None)
        if view is None:
            return
        self._retire(view)
        self._app_bar_timer.start()

    def open_filer(self, volume, path: tuple[str, ...] = (),
                    options: FilerOptions | None = None) -> FilerWindow:
        """A Filer window over one folder of a disk image or host folder, or the one
        already open there: brought to the front instead."""
        for win in self.filer_windows:
            if win.volume is volume and win.path == path:
                win.raise_()
                win.activateWindow()
                win.show()
                self.filer_front(win)
                return win
        win = FilerWindow(self, volume, path, options, parent=self.screen_widget)
        self.filer_windows.append(win)
        i = len(self.filer_windows) - 1
        w, h = self.size
        x = min(400 + 26 * i, max(4, w - win.width() - 8))
        y = min(self.desk_top + 70 + 26 * i, max(self.desk_top, h - win.height() - 8))
        win.place(Point(y, x))
        win.show()
        if win.isWindow():
            win.activateWindow()
        else:
            win.tree.setFocus()
        self.filer_front(win)
        return win

    def filer_front(self, win: FilerWindow | None) -> None:
        """Make win the Filer's frontmost window, or none: while one is
        frontmost the bar shows the Filer's menus instead of a process's."""
        if win is not None and win not in self.filer_windows:
            return
        # No About, Get Info or Viewer window is frontmost together with the
        # Filer.  It is raised above them below; lowering the auxiliary
        # windows is not an option — on the host's desktop that buries them
        # behind every other window instead of merely behind the Filer.
        self.front_aux_window = None
        if win is self.front_filer:
            return

        old_filer = self.front_filer
        old_process = self.front_process
        self.front_filer = win

        # If showing the Filer, hide the front app
        if win is not None:
            win.raise_()
            if win.isWindow():
                win.activateWindow()
            else:
                win.tree.setFocus()
            # Hide the front process if it exists
            if old_process is not None:
                fw = old_process.front_window()
                if fw is not None and fw.hilited:
                    fw.hilited = False
                    self.sync(fw)
                    old_process.events.post_activate(fw, False)
                old_process.events.post(EventRecord(osEvt, suspendResumeMessage << 24, 0,
                                                    self.input.mac_pos, self.input.modifiers))
        # If hiding the Filer, show the front process
        else:
            # The application coming forward is raised by switch_to()'s
            # restack(); the Filer's own windows are not to be buried —
            # on the host's desktop that puts them behind every window,
            # instead of merely behind the application's.
            if old_process is not None and old_process not in self.processes:
                # Process might have been quit
                pass
            elif old_process is not None and old_process is not self.front_process:
                self.switch_to(old_process)
        
        self.refresh_bars()

    def filer_blurred(self, win: FilerWindow, new: QWidget | None) -> None:
        """A Filer window has lost the focus.  It is frontmost no more only
        if what took it is an application's window: the desk, the menu bar,
        a host alert and the Filer's own windows are all still its own, and
        only an application taking the keyboard gives the bar back."""
        if self.front_filer is not win:
            return
        w = new
        while w is not None and not isinstance(w, (MacWindowView, AppBarWindow)):
            w = w.parentWidget()
        if w is not None:
            self.filer_front(None)

    def close_filer(self, win: FilerWindow) -> None:
        """Close a Filer window.  With no applications left running and
        no Filer windows open, macrt quits: they were all that was up."""
        if win not in self.filer_windows:
            return
        self.filer_windows.remove(win)
        
        # Eject/unmount volume if applicable (skip Macintosh HD boot volume)
        vol = getattr(win, "volume", None)
        if isinstance(vol, VolumeFolder):
            image_name = vol.volume.name
            # Find and unmount in FileSystem if not boot volume
            fs = self.system.files
            for v in list(fs.volumes):
                if v.name == image_name and v is not fs.boot:
                    try:
                        fs.unmount(v)
                        # Clean up cache in macfile
                        from .. import macfile
                        macfile._VOLUME_CACHE.pop(str(v.store.source) if hasattr(v.store, "source") and v.store.source else "", None)
                    except Exception:
                        pass
        
        self._retire(win)
        if self.front_filer is win:
            self.front_filer = self.filer_windows[-1] if self.filer_windows else None
            self.refresh_bars()
        if not self.processes and not self._alerts and not self.filer_windows:
            self.shutdown()

    def _retire(self, widget: MacWindowView | AppBarWindow | FilerWindow) -> None:
        """Hide a window view, bar window or Filer window for good,
        first ending any tracking that's using it."""
        t = self.tracker
        tracked = getattr(t, "view", None) or getattr(t, "widget", None)
        if tracked is widget or (isinstance(t, MenuTracker)
                                 and getattr(widget, "bar", None) is t.bar):
            t.cancel()
        widget.hide()
        widget.deleteLater()

    def sync(self, rec: WindowRecord) -> None:
        view = self.views.get(rec)
        if view is not None:
            view.sync()
            self._app_bar_timer.start()
        self.system.screen.moved()

    def _laid_out(self) -> None:
        """Show the views of windows a program has made visible by setting
        their visible flags itself, as Cap'n Magneto does, and hide those
        it's made invisible."""
        for rec, view in list(self.views.items()):
            if view.isHidden() == (rec.visible and not rec.process.hidden):
                view.reshape()

    def reshape(self, rec: WindowRecord) -> None:
        """The window's own WDEF has shaped or drawn it."""
        view = self.views.get(rec)
        if view is not None:
            view.reshape()
            self._app_bar_timer.start()
        self.system.screen.moved()

    def repaint_content(self, rec: WindowRecord) -> None:
        view = self.views.get(rec)
        if view is not None:
            view.content.update()

    def _desk_drawn(self, box: Rect) -> None:
        """A program drew on the desk, in box (global), or over the menu bar."""
        if self.screen_widget is not None:
            self.screen_widget.update(qrect(box))
            if self.global_bar is not None and box.top < self.desk_top:
                self.global_bar.update(qrect(box))

    def view_moved(self, view: MacWindowView) -> None:
        """The host moved a window (e.g. a Wayland system move)."""
        l, t = view.content_offset()
        o = view.mac_origin()
        rec = view.rec
        with self.system.lock:
            rec.content = rec.content.moved_to(o.h + l, o.v + t)
        self.system.screen.moved()
        if rec.fit_backing(moved=True):
            with self.system.lock:
                rec.update_pending = True
            rec.process.events.wake()

    def find_window(self, proc: Process, pt: Point) -> tuple[int, WindowRecord | None]:
        if (self.global_bar is not None and pt.v < theme.MENU_BAR_HEIGHT
                and 0 <= pt.h < self.size[0]):
            return inMenuBar, None
        for owner, rec in self.z_order():
            view = self.views.get(rec)
            if view is None:
                continue
            if rec.wdef_shaped:
                # Its structure region is the window; its WDEF says which part.
                if not view.regions()[0].contains(pt):
                    continue
                return (ASK_WDEF, rec) if owner is proc else (inDesk, None)
            o = view.structure_origin()
            code = view.hit(QPoint(pt.h - o.h, pt.v - o.v))
            if code == wNoHit:
                continue
            if owner is not proc:
                return inDesk, None
            if code == wInMenuBar:
                return inMenuBar, None
            return WHIT_TO_PART[code], rec
        bar = self.app_bars.get(proc)
        if bar is not None and bar.isVisible():
            local = bar.mapFromGlobal(self.global_from_mac(pt))
            if bar.bar.geometry().contains(local):
                return inMenuBar, None
        return inDesk, None

    # =====================================================================
    # Trackers
    # =====================================================================
    def _start_tracker(self, tracker, owner: Process | None = None) -> Future:
        """Start tracker, for owner if an application waits for it."""
        if self.tracker is not None:
            self.tracker.cancel()
        tracker.owner = owner
        self.tracker = tracker
        try:
            tracker.start()
        except BaseException:
            self.tracker = None     # or it would swallow every click from now on
            raise
        return tracker.future

    def tracker_finished(self, tracker) -> None:
        if self.tracker is tracker:
            self.tracker = None

    def track_box(self, rec: WindowRecord, part: int) -> Future:
        return self._start_tracker(BoxTracker(self, Future(), self.views[rec], part),
                                   rec.process)

    def track_drag(self, rec: WindowRecord, bounds: Rect, start: Point) -> Future:
        view = self.views[rec]
        return self._start_tracker(DragTracker(self, Future(), view, view.structure_origin(),
                                               self._drag_limit(bounds), start), rec.process)

    def track_grow(self, rec: WindowRecord, limits: Rect, start: Point) -> Future:
        return self._start_tracker(GrowTracker(self, Future(), self.views[rec], limits, start),
                                   rec.process)

    def end_grow_preview(self, rec: WindowRecord) -> None:
        """The guest didn't take the size GrowWindow gave it."""
        view = self.views.get(rec)
        if view is not None and view.preview_size is not None:
            view.preview_size = None
            view.relayout()

    def track_menu(self, proc: Process, pt: Point, ask=None) -> Future | int:
        bar = self.bar_at(pt) or self.default_bar(proc)
        if bar is None or bar.process is not proc:
            return 0
        proc.last_bar = bar
        return self._start_tracker(MenuTracker(self, bar, Future(), self.config.sticky_menus,
                                               ask), proc)

    def track_popup(self, proc: Process, menu: Menu, where: Point, item: int,
                    ask=None) -> Future:
        """PopUpMenuSelect: menu's item `item` at where, global."""
        return self._start_tracker(PopUpTracker(self, proc, menu, where, item, Future(),
                                                self.config.sticky_menus, ask), proc)

    def _drag_limit(self, bounds: Rect | None) -> Rect:
        screen = self.screen_bounds
        top = self.desk_top + 4
        if bounds is None:
            return Rect(top, 4, screen.bottom - 4, screen.right - 4)
        return Rect(max(bounds.top, top), bounds.left, bounds.bottom, bounds.right)

    # =====================================================================
    # Menu bars
    # =====================================================================
    def bars_for(self, proc: Process | FilerMenus) -> list[MenuBarWidget]:
        bars = []
        if self.global_bar is not None and self.global_bar.process is proc:
            bars.append(self.global_bar)
        if proc is self.filer_menus:
            return bars             # Filer windows all share the one bar
        for rec in proc.windows:
            view = self.views.get(rec)
            if view is not None and view.bar is not None:
                bars.append(view.bar)
        if proc in self.app_bars:
            bars.append(self.app_bars[proc].bar)
        return bars

    def default_bar(self, proc: Process | FilerMenus) -> MenuBarWidget | None:
        if self.global_bar is not None:
            return self.global_bar if self.global_bar.process is proc else None
        if proc is self.filer_menus:
            return None             # Filer windows share the one bar, above
        # A dialog in front has no bar of its own; use the next window's.
        for rec in proc.windows:
            view = self.views.get(rec)
            if rec.visible and view is not None and view.bar is not None:
                return view.bar
        bar = self.app_bars.get(proc)
        return bar.bar if bar is not None else None

    def bar_at(self, pt: Point) -> MenuBarWidget | None:
        g = self.global_from_mac(pt)

        def over(widget):
            return widget.isVisible() and widget.rect().contains(widget.mapFromGlobal(g))

        if self.global_bar is not None and over(self.global_bar):
            return self.global_bar
        for _, rec in self.z_order():
            view = self.views.get(rec)
            if view is not None and over(view):
                return view.bar if view.bar is not None and over(view.bar) else None
        for bar in self.app_bars.values():
            if over(bar.bar):
                return bar.bar
        return None

    def hilite_menu(self, proc: Process, menu_id: int) -> None:
        bars = self.bars_for(proc)
        if menu_id == 0:
            for bar in bars:
                bar.set_hilite(0)
            return
        bar = proc.last_bar if proc.last_bar in bars else self.default_bar(proc)
        if bar is not None:
            bar.set_hilite(menu_id)
            proc.last_bar = bar

    def refresh_bars(self) -> None:
        self._sync_application_menu()
        if self.global_bar is not None:
            self.global_bar.process = (self.filer_menus if self.front_filer is not None
                                       else self.front_process)
            self.global_bar.update()
        for view in self.views.values():
            if view.bar is not None:
                view.bar.update()
        for bar in self.app_bars.values():
            bar.relayout()
        self._app_bar_timer.start()

    def show_about_classetta(self) -> AboutClassettaWindow:
        win = AboutClassettaWindow(self)
        self.about_windows.append(win)
        self.front_aux_window = win
        self.place_aux_window(win, self.front_filer)
        win.show()
        win.raise_()
        win.activateWindow()
        return win

    def toggle_record_sound(self) -> None:
        """File > Record Sound…: start or stop writing the Mac's sound to a
        WAV file as it plays (--record-sound)."""
        recording = [out for out in self.sound if isinstance(out, WaveFile)]
        if recording:
            for out in recording:
                out.close()
                self.sound.remove(out)
            self.config.record_sound = None
            self.show_alert(QMessageBox.Icon.Information, "Record Sound",
                            "Stopped recording the Mac's sound.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self.host_window, "Record Sound", "sound.wav", "WAV files (*.wav)")
        if not path:
            return
        try:
            self.sound.append(WaveFile(path))
        except OSError as e:
            self.show_alert(QMessageBox.Icon.Warning, "Record Sound",
                            f"Couldn't record to {path}: {e}")
            return
        self.config.record_sound = path
        self.show_alert(QMessageBox.Icon.Information, "Record Sound",
                        f"Recording the Mac's sound to {path}")

    def filer_menu_choice(self, menu_id: int, item: int) -> None:
        """A choice from the Filer's own menus, which no process answers."""
        if menu_id == APPLE_MENU_ID and item == 1:
            self.show_about_classetta()
        elif menu_id == FILE_MENU_ID and item == 1:
            front = self.front_filer
            if front is not None:
                front.open_selected()
        elif menu_id == FILE_MENU_ID and item == 3:
            front = self.front_filer
            if front is not None:
                front.get_info()
        elif menu_id == FILE_MENU_ID and item == 4:
            front = self.front_filer
            if front is not None:
                front.display_contents()
        elif menu_id == FILE_MENU_ID and item == 5:
            front = self.front_filer
            if front is not None:
                front.view_selected()
        elif menu_id == FILE_MENU_ID and item == 6:
            front = self.front_filer
            if front is not None:
                front.trace_selected()
        elif menu_id == FILE_MENU_ID and item == 7:
            front = self.front_filer
            if front is not None:
                front.set_memory()
        elif menu_id == FILE_MENU_ID and item == 9:
            front = self.front_filer
            if front is not None:
                front.save_catalog()
        elif menu_id == FILE_MENU_ID and item == 10:
            front = self.front_filer
            if front is not None:
                front.extract()
        elif menu_id == FILE_MENU_ID and item == 11:
            self.toggle_record_sound()
        elif menu_id == FILE_MENU_ID and item == 13:
            aux = self.front_aux_window
            if aux is not None:
                # the window the Close is for is the one on top: an
                # auxiliary window, not the Filer behind it
                aux.close()
                return
            front = self.front_filer
            if front is not None:
                front.close()
        elif menu_id == DISK_MENU_ID and item >= 1:
            self.filer_menus.disk_choice(item)
        elif menu_id == SPECIAL_MENU_ID and item == 6:
            self.restarting = True             # Special > Restart
            self.shutdown()                    # the same graceful shutdown first
        elif menu_id == SPECIAL_MENU_ID and item == 7:
            self.shutdown()                    # Special > Shutdown

    def bar_image(self, proc: Process) -> QImage | None:
        """The menu bar as proc's DrawMenuBar draws it in the screen
        buffer; None if there's no bar on the screen showing its menus."""
        bar = self.global_bar
        if bar is None or bar.process is not proc:
            return None
        return bar.image()

    def _update_app_bars(self) -> None:
        if self.config.menu_bar != "window":
            return
        for i, proc in enumerate(self.launch_order):
            needs = not proc.hidden and not any(
                rec.visible and rec in self.views and self.views[rec].bar is not None
                for rec in proc.windows)
            bar = self.app_bars.get(proc)
            if needs and bar is None:
                bar = AppBarWindow(self, proc, self.screen_widget)
                bar.place(Point(self.desk_top + 4 + i * 50, 8))
                bar.show()
                self.app_bars[proc] = bar
            elif not needs and bar is not None:
                del self.app_bars[proc]
                self._retire(bar)
        self.restack()

    # -- the System 7 Application menu -----------------------------------
    def application_menu_for(self, bar: MenuBarWidget) -> Menu | None:
        mode = self.config.application_menu
        if bar is not self.global_bar or mode == "never":
            return None
        if mode == "auto" and len(self.processes) + len(self.filer_windows) < 2:
            return None
        return Menu(kApplicationMenuID, "", self._application_items())

    def _application_items(self) -> list[MenuItem]:
        front = self.front_process
        # Count Filer as an application for menu visibility purposes
        # Filer is not a Process, so we need to check separately
        others = len(self.processes) > 1 or (len(self.processes) == 0 and self.filer_windows)
        hide_text = f"Hide {front.name}" if front is not None else ""
        items = [
            MenuItem(hide_text, enabled=others) if hide_text else MenuItem(""),
            MenuItem("Hide Others", enabled=others),
            MenuItem("Show All"),
            MenuItem("-", enabled=False),
        ]
        # Add all apps (checked only if this app is front and front_filer is None)
        items += [MenuItem(p.name, mark=checkMark if (p is front and self.front_filer is None) else "")
                  for p in self.launch_order]
        # Add Filer if any Filer windows exist (checked if front_filer is not None)
        if self.filer_windows:
            items.append(MenuItem("Filer", mark=checkMark if self.front_filer is not None else ""))
        return items

    def _sync_application_menu(self) -> None:
        """The System's Application menu, which programs find in their
        menu lists, has the bar's items."""
        menu = self.system.application_menu
        if menu is None or self.front_process is None:
            return
        items = self._application_items()
        with self.system.lock:
            if menu.items != items:
                menu.set_items(items)

    def application_menu_choice(self, item: int) -> None:
        front = self.front_process
        if item == 1:
            nxt = next((p for p in self.processes[1:] if not p.hidden), None)
            if nxt is not None:
                self.switch_to(nxt)
                front.hidden = True
                self._sync_process(front)
        elif item == 2:
            for proc in self.processes[1:]:
                proc.hidden = True
                self._sync_process(proc)
        elif item == 3:
            for proc in self.processes:
                if proc.hidden:
                    proc.hidden = False
                    self._sync_process(proc)
            self.restack()
        elif item >= 5 and item - 5 < len(self.launch_order):
            self.switch_to(self.launch_order[item - 5])
        elif self.filer_windows and item == 5 + len(self.launch_order):
            self.filer_front(self.front_filer or self.filer_windows[0])
        self.refresh_bars()

    # =====================================================================
    # The cursor.  Each process has its own, which the host's pointer has
    # over its windows; with host="screen", the front process's over the
    # whole screen, as a Mac has one cursor for all of it.  Menu bars keep
    # the host's arrow.
    # =====================================================================
    def set_cursor(self, proc: Process, shape) -> None:
        if proc in self.processes:
            self.cursors[proc] = shape
            self._show_cursor(proc)

    def obscure_cursor(self, proc: Process) -> None:
        """ObscureCursor: hidden till the mouse moves."""
        if proc in self.processes:
            self._obscured = proc
            self._show_cursor(proc)
        else:
            proc.cursor_obscured = False

    def _cursor_of(self, proc: Process):
        return cursors.HIDDEN if self._obscured is proc else self.cursors.get(proc)

    def _show_cursor(self, proc: Process) -> None:
        cursor = self._qcursor(self._cursor_of(proc))
        if self.screen_widget is not None:
            if self.is_front(proc):
                self.screen_widget.setCursor(cursor)
            return
        for rec in list(proc.windows):
            view = self.views.get(rec)
            if view is not None:
                view.setCursor(cursor)

    def _qcursor(self, shape) -> QCursor:
        if shape is None:
            return QCursor(Qt.CursorShape.ArrowCursor)
        if shape == cursors.HIDDEN:
            return QCursor(Qt.CursorShape.BlankCursor)
        cursor = self._qcursors.get(shape)
        if cursor is None:
            image = QImage(shape.rgba, cursors.SIZE, cursors.SIZE,
                           QImage.Format.Format_RGBA8888).copy()
            cursor = QCursor(QPixmap.fromImage(image), shape.hot.h, shape.hot.v)
            if len(self._qcursors) >= 64:
                self._qcursors.clear()
            self._qcursors[shape] = cursor
        return cursor

    # =====================================================================
    # Input
    # =====================================================================
    def update_mouse(self, gpos: QPoint, qt_mods, down: bool | None) -> None:
        if down is not None:
            self.input.down = down
            if down:
                self.input.press_time = time.monotonic()
                self.input.presses += 1
        if self._obscured is not None and gpos != self.input.global_pos:
            proc, self._obscured = self._obscured, None
            proc.cursor_obscured = False
            self._show_cursor(proc)
        self.input.global_pos = gpos
        self.input.mac_pos = self.mac_from_global(gpos)
        self.input.modifiers = modifiers_from_qt(qt_mods, self.input.down)
        self.system.input.mouse(self.input.mac_pos, self.input.down)
        self.system.input.modifiers(self.input.modifiers)

    def mouse_down(self, obj: QWidget) -> None:
        gpos = self.input.global_pos
        if self.tracker is not None:
            # A press in a menu that is down belongs to the menu, not to a
            # window: the auxiliary window in front stays front.
            self.tracker.press(gpos)
            return
        owner, widget = self._owner(obj)
        if owner is self.filer_menus:
            # The Filer's menus, which no process waits on: the Display pulls
            # them down and takes the pick itself, as it does the Application
            # menu's.  The bar is not a place a window comes forward, so an
            # auxiliary window in front stays front.
            self._start_tracker(MenuTracker(self, widget, Future(),
                                            self.config.sticky_menus))
            return
        self.front_aux_window = None
        if owner is not None and owner not in self.processes:
            # A window of an app that has quit, not yet gone from the screen.
            self._swallow_release = True
            return
        if owner is not None and self.front_filer is not None:
            self.filer_front(None)             # a click in an application's window
        if owner is not None and owner is not self.front_process:
            self.switch_to(owner)
            self._swallow_release = True
            return
        if isinstance(widget, AppBarWindow) and obj is widget:
            # The bar-only window isn't a Mac window; move it ourselves.
            self._start_tracker(DragTracker(self, Future(), widget, widget.mac_origin(),
                                            self._drag_limit(None), self.input.mac_pos))
            return
        proc = self.front_process
        if proc is not None:
            proc.events.post(EventRecord(mouseDown, 0, 0, self.system.input.position,
                                         self.input.modifiers))

    def mouse_up(self) -> None:
        try:
            if self.tracker is not None:
                # Tracking calls eat the mouseUp, as WaitMouseUp does.
                self.tracker.release(self.input.global_pos)
                return
            if self._swallow_release:
                self._swallow_release = False
                return
            proc = self.front_process
            if proc is not None:
                proc.events.post(EventRecord(mouseUp, 0, 0, self.system.input.position,
                                             self.input.modifiers))
            clicked = self._last_clicked
            if (self.screen_widget is None and isinstance(clicked, MacWindowView)
                    and clicked.rec is not clicked.rec.process.front_window()):
                # The host window manager raised a window the guest hasn't
                # selected (yet); put the Mac stacking order back.
                QTimer.singleShot(0, self.restack)
        finally:
            # Its mouse-up is in the queue now, if it made one, for the
            # Toolbox's tracking calls to take.
            self.input.releases = self.input.presses

    def _owner(self, obj: QWidget) -> tuple[Process | None, QWidget | None]:
        w = obj
        while w is not None:
            if isinstance(w, MenuBarWidget):
                return w.process, w
            if isinstance(w, MacWindowView):
                return w.rec.process, w
            if isinstance(w, AppBarWindow):
                return w.process, w
            w = w.parentWidget()
        return None, None

    def _app_state(self, state) -> None:
        if state != Qt.ApplicationState.ApplicationActive:
            self.system.input.release()

    def _filer_shortcut(self, ev) -> bool:
        """A Control key over a key equivalent of the front Filer's menus
        gives its command, as MenuKey gives an application's: True if this
        key was one of them."""
        if self.front_filer is None or ev.type() != QEvent.Type.KeyPress:
            return False
        if self.front_aux_window is not None:
            return False
        if not ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                 | Qt.KeyboardModifier.MetaModifier):
            return False
        try:
            ch = chr(ev.key())
        except (ValueError, OverflowError):
            return False
        for menu in self.filer_menus.bar_menus():
            for i, item in enumerate(menu.items, start=1):
                if item.enabled and item.key and item.key.upper() == ch.upper():
                    # The menu's title lights, then the choice is taken.
                    self.hilite_menu(self.filer_menus, menu.id)
                    QTimer.singleShot(100, lambda m=menu.id, n=i: (
                        self.hilite_menu(self.filer_menus, 0),
                        self.filer_menu_choice(m, n)))
                    return True
        return False

    def key_event(self, ev) -> bool:
        """A key for the screen or a Filer window: True when a Filer menu
        shortcut took it, so the widget it arrived at need not see it."""
        self.input.modifiers = modifiers_from_qt(ev.modifiers(), self.input.down)
        self.system.input.modifiers(self.input.modifiers)
        msg = key_message(ev, self.system.model.keyboard)
        if msg is not None and not ev.isAutoRepeat():
            self.system.input.key(msg, ev.type() == QEvent.Type.KeyPress)
        if self.tracker is not None:
            if ev.key() == Qt.Key.Key_Escape and ev.type() == QEvent.Type.KeyPress:
                self.tracker.cancel()
            return False
        if self._filer_shortcut(ev):
            return True
        proc = self.front_process
        if proc is None or self.front_filer is not None:
            # A Filer window is frontmost, so these keys are its own: it has
            # commands for some and ignores the rest, rather than leave them
            # queued for an application that is behind it.
            return False
        if ev.type() == QEvent.Type.KeyPress:
            what = autoKey if ev.isAutoRepeat() else keyDown
        elif ev.isAutoRepeat():
            return False
        else:
            what = keyUp
        if msg is not None:
            proc.events.post(EventRecord(what, msg, 0, self.system.input.position,
                                         self.input.modifiers))
        return False
