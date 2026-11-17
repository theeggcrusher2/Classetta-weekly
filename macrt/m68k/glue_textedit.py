"""
TextEdit traps (Inside Macintosh I-371, V-261): edit records made, edited
and drawn by macrt.toolbox.textedit, with the record's procedures (word
break, click loop, highlighting hooks) run on the 68k, and its port's own
textProc and txMeasProc, if it has them, drawing and measuring its text.

The lower-case functions are the same calls for Python code, such as the
Dialog Manager's editText items: each takes the trap context and a
TEHandle, and the record they make or change is the same TERec an
application's TECut or TEKey would take.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from ..toolbox import lowmem, textedit
from ..toolbox.events import ProcessKilled, tick_count
from ..toolbox.memmgr import memFullErr, noErr
from ..toolbox.qdraw import Region, read_rgn, write_rgn
from ..toolbox.scrap import TEXT
from ..toolbox.textedit import (EditRecord, Hooks, Style, Tracker, read_style_scrap, teBitClear,
                                teBitSet, teFAutoScroll)
from ..toolbox.types import Point, Rect
from . import glue_scrap
from .dispatch import trap
from .glue_controls import _s16
from .glue_common import _port, _the_port_var
from .glue_quickdraw import draw_with, measure_with, shape_proc, text_procs

STYL = b"styl"
ERASE = 2                       # a GrafVerb


class _Hooks(Hooks):
    """An edit record's procedures, run on the 68k."""

    def __init__(self, t):
        self.t = t

    @contextmanager
    def _record(self, h: int):
        """The record locked, and its port the current one, as TextEdit
        has them while it calls out."""
        t = self.t
        heap = t.system.heap_of(h)
        state = heap.get_state(h)
        heap.lock(h)
        the_port = _the_port_var(t)
        port = t.mem.r32(the_port)
        t.mem.w32(the_port, textedit.TERec(t.mem, heap.deref(h)).in_port)
        try:
            yield heap.deref(h)
        finally:
            t.mem.w32(the_port, port)
            heap.set_state(h, state)

    def word_break(self, proc: int, text_ptr: int, offset: int) -> bool:
        """A0 the text, D0 the offset: Z clear for a break."""
        cpu = self.t.cpu
        ctx = cpu.context()
        cpu.set_a(0, text_ptr)
        cpu.set_d(0, offset)
        self.t._jsr(proc)
        brk = not cpu.sr & 0x4
        cpu.restore(ctx)
        return brk

    def clik_loop(self, proc: int, h: int) -> bool:
        """TRUE in D0 to go on tracking the mouse."""
        cpu = self.t.cpu
        ctx = cpu.context()
        with self._record(h):
            self.t._jsr(proc)
        go = cpu.d(0) & 0xFF != 0
        cpu.restore(ctx)
        return go

    def hilite(self, proc: int, h: int, r: Rect) -> None:
        """highHook or caretHook: A3 the locked record and the rectangle's
        address on the stack above the return address, as _InvertRect, the
        hook TextEdit has itself, takes it (Jazz's own hook pops it and
        returns)."""
        t = self.t
        cpu = t.cpu
        ctx = cpu.context()
        with self._record(h) as p:
            cpu.sp = cpu.sp - 8
            r_ptr = cpu.sp
            t.mem.w_rect(r_ptr, r)
            cpu.set_a(3, p)
            pc = cpu.pc
            cpu.push32(t.machine.return_stub)
            cpu.push32(r_ptr)
            t.run_until(t.machine.return_stub, proc)
            cpu.pc = pc
        cpu.restore(ctx)

    def text_procs(self, port) -> tuple[int, int]:
        return text_procs(self.t, port)

    def measure(self, proc: int, h: int, text_ptr: int, count: int) -> int:
        with self._record(h):
            return measure_with(self.t, proc, text_ptr, count)

    @contextmanager
    def _drawing(self, h: int, clip: Region):
        """As _record, with the port's clip region narrowed to clip, the
        line and the viewRect, as TextEdit's own drawing is."""
        t = self.t
        with self._record(h) as p:
            port = t.system.port_at(textedit.TERec(t.mem, p).in_port & t.mem.system.addr_mask)
            old = read_rgn(t.mem, port.clip_rgn) if port is not None and port.clip_rgn else None
            if old is not None:
                write_rgn(t.system.heap_of(port.clip_rgn), port.clip_rgn, old.sect(clip))
            try:
                yield
            finally:
                if old is not None:
                    write_rgn(t.system.heap_of(port.clip_rgn), port.clip_rgn, old)

    def draw(self, proc: int, h: int, text_ptr: int, count: int, clip: Region) -> None:
        with self._drawing(h, clip):
            draw_with(self.t, proc, text_ptr, count)

    def rect_proc(self, port) -> int:
        return shape_proc(self.t, port, "Rect")

    def erase(self, proc: int, h: int, r: Rect, clip: Region) -> None:
        """The rectangle on the stack, and the verb erase."""
        t = self.t
        cpu = t.cpu
        ctx = cpu.context()
        with self._drawing(h, clip):
            cpu.sp = cpu.sp - 8
            t.mem.w_rect(cpu.sp, r)
            t.call(proc, (("b", ERASE), ("p", cpu.sp)))
        cpu.restore(ctx)


def _tracker(t) -> Tracker:
    events = t.process.events

    def pause():
        with t.yielding():
            time.sleep(1 / 60)
        if events.killed:
            raise ProcessKilled()
    return Tracker(t.tb.StillDown, lambda: t.tb._d.input.mac_pos, pause, tick_count,
                   t.mem.r32(lowmem.DoubleTime))


def do_text(t) -> None:
    """TEDoText's routine, called with JSR: A3 the locked record, D3 and D4
    the characters and D7 what to do with them (EditRecord.do_text); the
    port in A0, and from a hit test the offset in D0."""
    cpu = t.cpu
    cpu.pc = cpu.pop32()
    p = cpu.a(3) & cpu.mask
    heap = t.system.find_heap(p)
    h = heap.recover_handle(p) if heap is not None else 0
    rec = edit(t, h)
    if rec is not None:
        offset = rec.do_text(_s16(cpu.d(7)), _s16(cpu.d(3)), _s16(cpu.d(4)))
        if _s16(cpu.d(7)) == textedit.teFind:
            cpu.set_d(0, offset)
    cpu.set_a(0, t.mem.r32(_the_port_var(t)))


def edit(t, h: int) -> EditRecord | None:
    """The edit record h is, None if it's no handle."""
    h &= t.mem.system.addr_mask
    if not h or not t.mem.r32(h) & t.mem.system.addr_mask:
        return None
    try:
        return EditRecord(t.system, t.tb.qd, h, _Hooks(t))
    except ValueError:
        return None


# =========================================================================
# For the rest of the Toolbox
# =========================================================================
def new(t, dest: Rect, view: Rect, styled: bool = False) -> int:
    """TENew (TEStyleNew): a record drawing in the current port; 0 (and
    MemErr) if there's no room."""
    port = _port(t)
    h = 0
    if port is not None:
        h = textedit.new_record(t.system, t.tb.qd, t.process.heap, port, dest, view, styled)
    t.set_mem_err(noErr if h else memFullErr)
    return h


def dispose(t, h: int) -> None:
    if edit(t, h) is not None:
        textedit.dispose_record(t.system, h)


def set_text(t, h: int, data: bytes) -> None:
    ed = edit(t, h)
    if ed is not None:
        t.set_mem_err(noErr if ed.set_text(data) else memFullErr)


def set_select(t, h: int, start: int, end: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.select(start, end)


def activate(t, h: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.activate()


def deactivate(t, h: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.deactivate()


def key(t, h: int, char: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.key(char)


def click(t, h: int, pt: Point, extend: bool) -> None:
    """TEClick: pt is local to the record's port."""
    ed = edit(t, h)
    if ed is not None:
        ed.click(pt, bool(extend), _tracker(t))
        t.take_mouse_up()


def idle(t, h: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.idle()


def update(t, h: int, r: Rect) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.update(r)


def _to_te_scrap(t, ed: EditRecord) -> None:
    """The selection into TextEdit's scrap, and for a styled record into
    the desk scrap too, with its styles."""
    a, b = ed.selection
    data = ed.text[a:b]
    if not textedit.set_te_scrap(t.process.world, t.process.heap, data):
        t.set_mem_err(memFullErr)
        return
    if ed.styled:
        glue_scrap.zero_scrap(t)
        glue_scrap.put_scrap(t, TEXT, data)
        glue_scrap.put_scrap(t, STYL, ed.style_scrap(a, b))
    t.set_mem_err(noErr)


def cut(t, h: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        _to_te_scrap(t, ed)
        ed.delete()
        ed.sel_view()


def copy(t, h: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        _to_te_scrap(t, ed)


def paste(t, h: int) -> None:
    """TEPaste: TextEdit's scrap replaces the selection."""
    ed = edit(t, h)
    if ed is not None:
        data = textedit.te_scrap(t.process.world, t.mem)
        t.set_mem_err(noErr if ed.paste(data) else memFullErr)


def delete(t, h: int) -> None:
    ed = edit(t, h)
    if ed is not None:
        ed.delete()
        ed.sel_view()


def insert(t, h: int, data: bytes) -> None:
    ed = edit(t, h)
    if ed is not None:
        t.set_mem_err(noErr if ed.insert(data) else memFullErr)


def text_box(t, data: bytes, r: Rect, just: int, erase: bool = True) -> None:
    textedit.text_box(t.tb.qd, _port(t), data, r, just, erase)


# =========================================================================
# The traps
# =========================================================================
@trap
def TEInit(t):
    textedit.set_te_scrap(t.process.world, t.process.heap, b"")


@trap
def TENew(t, dest, view):
    return new(t, t.rect(dest), t.rect(view))


@trap
def TEStyleNew(t, dest, view):
    return new(t, t.rect(dest), t.rect(view), styled=True)


@trap
def TEDispose(t, h):
    dispose(t, h)


@trap
def TESetText(t, p, length, h):
    set_text(t, h, t.mem.read(p, max(length, 0)))


@trap
def TEGetText(t, h):
    ed = edit(t, h)
    return ed.te.h_text if ed is not None else 0


@trap
def TECalText(t, h):
    ed = edit(t, h)
    if ed is not None:
        ed.cal_text()


@trap
def TESetSelect(t, start, end, h):
    # The ROM pins each offset to teLength comparing unsigned, so -1 is
    # the end of the text: Enchanted Scepters sets it so before each key.
    set_select(t, h, start & 0xFFFFFFFF, end & 0xFFFFFFFF)


@trap
def TEActivate(t, h):
    activate(t, h)


@trap
def TEDeactivate(t, h):
    deactivate(t, h)


@trap
def TEIdle(t, h):
    idle(t, h)


@trap
def TEKey(t, ch, h):
    key(t, h, ch)


@trap
def TEClick(t, pt, extend, h):
    click(t, h, pt, extend)


@trap
def TEUpdate(t, r, h):
    update(t, h, t.rect(r))


@trap
def TETextBox(t, p, length, r, just):
    text_box(t, t.mem.read(p, max(length, 0)), t.rect(r), just)


@trap
def TECut(t, h):
    cut(t, h)


@trap
def TECopy(t, h):
    copy(t, h)


@trap
def TEPaste(t, h):
    paste(t, h)


@trap
def TEDelete(t, h):
    delete(t, h)


@trap
def TEInsert(t, p, length, h):
    insert(t, h, t.mem.read(p, max(length, 0)))


@trap
def TESetAlignment(t, just, h):
    ed = edit(t, h)
    if ed is not None:
        ed.set_just(just)


TESetJust = TESetAlignment


@trap
def TEScroll(t, dh, dv, h):
    ed = edit(t, h)
    if ed is not None:
        ed.scroll(dh, dv)


@trap
def TEPinScroll(t, dh, dv, h):
    ed = edit(t, h)
    if ed is not None:
        ed.pin_scroll(dh, dv)


@trap
def TESelView(t, h):
    ed = edit(t, h)
    if ed is not None:
        ed.sel_view()


@trap
def TEAutoView(t, auto, h):
    ed = edit(t, h)
    if ed is not None:
        ed.feature(teFAutoScroll, teBitSet if auto else teBitClear)


@trap
def TEFeatureFlag(t, feature, action, h):
    ed = edit(t, h)
    return ed.feature(feature, action) if ed is not None else teBitClear


@trap
def TEGetOffset(t, pt, h):
    ed = edit(t, h)
    return ed.offset_at(pt) if ed is not None else 0


@trap
def TEGetPoint(t, offset, h):
    ed = edit(t, h)
    if ed is None:
        return 0
    pt = ed.point_of(offset)
    return ((pt.v & 0xFFFF) << 16) | (pt.h & 0xFFFF)


@trap
def TEGetHeight(t, end_line, start_line, h):
    ed = edit(t, h)
    return ed.height(end_line, start_line) if ed is not None else 0


@trap
def TECustomHook(t, which, addr_p, h):
    ed = edit(t, h)
    if ed is not None and addr_p:
        t.mem.w32(addr_p, ed.custom_hook(which, t.mem.r32(addr_p)))


# =========================================================================
# Styled TextEdit
# =========================================================================
@trap
def TEGetStyleHandle(t, h):
    ed = edit(t, h)
    return ed.te.style_handle if ed is not None and ed.styled else 0


@trap
def TESetStyleHandle(t, style_h, h):
    ed = edit(t, h)
    if ed is not None and ed.styled:
        ed.te.style_handle = style_h


GetStylHandle, SetStylHandle = TEGetStyleHandle, TESetStyleHandle


@trap
def TEGetStyleScrapHandle(t, h):
    """GetStylScrap: a new StScrpHandle with the selection's styles."""
    ed = edit(t, h)
    if ed is None or not ed.styled:
        return 0
    data = ed.style_scrap(*ed.selection)
    sh = t.process.heap.new_handle(len(data))
    if sh:
        t.mem.write(t.process.heap.deref(sh), data)
    t.set_mem_err(noErr if sh else memFullErr)
    return sh


GetStylScrap = TEGetStyleScrapHandle


def _style_scrap(t, sh: int):
    sh &= t.mem.system.addr_mask
    p = t.mem.r32(sh) & t.mem.system.addr_mask if sh else 0
    if not p:
        return []
    return read_style_scrap(t.mem.read(p, t.system.heap_of(sh).handle_size(sh)))


@trap
def TESetStyle(t, mode, style_p, redraw, h):
    ed = edit(t, h)
    if ed is not None:
        ed.set_style(mode, Style.read(t.mem, style_p), bool(redraw))


@trap
def TEReplaceStyle(t, mode, old_p, new_p, redraw, h):
    ed = edit(t, h)
    if ed is not None:
        ed.replace_style(mode, Style.read(t.mem, old_p), Style.read(t.mem, new_p), bool(redraw))


@trap
def TEGetStyle(t, offset, style_p, line_height_p, ascent_p, h):
    ed = edit(t, h)
    if ed is None:
        return
    style, height, ascent = ed.get_style(offset)
    if style_p:
        style.write(t.mem, style_p)
    t.w16(line_height_p, height)
    t.w16(ascent_p, ascent)


@trap
def TEContinuousStyle(t, mode_p, style_p, h):
    ed = edit(t, h)
    if ed is None:
        return False
    ok, mode, style = ed.continuous_style(t.mem.r16s(mode_p))
    t.w16(mode_p, mode)
    if style_p:
        style.write(t.mem, style_p)
    return ok


@trap
def TENumStyles(t, start, end, h):
    ed = edit(t, h)
    return ed.num_styles(start, end) if ed is not None else 0


@trap
def TEStyleInsert(t, p, length, style_h, h):
    ed = edit(t, h)
    if ed is not None:
        ok = ed.insert(t.mem.read(p, max(length, 0)), _style_scrap(t, style_h) or None)
        t.set_mem_err(noErr if ok else memFullErr)


@trap
def TEUseStyleScrap(t, start, end, style_h, redraw, h):
    ed = edit(t, h)
    if ed is not None:
        ed.use_style_scrap(start, end, _style_scrap(t, style_h), bool(redraw))


@trap
def TEStylePaste(t, h):
    """The desk scrap's text, in its styles ('styl'), replaces the
    selection; TextEdit's scrap if the desk scrap has no text."""
    ed = edit(t, h)
    if ed is None:
        return
    found = glue_scrap.get_scrap(t, TEXT)
    if found is None:
        data, runs = textedit.te_scrap(t.process.world, t.mem), None
    else:
        data = found[1]
        styl = glue_scrap.get_scrap(t, STYL)
        runs = read_style_scrap(styl[1]) if styl is not None else None
    t.set_mem_err(noErr if ed.paste(data, runs or None) else memFullErr)
