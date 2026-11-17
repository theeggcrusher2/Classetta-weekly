"""
The Color Menu Manager's menu colour table (Inside Macintosh V, the Menu
Manager): the colours MenuCInfo's table gives the menu bar, the titles
and the items, which the host draws them in (toolbox.menus has the
table).  GetMenu, and so GetNewMBar, adds the entries of the 'mctb' with
the menu's ID on a Mac with Color QuickDraw.

    MCEntry     mctID, mctItem, mctRGB1 to 4, mctReserved: 30 bytes
    mctb        a count, then the entries
"""

from __future__ import annotations

from ..toolbox.menus import (MCT_ENTRY, mct_entries, mct_key, mctAllItems, menu_color_table,
                             set_mct_entries, write_mct)
from .dispatch import trap
from .glue_os import _zone


def _table(t, make: bool = False) -> int:
    p = t.process
    return menu_color_table(t.system, p.world, p.heap if make else None)


@trap
def GetMCEntry(t, menu_id, item):
    """The entry for the menu's item (0, its title; menu 0, the bar), in
    the table, or NIL."""
    h = _table(t)
    if not h:
        return 0
    for i, e in enumerate(mct_entries(t.system, h)):
        if mct_key(e) == (menu_id, item):
            return (t.mem.r32(h) & t.mem.system.addr_mask) + i * MCT_ENTRY
    return 0


@trap
def SetMCEntries(t, count, entries):
    """Each entry in the array takes the place of the table's for its
    menu and item, or is added."""
    p = t.process
    array = t.mem.read(entries & t.mem.system.addr_mask, max(count, 0) * MCT_ENTRY) if count > 0 else b""
    set_mct_entries(t.system, p.world, p.heap,
                    [array[i:i + MCT_ENTRY] for i in range(0, len(array), MCT_ENTRY)])


@trap
def DeleteMCEntries(t, menu_id, item):
    """DelMCEntries: the menu's item's entry, or with mctAllItems all the
    menu's."""
    h = _table(t)
    if not h:
        return
    kept = [e for e in mct_entries(t.system, h)
            if mct_key(e)[0] != menu_id or item not in (mctAllItems, mct_key(e)[1])]
    write_mct(t.system, h, kept)


@trap
def GetMCInfo(t):
    """A copy of the table, in the current heap."""
    heap = _zone(t, False)
    copy = heap.new_handle(0)
    if copy and not write_mct(t.system, copy, mct_entries(t.system, _table(t))):
        heap.dispose_handle(copy)
        return 0
    return copy


@trap
def SetMCInfo(t, h):
    """The table becomes a copy of h."""
    table = _table(t, make=True)
    if table:
        write_mct(t.system, table, mct_entries(t.system, h))


@trap
def DisposeMCInfo(t, h):
    """DispMCInfo: a table GetMCInfo gave."""
    h &= t.mem.system.addr_mask
    if h:
        t.system.heap_of(h).dispose_handle(h)
