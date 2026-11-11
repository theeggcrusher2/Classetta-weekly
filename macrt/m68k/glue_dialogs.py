"""
Dialog Manager traps: dialogs, their items, and alerts.

A dialog is a window with more to its record (macrt.toolbox.dialogs).
Its items are drawn here: controls by the Control Manager, static text
with TETextBox, the edit field with TextEdit, icons and pictures with
QuickDraw, and user items by the application's own procedures.  The
TextEdit record of the item being edited uses that item's text handle as
its text, as on a Mac, so what GetDialogItemText reads is what's typed.

ModalDialog and Alert wait for the user as WaitNextEvent does, letting
the other applications run.  Only the dialog is updated meanwhile: the
application's other windows keep their pixels until it's done.
"""

from __future__ import annotations

from contextlib import contextmanager

from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.api import IN_FRONT
from ..toolbox.dialogs import (
    ALERT_ICON,
    HIDDEN,
    SIZE,
    Dialog,
    DialogState,
    Item,
    cautionIcon,
    control_colors,
    doAddSize,
    doBColor,
    doColor,
    doFace,
    doFont,
    doFontName,
    doMode,
    doSize,
    items,
    noteIcon,
    stopIcon,
    substitute,
    text_style,
)
from ..toolbox.events import EventRecord
from ..toolbox.memmgr import noErr
from ..toolbox.qdraw import frame_path, port_rgb, read_rgn, rect_path, round_rect_path
from ..toolbox.quickdraw import BLACK_PAT, GRAY_PAT
from ..toolbox.textedit import TERec
from ..toolbox.templates import (
    editText,
    iconItem,
    parse_alrt,
    parse_dlog,
    picItem,
    resCtrl,
    statText,
    userItem,
)
from ..toolbox.types import Point, Rect
from . import dispatch, glue_cport, glue_palette, wdefs
from .dispatch import raw, trap
from .glue_controls import (
    ACTION_IN_RECORD,
    Draw1Control,
    GetNewControl,
    HideControl,
    HiliteControl,
    ShowControl,
    TrackControl,
    _ctl,
    controls_of,
    dispose as dispose_control,
    kill_controls,
    new_control,
    scratch,
)
from .glue_fonts import family_named
from .glue_icons import GetCIcon, PlotCIcon, PlotIcon, is_cicon_record
from .glue_pict import DrawPicture
from .glue_windows import window_pictures

from . import glue_textedit as te

TE_HTEXT = 62                   # TERec.hText
TE_DEST, TE_VIEW = 0, 8
RETURN, ENTER, ESCAPE, TAB = 0x0D, 0x03, 0x1B, 0x09
ALL = 32767                     # SelectDialogItemText to the end


# =========================================================================
# Finding things
# =========================================================================
def _dialog(t, p: int):
    """The dialog a DialogPtr points at, None for anything else."""
    w = t.window(p)
    return w if w is not None and w.dialog is not None else None


def _rec(t, w) -> Dialog:
    return Dialog(t.mem, w.addr)


def _items(t, w) -> list[Item]:
    return items(t.mem, _rec(t, w).items)


def _item(t, w, n: int) -> Item | None:
    found = _items(t, w)
    return found[n - 1] if 1 <= n <= len(found) else None


def _edit_item(t, w) -> Item | None:
    n = _rec(t, w).edit_field
    return _item(t, w, n + 1) if n >= 0 else None


def _text(t, h: int) -> bytes:
    h &= t.mem.system.addr_mask
    return t.tb.contents(h) if h and t.mem.r32(h) & t.mem.system.addr_mask else b""


def _set_handle(t, h: int, data: bytes) -> None:
    heap = t.system.heap_of(h)
    if heap.set_handle_size(h, len(data)):
        t.mem.write(heap.deref(h), data)


def _the_port_var(t) -> int:
    return t.mem.r32(t.process.partition.a5) & t.mem.system.addr_mask


@contextmanager
def _in_port(t, w):
    """thePort set to w, for code that draws in the current port."""
    var = _the_port_var(t)
    saved = t.mem.r32(var)
    t.mem.w32(var, w.addr)
    try:
        yield
    finally:
        t.mem.w32(var, saved)


def _read_event(t, p: int) -> EventRecord:
    m = t.mem
    return EventRecord(m.r16(p), m.r32(p + 2), m.r32(p + 6), m.r_point(p + 10), m.r16(p + 14))


def _event_window(t, ev: EventRecord):
    msg = ev.message
    return msg if hasattr(msg, "addr") else t.window(msg)


def _event_dialog(t, ev: EventRecord):
    """The dialog an event is for, as IsDialogEvent decides: the one it
    names, the one clicked in, or for anything else the front window."""
    if ev.what in (k.updateEvt, k.activateEvt):
        w = _event_window(t, ev)
    elif ev.what == k.mouseDown:
        part, w = t.tb.FindWindow(ev.where)
        if part != k.inContent:
            return None
    else:
        w = t.tb.FrontWindow()
    return w if w is not None and w.dialog is not None else None


def _inval(t, w, r: Rect) -> None:
    with t.system.lock:
        b = w.port_bits_bounds
        w.invalidate(r.offset(-b.left, -b.top))
    t.process.events.wake()


# =========================================================================
# ParamText and sounds
# =========================================================================
def _params(t) -> list[bytes]:
    out = []
    for i in range(4):
        h = t.process.world.r32(lowmem.DAStrings + 4 * i) & t.mem.system.addr_mask
        out.append(t.mem.r_pstr(t.mem.r32(h) & t.mem.system.addr_mask) if h and t.mem.r32(h) else b"")
    return out


def _standard_beeper(t) -> int:
    return t.machine.stub_for(dispatch.trap_key(dispatch.SIGNATURES["SysBeep"].trap))


def _sound(t, n: int) -> None:
    """An alert stage's sound: the standard one beeps n times."""
    beeper = t.process.world.r32(lowmem.DABeeper) & t.mem.system.addr_mask
    if n <= 0 or not beeper:
        return
    if beeper == _standard_beeper(t):
        for _ in range(n):
            t.tb.SysBeep()
    else:
        t.call(beeper, (("h", n),))


# =========================================================================
# The edit field
# =========================================================================
def _new_record(t, w) -> None:
    """The dialog's TextEdit record, which it has from the start, with
    editText items or not: Generate Numbers sets the font of the record
    of its About box, which has none.  It takes the port's font, and
    stays inactive until an item is edited with it."""
    d = _rec(t, w)
    with _in_port(t, w):
        d.text_h = te.new(t, Rect(0, 0, 0, 0), Rect(0, 0, 0, 0))
    d.edit_field = -1


def _item_texts(t, w) -> set[int]:
    return {item.handle & t.mem.system.addr_mask for item in _items(t, w) if item.is_text}


def _own_text(t, w) -> None:
    """Give the record a text of its own, in place of the item's it was
    editing, before it's let go of or the item goes."""
    d = _rec(t, w)
    p = t.mem.r32(d.text_h) & t.mem.system.addr_mask
    if t.mem.r32(p + TE_HTEXT) & t.mem.system.addr_mask in _item_texts(t, w):
        t.mem.w32(p + TE_HTEXT, t.process.heap.new_handle(0))


def _set_edit_field(t, w, item: Item) -> None:
    """Make an editText item the one being edited: TextEdit's record is
    pointed at its rectangle and its text."""
    d = _rec(t, w)
    if d.edit_field == item.number - 1 and d.text_h:
        return
    if not d.text_h:
        _new_record(t, w)
    if not d.text_h:
        return
    te.deactivate(t, d.text_h)
    p = t.mem.r32(d.text_h) & t.mem.system.addr_mask
    own = t.mem.r32(p + TE_HTEXT) & t.mem.system.addr_mask
    if own and own not in _item_texts(t, w):
        t.system.heap_of(own).dispose_handle(own)
    t.mem.w_rect(p + TE_DEST, item.rect)
    t.mem.w_rect(p + TE_VIEW, item.rect)
    t.mem.w32(p + TE_HTEXT, item.handle)
    te.set_text(t, d.text_h, _text(t, item.handle))
    d.edit_field = item.number - 1
    _style_record(t, w)
    if w.hilited:
        te.activate(t, d.text_h)


def _select_text(t, w, item: Item, start: int, end: int) -> None:
    _set_edit_field(t, w, item)
    d = _rec(t, w)
    if d.text_h:
        te.set_select(t, d.text_h, start, end)


@contextmanager
def _styled(t, w, item: Item):
    """Draw a text item in its style in the dialog's item colour table, if
    it has one (Inside Macintosh V, the Dialog Manager): the dialog's font,
    face, size, colours and mode, with the fields the style says changed.
    A font named that no family has stays as it was; adding to size 0 adds
    to 12, the system font's, as TextSize's 0 is (Inside Macintosh I)."""
    style = text_style(w.dialog.item_colors, item.number) if w.dialog is not None else None
    if style is None:
        yield
        return
    qd, flags = t.tb.qd, style.flags
    saved = w.tx_font, w.tx_face, w.tx_size, w.tx_mode, port_rgb(w, True), port_rgb(w, False)
    if flags & doFontName:
        w.tx_font = family_named(t, style.font_name.decode("mac_roman")) or w.tx_font
    elif flags & doFont:
        w.tx_font = style.font
    if flags & doFace:
        w.tx_face = style.face
    if flags & doSize:
        w.tx_size = style.size
    elif flags & doAddSize:
        w.tx_size = (w.tx_size or 12) + style.size
    if flags & doMode:
        w.tx_mode = style.mode
    if flags & doColor:
        qd.set_rgb_color(w, True, style.fore)
    if flags & doBColor:
        qd.set_rgb_color(w, False, style.back)
    try:
        yield
    finally:
        w.tx_font, w.tx_face, w.tx_size, w.tx_mode = saved[:4]
        qd.set_rgb_color(w, True, saved[4])
        qd.set_rgb_color(w, False, saved[5])


def _style_record(t, w) -> None:
    """In a dialog with an item colour table, the TextEdit record takes
    the style of the editText item it's editing, the dialog's own if the
    item has none."""
    d = _rec(t, w)
    item = _item(t, w, d.edit_field + 1)
    if not w.dialog.item_colors or item is None or not d.text_h:
        return
    with _styled(t, w, item):
        ascent, descent, _, leading = t.tb.qd.font_info(w)
        rec = TERec(t.mem, t.mem.r32(d.text_h) & t.mem.system.addr_mask)
        if rec.tx_size == -1:           # a styled record has its own styles
            return
        rec.tx_font, rec.tx_face, rec.tx_mode, rec.tx_size = (w.tx_font, w.tx_face, w.tx_mode,
                                                              w.tx_size)
        rec.line_height, rec.font_ascent = ascent + descent + leading, ascent
        selection = rec.sel_start, rec.sel_end
    te.set_text(t, d.text_h, _text(t, item.handle))        # its lines again
    te.set_select(t, d.text_h, *selection)


def _item_colors(t, w, items_id: int) -> None:
    """A colour dialog's (or alert's) item colour table, the 'ictb' of its
    item list's ID (Inside Macintosh V, the Dialog Manager): its text items
    are drawn in their styles, and its controls get their colour tables,
    as SetCtlColor gives them."""
    h = t.process.resources.GetResource("ictb", items_id) if w.is_color else 0
    if not h or not t.mem.r32(h) & t.mem.system.addr_mask:
        return
    data = w.dialog.item_colors = t.tb.contents(h)
    for item in _items(t, w):
        table = control_colors(data, item.number) if item.is_control else None
        if table and _ctl(t, item.handle) is not None:
            ct = t.process.heap.new_handle_with(table)
            if ct:
                glue_cport._set_aux_color(t, item.handle & t.mem.system.addr_mask, ct, lowmem.AuxCtlHead)
    _style_record(t, w)


# =========================================================================
# Drawing
# =========================================================================
def _frame(t, w, r: Rect, pen: int = 1, pat: bytes = BLACK_PAT, shape=rect_path) -> None:
    t.tb.qd.fill(w, frame_path(shape, r, Point(pen, pen)), pat, k.patCopy)


def _text_box(t, w, data: bytes, r: Rect) -> None:
    """A text item, drawn with TEUpdate, which doesn't erase first (IM I,
    TextEdit): over the controls drawn before it, or what's under it.  An
    empty one draws nothing (Hermes's empty static text items, its
    buttons' click targets, are over its arrow pictures)."""
    if not data:
        return
    with _in_port(t, w):
        te.text_box(t, data, r, 0, erase=False)


def _plot_icon(t, w, r: Rect, h: int, color: bool = False) -> None:
    with _in_port(t, w), scratch(t, 8) as rp:
        t.set_rect(rp, r)
        (PlotCIcon if color else PlotIcon)(t, rp, h)


def _draw_picture(t, w, r: Rect, h: int) -> None:
    with _in_port(t, w), scratch(t, 8) as rp:
        t.set_rect(rp, r)
        DrawPicture(t, h, rp)


def _alert_icon(t, w, which: int) -> None:
    """An alert's icon: ICON 0, 1 or 2, which the System file has."""
    h = t.process.resources.GetResource("ICON", which)
    if h:
        _plot_icon(t, w, ALERT_ICON, h)


def _outline_default(t, w) -> None:
    """The bold outline an alert, or a dialog with SetDialogDefaultItem,
    draws around its default button."""
    item = _item(t, w, _rec(t, w).a_def_item)
    if item is None or not item.is_control or item.hidden:
        return
    c = _ctl(t, item.handle)
    if c is None or not c.visible:
        return
    _frame(t, w, c.rect.inset(-4, -4), 3, BLACK_PAT if c.active else GRAY_PAT,
           lambda r: round_rect_path(r, 16, 16))


def _draw_item(t, w, item: Item) -> None:
    if item.hidden:
        return
    kind, r = item.type, item.rect
    if item.is_control:
        if _ctl(t, item.handle) is not None:
            Draw1Control(t, item.handle)
    elif kind == statText:
        with _styled(t, w, item):
            _text_box(t, w, substitute(_text(t, item.handle), _params(t)), r)
    elif kind == editText:
        d = _rec(t, w)
        with _styled(t, w, item):
            if d.text_h and d.edit_field == item.number - 1:
                t.tb.qd.fill(w, rect_path(r), w.bk_pat, k.patCopy)
                te.update(t, d.text_h, r)
            else:
                _text_box(t, w, _text(t, item.handle), r)
        _frame(t, w, r.inset(-3, -3))
    elif kind == iconItem:
        h = item.handle & t.mem.system.addr_mask
        _plot_icon(t, w, r, h, is_cicon_record(t, h))
    elif kind == picItem:
        _draw_picture(t, w, r, item.handle)
    elif kind == userItem and item.handle & t.mem.system.addr_mask:
        with _in_port(t, w):
            t.call(item.handle & t.mem.system.addr_mask, (("p", w.addr), ("h", item.number)))


def _redraw_item(t, w, item: Item) -> None:
    if not item.is_control and not item.hidden:
        t.tb.qd.fill(w, rect_path(item.rect), w.bk_pat, k.patCopy)
    _draw_item(t, w, item)


def draw_dialog(t, w, area: Rect | None = None) -> None:
    """Draw the items (those in area, local, if given) in the order IM:
    Macintosh Toolbox Essentials, Dialog Manager, DrawDialog, gives: "draws
    all dialog items, calls the Control Manager procedure DrawControls to
    draw all controls, and calls the TextEdit procedure TEUpdate to update
    all static and editable text items".  So macrt draws the pictures,
    icons and user items first, in item order; then the window's controls
    as DrawControls does, the dialog's and any the program added with
    NewControl (Widget Workshop's puzzle list's scroll bar); then the
    text.  G-Netix's dialogs have a picture, after their buttons in the
    item list, with blank 3D button faces where the buttons are and an
    empty field where the message goes.  TEUpdate
    doesn't erase: Apple's own dialogs have text whose rect runs into a
    button (MacPaint's About box, Font Mover's help), and ScreenChooser's
    has its check boxes' labels inside their rects."""
    def meets(r: Rect) -> bool:
        r = r.inset(-4, -4)
        return area is None or not (r.right <= area.left or area.right <= r.left or
                                    r.bottom <= area.top or area.bottom <= r.top)
    items = [item for item in _items(t, w) if not item.is_control and meets(item.rect)]
    for item in items:
        if item.type not in (statText, editText):
            _draw_item(t, w, item)
    for c in controls_of(t, w):
        if meets(c.rect):
            Draw1Control(t, c.handle)
    for item in items:
        if item.type in (statText, editText):
            _draw_item(t, w, item)
    state = w.dialog
    if state.icon >= 0:
        _alert_icon(t, w, state.icon)
    if state.outline:
        _outline_default(t, w)


# =========================================================================
# Making and disposing of dialogs
# =========================================================================
def _copy_resource(t, rtype: str, rid: int) -> int:
    """A handle with a copy of a resource (the Dialog Manager changes
    its item lists, and the resources may be purged)."""
    h = t.process.resources.GetResource(rtype, rid)
    if not h:
        return 0
    return t.process.heap.new_handle_with(t.tb.contents(h))


def _make_items(t, w, first: int = 1) -> None:
    """Fill in the item list's placeholders, from item first on; the
    first edit text item is selected if none is.  A user item's is left
    as it is: a program that makes its own item list can have put the
    item's procedure there, where SetDItem would store it (LaserKey's
    installer, whose text is all user items)."""
    heap, rm = t.process.heap, t.process.resources
    first_edit = None
    for item in _items(t, w)[first - 1:]:
        kind, data = item.type, item.data
        if item.is_control:
            if kind & 3 == resCtrl:
                # The template's size, at the item's place.
                rid = int.from_bytes(data[:2], "big", signed=True)
                h = GetNewControl(t, rid, w.addr)
                c = _ctl(t, h)
                if c is not None:
                    c.rect = c.rect.moved_to(item.rect.left, item.rect.top)
            else:
                h = new_control(t, w, item.rect, data, True, 0, 0, 1, kind & 3, 0)
        elif item.is_text:
            h = heap.new_handle_with(data)
            if kind == editText and first_edit is None:
                first_edit = item
        elif kind == iconItem:
            rid = int.from_bytes(data[:2], "big", signed=True)
            h = (GetCIcon(t, rid) if t.system.model.color else 0) or rm.GetResource("ICON", rid)
        elif kind == picItem:
            h = rm.GetResource("PICT", int.from_bytes(data[:2], "big", signed=True))
        elif kind == userItem:
            continue
        else:
            h = 0
        item.handle = h
    if first_edit is not None and _rec(t, w).edit_field < 0:
        _select_text(t, w, first_edit, 0, ALL)


def new_dialog(t, storage: int, bounds: Rect, title: str, visible: bool, proc_id: int,
               behind, go_away: bool, ref_con: int, item_list: int, color: bool = False):
    """NewDialog's work: the dialog's window, None if there's no room; with
    color, and Color QuickDraw, NewColorDialog's, whose port is a cGrafPort."""
    heap = t.process.heap
    addr = storage or heap.new_ptr(SIZE, clear=True)
    if not addr:
        return None
    color = color and t.system.model.color
    w = t.tb.NewWindow(bounds, title, False, proc_id, behind, go_away, ref_con, addr,
                       color=color)
    if w is None:
        if not storage:
            heap.dispose_ptr(addr)
        return None
    if color:
        glue_palette.new_window(t, w)
    w.owns_storage = not storage
    t.mem.fill(addr + w.SIZE, SIZE - w.SIZE)
    w.window_kind = k.dialogKind
    w.dialog = DialogState()
    w.tx_font = t.process.world.r16(lowmem.DlgFont)
    d = _rec(t, w)
    d.items, d.edit_field, d.a_def_item = item_list, -1, 1
    _new_record(t, w)
    _make_items(t, w)
    if visible:
        t.tb.ShowWindow(w)
    return w


def set_font(t, w, font: int, face: int, size: int) -> None:
    """The dialog's text in font, face and size, its edit text's too: its
    TextEdit record takes the port's font when it's made, so is made again."""
    w.tx_font, w.tx_face, w.tx_size = font, face, size
    d = _rec(t, w)
    item = _edit_item(t, w)
    if d.text_h:
        _own_text(t, w)
        te.dispose(t, d.text_h)
    _new_record(t, w)
    if item is not None:
        _select_text(t, w, item, 0, ALL)


def _behind(t, p: int):
    return IN_FRONT if p == 0xFFFFFFFF else t.window(p)


@trap
def NewDialog(t, storage, bounds, title, visible, proc_id, behind, go_away, ref_con, item_list):
    return new_dialog(t, storage, t.rect(bounds), t.str255(title), bool(visible), proc_id,
                      _behind(t, behind), bool(go_away), ref_con, item_list) or 0


@trap
def NewColorDialog(t, storage, bounds, title, visible, proc_id, behind, go_away, ref_con,
                   item_list):
    return new_dialog(t, storage, t.rect(bounds), t.str255(title), bool(visible), proc_id,
                      _behind(t, behind), bool(go_away), ref_con, item_list, color=True) or 0


def _colored(t, w, ct: int) -> None:
    """Give a colour dialog (or alert) the colours of its 'dctb' (or
    'actb') ct, if it has any: one whose ctSize is -1 says the default
    colours (Inside Macintosh V, the Dialog Manager)."""
    if w is None or not w.is_color:
        return
    p = t.system.heap_of(ct).deref(ct)
    if p and t.mem.r16s(p + 6) != -1:
        glue_cport._set_win_color(t, w.addr, ct)


@trap
def GetNewDialog(t, dialog_id, storage, behind):
    """The dialog is a colour one if there's a 'dctb' of its ID, and an
    old grafPort if there isn't (Inside Macintosh V, the Dialog Manager):
    Pinball Arcade draws its ball straight into its palette dialog's
    portBits."""
    h = t.process.resources.GetResource("DLOG", dialog_id)
    if not h:
        return 0
    tmpl = parse_dlog(t.tb.contents(h))
    item_list = _copy_resource(t, "DITL", tmpl.items_id)
    if not item_list:
        return 0
    ct = t.process.resources.GetResource("dctb", dialog_id)
    bounds = t.tb.placed(tmpl.bounds, tmpl.position, tmpl.proc_id)
    w = new_dialog(t, storage, bounds, tmpl.title, False, tmpl.proc_id, _behind(t, behind),
                   tmpl.go_away, tmpl.ref_con, item_list, color=bool(ct))
    if w is None:
        t.process.heap.dispose_handle(item_list)    # the copy is ours, not the caller's
        return 0
    if ct:
        _colored(t, w, ct)
        _item_colors(t, w, tmpl.items_id)
    if tmpl.visible:
        t.tb.ShowWindow(w)
    return w


def close_dialog(t, w, dispose: bool) -> None:
    """CloseDialog, and with dispose DisposeDialog, which frees the item
    list and the record too."""
    d = _rec(t, w)
    if d.text_h:
        _own_text(t, w)                 # the item's text goes with the items
        te.dispose(t, d.text_h)
    d.text_h = 0
    for item in _items(t, w):
        h = item.handle & t.mem.system.addr_mask
        if item.is_text and h:
            t.system.heap_of(h).dispose_handle(h)
            item.handle = 0
    kill_controls(t, w, erase=False)
    if dispose and d.items & t.mem.system.addr_mask:
        t.system.heap_of(d.items & t.mem.system.addr_mask).dispose_handle(d.items & t.mem.system.addr_mask)
    w.dialog = None
    (t.tb.DisposeWindow if dispose else t.tb.CloseWindow)(w)


@trap
def CloseDialog(t, p):
    w = _dialog(t, p)
    if w is not None:
        close_dialog(t, w, dispose=False)


@trap
def DisposeDialog(t, p):
    w = _dialog(t, p)
    if w is not None:
        close_dialog(t, w, dispose=True)


@trap
def InitDialogs(t, resume_proc):
    world, heap = t.process.world, t.process.heap
    world.w32(lowmem.ResumeProc, resume_proc)
    world.w32(lowmem.DABeeper, _standard_beeper(t))
    world.w16(lowmem.ANumber, -1)
    world.w16(lowmem.ACount, -1)
    world.w16(lowmem.DlgFont, 0)
    for i in range(4):
        world.w32(lowmem.DAStrings + 4 * i, heap.new_handle(1, clear=True))


@trap
def ParamText(t, p0, p1, p2, p3):
    world, heap = t.process.world, t.process.heap
    for i, p in enumerate((p0, p1, p2, p3)):
        if not p:
            continue
        data = t.pstr(p)
        h = world.r32(lowmem.DAStrings + 4 * i) & t.mem.system.addr_mask
        if not h:
            h = heap.new_handle(0)
            world.w32(lowmem.DAStrings + 4 * i, h)
        _set_handle(t, h, bytes([len(data)]) + data)


@trap
def ErrorSound(t, proc):
    t.process.world.w32(lowmem.DABeeper, proc)


for _name in ("CouldDialog", "FreeDialog", "CouldAlert", "FreeAlert"):
    def _nothing(t, rid):       # resources are never purged here
        pass
    _nothing.__name__ = _name
    trap(_nothing)


# =========================================================================
# Drawing them
# =========================================================================
@trap
def DrawDialog(t, p):
    w = _dialog(t, p)
    if w is not None:
        draw_dialog(t, w)


@trap
def UpdateDialog(t, p, rgn):
    w = _dialog(t, p)
    if w is not None:
        draw_dialog(t, w, read_rgn(t.mem, rgn).bbox if rgn else None)


# =========================================================================
# Events
# =========================================================================
def _press(t, w, n: int) -> bool:
    """What Return and Escape do: flash item n's button, if it's an
    active one."""
    item = _item(t, w, n)
    if item is None or not item.is_control or item.hidden:
        return False
    c = _ctl(t, item.handle)
    if c is None or not c.visible or not c.active:
        return False
    HiliteControl(t, c.handle, k.inButton)
    t.sleep(8)
    HiliteControl(t, c.handle, k.noHilite)
    return True


def _standard_filter(t, w, ev: EventRecord, item_p: int) -> bool:
    """Return and Enter press the default button; Escape and Command-period
    the cancel button, if there is one."""
    if ev.what not in (k.keyDown, k.autoKey):
        return False
    ch = ev.message & 0xFF
    if ch in (RETURN, ENTER):
        n = _rec(t, w).a_def_item
    elif ch == ESCAPE or (ev.modifiers & k.cmdKey and ch == ord(".")):
        n = w.dialog.cancel_item
    else:
        return False
    if not _press(t, w, n):
        return False
    t.w16(item_p, n)
    return True


def _next_field(t, w) -> None:
    """Tab: on to the next editText item, its text selected."""
    edits = [i for i in _items(t, w) if i.type == editText and not i.hidden]
    if not edits:
        return
    n = _rec(t, w).edit_field + 1
    later = [i for i in edits if i.number > n]
    _select_text(t, w, (later or edits)[0], 0, ALL)


def _select(t, w, ev: EventRecord, modal: bool) -> tuple[bool, int]:
    """DialogSelect for dialog w: handle the event; whether it hit an
    enabled item, and which."""
    what = ev.what
    d = _rec(t, w)
    if what == k.updateEvt:
        if _event_window(t, ev) is w:
            t.tb.BeginUpdate(w)
            draw_dialog(t, w)
            t.tb.EndUpdate(w)
        return False, 0
    if what == k.activateEvt:
        if _event_window(t, ev) is w and d.text_h and d.edit_field >= 0:
            (te.activate if ev.modifiers & k.activeFlag else te.deactivate)(t, d.text_h)
        return False, 0
    if what == k.mouseDown:
        part, win = t.tb.FindWindow(ev.where)
        if win is not w or part != k.inContent:
            if modal:
                t.tb.SysBeep()
            return False, 0
        pt = w.global_to_local(ev.where)
        item = next((i for i in _items(t, w) if i.rect.contains(pt)), None)
        if item is None:
            return False, 0
        if item.is_control:
            c = _ctl(t, item.handle)
            if c is None or not c.visible or not c.active:
                return False, 0
            hit = TrackControl(t, c.handle, pt, ACTION_IN_RECORD)
            return bool(hit) and item.enabled, item.number
        if item.type == editText:
            _set_edit_field(t, w, item)
            te.click(t, d.text_h, pt, bool(ev.modifiers & k.shiftKey))
        return item.enabled, item.number
    if what in (k.keyDown, k.autoKey):
        if ev.modifiers & k.cmdKey:
            return False, 0
        if ev.message & 0xFF == TAB:
            _next_field(t, w)
            return False, 0
        item = _edit_item(t, w)
        if item is None or not d.text_h:
            return False, 0
        te.key(t, d.text_h, ev.message & 0xFF)
        return item.enabled, item.number
    if what == k.nullEvent and d.text_h:
        te.idle(t, d.text_h)
    return False, 0


def _update_wanted(t, w):
    """ModalDialog's updates: the dialog's only, unless it has a picture
    of its own (window_pictures draws that)."""
    def next_update():
        with t.system.lock:
            return w if w.visible and w.update_pending and not w.window_pic else None
    return next_update


def _modal(t, w, filter_proc: int, item_p: int) -> None:
    # high-level events go to a filter procedure, which can handle them;
    # without one they're left for the application's event loop, as Find
    # File's Open Application event is when it puts up an alert first
    mask = k.everyEvent if filter_proc else k.everyEvent & ~k.highLevelEventMask
    with scratch(t, 16) as ev_p:
        while True:
            window_pictures(t)
            wdefs.refresh(t)            # as at GetNextEvent
            with t.yielding():
                _, ev = t.process.events.get(mask, 1, _update_wanted(t, w))
            t.write_event(ev_p, ev)
            if filter_proc:
                if t.call(filter_proc, (("p", w.addr), ("p", ev_p), ("p", item_p)), "b"):
                    return
                ev = _read_event(t, ev_p)
            elif _standard_filter(t, w, ev, item_p):
                return
            hit, n = _select(t, w, ev, modal=True)
            if hit:
                t.w16(item_p, n)
                return


@trap
def ModalDialog(t, filter_proc, item_p):
    w = t.tb.FrontWindow()
    if w is None or w.dialog is None:
        w = next((x for x in t.process.windows if x.dialog is not None), None)
    if w is None:
        t.w16(item_p, -1)
        return
    _modal(t, w, filter_proc, item_p)


@trap
def IsDialogEvent(t, ev_p):
    return _event_dialog(t, _read_event(t, ev_p)) is not None


@trap
def DialogSelect(t, ev_p, dialog_p, item_p):
    ev = _read_event(t, ev_p)
    w = _event_dialog(t, ev)
    if w is None:
        return False
    t.w32(dialog_p, w.addr)
    hit, n = _select(t, w, ev, modal=False)
    if hit:
        t.w16(item_p, n)
    return hit


@trap
def StdFilterProc(t, p, ev_p, item_p):
    w = _dialog(t, p)
    return w is not None and _standard_filter(t, w, _read_event(t, ev_p), item_p)


@trap
def GetStdFilterProc(t, proc_p):
    t.w32(proc_p, t.machine.std_filter)
    return noErr


@trap
def SetDialogDefaultItem(t, p, n):
    w = _dialog(t, p)
    if w is not None:
        _rec(t, w).a_def_item = n
        w.dialog.outline = True
        _outline_default(t, w)
    return noErr


@trap
def SetDialogCancelItem(t, p, n):
    w = _dialog(t, p)
    if w is not None:
        w.dialog.cancel_item = n
    return noErr


@trap
def SetDialogTracksCursor(t, p, tracks):
    w = _dialog(t, p)
    if w is not None:
        w.dialog.tracks_cursor = bool(tracks)
    return noErr


# =========================================================================
# Items
# =========================================================================
@trap
def GetDialogItem(t, p, n, type_p, h_p, rect_p):
    w = _dialog(t, p)
    item = _item(t, w, n) if w is not None else None
    if item is None:
        return
    t.w16(type_p, item.kind)
    t.w32(h_p, item.handle)
    t.set_rect(rect_p, item.rect)


@trap
def SetDialogItem(t, p, n, kind, h, rect_p):
    w = _dialog(t, p)
    item = _item(t, w, n) if w is not None else None
    if item is None:
        return
    item.kind = kind & 0xFF
    item.handle = h
    item.rect = t.rect(rect_p)


@trap
def GetDialogItemText(t, h, str_p):
    t.set_str255(str_p, _text(t, h)[:255])


def _owner(t, h: int):
    """The dialog, and the item, whose text a handle is."""
    for w in t.process.windows:
        if w.dialog is not None:
            for item in _items(t, w):
                if item.is_text and item.handle & t.mem.system.addr_mask == h:
                    return w, item
    return None, None


@trap
def SetDialogItemText(t, h, str_p):
    h &= t.mem.system.addr_mask
    if not h:
        return
    data = t.pstr(str_p)
    w, item = _owner(t, h)
    d = _rec(t, w) if w is not None else None
    if d is not None and d.text_h and d.edit_field == item.number - 1:
        te.set_text(t, d.text_h, data)          # into its hText, which is h
    else:
        _set_handle(t, h, data)
    if item is not None:
        _redraw_item(t, w, item)


@trap
def SelectDialogItemText(t, p, n, start, end):
    w = _dialog(t, p)
    item = _item(t, w, n) if w is not None else None
    if item is not None and item.type == editText:
        _select_text(t, w, item, start, end)


@trap
def HideDialogItem(t, p, n):
    w = _dialog(t, p)
    item = _item(t, w, n) if w is not None else None
    if item is None or item.hidden:
        return
    r = item.rect
    if item.is_control:
        HideControl(t, item.handle)
    else:
        area = r.inset(-4, -4)
        t.tb.qd.fill(w, rect_path(area), w.bk_pat, k.patCopy)
        _inval(t, w, area)
    item.rect = r.offset(HIDDEN, 0)


@trap
def ShowDialogItem(t, p, n):
    w = _dialog(t, p)
    item = _item(t, w, n) if w is not None else None
    if item is None or not item.hidden:
        return
    item.rect = r = item.rect.offset(-HIDDEN, 0)
    if item.is_control:
        ShowControl(t, item.handle)
    else:
        _inval(t, w, r.inset(-4, -4))


@trap
def FindDialogItem(t, p, pt):
    w = _dialog(t, p)
    if w is not None:
        for item in _items(t, w):
            if item.rect.contains(pt):
                return item.number - 1
    return -1


# =========================================================================
# Adding items and taking them away (Inside Macintosh: Macintosh Toolbox
# Essentials, the Dialog Manager): System 7's, and the Communications
# Toolbox's before it
# =========================================================================
overlayDITL, appendDITLRight, appendDITLBottom = 0, 1, 2


def _end(t, w) -> int:
    """Where in the item list its last item ends."""
    found = _items(t, w)
    if not found:
        return 2
    last = found[-1]
    return last.offset + 14 + last.length + (last.length & 1)


def append_ditl(t, w, ditl: bytes, method: int) -> None:
    """The items of an item list, a DITL's bytes, added after the dialog's:
    as far from the dialog's top left as their rectangles say, or its top
    right, its bottom left or the top left of item -method.  The top left
    is the port rectangle's, which for most dialogs is (0, 0), but the
    Sound control panel's window has its origin where its 'nrct' starts,
    and its items are placed from there.  Put at the right or the bottom,
    they're given as much room past them as before them."""
    entries, at = [], 2
    for _ in range((int.from_bytes(ditl[:2], "big") + 1) & 0xFFFF if len(ditl) >= 2 else 0):
        if at + 14 > len(ditl):
            break
        length = ditl[at + 13]
        entries.append(bytes(4) + ditl[at + 4:at + 14 + length + (length & 1)])
        at += len(entries[-1])
    h = _rec(t, w).items & t.mem.system.addr_mask
    if not entries or not h:
        return
    port = w.port_rect
    anchor = _item(t, w, -method) if method < 0 else None
    dh, dv = ((port.right, port.top) if method == appendDITLRight
              else (port.left, port.bottom) if method == appendDITLBottom
              else (anchor.rect.left, anchor.rect.top) if anchor is not None
              else (port.left, port.top))
    before, end = len(_items(t, w)), _end(t, w)
    heap = t.system.heap_of(h)
    if not heap.set_handle_size(h, end + sum(map(len, entries))):
        return
    p = heap.deref(h)
    t.mem.w16(p, before + len(entries) - 1)
    t.mem.write(p + end, b"".join(entries))
    added = _items(t, w)[before:]
    for item in added:
        item.rect = item.rect.offset(dh, dv)
    _make_items(t, w, before + 1)
    shown = [item.rect for item in added if not item.hidden]
    if not shown:
        return
    area = shown[0]
    for r in shown[1:]:
        area = area.union(r)
    if method in (appendDITLRight, appendDITLBottom):
        right = max(port.right, area.right + max(area.left - dh, 0))
        bottom = max(port.bottom, area.bottom + max(area.top - dv, 0))
        if (right, bottom) != (port.right, port.bottom):
            t.tb.SizeWindow(w, right - port.left, bottom - port.top, True)
    _inval(t, w, area.inset(-4, -4))


def shorten_ditl(t, w, n: int) -> None:
    """The dialog's last n items gone, and what they had: their controls,
    their text and, if one was being edited, its TextEdit record."""
    found = _items(t, w)
    n = min(max(n, 0), len(found))
    if not n:
        return
    d = _rec(t, w)
    gone = found[len(found) - n:]
    for item in reversed(gone):
        h = item.handle & t.mem.system.addr_mask
        if d.text_h and d.edit_field == item.number - 1:
            te.deactivate(t, d.text_h)
            _own_text(t, w)
            d.edit_field = -1
        if item.is_control:
            c = _ctl(t, h)
            if c is not None:
                dispose_control(t, c)
        else:
            if item.is_text and h:
                t.system.heap_of(h).dispose_handle(h)
            if not item.hidden:
                area = item.rect.inset(-4, -4)
                t.tb.qd.fill(w, rect_path(area), w.bk_pat, k.patCopy)
                _inval(t, w, area)
    list_h = d.items & t.mem.system.addr_mask
    t.mem.w16(t.mem.r32(list_h) & t.mem.system.addr_mask, len(found) - n - 1)
    t.system.heap_of(list_h).set_handle_size(list_h, gone[0].offset)


def AppendDITL(t, p, ditl, method):
    w = _dialog(t, p)
    if w is not None and ditl & t.mem.system.addr_mask:
        append_ditl(t, w, _text(t, ditl), method)


def ShortenDITL(t, p, n):
    w = _dialog(t, p)
    if w is not None:
        shorten_ditl(t, w, n)


def CountDITL(t, p):
    w = _dialog(t, p)
    return len(_items(t, w)) if w is not None else 0


# _CommToolboxDispatch's: a selector word at A0, the routine's Pascal
# arguments above it, and its result in D0
CTB_ROUTINES = {
    0x0402: ("AppendDITL", "pLh", AppendDITL),
    0x0403: ("CountDITL", "p", CountDITL),
    0x0404: ("ShortenDITL", "ph", ShortenDITL),
}


@raw(0xA08B)
def CommToolboxDispatch(t, word):
    """The Communications Toolbox's routines, of which macrt has those
    that System 7 has too."""
    cpu = t.cpu
    pb = cpu.a(0) & cpu.mask
    selector = t.mem.r16(pb)
    routine = CTB_ROUTINES.get(selector)
    if routine is None:
        t.unimplemented(f"Communications Toolbox routine ${selector:04X}", word)
        cpu.set_d(0, 0xFFFFFFFF)            # an error
        return
    _, codes, fn = routine
    args, _ = dispatch.pop_args(t.mem, pb + 2, codes)
    cpu.set_d(0, (fn(t, *args) or 0) & 0xFFFFFFFF)


# =========================================================================
# Alerts
# =========================================================================
def _alert(t, alert_id: int, filter_proc: int, icon: int = -1) -> int:
    """Alert's work, and StopAlert's, NoteAlert's and CautionAlert's with
    their icons: sound this stage's beeps and, if the stage shows the
    alert, show it until the user picks an item."""
    h = t.process.resources.GetResource("ALRT", alert_id)
    if not h:
        t.note_once(f"ALRT {alert_id}", "an alert asks for ALRT %d, which isn't in any open "
                    "resource file; nothing shown", alert_id)
        return -1
    a = parse_alrt(t.tb.contents(h))
    world = t.process.world
    same = world.r16(lowmem.ANumber) == alert_id
    count = min(world.r16(lowmem.ACount) + 1, 3) if same else 0
    world.w16(lowmem.ANumber, alert_id)
    world.w16(lowmem.ACount, count)
    stage = a.stages[count]
    _sound(t, stage.sounds)
    if not stage.drawn:
        return -1
    item_list = _copy_resource(t, "DITL", a.items_id)
    if not item_list:
        return -1
    ct = t.process.resources.GetResource("actb", alert_id)
    bounds = t.tb.placed(a.bounds, a.position, k.dBoxProc)
    w = new_dialog(t, 0, bounds, "", False, k.dBoxProc, IN_FRONT, False, 0, item_list,
                   color=bool(ct))
    if w is None:
        t.process.heap.dispose_handle(item_list)
        return -1
    if ct:
        _colored(t, w, ct)
        _item_colors(t, w, a.items_id)
    _rec(t, w).a_def_item = stage.bold_item
    w.dialog.outline, w.dialog.icon = True, icon
    t.tb.ShowWindow(w)
    try:
        with scratch(t, 2) as item_p:
            _modal(t, w, filter_proc, item_p)
            return t.mem.r16s(item_p)
    finally:
        close_dialog(t, w, dispose=True)


@trap
def Alert(t, alert_id, filter_proc):
    return _alert(t, alert_id, filter_proc)


@trap
def StopAlert(t, alert_id, filter_proc):
    return _alert(t, alert_id, filter_proc, stopIcon)


@trap
def NoteAlert(t, alert_id, filter_proc):
    return _alert(t, alert_id, filter_proc, noteIcon)


@trap
def CautionAlert(t, alert_id, filter_proc):
    return _alert(t, alert_id, filter_proc, cautionIcon)
