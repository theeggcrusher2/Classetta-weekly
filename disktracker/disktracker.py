"""Core reader/writer for the DiskTracker 2 .dtc format."""

import struct
from dataclasses import dataclass
from typing import List, Union

HEADER_SIZE = 0x2F0
ITEM_FRONT = 39

# Disk Tracker 1.x layout (from the .dt1 samples, cross-checked against
# the 2.x conversion code in the OS X binary): a 0x37e-byte header, then
# per disk a 76-byte volume record followed by `descendants`
# variable-length item records (36 fixed bytes, then a Pascal name).
# A gap of padding may follow before the next volume record.
V1_HEADER_SIZE = 0x37e
V1_VOLUME_SIZE = 76
V1_ITEM_BASE = 37       # 36 fixed bytes, name length at +0x24, name at +0x25
V1_SCAN_LIMIT = 0x4000  # max gap searched for the next volume record

try:
    b'\xa5'.decode('mac_roman')

    def _decode_mac_roman(b: bytes) -> str:
        return b.decode('mac_roman', 'replace')
except LookupError:
    def _decode_mac_roman(b: bytes) -> str:
        return b.decode('latin-1', 'replace')

@dataclass
class Disk:
    name: str
    items: int
    created: int
    modified: int
    offset: int
    suffix: bytes

@dataclass
class Item:
    kind: str  # 'disk' | 'file' | 'folder'
    name: str
    offset: int = 0
    type: bytes = b''
    creator: bytes = b''
    size: int = 0
    parent: int = 0
    created: int = 0
    modified: int = 0
    depth: int = 0
    items: int = 0
    descendants: int = 0
    tail: bytes = b''
    front: bytes = b''
    suffix: bytes = b''

def parse_dtc(data: bytes) -> List[Item]:
    """Parse a .dtc or .dt1 file (auto-detecting 1.x or 2.x format)."""
    if data.startswith(b'disktrak'):
        if len(data) >= 0x10 and struct.unpack('>I', data[12:16])[0] == 1:
            return parse_dtc_v1(data)
        return _parse_dtc_v2(data)
    return parse_dtc_v1(data)


def _v1_volume_ok(data: bytes, off: int) -> bool:
    if off + V1_VOLUME_SIZE > len(data):
        return False
    rec = data[off:off+V1_VOLUME_SIZE]
    ln = rec[0x24]
    if not (1 <= ln <= 39 and off + 0x25 + ln <= len(data)):
        return False
    name = rec[0x25:0x25+ln]
    if not all(b >= 0x20 for b in name):
        return False
    items, desc = struct.unpack('>II', rec[0x1c:0x24])
    if not (0 < desc <= 500000):
        return False
    created, modified = struct.unpack('>II', rec[0x12:0x1a])
    if created <= 0x80000000 and modified <= 0x80000000:
        return False
    depth, = struct.unpack('>H', rec[0x1a:0x1c])
    return depth == 0


def _v1_scan(data: bytes, start: int, limit: int) -> Union[int, None]:
    for off in range(start, min(start + limit, len(data) - V1_VOLUME_SIZE)):
        if _v1_volume_ok(data, off):
            return off
    return None


def parse_dtc_v1(data: bytes) -> List[Item]:
    """Parse Disk Tracker 1.x format files (lacking comments, notes, and large volume extensions)."""
    if not data.startswith(b'disktrak') or len(data) < V1_HEADER_SIZE:
        return []
    out: List[Item] = []
    off = V1_HEADER_SIZE if _v1_volume_ok(data, V1_HEADER_SIZE) else _v1_scan(data, V1_HEADER_SIZE, V1_SCAN_LIMIT)
    while off is not None and len(out) < 500000:
        rec = data[off:off+V1_VOLUME_SIZE]
        ln = rec[0x24]
        name = _decode_mac_roman(rec[0x25:0x25+ln])
        created, modified = struct.unpack('>II', rec[0x12:0x1a])
        items, desc = struct.unpack('>II', rec[0x1c:0x24])
        
        disk_item = Item(
            kind='disk',
            name=name,
            offset=off,
            created=created,
            modified=modified,
            depth=0,
            items=desc,          # total descendants count for v2 parity
            descendants=items,   # immediate children count
            front=rec,
        )
        out.append(disk_item)
        off += V1_VOLUME_SIZE

        truncated = False
        for _ in range(desc):
            if off + V1_ITEM_BASE > len(data):
                truncated = True
                break
            it_bytes = data[off:]
            if len(it_bytes) < V1_ITEM_BASE:
                truncated = True
                break
            flags, = struct.unpack('>H', it_bytes[0:2])
            typ = it_bytes[2:6]
            cre = it_bytes[6:10]
            size, parent, created, modified = struct.unpack('>IIII', it_bytes[10:26])
            depth, items_c, desc_c = struct.unpack('>HII', it_bytes[26:36])
            iln = it_bytes[36]
            if off + V1_ITEM_BASE + iln > len(data):
                truncated = True
                break
            iname = _decode_mac_roman(it_bytes[37:37+iln])
            
            is_folder = (flags & 0x8000) or (typ[2:] == b'\xff\xff')
            kind = 'folder' if is_folder else 'file'
            
            item = Item(
                kind=kind,
                name=iname,
                offset=off,
                type=typ,
                creator=cre,
                size=size,
                parent=parent,
                created=created,
                modified=modified,
                depth=depth,
                items=items_c,
                descendants=desc_c,
                front=it_bytes[:V1_ITEM_BASE],
            )
            out.append(item)
            off += V1_ITEM_BASE + iln
        if truncated:
            break
        off = _v1_scan(data, off, V1_SCAN_LIMIT)
    return out


def _is_disk_record(data: bytes, off: int) -> bool:
    """Check if off points to a valid disk record header.

    Disk records: four NUL bytes at +1, a plausible count at +0x14, a valid
    name length at +0x52 and a printable name at +0x53, with the same name
    echoed at +0x19 (embedded volume-name field).  The echo matches every
    known disk record in the corpus and rejects item records and catalog
    junk, so b0 is not constrained.
    """
    if off + 0x53 >= len(data):
        return False
    if data[off + 1:off + 5] != b'\x00\x00\x00\x00':
        return False
    count = struct.unpack('>I', data[off + 0x14:off + 0x18])[0]
    if not (1 <= count <= 500_000):
        return False
    nlen = data[off + 0x52]
    if not (1 <= nlen <= 63):
        return False
    if off + 0x53 + nlen > len(data):
        return False
    name = data[off + 0x53:off + 0x53 + nlen]
    if not _name_ok(name):
        return False
    return data[off + 0x19:off + 0x19 + nlen] == name


_NAME_CTRL_OK = frozenset(range(0x00, 0x20)) | {0x7F}


def _name_ok(name: bytes) -> bool:
    """Name bytes.  Disk Tracker does not sanitize: real names in the
    corpus contain NULs, 0x01-0x03, LF/CR, 0x7f and Shift-JIS lead/trail
    bytes anywhere.  Failures we keep: empty (NUL-only) names, and names
    containing ':' (0x3A), the Classic Mac OS path separator, which real
    filenames never contain — it appears only in mount-path suffix junk
    (e.g. 'Volumes/AP Ref & Pres Winter 91:92')."""
    stripped = name.rstrip(b'\x00')
    if not stripped:
        return False
    return b'\x3a' not in stripped


def _decode_name(b: bytes) -> str:
    try:
        return b.decode('utf-8')
    except UnicodeDecodeError:
        return _decode_mac_roman(b)


def _type_ok(typ: bytes, creator: bytes = b'') -> bool:
    if typ == b'\x00\x00\x00\x00':
        return True
    if typ[2:] == b'\xff\xff':
        return True
    if typ[0] == 0 and typ[2] == 0 and typ[3] == 0:
        # Embedded-archive/no-type record: 00 <flags> 00 00
        return True
    if creator == b'pdos':
        # PC Exchange DOS/MacBinary records carry binary type codes
        return True
    if creator == b'jedi':
        # AppleWorks stationery embedded parts: binary type codes
        # (00 1a 1e 34, 00 15 38 94, ...) with creator 'jedi'
        return True
    if creator == b'edi\xa9' and len(typ) == 4 and typ[3] == ord('j'):
        # AppleWorks stationery embedded parts, split across the
        # type/creator boundary ('14 00 07 6a' + 'edi\xa9' = 'jedi\xa9')
        return True
    if (typ, creator) in ((b'\x00\x00\x52\x33', b'\x58\x50\x00\x00'),
                          (b'\x00\x00\x2a\x2a', b'\x2a\x2a\x00\x00')):
        # AppleWorks stationery part signature ('Signature\r' items, b0=0x48)
        return True
    return all(32 <= b < 127 or b >= 0x80 for b in typ)


def _wide_tail(typ: bytes) -> bool:
    """Records in the embedded/no-type family (or folders) carry a 5-byte tail."""
    if typ[2:] == b'\xff\xff':
        return True
    return typ[0] == 0 and typ[2] == 0 and typ[3] == 0


def _front_fields(data: bytes, off: int) -> dict:
    lb = off + ITEM_FRONT
    return {
        'type': data[off + 1:off + 5],
        'creator': data[off + 5:off + 9],
        'size': struct.unpack('>I', data[off + 13:off + 17])[0],
        'parent': struct.unpack('>I', data[off + 17:off + 21])[0],
        'created': struct.unpack('>I', data[off + 21:off + 25])[0],
        'modified': struct.unpack('>I', data[off + 25:off + 29])[0],
        'depth': struct.unpack('>H', data[off + 29:off + 31])[0],
        'items': struct.unpack('>I', data[off + 31:off + 35])[0],
        'descendants': struct.unpack('>I', data[off + 35:off + 39])[0],
    }


def _parse_item_at(data: bytes, off: int):
    """Parse item record at off. Returns (Item, next_offset) or None.

    Handles the long-name variant: 39-byte front, 0xe1 marker at the length
    byte, 31-byte truncated name, NUL, 8-byte meta, full UTF-8 name, NUL.
    """
    if off + ITEM_FRONT + 2 > len(data):
        return None
    f = _front_fields(data, off)
    typ = f['type']
    if not _type_ok(typ, f['creator']):
        return None
    is_folder = typ[2:] == b'\xff\xff'
    tail_len = 5 if _wide_tail(typ) else 1
    lb = off + ITEM_FRONT
    ln = data[lb]
    if ln >= 0xC0:
        # Long-name variant: 39-byte front, length-marker byte ln, name1 of
        # (256 - ln) bytes, NUL, 8-byte meta.  The meta's last byte is the
        # full-name field length in bytes (0 means name1 is the complete
        # name).  The stored full name is the real name followed by trailing
        # binary junk; the real name ends at the first NUL byte (this also
        # matches name1, which is the real name truncated to name1_len).
        name1_len = 256 - ln
        if not (1 <= name1_len <= 63):
            return None
        name1 = data[lb + 1:lb + 1 + name1_len]
        if len(name1) < name1_len or data[lb + 1 + name1_len] != 0:
            return None
        mp = lb + 2 + name1_len
        # Candidate meta positions: the exact byte after name1's NUL (most
        # records — meta often begins with NULs, e.g. flen==0 records carry
        # no full name), or the first non-NUL byte for writers that
        # zero-pad between name1's NUL and the meta.  Pick the candidate
        # whose next_off lands on a parseable record; otherwise keep the
        # exact position (historical behaviour).
        candidates = [mp]
        m2 = mp
        while m2 < len(data) and data[m2] == 0:
            m2 += 1
        if m2 != mp:
            candidates.append(m2)
        picked = None
        fallback = None
        for cand in candidates:
            if cand + 8 > len(data):
                continue
            meta = data[cand:cand + 8]
            flen = meta[7]
            fp = cand + 8
            if flen:
                full = data[fp:fp + flen]
                if len(full) < flen:
                    continue
                name = full.split(b'\x00', 1)[0]
                next_off = fp + flen + 1
            else:
                name = name1.split(b'\x00', 1)[0]
                next_off = fp + 8
            if not name:
                continue
            if fallback is None:
                fallback = (name, next_off)
            if next_off <= len(data) and (_parse_item_at(data, next_off) is not None or _is_disk_record(data, next_off)):
                picked = (name, next_off)
                break
        if picked is None:
            picked = fallback
        if picked is None:
            return None
        name, next_off = picked
        item = Item(kind='folder' if is_folder else 'file',
                    name=_decode_name(name.rstrip(b'\x00')), offset=off,
                    type=typ, creator=f['creator'], size=f['size'],
                    parent=f['parent'], created=f['created'], modified=f['modified'],
                    depth=f['depth'], items=f['items'], descendants=f['descendants'],
                    tail=b'', front=data[off:lb + 1])
        return item, next_off
    if not (1 <= ln <= 63):
        return None
    if lb + 1 + ln > len(data):
        return None
    name_bytes = data[lb + 1:lb + 1 + ln]
    if not _name_ok(name_bytes):
        return None
    if lb + 1 + ln + tail_len > len(data):
        return None
    item = Item(kind='folder' if is_folder else 'file',
                name=_decode_name(name_bytes.rstrip(b'\x00')), offset=off,
                type=typ, creator=f['creator'], size=f['size'],
                parent=f['parent'], created=f['created'], modified=f['modified'],
                depth=f['depth'], items=f['items'], descendants=f['descendants'],
                tail=data[lb + 1 + ln:lb + 1 + ln + tail_len],
                front=data[off:lb + 1])
    return item, lb + 1 + ln + tail_len


def _is_item_start(data: bytes, off: int) -> bool:
    return _parse_item_at(data, off) is not None


def _item_chain_ok(data: bytes, parsed) -> bool:
    """True if the record following a parsed item is itself parseable
    (as an item or a disk record).  Rejects phantom one-off parses.
    A successor that lands exactly at end of data is accepted: a disk
    whose only record ends the file has nothing to chain against."""
    _, nxt = parsed
    if nxt == len(data):
        return True
    return _is_item_start(data, nxt) or _is_disk_record(data, nxt)


def _find_first_item(data: bytes, after_name: int, max_scan: int = 5000) -> int | None:
    """Scan forward from after_name to find first real item record.

    Disk record suffix length varies (mount path, source location, network
    info), so items cannot be assumed to start at after_name + 308.  First
    item has depth=1, parses cleanly, and its successor must parse too —
    single phantom records in the header suffix pass a depth==1 test.
    """
    end = min(after_name + max_scan, len(data))
    for candidate in range(after_name, end):
        depth = int.from_bytes(data[candidate + 29:candidate + 31], 'big')
        if depth != 1:
            continue
        parsed = _parse_item_at(data, candidate)
        if parsed is not None and _item_chain_ok(data, parsed):
            return candidate
    return None


def _parse_dtc_v2(data: bytes) -> List[Item]:
    """Parse Disk Tracker 2 .dtc format."""
    out: List[Item] = []
    off = HEADER_SIZE
    while off < len(data) - 0x52:
        # Skip to next disk record if current position isn't one
        if not _is_disk_record(data, off):
            off += 1
            continue

        count = struct.unpack('>I', data[off + 0x14:off + 0x18])[0]
        nlen = data[off + 0x52]
        name = _decode_mac_roman(data[off + 0x53:off + 0x53 + nlen])
        after = off + 0x53 + nlen

        # Find where first item starts (suffix length is variable)
        first_item = _find_first_item(data, after) if count > 0 else None
        if first_item is None:
            # No items found; scan forward for next disk record
            probe = after + 308
            while probe < len(data) - 0x52 and not _is_disk_record(data, probe):
                probe += 1
            else:
                off = probe
                continue

        disk_rec = Item(kind='disk', name=name, offset=off, items=count,
                        created=struct.unpack('>I', data[off + 6:off + 10])[0],
                        modified=struct.unpack('>I', data[off + 10:off + 14])[0],
                        suffix=data[after:first_item])
        out.append(disk_rec)
        off = first_item

        # Parse items for this disk until count is reached or next disk found
        while count > 0 and off + 40 <= len(data):
            # Stop at next disk record
            if _is_disk_record(data, off):
                break

            parsed = _parse_item_at(data, off)
            if parsed is not None:
                item, off = parsed
                out.append(item)
                count -= 1
                continue

            # Unparseable record: find the next plausible item record (or
            # disk record) within a bounded window and count the skipped
            # span as one item so per-disk counts stay aligned.
            nxt = None
            probe_end = min(off + 512, len(data) - 40)
            for probe in range(off + 1, probe_end):
                if _is_disk_record(data, probe):
                    nxt = probe
                    break
                if _parse_item_at(data, probe) is not None:
                    nxt = probe
                    break
            if nxt is None:
                break
            off = nxt
            count -= 1

    return out
