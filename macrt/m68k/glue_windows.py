"""
Window Manager traps: windows' creation, order, highlighting, titles,
dragging, growing and zooming, update regions, window pictures and the
desktop.  DragWindow, TrackGoAway, TrackBox and GrowWindow let the other
applications run while they follow the mouse.
"""

from __future__ import annotations

from typing import Callable

from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.api import IN_FRONT
from ..toolbox.qdraw import Region, read_rgn
from ..toolbox.quickdraw import GRAY_PAT
from ..toolbox.windows import WindowRecord
from .dispatch import trap
from .glue_common import _noop, _port, _the_port_var
from .glue_controls import drag_outline, kill_controls, scratch
from .glue_pict import DrawPicture
from .glue_quickdraw import _rgn


# =========================================================================
# Window Manager
# =========================================================================
@trap
def InitWindows(t):
    # It opens the Window Manager port, which makes that the current port
    # (IM I: OpenPort), for a program's first call or when the program has
    # thrown the last one away, as Rogue's start does.  Called again, as
    # Disk Copy's licence agreement does after SetPort to its dialog, it
    # leaves thePort be.
    opened = not t.system.wmgr_port_kept()
    if opened:
        t.system.open_wmgr_port()
    t.mem.w8(lowmem.WWExist, 0)
    if opened or not t.process.windows_set_up:
        t.mem.w32(_the_port_var(t), t.mem.r32(lowmem.WMgrPort))
    t.process.windows_set_up = True
    if t.system.model.color:
        # With Color QuickDraw it makes the default auxiliary window and
        # control records (Inside Macintosh V); Microsoft Word 4 reads the
        # control record's colour table from AuxCtlHead.
        from .glue_cport import default_aux_records   # which imports this module
        default_aux_records(t)


def _behind(t, p: int):
    if p == 0xFFFFFFFF:
        return IN_FRONT
    return t.window(p)


@trap
def NewWindow(t, storage, bounds, title, visible, proc_id, behind, go_away, ref_con):
    w = t.tb.NewWindow(t.rect(bounds), t.str255(title), bool(visible), proc_id,
                       _behind(t, behind), bool(go_away), ref_con, storage)
    _front_changed(t)
    return w


@trap
def GetNewWindow(t, window_id, storage, behind):
    w = t.tb.GetNewWindow(window_id, storage, _behind(t, behind))
    _front_changed(t)
    return w


# What's called after a call that may change which window is in front:
# the Palette Manager's (glue_palette), which gives it its colours.
front_changed: list[Callable] = []


def _front_changed(t) -> None:
    for hook in front_changed:
        hook(t)


def _on_window(method: str, reorders: bool = False):
    def call(t, p, *args):
        w = t.window(p)
        if w is not None:
            got = getattr(t.tb, method)(w, *args)
            if reorders:
                _front_changed(t)
            return got
    call.__name__ = method
    return call


for _name in ("SelectWindow", "BringToFront", "ShowWindow", "HideWindow"):
    trap(_on_window(_name, reorders=True))
for _name in ("DrawGrowIcon", "BeginUpdate", "EndUpdate"):
    trap(_on_window(_name))


@trap
def CloseWindow(t, p):
    w = t.window(p)
    if w is not None:
        kill_controls(t, w, erase=False)
        t.tb.CloseWindow(w)
        _front_changed(t)


@trap
def DisposeWindow(t, p):
    w = t.window(p)
    if w is not None:
        kill_controls(t, w, erase=False)
        t.tb.DisposeWindow(w)
        _front_changed(t)


@trap
def HiliteWindow(t, p, hilite):
    w = t.window(p)
    if w is not None:
        t.tb.HiliteWindow(w, bool(hilite))


@trap
def ShowHide(t, p, show):
    w = t.window(p)
    if w is not None:
        t.tb.ShowHide(w, bool(show))


@trap
def SendBehind(t, p, behind):
    w = t.window(p)
    if w is not None:
        t.tb.SendBehind(w, t.window(behind))
        _front_changed(t)


@trap
def FrontWindow(t):
    return t.tb.FrontWindow()


@trap
def FindWindow(t, pt, which_p):
    part, w = t.tb.FindWindow(pt)
    t.w32(which_p, w.addr if w is not None else 0)
    if w is not None and w.window_kind < 0 and part not in (k.inDesk, k.inMenuBar):
        return k.inSysWindow                # a desk accessory's: SystemClick's to deal with
    return part


@trap
def SetWTitle(t, p, title):
    w = t.window(p)
    if w is not None:
        t.tb.SetWTitle(w, t.str255(title))


@trap
def GetWTitle(t, p, title_p):
    w = t.window(p)
    t.set_str255(title_p, w.title if w is not None else "")


@trap
def SetWRefCon(t, p, ref_con):
    w = t.window(p)
    if w is not None:
        w.ref_con = ref_con


@trap
def GetWRefCon(t, p):
    w = t.window(p)
    return w.ref_con if w is not None else 0


@trap
def GetWVariant(t, p):
    w = t.window(p)
    return w.proc_id & 0xF if w is not None else 0


@trap
def MoveWindow(t, p, h, v, front):
    w = t.window(p)
    if w is not None:
        t.tb.MoveWindow(w, h, v, bool(front))


@trap
def SizeWindow(t, p, width, height, update):
    w = t.window(p)
    if w is not None:
        t.tb.SizeWindow(w, width, height, bool(update))


@trap
def ZoomWindow(t, p, part, front):
    w = t.window(p)
    if w is not None:
        t.tb.ZoomWindow(w, part, bool(front))


@trap
def DragWindow(t, p, start, bounds):
    w = t.window(p)
    if w is not None:
        with t.yielding():
            t.tb.DragWindow(w, start, t.rect(bounds) if bounds else None)


def _drag_rgn(t, h, start, limit, slop, axis, action, pat: bytes) -> int:
    """The region's outline, in thePort, follows the mouse until the
    button comes up: how far, the vertical distance in the high word, or
    $80008000 if it came up outside slop."""
    port = _port(t)
    if port is None or not h:
        return 0
    moved = drag_outline(t, port, start, read_rgn(t.mem, h), t.rect(limit), t.rect(slop),
                         axis, action, pat)
    return (moved.v & 0xFFFF) << 16 | moved.h & 0xFFFF


@trap
def DragGrayRgn(t, h, start, limit, slop, axis, action):
    return _drag_rgn(t, h, start, limit, slop, axis, action, GRAY_PAT)


@trap
def DragTheRgn(t, h, start, limit, slop, axis, action):
    """DragGrayRgn in DragPattern's pattern: Sargon III drags its pieces so."""
    return _drag_rgn(t, h, start, limit, slop, axis, action, t.mem.read(lowmem.DragPattern, 8))


@trap
def TrackGoAway(t, p, pt):
    w = t.window(p)
    if w is None:
        return False
    with t.yielding():
        return t.tb.TrackGoAway(w, pt)


@trap
def TrackBox(t, p, pt, part):
    w = t.window(p)
    if w is None:
        return False
    with t.yielding():
        return t.tb.TrackBox(w, pt, part)


@trap
def GrowWindow(t, p, pt, size_rect):
    w = t.window(p)
    if w is None:
        return 0
    with t.yielding():
        return t.tb.GrowWindow(w, pt, t.rect(size_rect))


@trap
def InvalRect(t, r):
    t.tb.InvalRect(t.rect(r))


@trap
def InvalRgn(t, h):
    t.tb.InvalRgn(_rgn(t, h))


@trap
def ValidRect(t, r):
    t.tb.ValidRect(t.rect(r))


@trap
def ValidRgn(t, h):
    t.tb.ValidRgn(_rgn(t, h))


@trap
def CheckUpdate(t, p):
    """Inside Macintosh I, Window Manager: the windows with pictures of
    their own are drawn (window_pictures); the event is for the frontmost
    other window that needs updating, if any."""
    window_pictures(t)
    got, ev = t.process.events.peek(k.event_mask(k.updateEvt), t.tb._next_update)
    if got:
        t.write_event(p, ev)
    return got


def window_pictures(t) -> None:
    """Inside Macintosh I, Window Manager (SetWindowPic, CheckUpdate): a
    window with a picture handle in its record isn't sent update events.
    The Window Manager draws the picture in it, front to back, between
    BeginUpdate and EndUpdate.  Populous II's title is one."""
    while (w := t.tb._next_update(picture=True)) is not None:
        t.tb.BeginUpdate(w)
        saved = t.mem.r32(_the_port_var(t))
        t.mem.w32(_the_port_var(t), w.addr)
        try:
            with scratch(t, 8) as rp:
                t.set_rect(rp, w.port_rect)
                DrawPicture(t, w.window_pic, rp)
        finally:
            t.mem.w32(_the_port_var(t), saved)
            t.tb.EndUpdate(w)


@trap
def GetWindowPic(t, p):
    w = t.window(p)
    return w.window_pic if w is not None else 0


@trap
def SetWindowPic(t, p, pic):
    w = t.window(p)
    if w is not None:
        w.window_pic = pic


for _name in ("DrawNew", "ClipAbove", "SaveOld"):
    trap(_noop(_name))

pCDeskPat = 0x80        # SPMisc2's bit: the desk is DeskCPat


def _desk_brush(t):
    """The desk's pattern: DeskCPat, if SPMisc2 says it's in use, or the
    binary DeskPattern."""
    mem = t.mem
    h = mem.r32(lowmem.DeskCPat) if mem.r8(lowmem.SPMisc2) & pCDeskPat else 0
    return t.tb.qd.desk_brush(mem.read(lowmem.DeskPattern, 8), h & mem.system.addr_mask)


@trap
def PaintOne(t, w, clobbered):
    """With no window, the desk in clobbered is painted in the desk's
    pattern: so a program that has set DeskPattern shows it (Desktop
    Patterns).  The host draws windows' frames itself."""
    if not clobbered:
        return
    if not w:
        t.system.screen.paint_desk(_rgn(t, clobbered), _desk_brush(t))
        return
    rec = t.window(w)
    if rec is not None:
        _paint_one(t, rec, _rgn(t, clobbered))


@trap
def PaintBehind(t, start, clobbered):
    """PaintOne for start and each window behind it (Inside Macintosh I,
    the Window Manager): After Dark, its screen saver stopped, has its
    Control Panel window drawn again so."""
    rgn, behind = _rgn(t, clobbered), False
    for rec in list(t.process.windows):
        behind = behind or rec.addr == start & t.mem.system.addr_mask
        if behind:
            _paint_one(t, rec, rgn)


def _paint_one(t, w: WindowRecord, clobbered: Region) -> None:
    """What of clobbered (global) a window's content area has uncovered
    is erased and added to its update region: each window erased on its
    own, as if PaintWhite were always set (Inside Macintosh V, the Window
    Manager).  A hidden window shows none of it."""
    rgn = clobbered.sect(t.system.screen.exposed(w))
    if rgn.empty:
        return
    b = w.port_bits_bounds
    t.tb.qd.erase_content(w, rgn.offset(b.left, b.top))
    with t.system.lock:
        w.invalidate(rgn)
    t.process.events.wake()


@trap
def SetDeskCPat(t, h):
    """The desk's pattern becomes PixPat h, or with NIL the binary
    DeskPattern, and the desk is painted in it (Inside Macintosh V, the
    Window Manager)."""
    mem = t.mem
    misc = mem.r8(lowmem.SPMisc2)
    mem.w32(lowmem.DeskCPat, h)
    mem.w8(lowmem.SPMisc2, misc | pCDeskPat if h else misc & ~pCDeskPat)
    t.system.screen.paint_desk(None, _desk_brush(t))


@trap
def CalcVis(t, w):
    rec = t.window(w)
    if rec is not None:
        rec.calc_vis()


@trap
def CalcVisBehind(t, start, clobbered):
    """The visRgns of start and the windows behind it; each window's
    pixels are its own here, so those in front take nothing from them."""
    rec = t.window(start)
    behind = False
    for other in t.process.windows:
        behind = behind or other is rec
        if behind:
            other.calc_vis()
