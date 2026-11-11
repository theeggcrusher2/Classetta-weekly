"""
The Apple Event Manager (Inside Macintosh VI, chapter 6), for an
application and the system and the application's messages to itself.

A descriptor is what it is on a Mac: a type and a handle in the
application's heap.  The data of a list, a record or an Apple event is
macrt's own, kept in that handle, so that copying, disposing and
coercing one works as it does on any other descriptor: a tag, then an
Apple event's attributes, then the items, each a keyword, a type and its
data.  Programs don't look inside; on a Mac the format is private too.

An application whose SIZE resource says it's high-level-event aware is
opened with an Open Application event, as System 7's Finder opens one
without documents: a high-level event in its queue, which
AEProcessAppleEvent takes to the handler the application installed.
AESend reaches the application's own handlers, which is how recordable
programs carry out their menu commands; there are no other targets.
"""

from __future__ import annotations

import itertools
import re
import struct
from collections import deque
from dataclasses import dataclass, field
from typing import Callable
from weakref import WeakKeyDictionary

from ..memory import to_mac
from ..process import size_flags
from ..toolbox.constants import kHighLevelEvent
from ..toolbox.events import EventRecord
from ..toolbox.files import paramErr
from ..toolbox.memmgr import memFullErr, noErr
from ..toolbox.types import Point
from .dispatch import trap

errAECoercionFail = -1700
errAEDescNotFound = -1701
errAEWrongDataType = -1703
errAENotAEDesc = -1704
errAENotAppleEvent = -1707
errAEEventNotHandled = -1708
errAEHandlerNotFound = -1717
errAEIllegalIndex = -1719
errAEAccessorNotFound = -1723
errAENotAnObjSpec = -1727
procNotFound = -600

typeNull, typeWildCard = b"null", b"****"
typeAEList, typeAERecord, typeAppleEvent = b"list", b"reco", b"aevt"
typeProcessSerialNumber, typeApplSignature = b"psn ", b"sign"
typeType, typeChar, typeTrue, typeFalse = b"type", b"TEXT", b"true", b"fals"
typeShortInteger, typeLongInteger, typeBoolean = b"shor", b"long", b"bool"
NUMBERS = {typeShortInteger: ">h", typeLongInteger: ">l", b"magn": ">L", typeBoolean: ">B"}
CODES = {b"enum", typeType, b"keyw", b"prop"}           # four-character codes, all alike
DECIMAL = re.compile(rb" *[-+]?[0-9]+ *")               # text that coerces to a number
# More of the system's own coercions, (from, to): fn(t, data) -> the data
# coerced, or None.  Other modules add theirs: glue_aliases, an alias's.
COERCIONS: dict[tuple[bytes, bytes], Callable[..., bytes | None]] = {}

kCoreEventClass, kAEOpenApplication, kAEAnswer = b"aevt", b"oapp", b"ansr"
kAEOpenDocuments, keyDirectObject, typeFSS = b"odoc", b"----", b"fss "
keyEventClassAttr, keyEventIDAttr, keyAddressAttr = b"evcl", b"evid", b"addr"
keyReturnIDAttr, keyTransactionIDAttr, keyEventSourceAttr = b"rtid", b"tran", b"esrc"
keyErrorNumber = b"errn"
typeObjectSpecifier = b"obj "
keyAEDesiredClass, keyAEContainer, keyAEKeyForm, keyAEKeyData = b"want", b"from", b"form", b"seld"
formTest, formWhose = b"test", b"whos"
kAESameProcess, kAELocalProcess = 2, 3
kAEWaitReply = 3                        # sendMode's low two bits: no reply 1, queue it 2, wait 3
kAutoGenerateReturnID = -1
kAEUseStandardDispatch = -1
kCurrentProcess = 2
kAEInteractWithLocal = 1
isHighLevelEventAware = 0x0040          # in the SIZE resource's flags
DESC = 8                                # an AEDesc: descriptorType, dataHandle
HLE_MSG = 36                            # a HighLevelEventMsg
TARGET_ID = 252                         # a TargetID
NO_KEY = bytes(4)                       # a list item's keyword

TAGS = {typeAEList: b"AEls", typeAERecord: b"AErc", typeAppleEvent: b"AEev"}
KINDS = {tag: kind for kind, tag in TAGS.items()}


def _code(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "big")


@dataclass
class Composite:
    """A list's, a record's or an Apple event's data: [keyword, type,
    data] entries, the attributes being an Apple event's."""
    kind: bytes
    items: list = field(default_factory=list)
    attrs: list = field(default_factory=list)

    def flatten(self) -> bytes:
        out = [TAGS[self.kind]]
        for entries in (self.attrs, self.items):
            out.append(struct.pack(">I", len(entries)))
            for key, dtype, data in entries:
                out += [key, dtype, struct.pack(">I", len(data)), data, bytes(len(data) & 1)]
        return b"".join(out)

    @classmethod
    def parse(cls, data: bytes) -> Composite | None:
        kind = KINDS.get(data[:4])
        if kind is None:
            return None
        c, at = cls(kind), 4
        try:
            for entries in (c.attrs, c.items):
                n, = struct.unpack_from(">I", data, at)
                at += 4
                for _ in range(n):
                    key, dtype, size = struct.unpack_from(">4s4sI", data, at)
                    if at + 12 + size > len(data):
                        return None
                    entries.append([key, dtype, data[at + 12:at + 12 + size]])
                    at += 12 + size + (size & 1)
        except struct.error:
            return None
        return c

    def find(self, key: bytes, attr: bool = False) -> list | None:
        return next((e for e in (self.attrs if attr else self.items) if e[0] == key), None)

    def put(self, key: bytes, dtype: bytes, data: bytes, attr: bool = False) -> None:
        e = self.find(key, attr)
        if e is not None:
            e[1:] = [dtype, data]
        else:
            (self.attrs if attr else self.items).append([key, dtype, data])


@dataclass
class AEState:
    """One application's handlers, keyed (isSysHandler, ...), and its
    Apple events."""
    handlers: dict = field(default_factory=dict)        # (class, ID): (proc, refcon)
    coercions: dict = field(default_factory=dict)       # (from, to): (proc, refcon, fromIsDesc)
    specials: dict = field(default_factory=dict)        # function class: (proc,)
    accessors: dict = field(default_factory=dict)       # (class, container): (proc, refcon)
    object_callbacks: tuple = ()                        # AESetObjectCallbacks' seven
    pending: deque = field(default_factory=deque)       # (flattened event, its high-level event)
    current: list = field(default_factory=list)         # the events being handled, innermost last
    suspended: set = field(default_factory=set)         # their data handles
    interaction: int = kAEInteractWithLocal
    return_ids: itertools.count = field(default_factory=lambda: itertools.count(1))


_states: WeakKeyDictionary = WeakKeyDictionary()


def state(t, process=None) -> AEState:
    """An application's: the current one's, or process's."""
    process = t.process if process is None else process
    st = _states.get(process)
    if st is None:
        st = _states[process] = AEState()
    return st


# =========================================================================
# Descriptors
# =========================================================================
def read_desc(t, p: int) -> tuple[bytes, bytes] | None:
    """The type and data of the AEDesc at p; None if its handle isn't one."""
    dtype, h = _code(t.mem.r32(p)), t.mem.r32(p + 4) & t.mem.system.addr_mask
    if not h:
        return dtype, b""
    heap = t.system.find_heap(h)
    if heap is None:
        return None
    if heap.is_empty(h):
        return dtype, b""
    return dtype, t.mem.read(heap.deref(h), heap.handle_size(h))


def write_desc(t, p: int, dtype: bytes, data: bytes) -> int:
    """A new AEDesc at p, with its own handle."""
    h = 0
    if dtype != typeNull or data:
        h = t.process.heap.new_handle_with(data)
        if not h:
            dtype = typeNull
    t.w32(p, int.from_bytes(dtype, "big"))
    t.w32(p + 4, h)
    return noErr if h or dtype == typeNull and not data else memFullErr


def dispose_desc(t, p: int) -> None:
    h = t.mem.r32(p + 4) & t.mem.system.addr_mask
    heap = t.system.find_heap(h) if h else None
    if heap is not None:
        heap.dispose_handle(h)
    t.w32(p, int.from_bytes(typeNull, "big"))
    t.w32(p + 4, 0)


def _composite(t, p: int, *kinds: bytes) -> Composite | int:
    """The list, record or Apple event at p, or why it isn't one of kinds."""
    d = read_desc(t, p) if p else None
    if d is None:
        return errAENotAEDesc
    c = Composite.parse(d[1])
    if c is None or c.kind not in kinds:
        return errAEWrongDataType
    return c


def _store(t, p: int, c: Composite) -> int:
    """c back in the handle of the descriptor at p."""
    h = t.mem.r32(p + 4) & t.mem.system.addr_mask
    data = c.flatten()
    heap = t.system.heap_of(h)
    if not heap.set_handle_size(h, len(data)):
        return memFullErr
    t.mem.write(heap.deref(h), data)
    return noErr


def coerce(t, dtype: bytes, data: bytes, to: bytes) -> tuple[bytes, bytes] | None:
    """data, of type dtype, as type to: the Apple Event Manager's own
    coercions, then the application's; None if it can't be."""
    if to in (dtype, typeWildCard):
        return dtype, data
    c = Composite.parse(data)
    if c is not None:
        # A record can be given any type (an object specifier's, say),
        # and taken back.
        ok = c.kind == typeAERecord and to not in (typeAEList, typeAppleEvent)
        return (to, data) if ok else _coerce_by_handler(t, dtype, data, to)
    if to == typeAEList:
        return to, Composite(typeAEList, [[NO_KEY, dtype, data]]).flatten()
    if dtype in (typeTrue, typeFalse):
        dtype, data = typeBoolean, bytes([dtype == typeTrue])
    n = None
    if dtype in NUMBERS and len(data) == struct.calcsize(NUMBERS[dtype]):
        n = struct.unpack(NUMBERS[dtype], data)[0]
    elif dtype == typeChar and DECIMAL.fullmatch(data) \
            and len(data.strip(b" +-").lstrip(b"0")) <= 20:      # more fits no number
        n = int(data)
    if n is not None and to in NUMBERS and (to != typeBoolean or n in (0, 1)):
        try:
            return to, struct.pack(NUMBERS[to], n)
        except struct.error:
            pass
    if n is not None and to == typeChar and dtype != typeChar:
        return to, str(n).encode()
    if dtype in CODES and to in CODES and len(data) == 4:
        return to, data
    if (dtype, to) in COERCIONS:
        got = COERCIONS[dtype, to](t, data)
        if got is not None:
            return to, got
    return _coerce_by_handler(t, dtype, data, to)


def _coerce_by_handler(t, dtype: bytes, data: bytes, to: bytes) -> tuple[bytes, bytes] | None:
    st = state(t)
    for is_sys in (False, True):
        for key in ((is_sys, dtype, to), (is_sys, typeWildCard, to)):
            entry = st.coercions.get(key)
            if entry is not None:
                return _call_coercion(t, entry, dtype, data, to)
    return None


def _call_coercion(t, entry, dtype: bytes, data: bytes, to: bytes) -> tuple[bytes, bytes] | None:
    """A coercion handler's result: pascal OSErr fn(const AEDesc *from,
    DescType to, long refcon, AEDesc *result), or with the type, a
    pointer and a size in place of the descriptor."""
    proc, refcon, from_is_desc = entry
    heap = t.process.heap
    p = heap.new_ptr(2 * DESC + len(data))
    if not p:
        return None
    from_p, result, data_p = p, p + DESC, p + 2 * DESC
    t.mem.write(data_p, data)
    write_desc(t, result, typeNull, b"")
    to_l = int.from_bytes(to, "big")
    try:
        if from_is_desc:
            if write_desc(t, from_p, dtype, data) != noErr:
                return None
            err = t.call(proc, (("p", from_p), ("l", to_l), ("l", refcon), ("p", result)), "e")
            dispose_desc(t, from_p)
        else:
            err = t.call(proc, (("l", int.from_bytes(dtype, "big")), ("p", data_p),
                                ("l", len(data)), ("l", to_l), ("l", refcon), ("p", result)), "e")
        got = read_desc(t, result) if err == noErr else None
        dispose_desc(t, result)
        return got
    finally:
        heap.dispose_ptr(p)


def _give(t, got: tuple[bytes, bytes], type_p: int, data_p: int, max_size: int,
          actual_p: int) -> int:
    """What the Get...Ptr calls return: the type, as much of the data as
    there's room for, and how much there was."""
    dtype, data = got
    t.w32(type_p, int.from_bytes(dtype, "big"))
    if data_p and max_size > 0:
        t.mem.write(data_p, data[:max_size])
    t.w32(actual_p, len(data))
    return noErr


@trap
def AECreateDesc(t, dtype, ptr, size, result):
    return write_desc(t, result, _code(dtype), t.mem.read(ptr, max(size, 0)) if ptr else b"")


@trap
def AEDisposeDesc(t, p):
    dispose_desc(t, p)
    return noErr


@trap
def AEDuplicateDesc(t, p, result):
    d = read_desc(t, p)
    return write_desc(t, result, *d) if d else errAENotAEDesc


@trap
def AECoercePtr(t, dtype, ptr, size, to, result):
    got = coerce(t, _code(dtype), t.mem.read(ptr, max(size, 0)) if ptr else b"", _code(to))
    return write_desc(t, result, *got) if got else errAECoercionFail


@trap
def AECoerceDesc(t, p, to, result):
    d = read_desc(t, p)
    if d is None:
        return errAENotAEDesc
    got = coerce(t, *d, _code(to))
    return write_desc(t, result, *got) if got else errAECoercionFail


# =========================================================================
# Lists and records
# =========================================================================
@trap
def AECreateList(t, factoring, factored_size, is_record, result):
    kind = typeAERecord if is_record else typeAEList
    return write_desc(t, result, kind, Composite(kind).flatten())


ANY = (typeAEList, typeAERecord, typeAppleEvent)        # an Apple event's items are its parameters
KEYED = (typeAERecord, typeAppleEvent)


@trap
def AECountItems(t, p, count_p):
    c = _composite(t, p, *ANY)
    if isinstance(c, int):
        return c
    t.w32(count_p, len(c.items))
    return noErr


def _nth(t, p: int, index: int, desired: int) -> tuple[list, tuple[bytes, bytes]] | int:
    c = _composite(t, p, *ANY)
    if isinstance(c, int):
        return c
    if not 1 <= index <= len(c.items):
        return errAEIllegalIndex
    e = c.items[index - 1]
    got = coerce(t, e[1], e[2], _code(desired))
    return (e, got) if got else errAECoercionFail


@trap
def AEGetNthPtr(t, p, index, desired, key_p, type_p, data_p, max_size, actual_p):
    r = _nth(t, p, index, desired)
    if isinstance(r, int):
        return r
    t.w32(key_p, int.from_bytes(r[0][0], "big"))
    return _give(t, r[1], type_p, data_p, max_size, actual_p)


@trap
def AEGetNthDesc(t, p, index, desired, key_p, result):
    r = _nth(t, p, index, desired)
    if isinstance(r, int):
        return r
    t.w32(key_p, int.from_bytes(r[0][0], "big"))
    return write_desc(t, result, *r[1])


@trap
def AESizeOfNthItem(t, p, index, type_p, size_p):
    r = _nth(t, p, index, int.from_bytes(typeWildCard, "big"))
    if isinstance(r, int):
        return r
    return _give(t, r[1], type_p, 0, 0, size_p)


def _put_nth(t, p: int, index: int, item: tuple[bytes, bytes] | None) -> int:
    c = _composite(t, p, *ANY)
    if isinstance(c, int) or item is None:
        return c if isinstance(c, int) else errAENotAEDesc
    if index in (0, len(c.items) + 1):
        c.items.append([NO_KEY, *item])
    elif 1 <= index <= len(c.items):
        c.items[index - 1][1:] = item
    else:
        return errAEIllegalIndex
    return _store(t, p, c)


@trap
def AEPutPtr(t, p, index, dtype, ptr, size):
    return _put_nth(t, p, index, (_code(dtype), t.mem.read(ptr, max(size, 0)) if ptr else b""))


@trap
def AEPutDesc(t, p, index, desc):
    return _put_nth(t, p, index, read_desc(t, desc))


@trap
def AEDeleteItem(t, p, index):
    c = _composite(t, p, *ANY)
    if isinstance(c, int):
        return c
    if not 1 <= index <= len(c.items):
        return errAEIllegalIndex
    del c.items[index - 1]
    return _store(t, p, c)


# =========================================================================
# Parameters (a record's items) and attributes (an Apple event's)
# =========================================================================
def _keyed(t, p: int, key: int, desired: int, attr: bool) -> tuple[bytes, bytes] | int:
    c = _composite(t, p, *((typeAppleEvent,) if attr else KEYED))
    if isinstance(c, int):
        return c
    e = c.find(_code(key), attr)
    if e is None:
        return errAEDescNotFound
    return coerce(t, e[1], e[2], _code(desired)) or errAECoercionFail


def _put_keyed(t, p: int, key: int, item: tuple[bytes, bytes] | None, attr: bool) -> int:
    c = _composite(t, p, *((typeAppleEvent,) if attr else KEYED))
    if isinstance(c, int) or item is None:
        return c if isinstance(c, int) else errAENotAEDesc
    c.put(_code(key), *item, attr=attr)
    return _store(t, p, c)


def _get_ptr(t, p, key, desired, type_p, data_p, max_size, actual_p, attr=False) -> int:
    got = _keyed(t, p, key, desired, attr)
    return got if isinstance(got, int) else _give(t, got, type_p, data_p, max_size, actual_p)


def _get_desc(t, p, key, desired, result, attr=False) -> int:
    got = _keyed(t, p, key, desired, attr)
    return got if isinstance(got, int) else write_desc(t, result, *got)


def _size_of(t, p, key, type_p, size_p, attr=False) -> int:
    got = _keyed(t, p, key, int.from_bytes(typeWildCard, "big"), attr)
    return got if isinstance(got, int) else _give(t, got, type_p, 0, 0, size_p)


@trap
def AEPutParamPtr(t, p, key, dtype, ptr, size):
    data = t.mem.read(ptr, max(size, 0)) if ptr else b""
    return _put_keyed(t, p, key, (_code(dtype), data), False)


@trap
def AEPutParamDesc(t, p, key, desc):
    return _put_keyed(t, p, key, read_desc(t, desc), False)


@trap
def AEGetParamPtr(t, p, key, desired, type_p, data_p, max_size, actual_p):
    return _get_ptr(t, p, key, desired, type_p, data_p, max_size, actual_p)


@trap
def AEGetParamDesc(t, p, key, desired, result):
    return _get_desc(t, p, key, desired, result)


@trap
def AESizeOfParam(t, p, key, type_p, size_p):
    return _size_of(t, p, key, type_p, size_p)


@trap
def AEDeleteParam(t, p, key):
    c = _composite(t, p, *KEYED)
    if isinstance(c, int):
        return c
    e = c.find(_code(key))
    if e is None:
        return errAEDescNotFound
    c.items.remove(e)
    return _store(t, p, c)


@trap
def AEPutAttributePtr(t, p, key, dtype, ptr, size):
    data = t.mem.read(ptr, max(size, 0)) if ptr else b""
    return _put_keyed(t, p, key, (_code(dtype), data), True)


@trap
def AEPutAttributeDesc(t, p, key, desc):
    return _put_keyed(t, p, key, read_desc(t, desc), True)


@trap
def AEGetAttributePtr(t, p, key, desired, type_p, data_p, max_size, actual_p):
    """keyMissedKeywordAttr is never there: as far as anyone can tell,
    every handler has read every parameter it was sent."""
    return _get_ptr(t, p, key, desired, type_p, data_p, max_size, actual_p, attr=True)


@trap
def AEGetAttributeDesc(t, p, key, desired, result):
    return _get_desc(t, p, key, desired, result, attr=True)


@trap
def AESizeOfAttribute(t, p, key, type_p, size_p):
    return _size_of(t, p, key, type_p, size_p, attr=True)


# =========================================================================
# Apple events: making, sending and handling them
# =========================================================================
def _new_event(t, event_class: bytes, event_id: bytes, target: tuple[bytes, bytes],
               return_id: int = kAutoGenerateReturnID, transaction_id: int = 0) -> Composite:
    if return_id == kAutoGenerateReturnID:
        return_id = next(state(t).return_ids) % 0x7FFF + 1
    return Composite(typeAppleEvent, attrs=[
        [keyEventClassAttr, typeType, event_class],
        [keyEventIDAttr, typeType, event_id],
        [keyAddressAttr, *target],
        [keyReturnIDAttr, typeShortInteger, struct.pack(">h", return_id)],
        [keyTransactionIDAttr, typeLongInteger, struct.pack(">l", transaction_id)],
    ])


def _ids(c: Composite) -> tuple[bytes, bytes]:
    cls, eid = c.find(keyEventClassAttr, True), c.find(keyEventIDAttr, True)
    return (cls[2] if cls else typeNull), (eid[2] if eid else typeNull)


def post(t, c: Composite, process=None) -> None:
    """An Apple event from outside: a high-level event in the queue (the
    current application's, or process's), whose message is the event
    class and whose where is the event ID."""
    process = t.process if process is None else process
    cls, eid = _ids(c)
    ev = EventRecord(kHighLevelEvent, int.from_bytes(cls, "big"), 0,
                     Point(*struct.unpack(">hh", eid)))
    state(t, process).pending.append((c.flatten(), ev))
    process.events.post(ev)


def aware(process) -> bool:
    """Does an application take high-level events, as its SIZE says?"""
    return process.app is not None and bool(size_flags(process.app) & isHighLevelEventAware)


def launched(t, documents=()) -> None:
    """System 7 opens a high-level-event-aware application by sending it
    an Open Application event, or with documents (working directory,
    node), an Open Documents event listing them."""
    if aware(t.process):
        psn = struct.pack(">II", 0, t.process.serial_number)
        eid = kAEOpenDocuments if documents else kAEOpenApplication
        c = _new_event(t, kCoreEventClass, eid, (typeProcessSerialNumber, psn))
        c.put(keyEventSourceAttr, typeShortInteger, struct.pack(">h", kAELocalProcess), attr=True)
        if documents:
            specs = [[NO_KEY, typeFSS, fss(node.volume.vref, node.parent.cnid, node.name)]
                     for _, node in documents]
            c.put(keyDirectObject, typeAEList, Composite(typeAEList, specs).flatten())
        post(t, c)


def fss(vref: int, parid: int, name: str) -> bytes:
    """An FSSpec's data: a volume, a parent directory and a name."""
    n = to_mac(name)[:63]
    return struct.pack(">hIB", vref, parid, len(n)) + n.ljust(63, b"\0")


def _handlers(st: AEState, cls: bytes, eid: bytes):
    """The application's handler for an event, then the system's."""
    for is_sys in (False, True):
        for key in ((cls, eid), (cls, typeWildCard), (typeWildCard, eid),
                    (typeWildCard, typeWildCard)):
            entry = st.handlers.get((is_sys, *key))
            if entry is not None:
                yield entry
                break


def handle(t, event_p: int, reply_p: int) -> int:
    """Call the handler for the Apple event at event_p, with the reply at
    reply_p: pascal OSErr fn(const AppleEvent *event, AppleEvent *reply,
    long refcon).  A handler's error goes in the reply."""
    c = _composite(t, event_p, typeAppleEvent)
    if isinstance(c, int):
        return c
    st = state(t)
    st.current.append(event_p)
    try:
        err = errAEEventNotHandled
        for proc, refcon in _handlers(st, *_ids(c)):
            err = t.call(proc, (("p", event_p), ("p", reply_p), ("l", refcon)), "e")
            if err != errAEEventNotHandled:
                break
    finally:
        st.current.pop()
    reply = _composite(t, reply_p, typeAppleEvent)
    if err != noErr and not isinstance(reply, int):
        reply.put(keyErrorNumber, typeShortInteger, struct.pack(">h", err))
        _store(t, reply_p, reply)
    return err


def _deliver(t, c: Composite, reply_p: int = 0) -> int:
    """Hand an Apple event to its handler, with the reply at reply_p or
    else none, and dispose of the event when it's done with, unless the
    handler suspended it."""
    heap = t.process.heap
    p = heap.new_ptr(2 * DESC)
    if not p:
        return memFullErr
    try:
        if write_desc(t, p, typeAppleEvent, c.flatten()) != noErr:
            return memFullErr
        if not reply_p:
            reply_p = p + DESC
            write_desc(t, reply_p, typeNull, b"")
        h = t.mem.r32(p + 4)
        err = handle(t, p, reply_p)
        if h in state(t).suspended:
            return noErr
        dispose_desc(t, p)
        if reply_p == p + DESC:
            dispose_desc(t, reply_p)
        return err
    finally:
        heap.dispose_ptr(p)


@trap
def AECreateAppleEvent(t, cls, eid, target_p, return_id, transaction_id, result):
    target = read_desc(t, target_p) if target_p else (typeNull, b"")
    if target is None:
        return errAENotAEDesc
    c = _new_event(t, _code(cls), _code(eid), target, return_id, transaction_id)
    return write_desc(t, result, typeAppleEvent, c.flatten())


def _is_self(t, address: list | None) -> bool:
    if address is None:
        return False
    _, dtype, data = address
    if dtype == typeProcessSerialNumber and len(data) == 8:
        return struct.unpack(">II", data) in ((0, kCurrentProcess), (0, t.process.serial_number))
    if dtype == typeApplSignature:
        return t.process.app is not None and data == t.process.app.creator
    return False


@trap
def AESend(t, event_p, reply_p, mode, priority, timeout, idle_proc, filter_proc):
    """To the application itself, its handler called at once; a reply,
    if it waits for one, with the handler's error in it."""
    c = _composite(t, event_p, typeAppleEvent)
    if isinstance(c, int):
        return c
    wait = mode & 3 == kAEWaitReply
    if wait and reply_p:
        reply = _new_event(t, kCoreEventClass, kAEAnswer, (typeNull, b""))
        write_desc(t, reply_p, typeAppleEvent, reply.flatten())
    elif reply_p:
        write_desc(t, reply_p, typeNull, b"")
    if not _is_self(t, c.find(keyAddressAttr, True)):
        return procNotFound
    c.put(keyEventSourceAttr, typeShortInteger, struct.pack(">h", kAESameProcess), attr=True)
    err = _deliver(t, c, reply_p if wait else 0)
    return errAEEventNotHandled if err == errAEEventNotHandled else noErr


@trap
def AEProcessAppleEvent(t, event_p):
    """The Apple event a high-level event from the queue stands for, to
    its handler."""
    if t.mem.r16(event_p) != kHighLevelEvent:
        return errAENotAppleEvent
    ids = _code(t.mem.r32(event_p + 2)), _code(t.mem.r32(event_p + 10))
    st = state(t)
    for entry in list(st.pending):          # another application may be adding to it
        c = Composite.parse(entry[0])
        if _ids(c) == ids:
            # Taken, perhaps by a GetSpecificHighLevelEvent filter, while
            # its high-level event may still be queued.
            st.pending.remove(entry)
            t.process.events.discard(entry[1])
            return _deliver(t, c)
    return errAENotAppleEvent


@trap
def GetSpecificHighLevelEvent(t, filter_proc, context, err_p):
    """Each high-level event in the queue to the filter, until it says
    it's found the one it wants: pascal Boolean fn(Ptr context,
    HighLevelEventMsgPtr msg, const TargetID *sender), the sender on this
    machine."""
    st = state(t)
    heap = t.process.heap
    p = heap.new_ptr(HLE_MSG + TARGET_ID)
    t.w16(err_p, noErr if p else memFullErr)
    if not p:
        return False
    try:
        for data, ev in list(st.pending):
            t.mem.fill(p, HLE_MSG + TARGET_ID)
            t.mem.w16(p, HLE_MSG)                   # its header length
            t.write_event(p + 8, ev)
            t.mem.w32(p + 32, len(data))            # msgLength
            if t.call(filter_proc, (("p", context), ("p", p), ("p", p + HLE_MSG)), "b"):
                return True
        return False
    finally:
        heap.dispose_ptr(p)


@trap
def AEGetTheCurrentEvent(t, p):
    st = state(t)
    if st.current:
        t.mem.write(p, t.mem.read(st.current[-1], DESC))
    else:
        write_desc(t, p, typeNull, b"")
    return noErr


@trap
def AESetTheCurrentEvent(t, p):
    return noErr


@trap
def AESuspendTheCurrentEvent(t, p):
    state(t).suspended.add(t.mem.r32(p + 4))
    return noErr


@trap
def AEResumeTheCurrentEvent(t, event_p, reply_p, dispatcher, refcon):
    """Handled now, by the standard dispatch, by dispatcher, or already
    (kAENoDispatch); then disposed of, as it would have been."""
    state(t).suspended.discard(t.mem.r32(event_p + 4))
    if dispatcher & 0xFFFFFFFF == kAEUseStandardDispatch & 0xFFFFFFFF:
        handle(t, event_p, reply_p)
    elif dispatcher:
        t.call(dispatcher, (("p", event_p), ("p", reply_p), ("l", refcon)), "e")
    dispose_desc(t, event_p)
    dispose_desc(t, reply_p)
    return noErr


@trap
def AEInteractWithUser(t, timeout, nm_req, idle_proc):
    """Its windows are already where the user can see them."""
    return noErr


@trap
def AESetInteractionAllowed(t, level):
    state(t).interaction = level
    return noErr


@trap
def AEGetInteractionAllowed(t, level_p):
    if level_p:
        t.mem.w8(level_p, state(t).interaction)
    return noErr


@trap
def AEResetTimer(t, reply_p):
    return noErr


# =========================================================================
# The dispatch tables
# =========================================================================
def _remove(table: dict, key: tuple, proc: int) -> int:
    entry = table.get(key)
    if entry is None or (proc and entry[0] != proc):
        return errAEHandlerNotFound
    del table[key]
    return noErr


def _lookup(t, table: dict, key: tuple, *out_ps: int) -> int:
    """A table entry, written field by field to out_ps (a long each, but
    a Boolean for fromTypeIsDesc)."""
    entry = table.get(key)
    if entry is None:
        return errAEHandlerNotFound
    for value, p in zip(entry, out_ps):
        if p and isinstance(value, bool):
            t.mem.w8(p, value)
        else:
            t.w32(p, value)
    return noErr


@trap
def AEInstallEventHandler(t, cls, eid, proc, refcon, is_sys):
    state(t).handlers[bool(is_sys), _code(cls), _code(eid)] = (proc, refcon)
    return noErr


@trap
def AERemoveEventHandler(t, cls, eid, proc, is_sys):
    return _remove(state(t).handlers, (bool(is_sys), _code(cls), _code(eid)), proc)


@trap
def AEGetEventHandler(t, cls, eid, proc_p, refcon_p, is_sys):
    return _lookup(t, state(t).handlers, (bool(is_sys), _code(cls), _code(eid)),
                   proc_p, refcon_p)


@trap
def AEInstallCoercionHandler(t, from_type, to_type, proc, refcon, from_is_desc, is_sys):
    key = (bool(is_sys), _code(from_type), _code(to_type))
    state(t).coercions[key] = (proc, refcon, bool(from_is_desc))
    return noErr


@trap
def AERemoveCoercionHandler(t, from_type, to_type, proc, is_sys):
    return _remove(state(t).coercions, (bool(is_sys), _code(from_type), _code(to_type)), proc)


@trap
def AEGetCoercionHandler(t, from_type, to_type, proc_p, refcon_p, is_desc_p, is_sys):
    return _lookup(t, state(t).coercions, (bool(is_sys), _code(from_type), _code(to_type)),
                   proc_p, refcon_p, is_desc_p)


@trap
def AEInstallSpecialHandler(t, function_class, proc, is_sys):
    state(t).specials[bool(is_sys), _code(function_class)] = (proc,)
    return noErr


@trap
def AERemoveSpecialHandler(t, function_class, proc, is_sys):
    return _remove(state(t).specials, (bool(is_sys), _code(function_class)), proc)


@trap
def AEGetSpecialHandler(t, function_class, proc_p, is_sys):
    return _lookup(t, state(t).specials, (bool(is_sys), _code(function_class)), proc_p)


@trap
def AEInstallObjectAccessor(t, desired_class, container_type, proc, refcon, is_sys):
    state(t).accessors[bool(is_sys), _code(desired_class), _code(container_type)] = (proc, refcon)
    return noErr


@trap
def AERemoveObjectAccessor(t, desired_class, container_type, proc, is_sys):
    key = (bool(is_sys), _code(desired_class), _code(container_type))
    return _remove(state(t).accessors, key, proc)


@trap
def AEGetObjectAccessor(t, desired_class, container_type, proc_p, refcon_p, is_sys):
    key = (bool(is_sys), _code(desired_class), _code(container_type))
    return _lookup(t, state(t).accessors, key, proc_p, refcon_p)


@trap
def AESetObjectCallbacks(t, compare, count, dispose_token, get_mark_token, mark, adjust_marks,
                         get_error_desc):
    """The procedures AEResolve calls on: HyperCard 2.2's standalones set
    them at launch.  Only the token disposer is called, the others being
    for tests (whose clauses) and marking, which AEResolve doesn't do."""
    state(t).object_callbacks = (compare, count, dispose_token, get_mark_token, mark,
                                 adjust_marks, get_error_desc)
    return noErr


def _accessor(st: AEState, desired: bytes, container_type: bytes):
    """The application's object accessor for a class of object in a
    container whose token is of container_type, then the system's; either
    may be for any class or any type of container (typeWildCard)."""
    for is_sys in (False, True):
        for key in ((desired, container_type), (desired, typeWildCard),
                    (typeWildCard, container_type), (typeWildCard, typeWildCard)):
            entry = st.accessors.get((is_sys, *key))
            if entry is not None:
                return entry
    return None


def _call_accessor(t, desired: bytes, container_p: int, container_class: bytes, form: bytes,
                   key_data_p: int, token_p: int) -> int:
    """The accessor's token for an object: pascal OSErr fn(DescType
    desiredClass, const AEDesc *containerToken, DescType containerClass,
    DescType keyForm, const AEDesc *keyData, AEDesc *theToken, long
    refcon)."""
    container = read_desc(t, container_p)
    if container is None:
        return errAENotAEDesc
    entry = _accessor(state(t), desired, container[0])
    if entry is None:
        return errAEAccessorNotFound
    proc, refcon = entry
    return t.call(proc, (("l", int.from_bytes(desired, "big")), ("p", container_p),
                         ("l", int.from_bytes(container_class, "big")),
                         ("l", int.from_bytes(form, "big")), ("p", key_data_p),
                         ("p", token_p), ("l", refcon)), "e")


def _dispose_token(t, token_p: int) -> int:
    """A token done with, to the application's token disposer if it set
    one (pascal OSErr fn(AEDesc *unneededToken)), else disposed of as any
    descriptor is."""
    callbacks = state(t).object_callbacks
    if len(callbacks) > 2 and callbacks[2]:
        return t.call(callbacks[2], (("p", token_p),), "e")
    dispose_desc(t, token_p)
    return noErr


def _resolve(t, spec: tuple[bytes, bytes], token_p: int) -> int:
    """The token for the object an object specifier names, at token_p: its
    container's token first, the application's (a null descriptor) for
    the outermost, then the token the object's accessor gives for it.
    The key data goes to the accessor as the specifier has it; a test
    (formTest, formWhose), which needs the comparison and counting
    callbacks, isn't resolved."""
    write_desc(t, token_p, typeNull, b"")
    dtype, data = spec
    c = Composite.parse(data) if dtype == typeObjectSpecifier else None
    if c is None or c.kind != typeAERecord:
        return errAENotAnObjSpec
    want, form, key_data = (c.find(k) for k in (keyAEDesiredClass, keyAEKeyForm, keyAEKeyData))
    if want is None or form is None or key_data is None or len(want[2]) != 4 or len(form[2]) != 4:
        return errAENotAnObjSpec
    if form[2] in (formTest, formWhose):
        return errAEEventNotHandled
    container = c.find(keyAEContainer)
    container = (typeNull, b"") if container is None else (container[1], container[2])
    heap = t.process.heap
    p = heap.new_ptr(2 * DESC)
    if not p:
        return memFullErr
    container_p, key_data_p = p, p + DESC
    try:
        if container[0] == typeNull:
            container_class = typeNull
            write_desc(t, container_p, typeNull, b"")
        else:
            outer = Composite.parse(container[1]) if container[0] == typeObjectSpecifier else None
            outer_want = outer.find(keyAEDesiredClass) if outer is not None else None
            if outer_want is None:
                return errAENotAnObjSpec
            container_class = outer_want[2]
            err = _resolve(t, container, container_p)
            if err != noErr:
                return err
        err = write_desc(t, key_data_p, key_data[1], key_data[2])
        if err == noErr:
            err = _call_accessor(t, want[2], container_p, container_class, form[2],
                                 key_data_p, token_p)
            dispose_desc(t, key_data_p)
        if container[0] != typeNull:
            _dispose_token(t, container_p)
        return err
    finally:
        heap.dispose_ptr(p)


@trap
def AEResolve(t, spec_p, callback_flags, token_p):
    """The token for the object an object specifier names, found by the
    object accessors the application installed: Netscape Navigator 2.02
    resolves the window its Open Location event names."""
    spec = read_desc(t, spec_p) if spec_p else None
    if spec is None:
        return errAENotAEDesc
    return _resolve(t, spec, token_p)


@trap
def AECallObjectAccessor(t, desired, container_p, container_class, form, key_data_p, token_p):
    return _call_accessor(t, _code(desired), container_p, _code(container_class), _code(form),
                          key_data_p, token_p)


@trap
def AEDisposeToken(t, token_p):
    return _dispose_token(t, token_p)


keyAEVersion, keyAERecorderCount = b"vers", b"recr"
AE_VERSION = 0x01018000                 # 1.0.1, a final version: a NumVersion


@trap
def AEManagerInfo(t, keyword, result_p):
    """What the Apple Event Manager says of itself: its version, or how
    many processes are recording Apple events, which none are.  Netscape
    Navigator 2.02 asks for its version at launch."""
    info = {keyAEVersion: AE_VERSION, keyAERecorderCount: 0}.get(_code(keyword))
    if info is None:
        return paramErr
    t.w32(result_p, info)
    return noErr


# The Apple Event Manager's private handler tables, which System 7.5's
# Find File keeps handlers of its own in: a handle of entries, each two
# longs it's looked up by and the handler's 8 bytes (a procedure and its
# reference constant).  The format is macrt's; programs don't look inside.
HANDLER_ENTRY = 16


def _table_entries(t, table: int) -> list[bytes] | None:
    h = table & t.mem.system.addr_mask
    if not h:
        return None
    heap = t.system.find_heap(h)
    if heap is None or not heap.is_handle(h):
        return None
    data = t.mem.read(heap.deref(h), heap.handle_size(h))
    whole = len(data) - len(data) % HANDLER_ENTRY
    return [data[i:i + HANDLER_ENTRY] for i in range(0, whole, HANDLER_ENTRY)]


def _write_table(t, table: int, entries: list[bytes]) -> int:
    h = table & t.mem.system.addr_mask
    heap = t.system.heap_of(h)
    if not heap.set_handle_size(h, len(entries) * HANDLER_ENTRY):
        return memFullErr
    t.mem.write(heap.deref(h), b"".join(entries))
    return noErr


@trap
def _AE_hdlr_table_alloc(t, n_elts, unknown_2, unknown_3, unknown_p, table_p):
    """A new, empty table (n_elts is how many it's made room for)."""
    h = t.process.heap.new_handle(0)
    if not h:
        return memFullErr
    t.w32(table_p, h)
    return noErr


@trap
def _AE_hdlr_install(t, table, unknown, selector, hdlr):
    entries = _table_entries(t, table)
    if entries is None:
        return errAEHandlerNotFound
    key = t.mem.read(selector, 8)
    entries = [e for e in entries if e[:8] != key] + [key + t.mem.read(hdlr, 8)]
    return _write_table(t, table, entries)


@trap
def _AE_hdlr_lookup(t, table, unknown, selector, hdlr_p):
    key = t.mem.read(selector, 8)
    found = next((e for e in _table_entries(t, table) or () if e[:8] == key), None)
    if found is None:
        return errAEHandlerNotFound
    t.mem.write(hdlr_p, found[8:])
    return noErr


@trap
def _AE_hdlr_delete(t, table, unknown, selector):
    entries = _table_entries(t, table)
    key = t.mem.read(selector, 8)
    if not entries or all(e[:8] != key for e in entries):
        return errAEHandlerNotFound
    return _write_table(t, table, [e for e in entries if e[:8] != key])
