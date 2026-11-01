"""
Dialog Manager data: the rest of a DialogRecord, the item list, and
ParamText's substitutions.

A dialog is a window whose record goes on past the WindowRecord's 156
bytes (Inside Macintosh I-408):

    items      156  the item list, a handle
    textH      160  TextEdit's record for the editText item being edited
    editField  164  that item's number less one; -1 when there's none
    editOpen   166  (the Dialog Manager's own)
    aDefItem   168  the default button's item number; 1 to begin with

The item list starts as a copy of a DITL resource: a count less one, then
for each item a placeholder, its display rectangle, its type and its
data.  Making the dialog fills the placeholders in: a ControlHandle for a
control, a handle to the text for static and edit text, the ICON or PICT
for an icon or picture, and (left to SetDialogItem) a procedure for a user
item.  Applications read and change it through GetDialogItem and
SetDialogItem, so it stays laid out as on a Mac.

On a colour machine an icon item whose ICON has a 'cicn' of the same ID
is that colour icon instead (Inside Macintosh: Macintosh Toolbox
Essentials, Finder Interface, "The Icon Resource"; Inside Macintosh: More
Macintosh Toolbox, Icon Utilities, "The Color Icon Record"): its handle is
the CIcon record GetCIcon makes of it, macrt's own choice of handle, which
a program can dispose of with DisposeCIcon.  An icon item whose handle is
a record GetCIcon made is drawn as a colour icon however it got there:
Odell Down Under disposes of its item's and sets one it makes itself.

A colour dialog or alert (one with a 'dctb' or 'actb') can have an item
colour table, the 'ictb' of its item list's ID (Inside Macintosh V, the
Dialog Manager): for each item two words, itemCData and itemCOffset.  For
a statText or editText item itemCData's bits say which fields of the
20-byte text style record at itemCOffset to use: diFont, diStyle (the
face in its high byte), diSize, the foreground and background RGB colours
and diMode, with bit 15 saying diFont is the offset of the font's name.
For a control it's the length of the colour table at itemCOffset, laid out
as a 'cctb'.  Zeros for an item with neither.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from ..memory import ADDRESS_MASK, BYTE, INTEGER, LONG, RAM, RECT, Field, Record
from .templates import ctrlItem, editText, itemDisable, statText
from .types import Rect

SIZE = 170                  # a DialogRecord
HIDDEN = 16384              # HideDialogItem moves an item this far right
ALERT_ICON = Rect(10, 20, 42, 52)   # where StopAlert, NoteAlert and CautionAlert put theirs
stopIcon, noteIcon, cautionIcon = 0, 1, 2


class Dialog(Record):
    """The fields after the window's, at the same address."""
    SIZE = SIZE

    items = Field(156, LONG)
    text_h = Field(160, LONG)
    edit_field = Field(164, INTEGER)
    edit_open = Field(166, INTEGER)
    a_def_item = Field(168, INTEGER)


@dataclass
class DialogState:
    """What the Dialog Manager keeps about a dialog outside its record."""
    cancel_item: int = 0        # SetDialogCancelItem's; Escape and Command-period press it
    outline: bool = False       # draw the bold outline around the default button
    icon: int = -1              # an alert's icon, ICON 0, 1 or 2
    tracks_cursor: bool = False
    item_colors: bytes = b""    # the 'ictb', if it's a colour dialog with one


# itemCData's bits for a text item: TESetStyle's mode word's, and the
# Dialog Manager's own (Inside Macintosh V, the Dialog Manager)
doFont, doFace, doSize, doColor, doAddSize = 1, 2, 4, 8, 16
doBColor, doMode, doFontName = 0x2000, 0x4000, 0x8000


@dataclass
class TextStyle:
    """A text item's style in an item colour table: which of the fields
    to use, and the fields."""
    flags: int
    font: int
    face: int
    size: int
    fore: tuple[int, int, int]
    back: tuple[int, int, int]
    mode: int
    font_name: bytes | None = None


def _item_color(ictb: bytes, number: int) -> tuple[int, int]:
    """Item number's itemCData and itemCOffset; zeros past the table's end."""
    at = 4 * (number - 1)
    if number < 1 or at + 4 > len(ictb):
        return 0, 0
    return struct.unpack_from(">HH", ictb, at)


def text_style(ictb: bytes, number: int) -> TextStyle | None:
    """A text item's style, None if it has none."""
    flags, at = _item_color(ictb, number)
    if not flags or at + 20 > len(ictb):
        return None
    font, size = struct.unpack_from(">h", ictb, at)[0], struct.unpack_from(">h", ictb, at + 4)[0]
    fore = struct.unpack_from(">HHH", ictb, at + 6)
    back = struct.unpack_from(">HHH", ictb, at + 12)
    mode = struct.unpack_from(">h", ictb, at + 18)[0]
    name = None
    if flags & doFontName and 0 <= font < len(ictb):
        name = ictb[font + 1:font + 1 + ictb[font]]
    return TextStyle(flags, font, ictb[at + 2], size, fore, back, mode, name)


def control_colors(ictb: bytes, number: int) -> bytes | None:
    """A control item's colour table, None if it has none: as long as its
    ctSize says (the length word of Inside Macintosh V's own example
    counts more than its table)."""
    length, at = _item_color(ictb, number)
    if not length or not at or at + 8 > len(ictb):
        return None
    size = struct.unpack_from(">h", ictb, at + 6)[0]
    end = at + 8 + 8 * (size + 1)
    return ictb[at:end] if size >= 0 and end <= len(ictb) else None


class _Field:
    """An item's field, read through the item list's handle."""

    def __init__(self, offset: int, codec):
        self.offset = offset
        self.codec = codec

    def __get__(self, item, owner=None):
        if item is None:
            return self
        return self.codec.read(item.mem, item.addr + self.offset)

    def __set__(self, item, value) -> None:
        self.codec.write(item.mem, item.addr + self.offset, value)


class Item:
    """One entry in an item list.  Its place in the list's block is kept,
    not its address, since the block can move."""

    handle = _Field(0, LONG)            # or a user item's procedure
    rect = _Field(4, RECT)
    kind = _Field(12, BYTE)             # the type, with itemDisable
    length = _Field(13, BYTE)

    def __init__(self, mem: RAM, list_handle: int, offset: int, number: int):
        self.mem = mem
        self.list_handle = list_handle
        self.offset = offset
        self.number = number            # from 1

    @property
    def addr(self) -> int:
        return (self.mem.r32(self.list_handle) & ADDRESS_MASK) + self.offset

    @property
    def type(self) -> int:
        return self.kind & ~itemDisable

    @property
    def enabled(self) -> bool:
        return not self.kind & itemDisable

    @property
    def is_control(self) -> bool:
        return self.type & ~3 == ctrlItem

    @property
    def is_text(self) -> bool:
        return self.type in (statText, editText)

    @property
    def hidden(self) -> bool:
        return self.rect.left >= HIDDEN

    @property
    def data(self) -> bytes:
        return self.mem.read(self.addr + 14, self.length)

    def __repr__(self) -> str:
        return f"<Item {self.number} type {self.type} {self.rect}>"


def items(mem: RAM, list_handle: int) -> list[Item]:
    """The items in an item list, item 1 first."""
    h = list_handle & ADDRESS_MASK
    p = mem.r32(h) & ADDRESS_MASK if h else 0
    if not p:
        return []
    n = (mem.r16(p) + 1) & 0xFFFF
    out, at = [], 2
    for number in range(1, n + 1):
        out.append(Item(mem, h, at, number))
        length = mem.r8(p + at + 13)
        at += 14 + length + (length & 1)
    return out


def substitute(text: bytes, params: list[bytes]) -> bytes:
    """Static text with ParamText's strings put in for ^0 to ^3, each in
    turn through the whole text, so that one string can hold a later one's
    ^n: MONOPOLY (B&W) gives its alert's message as ^0, its name in it as ^2."""
    for n, s in enumerate(params):
        text = text.replace(b"^%d" % n, s)
    return text
