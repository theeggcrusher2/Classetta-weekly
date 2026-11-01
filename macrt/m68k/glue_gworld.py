"""
Offscreen graphics worlds, 32-Bit QuickDraw's QDExtensions (Inside
Macintosh VI-21).

A GWorld is a cGrafPort onto a PixMap of its own, its pixels in memory,
with a GDevice of its own for it (unless it's made with noNewDevice and
a program's), whose PixMap is the port's: SetGWorld makes that the
current device as it makes the port the current port, so that what's
drawn in it is matched to its colour table, as it would be on a screen
of its depth.  Its portVersion has bit 0 set, and its portRect is the
rectangle it was made with, as its PixMap's bounds are; made with depth
0, the rectangle is a global one, which picks the screen, and the world
is its size with its top left at 0,0.

Made with noNewDevice on a program's device, a world has that device's
depth and colour table (Inside Macintosh: Imaging With QuickDraw,
NewGWorld): given no table, its PixMap's is the device's own handle, as
OpenCPort gives a cGrafPort the current device's (Inside Macintosh V,
Color QuickDraw), so that what's drawn in it is in the device's pixel
values, and CopyBits copies them as they are.  Given a device,
UpdateGWorld gives a world that device's depth and colour table, whatever
pixelDepth and cTable are (Inside Macintosh: Imaging With QuickDraw,
UpdateGWorld): a world made on it with noNewDevice has the device's own
table then, as one made with no table has, and any other a copy.

The pixels are in a handle, in the application heap, or in the System
heap, as temporary memory, when there isn't room there (or a program asks
with useTempMem).  macrt keeps it locked, so a PixMap's baseAddr is
where they are whether or not LockPixels has been called, and never
purges it: GetPixelsState tells a program what it asked for.

    GWorldFlags  pixPurge 0, noNewDevice 1, useTempMem 2, keepLocal 3,
                 pixelsPurgeable 6, pixelsLocked 7; what UpdateGWorld
                 did: mapPix 16, newDepth 17, alignPix 18, newRowBytes 19,
                 reallocPix 20, clipPix 28, stretchPix 29, ditherPix 30,
                 gwFlagErr 31

UpdateGWorld keeps the pixels when there's nothing it needs to change;
otherwise, given clipPix, what's in both the old rectangle and the new,
or, given stretchPix, the whole image, scaled to the new; with neither,
the new pixels are white.  A screen buffer (NewScreenBuffer) is a PixMap
like the deepest screen's, sharing its colour table, for a program to draw
in with a port of its own.

With only basic QuickDraw (System 7 on a Plus), a world is a GrafPort whose
portBits are its pixels, one bit a pixel, with no GDevice; GetGWorldPixMap
gives a 1-bit pixel map of them, for LockPixels, GetPixBaseAddr and the
rest (Inside Macintosh: Imaging With QuickDraw, "Offscreen Graphics
Worlds").  It may be made with depth 0 or 1 and no colour table.
"""

from __future__ import annotations

from PyQt6.QtCore import QRect
from PyQt6.QtGui import QImage, QPainter

from ..memory import ADDRESS_MASK, BusError
from ..toolbox import lowmem, pixels
from ..toolbox.color import color_table, standard_table
from ..toolbox.gdevice import (DEPTH_MODES, GDEVICE_SIZE, ITAB_RES, PIXMAP_SIZE,
                               ROW_BYTES_MASK, GDevice, PixMap, clutType, deref, directType,
                               gdDevType, make_itable, noDriver, read_table, write_pixmap)
from ..toolbox.quickdraw import CPORT_VERSION, BitMap, GrafPort, set_rect_rgn
from ..toolbox.types import Rect
from .dispatch import trap
from .glue_color import _make_itable, _new_colors, _qd_error, cNoMemErr, noErr
from .glue_common import _heap, _the_port_var

pixPurge, noNewDevice, useTempMem, keepLocal = 1, 2, 4, 8
pixelsPurgeable, pixelsLocked = 0x40, 0x80
mapPix, newDepth, alignPix, newRowBytes, reallocPix = (1 << 16, 1 << 17, 1 << 18, 1 << 19,
                                                       1 << 20)
clipPix, stretchPix, ditherPix, gwFlagErr = 1 << 28, 1 << 29, 1 << 30, 1 << 31
GWORLD_VERSION = CPORT_VERSION | 1      # a GWorld's portVersion
paramErr, cDepthErr = -50, -157
# OffscreenVersion's answer.  No Apple document found gives it; Executor
# answers $130, and programs take $100 or more to mean 32-Bit QuickDraw's
# offscreen routines are there (Spaceward Ho! 3.0 wants them for colour).
OFFSCREEN_VERSION = 0x130


def _deref(t, h: int) -> int:
    try:
        return deref(t.mem, h)
    except BusError:
        return 0


def _dispose(t, h: int) -> None:
    heap = t.system.find_heap(h) if h else None
    if heap is not None and heap.is_handle(h & ADDRESS_MASK):
        heap.dispose_handle(h & ADDRESS_MASK)


def _row_bytes(width: int, depth: int) -> int:
    """A row's bytes, a whole number of longs (as 32-Bit QuickDraw has them)."""
    return (width * depth + 31) // 32 * 4


def _main(t) -> PixMap | None:
    devices = t.system.devices
    return devices.pixmap(devices.main) if devices is not None else None


def _table_data(t, depth: int, ct_h: int) -> bytes:
    """The colour table a world of depth bits gets: a copy of ct_h's, else
    the standard one for the depth (a 'clut' of its depth's ID, or the
    greys for 2 bits, as GetCTable has them); none for direct pixels."""
    if depth > 8:
        return color_table([])
    p = _deref(t, ct_h)
    if p:
        return t.mem.read(p, 8 + 8 * (max(0, t.mem.r16s(p + 6)) + 1))
    data = standard_table(depth)
    if data is None:
        res = t.process.resources.GetResource("clut", depth)
        data = t.tb.contents(res) if res else None     # (b"" if it's purged)
    return data or standard_table(32 + depth) or color_table([])


def _new_pixels(t, size: int, flags: int) -> int:
    """A locked handle of size bytes, cleared: in the application heap,
    unless useTempMem or there isn't room, then in the System heap."""
    heaps = [t.process.heap, t.system.sys_heap]
    if flags & useTempMem:
        heaps.reverse()
    for heap in heaps:
        with t.system.lock:
            h = heap.new_handle(max(size, 1), clear=True) if 0 < size < 0x800000 else 0
        if h:
            heap.lock(h)
            return h
    return 0


def _pixels_handle(t, pm: PixMap) -> int:
    """The handle a screen buffer's pixels are in, from its baseAddr.  (A
    world keeps its own: a program may point its baseAddr elsewhere.)"""
    base = pm.base_addr & ADDRESS_MASK
    heap = t.system.find_heap(base) if base else None
    return heap.recover_handle(base) if heap is not None else 0


def _new_device(t, heap, pm_h: int, bounds: Rect, depth: int) -> int:
    """A GDevice for a world's PixMap, with an inverse table for its colours."""
    mem = t.mem
    gdh = heap.new_handle(GDEVICE_SIZE, clear=True)
    if not gdh:
        return 0
    gd = GDevice(mem, heap.deref(gdh))
    gd.type = clutType if depth <= 8 else directType
    gd.res_pref = ITAB_RES
    gd.flags = (1 << gdDevType if depth > 1 else 0) | 1 << noDriver
    gd.pmap = pm_h
    gd.rect = bounds
    gd.mode = -1
    if depth <= 8 and not _new_itable(t, heap, gdh):
        heap.dispose_handle(gdh)
        return 0
    return gdh


def _new_itable(t, heap, gdh: int) -> bool:
    """Give a world's device an inverse table for its PixMap's colours."""
    mem = t.mem
    gd = GDevice(mem, _deref(t, gdh))
    pm = PixMap(mem, _deref(t, gd.pmap))
    if gd.i_table:
        return _make_itable(t, pm.pm_table, gd.i_table, gd.res_pref or ITAB_RES)
    ct = _deref(t, pm.pm_table)
    ih = heap.new_handle_with(make_itable(read_table(mem, pm.pm_table), mem.r32(ct) if ct else 0))
    gd = GDevice(mem, _deref(t, gdh))      # the heap may have moved it
    gd.i_table = ih
    return bool(ih)


def gworld(t, p: int) -> GrafPort | None:
    """The port at p, if it's a GWorld."""
    port = t.system.port_at(p) if p else None
    return port if port is not None and (port.gw_device or port.gw_pix_map) else None


def _pix_map_h(port: GrafPort) -> int:
    """A world's PixMap handle: its portPixMap, or, on basic QuickDraw, the
    1-bit one of its portBits."""
    return port.port_pix_map if port.is_color else port.gw_pix_map


def _depth_and_table(t, depth: int, ct_h: int) -> tuple[int, bytes] | None:
    """The depth and colour table a world asked for gets: given depth 0,
    the deepest screen's, and a copy of its table."""
    if depth == 0:
        main = _main(t)
        depth, ct_h = (main.pixel_size, main.pm_table) if main is not None else (0, 0)
    if depth not in DEPTH_MODES:
        return None
    return depth, _table_data(t, depth, ct_h)


def _world_rect(bounds: Rect, depth: int) -> Rect:
    """The rectangle a world given bounds gets: given depth 0, bounds are a
    global rectangle that picks the screen, and the world is that size with
    its top left at 0,0."""
    return bounds.offset(-bounds.left, -bounds.top) if depth == 0 else bounds


@trap
def NewGWorld(t, gw_p, depth, bounds_p, ct_h, gdh, flags):
    bounds = _world_rect(t.mem.r_rect(bounds_p), depth)
    if t.system.devices is None:
        err = _new_basic_gworld(t, gw_p, depth, bounds, flags)
        _qd_error(t, err)
        return err
    # Given depth 0, the world is like the deepest screen, and aGDevice is
    # ignored, noNewDevice or not.
    own_device = depth == 0 or not flags & noNewDevice
    device = _device_pixmap(t, gdh) if not own_device else None
    if not own_device and device is None:
        _qd_error(t, paramErr)
        return paramErr
    if device is not None:
        depth = device.pixel_size
    got = _depth_and_table(t, depth, ct_h)
    if got is None:
        _qd_error(t, cDepthErr)
        return cDepthErr
    depth, table = got
    if bounds.empty:
        _qd_error(t, paramErr)
        return paramErr
    shared = device.pm_table if device is not None and not ct_h and depth <= 8 else 0
    err = _new_gworld(t, gw_p, depth, bounds, table, 0 if own_device else gdh, flags, shared)
    _qd_error(t, err)
    return err


def _device_pixmap(t, gdh: int) -> PixMap | None:
    gd = _deref(t, gdh)
    pm = _deref(t, GDevice(t.mem, gd).pmap) if gd else 0
    return PixMap(t.mem, pm) if pm else None


def _shares_table(t, port: GrafPort, ct_h: int) -> bool:
    """Whether ct_h is the colour table of the device a world was made on
    with noNewDevice, rather than one of its own."""
    if port.gw_own_device or not ct_h:
        return False
    pm = _device_pixmap(t, port.gw_device)
    return pm is not None and pm.pm_table & ADDRESS_MASK == ct_h & ADDRESS_MASK


def _new_gworld(t, gw_p: int, depth: int, bounds: Rect, table: bytes, device: int,
                flags: int, shared: int = 0) -> int:
    """shared is the colour table handle of the device the world is made
    on, if its PixMap is to have that one, not a copy of table."""
    mem, heap = t.mem, t.process.heap
    row_bytes = _row_bytes(bounds.width, depth)
    pix = _new_pixels(t, row_bytes * bounds.height, flags)
    ct = shared or heap.new_handle_with(table)
    pm_h = heap.new_handle(PIXMAP_SIZE, clear=True)
    port_p = heap.new_ptr(GrafPort.SIZE, clear=True)
    made = [h for h in (pix, 0 if shared else ct, pm_h) if h]
    if not (pix and ct and pm_h and port_p):
        for h in made:
            _dispose(t, h)
        if port_p:
            heap.dispose_ptr(port_p)
        return cNoMemErr
    write_pixmap(mem, heap.deref(pm_h), _deref(t, pix), row_bytes, bounds, depth, ct)
    own = not device
    if own:
        device = _new_device(t, heap, pm_h, bounds, depth)
        if not device:
            for h in made:
                _dispose(t, h)
            heap.dispose_ptr(port_p)
            return cNoMemErr
    # OpenCPort onto the world's device, leaving the current port and
    # device as they were.
    var = _the_port_var(t)
    was_port, was_device = mem.r32(var), mem.r32(lowmem.TheGDevice)
    mem.w32(lowmem.TheGDevice, device)
    port = GrafPort(mem, port_p)
    opened = port.open_color(heap, t.system.screen_bits)
    mem.w32(lowmem.TheGDevice, was_device)
    mem.w32(var, was_port)
    if not opened:
        if own:
            _dispose_device(t, device)
        for h in made:
            _dispose(t, h)
        heap.dispose_ptr(port_p)
        return cNoMemErr
    heap.dispose_handle(port.port_pix_map)
    port.port_pix_map = pm_h
    port.port_version = GWORLD_VERSION
    port.port_rect = bounds
    set_rect_rgn(heap, port.vis_rgn, bounds)
    port.gw_device = device
    port.gw_own_device = own
    port.gw_pixels = pix
    t.system.register(port)
    t.w32(gw_p, port_p)
    return noErr


def _new_basic_gworld(t, gw_p: int, depth: int, bounds: Rect, flags: int) -> int:
    """A world on basic QuickDraw: a GrafPort, as OpenPort makes one, onto
    1-bit pixels of its own, its portRect and visRgn their bounds."""
    if depth not in (0, 1):
        return cDepthErr
    if bounds.empty:
        return paramErr
    mem, heap = t.mem, t.process.heap
    row_bytes = _row_bytes(bounds.width, 1)
    pix = _new_pixels(t, row_bytes * bounds.height, flags)
    pm_h = heap.new_handle(PIXMAP_SIZE, clear=True)
    port_p = heap.new_ptr(GrafPort.SIZE, clear=True)
    if not (pix and pm_h and port_p):
        for h in (pix, pm_h):
            if h:
                _dispose(t, h)
        if port_p:
            heap.dispose_ptr(port_p)
        return cNoMemErr
    base = _deref(t, pix)
    write_pixmap(mem, heap.deref(pm_h), base, row_bytes, bounds, 1, 0)
    port = GrafPort(mem, port_p)
    port.open(heap, BitMap(base, row_bytes, bounds))
    port.gw_pix_map = pm_h
    port.gw_pixels = pix
    t.system.register(port)
    t.w32(gw_p, port_p)
    return noErr


def _dispose_device(t, gdh: int) -> None:
    """A world's own device and its inverse table (not its PixMap, the port's)."""
    gd = GDevice(t.mem, _deref(t, gdh)) if _deref(t, gdh) else None
    if gd is not None:
        _dispose(t, gd.i_table)
    _dispose(t, gdh)


@trap
def DisposeGWorld(t, gw):
    port = gworld(t, gw)
    if port is None:
        return
    mem = t.mem
    if (port.gw_device
            and mem.r32(lowmem.TheGDevice) & ADDRESS_MASK == port.gw_device & ADDRESS_MASK):
        mem.w32(lowmem.TheGDevice, t.system.devices.main)
    pm_h = _pix_map_h(port)
    p = _deref(t, pm_h)
    if p and not _shares_table(t, port, PixMap(mem, p).pm_table):
        _dispose(t, PixMap(mem, p).pm_table)
    _dispose(t, port.gw_pixels)
    if port.gw_own_device:
        _dispose_device(t, port.gw_device)
    _dispose(t, pm_h)
    if port.is_color:
        port.port_pix_map = 0
    heap = _heap(t, gw)
    port.close((t.system.find_heap(port.vis_rgn) or heap) if port.vis_rgn else heap)
    t.system.unregister(port)
    port.gw_device = port.gw_pix_map = port.gw_pixels = 0
    if heap.contains(gw & ADDRESS_MASK):
        heap.dispose_ptr(gw & ADDRESS_MASK)


@trap
def GetGWorld(t, port_pp, gdh_pp):
    t.w32(port_pp, t.mem.r32(_the_port_var(t)))
    t.w32(gdh_pp, t.mem.r32(lowmem.TheGDevice))


@trap
def SetGWorld(t, p, gdh):
    """The current port, and the current device: a GWorld's own, for a
    GWorld; else gdh.  Given NIL, a port on the main screen (a window)
    has the main device, as Executor has it: FloorTiles goes back to its
    window that way from a GWorld, and then makes a colour dialog, whose
    PixMap is a copy of the current device's; any other port leaves the
    device as it was."""
    t.mem.w32(_the_port_var(t), p)
    devices = t.system.devices
    if devices is None:
        return
    port = gworld(t, p)
    device = port.gw_device if port is not None else gdh
    if not device:
        port, main = t.system.port_at(p) if p else None, devices.pixmap(devices.main)
        if (port is not None and main is not None
                and port.port_bits_base & ADDRESS_MASK == main.base_addr & ADDRESS_MASK):
            device = devices.main
    if device:
        t.mem.w32(lowmem.TheGDevice, device)


@trap
def GetGWorldDevice(t, gw):
    """A world's device; for another port, or a world on basic QuickDraw,
    which has none, the current one."""
    port = gworld(t, gw)
    return port.gw_device if port is not None and port.gw_device else t.mem.r32(
        lowmem.TheGDevice)


@trap
def GetGWorldPixMap(t, gw):
    if not gw:
        return 0
    port = t.system.port_at(gw)
    if port is not None:
        return _pix_map_h(port)
    return t.mem.r32(gw + 2) if t.mem.r16(gw + 6) & CPORT_VERSION == CPORT_VERSION else 0


@trap
def UpdateGWorld(t, gw_p, depth, bounds_p, ct_h, gdh, flags):
    """Change a world's depth, colour table or rectangle, keeping what's
    in it as clipPix or stretchPix say (and, for another colour table,
    in the nearest colours it has); what it did.  Given a device, the
    world takes its depth and colour table, whatever pixelDepth and
    cTable are."""
    mem = t.mem
    port = gworld(t, mem.r32(gw_p))
    if port is None:
        _qd_error(t, paramErr)
        return gwFlagErr
    pm = PixMap(mem, _deref(t, _pix_map_h(port)))
    old_depth, old_bounds = pm.pixel_size, pm.bounds
    old_rb, old_table = pm.row_bytes & ROW_BYTES_MASK, read_table(mem, pm.pm_table)
    old_data = _deref(t, pm.pm_table)
    old_ct = mem.read(old_data, 8 + 8 * (max(0, mem.r16s(old_data + 6)) + 1)) if old_data else b""
    gdh = gdh if port.is_color else 0
    bounds = _world_rect(mem.r_rect(bounds_p), depth if not gdh else 1)
    shared = 0                                  # the device's own table, for a world on it
    if not port.is_color:                       # basic QuickDraw's: 1 bit, no table
        if depth not in (0, 1):
            _qd_error(t, cDepthErr)
            return gwFlagErr
        depth, table = 1, old_ct
    elif gdh:
        device = _device_pixmap(t, gdh)
        if device is None or device.pixel_size not in DEPTH_MODES:
            _qd_error(t, paramErr)
            return gwFlagErr
        depth = device.pixel_size
        table = _table_data(t, depth, device.pm_table)
        if (depth <= 8 and not port.gw_own_device
                and port.gw_device & ADDRESS_MASK == gdh & ADDRESS_MASK):
            shared = device.pm_table
    elif depth == 0 or ct_h or depth != old_depth:
        got = _depth_and_table(t, depth, ct_h)
        if got is None:
            _qd_error(t, cDepthErr)
            return gwFlagErr
        depth, table = got
    else:
        table = old_ct
    if bounds.empty:
        _qd_error(t, paramErr)
        return gwFlagErr
    remap = table[8:] != old_ct[8:] and depth <= 8
    sized = (bounds.width, bounds.height) == (old_bounds.width, old_bounds.height)
    if depth == old_depth and sized and not remap:
        if bounds != old_bounds:
            _move(t, port, bounds)
        if shared:
            _share_table(t, port, pm, shared)
        _qd_error(t, noErr)
        return 0
    row_bytes = _row_bytes(bounds.width, depth)
    pix = _new_pixels(t, row_bytes * bounds.height, flags)
    if not pix:
        _qd_error(t, cNoMemErr)
        return gwFlagErr
    old_image = pixels.decode(mem.read(pm.base_addr & ADDRESS_MASK, old_rb * old_bounds.height),
                              old_bounds.width, old_bounds.height, old_rb, old_depth,
                              old_table).convertToFormat(QImage.Format.Format_ARGB32)
    if shared:
        _share_table(t, port, PixMap(mem, _deref(t, _pix_map_h(port))), shared)
    elif pm.pm_table and _shares_table(t, port, pm.pm_table):
        if table != old_ct:                 # its own table now, not the device's
            ct = t.process.heap.new_handle_with(table)
            if not ct:
                _dispose(t, pix)
                _qd_error(t, cNoMemErr)
                return gwFlagErr
            pm.pm_table = ct
    elif pm.pm_table:
        heap = _heap(t, pm.pm_table)
        if not heap.set_handle_size(pm.pm_table, len(table)):
            _dispose(t, pix)
            _qd_error(t, cNoMemErr)
            return gwFlagErr
        mem.write(heap.deref(pm.pm_table), table)
    pm = PixMap(mem, _deref(t, _pix_map_h(port)))
    write_pixmap(mem, pm.addr, _deref(t, pix), row_bytes, bounds, depth, pm.pm_table)
    done = reallocPix
    if depth != old_depth:
        done |= newDepth
    if row_bytes != old_rb:
        done |= newRowBytes
    keep = flags & (clipPix | stretchPix)
    if remap and depth == old_depth and sized and not keep:
        keep = clipPix
        done |= mapPix
    if keep:
        keep = clipPix if keep & clipPix else stretchPix
        _redraw(t, pm, old_image, old_bounds, keep)
        done |= keep
    _dispose(t, port.gw_pixels)
    port.gw_pixels = pix
    _move(t, port, bounds)
    gd = GDevice(mem, _deref(t, port.gw_device))
    if port.gw_own_device and gd.addr:
        gd.type = clutType if depth <= 8 else directType
        gd.flags = (gd.flags & ~(1 << gdDevType)) | (1 << gdDevType if depth > 1 else 0)
        if depth <= 8:
            _new_itable(t, t.process.heap, port.gw_device)
    _qd_error(t, noErr)
    return done


def _share_table(t, port: GrafPort, pm: PixMap, ct_h: int) -> None:
    """Give a world made on a device with noNewDevice that device's colour
    table, ct_h, in place of one of its own."""
    if pm.pm_table & ADDRESS_MASK != ct_h & ADDRESS_MASK:
        if pm.pm_table and not _shares_table(t, port, pm.pm_table):
            _dispose(t, pm.pm_table)
        pm.pm_table = ct_h


def _move(t, port: GrafPort, bounds: Rect) -> None:
    """A world's rectangle: its portRect, visRgn, PixMap's and device's;
    on basic QuickDraw, its portBits, the PixMap's pixels."""
    mem = t.mem
    pm = PixMap(mem, _deref(t, _pix_map_h(port)))
    pm.bounds = bounds
    if not port.is_color:
        port.port_bits_base = pm.base_addr
        port.port_bits_row_bytes = pm.row_bytes & ROW_BYTES_MASK
        port.port_bits_bounds = bounds
    port.port_rect = bounds
    heap = t.system.find_heap(port.vis_rgn)
    if heap is not None:
        set_rect_rgn(heap, port.vis_rgn, bounds)
    p = _deref(t, port.gw_device)
    if p and port.gw_own_device:
        GDevice(mem, p).rect = bounds


def _redraw(t, pm: PixMap, img: QImage, old: Rect, keep: int) -> None:
    """Put an old image in a PixMap's new pixels: where it was, clipped to
    the new rectangle (clipPix), or scaled to fill it (stretchPix)."""
    mem = t.mem
    b, depth, rb = pm.bounds, pm.pixel_size, pm.row_bytes & ROW_BYTES_MASK
    table = read_table(mem, pm.pm_table) if depth <= 8 else None
    # Whole rows, so that what's encoded has no padding of Qt's in it,
    # which it leaves as it finds it.
    out = pixels.decode(bytes(rb * b.height), rb * 8 // depth, b.height, rb, depth,
                        table).convertToFormat(QImage.Format.Format_ARGB32)
    p = QPainter(out)
    p.setClipRect(0, 0, b.width, b.height)
    if keep & clipPix:
        p.drawImage(old.left - b.left, old.top - b.top, img)
    else:
        p.drawImage(QRect(0, 0, b.width, b.height), img)
    p.end()
    data, bpl = pixels.encode(out, depth, table)
    base = pm.base_addr & ADDRESS_MASK
    n = min(bpl, rb)
    for v in range(b.height):
        mem.write(base + v * rb, data[v * bpl:v * bpl + n])


# -- pixels -------------------------------------------------------------------
def _states(t) -> dict[int, int]:
    states = getattr(t.process, "pixels_states", None)
    if states is None:
        states = t.process.pixels_states = {}
    return states


def _set_state(t, pm: int, on: int = 0, off: int = 0) -> None:
    states = _states(t)
    key = pm & ADDRESS_MASK
    states[key] = (states.get(key, 0) | on) & ~off


@trap
def LockPixels(t, pm):
    """TRUE: the pixels are never purged."""
    _set_state(t, pm, on=pixelsLocked)
    return True


@trap
def UnlockPixels(t, pm):
    _set_state(t, pm, off=pixelsLocked)


@trap
def AllowPurgePixels(t, pm):
    _set_state(t, pm, on=pixelsPurgeable)


@trap
def NoPurgePixels(t, pm):
    _set_state(t, pm, off=pixelsPurgeable)


@trap
def GetPixelsState(t, pm):
    return _states(t).get(pm & ADDRESS_MASK, 0)


@trap
def SetPixelsState(t, pm, state):
    _states(t)[pm & ADDRESS_MASK] = state & (pixelsLocked | pixelsPurgeable)


@trap
def GetPixBaseAddr(t, pm):
    p = _deref(t, pm)
    return PixMap(t.mem, p).base_addr if p else 0


@trap
def PixMap32Bit(t, pm):
    """FALSE: every PixMap's pixels are in 24-bit memory."""
    return False


@trap
def QDDone(t, port):
    """TRUE: drawing is done when the call that does it returns."""
    return True


@trap
def OffscreenVersion(t):
    return OFFSCREEN_VERSION


# -- changes a program has made to a record ----------------------------------
@trap
def CTabChanged(t, ct_h):
    """A new seed for the colour table, and for a device's, a new inverse
    table and the screen shown again in its colours."""
    devices, ct = t.system.devices, _deref(t, ct_h)
    if devices is None or not ct:
        return
    main = devices.pixmap(devices.main)
    if main is not None and main.pm_table & ADDRESS_MASK == ct_h & ADDRESS_MASK:
        _new_colors(t, devices.main, ct)
        return
    t.mem.w32(ct, devices.next_seed())
    for port in list(t.system.ports.values()):
        if port.gw_own_device:
            p = _deref(t, port.port_pix_map)
            if p and PixMap(t.mem, p).pm_table & ADDRESS_MASK == ct_h & ADDRESS_MASK:
                _new_itable(t, t.process.heap, port.gw_device)


@trap
def PixPatChanged(t, pp):
    pass


@trap
def PortChanged(t, port):
    pass


@trap
def GDeviceChanged(t, gdh):
    pass


# -- screen buffers -----------------------------------------------------------
def _new_screen_buffer(t, rect_p, gdh_p, pm_p, flags: int) -> int:
    devices = t.system.devices
    main = _main(t)
    if devices is None or main is None:
        return paramErr
    bounds = t.mem.r_rect(rect_p)
    if bounds.empty:
        return paramErr
    depth, heap = main.pixel_size, t.process.heap
    row_bytes = _row_bytes(bounds.width, depth)
    pix = _new_pixels(t, row_bytes * bounds.height, flags)
    pm_h = heap.new_handle(PIXMAP_SIZE, clear=True)
    if not pix or not pm_h:
        for h in (pix, pm_h):
            _dispose(t, h)
        return cNoMemErr
    write_pixmap(t.mem, heap.deref(pm_h), _deref(t, pix), row_bytes, bounds, depth,
                 main.pm_table)
    t.w32(gdh_p, devices.main)
    t.w32(pm_p, pm_h)
    return noErr


@trap
def NewScreenBuffer(t, rect_p, purgeable, gdh_p, pm_p):
    err = _new_screen_buffer(t, rect_p, gdh_p, pm_p, 0)
    _qd_error(t, err)
    return err


@trap
def NewTempScreenBuffer(t, rect_p, purgeable, gdh_p, pm_p):
    err = _new_screen_buffer(t, rect_p, gdh_p, pm_p, useTempMem)
    _qd_error(t, err)
    return err


@trap
def DisposeScreenBuffer(t, pm_h):
    """The pixels and the PixMap, not the colour table, the screen's."""
    p = _deref(t, pm_h)
    if p:
        _dispose(t, _pixels_handle(t, PixMap(t.mem, p)))
        _dispose(t, pm_h)
