"""
The one ISO 9660 reader, shared by the running emulator
(macrt.macfile's load_volume fallback, for a disc's ISO view when its
Mac volumes are unreadable) and the Disk Tracker (disktracker.index,
whose catalogues index ISO 9660 discs).

Names are the Rock Ridge "NM" entry's payload when the record carries
one — case and long names kept — otherwise the ISO 9660 name field,
latin-1 with the ";1" version suffix stripped, the way the tracker has
always written them to its catalogues. SUSP is scanned tolerantly:
well-formed entries are walked by their length byte wherever they sit
in the record's System Use Area, and "CL"/"PL"/"RE" are ignored.
"""
import struct
from datetime import datetime, timezone

ISO_SECTOR = 2048
MAC_EPOCH = 2082844800       # seconds from 1904-01-01 to 1970-01-01


def _iso_date(raw: bytes) -> int:
    """A PVD's 17-byte date string as a Mac epoch time, 0 if absent."""
    try:
        s = raw[:14].decode('ascii')
        when = datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]),
                        int(s[8:10]), int(s[10:12]), int(s[12:14]),
                        tzinfo=timezone.utc)
    except (ValueError, UnicodeDecodeError):
        return 0
    return int(when.timestamp()) + MAC_EPOCH


def _iso_dir_date(raw: bytes) -> int:
    """A directory record's 7-byte date as a Mac epoch time, 0 if absent."""
    if not any(raw):
        return 0
    try:
        when = datetime(1900 + raw[0], raw[1], raw[2], raw[3], raw[4], raw[5],
                        tzinfo=timezone.utc)
    except ValueError:
        return 0
    return int(when.timestamp()) + MAC_EPOCH


NM_FLAG_CONTINUE = 0x01               # this NM continues the record's name
NM_FLAG_CURRENT = 0x02                # the entry is a directory's "."
NM_FLAG_PARENT = 0x04                 # the entry is a directory's ".."


def _susp_entries(rec: bytes):
    """Yield (signature, version, body) SUSP entries from a directory
    record's System Use Area, walking by each entry's length byte.
    Tolerant: SP (or anything unknown) is skipped the same way, NUL
    padding is stepped over, and a malformed entry ends the scan —
    nothing raises."""
    if len(rec) < 33:
        return
    nlen = rec[32]
    at = 33 + nlen + (1 if nlen % 2 == 0 else 0)
    while at + 4 <= len(rec):
        sig = bytes(rec[at:at + 2])
        length = rec[at + 2]
        if rec[at] == 0:
            at += 1                   # zero fill inside the area
            continue
        if length < 4 or at + length > len(rec):
            break                     # malformed — trust the area no further
        yield sig, rec[at + 3], rec[at + 4:at + length]
        at += length


def _rock_ridge_name(rec: bytes):
    """The record's Rock Ridge name, or None when it carries none.
    All of the record's NM entries concatenate in order (0x01 marks a
    split entry, not a separate name). An NM entry flagged 0x02 or
    0x04 makes the record a directory's "." or "..", which has no name
    of its own. Decoded as UTF-8, falling back to latin-1 — NM holds
    both on real discs."""
    name = bytearray()
    saw_nm = False
    for sig, version, body in _susp_entries(rec):
        if sig != b'NM' or version != 1 or len(body) < 1:
            continue
        flags, payload = body[0], body[1:]
        if flags & (NM_FLAG_CURRENT | NM_FLAG_PARENT):
            return None
        saw_nm = True
        name += payload
    if not saw_nm or not name:
        return None
    try:
        return bytes(name).decode('utf-8')
    except UnicodeDecodeError:
        return bytes(name).decode('latin-1')


def _iso_children(data: bytes, lba: int, length: int, depth: int) -> list[dict]:
    """One directory's entries as nodes: files with their size and
    dates, folders with their own children read recursively."""
    out: list[dict] = []
    if depth > 64:                             # a directory in itself?
        return out
    at, end = lba * ISO_SECTOR, lba * ISO_SECTOR + length
    while at + 34 <= end and at + 34 <= len(data):
        reclen = data[at]
        if reclen < 34:                        # padding to the sector's end
            at = (at // ISO_SECTOR + 1) * ISO_SECTOR
            continue
        rec = data[at:at + reclen]
        nlen = rec[32]
        if len(rec) < 33 + nlen:
            break
        iso_name = bytes(rec[33:33 + nlen]).split(b';')[0].decode('latin-1', 'replace')
        name = _rock_ridge_name(rec) or iso_name
        child_lba, = struct.unpack_from('>I', rec, 2)
        size, = struct.unpack_from('>I', rec, 10)
        date = _iso_dir_date(bytes(rec[18:25]))
        if name not in ('.', '..'):
            if rec[25] & 0x02:
                node = {'kind': 'folder', 'name': name, 'crdate': date,
                        'mddate': date,
                        'children': _iso_children(data, child_lba, size, depth + 1)}
            else:
                node = {'kind': 'file', 'name': name, 'type': b'????',
                        'creator': b'????', 'size': size, 'lba': child_lba, 'crdate': date,
                        'mddate': date, 'children': []}
            out.append(node)
        at += reclen
    return out


def _read_iso(data: bytes, stem: str):
    """The ISO 9660 volume: its name, its dates, and its root folder
    as a tree of dicts."""
    at = 16 * ISO_SECTOR
    if len(data) < at + ISO_SECTOR or data[at] != 1 or data[at + 1:at + 6] != b'CD001':
        raise ValueError("it has no ISO 9660 primary volume descriptor")
    pvd = data[at:at + ISO_SECTOR]
    name = pvd[40:72].decode('latin-1').strip() or stem
    crdate, mddate = _iso_date(pvd[770:787]), _iso_date(pvd[787:804])
    root_lba, = struct.unpack_from('>I', pvd, 149)
    root_len, = struct.unpack_from('>I', pvd, 157)
    root = {'kind': 'folder', 'name': name, 'crdate': crdate, 'mddate': mddate,
            'children': _iso_children(data, root_lba, root_len, 1)}
    return name, crdate, mddate, root
