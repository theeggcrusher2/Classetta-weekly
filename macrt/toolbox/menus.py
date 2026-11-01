"""
Menu Manager data: menus and items, the menu bar, and AppendMenu's
metacharacter parsing.

An application's menus live in its heap as MenuInfo records (Inside
Macintosh I-346), reached through handles: the menu ID, its size, the
MDEF, the enable flags, then menuData, which is the title followed by the
items.  The menu bar is the MenuList, a handle in the application's
MenuList global with one entry per menu (Inside Macintosh V-245).

The System 7 Application menu isn't the application's: the bar shows a
plain Menu built on the Python side, and the System has a MenuInfo with
the same items, in its heap, that programs find in their menu lists.  The
Help menu is a MenuInfo in the application's heap, its own copy of it
that HMGetHelpMenuHandle gives it to append its items to.

With Color QuickDraw, the menu colour table in MenuCInfo gives the bar,
the titles and the items colours, which the host draws them in.

A title can be an icon suite's: five bytes, $01 and the suite's handle,
which the bar shows as the suite's small icon (Macintosh Technical Q&A
TB 06, "Creating a Menu with an Icon as its Title").
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Iterator

from ..memory import ADDRESS_MASK, from_mac, to_mac
from .constants import APPLE_MENU_TITLE, hMenuCmd, hierMenu
from .lowmem import MenuCInfo as MENU_C_INFO, MenuList as MENU_LIST, World
from .memmgr import Heap

if TYPE_CHECKING:
    from .system import System

STYLE_CODES = {"B": 0x01, "I": 0x02, "U": 0x04, "O": 0x08, "S": 0x10}

# The Help menu's standard items, About Balloon Help and Show Balloons
# (kHMAboutHelpItem 1, kHMShowBalloonsItem 3), with a line between; then
# the line the Help Manager puts before the items an application appends
# (Inside Macintosh: More Macintosh Toolbox, 3-83).  macrt shows no
# balloons, so the two are dimmed.
HELP_ITEMS = "(About Balloon Help\u2026;(-;(Show Balloons;(-"
HELP_ITEM_COUNT = 4


@dataclass
class MenuItem:
    text: str
    icon: int = 0
    cmd: str = ""
    mark: str = ""
    style: int = 0
    enabled: bool = True

    @property
    def is_separator(self) -> bool:
        return self.text.startswith("-")

    @property
    def shown(self) -> str:
        """The text as the menu shows it: desk accessories' names start
        with a NUL, which the Mac's fonts draw as nothing."""
        return self.text.replace("\0", "")

    @property
    def key(self) -> str:
        """The Command-key equivalent: cmd, unless it's one of the codes
        below $20 that mean something else."""
        return self.cmd if self.cmd >= " " else ""

    @property
    def submenu(self) -> int:
        """The ID of the menu a hierarchical item opens, 0 for others."""
        return to_mac(self.mark)[0] if self.cmd == hMenuCmd and self.mark else 0


class MenuBase:
    """What the menu bar needs from a menu: id, title, items, enabled."""

    own_mdef = False
    title_suite = 0

    @property
    def is_apple(self) -> bool:
        return self.title == APPLE_MENU_TITLE

    def item(self, index: int) -> MenuItem | None:
        """1-based, like the Menu Manager."""
        items = self.items
        if 1 <= index <= len(items):
            return items[index - 1]
        return None

    def selectable(self, index: int) -> bool:
        it = self.item(index)
        return bool(self.enabled and it and it.enabled and not it.is_separator)


@dataclass(eq=False)
class Menu(MenuBase):
    id: int
    title: str
    items: list[MenuItem] = field(default_factory=list)
    enabled: bool = True


def parse_menu_items(data: str) -> list[MenuItem]:
    """Parse AppendMenu item text: ; or CR separate items, ^ icon, ! mark,
    < style, / command key, ( disabled.  "(-" is a separator line."""
    items = []
    for chunk in data.replace("\r", ";").split(";"):
        if not chunk:
            continue
        item = MenuItem("")
        text = []
        i = 0
        while i < len(chunk):
            c = chunk[i]
            nxt = chunk[i + 1] if i + 1 < len(chunk) else ""
            if c == "^" and nxt:
                item.icon = ord(nxt) - ord("0")
                i += 2
            elif c == "!" and nxt:
                item.mark = nxt
                i += 2
            elif c == "<" and nxt:
                item.style |= STYLE_CODES.get(nxt.upper(), 0)
                i += 2
            elif c == "/" and nxt:
                item.cmd = nxt
                i += 2
            elif c == "(":
                item.enabled = False
                i += 1
            else:
                text.append(c)
                i += 1
        item.text = "".join(text)
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# MenuInfo
# ---------------------------------------------------------------------------
MENU_ID = 0
MENU_WIDTH = 2
MENU_HEIGHT = 4
MENU_PROC = 6
ENABLE_FLAGS = 10
MENU_DATA = 14
LAST_FLAGGED_ITEM = 31      # items past this can't be disabled


def _char(b: int) -> str:
    return from_mac(bytes([b])) if b else ""


def _byte(s: str) -> int:
    return to_mac(s)[0] if s else 0


PARSE_CHUNK = 1024


def _parse_menu_data(data: bytes) -> tuple[bytes, list[tuple[bytes, bytes]]] | None:
    """MenuInfo._parse from menuData's first bytes; None if it goes on
    past them."""
    n, p = len(data), 1 + data[0]
    title = data[1:p]
    items = []
    while p < n and data[p]:
        q = p + 1 + data[p]
        if q + 4 > n:
            return None
        items.append((data[p + 1:q], data[q:q + 4]))
        p = q + 4
    return (title, items) if p < n else None


class MenuInfo(MenuBase):
    """A view of a MenuInfo record through its handle.  The record may
    move, so every access goes through the master pointer."""

    def __init__(self, system: System, handle: int):
        self.system = system
        self.mem = system.mem
        self.handle = handle & ADDRESS_MASK

    @classmethod
    def new(cls, system: System, heap: Heap, menu_id: int, title: str,
            mdef: int = 0) -> MenuInfo | None:
        """NewMenu: no items, everything enabled, mdef or the System's
        MDEF 0 to define it."""
        data = to_mac(title)[:255]
        h = heap.new_handle(MENU_DATA + 1 + len(data) + 1, clear=True)
        if not h:
            return None
        menu = cls(system, h)
        p = menu.ptr
        system.mem.w16(p + MENU_ID, menu_id)
        system.mem.w32(p + MENU_PROC, mdef or system.defproc("MDEF", 0))
        system.mem.w32(p + ENABLE_FLAGS, 0xFFFFFFFF)
        system.mem.w_pstr(p + MENU_DATA, data)
        return menu

    def dispose(self) -> None:
        self.system.heap_of(self.handle).dispose_handle(self.handle)

    @property
    def ptr(self) -> int:
        return self.mem.r32(self.handle) & ADDRESS_MASK

    @property
    def id(self) -> int:
        return self.mem.r16s(self.ptr + MENU_ID)

    @property
    def menu_proc(self) -> int:
        return self.mem.r32(self.ptr + MENU_PROC)

    @property
    def own_mdef(self) -> bool:
        """True if menuProc is the application's own code, an MDEF of its
        own or code it put in a handle itself, rather than the System's
        MDEF 0, whose menus the host draws."""
        h = self.menu_proc & ADDRESS_MASK
        if not h or self.system.defproc_id(h)[0] == "MDEF":
            return False
        try:
            self.system.heap_of(h)
        except ValueError:
            # A handle of the application's own making, its master pointer
            # in its globals: the MacPaint demo's Tools and Patterns menus.
            p = self.mem.r32(h) & ADDRESS_MASK
            return bool(p) and not p & 1 and p < self.mem.size
        return True

    @property
    def size(self) -> tuple[int, int]:
        """menuWidth and menuHeight, as the MDEF last worked them out."""
        p = self.ptr
        return self.mem.r16s(p + MENU_WIDTH), self.mem.r16s(p + MENU_HEIGHT)

    @size.setter
    def size(self, size: tuple[int, int]) -> None:
        p = self.ptr
        self.mem.w16(p + MENU_WIDTH, size[0])
        self.mem.w16(p + MENU_HEIGHT, size[1])

    @property
    def enable_flags(self) -> int:
        return self.mem.r32(self.ptr + ENABLE_FLAGS)

    @enable_flags.setter
    def enable_flags(self, flags: int) -> None:
        self.mem.w32(self.ptr + ENABLE_FLAGS, flags)

    @property
    def enabled(self) -> bool:
        return bool(self.enable_flags & 1)

    @enabled.setter
    def enabled(self, on: bool) -> None:
        self.enable_flags = (self.enable_flags | 1) if on else (self.enable_flags & ~1)

    @property
    def title(self) -> str:
        return from_mac(self.mem.r_pstr(self.ptr + MENU_DATA))

    @property
    def title_suite(self) -> int:
        """The handle of the icon suite a title is, or 0 for a title of
        text."""
        raw = self.mem.r_pstr(self.ptr + MENU_DATA)
        return struct.unpack(">L", raw[1:])[0] if len(raw) == 5 and raw[0] == 1 else 0

    def contents(self, h: int) -> bytes | None:
        """What a handle holds, for the bar to find a title's icon in; None
        for one that's NIL, purged or not a handle."""
        h &= ADDRESS_MASK
        heap = self.system.find_heap(h)
        if heap is None:
            return None
        p = heap.deref(h) if heap.is_handle(h) else 0
        return self.mem.read(p, heap.handle_size(h)) if p else None

    def _parse(self) -> tuple[bytes, list[tuple[bytes, bytes]]]:
        """menuData: the title, then (text, icon/key/mark/style) per item.
        From the bytes in one go, as long as they're in RAM and hold the
        whole of it, as they do but for a menu at the very end of RAM."""
        mem, p = self.mem, self.ptr + MENU_DATA
        n = PARSE_CHUNK
        while p + n <= mem.size and not p < mem.mirror_lo < p + n:
            parsed = _parse_menu_data(mem.read(p, n))
            if parsed is not None:
                return parsed
            n *= 16
        title = mem.r_pstr(p)
        p += 1 + len(title)
        items = []
        while mem.r8(p):
            text = mem.r_pstr(p)
            p += 1 + len(text)
            items.append((text, mem.read(p, 4)))
            p += 4
        return title, items

    @property
    def items(self) -> list[MenuItem]:
        """A snapshot.  Change items with set_items or change_item."""
        flags = self.enable_flags
        return [MenuItem(from_mac(text), icon, _char(key), _char(mark), style,
                         i > LAST_FLAGGED_ITEM or bool(flags >> i & 1))
                for i, (text, (icon, key, mark, style)) in enumerate(self._parse()[1], start=1)]

    def contents_key(self) -> tuple:
        """The title and items, as a key that's the same for menus that
        hold the same ones, whatever is enabled."""
        title, items = self._parse()
        return title, tuple(items)

    def count(self) -> int:
        """CountMItems"""
        return len(self._parse()[1])

    def _menu_data(self, items: list[MenuItem]) -> tuple[bytes, int]:
        """menuData, the title and these items, and enableFlags."""
        title = self._parse()[0]
        data = bytearray([len(title)]) + title
        flags = self.enable_flags
        for i, it in enumerate(items, start=1):
            text = to_mac(it.text)[:255]
            data += bytes([len(text)]) + text
            data += bytes([it.icon & 0xFF, _byte(it.cmd), _byte(it.mark), it.style & 0xFF])
            if i <= LAST_FLAGGED_ITEM:
                flags = (flags | 1 << i) if it.enabled else (flags & ~(1 << i))
        data.append(0)
        return bytes(data), flags

    def set_items(self, items: list[MenuItem]) -> bool:
        data, flags = self._menu_data(items)
        heap = self.system.heap_of(self.handle)
        if not heap.set_handle_size(self.handle, MENU_DATA + len(data)):
            return False
        self.mem.write(self.ptr + MENU_DATA, data)
        self.enable_flags = flags
        return True

    def append(self, items: list[MenuItem]) -> bool:
        return self.set_items(self.items + items)

    def _changed(self, index: int, changes: dict) -> list[MenuItem] | None:
        items = self.items
        if not 1 <= index <= len(items):
            return None
        for name, value in changes.items():
            setattr(items[index - 1], name, value)
        return items

    def change_item(self, index: int, **changes) -> bool:
        items = self._changed(index, changes)
        return items is not None and self.set_items(items)

    def changes_item(self, index: int, **changes) -> bool:
        """Whether change_item would change the menu: CheckItem of an
        item that's checked already doesn't."""
        items = self._changed(index, changes)
        if items is None:
            return False
        data, flags = self._menu_data(items)
        heap = self.system.find_heap(self.handle)
        return (heap is None or heap.handle_size(self.handle) != MENU_DATA + len(data)
                or flags != self.enable_flags
                or self.mem.read(self.ptr + MENU_DATA, len(data)) != data)

    def __eq__(self, other) -> bool:
        return isinstance(other, MenuInfo) and other.handle == self.handle

    def __hash__(self) -> int:
        return hash(self.handle)

    def __repr__(self) -> str:
        return f"<MenuInfo {self.id} {self.title!r} handle=${self.handle:06X}>"


# ---------------------------------------------------------------------------
# MenuList: lastMenu, lastRight, mbResID, then (menuOH, menuLeft) per menu;
# then the hierarchical section: lastHMenu, menuTitleSave, and (menuHOH,
# reserved) per submenu (Inside Macintosh V, the Menu Manager).  lastHMenu
# is "the offset to the last hierarchical menu", which macrt counts from
# lastHMenu itself, so an empty section's is 0: MacApp's walk through the
# menus (Hodgepodge(Life)) reads lastHMenu / 6 submenus after menuTitleSave.
# ---------------------------------------------------------------------------
LIST_HEADER = 6
LIST_ENTRY = 6
LIST_MBDF_ID = 4            # mbResID: the MBDF, its variant in the low three bits
HIER_HEADER = 6             # lastHMenu, menuTitleSave


def _hier_section(mem, p: int) -> int:
    return p + LIST_HEADER + mem.r16(p)


def read_menu_list(mem, h: int) -> list[int]:
    """The menu handles in a menu list, in order."""
    h &= ADDRESS_MASK
    if not h:
        return []
    p = mem.r32(h) & ADDRESS_MASK
    n = mem.r16(p) // LIST_ENTRY
    return [mem.r32(p + LIST_HEADER + LIST_ENTRY * i) & ADDRESS_MASK for i in range(n)]


def read_hier_list(system: System, h: int) -> list[int]:
    """The submenus' handles in a menu list's hierarchical section, which
    a list a program made itself may not have."""
    h &= ADDRESS_MASK
    if not h:
        return []
    mem = system.mem
    p = mem.r32(h) & ADDRESS_MASK
    sect = _hier_section(mem, p)
    end = p + system.heap_of(h).handle_size(h)
    if sect + HIER_HEADER > end:
        return []
    n = mem.r16(sect) // LIST_ENTRY
    n = min(n, (end - sect - HIER_HEADER) // LIST_ENTRY)
    return [mem.r32(sect + HIER_HEADER + LIST_ENTRY * i) & ADDRESS_MASK for i in range(n)]


def read_mbdf_id(mem, h: int) -> int:
    h &= ADDRESS_MASK
    return mem.r16s((mem.r32(h) & ADDRESS_MASK) + LIST_MBDF_ID) if h else 0


def write_menu_list(system: System, h: int, handles: list[int], mbdf_id: int = 0,
                    hier: list[int] = ()) -> bool:
    size = LIST_HEADER + LIST_ENTRY * len(handles) + HIER_HEADER + LIST_ENTRY * len(hier)
    if not h or not system.heap_of(h).set_handle_size(h, size):
        return False
    mem = system.mem
    p = mem.r32(h) & ADDRESS_MASK
    mem.fill(p, size)
    mem.w16(p, LIST_ENTRY * len(handles))
    mem.w16(p + LIST_MBDF_ID, mbdf_id)
    for i, mh in enumerate(handles):
        mem.w32(p + LIST_HEADER + LIST_ENTRY * i, mh)
    sect = _hier_section(mem, p)
    mem.w16(sect, LIST_ENTRY * len(hier))
    for i, mh in enumerate(hier):
        mem.w32(sect + HIER_HEADER + LIST_ENTRY * i, mh)
    return True


def new_menu_list(system: System, heap: Heap, handles: list[int], mbdf_id: int = 0,
                  hier: list[int] = ()) -> int:
    """A menu list that isn't the menu bar (yet): GetNewMBar, GetMenuBar."""
    h = heap.new_handle(0)
    if h and not write_menu_list(system, h, handles, mbdf_id, hier):
        heap.dispose_handle(h)
        return 0
    return h


class MenuList:
    """An application's menu bar, in InsertMenu order."""

    def __init__(self, system: System, world: World):
        self.system = system
        self.mem = system.mem
        self.world = world
        # System 7's own menus, the Help menu once there is one and the
        # Application menu, come after the application's in the list,
        # whatever it does to those: a program that looks through the list
        # for an item finds them (HyperCard 2's doMenu "Hide Others").
        self.system_menus: list[MenuInfo] = []

    def init(self, heap: Heap) -> None:
        """InitMenus: an empty menu bar."""
        self.world.w32(MENU_LIST, new_menu_list(self.system, heap, []))

    @property
    def handle(self) -> int:
        return self.world.r32(MENU_LIST) & ADDRESS_MASK

    def handles(self) -> list[int]:
        return read_menu_list(self.mem, self.handle)

    @property
    def mbdf_id(self) -> int:
        """InitProcMenu's MBDF ID (0 is the System's)."""
        return read_mbdf_id(self.mem, self.handle)

    @mbdf_id.setter
    def mbdf_id(self, mbdf_id: int) -> None:
        self.mem.w16((self.mem.r32(self.handle) & ADDRESS_MASK) + LIST_MBDF_ID, mbdf_id)

    def hier_handles(self) -> list[int]:
        return read_hier_list(self.system, self.handle)

    def submenus(self) -> list[MenuInfo]:
        """The menus in the hierarchical section, InsertMenu(hierMenu)'s."""
        return [MenuInfo(self.system, h) for h in self.hier_handles()]

    def _write(self, handles: list[int], mbdf_id: int | None = None,
               hier: list[int] | None = None) -> None:
        system = [m.handle for m in self.system_menus]
        write_menu_list(self.system, self.handle, [h for h in handles if h not in system] + system,
                        self.mbdf_id if mbdf_id is None else mbdf_id,
                        self.hier_handles() if hier is None else hier)

    def add_system(self, menu: MenuInfo, first: bool = False) -> None:
        """One of the System's menus, at the end of the list or before the
        others (the Help menu)."""
        if menu not in self.system_menus:
            self.system_menus.insert(0 if first else len(self.system_menus), menu)
            self._write(self.handles())

    def own(self) -> list[MenuInfo]:
        """The application's menus, without the System's."""
        return [m for m in self if m not in self.system_menus]

    def __iter__(self) -> Iterator[MenuInfo]:
        for h in self.handles():
            yield MenuInfo(self.system, h)

    def __len__(self) -> int:
        return len(self.handles())

    def __contains__(self, menu: MenuInfo) -> bool:
        return menu.handle in self.handles()

    def get(self, menu_id: int) -> MenuInfo | None:
        """GetMHandle: a menu in the bar or a submenu."""
        return next((m for m in [*self, *self.submenus()] if m.id == menu_id), None)

    def offset(self, menu_id: int) -> int:
        """Where the menu's entry is in the list, which is how the MBDF
        is told which menu; 0 if it isn't there."""
        ids = [m.id for m in self]
        return LIST_HEADER + LIST_ENTRY * ids.index(menu_id) if menu_id in ids else 0

    def insert(self, menu: MenuInfo, before_id: int = 0) -> None:
        """InsertMenu: before the menu with before_id, or at the end; with
        hierMenu, among the submenus.  Nothing if a menu with its ID is in
        the list already."""
        if self.get(menu.id) is not None:
            return
        if before_id == hierMenu:
            self._write(self.handles(), hier=self.hier_handles() + [menu.handle])
            return
        menus = list(self)
        ids = [m.id for m in menus]
        at = ids.index(before_id) if before_id in ids else len(menus)
        handles = [m.handle for m in menus]
        handles.insert(at, menu.handle)
        self._write(handles)

    def delete(self, menu_id: int) -> None:
        self._write([m.handle for m in self if m.id != menu_id],
                    hier=[m.handle for m in self.submenus() if m.id != menu_id])

    def clear(self) -> None:
        self._write([], hier=[])

    def set_from(self, list_handle: int) -> None:
        """SetMenuBar: the menu bar becomes a copy of the given list."""
        self._write(read_menu_list(self.mem, list_handle), read_mbdf_id(self.mem, list_handle),
                    read_hier_list(self.system, list_handle))

    def copy(self, heap: Heap) -> int:
        """GetMenuBar: a copy of the menu bar as a new list."""
        return new_menu_list(self.system, heap, self.handles(), self.mbdf_id, self.hier_handles())


# -- the menu colour table ------------------------------------------------------
# The Color Menu Manager's colours (Inside Macintosh V, the Menu Manager):
# a handle in MenuCInfo to entries of a menu ID, an item and four colours,
# 30 bytes each, ending with an entry whose ID is mctLastIDIndic.  A menu's
# item 0 is its title, and menu 0's item 0 is the menu bar.  The colours
# are, for the bar: the titles', the menus' background, the items' and the
# bar's; for a title: its own, its menu's background, its items' and the
# bar's behind it; for an item: its mark's, its text's, its Command key's
# and its background.
MCT_ENTRY = 30
mctLastIDIndic = -99
mctAllItems = -98

RGB = tuple[int, int, int]
_BLACK: RGB = (0, 0, 0)
_WHITE: RGB = (0xFFFF, 0xFFFF, 0xFFFF)


def mct_entry(menu_id: int, item: int, colors: list[RGB]) -> bytes:
    return struct.pack(">hh12Hh", menu_id, item, *(c for rgb in colors for c in rgb), 0)


def mct_key(entry: bytes) -> tuple[int, int]:
    return struct.unpack_from(">hh", entry)


def mct_entries(system: System, h: int) -> list[bytes]:
    """The table's entries, but its last."""
    h &= ADDRESS_MASK
    heap = system.find_heap(h) if h else None
    p = heap.deref(h) if heap is not None else 0
    if not p:
        return []
    out = []
    for at in range(p, p + heap.handle_size(h) - MCT_ENTRY + 1, MCT_ENTRY):
        e = system.mem.read(at, MCT_ENTRY)
        if mct_key(e)[0] == mctLastIDIndic:
            break
        out.append(e)
    return out


def write_mct(system: System, h: int, entries: list[bytes]) -> bool:
    """Make the table h these entries, and its last."""
    heap = system.heap_of(h)
    data = b"".join(entries) + mct_entry(mctLastIDIndic, 0, [_BLACK] * 4)
    if not heap.set_handle_size(h, len(data)):
        return False
    system.mem.write(heap.deref(h), data)
    return True


def menu_color_table(system: System, world: World, heap: Heap | None = None) -> int:
    """MenuCInfo's table; with a heap, made there, empty, if there's none."""
    h = world.r32(MENU_C_INFO) & ADDRESS_MASK
    if h or heap is None:
        return h
    h = heap.new_handle(0)
    if not h or not write_mct(system, h, []):
        return 0
    world.w32(MENU_C_INFO, h)
    return h


def set_mct_entries(system: System, world: World, heap: Heap, entries: list[bytes]) -> bool:
    """SetMCEntries: each entry takes the place of the table's for its menu
    and item, or is added at the end."""
    h = menu_color_table(system, world, heap)
    if not h:
        return False
    table = mct_entries(system, h)
    keys = [mct_key(e) for e in table]
    for e in entries:
        key = mct_key(e)
        if key[0] == mctLastIDIndic:
            continue
        if key in keys:
            table[keys.index(key)] = e
        else:
            table.append(e)
            keys.append(key)
    return write_mct(system, h, table)


def mctb_entries(data: bytes) -> list[bytes]:
    """An 'mctb' resource's entries: a count, then the entries."""
    n = int.from_bytes(data[:2], "big") if len(data) >= 2 else 0
    n = min(n, (len(data) - 2) // MCT_ENTRY)
    return [data[2 + i * MCT_ENTRY:2 + (i + 1) * MCT_ENTRY] for i in range(n)]


class MenuColors:
    """The colours a menu colour table gives the menu bar, its titles and
    the menus' items: an entry's own, else its menu's title's, else the
    bar's, else black on white."""

    def __init__(self, entries: list[bytes]):
        self._colors = {}
        for e in entries:
            v = struct.unpack_from(">12H", e, 4)
            self._colors.setdefault(mct_key(e), [tuple(v[i:i + 3]) for i in (0, 3, 6, 9)])

    def __bool__(self) -> bool:
        return bool(self._colors)

    def _get(self, *places: tuple[int, int, int]) -> RGB | None:
        for menu_id, item, i in places:
            c = self._colors.get((menu_id, item))
            if c is not None:
                return c[i]
        return None

    def bar(self) -> RGB:
        return self._get((0, 0, 3)) or _WHITE

    def title(self, menu_id: int) -> tuple[RGB, RGB]:
        """A title's colour and the bar's behind it.  A title's entry has
        those two first, then its items' and its menu's background (where
        the bar's has the titles', the menus' background, the items' and
        then the bar's)."""
        return (self._get((menu_id, 0, 0), (0, 0, 0)) or _BLACK,
                self._get((menu_id, 0, 1), (0, 0, 3)) or _WHITE)

    def background(self, menu_id: int) -> RGB:
        return self._get((menu_id, 0, 3), (0, 0, 1)) or _WHITE

    def item(self, menu_id: int, item: int) -> tuple[RGB, RGB, RGB, RGB]:
        """An item's mark's, text's and Command key's colours, and its
        background."""
        text = self._get((menu_id, item, 1), (menu_id, 0, 2), (0, 0, 2)) or _BLACK
        mark = self._get((menu_id, item, 0)) or text
        key = self._get((menu_id, item, 2)) or text
        back = self._get((menu_id, item, 3), (menu_id, 0, 3), (0, 0, 1)) or _WHITE
        return mark, text, key, back
