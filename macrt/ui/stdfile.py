"""
macrt.ui.stdfile - The Standard File dialogs (Inside Macintosh: Files,
chapter 3; IM I-519), laid out as System 7's: the folder popup over the
list, the disk's name and the Eject and Desktop buttons beside it, and,
to save, the name to save as underneath.

The dialog doesn't look at the File Manager itself.  The application's
thread works out what each directory shows (it may have to ask the
application's file filter) and hands it over as a Place; the dialog
answers each step with an action, through a Future:

    ("open", i)              item i: a folder or disk to go into, or the file
    ("up", n)                the popup's nth folder out (len(path): the desktop)
    ("desktop",)
    ("new_folder", name)
    ("save", name, replacing)
    ("cancel",)
    ("item", n)              the application's own item n
    ("item", n, where)       a drawn one pressed, its top left at where on the Mac screen
    ("pick", n, value)       a pop-up's item `value` chosen
    ("select",)              a list shown, or another item in it (or none) selected
                             (only when the dialog reports it: its application has a hook)

and the application's thread answers with the next Place, or with the
same one (resume), or closes it.  The application's own items, its
buttons, check boxes, radio buttons, pop-up menus, text and edit text,
are shown under the rest as its dialog lays them out; the application's
thread says what they are each time (Extras), and they don't change
themselves when clicked: the application changes them.  What's typed in
edit text is the user's, and goes back with the state; each change is an
"item" action too.  What its dialog draws itself (user items, pictures
and icons) comes as an image of the item as drawn, under the rest.
"""

from __future__ import annotations

from concurrent.futures import Future
from dataclasses import dataclass, field
from functools import cache
from typing import TYPE_CHECKING

from PyQt6.QtCore import QPoint, QSize, Qt
from PyQt6.QtGui import QIcon, QImage, QPainter, QPixmap
from PyQt6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDialog, QFrame,
                             QHBoxLayout, QInputDialog, QLabel, QLineEdit, QListWidget,
                             QListWidgetItem, QMessageBox, QPushButton, QRadioButton, QVBoxLayout,
                             QWidget)

from . import icons, theme

if TYPE_CHECKING:
    from ..process import Process
    from .display import Display

NAME_LIMIT = 31
UNTITLED_FOLDER = "untitled folder"


@dataclass
class Item:
    name: str
    kind: str                       # "folder", "volume", "application" or "document"
    enabled: bool = True


@dataclass
class Place:
    """What one directory shows."""
    path: list[str]                 # it, then the folders it's in, out to its disk; [] the desktop
    items: list[Item] = field(default_factory=list)
    volume: str = ""                # the disk it's on
    writable: bool = True           # can a file or folder be made here
    message: str = ""               # what went wrong with the last step, to say
    selected: int = -1              # the item to select, if not the first that can be

    @property
    def desktop(self) -> bool:
        return not self.path


@dataclass
class Extra:
    """One of the application's own items."""
    number: int
    kind: str                       # "button", "check", "radio", "popup", "text", "edit"
    title: str                      # or "drawn"
    rect: tuple[int, int, int, int]     # (top, left, bottom, right) in its dialog
    value: int = 0
    enabled: bool = True
    visible: bool = True
    choices: tuple[str, ...] = ()   # a pop-up's menu
    image: QImage | None = None     # a drawn item as drawn, at rect


class PopUp(QWidget):
    """A pop-up menu control: its title, and its menu in a box."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.label = QLabel(self)
        self.box = QComboBox(self)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.label)
        row.addWidget(self.box, 1)

    def text(self) -> str:
        return self.label.text()

    def setText(self, text: str) -> None:  # noqa: N802
        self.label.setText(text)
        self.label.setVisible(bool(text))

    def set_menu(self, choices: tuple[str, ...], value: int) -> None:
        box = self.box
        box.blockSignals(True)
        if tuple(box.itemText(i) for i in range(box.count())) != choices:
            box.clear()
            box.addItems(choices)
        box.setCurrentIndex(value - 1)
        box.blockSignals(False)


class Drawn(QLabel):
    """An item the application's dialog draws itself.  Pressed, it's
    pressed on the Mac's mouse too, for the application to follow (a
    pop-up menu of its own, say) until it's released."""

    def __init__(self, parent: QWidget, dialog: StandardFileDialog, number: int):
        super().__init__(parent)
        self.dialog = dialog
        self.number = number

    def mousePressEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self.dialog._press_drawn(self, ev)

    def mouseMoveEvent(self, ev):  # noqa: N802
        self.dialog._follow_mouse(ev, None)

    def mouseReleaseEvent(self, ev):  # noqa: N802
        if ev.button() == Qt.MouseButton.LeftButton:
            self.dialog._follow_mouse(ev, False)


# ---------------------------------------------------------------------------
# Small icons, as the list shows them
# ---------------------------------------------------------------------------
FOLDER_ICON = [
    ".####..........",
    "#....#.........",
    "#.....#########",
    "#.............#",
    "###############",
    "#.............#",
    "#.............#",
    "#.............#",
    "#.............#",
    "#.............#",
    "###############",
]

DOCUMENT_ICON = [
    "########...",
    "#......##..",
    "#......#.#.",
    "#......####",
    "#.........#",
    "#.........#",
    "#.........#",
    "#.........#",
    "#.........#",
    "#.........#",
    "#.........#",
    "#.........#",
    "#.........#",
    "###########",
]

ALIAS_ICON = [
    "########...",
    "#......##..",
    "#......#.#.",
    "#......####",
    "#.........#",
    "#..#......#",
    "#..##.....#",
    "#..###....#",
    "#..####...#",
    "#..#####..#",
    "#..###....#",
    "#..##.....#",
    "#..#......#",
    "###########",
]

DISK_ICON = [
    "################",
    "#..............#",
    "#..............#",
    "#..............#",
    "#.##...........#",
    "#..............#",
    "################",
]

DESKTOP_ICON = [
    "##############",
    "#............#",
    "#.##########.#",
    "#.#........#.#",
    "#.#........#.#",
    "#.#........#.#",
    "#.##########.#",
    "#............#",
    "##############",
    "....######....",
]

CATALOG_ICON = [
    "################",
    "#..............#",
    "#.###########.#",
    "#.#.........#.#",
    "#.###########.#",
    "#.#.........#.#",
    "#.###########.#",
    "#..............#",
    "################",
]

CONTROL_PANEL_ICON = [
    "################",
    "#..............#",
    "#....####......#",
    "#....####......#",
    "#..##########..#",
    "#..##########..#",
    "#....####......#",
    "#....####......#",
    "#..............#",
    "#..............#",
    "################",
]

EXTENSION_ICON = [
    ".#.#.#.#.#.#.#.#",
    ".##############.",
    ".#............#.",
    ".#....####....#.",
    ".#....####....#.",
    ".#....####....#.",
    ".#............#.",
    ".#............#.",
    ".##############.",
    ".#.#.#.#.#.#.#.#",
]

DESK_ACCESSORY_ICON = [
    ".....####.....",
    "...########...",
    "..##########..",
    ".############.",
    "##############",
    ".############.",
    "..##########..",
    "...########...",
    ".....####.....",
]

FONT_ICON = [
    "############",
    "############",
    "###.........",
    "###.........",
    "##########..",
    "##########..",
    "###.........",
    "###.........",
    "###.........",
    "###.........",
    "###.........",
    "###.........",
]

ICONS = {"folder": FOLDER_ICON, "document": DOCUMENT_ICON, "alias": ALIAS_ICON,
         "volume": DISK_ICON, "application": theme.GENERIC_APP_ICON, "desktop": DESKTOP_ICON,
         "control_panel": CONTROL_PANEL_ICON, "extension": EXTENSION_ICON,
         "desk_accessory": DESK_ACCESSORY_ICON, "font": FONT_ICON}


def _filled(row: str) -> str:
    """The row with everything inside its outline set: white under the
    icon, so it reads on a selected row too."""
    a, b = row.find("#"), row.rfind("#")
    return row if a < 0 else "." * a + "#" * (b - a + 1) + "." * (len(row) - b - 1)


def icon_from_bitmap(bitmap: list[str]) -> QIcon:
    """Black on white from '#' and '.' rows; inverted, like its name,
    when it's selected."""
    w, h = theme.bitmap_size(bitmap)
    x, y = (16 - w) // 2, (16 - h) // 2
    out = QIcon()
    for paper, ink, mode in ((theme.WHITE, theme.BLACK, QIcon.Mode.Normal),
                             (theme.BLACK, theme.WHITE, QIcon.Mode.Selected)):
        pm = QPixmap(16, 16)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        theme.draw_bitmap(p, x, y, [_filled(row) for row in bitmap], paper)
        theme.draw_bitmap(p, x, y, bitmap, ink)
        p.end()
        out.addPixmap(pm, mode)
    return out


@cache
def icon(kind: str) -> QIcon:
    """Black on white; inverted, like its name, when it's selected."""
    return icon_from_bitmap(ICONS[kind])


_COLOUR_STANDINS = {"folder": "folder", "document": "document", "volume": "disk",
                    "application": "app", "archive": "archive", "catalog": "catalog"}


def display_depth(display) -> int:
    """The depth to make colour decisions by: the running System's, as the
    guest sees it; the configured depth; then 1."""
    system = getattr(display, "system", None)
    depth = getattr(system, "depth", None)
    if depth is None:
        config = getattr(display, "config", None)
        depth = getattr(config, "depth", None) if config is not None else None
    return depth if depth is not None else 1


@cache
def standin_icon(kind: str, depth: int) -> QIcon:
    """The system's stand-in for a kind of item: Classetta's colour art at
    depth > 2 for the kinds it has, the 1-bit icon otherwise.  At depth > 2 a
    colour stand-in is not inverted on selection; the 1-bit icon still is."""
    key = _COLOUR_STANDINS.get(kind)
    if key is not None and depth > 2:
        q = QIcon()
        q.addPixmap(icons.pixmap(key))
        return q
    return icon(kind)


STYLE = """
QDialog { background: white; }
QLabel { color: black; }
QListWidget { background: white; color: black; border: 1px solid black; outline: none; }
QListWidget::item { padding: 0px 2px; }
QListWidget::item:selected { background: black; color: white; }
QListWidget::item:disabled { color: #888888; }
QPushButton { background: white; color: black; border: 1px solid black; border-radius: 7px;
              padding: 2px 10px; min-width: 58px; min-height: 16px; }
QPushButton:default { border: 3px solid black; border-radius: 9px; }
QPushButton:pressed { background: black; color: white; }
QPushButton:disabled { color: #888888; border-color: #888888; }
QComboBox { background: white; color: black; border: 1px solid black; padding: 1px 6px; }
QComboBox QAbstractItemView { background: white; color: black; border: 1px solid black;
                              selection-background-color: black; selection-color: white; }
QLineEdit { background: white; color: black; border: 1px solid black; padding: 2px;
            selection-background-color: black; selection-color: white; }
QFrame#rule { color: black; }
QCheckBox, QRadioButton { color: black; }
QCheckBox:disabled, QRadioButton:disabled { color: #888888; }
"""


class StandardFileDialog(QDialog):
    """Open a file (put False), or name one to save (put True)."""

    def __init__(self, display: Display | None, put: bool, prompt: str = "", name: str = "",
                 title: str = "", ok: str = "", preview: QImage | None = None):
        parent = display.host_window if display is not None else None
        super().__init__(parent)
        self.display = display
        self._depth = display_depth(display) if display is not None else 1
        self.put = put
        self.ok_title = ok or ("Save" if put else "Open")
        self.place = Place([])
        self._future: Future | None = None
        self._asking = None                         # an open replace or new-folder question
        self.reports = False                        # does each change of selection come back
        self._told = -2                             # the selection last asked for (state)
        self._reporting = False                     # is the application hearing of a change
        self._pending: tuple | None = None          # what the user did meanwhile
        self.setWindowTitle(title or ("Save" if put else "Open"))
        self.setModal(False)
        self.setStyleSheet(STYLE)
        font = theme.menu_item_font()
        self.setFont(font)

        self.popup = QComboBox()
        self.popup.setMinimumWidth(200)
        self.popup.activated.connect(self._popup_chosen)
        self.list = QListWidget()
        self.list.setIconSize(QSize(16, 16))
        self.list.setUniformItemSizes(True)
        self.list.setMinimumSize(220, 160)
        self.list.itemDoubleClicked.connect(lambda _: self._open_selected())
        self.list.currentRowChanged.connect(self._row_changed)

        self.disk = QLabel()
        self.disk_icon = QLabel()
        self.disk_icon.setPixmap(standin_icon("volume", self._depth).pixmap(16, 16))
        self.eject = QPushButton("Eject")
        self.eject.setEnabled(False)                # nothing here is removable
        self.desk = QPushButton("Desktop")
        self.desk.clicked.connect(lambda: self._act("desktop"))
        self.new_folder = QPushButton(standin_icon("folder", self._depth), "New")
        self.new_folder.clicked.connect(self._ask_new_folder)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(self.cancel)
        self.ok = QPushButton(self.ok_title)
        self.ok.setDefault(True)
        self.ok.setAutoDefault(True)
        self.ok.clicked.connect(self._default)
        for b in (self.eject, self.desk, self.new_folder, self.cancel_button):
            b.setAutoDefault(False)
        self.prompt = QLabel(prompt)
        self.name = QLineEdit(name.replace(":", "-")[:NAME_LIMIT])
        self.name.setMaxLength(NAME_LIMIT)
        self.name.textChanged.connect(lambda _: self._update_buttons())
        self.name.textEdited.connect(lambda _: self.list.setCurrentRow(-1))
        self.message = QLabel()
        self.extras = QWidget()             # the application's own items
        self.extras.hide()
        self._extras: dict[int, QWidget] = {}
        self._values: dict[int, int] = {}

        self.preview = QLabel()             # what's to be published
        self.preview.setObjectName("preview")
        if preview is not None:
            self.preview.setPixmap(QPixmap.fromImage(preview))

        left = QVBoxLayout()
        left.addWidget(self.popup, 0, Qt.AlignmentFlag.AlignLeft)
        left.addWidget(self.list, 1)
        if put:
            left.addWidget(self.prompt)
            left.addWidget(self.name)
        left.addWidget(self.message)
        right = QVBoxLayout()
        disk = QHBoxLayout()
        disk.addWidget(self.disk_icon)
        disk.addWidget(self.disk, 1)
        right.addLayout(disk)
        right.addWidget(self.eject)
        right.addWidget(self.desk)
        if put:
            right.addWidget(self.new_folder)
        rule = QFrame()
        rule.setObjectName("rule")
        rule.setFrameShape(QFrame.Shape.HLine)
        right.addWidget(rule)
        right.addStretch(1)
        right.addWidget(self.cancel_button)
        right.addWidget(self.ok)
        top = QHBoxLayout()
        if preview is not None:
            top.addWidget(self.preview, 0, Qt.AlignmentFlag.AlignTop)
            top.addSpacing(10)
        top.addLayout(left, 1)
        top.addSpacing(10)
        top.addLayout(right)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(13, 10, 13, 13)
        outer.addLayout(top, 1)
        outer.addWidget(self.extras)
        if not put:
            self.new_folder.hide()
            self.prompt.hide()
            self.name.hide()

    # =====================================================================
    # The application's thread
    # =====================================================================
    def show_place(self, place: Place, extras: list[Extra] | None = None) -> tuple[Future]:
        """Show a directory and wait for the next step.  (A tuple, so the
        bridge doesn't wait for the Future itself.)"""
        self.set_extras(extras or [])
        self.place = place
        self.popup.blockSignals(True)
        self.popup.clear()
        for i, folder in enumerate(place.path):
            kind = "volume" if i == len(place.path) - 1 else "folder"
            self.popup.addItem(standin_icon(kind, self._depth), folder)
        self.popup.addItem(standin_icon("desktop", self._depth), "Desktop")
        self.popup.setCurrentIndex(0)
        self.popup.blockSignals(False)
        self.list.clear()
        for item in place.items:
            w = QListWidgetItem(standin_icon(item.kind, self._depth), item.name)
            if not item.enabled:
                w.setFlags(w.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            self.list.addItem(w)
        row = place.selected
        if not (0 <= row < len(place.items) and place.items[row].enabled):
            row = -1 if self.put else next(
                (i for i, item in enumerate(place.items) if item.enabled), -1)
        if row >= 0:
            self.list.setCurrentRow(row)
        self._told = -2                     # new to the application
        self._reporting, self._pending = False, None    # a step in the last list is no step
        self.disk.setText(place.volume or "Desktop")
        self.disk_icon.setPixmap(standin_icon("volume" if place.volume else "desktop",
                                              self._depth).pixmap(16, 16))
        self.message.setText(place.message)
        self.message.setVisible(bool(place.message))
        fut = self._future = Future()
        self._update_buttons()
        if self.put:
            self.name.setFocus()
            self.name.selectAll()
        else:
            self.list.setFocus()
        if not self.isVisible():
            self.show()
            self.raise_()
            self.activateWindow()
        self._report()
        return (fut,)

    def resume(self, extras: list[Extra], name: str | None = None,
               message: str = "") -> tuple[Future]:
        """Wait for the next step where the dialog is, its application's
        items as they are now and, if it's changed, the name to save as."""
        self.set_extras(extras)
        if name is not None:
            self.name.setText(name.replace(":", "-")[:NAME_LIMIT])
        if message:
            self._tell(message)
        fut = self._future = Future()
        self._reporting = False
        self._update_buttons()
        pending, self._pending = self._pending, None
        if pending is not None:
            self._act(*pending)
        else:
            self._report()
        return (fut,)

    def state(self) -> tuple[int, str, dict[int, str]]:
        """The item selected (-1 for none), the name to save as, and
        what's in the application's edit text items."""
        self._told = self._selected()
        return self._told, self.name.text(), {
            n: w.text() for n, w in self._extras.items() if isinstance(w, QLineEdit)}

    def set_extras(self, extras: list[Extra]) -> None:
        """Show the application's items as they are."""
        if not extras:
            return
        if not self._extras:
            self._make_extras(extras)
        for e in extras:
            w = self._extras.get(e.number)
            if w is None:
                continue
            if isinstance(w, Drawn):
                if e.image is not None:
                    w.setPixmap(QPixmap.fromImage(e.image))
            elif w.text() != e.title:
                w.setText(e.title)
            if isinstance(w, (QCheckBox, QRadioButton)):
                w.setChecked(bool(e.value))
            elif isinstance(w, PopUp):
                w.set_menu(e.choices, e.value)
            self._values[e.number] = e.value
            w.setEnabled(e.enabled)
            w.setVisible(e.visible)

    def _make_extras(self, extras: list[Extra]) -> None:
        """Widgets for the application's items, placed as its dialog
        places them, below the rest."""
        top = min(e.rect[0] for e in extras)
        left = min(e.rect[1] for e in extras)
        bottom = right = 0
        for e in extras:
            if e.kind == "text":
                w = QLabel(self.extras)
                w.setWordWrap(True)
            elif e.kind == "edit":
                w = QLineEdit(self.extras)
                w.setMaxLength(255)
                w.textEdited.connect(lambda _, n=e.number: self._act("item", n))
            elif e.kind == "popup":
                w = PopUp(self.extras)
                w.box.activated.connect(lambda i, n=e.number: self._pick(n, i + 1))
            elif e.kind == "drawn":
                w = Drawn(self.extras, self, e.number)
                w.lower()                   # under the rest, which it may run into
            else:
                w = {"button": QPushButton, "check": QCheckBox,
                     "radio": QRadioButton}[e.kind](self.extras)
                if isinstance(w, QPushButton):
                    w.setAutoDefault(False)
                if isinstance(w, QRadioButton):
                    w.setAutoExclusive(False)
                w.clicked.connect(lambda _=False, n=e.number: self._clicked(n))
            t, lft, b, r = e.rect
            if e.kind == "drawn":
                height, width = b - t, r - lft
            else:
                w.setText(e.title)
                height = max(b - t, w.sizeHint().height())
                width = max(r - lft, w.sizeHint().width() if e.kind != "text" else 0)
            w.setGeometry(lft - left, t - top, width, height)
            bottom, right = max(bottom, t - top + height), max(right, lft - left + width)
            self._extras[e.number] = w
        self.extras.setFixedSize(right, bottom)
        self.extras.show()

    def _clicked(self, n: int) -> None:
        """An application's item: it, not the click, says what it shows."""
        w = self._extras[n]
        if isinstance(w, QAbstractButton) and w.isCheckable():
            w.setChecked(bool(self._values.get(n, 0)))
        self._act("item", n)

    def _pick(self, n: int, value: int) -> None:
        """A pop-up's item chosen: the application sets the control's value."""
        w = self._extras[n]
        w.set_menu(tuple(w.box.itemText(i) for i in range(w.box.count())),
                   self._values.get(n, 0))
        self._act("pick", n, value)

    def _press_drawn(self, w: Drawn, ev) -> None:
        d = self.display
        where = None
        if d is not None:
            d.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), True)
            where = d.mac_from_global(w.mapToGlobal(QPoint(0, 0)))
        self._act("item", w.number, where)

    def _follow_mouse(self, ev, down: bool | None) -> None:
        """The mouse, pressed on a drawn item, for a menu it pops up."""
        d = self.display
        if d is None:
            return
        d.update_mouse(ev.globalPosition().toPoint(), ev.modifiers(), down)
        if d.tracker is not None:
            if down is None:
                d.tracker.move(d.input.global_pos)
            else:
                d.tracker.release(d.input.global_pos)

    def press(self, n: int) -> None:
        """Click the application's item n (for tests)."""
        w = self._extras[n]
        if isinstance(w, Drawn):
            self._act("item", n, None)
        else:
            w.click()

    def pick(self, n: int, value: int) -> None:
        """Choose item `value` of the application's pop-up n (for tests)."""
        self._pick(n, value)

    def finish(self) -> None:
        """The application has its answer."""
        self._future = self._pending = None
        self._reporting = False
        if self._asking is not None:
            self._asking.close()
        if self.display is not None and self in self.display.file_dialogs:
            self.display.file_dialogs.remove(self)
        self.hide()
        self.deleteLater()

    # =====================================================================
    # The user
    # =====================================================================
    def _act(self, *action) -> None:
        fut, self._future = self._future, None
        if fut is None or fut.done():
            if self._reporting and self._pending is None:
                self._pending = action      # for when the application's heard
            return
        if action[0] == "select":           # the user can go on meanwhile
            self._reporting = True
        else:
            self._update_buttons()
        fut.set_result(action)

    def _can_act(self) -> bool:
        return self._future is not None or self._reporting

    def _row_changed(self, _row: int) -> None:
        self._update_buttons()
        self._report()

    def _report(self) -> None:
        """Tell the application's thread the selection has changed since
        it last asked, if it wants to know."""
        if self.reports and self._future is not None and self._selected() != self._told:
            self._act("select")

    def _selected(self) -> int:
        row = self.list.currentRow()
        if 0 <= row < len(self.place.items) and self.place.items[row].enabled:
            return row
        return -1

    def _selected_folder(self) -> bool:
        row = self._selected()
        return row >= 0 and self.place.items[row].kind in ("folder", "volume")

    def _update_buttons(self) -> None:
        waiting = self._can_act()
        self.cancel_button.setEnabled(waiting)
        self.desk.setEnabled(waiting and not self.place.desktop)
        if self.put:
            folder = self._selected_folder()
            self.ok.setText("Open" if folder else self.ok_title)
            self.ok.setEnabled(waiting and (folder or (bool(self.name.text().strip()) and
                                                       self.place.writable and
                                                       not self.place.desktop)))
            self.new_folder.setEnabled(waiting and self.place.writable and
                                       not self.place.desktop)
        else:
            self.ok.setEnabled(waiting and self._selected() >= 0)

    def _default(self) -> None:
        if self.put and not self._selected_folder():
            self._save()
        else:
            self._open_selected()

    def _open_selected(self) -> None:
        row = self._selected()
        if row >= 0 and self._can_act():
            if self.put and not self._selected_folder():
                return
            self._act("open", row)

    def _popup_chosen(self, index: int) -> None:
        if index > 0:
            self._act("up", index)

    def _existing(self, name: str) -> Item | None:
        key = name.lower()
        return next((i for i in self.place.items if i.name.lower() == key), None)

    def _save(self) -> None:
        name = self.name.text().replace(":", "-").strip()
        if not name or not self._can_act() or not self.place.writable or self.place.desktop:
            return
        existing = self._existing(name)
        if existing is None:
            self._act("save", name, False)
        elif existing.kind in ("folder", "volume"):
            self._tell(f"“{name}” is a folder here; choose another name.")
        else:
            self._confirm_replace(name)

    def _confirm_replace(self, name: str) -> None:
        box = QMessageBox(QMessageBox.Icon.Warning, self.windowTitle(),
                          f"Replace existing “{name}”?",
                          QMessageBox.StandardButton.Cancel, self)
        replace = box.addButton("Replace", QMessageBox.ButtonRole.AcceptRole)
        box.setDefaultButton(QMessageBox.StandardButton.Cancel)

        def answered(_):
            self._asking = None
            if box.clickedButton() is replace:
                self._act("save", name, True)
        box.finished.connect(answered)
        self._asking = box
        box.open()

    def _ask_new_folder(self) -> None:
        if not self._can_act():
            return
        ask = QInputDialog(self)
        ask.setWindowTitle(self.windowTitle())
        ask.setLabelText("Name of new folder:")
        ask.setTextValue(UNTITLED_FOLDER)
        ask.setOkButtonText("Create")

        def answered(result):
            self._asking = None
            name = ask.textValue().replace(":", "-").strip()[:NAME_LIMIT]
            if result and name:
                self._act("new_folder", name)
        ask.finished.connect(answered)
        self._asking = ask
        ask.open()

    def _tell(self, text: str) -> None:
        self.message.setText(text)
        self.message.setVisible(True)

    def cancel(self) -> None:
        self._act("cancel")

    def reject(self) -> None:  # Escape, or the close box
        self.cancel()

    def closeEvent(self, event):  # noqa: N802
        event.ignore()
        self.cancel()

    # =====================================================================
    # For tests, and anything else that drives the dialog
    # =====================================================================
    def names(self) -> list[str]:
        return [item.name for item in self.place.items]

    def waiting(self) -> bool:
        """Is it waiting for the user's next step?"""
        return self._future is not None

    def choose(self, name: str) -> None:
        """Select an item by name and open it."""
        row = next(i for i, item in enumerate(self.place.items) if item.name == name)
        self.list.setCurrentRow(row)
        self._open_selected()

    def make_folder(self, name: str) -> None:
        self._act("new_folder", name)

    def save_as(self, name: str, replace: bool = True) -> None:
        existing = self._existing(name)
        if existing is not None and existing.kind not in ("folder", "volume"):
            if replace:
                self._act("save", name, True)
            return
        self.name.setText(name)
        self._save()


def open_dialog(display: Display, proc: Process, put: bool, prompt: str, name: str,
                place: Place, extras: list[Extra] | None = None, ok: str = "",
                preview: QImage | None = None,
                reports: bool = False) -> tuple[StandardFileDialog, Future]:
    """On the GUI thread: show a new dialog at place."""
    dlg = StandardFileDialog(display, put, prompt, name, proc.name, ok, preview)
    dlg.reports = reports
    display.file_dialogs.append(dlg)
    (fut,) = dlg.show_place(place, extras)
    return dlg, fut
