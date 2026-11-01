"""
Colour calls, and the Color Manager's and graphics devices' (Inside
Macintosh V-119, V-133).

On a Mac II (macrt's --model ii) there's Color QuickDraw, and a device,
the main screen's, whose colour table the indexes are of.  On a
black-and-white Mac, programs that run on both kinds call some of these
whether or not there's Color QuickDraw; there an RGB colour is the
classic colour (black or white) it would draw as on a 1-bit screen, and
the device is 1-bit: index 0 white, 1 black.

    ColorSpec   value, then red, green, blue: 8 bytes
"""

from __future__ import annotations

import struct
from functools import partial

from ..memory import ADDRESS_MASK
from ..toolbox import lowmem
from ..toolbox.color import color_rgb, highlight_table, standard_table
from ..toolbox.constants import blackColor, whiteColor
from ..toolbox.gdevice import (DEFAULT_MODE, DEPTH_MODES, ITAB_RES, GDevice, PixMap, deref,
                               gdDevType, make_itable, read_table)
from ..toolbox.pixels import value_rgb
from ..toolbox.qdraw import port_rgb
from ..toolbox.types import Rect
from .dispatch import trap
from .glue_controls import scratch
from .glue_common import _port
from .glue_os import DisposeHandle

HiliteRGB = lowmem.HiliteRGB    # the highlight colour, an RGBColor
# QDError's results, as Imaging With QuickDraw lists them
noErr, cNoMemErr, cRangeErr, cProtectErr, cResErr = 0, -152, -153, -154, -156
# A device's colour table's ColorSpec value: the entry protected, reserved
# (the low byte then the gdID of the device that reserved it).
PROTECTED, RESERVED = 0x8000, 0x4000


def _rgb(t, p: int) -> tuple[int, int, int]:
    return t.mem.r16(p), t.mem.r16(p + 2), t.mem.r16(p + 4)


def _set_rgb(t, p: int, rgb: tuple[int, int, int]) -> None:
    for i, v in enumerate(rgb):
        t.mem.w16(p + 2 * i, v)


def _argb(t, p: int) -> int:
    """An RGBColor as 0xAARRGGBB."""
    r, g, b = _rgb(t, p)
    return 0xFF000000 | (r >> 8) << 16 | (g >> 8) << 8 | b >> 8


def _set_argb(t, p: int, argb: int) -> None:
    _set_rgb(t, p, tuple((argb >> s & 0xFF) * 0x101 for s in (16, 8, 0)))


def _qd_error(t, err: int) -> None:
    t.process.qd_error = err


# -- graphics devices ---------------------------------------------------------
def _device(t) -> int:
    """The current device, TheGDevice's."""
    return t.mem.r32(lowmem.TheGDevice) if t.system.devices is not None else 0


def _gd(t, gdh: int) -> GDevice | None:
    p = deref(t.mem, gdh)
    return GDevice(t.mem, p) if p else None


def _pixmap(t, gdh: int) -> PixMap | None:
    gd = _gd(t, gdh)
    p = deref(t.mem, gd.pmap) if gd is not None else 0
    return PixMap(t.mem, p) if p else None


def _colors(t) -> tuple[int, list[int]]:
    return t.tb.qd.device_colors()


@trap
def GetGDevice(t):
    return _device(t)


@trap
def SetGDevice(t, gdh):
    if t.system.devices is not None:
        t.mem.w32(lowmem.TheGDevice, gdh)


@trap
def GetMainDevice(t):
    return t.mem.r32(lowmem.MainDevice) if t.system.devices is not None else 0


@trap
def GetDeviceList(t):
    return t.mem.r32(lowmem.DeviceList) if t.system.devices is not None else 0


@trap
def GetNextDevice(t, gdh):
    gd = _gd(t, gdh)
    return gd.next_gd if gd is not None else 0


@trap
def GetMaxDevice(t, p):
    """The deepest device the global rectangle reaches: the main screen,
    if it reaches it."""
    devices = t.system.devices
    if devices is None:
        return 0
    r, gd = t.mem.r_rect(p), _gd(t, devices.main)
    screen = gd.rect
    return devices.main if (r.top < screen.bottom and screen.top < r.bottom
                            and r.left < screen.right and screen.left < r.right) else 0


@trap
def TestDeviceAttribute(t, gdh, attribute):
    gd = _gd(t, gdh)
    return gd is not None and bool(gd.flags >> (attribute & 15) & 1)


@trap
def DeviceLoop(t, rgn, proc, user_data, flags):
    """Call proc(depth, deviceFlags, targetDevice, userData) for each
    screen device drawingRgn reaches, to draw what's there as suits the
    device (Imaging With QuickDraw, Graphics Devices).  There's one
    screen: proc is called once for it, unless the region is empty; on
    a black-and-white Mac with no device, for a 1-bit one, NIL."""
    proc &= ADDRESS_MASK
    if not proc:
        return
    rgn &= ADDRESS_MASK
    if rgn and not flags & 4:                   # not allDevices
        p = deref(t.mem, rgn)
        if p and t.mem.r_rect(p + 2).empty:
            return
    devices = t.system.devices
    if devices is None:
        depth, gd_flags, gdh = 1, 0, 0
    else:
        gdh = devices.main
        gd_flags = _gd(t, gdh).flags
        depth = _pixmap(t, gdh).pixel_size
    t.call(proc, (("h", depth), ("h", gd_flags), ("l", gdh), ("l", user_data)))


@trap
def SetDeviceAttribute(t, gdh, attribute, value):
    gd = _gd(t, gdh)
    if gd is not None:
        bit = 1 << (attribute & 15)
        gd.flags = (gd.flags | bit) if value else (gd.flags & ~bit)


@trap
def NewGDevice(t, ref_num, mode):
    """A device, and its pixel map, colour table and inverse table, in
    the System heap, not in the device list.  With a mode, set up as
    InitGDevice would for a device of that mode's depth with no pixels
    of its own; with -1, left for the program to fill in."""
    devices = t.system.devices
    if devices is None:
        return 0
    depths = {m: d for d, m in DEPTH_MODES.items()}
    depth = depths.get(mode - DEFAULT_MODE, 1) if mode != -1 else 1
    gdh = devices.new_device(Rect(0, 0, 0, 0), 0, 0, depth)
    gd = _gd(t, gdh)
    gd.ref_num = ref_num
    gd.mode = mode
    gd.flags = 1 << gdDevType if mode not in (-1, DEFAULT_MODE) else 0
    return gdh


@trap
def DisposeGDevice(t, gdh):
    gd = _gd(t, gdh)
    devices = t.system.devices
    if gd is None or devices is None or gdh & ADDRESS_MASK == devices.main & ADDRESS_MASK:
        return
    pm = _pixmap(t, gdh)
    heap = t.system.sys_heap
    for h in ((pm.pm_table if pm is not None else 0), gd.pmap, gd.i_table, gdh):
        if h and heap.is_handle(h & ADDRESS_MASK):
            heap.dispose_handle(h & ADDRESS_MASK)


# -- the colour table ---------------------------------------------------------
@trap
def GetCTSeed(t):
    devices = t.system.devices
    return devices.next_seed() if devices is not None else 0


@trap
def SetEntries(t, start, count, p):
    """Change entries of the current device's colour table: count + 1 of
    them from start, or with start -1, those the ColorSpecs' values name.
    The table gets a new seed, and the device a new inverse table; the
    screen's pixels show the new colours."""
    devices = t.system.devices
    pm = _pixmap(t, _device(t)) if devices is not None else None
    ct = deref(t.mem, pm.pm_table) if pm is not None else 0
    if not ct:
        return _qd_error(t, cRangeErr)
    size = t.mem.r16s(ct + 6) + 1
    specs = [struct.unpack(">HHHH", t.mem.read(p + 8 * i, 8)) for i in range(max(count, -1) + 1)]
    places = [(v if start < 0 else start + i) for i, (v, *_) in enumerate(specs)]
    if any(not 0 <= at < size for at in places):
        return _qd_error(t, cRangeErr)
    if any(_kept(t, ct, at) for at in places):
        return _qd_error(t, cProtectErr)
    for at, (_, r, g, b) in zip(places, specs):
        t.mem.write(ct + 8 + 8 * at + 2, struct.pack(">HHH", r, g, b))
    _new_colors(t, _device(t), ct)
    _qd_error(t, noErr)


def _kept(t, ct: int, at: int) -> bool:
    """Whether SetEntries is to leave the current device's entry at as it
    is: protected, or reserved by another device's gdID.  (One the Palette
    Manager has for animation it changes as ever.)"""
    value = t.mem.r16(ct + 8 + 8 * at)
    if value & PROTECTED:
        return True
    if not value & RESERVED:
        return False
    gdh, devices = _device(t), t.system.devices
    if gdh & ADDRESS_MASK == devices.main & ADDRESS_MASK and at in devices.reserved:
        return False
    return value & 0xFF != _gd(t, gdh).id & 0xFF


def _entry(t, index: int) -> int:
    """Where the current device's colour table's entry index is: 0 if
    there's no such entry."""
    gdh = _device(t)
    pm = _pixmap(t, gdh) if gdh else None
    ct = deref(t.mem, pm.pm_table) if pm is not None else 0
    return ct + 8 + 8 * index if ct and 0 <= index <= t.mem.r16s(ct + 6) else 0


@trap
def ProtectEntry(t, index, protect):
    """Keep SetEntries from changing an entry of the current device's
    colour table, or let it again (Inside Macintosh V-143): an error to
    protect one already protected."""
    p = _entry(t, index)
    if not p:
        return _qd_error(t, cRangeErr)
    value = t.mem.r16(p)
    if protect and value & PROTECTED:
        return _qd_error(t, cProtectErr)
    t.mem.w16(p, value | PROTECTED if protect else value & ~PROTECTED)
    _qd_error(t, noErr)


@trap
def ReserveEntry(t, index, reserve):
    """Reserve an entry of the current device's colour table for the
    device's gdID, or let it go (Inside Macintosh V-143): no colour is
    matched to it, and only that gdID's SetEntries changes it.  An error
    to reserve one already reserved."""
    p = _entry(t, index)
    if not p:
        return _qd_error(t, cRangeErr)
    value = t.mem.r16(p)
    if reserve and value & RESERVED:
        return _qd_error(t, cProtectErr)
    gdh, devices = _device(t), t.system.devices
    if reserve:
        t.mem.w16(p, value & 0xFF00 | RESERVED | _gd(t, gdh).id & 0xFF)
    else:
        t.mem.w16(p, value & ~RESERVED)
    if gdh & ADDRESS_MASK == devices.main & ADDRESS_MASK:
        devices.held = devices.held | {index} if reserve else devices.held - {index}
    if reserve or value & RESERVED:
        _new_colors(t, gdh, p - 8 - 8 * index)
    _qd_error(t, noErr)


def _new_colors(t, gdh: int, ct: int, seed: bool = True) -> None:
    """A device's colour table has changed: a new seed for it (unless
    not), a new inverse table, and the screen shown again in it."""
    devices = t.system.devices
    if seed:
        t.mem.w32(ct, devices.next_seed())
    gd = _gd(t, gdh)
    pm = _pixmap(t, gdh)
    if gd is not None and pm is not None and gd.i_table:
        _make_itable(t, pm.pm_table, gd.i_table, gd.res_pref or ITAB_RES)
    if gdh & ADDRESS_MASK == devices.main & ADDRESS_MASK:
        devices.changed()
        # Shown in the new colours by the next tick: each trap and the
        # clock look then, and QuickDraw looks before it draws in a window,
        # so it matches to them.  A program that reserves entry after entry
        # (Rebel Assault, 508 a pass) isn't shown each change in turn.
        t.system.screen.tick()


def _make_itable(t, ct_h: int, it_h: int, res: int) -> bool:
    ct = deref(t.mem, ct_h)
    if not ct:
        return False
    data = make_itable(read_table(t.mem, ct_h), t.mem.r32(ct), res)
    heap = t.system.heap_of(it_h)
    if heap.handle_size(it_h) != len(data) and not heap.set_handle_size(it_h, len(data)):
        return False
    t.mem.write(heap.deref(it_h), data)
    return True


@trap
def MakeITable(t, ct_h, it_h, res):
    """An inverse table for a colour table, of res bits a component: the
    current device's, for either handle NIL, and res 0."""
    gdh = _device(t)
    gd, pm = _gd(t, gdh), _pixmap(t, gdh)
    if not ct_h and pm is not None:
        ct_h = pm.pm_table
    if not it_h and gd is not None:
        it_h = gd.i_table
    if not res:
        res = gd.res_pref if gd is not None and gd.res_pref else ITAB_RES
    if not 3 <= res <= 5:
        return _qd_error(t, cRangeErr)
    if not ct_h or not it_h or not _make_itable(t, ct_h, it_h, res):
        return _qd_error(t, cNoMemErr)
    _qd_error(t, noErr)


@trap
def SaveEntries(t, src, result, sel):
    """Copy the entries selection names, by position, from a colour table
    (the current device's for NIL) into result, in its order."""
    if not src:
        pm = _pixmap(t, _device(t))
        src = pm.pm_table if pm is not None else 0
    sp, rp = deref(t.mem, src), deref(t.mem, result)
    if not sp or not rp:
        return _qd_error(t, cRangeErr)
    n = t.mem.r16s(sel) + 1
    size = t.mem.r16s(sp + 6) + 1
    try:
        heap = t.system.heap_of(result)
    except ValueError:      # not a handle: Giffer passes the address of its variable
        return _qd_error(t, cNoMemErr)
    if heap.handle_size(result) < 8 + 8 * n and not heap.set_handle_size(result, 8 + 8 * n):
        return _qd_error(t, cNoMemErr)
    rp = heap.deref(result)
    t.mem.write(rp, struct.pack(">lHh", t.mem.r32(sp), 0, n - 1))
    err = noErr
    for i in range(n):
        at = t.mem.r16s(sel + 2 + 2 * i)
        if not 0 <= at < size:
            t.mem.w16(sel + 2 + 2 * i, 0xFFFF)          # colReqErr
            err = cRangeErr
            continue
        spec = bytearray(t.mem.read(sp + 8 + 8 * at, 8))
        spec[0:2] = struct.pack(">h", at)
        t.mem.write(rp + 8 + 8 * i, bytes(spec))
    _qd_error(t, err)


@trap
def RestoreEntries(t, src, dst, sel):
    """Put src's entries back where selection says, in dst (the current
    device's table for NIL), keeping its seed."""
    gdh = _device(t)
    pm = _pixmap(t, gdh)
    device_table = pm.pm_table if pm is not None else 0
    if not dst:
        dst = device_table
    sp, dp = deref(t.mem, src), deref(t.mem, dst)
    if not sp or not dp:
        return _qd_error(t, cRangeErr)
    n, size = t.mem.r16s(sel) + 1, t.mem.r16s(dp + 6) + 1
    err = noErr
    for i in range(n):
        at = t.mem.r16s(sel + 2 + 2 * i)
        if not 0 <= at < size:
            t.mem.w16(sel + 2 + 2 * i, 0xFFFF)
            err = cRangeErr
            continue
        t.mem.write(dp + 8 + 8 * at + 2, t.mem.read(sp + 8 + 8 * i + 2, 6))
    if dst & ADDRESS_MASK == device_table & ADDRESS_MASK and device_table:
        _new_colors(t, gdh, dp, seed=False)
    _qd_error(t, err)


@trap
def QDError(t):
    return getattr(t.process, "qd_error", noErr)


# -- colours ------------------------------------------------------------------
@trap
def ForeColor(t, color):
    port = _port(t)
    if port is not None:
        t.tb.qd.set_classic_color(port, True, color)


@trap
def BackColor(t, color):
    port = _port(t)
    if port is not None:
        t.tb.qd.set_classic_color(port, False, color)


@trap
def ColorBit(t, which):
    """The plane of a colour printer's the port draws in, its colrBit
    (Inside Macintosh I, QuickDraw); for the screen it changes nothing."""
    port = _port(t)
    if port is not None:
        port.colr_bit = which


@trap
def RGBForeColor(t, p):
    port = _port(t)
    if port is not None:
        t.tb.qd.set_rgb_color(port, True, _rgb(t, p))


@trap
def RGBBackColor(t, p):
    port = _port(t)
    if port is not None:
        t.tb.qd.set_rgb_color(port, False, _rgb(t, p))


@trap
def GetForeColor(t, p):
    _set_rgb(t, p, port_rgb(_port(t), True))


@trap
def GetBackColor(t, p):
    _set_rgb(t, p, port_rgb(_port(t), False))


# SaveFore, SaveBack, RestoreFore and RestoreBack (Inside Macintosh VI, the
# Palette Manager): the port's colour kept in a ColorSpec, here its RGB
# with a value of 0, and set again from it.
@trap
def SaveFore(t, p):
    t.w16(p, 0)
    _set_rgb(t, p + 2, port_rgb(_port(t), True))


@trap
def SaveBack(t, p):
    t.w16(p, 0)
    _set_rgb(t, p + 2, port_rgb(_port(t), False))


@trap
def RestoreFore(t, p):
    RGBForeColor(t, p + 2)


@trap
def RestoreBack(t, p):
    RGBBackColor(t, p + 2)


@trap
def HiliteColor(t, p):
    """Kept in HiliteRGB, where the highlighting colour is looked for;
    highlighting is inverting here anyway."""
    _set_rgb(t, HiliteRGB, _rgb(t, p))


@trap
def OpColor(t, p):
    """Kept in a cGrafPort's GrafVars, as rgbOpColor, for the arithmetic
    modes; in an old port it has no effect."""
    port = _port(t)
    gv = deref(t.mem, port.graf_vars) if port is not None and port.is_color else 0
    if gv:
        _set_rgb(t, gv, _rgb(t, p))


@trap
def InvertColor(t, p):
    """The colour's complement, by the default complement procedure's
    rule, each component's ones' complement.  On a black-and-white Mac,
    left as it is: inverting black and white gives white and black."""
    if t.system.devices is not None:
        _set_rgb(t, p, tuple(~v & 0xFFFF for v in _rgb(t, p)))


@trap
def Color2Index(t, p):
    return t.tb.qd.color_index(_rgb(t, p))


# -- search procedures (Inside Macintosh V, The Color Manager) -----------------
# A search procedure is asked a colour's pixel value before the inverse
# table is, by Color2Index and by CopyBits as it maps a bitmap's black and
# white to pixel values: NetTrek's draws its ships, black and white
# pictures, in each side's colours.  Each process keeps its own, by
# device, rather than in the device's gdSearchProc chain.
@trap
def AddSearch(t, proc):
    procs = _search_procs(t)
    procs.insert(0, proc & ADDRESS_MASK)        # asked first
    t.tb.qd.search = partial(_searched, t)


@trap
def DelSearch(t, proc):
    procs = _search_procs(t)
    if proc & ADDRESS_MASK in procs:
        procs.remove(proc & ADDRESS_MASK)
    if not any(t.process.search_procs.values()):
        t.tb.qd.search = None


@trap
def SetClientID(t, client):
    """Set the current device's gdID, which identifies the program to
    its search procedures (Advanced Color Imaging Reference, Color
    Manager Reference), and which ReserveEntry marks entries with."""
    gdh = _device(t)
    gd = _gd(t, gdh) if gdh else None
    if gd is not None:
        gd.id = client


def _search_procs(t) -> list[int]:
    by_device = getattr(t.process, "search_procs", None)
    if by_device is None:
        by_device = t.process.search_procs = {}
    return by_device.setdefault(_device(t) & ADDRESS_MASK, [])


def _searched(t, rgb: tuple[int, int, int]) -> int | None:
    """The pixel value the current device's search procedures give a
    colour: FUNCTION (rgb: RGBColor; VAR position: LONGINT): BOOLEAN,
    the first that says it found one; else None."""
    procs = getattr(t.process, "search_procs", {}).get(_device(t) & ADDRESS_MASK)
    if not procs:
        return None
    with scratch(t, 10) as rec:
        for proc in tuple(procs):
            _set_rgb(t, rec, rgb)
            t.mem.w32(rec + 6, 0)
            if t.call(proc, (("p", rec), ("p", rec + 6)), "b"):
                return t.mem.r32(rec + 6)
    return None


@trap
def Index2Color(t, index, p):
    if t.system.devices is None:
        return _set_rgb(t, p, color_rgb(whiteColor if index == 0 else blackColor))
    depth, table = _colors(t)
    _set_argb(t, p, value_rgb(index, depth, table))


@trap
def RealColor(t, p):
    """Whether the current device has the colour, to its inverse table's
    resolution: the top bits of each component."""
    depth, table = _colors(t)
    if depth > 8:
        return True
    gd = _gd(t, _device(t))
    shift = 16 - (gd.res_pref if gd is not None and gd.res_pref else ITAB_RES)
    want = tuple(v >> shift for v in _rgb(t, p))
    return any(tuple(((c >> s & 0xFF) * 0x101) >> shift for s in (16, 8, 0)) == want
               for c in table)


@trap
def GetCTable(t, ct_id):
    """A ColorTable handle: one of the standard ones, with the highlight
    colour if 64 + depth, else a copy of the clut resource of that ID."""
    data = standard_table(ct_id) or highlight_table(ct_id, _rgb(t, HiliteRGB))
    if data is None:
        res = t.process.resources.GetResource("clut", ct_id)
        if not res or not t.mem.r32(res) & ADDRESS_MASK:
            return 0
        data = t.tb.contents(res)
    return t.process.heap.new_handle_with(data)


@trap
def DisposeCTable(t, h):
    """DisposeHandle for a colour table, MemErr and all: one in no heap
    sets memWZErr.  The Secret Island of Dr. Quandary copies a PixMap
    from its own data, pmTable ($FFFFFFFF) and all, and disposes of that
    before it puts a table of its own there."""
    if h:
        DisposeHandle(t, h)


@trap
def GetGray(t, device, back, fore):
    """No grey between two colours on a 1-bit device: FALSE, fore unchanged."""
    return False
