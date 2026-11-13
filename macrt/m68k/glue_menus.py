"""
Menu Manager traps: the menu bar, menus and their items, MenuSelect and
pop-up menus, and the System's WDEFs, MDEF and MBDF for 68k code that
calls one itself.  While the user chooses from the host's menus, the
other applications run.
"""

from __future__ import annotations

from ..memory import to_mac
from ..toolbox import constants as k
from ..toolbox import lowmem
from ..toolbox.menus import MenuInfo, MenuItem, parse_menu_items
from ..toolbox.script import smAllScripts, smCurrentScript, smRoman, smSystemScript, script_of
from ..toolbox.types import Rect
from . import glue_desk, mdefs, wdefs
from .dispatch import trap
from .glue_common import _noop
from .glue_controls import _s16, scratch
from .glue_fonts import _unloaded


def _menu_handle(m: MenuInfo | None) -> int:
    return m.handle if m is not None else 0


def _char(code: int) -> str:
    return bytes([code & 0xFF]).decode("mac_roman")


# =========================================================================
# Menu Manager
# =========================================================================
@trap
def InitMenus(t):
    """Clears the menu bar and draws it, empty, as a Mac's does: a game
    with no menus of its own has a bar in the screen buffer to draw over.
    With Color QuickDraw it makes the menu colour table, which Microsoft
    Word 4 reads at MenuCInfo."""
    t.tb.InitMenus()
    t.tb.DrawMenuBar()


@trap
def InitProcMenu(t, mbar_id):
    """The menu bar's definition procedure.  macrt draws the bar and
    tracks the mouse itself, but an application's own MBDF is told about
    each menu as it's pulled down (MenuSelect)."""
    t.process.menus.mbdf_id = mbar_id
    h = t.process.resources.defproc("MBDF", mbar_id)
    t.process.world.w32(lowmem.MBDFHndl, h)
    if _app_mbdf(t):
        _call_mbdf(t, k.mbInit)


def _app_mbdf(t) -> int:
    """The MBDF InitProcMenu named if it's the application's, else 0."""
    h = t.process.world.r32(lowmem.MBDFHndl) & t.mem.system.addr_mask
    return h if h and t.system.defproc_id(h)[0] != "MBDF" else 0


def _call_mbdf(t, msg: int, param1: int = 0, param2: int = 0) -> int:
    h = _app_mbdf(t)
    heap = t.system.heap_of(h)
    if not heap.deref(h):
        t.process.resources.LoadResource(h)
    entry = heap.deref(h)
    if not entry:
        return 0
    state = heap.get_state(h)
    heap.lock(h)
    try:
        return t.call(entry, (("h", t.process.menus.mbdf_id & 7), ("h", msg), ("h", param1),
                              ("l", param2)), "l")
    finally:
        heap.set_state(h, state)


@trap
def NewMenu(t, menu_id, title):
    return _menu_handle(t.tb.NewMenu(menu_id, t.str255(title)))


@trap
def GetMenu(t, menu_id):
    return _menu_handle(t.tb.GetMenu(menu_id))


@trap
def GetNewMBar(t, mbar_id):
    return t.tb.GetNewMBar(mbar_id)


@trap
def GetMenuBar(t):
    return t.tb.GetMenuBar()


@trap
def SetMenuBar(t, h):
    if h:
        t.tb.SetMenuBar(h)


@trap
def DrawMenuBar(t):
    t.tb.DrawMenuBar()


@trap
def InvalMenuBar(t):
    """The bar's to be drawn again, which the host does now."""
    t.tb.DrawMenuBar()


@trap
def ClearMenuBar(t):
    t.tb.ClearMenuBar()


@trap
def GetMenuHandle(t, menu_id):
    return _menu_handle(t.tb.GetMHandle(menu_id))


@trap
def InsertMenu(t, h, before_id):
    m = t.menu(h)
    if m is not None:
        t.tb.InsertMenu(m, before_id)


@trap
def DeleteMenu(t, menu_id):
    t.tb.DeleteMenu(menu_id)


@trap
def DisposeMenu(t, h):
    m = t.menu(h)
    if m is not None:
        t.tb.DisposeMenu(m)


@trap
def AppendMenu(t, h, data):
    m = t.menu(h)
    if m is not None:
        t.tb.AppendMenu(m, t.str255(data))


def _resource_names(t, rtype: int, script: int = smAllScripts) -> list[str]:
    """The names a menu of the resource type lists, sorted, of those in the
    script (or all, or the System's or the current one's: Roman, the only
    one here).  A font menu lists the font families too.  Names that start
    with a period are hidden, as drivers' are among the desk accessories,
    and so are those that start with a percent sign."""
    kinds = [t.ostype(rtype)] + ([b"FOND"] if t.ostype(rtype) == b"FONT" else [])
    if script in (smSystemScript, smCurrentScript):
        script = smRoman
    names = set()
    with _unloaded(t) as rm:
        for kind in kinds:
            for i in range(1, rm.CountResources(kind) + 1):
                h = rm.GetIndResource(kind, i)
                rid, _, name = rm.GetResInfo(h) if h else (0, b"", "")
                family = rid >> 7 if kind == b"FONT" else rid
                if name and name[0] not in ".%" and script in (smAllScripts, script_of(family)):
                    names.add(name)
    return sorted(names, key=str.lower)


@trap
def AppendResMenu(t, h, rtype):
    InsertResMenu(t, h, rtype, 0x7FFF)


@trap
def InsertResMenu(t, h, rtype, after):
    _insert_names(t, h, rtype, after, smAllScripts)


@trap
def InsertFontResMenu(t, h, after, script):
    _insert_names(t, h, int.from_bytes(b"FONT", "big"), after, script)


@trap
def InsertIntlResMenu(t, h, rtype, after, script):
    _insert_names(t, h, rtype, after, script)


def _insert_names(t, h, rtype: int, after: int, script: int) -> None:
    m = t.menu(h)
    if m is None:
        return
    names = _resource_names(t, rtype, script)
    if names:
        items = m.items
        after = max(0, min(after, len(items)))
        new = [MenuItem(name) for name in names]    # as written: no metacharacters
        m.set_items(items[:after] + new + items[after:])
        t.tb.menus_changed(m)


@trap
def CountMItems(t, h):
    m = t.menu(h)
    return m.count() if m is not None else 0


@trap
def EnableItem(t, h, item):
    m = t.menu(h)
    if m is not None:
        t.tb.EnableItem(m, item)


@trap
def DisableItem(t, h, item):
    m = t.menu(h)
    if m is not None:
        t.tb.DisableItem(m, item)


@trap
def CheckItem(t, h, item, checked):
    m = t.menu(h)
    if m is not None:
        t.tb.CheckItem(m, item, bool(checked))


@trap
def SetItemMark(t, h, item, mark):
    m = t.menu(h)
    if m is not None:
        t.tb.SetItemMark(m, item, "" if not mark & 0xFF else _char(mark))


@trap
def GetItemMark(t, h, item, mark_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    t.w16(mark_p, to_mac(it.mark)[0] if it is not None and it.mark else 0)


@trap
def SetMenuItemText(t, h, item, text):
    m = t.menu(h)
    if m is not None:
        t.tb.SetItem(m, item, t.str255(text))


@trap
def GetMenuItemText(t, h, item, text_p):
    m = t.menu(h)
    t.set_str255(text_p, t.tb.GetItem(m, item) if m is not None else "")


@trap
def SetItemStyle(t, h, item, style):
    m = t.menu(h)
    if m is not None:
        t.tb._change_item(m, item, style=style & 0xFF)


@trap
def GetItemStyle(t, h, item, style_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    if style_p:
        t.mem.w8(style_p, it.style if it is not None else 0)


@trap
def SetItemCmd(t, h, item, cmd):
    m = t.menu(h)
    if m is not None:
        t.tb._change_item(m, item, cmd="" if not cmd & 0xFF else _char(cmd))


@trap
def GetItemCmd(t, h, item, cmd_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    t.w16(cmd_p, to_mac(it.cmd)[0] if it is not None and it.cmd else 0)


@trap
def SetItemIcon(t, h, item, icon):
    m = t.menu(h)
    if m is not None:
        t.tb._change_item(m, item, icon=icon)


@trap
def GetItemIcon(t, h, item, icon_p):
    m = t.menu(h)
    it = m.item(item) if m is not None else None
    t.w16(icon_p, it.icon if it is not None else 0)


@trap
def InsertMenuItem(t, h, data, after):
    m = t.menu(h)
    if m is None:
        return
    items = m.items
    after = max(0, min(after, len(items)))
    new = parse_menu_items(t.str255(data))
    m.set_items(items[:after] + new + items[after:])
    t.tb.menus_changed(m)


@trap
def DeleteMenuItem(t, h, item):
    m = t.menu(h)
    if m is None:
        return
    items = m.items
    if 1 <= item <= len(items):
        del items[item - 1]
        m.set_items(items)
        t.tb.menus_changed(m)


@trap
def MenuSelect(t, pt):
    """The host follows the mouse.  An application's own MBDF hears of
    each menu pulled down, and its own MDEFs draw and track theirs, on
    this thread while the host waits (mdefs.Menus)."""
    mbdf = bool(_app_mbdf(t))
    menus = t.process.menus
    if not mbdf and not any(m.own_mdef for m in [*menus, *menus.submenus()]):
        with t.yielding():
            result = t.tb.MenuSelect(pt)
        return glue_desk.menu_picked(t, result)
    shown = []

    def opening(menu_id: int, where: Rect) -> None:
        """As the System's MenuSelect does: restore what the last menu
        covered, then ask where this one goes and save what it covers."""
        _restore_menu(t, shown)
        offset = t.process.menus.offset(menu_id)
        if not offset:
            return
        with scratch(t, 8) as rp:
            t.mem.w_rect(rp, where)
            t.menu_rect = rp
            try:
                r = _call_mbdf(t, k.mbRect, 0, offset) & t.mem.system.addr_mask
                _call_mbdf(t, k.mbSave, offset, r or rp)
            finally:
                t.menu_rect = 0
        shown.append(offset)

    with t.yielding():
        result = t.tb.MenuSelect(pt, mdefs.Menus(t, opening if mbdf else None))
    _restore_menu(t, shown)
    return glue_desk.menu_picked(t, result)


@trap
def PopUpMenuSelect(t, h, top, left, item):
    """A pop-up menu with item at (top, left), global coordinates: what
    MenuSelect returns for the item chosen (Inside Macintosh V-241)."""
    m = t.menu(h)
    if m is None:
        return 0
    with t.yielding():
        return t.tb.PopUpMenuSelect(m, _s16(top), _s16(left), _s16(item),
                                    mdefs.Menus(t, popup=m) if m.own_mdef else None)


@trap
def CalcMenuSize(t, h):
    """An application's own MDEF works out the menu's size (mSizeMsg); a
    menu of the System's MDEF 0 is the size the host pulls it down at."""
    m = t.menu(h)
    if m is not None and m.own_mdef:
        mdefs.calc_size(t, m)
    elif m is not None:
        t.tb.size_menu(m)


def _restore_menu(t, shown: list[int]) -> None:
    if shown:
        shown.clear()
        _call_mbdf(t, k.mbRestore)


@trap
def MenuKey(t, ch):
    return glue_desk.menu_picked(t, t.tb.MenuKey(ch & 0xFF))


@trap
def MenuChoice(t):
    """After a MenuSelect that chose nothing: the menu ID and number of the
    disabled item the mouse was released over, or 0 (Inside Macintosh:
    Macintosh Toolbox Essentials, Menu Manager)."""
    return t.process.menu_choice


@trap
def HiliteMenu(t, menu_id):
    t.tb.HiliteMenu(menu_id)


for _name in ("FlashMenuBar", "SetMenuFlash"):
    trap(_noop(_name))


# =========================================================================
# The System's WDEFs, MDEF and MBDF, for 68k code that calls one itself.
# The host draws the menu bar, so there's little for the MBDF to answer,
# but the arguments come off the stack.
# =========================================================================
def _system_defproc(t, name: str, msg: int) -> int:
    t.unimplemented(f"the System's {name}, message {msg}", 0xAA68)
    return 0


@trap
def SystemWDEF0(t, var, w, msg, param):
    """WDEF 0's code, for an application's own WDEF that passes it the
    messages it doesn't answer, as Excel's do."""
    return wdefs.system_wdef(t, 0, var, w, msg, param)


@trap
def SystemWDEF1(t, var, w, msg, param):
    return wdefs.system_wdef(t, 1, var, w, msg, param)


@trap
def SystemMDEF0(t, msg, menu, rect_p, hit, item_p):
    """MDEF 0's code, for an application's own MDEF that calls it, as
    Theldrow's does once it has set the port's font."""
    mdefs.system_mdef(t, msg, menu, rect_p & t.mem.system.addr_mask, hit, item_p & t.mem.system.addr_mask)


@trap
def SystemMBDF0(t, selector, msg, param1, param2):
    """For an application's MBDF that leaves the work to the System's:
    there's nothing to draw, save or work out here, and mbRect has where
    the menu being pulled down goes."""
    if msg == k.mbRect and t.menu_rect:
        return t.menu_rect
    if msg in (k.mbDraw, k.mbCalc, k.mbInit, k.mbDispose, k.mbHilite, k.mbHeight, k.mbSave,
               k.mbRestore, k.mbSaveAlt, k.mbResetAlt):
        return 0
    return _system_defproc(t, "MBDF 0", msg)
