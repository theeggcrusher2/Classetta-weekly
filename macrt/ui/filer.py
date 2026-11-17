"""
macrt.ui.filer - A System 6 Filer window over a disk image's folder.

When a disk image is named on the command line with no --app, the Display
opens this instead of running the image's first application: a window with
the chrome of a System 6 document window — Chicago title, pinstripes, a
close box — listing the folder's contents.  Double-clicking an
application runs it, a document opens it with the application that made
it, and a folder opens a window of its own.  The window stays open, so
several applications can be started from one, and its close box closes
it like a real Filer window's.

Nothing here writes back to the disk: the window only lists what the
folder already holds, so its header wears the padlock System 6 showed
beside Name on a locked disk — that the list may be looked at, not
changed.  FilerOptions.writable is how a window says otherwise.
"""

from __future__ import annotations

import copy
import time
from concurrent.futures import Future
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import TYPE_CHECKING

import machfs
from PyQt6.QtCore import QEvent, QSize, Qt
from PyQt6.QtGui import QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHeaderView, QInputDialog, QListWidget,
    QMessageBox, QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QWidget,
)

from pathlib import Path

from ..macfile import (ArchiveFolder, ContainerFolder, HostFolder, LazyArchiveFile,
                       MacFile, VolumeFolder,
                       alias_open, alias_original, archive_entries, archive_kind,
                       archive_name, container_contents, creator_application,
                       extract_archive, image_kind,
                       is_alias, is_control_panel, launch_target, list_folder,
                       load_volume, open_container, image_volume)
from ..process import size_resource
from ..m68k.segload import has_code, why_no_code
from ..resfork import InvalidForkError, is_fork, read_fork
from ..toolbox import constants as k
from ..toolbox.icons import LARGE, MINI, SMALL, Icon, bitmap_icon, color_suite_icon, suite_icon
from ..toolbox.menus import Menu, parse_menu_items
from ..toolbox.windows import WindowKind
from ..viewer import describe, viewer_main
from . import chrome, theme
from .busy import run_busy
from .stdfile import (CATALOG_ICON, CONTROL_PANEL_ICON, DESK_ACCESSORY_ICON, DISK_ICON,
                      DOCUMENT_ICON, EXTENSION_ICON, FONT_ICON, FOLDER_ICON, icon,
                      icon_from_bitmap)
from .tracking import BoxTracker, DragTracker
from .window import _Hosted

if TYPE_CHECKING:
    from .display import Display
    from .menubar import MenuBarWidget

CONTENT = (316, 236)       # the list's size, inside the frame
CONTENT_LIMIT = 65536      # bytes of a fork the Content Viewer shows at once
LIST_STYLE = """
QTreeWidget { background: white; color: black; border: none; outline: none; }
QTreeWidget::item { padding: 1px; }
QTreeWidget::item:selected { background: black; color: white; }
QHeaderView::section { background: white; color: black; border: none;
                       border-right: 1px solid black; border-bottom: 1px solid black;
                       padding: 2px 4px; }
"""
FOLDER = b"fldr"
KINDS = [(b"ZSYS", "System file"), (b"zsys", "System file"), (b"cdev", "Control Panel"),
         (b"INIT", "Extension"), (b"dmov", "Desk Accessory"), (b"FFIL", "Font"),
         (b"FONT", "Font")]
# Types the system itself provides a default icon for, shown when the
# file declares no icon family of its own (a control panel's is at
# -4064 by convention).  Maps to the key icon() knows.
SYSTEM_FILE_ICONS = {b"cdev": "control_panel", b"INIT": "extension",
                     b"dmov": "desk_accessory", b"FFIL": "font", b"FONT": "font"}
SYSTEM_FILE_BITMAPS = {"control_panel": CONTROL_PANEL_ICON, "extension": EXTENSION_ICON,
                       "desk_accessory": DESK_ACCESSORY_ICON, "font": FONT_ICON}
# The padlock beside Name while the window is view-only: solid body, white
# keyhole (the transparency behind it), close to the one System 6 drew.
LOCK = [
    "..#####..",
    "..#...#..",
    "..#...#..",
    "..#...#..",
    "#########",
    "#########",
    "#########",
    "####.####",
    "###...###",
    "#########",
    "#########",
]

# A container's own icon: what the Finder calls the file (Icon plus a
# carriage return) that keeps a folder's or a volume's, and the flag a
# file's custom icon is set by (kHasCustomIcon).
ICON_FILE = "Icon\r"
HAS_CUSTOM_ICON = 0x0400

# A window over a packed file: a case with a handle and a strap down it.
ARCHIVE_ICON = [
    ".....######.....",
    "################",
    "#......##......#",
    "#......##......#",
    "################",
    "#......##......#",
    "#......##......#",
    "#......##......#",
    "################",
]

# The title bar's stand-in when a container has no icon of its own, by
# what the container is: the volume (its disk image), a folder, or a
# file — an archive to look through, what no window opens yet.
CONTAINER_ICONS = {"volume": DISK_ICON, "folder": FOLDER_ICON,
                   "archive": ARCHIVE_ICON, "document": DOCUMENT_ICON,
                   "catalog": CATALOG_ICON}


@dataclass(frozen=True)
class FilerOptions:
    """How applications are launched from the window, from the command
    line: view shows them from their resources without running them,
    trace logs the traps they call, memory is their size in bytes.
    writable is about the window itself: while it is False — always,
    until Filer Windows can be written from here — the list is
    view-only and the header shows the lock."""
    view: bool = False
    trace: bool = False
    memory: int | None = None
    writable: bool = False


def _kind(f: MacFile) -> str:
    if f.type == FOLDER:
        return "folder"
    if f.is_application:
        return "application"
    if not _fork_pending(f) and is_alias(f):
        return "alias"
    known = archive_name(f) or image_kind(f)
    if known:
        return known
    from ..assoc import associated_kind
    return associated_kind(f) or next((name for t, name in KINDS if f.type == t), "document")


def _icon_for(f: MacFile) -> str:
    if f.type == FOLDER:
        return "folder"
    if f.is_application:
        return "application"
    if is_alias(f):
        return "alias"
    return SYSTEM_FILE_ICONS.get(f.type, "document")


def _lock() -> QIcon:
    """The header's padlock: the list may be looked at, not changed."""
    w, h = theme.bitmap_size(LOCK)
    pm = QPixmap(w, h)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    theme.draw_bitmap(p, 0, 0, LOCK)
    p.end()
    return QIcon(pm)


def _bitmap(icon: Icon) -> list[str]:
    """An icon as '#' and '.' rows, only where its mask lets it show."""
    mask = icon.mask or [b"\xFF" * icon.row_bytes] * icon.height
    return ["".join("#" if irow[x >> 3] & 0x80 >> (x & 7) and mrow[x >> 3] & 0x80 >> (x & 7)
                    else "." for x in range(icon.width))
            for irow, mrow in zip(icon.rows, mask)]


def _halve(bitmap: list[str]) -> list[str]:
    """A 32-by-32 icon's every other row and column: the bar's 16."""
    return [row[::2] for row in bitmap[::2]]


@lru_cache(maxsize=256)
def _icon_rows(rsrc: bytes, large: bool,
               prefer: tuple[int, ...] = (128, 0, 1, -4064, -15775)) -> tuple[str, ...] | None:
    """The '#'/'.' rows a resource fork's icon gives, kept as a tuple so
    the cache shares them safely: the small one for rows and title bars
    (halved when the fork only has a 32-by-32), the full one for Get
    Info; None for a fork that holds no icon.  prefer names the
    resource ids to try, in order, before falling back to any id."""
    if not is_fork(rsrc):
        return None
    try:
        resources = read_fork(rsrc)
    except (InvalidForkError, ValueError, DecompressError, Exception):
        return None

    # Try preferred IDs first, then any ID
    all_ids = tuple(r.id for r in resources)
    id_tries = tuple(dict.fromkeys(prefer + all_ids))
    
    for rid in id_tries:
        def get(kind: str) -> bytes | None:
            kbytes = kind.encode("ascii")
            for r in resources:
                if r.type == kbytes and r.id == rid:
                    return r.data
            for r in resources:
                if r.type == kbytes:
                    return r.data
            return None

        order = (LARGE, SMALL, MINI) if large else (SMALL, LARGE, MINI)
        icon = None
        for size in order:
            icon = suite_icon(get, size)
            if icon is not None:
                break
        if icon is None:
            for r in resources:
                if r.type == b"ICON" and r.id == rid:
                    icon = bitmap_icon(r.data, LARGE, masked=False)
                    break
            if icon is None:
                for r in resources:
                    if r.type == b"ICON":
                        icon = bitmap_icon(r.data, LARGE, masked=False)
                        break
        if icon is not None:
            bitmap = _bitmap(icon)
            if not large and len(bitmap[0]) > 16:
                bitmap = _halve(bitmap)
            return tuple(bitmap)
    return None


def _icon_from(rsrc: bytes, large: bool = False) -> list[str] | None:
    """The icon a resource fork holds (see _icon_rows), as rows to draw,
    or None for a fork that holds no icon."""
    rows = _icon_rows(rsrc, large)
    return None if rows is None else list(rows)


def _custom_icon(node: machfs.Volume | machfs.Folder | machfs.File,
                 *, large: bool = False) -> list[str] | None:
    """A container's own icon: a file's when its Finder flags say it has
    one, or a folder's or a volume's, kept in the Icon-plus-return file
    the Finder writes inside them.  machfs keeps no folder flags, so for
    those, that file's being there is what says the icon is set."""
    if isinstance(node, machfs.File):
        if not node.flags & HAS_CUSTOM_ICON:
            return None
        return _icon_from(bytes(node.rsrc), large)
    icon_file = next((f for name, f in node.items()
                      if name.lower() == ICON_FILE.lower()), None)
    if isinstance(icon_file, machfs.File):
        return _icon_from(bytes(icon_file.rsrc), large)
    return None


def _fork_pending(f: MacFile) -> bool:
    """True while an archive member's forks are still undecoded — the
    icon paths then show a generic icon, and drawing a listing never
    decodes an archive member's resources."""
    return isinstance(f, LazyArchiveFile) and f.forks_pending


def item_icon(f: MacFile, *, large: bool = False) -> list[str] | None:
    """The icon the item's own resources give it — an application's or a
    marked file's from its resource fork, a folder's from its Icon file,
    a system file's declared family — else None for an item with no icon
    of its own."""
    if f.type == FOLDER:
        volume = getattr(f.folder, "volume", None)
        if volume is None:
            return None
        node = volume
        for part in getattr(f.folder, "path", ()):
            node = node[part]
        try:
            return _custom_icon(node[f.name], large=large)
        except KeyError:
            return None
    if _fork_pending(f):
        return None
    if f.is_application or f.flags & HAS_CUSTOM_ICON:
        return _icon_from(bytes(f.rsrc), large)
    from ..assoc import associated_icon_rows, declared_icon_rows
    own = associated_icon_rows(f, large=large)
    if own is not None:
        return list(own)
    if f.type in SYSTEM_FILE_ICONS:
        # A system file may carry its own family (a control panel's is
        # at -4064 by convention); the Finder shows it in place of the
        # system's default.
        rows = declared_icon_rows(f, large=large)
        if rows is None:
            prefer = (-4064, 128) if f.type == b"cdev" else (128,)
            rows = _icon_rows(bytes(f.rsrc), large, prefer=prefer)
        return None if rows is None else list(rows)
    return None


def _generic_icon(f: MacFile) -> list[str]:
    """A stand-in for an item with no icon of its own, by what it is."""
    if f.type == FOLDER:
        return FOLDER_ICON
    if f.is_application:
        return theme.GENERIC_APP_ICON
    try:
        if archive_kind(f):
            return ARCHIVE_ICON
    except Exception:               # an unreadable data fork says nothing
        pass
    system = SYSTEM_FILE_ICONS.get(f.type)
    if system is not None:
        return SYSTEM_FILE_BITMAPS[system]
    return DOCUMENT_ICON


@lru_cache(maxsize=256)
def _qicon_cached(rows: tuple[str, ...]) -> QIcon:
    return icon_from_bitmap(list(rows))


def _qicon(bitmap: list[str] | None) -> QIcon | None:
    """The list's QIcon for an item's own icon, cached with its rows,
    or None when the item has no icon of its own."""
    return None if bitmap is None else _qicon_cached(tuple(bitmap))


def _container_icon(volume: machfs.Volume, path: tuple[str, ...] = ()) -> list[str]:
    """What the title bar shows for the container the window lists: the
    container's own icon when it has one, else a generic for what the
    container is — the volume (its disk image), a folder, or a file."""
    node = volume
    for part in path:
        node = node[part]
    custom = _custom_icon(node)
    if custom is not None:
        return custom
    # DiskTracker catalogs get their own icon
    from disktracker.filer_adapter import DiskTrackerVolume
    if isinstance(node, DiskTrackerVolume):
        return CATALOG_ICON
    kind = ("volume" if isinstance(node, machfs.Volume)
            else "folder" if isinstance(node, machfs.Folder)
            else "archive" if isinstance(node, ArchiveFolder) or archive_kind(node)
            else "document")
    return CONTAINER_ICONS[kind]


class _ListHeader(QHeaderView):
    """The list's header, which paints Name over the names below it."""

    def __init__(self, parent=None):
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft
                                 | Qt.AlignmentFlag.AlignVCenter)

    def paintSection(self, painter, rect, logicalIndex):  # noqa: N802
        super().paintSection(painter, rect, logicalIndex)
        if logicalIndex != 0:
            return
        model = self.model()
        text = model.headerData(logicalIndex, Qt.Orientation.Horizontal,
                                Qt.ItemDataRole.DisplayRole)
        if not text:
            return
        icon = model.headerData(logicalIndex, Qt.Orientation.Horizontal,
                                Qt.ItemDataRole.DecorationRole)
        painter.save()
        # The stylesheet's section leaves the painter clipped to nothing,
        # so whatever is drawn on top of it — the lock, the label — needs
        # the clip set back to the section first or it never lands.
        painter.setClipRect(rect, Qt.ClipOperation.ReplaceClip)
        painter.setFont(self.font())
        # Drawn one pixel left of where the ink should fall: the 'N' of
        # Name has a pixel of left bearing, so at 26 it lands at 27 —
        # the column the file names below begin in.
        text_left = rect.x() + 26
        painter.fillRect(rect.x(), rect.y(),
                         text_left + painter.fontMetrics().horizontalAdvance(text)
                         + 2 - rect.x(), rect.height() - 1, Qt.GlobalColor.white)
        if icon is not None and not icon.isNull():
            # The lock moved off the padding: drawn there its first pixel
            # fell at 4, too far from Name at 26 — at 8 it sits beside it.
            size = icon.actualSize(QSize(rect.width(), rect.height()))
            icon.paint(painter, rect.x() + 8,
                       rect.y() + (rect.height() - size.height()) // 2,
                       size.width(), size.height())
        painter.setPen(Qt.GlobalColor.black)
        painter.drawText(text_left, rect.y() + 2, rect.width() - 30,
                         rect.height() - 5,
                         int(Qt.AlignmentFlag.AlignLeft
                             | Qt.AlignmentFlag.AlignVCenter), text)
        painter.restore()


class FilerWindow(_Hosted):
    """One folder's window: its contents listed, ready to open.  Its
    title bar wears an icon of the container it lists — the disk image
    at the root, a folder inside — the container's own when it has one."""

    def __init__(self, display: Display,
                 volume: machfs.Volume | HostFolder | ContainerFolder | ArchiveFolder,
                 path: tuple[str, ...] = (), options: FilerOptions | None = None,
                 parent: QWidget | None = None):
        super().__init__(display, parent)
        self.volume = volume
        self.path = path
        self.options = options or FilerOptions()
        if isinstance(volume, HostFolder):
            self._title_icon = FOLDER_ICON
            self.setWindowTitle(path[-1] if path else str(volume.path.name) or "Host Folder")
        elif isinstance(volume, (ContainerFolder, ArchiveFolder)):
            self._title_icon = ARCHIVE_ICON
            self.setWindowTitle(volume.name)
        else:
            self._title_icon = _container_icon(volume, path)
            self.setWindowTitle(self.path[-1] if self.path else self.volume.name)
        self._hilited = True
        self._pressed = k.wNoHit
        self._last_name: str | None = None
        self._opened_at = 0.0
        self.entries: dict[str, MacFile] = {}
        self.tree = QTreeWidget(self)
        self._content_width = CONTENT[0]
        self._fill()
        self.tree.setStyleSheet(LIST_STYLE)
        self.tree.setFont(theme.body_font())
        # Size the list so the Name column fits 32 W's without truncating,
        # whatever the host's stand-in for the Mac font measures: text +
        # icon and padding (16px icon + padding, plus headroom for a
        # vertical scrollbar's width) + the fixed Size and Kind columns.
        fm = QFontMetrics(self.tree.font())
        self._content_width = max(CONTENT[0], fm.horizontalAdvance('W' * 32) + 30 + 56 + 84)
        self.tree.setRootIsDecorated(False)
        self.tree.setIndentation(0)
        self.tree.setUniformRowHeights(True)
        self.tree.setIconSize(QSize(16, 16))
        self.tree.setHeaderLabels(["Name", "Size", "Kind"])
        if not self.options.writable:      # view-only: the lock beside Name
            self.tree.headerItem().setIcon(0, _lock())
        self.tree.setHeader(_ListHeader(self.tree))
        header = self.tree.header()
        header.setFont(theme.title_font())
        header.setStretchLastSection(False)
        header.setSectionsClickable(False)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Fixed)
        self.tree.setColumnWidth(1, 56)
        self.tree.setColumnWidth(2, 84)
        self.tree.itemActivated.connect(self._activated)
        self.tree.installEventFilter(self)
        self.tree.viewport().installEventFilter(self)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._focus_changed)
        self._relayout()

    # -- content ----------------------------------------------------------
    def _fill(self) -> None:
        def list_it():
            return sorted(list_folder(self.volume, self.path), key=lambda f: f.name.lower())
        try:
            # A host folder full of big files reads them whole while the
            # listing is built; that work runs behind a busy window.
            # No parent: this runs before the window is shown.
            files = run_busy(None, f"Opening {self.windowTitle()}",
                             f"“{self.windowTitle()}” is being listed now; the Filer stays "
                             "usable while it runs.",
                             list_it) or []
        except Exception:
            files = []
        self.entries = {f.name: f for f in files}
        self.tree.clear()
        for f in files:
            folder = f.type == FOLDER
            try:
                data, rsrc = f.sizes()
                size = "--" if folder else f"{data + rsrc:,}"
            except Exception:
                size = "--"
            item = QTreeWidgetItem([f.name, size, _kind(f)])
            item.setIcon(0, item_icon_qicon(f, depth=_depth(self.display))
                         or icon(_icon_for(f)))
            self.tree.addTopLevelItem(item)

    # -- frame ------------------------------------------------------------
    def frame(self) -> chrome.Frame:
        if isinstance(self.volume, HostFolder):
            title = self.path[-1] if self.path else str(self.volume.path.name) or "Host Folder"
        elif isinstance(self.volume, (ContainerFolder, ArchiveFolder)):
            title = self.volume.name
        else:
            title = self.path[-1] if self.path else self.volume.name
        return chrome.Frame(kind=WindowKind.DOCUMENT, title=title,
                            title_icon=self._title_icon, hilited=self._hilited,
                            go_away=True, pressed=self._pressed)

    def content_size(self) -> tuple[int, int]:
        return (getattr(self, '_content_width', CONTENT[0]), CONTENT[1])

    def _relayout(self) -> None:
        f = self.frame()
        w, h = chrome.structure_size(f, *self.content_size())
        s = chrome.shadow(f)
        self.resize(w + s, h + s)
        self.setMask(chrome.mask(f, w, h))
        self.tree.setGeometry(chrome.content_rect(f, w, h))
        self.update()

    def paintEvent(self, event):  # noqa: N802
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        chrome.paint(QPainter(self), f, w, h)

    def _hit(self, x: int, y: int) -> int:
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        return chrome.hit(f, w, h, x, y)

    def box_rect(self, part: int):
        if part != k.wInGoAway:
            return None
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        return chrome.close_box_rect(f, w)

    def set_pressed(self, part: int) -> None:
        if part != self._pressed:
            self._pressed = part
            self.update()

    # -- the close box and the title bar ----------------------------------
    def mousePressEvent(self, ev):  # noqa: N802
        self.raise_()
        if self.isWindow():
            self.activateWindow()
        else:
            self.tree.setFocus()       # on the emulated screen the focus says who's front
        self.display.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down=True)
        part = self._hit(ev.position().toPoint().x(), ev.position().toPoint().y())
        if part == k.wInGoAway:
            self.display._start_tracker(
                BoxTracker(self.display, Future(), self, k.wInGoAway)
            ).add_done_callback(self._close_box_done)
        elif part == k.wInDrag:
            self.display._start_tracker(DragTracker(
                self.display, Future(), self, self.mac_origin(),
                self.display._drag_limit(None), self.display.input.mac_pos))

    def mouseMoveEvent(self, ev):  # noqa: N802
        self.display.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down=None)
        t = self.display.tracker
        if isinstance(t, (BoxTracker, DragTracker)) and self._tracks(t):
            t.move(ev.globalPosition().toPoint())

    def mouseReleaseEvent(self, ev):  # noqa: N802
        self.display.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down=False)
        t = self.display.tracker
        if isinstance(t, (BoxTracker, DragTracker)) and self._tracks(t):
            t.release(ev.globalPosition().toPoint())
        self.display.input.releases = self.display.input.presses

    def _tracks(self, t) -> bool:
        return getattr(t, "view", None) is self or getattr(t, "widget", None) is self

    def _close_box_done(self, closed) -> None:
        if closed:
            self.display.close_filer(self)

    def closeEvent(self, event):  # noqa: N802
        event.accept()
        if not self.display.shutting_down:
            self.display.close_filer(self)

    # -- activation, so the pinstripes and close box show when it's front --
    def _set_hilited(self, hilited: bool) -> None:
        if hilited != self._hilited:
            self._hilited = hilited
            self._relayout()

    def changeEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.ActivationChange and self.isWindow():
            self._set_hilited(self.isActiveWindow())
        super().changeEvent(event)

    def _focus_changed(self, old: QWidget | None, new: QWidget | None) -> None:
        def inside(w):
            return w is not None and (w is self or self.isAncestorOf(w))
        if inside(new) and not inside(old):
            self._set_hilited(True)
            self.display.filer_front(self)         # screen: focus in a Filer window
        elif inside(old) and not inside(new):
            self._set_hilited(False)
            self.display.filer_blurred(self, new)

    def eventFilter(self, obj, ev):  # noqa: N802
        if ev.type() == QEvent.Type.MouseButtonPress:
            self.raise_()
            if self.isWindow():
                self.activateWindow()
            self.display.filer_front(self)         # a click in the list
        elif (obj is self.tree and ev.type() == QEvent.Type.KeyPress
              and ev.modifiers() & (Qt.KeyboardModifier.ControlModifier
                                    | Qt.KeyboardModifier.MetaModifier)):
            # The list takes most keys itself — Space selects — so a
            # Command-key equivalent it would swallow is offered to the
            # Display first, which answers True when it took it.
            return self.display.key_event(ev)
        return False

    # -- what the menus act on ---------------------------------------------
    def selected_item(self) -> MacFile | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        return self.entries.get(items[0].text(0))

    def selected_actions(self) -> set[str]:
        """The things the menus can do with the selection: an empty set
        while nothing is selected."""
        item = self.selected_item()
        if item is None:
            return set()
        actions = {"open", "info"}
        try:
            dlen, rlen = item.sizes()
            has_data = item.type != FOLDER and (dlen > 0 or rlen > 0)
        except Exception:
            has_data = item.type != FOLDER
        if has_data:
            actions.add("display_contents")
        return actions

    def open_selected(self) -> None:
        """Open the currently selected item, as double-clicking it would."""
        item = self.selected_item()
        if item is not None:
            self._open_item(item)

    def view_selected(self) -> None:
        """Open the currently selected item showing its menus and windows
        from its resources, without running its code."""
        item = self.selected_item()
        if item is not None:
            self._open_item(item, view=True)

    def trace_selected(self) -> None:
        """Open the currently selected item logging the traps it calls."""
        item = self.selected_item()
        if item is not None:
            self._open_item(item, trace=True)

    def set_memory(self) -> None:
        """Set the memory size, in K, launches from this window use in place
        of what their SIZE resources ask for."""
        current = (self.options.memory or 0) // 1024
        k, ok = QInputDialog.getInt(
            self, "Memory", "Memory size in K, in place of what SIZE asks for\n"
            "(0: what the application asks for)", current, 0, 2 ** 24 - 1, 16)
        if ok:
            self.options = replace(self.options, memory=k * 1024 if k else None)

    def _open_item(self, f: MacFile, view: bool | None = None,
                   trace: bool | None = None) -> None:
        """Open f, following alias files to what they point at first."""
        for _ in range(8):
            if not is_alias(f):
                break
            target, volume, path = alias_open(f, self.display.system.files)
            if target is None and volume is None:
                self._say(f"{f.name} is an alias, but its original can't be found.")
                return
            if target is None:
                self.display.open_filer(volume, path, self.options)
                return
            f = target
        else:
            self._say(f"{f.name}'s aliases lead on eight deep, which they must not.")
            return
        if f.type == FOLDER:
            if isinstance(self.volume, HostFolder):
                sub_path = self.volume.path / f.name
                self.display.open_filer(HostFolder(sub_path, self.volume.root), (), self.options)
            else:
                self.display.open_filer(self.volume, self.path + (f.name,), self.options)
            return
        self._open(f, view, trace)

    def get_info(self) -> None:
        """Open a Get Info window for the currently selected item."""
        item = self.selected_item()
        if item is None:
            return
        win = GetInfoWindow(self.display, item)
        self.display.info_windows.append(win)
        self.display.front_aux_window = win
        # Center the info window over the Filer window.
        fr = self.frameGeometry()
        win.move(fr.center().x() - win.width() // 2,
                 fr.center().y() - win.height() // 2)
        win.show()
        win.raise_()
        win.activateWindow()

    def display_contents(self) -> None:
        """Open a Resource/Content Viewer window for the selected file."""
        item = self.selected_item()
        if item is None:
            return
        item = self._materialised(item)
        if item is None:
            return
        win = ContentViewerWindow(self.display, item)
        self.display.viewer_windows.append(win)
        self.display.front_aux_window = win
        fr = self.frameGeometry()
        win.move(fr.center().x() - win.width() // 2,
                 fr.center().y() - win.height() // 2)
        win.show()
        win.raise_()
        win.activateWindow()

    def extractable(self) -> bool:
        """Extract… works on a window browsing an archive, or on the
        selected archive file in any listing; macextract decides what
        an archive is, so new formats get this with no change here."""
        if isinstance(self.volume, ArchiveFolder):
            return True
        item = self.selected_item()
        return (item is not None and item.type != FOLDER
                and archive_kind(item) == "archive")

    def extract(self) -> None:
        """Extract the archive being browsed (or the selected one)
        into a folder the user picks."""
        if isinstance(self.volume, ArchiveFolder):
            raw, stem = self.volume.raw, self.volume.name
        else:
            item = self.selected_item()
            if item is None or item.type == FOLDER or archive_kind(item) != "archive":
                return
            item = self._materialised(item)
            if item is None:
                return
            raw, stem = item.data, item.name
        dest = QFileDialog.getExistingDirectory(self, f"Extract {stem} to…")
        if not dest:
            return
        try:
            written = run_busy(self, f"Extracting {stem}",
                               f"“{stem}” is extracting now; the Filer stays "
                               "usable while it runs.",
                               lambda: extract_archive(raw, dest))
        except ValueError as e:
            self._say(str(e))
            return
        if written is None:
            return
        self.display.show_alert(QMessageBox.Icon.Information, "Extract",
                                f"Extracted {len(written)} file(s) to {dest}")

    def savable(self) -> bool:
        """Save Catalog works on what can be browsed — volumes, host
        folders, encoded containers — not on the read-only views of
        Disk Tracker catalogs."""
        from ..save_dtc import savable
        return savable(self.volume)

    def save_catalog(self) -> None:
        """Write what the window browses to a Disk Tracker 2 catalog."""
        from ..save_dtc import build_catalog, default_save_name
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Disk Tracker 2 catalog", default_save_name(self.volume),
            "Disk Tracker catalogs (*.dtc)")
        if not path:
            return
        try:
            data = run_busy(self, f"Saving {self.windowTitle()}",
                            f"“{self.windowTitle()}” is being written as a Disk Tracker "
                            "catalog; the Filer stays usable while it runs.",
                            lambda: build_catalog(self.volume))
        except Exception as e:
            self._say(f"Couldn't save the catalog: {e}")
            return
        if data is None:
            return                    # stopped waiting; the write runs on
        try:
            Path(path).write_bytes(data)
        except OSError as e:
            self._say(f"Couldn't write {path}: {e}")

    # -- the keyboard -------------------------------------------------------
    # A Filer window has no Process behind it, so its keys come here rather
    # than through the Display's event filter: the Display takes them, the
    # Filer's own menu shortcuts first.
    def keyPressEvent(self, ev):  # noqa: N802
        self.display.key_event(ev)
        ev.accept()

    def keyReleaseEvent(self, ev):  # noqa: N802
        self.display.key_event(ev)
        ev.accept()

    # -- opening what's clicked -------------------------------------------
    def _activated(self, item: QTreeWidgetItem) -> None:
        name = item.text(0)
        now = time.monotonic()
        if name == self._last_name and now - self._opened_at < 0.4:
            return                      # a double-click arrives as two activations
        self._last_name, self._opened_at = name, now
        f = self.entries.get(name)
        if f is None:
            return
        self._open_item(f)

    def _materialised(self, f: MacFile) -> MacFile | None:
        """f with its forks decoded, off the GUI thread when f is an
        archive member's: a big member's fork can take minutes, and the
        Filer must stay up while it does.  None when the user stopped
        waiting or the decode failed."""
        if not isinstance(f, LazyArchiveFile) or not f.forks_pending:
            return f
        def work():
            # The loader decodes both forks together; a fork the
            # archive declares empty costs nothing.
            _ = f.data
            _ = f.rsrc
            return f
        try:
            return run_busy(self, f"Reading {f.name}",
                            f"“{f.name}” is inside an archive; its forks "
                            "decode now, and the Filer stays usable.",
                            work)
        except Exception as e:
            self._named(f, e)
            return None

    def _open(self, f: MacFile, view: bool | None = None,
              trace: bool | None = None) -> None:
        f = self._materialised(f)
        if f is None:
            return
        # A file whose data fork is itself an encoding — MacBinary, BinHex
        # or AppleSingle — shows what it holds in a Filer window rather
        # than run.  A container that's there but damaged says so instead.
        try:
            nested = container_contents(f.data, f.name, f.folder) if f.data else None
        except ValueError as e:
            self._named(f, e)
            return
        if nested is not None:
            self.display.open_filer(ContainerFolder(nested, f.name), (), self.options)
            return
        # A disk image is a volume to browse — what the command line
        # opens it as — not a file to run.  A big one, or one whose other
        # half of an .mdf/.mds pair sits inside an archive, reads off the
        # GUI thread behind a busy window.
        if image_kind(f) is not None:
            try:
                volume = run_busy(self, f"Opening {f.name}",
                                  f"“{f.name}” is a disk image; its volume is read "
                                  "now, and the Filer stays usable while it runs.",
                                  lambda: image_volume(f))
            except (OSError, ValueError) as e:
                self._named(f, e)
                return
            if volume is None:
                return                    # stopped waiting; the read runs on
            system = getattr(self.display, "system", None)
            if system is not None and isinstance(volume, machfs.Volume):
                # mounted, so aliases into it resolve, as for --disk
                system.files.mount_folder(VolumeFolder(volume))
            self.display.open_filer(volume, (), self.options)
            return
        # A big archive — the host's or an in-memory one — reads and
        # lists off the GUI thread behind a busy window.
        try:
            listed = run_busy(self, f"Opening {f.name}",
                              f"“{f.name}” is an archive; its contents are listed "
                              "now, and the Filer stays usable while it runs.",
                              lambda: (archive_entries(f.data, f.name) if f.data else None,))
        except ValueError as e:
            self._named(f, e)
            return
        if listed is None:
            return                    # stopped waiting; the listing runs on
        entries = listed[0]
        if entries is not None:
            self.display.open_filer(ArchiveFolder(entries, f.name, f.data), (),
                                    self.options)
            return
        kind = archive_kind(f)
        if kind in ("StuffIt", "zip"):
            # an archive that is there but that the Filer cannot read:
            # say so, as the command line does, rather than complain
            # about a resource fork it never had
            self._say(f"{f.name} is a {kind} archive: unpack it first, for "
                      f"example with The Unarchiver (unar {f.name})")
            return
        try:
            target = launch_target(f)
        except ValueError as e:
            if not isinstance(self.volume, ArchiveFolder):
                self._say(str(e))
                return
            # A document inside an archive: its creator is not here to
            # open it with, so either one is on a mounted volume or the
            # archive is offered for extraction first.
            target = self._member_target(f, e)
            if target is None:
                return                    # an offer is up, or it was refused
        self._launch(target, view, trace)

    def _member_target(self, f: MacFile, why: ValueError) -> MacFile | None:
        """What to launch for a document inside this window's archive:
        the application that made it, when one is on a mounted volume —
        opening at once, as the Finder hands a document to the running
        application.  Otherwise None with an offer up to extract the
        archive and open the extracted copy; declining says why it
        cannot open."""
        made = creator_application(f.creator, self._mounted_volumes())
        if made is not None:
            return _opened_with(made, f)
        message = str(why)
        self.display.ask_question(
            "Open",
            f"“{f.name}” is a document in “{self.volume.name}”. Extract the "
            "archive and open it?",
            lambda yes: self._extract_and_open(f) if yes else self._say(message),
            detail=message)
        return None

    def _extract_and_open(self, f: MacFile) -> None:
        """Extract the archive this window browses into a folder the
        user picks, then open the extracted copy of f with the
        application that made it: beside it among the extracted files,
        or on a mounted volume."""
        archive = self.volume
        dest = QFileDialog.getExistingDirectory(self, f"Extract {archive.name} to…")
        if not dest:
            return
        try:
            written = run_busy(self, f"Extracting {archive.name}",
                               f"“{archive.name}” is extracting now; the Filer "
                               "stays usable while it runs.",
                               lambda: extract_archive(archive.raw, dest))
        except ValueError as e:
            self._say(str(e))
            return
        if written is None:
            return
        folder = HostFolder(dest)
        extracted = folder.find(f.name)
        if extracted is None:
            self._say(f"{f.name} is not among the files extracted from {archive.name}.")
            return
        made = next((c for c in folder.files()
                     if c.is_application and c.creator == extracted.creator), None)
        if made is None:
            made = creator_application(extracted.creator, self._mounted_volumes())
        if made is None:
            self._say(f"{extracted.name} is a document, and no application extracted "
                      f"with it made it (its creator is "
                      f"'{extracted.creator.decode('latin1', errors='replace')}').")
            return
        self._launch(_opened_with(made, extracted), None, None)

    def _mounted_volumes(self):
        """The System's FileSystem: the disks and disk images the Display
        has mounted, for looking up the application that made a
        document."""
        system = getattr(self.display, "system", None)
        return getattr(system, "files", None)

    def _launch(self, target: MacFile, view: bool | None,
                trace: bool | None) -> None:
        """Run what was chosen to open: an application, or the one that
        made a document with the document to open."""
        running = next((p for p in self.display.processes
                        if p.app is not None and p.app.key == target.key), None)
        if running is not None and not target.documents:
            self.display.switch_to(running)
            return
        try:
            if not target.rsrc and is_fork(target.data):
                raise ValueError(
                    f"{target.name}'s resource fork is in its data fork: it was copied "
                    "by something that didn't keep its two forks apart.")
            if not target.rsrc:
                raise ValueError(
                    f"{target.name} has no resource fork. A Mac file has to travel in a "
                    "form that keeps it: MacBinary, BinHex, AppleDouble or a disk image.")
            if not is_fork(target.rsrc):
                raise ValueError(f"{target.name}'s resource fork is damaged")
        except ValueError as e:
            self._say(str(e))
            return
        view = self.options.view if view is None else view
        if not view and not has_code(target) and not is_control_panel(target):
            print(f"{target.name} has no 68k code ({why_no_code(target)}), so macrt shows "
                  "its menus and windows from its resources.")
            view = True
        print(describe(target))
        for doc in target.documents:
            print(f"Opening {doc.name} with it.")
        try:
            self.display.launch(target.name, viewer_main if view else None, app=target,
                                trace=self.options.trace if trace is None else trace,
                                memory=self.options.memory)
        except (MemoryError, ValueError) as e:
            self._say(str(e))

    def _say(self, message: str) -> None:
        self.display.show_alert(QMessageBox.Icon.Warning, "Could Not Open", message)

    def _named(self, f: MacFile, e: Exception) -> None:
        """e's message as an alert, with the item's own name when the
        message doesn't carry it: levels apart, the name says which
        item could not be opened."""
        message = str(e)
        self._say(message if f.name in message else f"{f.name}: {message}")


def _opened_with(app: MacFile, doc: MacFile) -> MacFile:
    """`app` set to open `doc`, as launch_target leaves it: a copy, so
    the application found on a volume or extracted beside the document
    is not marked with documents for every later launch of it."""
    made = copy.copy(app)
    made.documents = (doc,)
    return made


def _depth(display) -> int:
    """The screen depth the Filer may use for colour, as the guest sees
    it: the running System's, so a guest that changes depth changes the
    Filer with it.  None only for a Display without a System (nothing
    here runs without one)."""
    system = getattr(display, "system", None)
    return getattr(system, "depth", 1)


def _icon_qicon(icon: Icon) -> QIcon:
    q = QIcon()
    q.addPixmap(QPixmap.fromImage(icon.pixels))
    return q


@lru_cache(maxsize=256)
def _fork_color_icon(rsrc: bytes, large: bool, depth: int,
                     preferred: tuple[int, ...]) -> QIcon | None:
    """A resource fork's colour icon at the preferred ids (in order) then
    any id; None when there is no colour icon for it."""
    if not is_fork(rsrc):
        return None
    try:
        resources = read_fork(rsrc)
    except Exception:
        return None
    size = LARGE if large else SMALL
    pools = [{r.type: r.data for r in resources if r.id == rid}
             for rid in preferred]
    pools.append({r.type: r.data for r in resources})
    for pool in pools:
        if not pool:
            continue
        icon = color_suite_icon(lambda kind: pool.get(kind.encode("ascii")),
                                size, depth)
        if icon is not None:
            return _icon_qicon(icon)
    return None


def item_icon_qicon(f: MacFile, *, large: bool = False, depth: int = 1) -> QIcon | None:
    """f's icon as a QIcon: its colour family at depth > 2 when its
    resources have one the depth can show, else the 1-bit icon (inverted
    when selected); None for an item with no icon of its own."""
    if _fork_pending(f):
        return None
    if depth <= 2 or f.type == FOLDER:
        rows = item_icon(f, large=large)
        return None if rows is None else icon_from_bitmap(rows)
    q = None
    if f.is_application or f.flags & HAS_CUSTOM_ICON:
        q = _fork_color_icon(bytes(f.rsrc), large, depth, (128,))
    else:
        from ..assoc import associated_color_icon, declared_color_icon
        icon = associated_color_icon(f, large=large, depth=depth)
        if icon is not None:
            q = _icon_qicon(icon)
        elif f.type in SYSTEM_FILE_ICONS:
            icon = declared_color_icon(f, large=large, depth=depth)
            q = _icon_qicon(icon) if icon is not None else _fork_color_icon(
                bytes(f.rsrc), large, depth,
                (-4064, 128) if f.type == b"cdev" else (128,))
    if q is not None:
        return q
    rows = item_icon(f, large=large)
    return None if rows is None else icon_from_bitmap(rows)


class AboutWindow(_Hosted):
    """An About window, modeled on the System 7 About dialogs: a small
    window with a title bar and close box, its lines of text drawn in the
    content area, draggable by its title."""

    def __init__(self, display: Display, title: str, lines: list[str],
                 title_icon: list[str] | None = None,
                 content_size: tuple[int, int] = (280, 100),
                 parent: QWidget | None = None,
                 title_pixmap: QPixmap | None = None):
        super().__init__(display, parent)
        self._title = title
        self._lines = lines
        self._title_icon = title_icon
        self._title_pixmap = title_pixmap
        self._content_size = content_size
        self.setWindowTitle(title)
        self._hilited = True
        self._pressed = k.wNoHit
        self._relayout()

    def frame(self) -> chrome.Frame:
        return chrome.Frame(kind=WindowKind.DOCUMENT, title=self._title,
                            title_icon=self._title_icon, title_pixmap=self._title_pixmap,
                            hilited=self._hilited, go_away=True, pressed=self._pressed)

    def content_size(self) -> tuple[int, int]:
        return self._content_size

    def _relayout(self) -> None:
        f = self.frame()
        w, h = chrome.structure_size(f, *self.content_size())
        s = chrome.shadow(f)
        self.resize(w + s, h + s)
        self.setMask(chrome.mask(f, w, h))
        self.update()

    def paintEvent(self, event):  # noqa: N802
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        p = QPainter(self)
        chrome.paint(p, f, w, h)
        rect = chrome.content_rect(f, w, h)
        p.fillRect(rect, theme.WHITE)
        p.setFont(theme.body_font())
        y = rect.top() + 20
        for line in self._lines:
            p.drawText(rect.left() + 20, y, line)
            y += 18

    def _hit(self, x: int, y: int) -> int:
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        return chrome.hit(f, w, h, x, y)

    def box_rect(self, part: int):
        if part != k.wInGoAway:
            return None
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        return chrome.close_box_rect(f, w)

    def set_pressed(self, part: int) -> None:
        if part != self._pressed:
            self._pressed = part
            self.update()

    def _set_hilited(self, hilited: bool) -> None:
        if hilited != self._hilited:
            self._hilited = hilited
            self.update()

    # -- activation, so the pinstripes and close box show when it's front --
    def changeEvent(self, event):  # noqa: N802
        if event.type() == QEvent.Type.ActivationChange and self.isWindow():
            self._set_hilited(self.isActiveWindow())
        super().changeEvent(event)

    def mousePressEvent(self, ev):  # noqa: N802
        self.raise_()
        self.activateWindow()
        self.display.front_aux_window = self
        self.display.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down=True)
        part = self._hit(ev.position().toPoint().x(), ev.position().toPoint().y())
        if part == k.wInGoAway:
            self.display._start_tracker(
                BoxTracker(self.display, Future(), self, k.wInGoAway)
            ).add_done_callback(self._close_box_done)
        elif part == k.wInDrag:
            self.display._start_tracker(DragTracker(
                self.display, Future(), self, self.mac_origin(),
                self.display._drag_limit(None), self.display.input.mac_pos))

    def mouseMoveEvent(self, ev):  # noqa: N802
        self.display.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down=None)
        t = self.display.tracker
        if isinstance(t, (BoxTracker, DragTracker)) and self._tracks(t):
            t.move(ev.globalPosition().toPoint())

    def mouseReleaseEvent(self, ev):  # noqa: N802
        self.display.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down=False)
        t = self.display.tracker
        if isinstance(t, (BoxTracker, DragTracker)) and self._tracks(t):
            t.release(ev.globalPosition().toPoint())
        self.display.input.releases = self.display.input.presses

    def _tracks(self, t) -> bool:
        return getattr(t, "view", None) is self or getattr(t, "widget", None) is self

    def _close_box_done(self, closed) -> None:
        if closed:
            self.close()

    def closeEvent(self, event):  # noqa: N802
        event.accept()
        if self.display.front_aux_window is self:
            self.display.front_aux_window = None
        if self in self.display.about_windows:
            self.display.about_windows.remove(self)

    def keyPressEvent(self, ev):  # noqa: N802
        if ev.key() == Qt.Key.Key_W and (ev.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.MetaModifier)):
            self.close()
            ev.accept()
            return
        super().keyPressEvent(ev)


class GetInfoWindow(AboutWindow):
    """A Get Info window for a Filer item, modeled on System 6 Get Info boxes."""

    def __init__(self, display: Display, item: MacFile, parent: QWidget | None = None):
        lines = [f"Name: {item.name}"]
        lines.append(f"Kind: {_kind(item) if item.type != FOLDER else 'Folder'}")
        if is_alias(item):
            original = alias_original(item, getattr(getattr(display, "system", None),
                                                    "files", None))
            if original:
                lines.append(f"Original: {original}")
        if item.type != FOLDER:
            data, rsrc = item.sizes()
            lines.append(f"Data fork: {data:,} bytes")
            lines.append(f"Resource fork: {rsrc:,} bytes")
            lines.append(f"Total size: {data + rsrc:,} bytes")
            if item.type == b"APPL":
                if sizes := size_resource(item):
                    preferred, minimum = sizes
                    lines.append(f"Minimum heap: {minimum // 1024:,}K")
                    lines.append(f"Desired heap: {preferred // 1024:,}K")
            lines.append(f"Type: {item.type.decode('latin1', errors='replace')}")
            lines.append(f"Creator: {item.creator.decode('latin1', errors='replace')}")
            if item.flags:
                lines.append(f"Flags: 0x{item.flags:04X}")
        # Add creation and modification dates if available
        if hasattr(item, 'crdate') and item.crdate:
            from datetime import datetime, timezone, timedelta
            mac_epoch = datetime(1904, 1, 1, tzinfo=timezone.utc)
            dt = mac_epoch + timedelta(seconds=item.crdate)
            lines.append(f"Created: {dt.strftime('%a, %b %d, %Y, %I:%M %p')}")
        if hasattr(item, 'mddate') and item.mddate:
            from datetime import datetime, timezone, timedelta
            mac_epoch = datetime(1904, 1, 1, tzinfo=timezone.utc)
            dt = mac_epoch + timedelta(seconds=item.mddate)
            lines.append(f"Modified: {dt.strftime('%a, %b %d, %Y, %I:%M %p')}")
        depth = _depth(display)
        title_pixmap = None
        if depth > 2:
            q = item_icon_qicon(item, depth=depth)
            title_pixmap = q.pixmap(16, 16) if q is not None else None
        super().__init__(display, f"{item.name} Info", lines,
                         title_icon=item_icon(item) or _generic_icon(item),
                         content_size=(320, 236), parent=parent,
                         title_pixmap=title_pixmap)
        self.item = item
        # The item's own icon at its full size, for the box; None keeps
        # the generic one for what it is.
        self._own_icon = item_icon(item, large=True)
        self._own_pixmap = None
        if depth > 2:
            q = item_icon_qicon(item, large=True, depth=depth)
            self._own_pixmap = q.pixmap(32, 32) if q is not None else None

    def paintEvent(self, event):  # noqa: N802
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        p = QPainter(self)
        chrome.paint(p, f, w, h)
        rect = chrome.content_rect(f, w, h)
        p.fillRect(rect, theme.WHITE)

        # Draw icon: the item's own when it has one, else the generic one
        own = self._own_icon
        if self._own_pixmap is not None:
            p.drawPixmap(rect.left() + 16, rect.top() + 16, self._own_pixmap)
        elif own is not None:
            icon_w, icon_h = theme.bitmap_size(own)
            theme.draw_bitmap(p, rect.left() + 16 + (32 - icon_w) // 2,
                              rect.top() + 16 + (32 - icon_h) // 2,
                              own, theme.BLACK)
        else:
            ico = icon(_icon_for(self.item))
            p.drawPixmap(rect.left() + 16, rect.top() + 16, 32, 32,
                         ico.pixmap(32, 32))

        # Draw details
        p.setFont(theme.body_font())
        y = rect.top() + 20
        for line in self._lines:
            p.drawText(rect.left() + 64, y, line)
            y += 18

    def closeEvent(self, event):  # noqa: N802
        event.accept()
        if self.display.front_aux_window is self:
            self.display.front_aux_window = None
        if self in self.display.info_windows:
            self.display.info_windows.remove(self)


class ContentViewerWindow(AboutWindow):
    """A viewer window for a file's contents (data and/or resource fork list and viewer)."""

    def __init__(self, display: Display, item: MacFile):
        depth = _depth(display)
        title_pixmap = None
        if depth > 2:
            q = item_icon_qicon(item, depth=depth)
            title_pixmap = q.pixmap(16, 16) if q is not None else None
        super().__init__(display, item.name, [],
                         title_icon=item_icon(item) or _generic_icon(item),
                         content_size=(720, 480), parent=None,
                         title_pixmap=title_pixmap)
        self.item = item
        self.text_only = False

        # Gather items to list: DATA if data fork present, plus resources in alphabetical order
        self.resources = []
        try:
            if item.data:
                self.resources.append(("DATA", 0, len(item.data), item.data))
        except Exception:
            pass

        try:
            if item.rsrc:
                rlist = read_fork(item.rsrc)
                # Sort resources alphabetically by type name, then id/name
                def res_key(r):
                    t_str = r.type.decode('latin1', errors='replace')
                    n_str = r.name.decode('latin1', errors='replace') if r.name else f"#{r.id}"
                    return (t_str, n_str, r.id)
                for r in sorted(rlist, key=res_key):
                    t_str = r.type.decode('latin1', errors='replace')
                    n_str = r.name.decode('latin1', errors='replace') if r.name else f"#{r.id}"
                    display_name = f"{t_str} {n_str}"
                    self.resources.append((display_name, r.id, len(r.data), r.data))
        except Exception:
            pass

        self.list_widget = QListWidget(self)
        self.list_widget.setFont(theme.body_font())
        for name, rid, size, data in self.resources:
            self.list_widget.addItem(f"{name}  ({size:,} bytes)")

        self.checkbox = QCheckBox("Text only", self)
        self.checkbox.setChecked(False)
        self.checkbox.stateChanged.connect(self._toggle_view)

        self.text_edit = QPlainTextEdit(self)
        self.text_edit.setReadOnly(True)
        self.text_edit.setFont(theme.body_font())
        self.text_edit.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)

        self.list_widget.currentRowChanged.connect(self._selection_changed)
        if self.resources:
            self.list_widget.setCurrentRow(0)

        self._update_content()
        self._relayout()

    def _selection_changed(self, row: int) -> None:
        self._update_content()

    def _toggle_view(self, state) -> None:
        self.text_only = bool(state)
        self._update_content()

    def _current_data(self) -> bytes:
        row = self.list_widget.currentRow()
        if 0 <= row < len(self.resources):
            return self.resources[row][3]
        return getattr(self.item, "data", b"")

    def _update_content(self) -> None:
        try:
            data = self._current_data()
            if not data:
                self.text_edit.setPlainText("(empty)")
                return
            # A big fork is shown from its head: hex-dumping the whole of
            # one froze the window for seconds on a large file, and the
            # first bytes are what the viewer is opened to see.
            note = ""
            if len(data) > CONTENT_LIMIT:
                note = (f"\n(showing the first {CONTENT_LIMIT:,} bytes "
                        f"of {len(data):,})")
                data = data[:CONTENT_LIMIT]
            if self.text_only:
                try:
                    text = data.decode('utf-8', errors='replace')
                except Exception:
                    text = data.decode('latin1', errors='replace')
                self.text_edit.setPlainText(text + note)
            else:
                lines = []
                for i in range(0, len(data), 16):
                    chunk = data[i:i+16]
                    hex_part = " ".join(f"{b:02X}" for b in chunk)
                    hex_part = hex_part.ljust(47)
                    ascii_part = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
                    lines.append(f"{i:08X}: {hex_part}  {ascii_part}")
                self.text_edit.setPlainText("\n".join(lines) + note)
        except Exception:
            self.text_edit.setPlainText("(unreadable data fork)")

    def _relayout(self) -> None:
        super()._relayout()
        if not hasattr(self, "list_widget") or not hasattr(self, "checkbox") or not hasattr(self, "text_edit"):
            return
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        rect = chrome.content_rect(f, w, h)
        
        # Left pane: list of resources/data (width 200)
        self.list_widget.setGeometry(rect.left() + 8, rect.top() + 8, 200, rect.height() - 16)
        
        # Right pane: checkbox and text edit
        right_x = rect.left() + 216
        right_w = rect.width() - 224
        self.checkbox.setGeometry(right_x, rect.top() + 8, 100, 24)
        self.text_edit.setGeometry(right_x, rect.top() + 38, right_w, rect.height() - 46)

    def closeEvent(self, event):  # noqa: N802
        event.accept()
        if self.display.front_aux_window is self:
            self.display.front_aux_window = None
        if self in self.display.viewer_windows:
            self.display.viewer_windows.remove(self)


class AboutClassettaWindow(AboutWindow):
    """About Classetta, modeled on System 7's About This Macintosh: the
    Classetta logo (the machine's own large icon until it arrives), the
    emulated model name, version, and build info at top; Total Memory and
    Largest Unused Block below; then a scrollable list of the System's
    heap and of the running applications with their memory usage bars."""

    def __init__(self, display: Display, parent: QWidget | None = None):
        # Get system info
        system = display.system
        model_name = system.model.name
        version = "0.1.0"  # From pyproject.toml
        build_date = "2026-10-30"  # Build date
        total_memory = 8 * 1024  # 8MB in K
        largest_free = system.largest_free() // 1024  # In K

        # The System's heap at the top of the list — how much of memory
        # is reserved for it and how far its heap has grown — then each
        # running application the same way (System/Filer processes outside
        # the application heap are not among them).
        depth = _depth(display)
        sys_heap = system.sys_heap
        processes = [("System", (sys_heap.limit - sys_heap.start) // 1024,
                      (sys_heap.end - sys_heap.start) // 1024,
                      theme.GENERIC_APP_ICON, None)]
        for proc in display.processes:
            if proc.name not in ("System", "Filer"):
                partition_size = (proc.partition.end - proc.partition.start) // 1024
                heap_used = (proc.partition.heap.end - proc.partition.heap.start) // 1024
                # The running application's own icon, else the generic one;
                # at depth > 2, its colour one when it has it
                row_icon = (theme.GENERIC_APP_ICON if proc.app is None
                            else _icon_from(bytes(proc.app.rsrc))
                                 or theme.GENERIC_APP_ICON)
                row_pixmap = None
                if depth > 2 and proc.app is not None:
                    q = _fork_color_icon(bytes(proc.app.rsrc), False, depth, (128,))
                    row_pixmap = q.pixmap(16, 16) if q is not None else None
                processes.append((proc.name, partition_size, heap_used, row_icon,
                                  row_pixmap))

        # Build the window - no title icon
        title = "About Classetta"
        lines = [
            model_name,
            "",
            f"Classetta {version}",
            f"Build {build_date}",
            "",
            f"Total Memory: {total_memory}K",
            f"Largest Unused Block: {largest_free}K",
        ]
        super().__init__(display, title, lines, content_size=(400, 500),
                         parent=parent)
        self._processes = processes
        self._total_memory = total_memory
        self._largest_free = largest_free
        self._logo_icon = (theme.MAC_II_ICON if model_name == "Macintosh II"
                           else theme.MAC_PLUS_ICON)

        # The command line's environment flags have no menu items — model,
        # depth and their kin only set up how it runs — so they live here,
        # populated.
        cfg = display.config
        self._env = [
            ("Host", cfg.host),
            ("Menu bar", cfg.menu_bar),
            ("Screen", f"{cfg.screen_size[0]}×{cfg.screen_size[1]}"
             if cfg.screen_size else "the host's"),
            ("Speed", f"{cfg.speed}× a Mac Plus" if cfg.speed is not None
             else "as fast as it goes"),
            ("Model", model_name),
            ("Depth", f"{cfg.depth} bits" if cfg.depth is not None else "the model's"),
            ("Row bytes", str(cfg.row_bytes) if cfg.row_bytes is not None
             else "a row's worth"),
            ("Sound", "on" if cfg.sound else "off"),
            ("Record sound", cfg.record_sound or "off"),
            ("Sticky menus", "on" if cfg.sticky_menus else "classic"),
            ("Rainbow apple", "on" if theme.RAINBOW_APPLE else "off"),
            ("System file", cfg.system_file.name if cfg.system_file is not None
             else "the built-in empty one"),
        ]

        # Create scroll area for application list
        self._app_list_y = 0  # Will be set in paintEvent
        self._scroll_offset = 0
        self._scroll_bar_width = 16
        self._row_height = 20

    def paintEvent(self, event):  # noqa: N802
        f = self.frame()
        w = self.width() - chrome.shadow(f)
        h = self.height() - chrome.shadow(f)
        p = QPainter(self)
        chrome.paint(p, f, w, h)
        content = chrome.content_rect(f, w, h)
        p.fillRect(content, theme.WHITE)

        cx, cy = content.x(), content.y()
        cw = content.width()

        body = theme.body_font()
        title = theme.title_font()
        p.setFont(body)
        bm = p.fontMetrics()

        # Three columns: the icon down the left, the left-hand text (the
        # model name and Total Memory), and the right-hand text (version,
        # build, and Largest Unused Block) which the rows' bars sit under.
        icon_bitmap = self._logo_icon
        icon_w, icon_h = theme.bitmap_size(icon_bitmap)
        col1_x = cx + 6
        col2_x = col1_x + icon_w + 8
        col3_x = cx + cw - 8 - max(bm.horizontalAdvance(line)
                                   for line in (self._lines[2], self._lines[3],
                                                self._lines[6]))
        mem_x = col2_x + bm.horizontalAdvance("Total Memory: ")

        p.setFont(title)
        tm = p.fontMetrics()
        row_h = max(tm.height(), bm.height())
        y_top = cy + 8

        # The logo spans both lines: line 1 beside it is the blank area
        # the model name goes underneath, with the version on the right.
        theme.draw_bitmap(p, col1_x, y_top, icon_bitmap, theme.BLACK)
        p.setFont(body)
        p.drawText(col3_x, y_top + tm.ascent(), self._lines[2])

        # Line 2: the model name beside the logo, the build under the version.
        p.setFont(title)
        p.drawText(col2_x, y_top + row_h + tm.ascent(), self._lines[0])
        p.setFont(body)
        p.drawText(col3_x, y_top + row_h + tm.ascent(), self._lines[3])

        # Line 4 (line 3 is left blank): the memory summary.
        mem_top = y_top + 3 * row_h
        p.drawText(col2_x, mem_top + bm.ascent(), self._lines[5])
        p.drawText(col3_x, mem_top + bm.ascent(), self._lines[6])

        # The horizontal line, full width across the content area.
        line_y = mem_top + row_h + 4
        p.drawLine(cx, line_y, cx + cw, line_y)

        # The environment block's height: a divider over rows of label and value.
        env_row = 16
        env_height = 12 + len(self._env) * env_row + 8

        # Application list area, above the environment block.
        self._app_list_y = line_y + 8
        app_list_height = content.bottom() - env_height - self._app_list_y
        row_height = self._row_height

        # Calculate total height of all rows
        total_rows_height = len(self._processes) * row_height
        has_scrollbar = total_rows_height > app_list_height

        # Draw scroll bar if needed
        if has_scrollbar:
            # Draw scroll bar track
            scroll_x = content.right() - self._scroll_bar_width
            p.fillRect(scroll_x, self._app_list_y, self._scroll_bar_width,
                      app_list_height, theme.WHITE)
            p.drawRect(scroll_x, self._app_list_y, self._scroll_bar_width - 1,
                      app_list_height - 1)

            # Calculate scroll bar thumb size and position
            visible_ratio = app_list_height / total_rows_height
            thumb_height = int(app_list_height * visible_ratio)
            max_scroll = total_rows_height - app_list_height
            if max_scroll > 0:
                thumb_pos = int((app_list_height - thumb_height) * (self._scroll_offset / max_scroll))
            else:
                thumb_pos = 0

            # Draw scroll bar thumb
            thumb_y = self._app_list_y + thumb_pos
            p.fillRect(scroll_x + 2, thumb_y + 2, self._scroll_bar_width - 4,
                      thumb_height - 4, theme.BLACK)

        bar_end = content.right() - (self._scroll_bar_width if has_scrollbar else 0)

        # Draw application rows
        for i, (name, partition_size, heap_used, row_icon, row_pixmap) in enumerate(
                self._processes):
            row_y = self._app_list_y + i * row_height - self._scroll_offset

            # Skip rows that are not visible
            if row_y + row_height < self._app_list_y or row_y > self._app_list_y + app_list_height:
                continue

            # Clip drawing to the app list area
            p.save()
            p.setClipRect(content.x(), self._app_list_y, content.width() - (
                self._scroll_bar_width if has_scrollbar else 0), app_list_height)

            # Draw the application's own icon (the generic one when it has none)
            p.setFont(body)
            if row_pixmap is not None:
                p.drawPixmap(col1_x, row_y + (row_height - row_pixmap.height()) // 2,
                             row_pixmap)
            else:
                row_icon_w, row_icon_h = theme.bitmap_size(row_icon)
                theme.draw_bitmap(p, col1_x, row_y + (row_height - row_icon_h) // 2,
                                  row_icon, theme.BLACK)

            # Draw application name, up to the memory column
            max_name = mem_x - col2_x - 6
            p.drawText(col2_x, row_y + 14,
                       bm.elidedText(name, Qt.TextElideMode.ElideRight, max_name))

            # Draw application memory, aligned under Total Memory's value
            p.drawText(mem_x, row_y + 14, f"{partition_size}K")

            # Draw memory bar, under Largest Unused Block
            bar_x = col3_x
            bar_width = bar_end - bar_x - 4
            bar_height = 12
            bar_y = row_y + 4

            # Draw outline
            p.drawRect(bar_x, bar_y, bar_width, bar_height)

            # Draw used portion (black) and unused portion (white)
            if partition_size > 0 and bar_width > 2:
                used_width = min(bar_width - 2,
                                 max(0, int((heap_used / partition_size) * bar_width)))
                p.fillRect(bar_x + 1, bar_y + 1, used_width, bar_height - 2, theme.BLACK)
                p.fillRect(bar_x + 1 + used_width, bar_y + 1,
                          bar_width - 2 - used_width, bar_height - 2, theme.WHITE)

            p.restore()

        # The divider over the environment, then its rows: label at the left,
        # value beside it, elided if the text is longer than the window.
        div_y = content.bottom() - env_height + 4
        p.drawLine(cx, div_y, cx + cw, div_y)
        label_x = col1_x
        value_x = col1_x + max(bm.horizontalAdvance(label + ": ")
                               for label, _ in self._env)
        for i, (label, value) in enumerate(self._env):
            y = div_y + 10 + i * env_row + bm.ascent()
            p.drawText(label_x, y, label + ":")
            p.drawText(value_x, y, bm.elidedText(value, Qt.TextElideMode.ElideRight,
                                                   content.right() - 4 - value_x))

    def mousePressEvent(self, ev):  # noqa: N802
        # Handle scroll bar clicks
        if self._scroll_offset >= 0:
            # Check if click is in scroll bar area
            f = self.frame()
            w = self.width() - chrome.shadow(f)
            h = self.height() - chrome.shadow(f)
            content = chrome.content_rect(f, w, h)
            pos = ev.position().toPoint()

            if (content.right() - self._scroll_bar_width <= pos.x() <= content.right()
                    and pos.y() >= self._app_list_y):
                # Click in scroll bar area
                total_rows_height = len(self._processes) * self._row_height
                env_row = 16
                env_height = 12 + len(self._env) * env_row + 8
                app_list_height = content.bottom() - env_height - self._app_list_y
                if pos.y() >= self._app_list_y + app_list_height:
                    return
                max_scroll = total_rows_height - app_list_height

                if max_scroll > 0:
                    # Calculate click position relative to scroll bar
                    click_y = pos.y() - self._app_list_y
                    thumb_height = int(app_list_height * (app_list_height / total_rows_height))

                    # Simple scroll: scroll up or down based on click position
                    if click_y < thumb_height // 2:
                        # Click near top - scroll up
                        self._scroll_offset = max(0, self._scroll_offset - self._row_height * 3)
                    elif click_y > app_list_height - thumb_height // 2:
                        # Click near bottom - scroll down
                        self._scroll_offset = min(max_scroll, self._scroll_offset + self._row_height * 3)
                    else:
                        # Click in middle - scroll proportionally
                        ratio = click_y / app_list_height
                        self._scroll_offset = int(max_scroll * ratio)

                    self.update()
                    return

        # Call parent for other mouse events
        super().mousePressEvent(ev)


# The Filer's menus, by the IDs the Menu Manager gives them.
APPLE_MENU_ID = 128
FILE_MENU_ID = 129
EDIT_MENU_ID = 130
VIEW_MENU_ID = 131
SPECIAL_MENU_ID = 132


class FilerMenus:
    """The Filer's menus for the menu bar, kept host-side.  The Filer is
    not a Process: it is one application with many windows, so while any
    of them is frontmost the bar shows these instead of a process's, and
    a choice from them is answered by the Display itself, as the System 7
    Application menu's is."""

    def __init__(self, display: Display):
        self.display = display
        self.last_bar: MenuBarWidget | None = None
        self.menus: dict[int, Menu] = {m.id: m for m in (
            Menu(APPLE_MENU_ID, k.APPLE_MENU_TITLE,
                 parse_menu_items("About Classetta…;(-")),
            Menu(FILE_MENU_ID, "File", parse_menu_items(
                 "(Open/O;(-;(Get Info…/I;(Display Contents/ ;(View/R;(Trace Traps/T;"
                 "(Memory…/M;(-;(Save Catalog…/S;(Extract…;(Record Sound…;(-;(Close/W;(Eject/E")),
            Menu(EDIT_MENU_ID, "Edit", parse_menu_items(
                "(Undo/Z;(-;(Cut/X;(Copy/C;(Paste/V;(Clear;(Select All/A")),
            Menu(VIEW_MENU_ID, "View", parse_menu_items(
                "(by Icon;(by Small Icon;(by Name;(by Date;(by Size;(by Kind")),
            Menu(SPECIAL_MENU_ID, "Special", parse_menu_items(
                "(Clean Up Window;(Empty Trash;(Erase Disk…;(Set Startup…;"
                "(-;Restart;Shutdown")),
        )}

    def bar_menus(self) -> list[Menu]:
        """The Filer's menu headings, with File > Open, File > Get Info,
        File > Display Contents, File > View and File > Trace Traps enabled
        only where the selection takes them, and File > Save Catalog and
        File > Extract enabled only where appropriate."""
        front = self.display.front_filer
        openable = front is not None and "open" in front.selected_actions()
        infoable = front is not None and "info" in front.selected_actions()
        displayable = front is not None and "display_contents" in front.selected_actions()
        savable = front is not None and front.savable()
        extractable = front is not None and front.extractable()
        closeable = front is not None
        file_menu = self.menus[FILE_MENU_ID]
        if file_menu.items:
            file_menu.items[0].enabled = openable
            file_menu.items[2].enabled = infoable
            file_menu.items[3].enabled = displayable
            file_menu.items[4].enabled = openable     # View acts like Open
            file_menu.items[5].enabled = openable     # Trace Traps too
            file_menu.items[6].enabled = True         # Memory… sets the window default
            file_menu.items[8].enabled = savable
            file_menu.items[9].enabled = extractable
            file_menu.items[10].enabled = True        # Record Sound… creates a host file
            file_menu.items[12].enabled = closeable
        return list(self.menus.values())
