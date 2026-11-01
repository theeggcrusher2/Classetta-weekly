"""
The Standard File dialog itself (macrt.ui.stdfile), with nothing behind
it: what each thing the user does there answers the application's
thread with.  (test_stdfile_traps.py runs it from the traps.)
"""

from types import SimpleNamespace

import pytest
from PyQt6.QtCore import QEvent, QPoint, QPointF, Qt
from PyQt6.QtGui import QImage, QMouseEvent
from PyQt6.QtWidgets import QInputDialog, QMessageBox

from macrt.ui.stdfile import Extra, Item, Place, StandardFileDialog

DISK = Place(["Disk"], [Item("Gone", "document", enabled=False), Item("Old", "document"),
                        Item("Box", "folder")], volume="Disk")
DRAWN = Extra(10, "drawn", "", (0, 0, 20, 40), image=QImage(40, 20, QImage.Format.Format_RGB32))


class Tracker:
    def __init__(self):
        self.moves, self.releases = [], []

    def move(self, pos):
        self.moves.append(pos)

    def release(self, pos):
        self.releases.append(pos)


class Screen:
    """What the dialog asks of the display: the Mac's mouse, and where a
    point on the host is on the Mac's screen."""
    host_window = None

    def __init__(self):
        self.file_dialogs = []
        self.mouse = []
        self.input = SimpleNamespace(global_pos=QPoint(5, 6))
        self.tracker = None

    def update_mouse(self, pos, modifiers, down):
        self.mouse.append((pos, down))

    def mac_from_global(self, g):
        return ("mac", g.x(), g.y())


@pytest.fixture(autouse=True)
def _dialogs_closed(qapp):
    """A dialog a test leaves up is hidden after it, as the application's
    answer would: the survey's tests, run after these in the same process,
    would take it for one of macrt's own dialogs waiting for the user."""
    yield
    for w in qapp.topLevelWidgets():
        if w.isVisible() and w.windowType() == Qt.WindowType.Dialog:
            w.hide()


def mouse(kind, button=Qt.MouseButton.LeftButton):
    return QMouseEvent(kind, QPointF(1, 2), QPointF(30, 40), button, button,
                       Qt.KeyboardModifier.NoModifier)


def test_a_drawn_item_pressed_presses_the_macs_mouse(qapp):
    d = Screen()
    dlg = StandardFileDialog(d, False)
    d.file_dialogs.append(dlg)
    (fut,) = dlg.show_place(DISK, [DRAWN])
    w = dlg._extras[10]
    w.mousePressEvent(mouse(QEvent.Type.MouseButtonPress, Qt.MouseButton.RightButton))
    assert not fut.done() and d.mouse == []
    w.mousePressEvent(mouse(QEvent.Type.MouseButtonPress))
    top_left = w.mapToGlobal(QPoint(0, 0))
    assert fut.result(0) == ("item", 10, ("mac", top_left.x(), top_left.y()))
    assert d.mouse == [(QPoint(30, 40), True)]
    # Followed, for a menu of the application's own, until it's let go.
    d.tracker = t = Tracker()
    w.mouseMoveEvent(mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton))
    w.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease, Qt.MouseButton.RightButton))
    assert (t.moves, t.releases, d.mouse[1:]) == ([QPoint(5, 6)], [], [(QPoint(30, 40), None)])
    w.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease))
    assert (t.moves, t.releases, d.mouse[2:]) == ([QPoint(5, 6)], [QPoint(5, 6)],
                                                  [(QPoint(30, 40), False)])
    d.tracker = None
    w.mouseMoveEvent(mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton))
    assert len(d.mouse) == 4
    dlg.finish()
    assert d.file_dialogs == []
    # With no display, the press is just the item's.
    dlg = StandardFileDialog(None, False)
    (fut,) = dlg.show_place(DISK, [DRAWN])
    w = dlg._extras[10]
    w.mousePressEvent(mouse(QEvent.Type.MouseButtonPress))
    w.mouseMoveEvent(mouse(QEvent.Type.MouseMove, Qt.MouseButton.NoButton))
    w.mouseReleaseEvent(mouse(QEvent.Type.MouseButtonRelease))
    assert fut.result(0) == ("item", 10, None)


def test_open_opens_whats_selected(qapp):
    dlg = StandardFileDialog(None, False)
    (fut,) = dlg.show_place(DISK)
    dlg.ok.click()
    assert fut.result(0) == ("open", 1)                  # the first that can be


def test_saving_asks_before_replacing_a_file(qapp):
    dlg = StandardFileDialog(None, True, "Save as:", "Draft")
    (fut,) = dlg.show_place(DISK)
    dlg.name.setText("Box")
    dlg.ok.click()
    assert not fut.done() and not dlg.message.isHidden()
    assert dlg.message.text() == "“Box” is a folder here; choose another name."
    dlg.name.setText("old")
    dlg.ok.click()
    box = dlg._asking
    assert isinstance(box, QMessageBox) and box.text() == "Replace existing “old”?"
    assert box.defaultButton() is box.button(QMessageBox.StandardButton.Cancel)
    box.button(QMessageBox.StandardButton.Cancel).click()
    assert not fut.done() and dlg._asking is None
    dlg.ok.click()
    next(b for b in dlg._asking.buttons() if b.text() == "Replace").click()
    assert fut.result(0) == ("save", "old", True) and dlg._asking is None
    dlg.save_as("New")                                  # nothing waits for it
    assert not dlg.waiting() and not dlg.ok.isEnabled()
    (fut,) = dlg.resume([], name="a:b", message="The disk is full.")
    assert (dlg.name.text(), dlg.message.text()) == ("a-b", "The disk is full.")
    dlg.ok.click()
    assert fut.result(0) == ("save", "a-b", False)
    # A folder selected is opened; a file isn't, whatever's clicked.
    (fut,) = dlg.show_place(DISK)
    dlg.list.setCurrentRow(1)
    dlg.list.itemDoubleClicked.emit(dlg.list.item(1))
    assert not fut.done() and dlg.ok.text() == "Save"
    dlg.list.setCurrentRow(2)
    assert dlg.ok.text() == "Open"
    dlg.ok.click()
    assert fut.result(0) == ("open", 2)
    # The application has its answer while the user is still asked.
    (fut,) = dlg.show_place(DISK)
    dlg.name.setText("Old")
    dlg.ok.click()
    box = dlg._asking
    assert box.isVisible()
    dlg.finish()
    assert not box.isVisible() and not dlg.isVisible()


def test_the_folder_popup_a_new_folder_and_closing(qapp):
    dlg = StandardFileDialog(None, True, name="x")
    place = Place(["Inner", "Outer", "Disk"], volume="Disk")
    (fut,) = dlg.show_place(place)
    dlg.popup.activated.emit(0)                         # where it is already
    assert not fut.done()
    dlg.popup.activated.emit(2)
    assert fut.result(0) == ("up", 2)
    (fut,) = dlg.show_place(place)
    dlg.new_folder.click()
    ask = dlg._asking
    assert isinstance(ask, QInputDialog) and ask.isVisible()
    assert ask.windowTitle() == dlg.windowTitle() == "Save"
    assert (ask.labelText(), ask.textValue(), ask.okButtonText()) == (
        "Name of new folder:", "untitled folder", "Create")
    ask.reject()
    assert not fut.done() and dlg._asking is None
    dlg.new_folder.click()
    dlg._asking.setTextValue("   ")
    dlg._asking.accept()
    assert not fut.done()
    dlg.new_folder.click()
    dlg._asking.setTextValue(" a:b ")
    dlg._asking.accept()
    assert fut.result(0) == ("new_folder", "a-b")
    dlg._ask_new_folder()                               # nothing waits for it
    assert dlg._asking is None
    (fut,) = dlg.show_place(place)
    dlg.reject()                                        # Escape
    assert fut.result(0) == ("cancel",)
    (fut,) = dlg.show_place(place)
    dlg.close()                                         # the close box
    assert fut.result(0) == ("cancel",) and dlg.isVisible()


def test_the_applications_items_change_as_it_says(qapp):
    dlg = StandardFileDialog(None, False)
    dlg.show_place(DISK, [Extra(1, "button", "One", (0, 0, 20, 60))])
    w = dlg._extras[1]
    assert w.text() == "One"
    dlg.resume([Extra(1, "button", "Uno", (0, 0, 20, 60)),
                Extra(9, "check", "Nine", (30, 0, 50, 60))])    # made after the dialog's items
    assert w.text() == "Uno" and set(dlg._extras) == {1}
