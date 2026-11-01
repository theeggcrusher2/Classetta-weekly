"""The Dialog Manager, driven by Dialogs (tests/apps/dialogs.s): a 68k
application with a modal dialog and a stop alert, answered with synthetic
clicks and keys."""

import contextlib
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import Recorder
from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest

from macrt.m68k import glue_dialogs
from macrt.m68k.glue_cport import STANDARD_WCTB
from macrt.macfile import open_application
from macrt.resfork import Resource
from macrt.toolbox import constants as k
from macrt.toolbox import textedit as te
from macrt.toolbox.color import color_table
from macrt.toolbox.controls import Control
from macrt.toolbox.dialogs import HIDDEN, Dialog, items, substitute
from macrt.toolbox.qdraw import port_rgb, rgb_argb
from macrt.toolbox.templates import (
    btnCtrl, ctrlItem, editText, itemDisable, picItem, resCtrl, statText, userItem)
from macrt.toolbox.types import Rect

from builders import alrt, ditl, dlog
from guest import Guest, Processor
APPS = Path(__file__).parent / "apps"
DIALOG, ITEM, COUNT, TEXT, ALERTED, STAGE, USERCALLS = -230, -232, -234, -504, -506, -508, -510


class App:
    def __init__(self, h):
        f = open_application(APPS / "Dialogs.bin")
        h.launch(Recorder("Other"))
        self.h = h
        self.proc = h.d.launch(f.name, None, app=f)
        self.mem = self.proc.system.mem
        self.a5 = self.proc.partition.a5
        h.wait_until(lambda: self.word(STAGE) == 1 and self.word(USERCALLS) > 0, timeout=10,
                     what="the dialog")
        self.window = self.proc.windows[0]
        h.wait_until(lambda: self.black(210, 150), what="the OK button drawn")

    def word(self, offset):
        return self.mem.r16s(self.a5 + offset)

    def items(self, w=None):
        return items(self.mem, Dialog(self.mem, (w or self.window).addr).items)

    def black(self, h, v, w=None):
        w = w or self.window
        with w.backing.lock:
            return w.backing.image.pixel(h, v) & 0xFFFFFF == 0

    def any_black(self, r, w=None):
        return any(self.black(h, v, w) for v in range(r.top, r.bottom)
                   for h in range(r.left, r.right))

    def click(self, h, v, w=None):
        c = (w or self.window).content
        self.h.click(c.left + h, c.top + v)

    def finish(self, key=Qt.Key.Key_Return):
        """Dismiss the dialog with a key; the alert comes up."""
        self.h.key(key)
        self.h.wait_until(lambda: self.word(STAGE) == 2, what="the dialog dismissed")
        self.h.wait_until(lambda: self.proc.windows and self.proc.windows[0] is not self.window
                          and self.proc.windows[0].visible, what="the alert")
        return self.proc.windows[0]


@pytest.fixture
def app(harness):
    return App(harness())


def test_the_dialog_is_made_and_drawn(app):
    w = app.window
    assert w.window_kind == k.dialogKind
    assert app.mem.r32(app.a5 + DIALOG) == w.addr
    found = app.items()
    assert len(found) == 7
    ok, cancel, hello, edit, check, user, hidden = found
    assert Control(app.mem, ok.handle).title == "OK"
    assert Control(app.mem, cancel.handle).title == "Cancel"
    assert w.control_list in (ok.handle, cancel.handle, check.handle)
    assert hello.type == statText and app.proc.runner.tb.contents(hello.handle) == b"Hello, ^0!"
    assert edit.type == editText and app.proc.runner.tb.contents(edit.handle) == b"Edit me"
    assert not user.enabled and user.handle != 0           # SetDialogItem put the procedure there
    assert hidden.hidden and hidden.rect.left == 10 + HIDDEN
    # The fixture saw the OK button drawn; the items after it may still be drawing.
    app.h.wait_until(lambda: app.any_black(hello.rect)      # its text, with ParamText's
                     and app.black(220, 95)                 # the user item painted itself
                     and app.black(10, 50) and app.black(202, 50),  # the edit field's frame
                     what="the items drawn")
    assert not app.any_black(hidden.rect.offset(-HIDDEN, 0))
    assert app.proc.runner.unimplemented_calls == {}


def test_the_items_are_drawn_in_drawdialogs_order(monkeypatch):
    """DrawDialog "draws all dialog items, calls the Control Manager
    procedure DrawControls to draw all controls, and calls the TextEdit
    procedure TEUpdate to update all static and editable text items" (IM:
    Macintosh Toolbox Essentials, Dialog Manager): the pictures and user
    items, then the controls, then the text, which doesn't erase its rect:
    Font Mover's help has the last of it run into the OK button.  The
    controls drawn are the window's, as DrawControls draws them, so one
    the program added with NewControl is too (Widget Workshop's puzzle
    list's scroll bar)."""
    ok = SimpleNamespace(number=1, is_control=True, rect=Rect(239, 171, 255, 261))
    hints = [SimpleNamespace(number=n, is_control=False, type=statText,
                             rect=Rect(top, 5, bottom, 425))
             for n, top, bottom in ((2, 13, 29), (3, 42, 95), (4, 91, 170), (5, 158, 250))]
    art = SimpleNamespace(number=6, is_control=False, type=picItem, rect=Rect(0, 0, 270, 440))
    ring = SimpleNamespace(number=7, is_control=False, type=userItem, rect=Rect(235, 167, 259, 265))
    bar = SimpleNamespace(handle=0x2000, rect=Rect(10, 430, 200, 446))
    button = SimpleNamespace(handle=0x1000, rect=ok.rect)
    drawn = []
    monkeypatch.setattr(glue_dialogs, "_items", lambda t, w: [ok, *hints, art, ring])
    monkeypatch.setattr(glue_dialogs, "_draw_item", lambda t, w, item: drawn.append(item.number))
    monkeypatch.setattr(glue_dialogs, "controls_of", lambda t, w: [bar, button])
    monkeypatch.setattr(glue_dialogs, "Draw1Control", lambda t, h: drawn.append(h))
    w = SimpleNamespace(dialog=SimpleNamespace(icon=-1, outline=False))
    glue_dialogs.draw_dialog(None, w)
    assert drawn == [6, 7, 0x2000, 0x1000, 2, 3, 4, 5]
    drawn.clear()
    glue_dialogs.draw_dialog(None, w, Rect(240, 0, 260, 200))
    assert drawn == [6, 7, 0x1000, 5]


def test_a_resource_control_is_placed_at_its_item(qapp, monkeypatch):
    """Its template's rectangle gives its size, the item its place
    (Spaceward Ho!'s templates all sit at 0,0)."""
    g = Guest()
    made = []

    def get_new_control(t, rid, owner):     # a scroll bar from template rid
        h = g.handle(bytes(40))
        Control(g.mem, h).rect = Rect(0, 0, 16, 120)
        made.append(rid)
        return h
    item = SimpleNamespace(type=ctrlItem + resCtrl, data=struct.pack(">h", 128),
                           rect=Rect(30, 36, 46, 166), is_control=True, handle=0)
    monkeypatch.setattr(glue_dialogs, "_items", lambda t, w: [item])
    monkeypatch.setattr(glue_dialogs, "GetNewControl", get_new_control)
    glue_dialogs._make_items(g, SimpleNamespace(addr=0))
    assert made == [128]
    assert Control(g.mem, item.handle).rect == Rect(30, 36, 46, 156)


def test_param_text_substitution():
    assert substitute(b"Hello, ^0! ^1^3^9", [b"World", b"a", b"", b"c"]) == b"Hello, World! ac^9"
    assert substitute(b"^0", [b"This version of ^2 needs ^1", b"^0", b"MONOPOLY", b""]) == (
        b"This version of MONOPOLY needs ^0")


def test_the_check_box_returns_its_item(app):
    check = Control(app.mem, app.items()[4].handle)
    app.click(30, 84)
    app.h.wait_until(lambda: app.word(COUNT) == 1, what="ModalDialog to return")
    assert app.word(ITEM) == 5
    app.h.wait_until(lambda: check.value == 1, what="the application to check it")
    app.h.wait_until(lambda: app.black(15, 81), what="the X")
    app.click(220, 95)                                      # the disabled user item
    app.click(5, 170)                                       # no item
    app.click(30, 84)
    app.h.wait_until(lambda: app.word(COUNT) == 2, what="it again")
    app.h.wait_until(lambda: check.value == 0, what="the application to uncheck it")
    assert app.word(STAGE) == 1


def test_clicking_ok(app):
    app.click(245, 150)
    app.h.wait_until(lambda: app.word(STAGE) == 2, what="the dialog dismissed")
    assert app.word(ITEM) == 1
    assert app.mem.r_pstr(app.a5 + TEXT) == b"Edit me"


def test_typing_into_the_edit_field(app):
    """TextEdit edits the field; ModalDialog returns its item for the click
    and each key, and the application reads the text back on OK."""
    app.click(195, 53)                                      # past the end of "Edit me"
    QTest.keyClicks(app.h.d.screen_widget, " too")
    app.h.wait_until(lambda: app.word(COUNT) == 5, what="the click, then a key at a time")
    assert app.proc.runner.tb.contents(app.items()[3].handle) == b"Edit me too"
    assert app.word(ITEM) == 4 and app.word(STAGE) == 1
    app.h.key(Qt.Key.Key_Backspace)
    app.finish(Qt.Key.Key_Return)
    assert app.word(ITEM) == 1
    assert app.mem.r_pstr(app.a5 + TEXT) == b"Edit me to"
    assert app.proc.runner.unimplemented_calls == {}


def test_return_presses_the_default_item(app):
    app.finish(Qt.Key.Key_Return)
    assert app.word(ITEM) == 1 and app.word(COUNT) == 1
    assert app.window not in list(app.proc.windows)


def test_escape_presses_the_cancel_item(app):
    app.finish(Qt.Key.Key_Escape)
    assert app.word(ITEM) == 2


def test_the_stop_alert(app):
    alert = app.finish()
    assert alert.dialog is not None and alert.dialog.icon == 0
    assert alert.window_kind == k.dialogKind
    app.h.wait_until(lambda: app.black(25, 26, alert), what="the stop icon")
    assert not app.black(36, 20, alert)                     # the white bar in it
    assert app.black(217, 80, alert)                        # the bold outline around OK
    assert app.any_black(app.items(alert)[1].rect, alert)  # "All done."
    app.click(255, 80, alert)
    app.h.wait_until(lambda: app.word(STAGE) == 3, what="the alert dismissed")
    assert app.word(ALERTED) == 1
    assert list(app.proc.windows) == []
    assert app.proc.runner.unimplemented_calls == {}


def test_return_answers_the_alert(app):
    app.finish()
    app.h.key(Qt.Key.Key_Return)
    app.h.wait_until(lambda: app.word(STAGE) == 3, what="the alert dismissed")
    assert app.word(ALERTED) == 1


def test_modal_dialog_leaves_high_level_events_to_the_application(qapp, monkeypatch):
    """Without a filter procedure, which could handle them, ModalDialog
    leaves high-level events queued (System 7.5's Find File's alert comes
    up before it has taken its Open Application event)."""
    g = Guest()
    g.yielding = contextlib.nullcontext
    masks = []

    class Stop(Exception):
        pass

    def get(mask, *rest):
        masks.append(mask)
        raise Stop
    monkeypatch.setattr(g.process.events, "get", get)
    for proc in (0, 0x1234):
        with pytest.raises(Stop):
            glue_dialogs._modal(g, SimpleNamespace(addr=0), proc, g.ptr(bytes(2)))
    assert masks == [k.everyEvent & ~k.highLevelEventMask, k.everyEvent]


class NoDisplay:
    """A display that shows nothing: windows are placed where they say."""
    bridge = SimpleNamespace(call=lambda fn, *args, serve=None, killed=None: fn(*args),
                             post=lambda fn, *args: None)
    views: dict = {}

    @staticmethod
    def place_window(bounds, position, proc_id):
        return bounds

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def test_a_dialog_or_alert_with_no_room_leaves_no_item_list(qapp, monkeypatch):
    items = ditl((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"))
    g = Guest([Resource(b"DITL", 300, items),
               Resource(b"DLOG", 300, dlog((40, 40, 140, 340), "", 300)),
               Resource(b"ALRT", 300, alrt((40, 40, 140, 340), 300))])
    g.tb._d = NoDisplay()
    lists = []

    def no_room(t, *args, **kwargs):
        lists.append(args[8])                   # item_list
        return None
    monkeypatch.setattr(glue_dialogs, "new_dialog", no_room)
    assert g("GetNewDialog", 300, 0, -1) == 0
    assert len(lists) == 1 and not g.heap.is_handle(lists[0])
    assert g("Alert", 300, 0) == -1
    assert len(lists) == 2 and not g.heap.is_handle(lists[1])


def test_dialogs_with_colour_tables_are_colour_ones(qapp, monkeypatch):
    """With Color QuickDraw GetNewDialog's and alerts' ports are cGrafPorts
    if there's a 'dctb' (or 'actb') of their ID, in its colours if it has
    any, and old grafPorts if there isn't (Inside Macintosh V, the Dialog
    Manager): Pinball Arcade draws straight into its palette's portBits.
    NewColorDialog's is one too and NewDialog's isn't.  Without Color
    QuickDraw none is."""
    lilac = (0x8000, 0x8000, 0xFFFF)
    items = ditl((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"))
    resources = [Resource(b"DITL", 300, items),
                 Resource(b"ALRT", 300, alrt((40, 40, 140, 340), 300)),
                 Resource(b"actb", 300, color_table([lilac] + STANDARD_WCTB[1:])),
                 Resource(b"ALRT", 301, alrt((40, 40, 140, 340), 300))]
    for rid, ct in ((300, color_table([lilac] + STANDARD_WCTB[1:])), (301, color_table([])),
                    (302, None)):
        resources.append(Resource(b"DLOG", rid, dlog((40, 40, 140, 340), "", 300)))
        if ct is not None:
            resources.append(Resource(b"dctb", rid, ct))
    alerts = []

    def modal(t, w, proc, item_p):
        alerts.append((w.is_color, w.rgb_bk_color if w.is_color else None))
        t.mem.w16(item_p, 1)
    monkeypatch.setattr(glue_dialogs, "_modal", modal)
    for model in ("ii", "plus"):
        g = Guest(resources, model=model)
        g.tb._d = NoDisplay()
        pp = g.ptr(bytes(4))
        for rid in (300, 301, 302):
            w = g("GetNewDialog", rid, 0, -1)
            color = model == "ii" and rid != 302
            assert w.is_color == color and w.visible
            if color:
                pixmap = g.mem.r32(g.mem.r32(w.addr + 2))
                assert g.mem.r16(pixmap + 4) & 0x8000
                assert g("GetAuxWin", w.addr, pp) == (rid == 300)
                assert w.rgb_bk_color == (lilac if rid == 300 else STANDARD_WCTB[0])
                # Shown with its content in its content colour, not white.
                paper = g.system.devices.nearest(rgb_argb(w.rgb_bk_color))
                assert w.backing.image.pixel(10, 10) | 0xFF000000 == paper
            else:
                assert not g.mem.r16(w.addr + 6) & 0xC000     # portBits.rowBytes
            g("DisposeDialog", w.addr)
        bounds, title = g.rect_ptr(Rect(40, 40, 140, 340)), g.ptr(b"\0")
        item_list = g.handle(items)
        w = g("NewDialog", 0, bounds, title, 1, k.dBoxProc, -1, 0, 0, item_list)
        assert not w.is_color
        w = g("NewColorDialog", 0, bounds, title, 1, k.dBoxProc, -1, 0, 0, g.handle(items))
        assert w.is_color == (model == "ii")
        assert g("Alert", 300, 0) == 1
        assert g("Alert", 301, 0) == 1
    assert alerts == [(True, lilac), (False, None), (False, None), (False, None)]


def test_item_colour_tables_style_text_and_colour_controls(qapp, monkeypatch):
    """A colour dialog's 'ictb' of its item list's ID (Inside Macintosh V,
    the Dialog Manager): its statText and editText items are drawn with the
    style fields their itemCData says, a font by name too, and its
    controls get their colour tables.  A dialog without Color QuickDraw
    has none."""
    red, blue = (0xFFFF, 0, 0), (0, 0, 0xFFFF)
    item_list = ditl((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
                     (statText, (10, 10, 30, 200), b"Styled"),
                     (editText, (40, 10, 56, 200), b"abc"),
                     (statText, (10, 210, 30, 300), b"Plain"))
    ictb = struct.pack(">8H", 0x10, 0x10, 0x000F, 0x20, 0x8004, 0x34, 0, 0)
    ictb += color_table([blue])                                     # $10
    ictb += struct.pack(">hBxh3H3Hh", 3, 1, 9, *red, 0, 0, 0, 0)   # $20: Geneva, bold, 9
    ictb += struct.pack(">hBxh3H3Hh", 0x48, 0, 10, *red, 0, 0, 0, 0)    # $34: the name's, 10
    ictb += b"\x06Monaco"                                           # $48
    resources = [Resource(b"DITL", 310, item_list), Resource(b"ictb", 310, ictb),
                 Resource(b"DLOG", 310, dlog((40, 40, 140, 340), "", 310)),
                 Resource(b"dctb", 310, color_table([]))]
    drawn = []

    def text_box(t, w, data, r):
        drawn.append((data, w.tx_font, w.tx_face, w.tx_size, port_rgb(w, True)))
    monkeypatch.setattr(glue_dialogs, "_text_box", text_box)
    for model in ("ii", "plus"):
        g = Guest(resources, model=model)
        g.tb._d = NoDisplay()
        drawn.clear()
        w = g("GetNewDialog", 310, 0, -1)
        assert bool(w.dialog.item_colors) == (model == "ii")
        g("DrawDialog", w.addr)
        styled = dict((d[0], d[1:]) for d in drawn)
        if model == "plus":
            assert styled[b"Styled"] == styled[b"Plain"]
            continue
        assert styled[b"Styled"] == (3, 1, 9, red)
        assert styled[b"Plain"] == (w.tx_font, 0, 0, (0, 0, 0))
        assert (w.tx_face, w.tx_size, port_rgb(w, True)) == (0, 0, (0, 0, 0))   # as it was
        rec = te.TERec(g.mem, g.mem.r32(Dialog(g.mem, w.addr).text_h))
        assert (rec.tx_font, rec.tx_size) == (4, 10)        # Monaco, by name
        assert (rec.sel_start, rec.sel_end) == (0, 3)
        pp = g.ptr(bytes(4))
        button = items(g.mem, Dialog(g.mem, w.addr).items)[0].handle
        assert g("GetAuxiliaryControlRecord", button, pp)
        ct = g.mem.r32(g.mem.r32(g.mem.r32(pp)) + 8)
        assert g.contents(ct) == color_table([blue])


def ditl_dialog(model="plus"):
    """A Processor's dialog, 100 by 300, with an OK button and some text."""
    g = Processor(model=model)
    g.tb._d = NoDisplay()
    bounds, title = g.rect_ptr(Rect(40, 40, 140, 340)), g.ptr(b"\0")
    item_list = g.handle(ditl((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
                              (statText, (10, 10, 26, 200), b"Some text")))
    return g, g("NewDialog", 0, bounds, title, 0, k.dBoxProc, -1, 0, 0, item_list)


def test_a_user_items_procedure_put_in_the_list_is_kept(qapp):
    """The placeholder is "for the item's handle or, for item type
    userItem, its procedure pointer" (Inside Macintosh I, the Dialog
    Manager): a program that makes its own item list can put the
    procedure there before NewDialog, as LaserKey's installer does."""
    g = Guest()
    g.tb._d = NoDisplay()
    data = bytearray(ditl((ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
                          (userItem | itemDisable, (10, 10, 26, 200), b""),
                          (userItem, (30, 10, 46, 200), b"")))
    proc = g.ptr(b"\x4e\x75")
    struct.pack_into(">I", data, 18, proc)                         # item 2's placeholder
    drawn = []
    g.procs[proc] = lambda w, n: drawn.append(n)
    bounds, title = g.rect_ptr(Rect(40, 40, 140, 340)), g.ptr(b"\0")
    w = g("NewDialog", 0, bounds, title, 1, k.dBoxProc, -1, 0, 0, g.handle(bytes(data)))
    found = items(g.mem, Dialog(g.mem, w.addr).items)
    assert [item.handle for item in found[1:]] == [proc, 0]
    drawn.clear()
    g("DrawDialog", w.addr)
    assert drawn == [2]


def test_items_are_appended(qapp):
    """Over the dialog's, at its right or bottom, which grows to hold them,
    or from another item (Inside Macintosh: Macintosh Toolbox Essentials,
    the Dialog Manager)."""
    added = ditl((statText, (5, 10, 21, 60), b"More"),
                 (ctrlItem + btnCtrl, (30, 10, 50, 70), b"Again"))
    g, w = ditl_dialog()

    def rects():
        return [item.rect for item in items(g.mem, Dialog(g.mem, w.addr).items)][2:]
    glue_dialogs.AppendDITL(g, w.addr, g.handle(added), glue_dialogs.overlayDITL)
    assert glue_dialogs.CountDITL(g, w.addr) == 4
    assert rects() == [Rect(5, 10, 21, 60), Rect(30, 10, 50, 70)]
    found = items(g.mem, Dialog(g.mem, w.addr).items)
    assert g.tb.contents(found[2].handle) == b"More"
    assert Control(g.mem, found[3].handle).title == "Again"
    assert Control(g.mem, found[3].handle).rect == Rect(30, 10, 50, 70)
    glue_dialogs.AppendDITL(g, w.addr, g.handle(added), -2)        # from item 2's top left
    assert rects()[2:] == [Rect(15, 20, 31, 70), Rect(40, 20, 60, 80)]
    glue_dialogs.AppendDITL(g, w.addr, g.handle(added), glue_dialogs.appendDITLRight)
    assert rects()[4:] == [Rect(5, 310, 21, 360), Rect(30, 310, 50, 370)]
    assert w.port_rect == Rect(0, 0, 100, 380)                     # 10 past them, as before
    glue_dialogs.AppendDITL(g, w.addr, g.handle(added), glue_dialogs.appendDITLBottom)
    assert rects()[6:] == [Rect(105, 10, 121, 60), Rect(130, 10, 150, 70)]
    assert w.port_rect == Rect(0, 0, 155, 380)
    assert glue_dialogs.CountDITL(g, w.addr) == 10
    g("SetPort", w.addr)
    g("SetOrigin", 88, 0)                                          # as a control panel's is
    glue_dialogs.AppendDITL(g, w.addr, g.handle(added), glue_dialogs.overlayDITL)
    assert rects()[8:] == [Rect(5, 98, 21, 148), Rect(30, 98, 50, 158)]


def test_items_are_taken_away(qapp):
    g, w = ditl_dialog()
    glue_dialogs.AppendDITL(g, w.addr, g.handle(ditl(
        (editText, (40, 10, 56, 100), b"Typed"),
        (ctrlItem + btnCtrl, (70, 10, 90, 70), b"Gone"))), glue_dialogs.overlayDITL)
    d = Dialog(g.mem, w.addr)
    assert d.edit_field == 2 and d.text_h
    button = items(g.mem, d.items)[3].handle
    typed = items(g.mem, d.items)[2].handle
    glue_dialogs.ShortenDITL(g, w.addr, 2)
    assert glue_dialogs.CountDITL(g, w.addr) == 2
    rec = te.TERec(g.mem, g.mem.r32(d.text_h) & 0xFFFFFF)
    assert d.edit_field == -1 and not rec.active                   # the record stays
    assert g.heap.is_handle(rec.h_text) and not g.heap.is_handle(typed)
    assert not g.heap.is_handle(button) or button == rec.h_text     # its master pointer again
    assert g.heap.handle_size(d.items) == len(ditl(
        (ctrlItem + btnCtrl, (70, 230, 90, 290), b"OK"),
        (statText, (10, 10, 26, 200), b"Some text")))
    glue_dialogs.ShortenDITL(g, w.addr, 5)                         # as many as there are
    assert glue_dialogs.CountDITL(g, w.addr) == 0
    glue_dialogs.AppendDITL(g, w.addr, g.handle(ditl((statText, (5, 5, 20, 50), b"Back"))), 0)
    assert glue_dialogs.CountDITL(g, w.addr) == 1


def test_a_dialog_without_edit_text_has_a_textedit_record(qapp):
    """Generate Numbers sets the font of its About box's textH, a dialog
    of static text, and stops at its own check for NIL if there's none.
    The record has the dialog's font and is inactive until an editText
    item is edited with it, which then has its text."""
    g, w = ditl_dialog()
    d = Dialog(g.mem, w.addr)
    assert d.edit_field == -1 and d.text_h and g.heap.is_handle(d.text_h)
    rec = te.TERec(g.mem, g.mem.r32(d.text_h) & 0xFFFFFF)
    assert not rec.active and rec.tx_font == w.tx_font
    assert g.heap.is_handle(rec.h_text)
    glue_dialogs.AppendDITL(g, w.addr, g.handle(ditl(
        (editText, (40, 10, 56, 100), b"Typed"))), glue_dialogs.overlayDITL)
    typed = items(g.mem, d.items)[2].handle
    assert d.edit_field == 2 and rec.h_text == typed
    record = d.text_h
    g("DisposeDialog", w.addr)
    assert not g.heap.is_handle(record) and not g.heap.is_handle(typed)


def test_the_communications_toolbox_dispatch(qapp):
    """The glue for them calls _CommToolboxDispatch with a selector word at
    A0 and the arguments above it, and takes the result from D0."""
    g, w = ditl_dialog()
    cpu = g.cpu

    def call(selector, *args):
        sp = cpu.sp
        for code, value in args:
            (cpu.push32 if code == "l" else cpu.push16)(value & 0xFFFFFFFF)
        cpu.push16(selector)
        cpu.set_a(0, cpu.sp)
        g.run(b"\xa0\x8b")
        cpu.sp = sp
        return cpu.d(0)
    added = g.handle(ditl((statText, (5, 10, 21, 60), b"More")))
    call(0x0402, ("l", w.addr), ("l", added), ("w", 0))
    assert call(0x0403, ("l", w.addr)) == 3
    call(0x0404, ("l", w.addr), ("w", 2))
    assert call(0x0403, ("l", w.addr)) == 1
    assert call(0x0599) == 0xFFFFFFFF and g.missing
    assert g("Gestalt", struct.unpack(">I", b"ditl")[0]) == (0, 1)
