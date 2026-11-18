"""DiskTracker 2 .dtc format writer."""

import struct
from typing import List, Union
from .disktracker import Disk, Item, HEADER_SIZE, _wide_tail


def _utf8_prefix(text: str, limit: int) -> bytes:
    """A prefix of text encoded to UTF-8, no longer than limit bytes,
    cut at a codepoint boundary."""
    out = b''
    for ch in text:
        piece = ch.encode('utf-8')
        if len(out) + len(piece) > limit:
            break
        out += piece
    return out

def write_dtc(items: List[Union[Item, dict]]) -> bytes:
    """Generate a valid DiskTracker 2 .dtc byte string from items.

    Each disk record owns the non-disk items that follow it in the
    list, in order: a multi-disk list becomes a multi-disk file."""
    # Build header (constant block of 0x2F0 bytes, magic 'disktrak')
    header = bytearray(HEADER_SIZE)
    header[0:8] = b'disktrak'
    struct.pack_into('>I', header, 0x0C, 2)  # version 2
    struct.pack_into('>I', header, 0x10, 638) # block size

    # Group items: a disk record and the items that follow it
    groups: list[tuple[Item, list[Item]]] = []
    orphans: list[Item] = []
    for i in items:
        if isinstance(i, Item) and i.kind == 'disk':
            groups.append((i, []))
        elif isinstance(i, Item) and groups:
            groups[-1][1].append(i)
        elif isinstance(i, Item):
            orphans.append(i)
    if orphans or not groups:
        # Items have no disk record ahead of them: give them a default
        # root disk rather than dropping them silently.
        groups.insert(0, (Item(kind='disk', name='Untitled Disk', offset=HEADER_SIZE), orphans))

    out = bytearray(header)

    # For each disk, write disk record + suffix + its items
    for disk, disk_items in groups:
        # A disk record carries its name raw and its echo at +0x19; the
        # parser caps the name at 63 bytes and refuses a ':', and detection
        # needs the echo, so the name fits the space before the raw-name
        # field (0x19 + len <= 0x52). Real HFS volume names never hit that
        # (27 bytes max); a UDF one can, so long or colon-carrying names
        # are truncated at a codepoint boundary and the colon becomes
        # '?' — the catalog is an index, not the disk.
        disk_name = disk.name.replace(':', '?')
        nb_disk = _utf8_prefix(disk_name, 0x52 - 0x19)
        # Disk record header structure
        d_rec = bytearray(0x52 + 1 + len(nb_disk) + 308)
        # pack dates, count, etc.
        struct.pack_into('>I', d_rec, 6, disk.created)
        struct.pack_into('>I', d_rec, 10, disk.modified)
        struct.pack_into('>I', d_rec, 0x14, len(disk_items))

        name_b = nb_disk
        d_rec[0x52] = len(name_b)
        d_rec[0x53:0x53+len(name_b)] = name_b
        # Name echoed at +0x19, as in real Disk Tracker catalogs —
        # the parser's disk-record detection requires it.
        echo_end = 0x19 + len(name_b)
        if echo_end <= 0x52:
            d_rec[0x19:echo_end] = name_b
        
        # Suffix / padding
        if disk.suffix:
            after = 0x52 + 1 + len(name_b)
            d_rec[after:after+len(disk.suffix)] = disk.suffix

        out.extend(d_rec)

        for it in disk_items:
            # Item record layout
            is_folder = (it.kind == 'folder')
            typ = it.type if it.type else (b'FNDR' if not is_folder else b'\x00\x00\xff\xff')
            # Tail length must match the parser's rule for the record
            # family: the embedded/no-type family (or folders) carries a
            # 5-byte tail even for files.
            tail_len = 5 if (is_folder or _wide_tail(typ)) else 1

            cre = it.creator if it.creator else b'MACS'

            nb = it.name.encode('mac_roman', 'replace')
            if len(nb) > 63 or ':' in it.name:
                # The long-name variant: 39-byte front, 0xe1 marker at
                # the length byte, 31-byte UTF-8 name1 (NUL-padded), a
                # NUL, an 8-byte meta whose last byte is the full-name
                # length, then the full UTF-8 name and a NUL.
                full = it.name.encode('utf-8')
                if len(full) > 255:
                    raise ValueError(f"the name {it.name!r} is too long for a Disk Tracker catalog (over 255 bytes encoded)")
                front = bytearray(39)
                if it.front:
                    front[:len(it.front)] = it.front
                else:
                    front[38] = 0x00
                struct.pack_into('>4s', front, 39 - 38, typ)
                struct.pack_into('>4s', front, 39 - 34, cre)
                struct.pack_into('>I', front, 39 - 26, it.size)
                struct.pack_into('>I', front, 39 - 22, it.parent)
                struct.pack_into('>I', front, 39 - 18, it.created)
                struct.pack_into('>I', front, 39 - 14, it.modified)
                struct.pack_into('>H', front, 39 - 10, it.depth)
                struct.pack_into('>I', front, 39 - 8, it.items)
                struct.pack_into('>I', front, 39 - 4, it.descendants)
                name1 = _utf8_prefix(it.name, 31)
                rec = bytes(front) + b'\xe1' + name1.ljust(31, b'\x00') + b'\x00'
                rec += b'\x80' + b'\x00' * 6 + bytes([len(full)])
                rec += full + b'\x00'
                out.extend(rec)
                continue

            rec_len = 39 + 1 + len(nb) + tail_len
            item_b = bytearray(rec_len)

            # front byte
            if it.front:
                item_b[:len(it.front)] = it.front
            else:
                item_b[38] = 0x00

            # type/creator/size/parent/created/modified/depth/items/descendants
            struct.pack_into('>4s', item_b, 39 - 38, typ)
            struct.pack_into('>4s', item_b, 39 - 34, cre)
            struct.pack_into('>I', item_b, 39 - 26, it.size)
            struct.pack_into('>I', item_b, 39 - 22, it.parent)
            struct.pack_into('>I', item_b, 39 - 18, it.created)
            struct.pack_into('>I', item_b, 39 - 14, it.modified)
            struct.pack_into('>H', item_b, 39 - 10, it.depth)
            struct.pack_into('>I', item_b, 39 - 8, it.items)
            struct.pack_into('>I', item_b, 39 - 4, it.descendants)

            # name len & bytes
            lb = 39
            item_b[lb] = len(nb)
            item_b[lb+1:lb+1+len(nb)] = nb

            # tail
            if it.tail:
                item_b[lb+1+len(nb):lb+1+len(nb)+len(it.tail)] = it.tail

            out.extend(item_b)

    return bytes(out)
