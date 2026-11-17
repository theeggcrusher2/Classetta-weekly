"""The Dialog Manager's traps as a program calls them: which dialog an
event is for, DialogSelect and ModalDialog with and without a filter
procedure, the standard filter's keys, getting, setting, hiding and
finding items and their text, drawing them, making and closing dialogs,
alert stages and their sounds, and appending items that don't fit
(Inside Macintosh I, the Dialog Manager)."""

import struct
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from builders import alrt, ditl, dlog
from guest import Guest
from test_dialogs import NoDisplay

from macrt.m68k import glue_dialogs
from macrt.resfork import Resource
from macrt.toolbox import constants as k
from macrt.toolbox.controls import Control
from macrt.toolbox.dialogs import HIDDEN, Dialog, cautionIcon, items, noteIcon
from macrt.toolbox.events import EventRecord
from macrt.toolbox.quickdraw import new_rgn
from macrt.toolbox.templates import btnCtrl, ctrlItem, editText, iconItem, itemDisable, picItem
from macrt.toolbox.templates import statText, userItem
from macrt.toolbox.textedit import TERec
from macrt.toolbox.types import Point, Rect

IN_FRONT = 0xFFFFFFFF
BOUNDS = Rect(40, 40, 140, 340)                     # 100 by 300, its content at (40, 40)
OK, CANCEL, TEXT, FIRST, SECOND, USER = range(1, 7)
ITEMS = ((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
         (ctrlItem + btnCtrl, (70, 150, 90, 210), b"Cancel"),
         (statText, (10, 10, 26, 200), b"Name: ^0"),
         (editText, (40, 10, 56, 140), b"First"),
         (editText, (40, 160, 56, 290), b"Second"),
         (userItem | itemDisable, (10, 210, 26, 290), b""))
RETURN, ENTER, ESCAPE, TAB = 0x0D, 0x03, 0x1B, 0x09
BLACK_ICON = bytes([0xFF]) * 128
PICTURE = (struct.pack(">H4h", 22, 0, 0, 20, 20) + b"\x11\x01"     # version 1
           + b"\x31" + struct.pack(">4h", 0, 0, 20, 20) + b"\xFF")  # paintRect, the end
BLANK = PICTURE.replace(b"\x31", b"\x32")                           # eraseRect


class Host(NoDisplay):
    """The display, with the application in front: a click is in the
    content of the frontmost window whose content has it, or in its title
    bar just above, and on the desk if none has.  The button is let go of
    at once."""

    input = SimpleNamespace(down=False, presses=0, releases=0, mac_pos=Point(0, 0))

    @staticmethod
    def is_front(process):
        return True

    @staticmethod
    def find_window(process, pt):
        for w in process.windows:
            c = w.content
            if w.visible and c.contains(pt):
                return k.inContent, w
            if w.visible and Rect(c.top - 18, c.left, c.top, c.right).contains(pt):
                return k.inDrag, w
        return k.inDesk, None


@pytest.fixture
def g(qapp):
    g = Guest([Resource(b"ICON", 128, BLACK_ICON), Resource(b"PICT", 128, PICTURE),
               Resource(b"PICT", 129, BLANK),
               Resource(b"DLOG", 200, dlog(BOUNDS, "", 999)),      # its DITL isn't there
               Resource(b"DITL", 300, ditl(*ITEMS[:2])),
               Resource(b"ALRT", 300, alrt(BOUNDS, 300, 0x0321)),   # beeps 1, 2, 3, 0 times
               Resource(b"ALRT", 301, alrt(BOUNDS, 999, 0x5555)),
               Resource(b"ALRT", 302, alrt(BOUNDS, 300, 0xCCC4))])  # Cancel bold from stage 2
    g.tb._d = Host()
    waits = iter(range(200))

    def yielding():
        assert next(waits, None) is not None, "it waits for ever"
        return nullcontext()
    g.yielding = yielding
    g.sleep = lambda ticks: None
    g.beeps = []
    g.tb.SysBeep = lambda duration=1: g.beeps.append(duration)
    return g


def new_dialog(g, *found, visible=True, bounds=BOUNDS):
    return g("NewDialog", 0, g.rect_ptr(bounds), g.ptr(b"\0"), visible, k.dBoxProc, IN_FRONT,
             False, 0, g.handle(ditl(*(found or ITEMS))))


def item(g, w, n):
    return items(g.mem, Dialog(g.mem, w.addr).items)[n - 1]


def record(g, w) -> TERec:
    return TERec(g.mem, g.mem.r32(Dialog(g.mem, w.addr).text_h) & 0xFFFFFF)


def ev(what, message=0, where=Point(0, 0), modifiers=0):
    return EventRecord(what, message, 1, where, modifiers)


def key(ch, modifiers=0):
    return ev(k.keyDown, ch, modifiers=modifiers)


def at(w, v, h):
    """A local point of the dialog's, globally."""
    return Point(w.content.top + v, w.content.left + h)


def event_ptr(g, e) -> int:
    p = g.ptr(bytes(16))
    g.write_event(p, e)
    return p


def select(g, e):
    """DialogSelect: whether it hit, the dialog and the item it says."""
    dialog_p, item_p = g.ptr(bytes(4)), g.ptr(b"\xff\xff")
    hit = g("DialogSelect", event_ptr(g, e), dialog_p, item_p)
    return bool(hit), g.mem.r32(dialog_p), g.mem.r16s(item_p)


def test_the_dialog_an_event_is_for(g):
    """An update or activate event's window, the window clicked in if the
    click's in its content, and for anything else the front window: if
    it's a dialog, DialogSelect handles the event for it."""
    plain = g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), True,
              k.documentProc, IN_FRONT, True, 0)
    w = new_dialog(g)

    def is_dialog_event(e):
        return g("IsDialogEvent", event_ptr(g, e))
    assert is_dialog_event(ev(k.updateEvt, w.addr)) and is_dialog_event(ev(k.activateEvt, w.addr))
    assert not is_dialog_event(ev(k.updateEvt, plain.addr))
    assert is_dialog_event(ev(k.mouseDown, where=at(w, 5, 5)))
    assert not is_dialog_event(ev(k.mouseDown, where=at(w, -5, 5)))      # its title bar
    assert not is_dialog_event(ev(k.mouseDown, where=Point(250, 60)))     # the plain window
    assert not is_dialog_event(ev(k.mouseDown, where=Point(10, 400)))     # the desk
    assert is_dialog_event(key(ord("a")))
    # An update is drawn, and hits nothing.
    w.update_pending = True
    assert select(g, ev(k.updateEvt, w.addr)) == (False, w.addr, -1)
    assert not w.update_pending
    g("SelectWindow", plain.addr)
    assert not is_dialog_event(key(ord("a")))
    assert select(g, key(ord("a"))) == (False, 0, -1)
    assert is_dialog_event(ev(k.activateEvt, w.addr))          # its deactivation, behind
    assert not is_dialog_event(ev(k.activateEvt, plain.addr))


def test_dialog_select_on_the_items(g, monkeypatch):
    """A click in an enabled item hits it, a control's only if it's
    released in it; a disabled item, a dimmed control and no item at all
    aren't hit.  A click in an edit field makes it the one being edited."""
    tracked = []

    def track(t, h, pt, action):
        tracked.append((h, pt))
        return part
    monkeypatch.setattr(glue_dialogs, "TrackControl", track)
    w = new_dialog(g)
    part = k.inButton
    assert select(g, ev(k.mouseDown, where=at(w, 80, 250))) == (True, w.addr, OK)
    assert tracked == [(item(g, w, OK).handle, Point(80, 250))]
    part = 0                                            # let go of outside it
    assert select(g, ev(k.mouseDown, where=at(w, 80, 250))) == (False, w.addr, -1)
    g("HiliteControl", item(g, w, OK).handle, 255)
    assert select(g, ev(k.mouseDown, where=at(w, 80, 250))) == (False, w.addr, -1)
    g("HideDialogItem", w.addr, CANCEL)
    assert select(g, ev(k.mouseDown, where=at(w, 80, 160))) == (False, w.addr, -1)
    g("ShowDialogItem", w.addr, CANCEL)
    g("HideControl", item(g, w, CANCEL).handle)
    assert select(g, ev(k.mouseDown, where=at(w, 80, 160))) == (False, w.addr, -1)
    assert len(tracked) == 2
    g("HiliteControl", item(g, w, OK).handle, 0)
    rect_p = g.rect_ptr(item(g, w, OK).rect)
    g("SetDialogItem", w.addr, OK, ctrlItem + btnCtrl + itemDisable, item(g, w, OK).handle,
      rect_p)
    part = k.inButton
    assert select(g, ev(k.mouseDown, where=at(w, 80, 250))) == (False, w.addr, -1)
    assert len(tracked) == 3
    assert select(g, ev(k.mouseDown, where=at(w, 15, 20))) == (True, w.addr, TEXT)
    assert select(g, ev(k.mouseDown, where=at(w, 15, 250))) == (False, w.addr, -1)   # disabled
    assert select(g, ev(k.mouseDown, where=at(w, 95, 5))) == (False, w.addr, -1)     # no item
    d = Dialog(g.mem, w.addr)
    assert d.edit_field == FIRST - 1
    assert select(g, ev(k.mouseDown, where=at(w, 45, 172))) == (True, w.addr, SECOND)
    assert d.edit_field == SECOND - 1 and record(g, w).h_text == item(g, w, SECOND).handle
    assert record(g, w).active
    assert 0 < record(g, w).sel_start == record(g, w).sel_end < len(b"Second")     # clicked


def test_dialog_select_on_keys_and_other_events(g, monkeypatch):
    """Keys are typed into the edit field, unless they're Command keys;
    Tab goes on to the next field, round to the first.  Activate events
    activate the field and deactivate it; null events blink its caret."""
    idled = []
    monkeypatch.setattr(glue_dialogs.te, "idle", lambda t, h: idled.append(h))
    w = new_dialog(g)
    d = Dialog(g.mem, w.addr)
    assert d.edit_field == FIRST - 1
    assert select(g, key(TAB)) == (False, w.addr, -1)
    assert d.edit_field == SECOND - 1
    assert (record(g, w).sel_start, record(g, w).sel_end) == (0, len(b"Second"))
    assert select(g, key(TAB)) == (False, w.addr, -1)
    assert d.edit_field == FIRST - 1
    assert select(g, key(ord("x"))) == (True, w.addr, FIRST)
    assert select(g, key(ord("y"), k.cmdKey)) == (False, w.addr, -1)
    assert g.contents(item(g, w, FIRST).handle) == b"x"
    assert select(g, ev(k.activateEvt, w.addr, modifiers=0)) == (False, w.addr, -1)
    assert not record(g, w).active
    assert select(g, ev(k.activateEvt, w.addr, modifiers=k.activeFlag)) == (False, w.addr, -1)
    assert record(g, w).active
    assert select(g, ev(k.nullEvent)) == (False, w.addr, -1)
    assert idled == [d.text_h]
    g("HideDialogItem", w.addr, SECOND)
    assert select(g, key(TAB)) == (False, w.addr, -1)
    assert d.edit_field == FIRST - 1
    # With no edit field, keys and Tab do nothing.
    plain = new_dialog(g, *ITEMS[:3])
    assert Dialog(g.mem, plain.addr).edit_field == -1
    assert select(g, key(ord("x"))) == (False, plain.addr, -1)
    assert select(g, key(TAB)) == (False, plain.addr, -1)
    assert Dialog(g.mem, plain.addr).edit_field == -1


def black(w, v, h):
    """Whether the dialog's pixel at local (v, h) is black."""
    with w.backing.lock:
        return w.backing.image.pixel(h, v) & 0xFFFFFF == 0


def any_black(w, r):
    return any(black(w, v, h) for v in range(r[0], r[2]) for h in range(r[1], r[3]))


def post(g, *events):
    for e in events:
        g.process.events.post(e)


def modal(g, filter_proc=0):
    item_p = g.ptr(b"\0\0")
    g("ModalDialog", filter_proc, item_p)
    return g.mem.r16s(item_p)


def test_modal_dialog_with_a_filter_procedure(g):
    """A filter procedure sees each event first, and can change it: here
    it makes the key a null event, so it isn't typed, and ends the dialog
    with item 2 on the next null event."""
    w = new_dialog(g)
    seen = []

    def filter_proc(dialog, ev_p, item_p):
        what = g.mem.r16(ev_p)
        seen.append((dialog, what))
        if what == k.keyDown:
            g.mem.w16(ev_p, k.nullEvent)
        elif what == k.nullEvent and (w.addr, k.keyDown) in seen:
            g.mem.w16(item_p, CANCEL)
            return True
        return False
    proc = g.ptr(b"\x4e\x75")
    g.procs[proc] = filter_proc
    post(g, key(ord("a")))
    assert modal(g, proc) == CANCEL
    assert g.contents(item(g, w, FIRST).handle) == b"First"
    assert {d for d, _ in seen} == {w.addr}


def test_modal_dialog_without_one(g):
    """Return presses the default button, of the dialog in front or, with
    another window in front, the frontmost dialog.  A click outside the
    dialog beeps.  With no dialog at all the item is -1."""
    assert modal(g) == -1
    w = new_dialog(g)
    g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), True,
      k.documentProc, IN_FRONT, True, 0)
    assert g.tb.FrontWindow() is not w
    post(g, ev(k.mouseDown, where=Point(10, 400)), key(RETURN))
    assert modal(g) == OK
    assert g.beeps == [1]


def test_the_standard_filter(g):
    """Return and Enter press the default button and Escape and
    Command-period the cancel one, if there's one, it's a button that can
    be seen and it's active; anything else is left to DialogSelect."""
    w = new_dialog(g)
    plain = g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), False,
              k.documentProc, IN_FRONT, True, 0)

    def std(e, p=w.addr):
        item_p = g.ptr(b"\xff\xff")
        return bool(g("StdFilterProc", p, event_ptr(g, e), item_p)), g.mem.r16s(item_p)
    assert std(key(RETURN)) == (True, OK)
    assert std(ev(k.autoKey, ENTER)) == (True, OK)
    assert std(key(ESCAPE)) == (False, -1)              # no cancel item
    assert g("SetDialogCancelItem", w.addr, CANCEL) == 0
    assert std(key(ESCAPE)) == (True, CANCEL)
    assert std(key(ord("."), k.cmdKey)) == (True, CANCEL)
    assert std(key(ord("."))) == (False, -1)
    assert std(key(ord("a"))) == (False, -1)
    assert std(ev(k.mouseDown, where=at(w, 80, 250))) == (False, -1)
    assert std(key(RETURN), plain.addr) == (False, -1)
    g("HideDialogItem", w.addr, OK)
    assert std(key(RETURN)) == (False, -1)
    g("ShowControl", item(g, w, OK).handle)             # seen, but its item hidden
    assert std(key(RETURN)) == (False, -1)
    g("HideControl", item(g, w, OK).handle)
    g("ShowDialogItem", w.addr, OK)
    g("HideControl", item(g, w, OK).handle)             # unseen, but in its place
    assert std(key(RETURN)) == (False, -1)
    g("ShowControl", item(g, w, OK).handle)
    g("HiliteControl", item(g, w, OK).handle, 255)
    assert std(key(RETURN)) == (False, -1)
    g("HiliteControl", item(g, w, OK).handle, 0)
    assert std(key(RETURN)) == (True, OK)
    assert g("SetDialogDefaultItem", w.addr, TEXT) == 0     # not a button
    assert std(key(RETURN)) == (False, -1)
    g.machine.std_filter = 0x123450
    proc_p = g.ptr(bytes(4))
    assert g("GetStdFilterProc", proc_p) == 0 and g.mem.r32(proc_p) == 0x123450
    # The calls that set a dialog's items do nothing for another window.
    for name in ("SetDialogDefaultItem", "SetDialogCancelItem", "SetDialogTracksCursor"):
        assert g(name, plain.addr, 2) == 0
    assert g("SetDialogTracksCursor", w.addr, True) == 0 and w.dialog.tracks_cursor
    assert w.dialog.cancel_item == CANCEL and Dialog(g.mem, w.addr).a_def_item == TEXT


def test_getting_setting_and_finding_items(g):
    """An item's kind, handle and rectangle, got and set; the item a point
    is in, from 0; nothing for an item a dialog hasn't or another window."""
    w = new_dialog(g)
    plain = g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), False,
              k.documentProc, IN_FRONT, True, 0)
    type_p, h_p, rect_p = g.ptr(b"\xff\xff"), g.ptr(bytes(4)), g.rect_ptr((1, 2, 3, 4))

    def get(p, n):
        g("GetDialogItem", p, n, type_p, h_p, rect_p)
        return g.mem.r16s(type_p), g.mem.r32(h_p), g.rect(rect_p)
    assert get(w.addr, TEXT) == (statText, item(g, w, TEXT).handle, Rect(10, 10, 26, 200))
    assert get(w.addr, USER) == (userItem | itemDisable, 0, Rect(10, 210, 26, 290))
    for p, n in ((w.addr, 0), (w.addr, USER + 1), (plain.addr, 1)):
        assert get(p, n) == get(w.addr, USER)           # left as they were
    proc = g.ptr(b"\x4e\x75")
    g("SetDialogItem", w.addr, USER, 0x100 | userItem, proc, g.rect_ptr((30, 210, 50, 290)))
    assert get(w.addr, USER) == (userItem, proc, Rect(30, 210, 50, 290))
    for p, n in ((w.addr, 0), (w.addr, USER + 1), (plain.addr, 1)):
        g("SetDialogItem", p, n, statText, 0, g.rect_ptr((0, 0, 1, 1)))
    assert [i.rect for i in items(g.mem, Dialog(g.mem, w.addr).items)][-1] == \
        Rect(30, 210, 50, 290)
    assert g("FindDialogItem", w.addr, Point(15, 20)) == TEXT - 1
    assert g("FindDialogItem", w.addr, Point(95, 5)) == -1
    assert g("FindDialogItem", plain.addr, Point(15, 20)) == -1
    assert g("FindDialogItem", 0, Point(15, 20)) == -1


def test_hiding_and_showing_items(g):
    """A hidden item is moved far to the right, a control hidden too and
    another item's place erased; shown, it's put back.  Hiding a hidden
    item, or showing one that isn't, does nothing."""
    w = new_dialog(g)
    ok = item(g, w, OK).handle
    g("HideDialogItem", w.addr, OK)
    assert item(g, w, OK).hidden and not Control(g.mem, ok).vis
    assert item(g, w, OK).rect == Rect(70, 230 + HIDDEN, 90, 290 + HIDDEN)
    g("HideDialogItem", w.addr, OK)
    assert item(g, w, OK).rect == Rect(70, 230 + HIDDEN, 90, 290 + HIDDEN)
    g("ShowDialogItem", w.addr, OK)
    assert item(g, w, OK).rect == Rect(70, 230, 90, 290)
    assert Control(g.mem, ok).vis
    g("ShowDialogItem", w.addr, OK)
    assert item(g, w, OK).rect == Rect(70, 230, 90, 290)
    w.update_pending = False
    g("HideDialogItem", w.addr, TEXT)
    assert item(g, w, TEXT).hidden
    assert w.update.bbox == at_rect(w, Rect(6, 6, 30, 204))
    w.update_pending = False
    g("ShowDialogItem", w.addr, TEXT)
    assert not item(g, w, TEXT).hidden
    assert w.update.bbox == at_rect(w, Rect(6, 6, 30, 204))
    for name in ("HideDialogItem", "ShowDialogItem"):
        g(name, w.addr, 0)
        g(name, w.addr, USER + 1)
        g(name, 0, 1)


def at_rect(w, r):
    return r.offset(w.content.left, w.content.top)


def test_selecting_an_items_text(g):
    """Only edit text can be selected, which makes it the field edited."""
    w = new_dialog(g)
    d = Dialog(g.mem, w.addr)
    g("SelectDialogItemText", w.addr, SECOND, 1, 3)
    assert d.edit_field == SECOND - 1
    assert (record(g, w).sel_start, record(g, w).sel_end) == (1, 3)
    for p, n in ((w.addr, TEXT), (w.addr, 0), (0, SECOND)):
        g("SelectDialogItemText", p, n, 0, 0)
    assert d.edit_field == SECOND - 1
    assert (record(g, w).sel_start, record(g, w).sel_end) == (1, 3)


def test_an_items_text(g):
    """Setting an item's text sets its handle's, the TextEdit record's if
    it's the field being edited; a handle no dialog has is set all the
    same.  What's got is at most 255 characters."""
    w = new_dialog(g)
    redrawn = []
    g.tb.qd.fill = lambda port, path, pat, mode, fill=g.tb.qd.fill: (
        redrawn.append(port), fill(port, path, pat, mode))
    text, first = item(g, w, TEXT).handle, item(g, w, FIRST).handle
    g("SetDialogItemText", text, g.ptr(b"\x02Hi"))
    assert g.contents(text) == b"Hi" and redrawn
    g("SetDialogItemText", first, g.ptr(b"\x03New"))
    assert g.contents(first) == b"New" and record(g, w).h_text == first
    assert record(g, w).te_length == 3
    other = g.handle(b"old")
    redrawn.clear()
    g("SetDialogItemText", other, g.ptr(b"\x03abc"))
    assert g.contents(other) == b"abc" and not redrawn
    g("SetDialogItemText", 0, g.ptr(b"\x03abc"))
    str_p = g.ptr(bytes(256))
    g("GetDialogItemText", g.handle(b"x" * 300), str_p)
    assert g.mem.r_pstr(str_p) == b"x" * 255
    g("GetDialogItemText", 0, str_p)
    assert g.mem.r_pstr(str_p) == b""


def test_drawing_the_items(g, monkeypatch):
    """An icon item is the ICON its resource ID names, a picture item the
    PICT, each drawn in its rectangle, and edit text not being edited is
    drawn as text; an item of a kind the Dialog Manager doesn't know, or
    whose resource there isn't, has no handle and draws nothing."""
    rid = struct.pack(">h", 128)
    w = new_dialog(g, (iconItem, (10, 10, 42, 42), rid), (picItem, (10, 60, 30, 80), rid),
                   (iconItem, (50, 10, 82, 42), struct.pack(">h", 129)),
                   (1, (50, 60, 70, 80), b""),
                   (editText, (10, 100, 26, 200), b"One"),
                   (editText, (40, 100, 56, 200), b"Two"))
    rm = g.process.resources
    assert item(g, w, 1).handle == rm.GetResource("ICON", 128)
    assert item(g, w, 2).handle == rm.GetResource("PICT", 128)
    assert item(g, w, 3).handle == 0 and item(g, w, 4).handle == 0
    g("DrawDialog", w.addr)
    assert black(w, 20, 20) and black(w, 20, 70)
    assert not any_black(w, (50, 10, 82, 42)) and not any_black(w, (50, 60, 70, 80))
    assert any_black(w, (40, 100, 56, 200))             # "Two", inside its frame
    # UpdateDialog draws what's in the region, all of it with none.
    drawn = []
    monkeypatch.setattr(glue_dialogs, "draw_dialog", lambda t, d, area=None: drawn.append(
        (d, area)))
    rgn = new_rgn(g.heap, Rect(5, 6, 7, 8))
    g("UpdateDialog", w.addr, rgn)
    g("UpdateDialog", w.addr, 0)
    plain = g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), False,
              k.documentProc, IN_FRONT, True, 0)
    g("UpdateDialog", plain.addr, rgn)
    g("DrawDialog", plain.addr)
    assert drawn == [(w, Rect(5, 6, 7, 8)), (w, None)]


def test_a_picture_after_the_buttons_and_text_is_drawn_under_them(g):
    """DrawDialog draws the pictures, then the controls, then the text (IM:
    Macintosh Toolbox Essentials, Dialog Manager, DrawDialog).  G-Netix's
    dialogs end with a picture that fills them, with blank button faces
    where the buttons are and an empty field for the message."""
    w = new_dialog(g, (ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
                   (statText, (10, 10, 26, 200), b"Message"),
                   (picItem, (0, 0, 100, 300), struct.pack(">h", 129)))
    g("DrawDialog", w.addr)
    assert any_black(w, (10, 10, 26, 200)) and black(w, 80, 230)


def test_an_empty_static_text_item_draws_nothing(g):
    """Hermes's arrow pictures are under empty static text items, its
    buttons' click targets: with nothing to draw, they don't erase."""
    w = new_dialog(g, (statText, (10, 10, 26, 60), b""),
                   (picItem, (10, 10, 30, 60), struct.pack(">h", 128)))
    g("DrawDialog", w.addr)
    assert black(w, 18, 30)


def test_an_icon_item_with_a_colour_icon(qapp):
    """On a colour machine an icon item whose ICON has a cicn of the same
    ID is that colour icon: its handle the CIcon record GetCIcon makes,
    which the program can dispose of (Odell Down Under does, then gives
    the item one of its own); on a Plus it's the ICON."""
    from test_icons import red_cicn
    resources = [Resource(b"ICON", 128, BLACK_ICON), Resource(b"cicn", 128, red_cicn())]
    rid = struct.pack(">h", 128)
    g = Guest(resources, model="ii", depth=8)
    g.tb._d = Host()
    w = new_dialog(g, (iconItem, (10, 10, 14, 14), rid), (iconItem, (20, 10, 24, 14), rid))
    icon = g.process.resources.GetResource("ICON", 128)
    h = item(g, w, 1).handle
    assert h not in (0, icon, item(g, w, 2).handle)
    p = g.heap.deref(h)
    ctab, pixels = g.mem.r32(p + 42), g.mem.r32(p + 78)
    assert g.heap.is_handle(ctab) and g.heap.is_handle(pixels)
    g("DrawDialog", w.addr)
    with w.backing.lock:
        img = w.backing.image
        assert img.pixel(11, 11) & 0xFFFFFF == 0xFF0000 and black(w, 11, 10)
        assert img.pixel(13, 11) == img.pixel(13, 30)       # the mask's right column
    g("DisposeCIcon", h)
    assert not any(map(g.heap.is_handle, (h, ctab, pixels)))
    g("SetDialogItem", w.addr, 1, iconItem, icon, g.rect_ptr((10, 10, 14, 14)))
    # A record the program makes itself is drawn as a colour icon too.
    own = g("GetCIcon", 128)
    g("SetDialogItem", w.addr, 2, iconItem + itemDisable, own, g.rect_ptr((20, 30, 24, 34)))
    g("DrawDialog", w.addr)
    assert black(w, 11, 11)                             # the ICON, drawn as one
    with w.backing.lock:
        assert w.backing.image.pixel(31, 21) & 0xFFFFFF == 0xFF0000
    # A record that isn't one GetCIcon made is disposed of, its handles
    # left be where they're in no heap.
    other = g.handle(BLACK_ICON)
    g("DisposeCIcon", other)
    assert not g.heap.is_handle(other)

    plus = Guest(resources)
    plus.tb._d = Host()
    w = new_dialog(plus, (iconItem, (10, 10, 42, 42), rid))
    assert item(plus, w, 1).handle == plus.process.resources.GetResource("ICON", 128)


def test_the_default_buttons_outline(g):
    """SetDialogDefaultItem draws a bold outline round the button, grey
    if the button's dimmed; none for an item that isn't a button that can
    be seen."""
    w = new_dialog(g)
    edge = [(v, 146) for v in range(74, 86)]            # its left side, 4 out from Cancel's

    def outlined():
        return [black(w, v, h) for v, h in edge]
    assert not any(outlined())
    cancel = item(g, w, CANCEL).handle
    g("HideDialogItem", w.addr, CANCEL)
    g("ShowControl", cancel)                            # the item's still hidden
    g("SetDialogDefaultItem", w.addr, CANCEL)
    assert not any(outlined())
    g("HideControl", cancel)
    g("ShowDialogItem", w.addr, CANCEL)
    g("HideControl", cancel)
    g("SetDialogDefaultItem", w.addr, CANCEL)
    assert not any(outlined())
    g("ShowControl", cancel)
    g("HiliteControl", cancel, 255)
    g("SetDialogDefaultItem", w.addr, CANCEL)
    assert any(outlined()) and not all(outlined())      # grey
    g("HiliteControl", cancel, 0)
    g("SetDialogDefaultItem", w.addr, CANCEL)
    assert all(outlined()) and w.dialog.outline
    assert all(black(w, 80, h) for h in (146, 147, 148))    # three pixels wide


def test_dialogs_that_cant_be_made(g, monkeypatch):
    """No DLOG, no DITL, no room for the record or the window: no dialog.
    The record NewDialog made for it is let go of, not the caller's."""
    assert g("GetNewDialog", 199, 0, IN_FRONT) == 0
    assert g("GetNewDialog", 200, 0, IN_FRONT) == 0
    args = (g.rect_ptr(BOUNDS), g.ptr(b"\0"), True, k.dBoxProc, IN_FRONT, False, 0,
            g.handle(ditl(*ITEMS)))
    storage = g.ptr(bytes(glue_dialogs.SIZE))
    disposed = []
    monkeypatch.setattr(g.heap, "dispose_ptr", disposed.append)
    monkeypatch.setattr(g.tb, "NewWindow", lambda *args, **kw: None)
    assert g("NewDialog", storage, *args) == 0 and disposed == []
    assert g("NewDialog", 0, *args) == 0 and len(disposed) == 1
    monkeypatch.setattr(g.heap, "new_ptr", lambda *args, **kw: 0)
    assert g("NewDialog", 0, *args) == 0 and len(disposed) == 1


def test_a_dialogs_font(g):
    """set_font gives the dialog's text, and its TextEdit record, which is
    made again, the font: the field being edited stays so, with its text."""
    w = new_dialog(g)
    d = Dialog(g.mem, w.addr)
    glue_dialogs.set_font(g, w, 3, 2, 9)
    assert (w.tx_font, w.tx_face, w.tx_size) == (3, 2, 9)
    assert (record(g, w).tx_font, record(g, w).tx_size) == (3, 9)
    assert d.edit_field == FIRST - 1 and record(g, w).h_text == item(g, w, FIRST).handle
    assert (record(g, w).sel_start, record(g, w).sel_end) == (0, len(b"First"))
    assert g.contents(item(g, w, FIRST).handle) == b"First"
    plain = new_dialog(g, *ITEMS[:3])
    glue_dialogs.set_font(g, plain, 4, 1, 12)
    assert Dialog(g.mem, plain.addr).text_h and Dialog(g.mem, plain.addr).edit_field == -1
    assert TERec(g.mem, g.mem.r32(Dialog(g.mem, plain.addr).text_h) & 0xFFFFFF).tx_size == 12


def test_the_edit_fields_textedit_record(g, monkeypatch):
    """A dialog whose TextEdit record has gone is given another when a
    field is edited, if there's room for one.  The field's activated only
    if the dialog's the active window."""
    w = new_dialog(g)
    d = Dialog(g.mem, w.addr)
    d.text_h = 0
    g("SelectDialogItemText", w.addr, SECOND, 0, 2)
    assert d.text_h and d.edit_field == SECOND - 1 and record(g, w).sel_end == 2
    g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), True,
      k.documentProc, IN_FRONT, True, 0)
    g("SelectDialogItemText", w.addr, FIRST, 0, 2)
    assert d.edit_field == FIRST - 1 and not record(g, w).active      # not in front
    d.text_h = 0
    monkeypatch.setattr(glue_dialogs.te, "new", lambda *args: 0)
    g("SelectDialogItemText", w.addr, FIRST, 0, 2)
    assert d.text_h == 0 and d.edit_field == -1


def test_closing_a_dialog(g):
    """CloseDialog lets go of the items' text and controls but keeps the
    item list, which is the program's; and does nothing to another
    window.  CouldDialog and the like do nothing."""
    w = new_dialog(g)
    d = Dialog(g.mem, w.addr)
    list_h = d.items
    plain = g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), True,
              k.documentProc, IN_FRONT, True, 0)
    g("CloseDialog", plain.addr)
    assert plain in g.process.windows
    g("CloseDialog", w.addr)
    assert w not in g.process.windows and w.dialog is None
    assert d.text_h == 0 and g.heap.handle_size(list_h) == len(ditl(*ITEMS))
    assert [i.handle for i in items(g.mem, list_h) if i.is_text] == [0, 0, 0]
    for name in ("CouldDialog", "FreeDialog", "CouldAlert", "FreeAlert"):
        assert g(name, 128) is None


def test_param_text_and_error_sounds(g):
    """ParamText makes the strings' handles if InitDialogs hasn't.  An
    alert's sound is the standard one's beeps, or the procedure
    ErrorSound gives called with how many, or with NIL none."""
    g("ParamText", g.ptr(b"\x03Ann"), 0, 0, g.ptr(b"\x01!"))
    assert glue_dialogs._params(g) == [b"Ann", b"", b"", b"!"]
    g("ParamText", g.ptr(b"\x02Bo"), 0, 0, 0)
    assert glue_dialogs._params(g) == [b"Bo", b"", b"", b"!"]
    g("InitDialogs", 0)
    assert g("Alert", 300, 0) == -1 and g.beeps == [1]          # stage 1: not drawn
    proc = g.ptr(b"\x4e\x75")
    g("ErrorSound", proc)
    assert g("Alert", 300, 0) == -1 and g.beeps == [1] and g.calls == [(proc, (("h", 2),))]
    g("ErrorSound", 0)
    assert g("Alert", 300, 0) == -1 and g.beeps == [1] and len(g.calls) == 1


def test_alert_stages(g):
    """Each alert of the same ID in a row goes on to the next stage, up to
    the fourth, which it stays at; another alert starts at the first.  No
    ALRT, or no DITL for one to be shown: -1, after the stage's sound."""
    g("InitDialogs", 0)
    assert g("Alert", 299, 0) == -1 and g.beeps == []
    counts = []
    for _ in range(6):
        before = len(g.beeps)
        assert g("Alert", 300, 0) == -1
        counts.append(len(g.beeps) - before)
    assert counts == [1, 2, 3, 0, 0, 0]
    assert g("Alert", 301, 0) == -1 and len(g.beeps) == 7      # its DITL isn't there
    assert g("Alert", 300, 0) == -1 and len(g.beeps) == 8      # from stage 1 again


def test_an_alert_with_no_alrt_is_noted_once(g, caplog):
    """An alert whose ALRT isn't there shows nothing; the log says so,
    once for each ID, so a program that only shows one isn't a mystery."""
    g("InitDialogs", 0)
    assert g("StopAlert", 299, 0) == -1
    assert g("Alert", 299, 0) == -1
    assert g("Alert", 300, 0) == -1                             # there, not drawn
    assert caplog.text.count("ALRT 299, which isn't in any open resource file") == 1
    assert "ALRT 300" not in caplog.text


def test_alerts_icons_and_default_button(g):
    """An alert's default button is its stage's, which Return presses; the
    icon StopAlert, NoteAlert and CautionAlert give it is drawn with it.
    It's gone when the user has picked an item."""
    g("InitDialogs", 0)
    post(g, key(RETURN))
    assert g("StopAlert", 302, 0) == OK                         # stage 1
    seen = []

    def filter_proc(p, ev_p, item_p):
        w = g.window(p)
        g("DrawDialog", p)
        seen.append((w.dialog.icon, Dialog(g.mem, p).a_def_item, w.dialog.outline,
                     any_black(w, (10, 20, 42, 52))))
        g.mem.w16(item_p, Dialog(g.mem, p).a_def_item)
        return True
    proc = g.ptr(b"\x4e\x75")
    g.procs[proc] = filter_proc
    assert g("NoteAlert", 302, proc) == CANCEL                  # stage 2
    assert g("CautionAlert", 302, proc) == CANCEL
    assert g("Alert", 302, proc) == CANCEL
    assert seen == [(noteIcon, CANCEL, True, True), (cautionIcon, CANCEL, True, True),
                    (-1, CANCEL, True, False)]
    assert not any(w.dialog is not None for w in g.process.windows)


def test_appending_items_that_dont_fit(g, monkeypatch):
    """AppendDITL takes the whole items of a list cut short, and nothing
    from an empty one, a NIL handle or when the dialog's list can't grow;
    items appended hidden leave the dialog as it was.  ShortenDITL by
    nothing, or less, takes nothing away."""
    w = new_dialog(g)

    def count(p=w.addr):
        return glue_dialogs.CountDITL(g, p)

    def append(data, method=0):
        glue_dialogs.AppendDITL(g, w.addr, g.handle(data), method)
    two = ditl((statText, (100, 10, 116, 100), b"A"), (statText, (120, 10, 136, 100), b"B"))
    append(two[:-4])                                    # B cut short
    assert count() == 7 and g.contents(item(g, w, 7).handle) == b"A"
    append(b"")
    append(struct.pack(">h", -1))
    glue_dialogs.AppendDITL(g, w.addr, 0, 0)
    assert count() == 7
    w.update_pending = False
    append(ditl((statText, (100, HIDDEN + 10, 116, HIDDEN + 100), b"C")))
    assert count() == 8 and not w.update_pending
    for n in (0, -2):
        glue_dialogs.ShortenDITL(g, w.addr, n)
    assert count() == 8
    plain = g("NewWindow", 0, g.rect_ptr((200, 40, 300, 340)), g.ptr(b"\x05Plain"), False,
              k.documentProc, IN_FRONT, True, 0)
    assert count(plain.addr) == 0
    glue_dialogs.AppendDITL(g, plain.addr, g.handle(two), 0)
    glue_dialogs.ShortenDITL(g, plain.addr, 1)
    monkeypatch.setattr(g.heap, "set_handle_size", lambda h, size: False)
    append(two)
    assert count() == 8
