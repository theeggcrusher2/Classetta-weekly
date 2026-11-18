"""
The one ISO 9660 reader, shared by the running emulator
(macrt.macfile's load_volume fallback, for a disc's ISO view when its
Mac volumes are unreadable) and the Disk Tracker (disktracker.index,
whose catalogues index ISO 9660 discs).

Names are latin-1 with the ";1" version suffix stripped, the way the
tracker has always written them to its catalogues.  No SUSP/Rock
Ridge: an ISO 9660 view is read exactly as the basic 9660 directory
records describe it.
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
        name = bytes(rec[33:33 + nlen]).split(b';')[0].decode('latin-1', 'replace')
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
