"""
The Palette Manager (Inside Macintosh V): a window's palette
of colours, which the main screen's colour table is given when the
window is in front, and that the window draws in.

    Palette     pmEntries, then 7 private words: 16 bytes; a ColorInfo each entry
    ColorInfo   ciRGB, ciUsage, ciTolerance, ciFlags, ciPrivate: 16 bytes

A 'pltt' resource is an image of the record.  An entry's usage says what
it needs of the screen:

- courteous: nothing; it draws in the nearest colour the screen has;
- tolerant: an index of the screen's table of its own, whose colour is
  no further from it than its tolerance (in red, green or blue), changed
  to its colour if it's further;
- animated: an index reserved for it, which nothing else draws in, so
  that changing the entry's colour (AnimateEntry, AnimatePalette)
  changes the pixels drawn in it at once, and nothing else;
- explicit: the index that's the entry's number, modulo the table's
  size, whatever colour it is; with tolerant or animated (as later
  Palette Managers have it), that's the index changed or reserved.

Later Palette Managers' usages that keep an entry off a device of a
depth (pmInhibitC8 and the rest) are honoured too.  White, index 0, and
black, the last, are never changed.

The table is changed for the window in front, by ActivatePalette, which
the Window Manager calls as windows come and go and change order.  There
is one device to give colours to, the main screen.  When its colours
change (animating isn't a change), each window that asked for colour
updates (SetPalette's cUpdates, NSetPalette's nCUpdates) has the
screen's rectangle added to its update region: the front window if it
wants updates for changes its own palette makes, the others if they
want them for changes another's makes.

A pixel map whose colour table has bit 14 of its ctFlags set has, for
each pixel value, an entry of a palette in its value field "instead of its
rgb values" (Apple's Out of This GWorld sample): what's copied from it is
in the colours of the entries of the current port's palette, as the
screen shows them (_port_colors).
"""

from __future__ import annotations

import struct
from functools import partial

from ..memory import BusError
from ..toolbox.gdevice import GDevice, deref, gdDevType
from ..toolbox.qdraw import rgb_argb
from ..toolbox.windows import WindowRecord
from .dispatch import trap
from .glue_color import RESERVED, _new_colors, _pixmap, _rgb, _set_rgb
from .glue_common import _heap, _port
from .glue_windows import front_changed

pmCourteous, pmDithered, pmTolerant, pmAnimated, pmExplicit = 0, 1, 2, 4, 8
# The usages that keep an entry off a device of a depth: a grey one, a
# colour one (Inside Macintosh VI, Palette Manager).
INHIBIT = {2: (0x0100, 0x0200), 4: (0x0400, 0x0800), 8: (0x1000, 0x2000)}
HEADER = INFO = 16
MAX_ENTRIES = 4095
CI_USAGE, CI_TOLERANCE = 6, 8
APP_PALETTE = -1            # the window that's the application's default palette's
SYSTEM_PALETTE = 0          # where 'pltt' 0 is kept, once looked for
TWO_ENTRIES = -2            # the default made when there's no 'pltt' 0
# NSetPalette's nCUpdates: none, when another's palette changes the
# colours, when the window's own does, both.
pmNoUpdates, pmBkUpdates, pmFgUpdates, pmAllUpdates = 0x8000, 0xA000, 0xC000, 0xE000
FG_UPDATES, BK_UPDATES = 0x4000, 0x2000


# -- the record ---------------------------------------------------------------
def _record(t, h: int) -> int:
    try:
        return deref(t.mem, h)
    except BusError:
        return 0


def _count(t, h: int) -> int:
    p = _record(t, h)
    return max(0, t.mem.r16s(p)) if p else 0


def _info(t, h: int, entry: int) -> int:
    """Where a palette's entry is; 0 if it has none of that number."""
    p = _record(t, h)
    if not p or not 0 <= entry < max(0, t.mem.r16s(p)):
        return 0
    return p + HEADER + INFO * entry


def _infos(t, h: int) -> list[tuple[tuple[int, int, int], int, int]]:
    """Each entry's colour, usage and tolerance."""
    n = _count(t, h)
    data = t.mem.read(_record(t, h) + HEADER, INFO * n) if n else b""
    return [(struct.unpack_from(">HHH", data, INFO * i),
             *struct.unpack_from(">HH", data, INFO * i + CI_USAGE)) for i in range(n)]


def _resize(t, h: int, n: int) -> int:
    """Make a palette n entries long, the private fields cleared; its
    record, or 0 if there isn't room."""
    heap = _heap(t, h)
    if not heap.set_handle_size(h, HEADER + INFO * n):
        return 0
    p = heap.deref(h)
    t.mem.write(p, struct.pack(">h", n) + bytes(HEADER - 2))
    return p


def _fill(t, p: int, colors: list[tuple[int, int, int]], usage: int, tolerance: int) -> None:
    t.mem.write(p + HEADER, b"".join(struct.pack(">HHHHH6x", *rgb, usage & 0xFFFF,
                                                 tolerance & 0xFFFF) for rgb in colors))


def _table_colors(t, ct_h: int) -> list[tuple[int, int, int]]:
    ct = _record(t, ct_h)
    if not ct:
        return []
    n = max(0, min(t.mem.r16s(ct + 6) + 1, MAX_ENTRIES))
    data = t.mem.read(ct + 8, 8 * n)
    return [struct.unpack_from(">HHH", data, 8 * i + 2) for i in range(n)]


def _new_palette(t, n: int) -> int:
    n = max(0, min(n, MAX_ENTRIES))
    heap = t.process.heap
    h = heap.new_handle(HEADER + INFO * n, clear=True)
    if h:
        t.mem.w16(heap.deref(h), n)
    return h


def _get_new_palette(t, palette_id: int) -> int:
    """A palette made from the 'pltt' of that ID, its private fields
    cleared; 0 if there's none."""
    res = t.process.resources.GetResource("pltt", palette_id)
    if not res or not _record(t, res):
        return 0
    data = bytearray(t.tb.contents(res))
    if len(data) < 2:
        return 0
    n = max(0, min(struct.unpack_from(">h", data)[0], MAX_ENTRIES))
    data = data[:HEADER + INFO * n].ljust(HEADER + INFO * n, b"\0")
    data[2:HEADER] = bytes(HEADER - 2)
    for i in range(n):
        at = HEADER + INFO * i + 10
        data[at:at + 6] = bytes(6)
    return t.process.heap.new_handle_with(bytes(data))


# -- which palette is a window's ----------------------------------------------
def _key(p: int, mask: int) -> int:
    return APP_PALETTE if p & 0xFFFFFFFF == 0xFFFFFFFF else p & mask


def _palette_of(t, p: int) -> int:
    """The palette whose colours a window has: its own, else the
    application's default, else the System's, 'pltt' 0, if there is one."""
    palettes = t.process.palettes
    for key in (_key(p, t.mem.system.addr_mask), APP_PALETTE):
        got = palettes.get(key)
        if got is not None and got[0]:
            return got[0]
    if SYSTEM_PALETTE not in palettes:
        palettes[SYSTEM_PALETTE] = (_get_new_palette(t, 0), 0)
    return palettes[SYSTEM_PALETTE][0]


def _default_palette(t) -> int:
    """The application's default palette: the one it set, else 'pltt' 0,
    else one made of two entries, white and black.  \"If the System file
    has no default palette, the Palette Manager creates a special
    two-entry palette (black and white)\" (Inside Macintosh: Advanced Color
    Imaging on the Mac OS, Palette Manager, \"Designating a Default Palette
    for Your Application\").  0 if there isn't room for it."""
    h = _palette_of(t, 0xFFFFFFFF)
    if h:
        return h
    palettes = t.process.palettes
    got = palettes.get(TWO_ENTRIES)
    if got is None:
        h = _new_palette(t, 2)
        if not h:
            return 0
        _fill(t, _record(t, h), [(0xFFFF, 0xFFFF, 0xFFFF), (0, 0, 0)], pmCourteous, 0)
        palettes[TWO_ENTRIES] = got = (h, 0)
    return got[0]


def _front(t) -> None:
    """Which window is in front may have changed: give the screen its
    palette's colours.  (And forget the palettes of windows that have
    gone.)"""
    if t.system.devices is None:
        return
    qd = t.tb.qd
    if qd.palette_colors is None:
        qd.palette_colors = partial(_port_colors, t)
    palettes = t.process.palettes
    windows = {w.addr & t.mem.system.addr_mask for w in t.process.windows}
    for key in [k for k in palettes if k > 0 and k not in windows]:
        del palettes[key]
    w = t.process.front_window()
    if w is not None:
        _activate(t, _palette_of(t, w.addr))


front_changed.append(_front)


def new_window(t, w: WindowRecord | None, palette_id: int | None = None) -> None:
    """A window has been made, in front if it's visible: with
    GetNewCWindow, its palette is the 'pltt' of its ID, if there is one."""
    if w is None or t.system.devices is None:
        return
    if palette_id is not None:
        h = _get_new_palette(t, palette_id)
        if h:
            t.process.palettes[w.addr & t.mem.system.addr_mask] = (h, FG_UPDATES | BK_UPDATES)
    _front(t)


def _port_colors(t, port) -> list[int] | None:
    """The colours of the entries of the palette a port draws in, as the
    main screen shows them, 0xAARRGGBB by entry: an explicit entry's, its
    index's; an animated one's, the index reserved for it, if it has one;
    any other's, the nearest the screen has."""
    devices = t.system.devices
    h = _palette_of(t, port.addr) if devices is not None else 0
    if not h:
        return None
    table = devices.table()
    owners = {owner: i for i, owner in devices.reserved.items()}
    out = []
    for e, (rgb, usage, _) in enumerate(_infos(t, h)):
        i = e % len(table) if table and usage & pmExplicit else owners.get((h, e))
        out.append(table[i] if i is not None and i < len(table) else devices.nearest(rgb_argb(rgb)))
    return out


# -- the screen's colours ------------------------------------------------------
def _distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


def _main_table(t) -> tuple[int, int]:
    """The main screen's colour table, and how many entries it has that
    its pixels can be; (0, 0) if it has none to change."""
    devices = t.system.devices
    if devices is None or devices.depth > 8:
        return 0, 0
    pm = _pixmap(t, devices.main)
    ct = _record(t, pm.pm_table) if pm is not None else 0
    if not ct:
        return 0, 0
    return ct, max(0, min(t.mem.r16s(ct + 6) + 1, 1 << devices.depth))


def _activate(t, h: int) -> None:
    """Give the main screen the colours palette h asks for: first its
    animated entries their indexes, then each tolerant entry one of its
    own, the best match of those left, changed if that isn't within the
    entry's tolerance.  Other palettes' reserved indexes are taken only
    when there's nothing else, and those ReserveEntry has never."""
    devices = t.system.devices
    ct, size = _main_table(t)
    if not ct or size < 3 or not _record(t, h):
        return
    last = size - 1
    specs = [struct.unpack(">HHHH", t.mem.read(ct + 8 + 8 * i, 8)) for i in range(size)]
    colors = [spec[1:] for spec in specs]
    gd = GDevice(t.mem, _record(t, devices.main))
    inhibit = INHIBIT.get(devices.depth, (0, 0))[gd.flags >> gdDevType & 1]
    infos = [(e, rgb, usage, tolerance) for e, (rgb, usage, tolerance) in enumerate(_infos(t, h))
             if not usage & inhibit]
    was = {e: i for i, (ph, e) in devices.reserved.items() if ph == h}
    others = {i: owner for i, owner in devices.reserved.items() if owner[0] != h}
    explicit = {e % size for e, _, usage, _ in infos
                if usage & pmExplicit and usage & (pmAnimated | pmTolerant)}
    used: set[int] = set(devices.held)
    reserved: dict[int, int] = {}
    changed: set[int] = set()

    def free(order) -> list[int]:
        """The indexes no entry has, in order: other palettes' last."""
        left = [i for i in order if i not in used and i not in explicit]
        return [i for i in left if i not in others] + [i for i in left if i in others]

    def take(i: int, rgb: tuple[int, int, int] | None) -> None:
        used.add(i)
        others.pop(i, None)
        if rgb is not None and colors[i] != rgb:
            colors[i] = rgb
            changed.add(i)

    for e, rgb, usage, _ in infos:
        if not usage & pmAnimated:
            continue
        if usage & pmExplicit:
            i = e % size
            if i in (0, last) or i in used:
                continue
        else:
            i = was.get(e)
            if i is None or i in used or i in explicit or i in (0, last):
                left = free(range(last - 1, 0, -1))
                if not left:
                    continue
                i = left[0]
        reserved[i] = e
        take(i, rgb)
    for e, rgb, usage, tolerance in infos:
        if usage & pmAnimated or not usage & pmTolerant:
            continue
        if usage & pmExplicit:
            i = e % size
            if i not in used:
                take(i, rgb if i not in (0, last) and _distance(colors[i], rgb) > tolerance
                     else None)
            continue
        left = free(range(size))
        if not left:
            continue
        i = min(left, key=lambda i: _distance(colors[i], rgb))
        if _distance(colors[i], rgb) <= tolerance:
            take(i, None)
            continue
        left = [i for i in left if i not in (0, last)]
        if left:
            take(min(left, key=lambda i: _distance(colors[i], rgb)), rgb)
    for i in changed:
        t.mem.write(ct + 8 + 8 * i + 2, struct.pack(">HHH", *colors[i]))
    now = dict(others)
    now.update({i: (h, e) for i, e in reserved.items()})
    _reserve(t, now, changed)
    if changed:
        _color_updates(t)


def _color_updates(t) -> None:
    """The main screen's colours have changed: the windows that want
    updates for it get them, for what of them is on the screen."""
    devices = t.system.devices
    bounds = GDevice(t.mem, _record(t, devices.main)).rect
    front = t.process.front_window()
    for w in t.process.windows:
        got = t.process.palettes.get(w.addr & t.mem.system.addr_mask)
        if got is None or not w.visible:
            continue
        if got[1] & (FG_UPDATES if w is front else BK_UPDATES):
            w.invalidate(bounds)


def _reserve(t, reserved: dict[int, tuple[int, int]], changed=()) -> None:
    """The main screen's indexes reserved for animation are now these, and
    the colours of those changed have been changed: mark them in its
    table, and show the screen in its new colours."""
    devices = t.system.devices
    ct, size = _main_table(t)
    if not ct or not changed and reserved == devices.reserved:
        return
    devices.reserved = reserved
    for i in range(size):
        value = t.mem.r16(ct + 8 + 8 * i)
        want = value & ~RESERVED | (RESERVED if i in reserved or i in devices.held else 0)
        if want != value:
            t.mem.w16(ct + 8 + 8 * i, want)
    if changed:
        _new_colors(t, devices.main, ct)
    else:
        devices.changed()
        t.system.screen.look()


def _release(t, h: int) -> None:
    """A palette's animated entries give up their indexes."""
    devices = t.system.devices
    if devices is not None and any(ph == h for ph, _ in devices.reserved.values()):
        _reserve(t, {i: owner for i, owner in devices.reserved.items() if owner[0] != h})


def _animate(t, h: int, colors: dict[int, tuple[int, int, int]]) -> None:
    """Animated entries of a palette have new colours: the indexes they
    have reserved get them at once.  It's no change to the colours
    anything else draws in, so the table keeps its seed."""
    devices = t.system.devices
    ct, size = _main_table(t)
    if not ct:
        return
    done = False
    for i, (ph, e) in devices.reserved.items():
        if ph == h and e in colors and i < size:
            t.mem.write(ct + 8 + 8 * i + 2, struct.pack(">HHH", *colors[e]))
            done = True
    if done:
        devices.changed()
        t.system.screen.look()


# =========================================================================
# The traps
# =========================================================================
@trap
def InitPalettes(t):
    pass


@trap
def NewPalette(t, entries, ct_h, usage, tolerance):
    """A palette of so many entries: the colour table's colours, as many as
    it has, and black for the rest."""
    h = _new_palette(t, entries)
    if h:
        n = _count(t, h)
        colors = _table_colors(t, ct_h)[:n]
        _fill(t, _record(t, h), colors + [(0, 0, 0)] * (n - len(colors)), usage, tolerance)
    return h


@trap
def GetNewPalette(t, palette_id):
    return _get_new_palette(t, palette_id)


@trap
def DisposePalette(t, h):
    if not h:
        return
    _release(t, h)
    palettes = t.process.palettes
    for key in [k for k, (ph, _) in palettes.items() if ph == h]:
        del palettes[key]
    _heap(t, h).dispose_handle(h)


@trap
def ActivatePalette(t, p):
    """If the window is in front, the screen gets its palette's colours."""
    w = t.window(p)
    if w is not None and t.system.devices is not None and w is t.process.front_window():
        _activate(t, _palette_of(t, w.addr))


def _updates(n_c_updates: int) -> int:
    """Which colour changes a window wants updates for, by NSetPalette's
    nCUpdates or SetPalette's Boolean, which is in the word's high byte
    (the low one is whatever was there): TRUE is all of them."""
    n = n_c_updates & 0xFFFF
    if n & 0x8000:
        return n & (FG_UPDATES | BK_UPDATES)
    return FG_UPDATES | BK_UPDATES if n & 0xFF00 else 0


@trap
def NSetPalette(t, p, h, n_c_updates):
    """SetPalette's too, the same trap."""
    t.process.palettes[_key(p, t.mem.system.addr_mask)] = (h & t.mem.system.addr_mask, _updates(n_c_updates))
    _front(t)


@trap
def GetPalette(t, p):
    """A colour window's palette, or the application's default, which
    there always is one of; NIL for a window that has none of its own, or
    isn't a colour window."""
    key = _key(p, t.mem.system.addr_mask)
    if key == APP_PALETTE:
        return _default_palette(t)
    w = t.window(p)
    if w is None or not w.is_color:
        return 0
    got = t.process.palettes.get(key)
    return got[0] if got else 0


def _entry_index(t, h: int, entry: int, usage: int) -> int | None:
    """The main screen's index that a palette's entry has to itself: an
    explicit entry's number, modulo the table's size; an animated one's
    reserved index, if it still has it.  None for any other."""
    ct, size = _main_table(t)
    if not ct:
        return None
    if usage & pmExplicit:
        return entry % size
    if usage & pmAnimated:
        return next((i for i, owner in t.system.devices.reserved.items() if owner == (h, entry)),
                    None)
    return None


def _pm_color(t, entry: int, fore: bool) -> None:
    """PmForeColor's or PmBackColor's: a courteous or tolerant entry's
    colour, as RGBForeColor would set it; an animated one's reserved
    index (if it still has it); an explicit one's index, whatever colour
    the screen has there."""
    port = _port(t)
    if port is None:
        return
    h = _palette_of(t, port.addr)
    at = _info(t, h, entry)
    if not at:
        return
    rgb, usage = _rgb(t, at), t.mem.r16(at + CI_USAGE)
    index = _entry_index(t, h, entry, usage) if port.is_color else None
    if index is not None and usage & pmExplicit:
        ct, _ = _main_table(t)
        rgb = struct.unpack(">HHH", t.mem.read(ct + 8 + 8 * index + 2, 6))
    if index is None:
        t.tb.qd.set_rgb_color(port, fore, rgb)
    elif fore:
        port.rgb_fg_color, port.fg_color = rgb, index
    else:
        port.rgb_bk_color, port.bk_color = rgb, index


@trap
def PmForeColor(t, entry):
    _pm_color(t, entry, True)


@trap
def PmBackColor(t, entry):
    _pm_color(t, entry, False)


@trap
def Entry2Index(t, entry):
    """The current device's index for an entry of the current port's
    palette (Inside Macintosh: Advanced Color Imaging, Palette Manager):
    the one it has to itself, else the index of the colour nearest the
    entry's, as Color2Index finds it; 0 if the palette has no such entry."""
    port = _port(t)
    h = _palette_of(t, port.addr) if port is not None else 0
    at = _info(t, h, entry)
    if not at:
        return 0
    index = _entry_index(t, h, entry, t.mem.r16(at + CI_USAGE))
    return index if index is not None else t.tb.qd.color_index(_rgb(t, at))


@trap
def AnimateEntry(t, p, entry, rgb_p):
    h = _palette_of(t, p)
    at = _info(t, h, entry)
    if not at:
        return
    rgb = _rgb(t, rgb_p)
    _set_rgb(t, at, rgb)
    if t.mem.r16(at + CI_USAGE) & pmAnimated:
        _animate(t, h, {entry: rgb})


@trap
def AnimatePalette(t, p, ct_h, src_index, dst_entry, dst_length):
    """Entries from dst_entry on get colours from the table, from
    src_index on: as many as there are of both."""
    h = _palette_of(t, p)
    colors = _table_colors(t, ct_h)
    animated = {}
    for k in range(max(0, dst_length)):
        s, e = src_index + k, dst_entry + k
        at = _info(t, h, e)
        if not 0 <= s < len(colors) or not at:
            break
        _set_rgb(t, at, colors[s])
        if t.mem.r16(at + CI_USAGE) & pmAnimated:
            animated[e] = colors[s]
    if animated:
        _animate(t, h, animated)


@trap
def GetEntryColor(t, h, entry, rgb_p):
    at = _info(t, h, entry)
    if at:
        _set_rgb(t, rgb_p, _rgb(t, at))


@trap
def SetEntryColor(t, h, entry, rgb_p):
    """The entry's new colour, which the screen gets at the next
    ActivatePalette."""
    at = _info(t, h, entry)
    if at:
        _set_rgb(t, at, _rgb(t, rgb_p))


@trap
def GetEntryUsage(t, h, entry, usage_p, tolerance_p):
    at = _info(t, h, entry)
    if at:
        t.w16(usage_p, t.mem.r16(at + CI_USAGE))
        t.w16(tolerance_p, t.mem.r16(at + CI_TOLERANCE))


@trap
def SetEntryUsage(t, h, entry, usage, tolerance):
    """-1 for either leaves it as it is."""
    at = _info(t, h, entry)
    if not at:
        return
    if usage & 0xFFFF != 0xFFFF:
        t.mem.w16(at + CI_USAGE, usage & 0xFFFF)
    if tolerance & 0xFFFF != 0xFFFF:
        t.mem.w16(at + CI_TOLERANCE, tolerance & 0xFFFF)


@trap
def CTab2Palette(t, ct_h, h, usage, tolerance):
    """The palette gets the table's colours, and as many entries as it
    has; its animated entries give up their indexes first."""
    if not ct_h or not h or not _record(t, ct_h) or not _record(t, h):
        return
    colors = _table_colors(t, ct_h)
    _release(t, h)
    p = _resize(t, h, len(colors))
    if p:
        _fill(t, p, colors, usage, tolerance)


@trap
def Palette2CTab(t, h, ct_h):
    """The table gets the palette's colours, and as many entries as it has."""
    if not ct_h or not h or not _record(t, ct_h) or not _record(t, h):
        return
    colors = [rgb for rgb, _, _ in _infos(t, h)]
    heap = _heap(t, ct_h)
    if not heap.set_handle_size(ct_h, 8 + 8 * len(colors)):
        return
    devices = t.system.devices
    seed = devices.next_seed() if devices is not None else 0
    t.mem.write(heap.deref(ct_h), struct.pack(">lHh", seed, 0, len(colors) - 1) + b"".join(
        struct.pack(">HHHH", i, *rgb) for i, rgb in enumerate(colors)))


@trap
def ResizePalette(t, h, size):
    """The palette has size entries: those added at the end courteous and
    black, and those taken off the end disposed of "safely" (Inside
    Macintosh: Advanced Color Imaging, Palette Manager), giving up the
    indexes they had reserved."""
    if not h or not _record(t, h) or size < 0:
        return
    n, size = _count(t, h), min(size, MAX_ENTRIES)
    if size == n:
        return
    kept = t.mem.read(_record(t, h) + HEADER, INFO * min(n, size))
    p = _resize(t, h, size)
    if not p:
        return
    t.mem.write(p + HEADER, kept)
    if size > n:
        _fill(t, p + INFO * n, [(0, 0, 0)] * (size - n), pmCourteous, 0)
    devices = t.system.devices
    if devices is not None and any(ph == h and e >= size for ph, e in devices.reserved.values()):
        _reserve(t, {i: (ph, e) for i, (ph, e) in devices.reserved.items()
                     if ph != h or e < size})


@trap
def CopyPalette(t, src, dst, src_entry, dst_entry, dst_length):
    """Entries from dst_entry on get the colours, usages and tolerances of
    the source's from src_entry on, the destination made longer if it
    needs to be (Inside Macintosh VI, Palette Manager)."""
    if not src or not dst or not _record(t, src) or not _record(t, dst) or dst_length <= 0:
        return
    if src_entry < 0 or dst_entry < 0:
        return
    infos = _infos(t, src)[src_entry:src_entry + dst_length]
    if not infos:
        return
    n = _count(t, dst)
    if dst_entry + len(infos) > n:
        old = t.mem.read(_record(t, dst) + HEADER, INFO * n)
        p = _resize(t, dst, min(dst_entry + len(infos), MAX_ENTRIES))
        if not p:
            return
        t.mem.write(p + HEADER, old)
    for k, (rgb, usage, tolerance) in enumerate(infos):
        at = _info(t, dst, dst_entry + k)
        if at:
            t.mem.write(at, struct.pack(">HHHHH6x", *rgb, usage, tolerance))
