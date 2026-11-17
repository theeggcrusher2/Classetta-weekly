"""
Read-only HFS Plus / HFSX volumes ('H+'/'HX' at offset 1024 of the
volume: the disk Mac OS 8.1 and later formatted, and what old Apple
CD-ROMs hold when they carry no ISO 9660 view).

The catalog B-tree is read whole: a volume's names, dates, Finder info
and fork extents come from it, one leaf node at a time.  A file's forks
are left on the disk until they're wanted (macrt.macfile.OnDisk), the
thousands of files on a CD not all read when it's put in a drive, only
the ones that are opened.
"""
import struct
import unicodedata
from collections.abc import Iterator

import machfs

from .macfile import OnDisk, _link_aliases, _gmt_to_local, _mac_name

HFSX_SIGNATURES = (b"H+", b"HX")
ROOT_FOLDER_ID, ROOT_PARENT_ID = 2, 1
NODE_KIND_LEAF = -1
EXTENT_DATA, EXTENT_RSRC = 0, 0xFF
FOLDER_RECORD, FILE_RECORD = 0x0001, 0x0002
FOLDER_THREAD, FILE_THREAD = 0x0003, 0x0004
INVISIBLE = 0x4000


def _fork_data(v: bytes, at: int) -> tuple[int, list[tuple[int, int]]]:
    """An HFSPlusForkData: its logical size, and its extents (block,
    count), the empty ones left out."""
    logical = struct.unpack_from(">Q", v, at)[0]
    extents = [e for e in (struct.unpack_from(">II", v, at + 16 + i * 8)
                           for i in range(8)) if e[1]]
    return logical, extents


def _node_size_of(tree: bytes) -> tuple[int, int, int]:
    """The B-tree's node size, and its first and last leaf node, from its
    header node (node 0), whose header record begins at the fixed
    offset 14."""
    if len(tree) < 14 + 20:
        raise ValueError("its B-tree is too small to hold a header node")
    if struct.unpack_from(">b", tree, 8)[0] != 1:
        raise ValueError("its B-tree's first node is not its header node")
    first, last = struct.unpack_from(">II", tree, 14 + 10)
    node_size = struct.unpack_from(">H", tree, 14 + 18)[0]
    if not 512 <= node_size <= 32768 or node_size & 511:
        raise ValueError(f"its B-tree's node size ({node_size}) is not one")
    nodes = len(tree) // node_size
    if first >= nodes or last >= nodes:
        raise ValueError("its B-tree's first or last leaf node does not exist")
    return node_size, first, last


def _node(tree: bytes, n: int, node_size: int) -> tuple[int, int, list[bytes]]:
    """A B-tree node's forward link, its kind (0: index, 1: header,
    2: map, -1: leaf), and its records."""
    at = n * node_size
    if at + node_size > len(tree):
        raise ValueError("its B-tree's node links reach past its end")
    kind = struct.unpack_from(">b", tree, at + 8)[0]
    count = struct.unpack_from(">H", tree, at + 10)[0]
    link = struct.unpack_from(">I", tree, at)[0]
    table = at + node_size - 2 * (count + 1)
    if table < at + 14:
        raise ValueError("its B-tree's node's record table reaches into its descriptor")
    offsets = struct.unpack_from(f">{count + 1}H", tree, table)[::-1]
    if any(a >= b or b > node_size for a, b in zip(offsets, offsets[1:])):
        raise ValueError("its B-tree's node's records are out of order")
    return link, kind, [bytes(tree[at + a:at + b]) for a, b in zip(offsets, offsets[1:])]


def _leaf_records(tree: bytes) -> Iterator[bytes]:
    """A B-tree's leaf records in order, from its first leaf node through
    each node's forward link to its last.  A damaged tree whose links go
    round is a damaged tree, not one read for ever."""
    node_size, first, last = _node_size_of(tree)
    seen, n = set(), first
    while n:
        if n in seen:
            raise ValueError("its B-tree's leaves link round in a circle")
        seen.add(n)
        link, kind, records = _node(tree, n, node_size)
        if kind != NODE_KIND_LEAF:
            raise ValueError("its B-tree's leaf link reaches a node that is not a leaf")
        yield from records
        if n == last:
            return
        n = link


def _unistr(raw: bytes) -> str:
    """An HFSUniStr255's text: UTF-16 big-endian, in the decomposed form
    the Mac wrote it in, normalized to composed for display."""
    return unicodedata.normalize("NFC", raw.decode("utf-16-be"))


def _record(rec: bytes) -> tuple[int, str, int, int]:
    """A catalog record's key's parent ID, its name, its record type and
    where its data begins."""
    if len(rec) < 8:
        raise ValueError("its catalog holds a record too small for a key")
    key_length, parent, name_length = struct.unpack_from(">H I H", rec, 0)
    if 8 + 2 * name_length > len(rec):
        raise ValueError("its catalog holds a record whose name reaches past it")
    name = _unistr(rec[8:8 + 2 * name_length])
    at = 2 + key_length
    if at + 2 > len(rec):
        raise ValueError("its catalog holds a record whose key reaches past it")
    return parent, name, struct.unpack_from(">H", rec, at)[0], at


def _spans(block_size: int, logical: int, extents: list[tuple[int, int]],
           overflow: dict[tuple[int, int, int], list[tuple[int, int]]],
           cnid: int, kind: int) -> list[tuple[int, int]]:
    """Where a fork's bytes are, relative to the volume: its inline
    extents', then as many extents-overflow records as the fork's
    logical size wants."""
    wanted = -(-logical // block_size)
    out, all_extents, idx, blocks = [], list(extents), 0, 0
    while blocks < wanted:
        if idx < len(all_extents):
            start, count = all_extents[idx]
            idx += 1
            if count:
                out.append((start * block_size, (start + count) * block_size))
                blocks += count
            continue
        more = overflow.get((kind, cnid, blocks))
        if more is None:
            raise ValueError(
                f"its extents end at block {blocks}, but file {cnid} wants {wanted} blocks")
        if not any(count for _, count in more):
            raise ValueError(f"its extents overflow file has no blocks for file {cnid}")
        all_extents += more
    return out


def _read_fork(data, logical: int, extents: list[tuple[int, int]],
               overflow: dict[tuple[int, int, int], list[tuple[int, int]]],
               cnid: int, kind: int, block_size: int) -> bytes:
    spans = _spans(block_size, logical, extents, overflow, cnid, kind)
    return b"".join(bytes(data[a:b]) for a, b in spans)[:logical]


def read_hfsplus(data) -> machfs.Volume:
    """The HFS+ volume data holds, as machfs reads an HFS volume one: the
    root folder's contents at the volume's root, each file's forks left
    on the disk until they're wanted."""
    vh_off = 1024
    if len(data) < vh_off + 512:
        raise ValueError("it is too small to hold an HFS+ volume header")
    v = bytes(memoryview(data[vh_off:vh_off + 512]))
    if v[:2] not in HFSX_SIGNATURES:
        raise ValueError(f"its volume header's signature is {v[:2]!r}, not an HFS+ one")
    version = struct.unpack_from(">H", v, 2)[0]
    if version not in (4, 5):
        raise ValueError(f"its HFS+ volume's version is {version}, not 4 or 5")
    crdate, mddate, bkdate = struct.unpack_from(">III", v, 16)
    block_size, total_blocks = struct.unpack_from(">II", v, 40)
    if not block_size or not total_blocks:
        raise ValueError("its HFS+ volume header holds no blocks")

    overflow: dict[tuple[int, int, int], list[tuple[int, int]]] = {}
    extents_logical, extents_inline = _fork_data(v, 192)
    if extents_logical:
        if sum(count for _, count in extents_inline) * block_size < extents_logical:
            raise ValueError("its extents overflow file spans past its inline extents, and nothing reads through it")
        extents_tree = b"".join(bytes(data[a:b]) for a, b in
                                _spans(block_size, extents_logical, extents_inline, {}, 3, EXTENT_DATA))[:extents_logical]
        for rec in _leaf_records(extents_tree):
            key_length = struct.unpack_from(">H", rec, 0)[0]
            at = 2 + key_length
            if at + 64 > len(rec):
                continue
            fork_type = rec[2]
            cnid = struct.unpack_from(">I", rec, 4)[0]
            start_block = struct.unpack_from(">I", rec, 8)[0]
            inline = [struct.unpack_from(">II", rec, at + i * 8) for i in range(8)]
            overflow[fork_type, cnid, start_block] = [e for e in inline if e[1]]

    catalog_logical, catalog_inline = _fork_data(v, 272)
    catalog = _read_fork(data, catalog_logical, catalog_inline, overflow, 4, EXTENT_DATA, block_size)
    cnids: dict[int, machfs.Folder | machfs.File] = {}
    children = []
    volume_name = None
    links: list[tuple[machfs.File, int]] = []
    for rec in _leaf_records(catalog):
        try:
            parent, name, record_type, at = _record(rec)
        except (UnicodeDecodeError, struct.error, ValueError) as e:
            raise ValueError(f"its catalog holds a record it cannot read ({e})") from None
        if record_type in (FOLDER_THREAD, FILE_THREAD):
            if parent == ROOT_FOLDER_ID:
                length = struct.unpack_from(">H", rec, at + 8)[0]
                volume_name = _unistr(rec[at + 10:at + 10 + 2 * length])
            continue
        if record_type == FOLDER_RECORD:
            cnid = struct.unpack_from(">I", rec, at + 8)[0]
            fdflags = struct.unpack_from(">H", rec, at + 56)[0]
            flags = struct.unpack_from(">H", rec, at + 2)[0]
            node = machfs.Folder()
            node.locked = bool(flags & 0x0001)
        elif record_type == FILE_RECORD:
            cnid = struct.unpack_from(">I", rec, at + 8)[0]
            fdflags = struct.unpack_from(">H", rec, at + 56)[0]
            data_length, data_extents = _fork_data(rec, at + 88)
            rsrc_length, rsrc_extents = _fork_data(rec, at + 168)
            node = OnDisk(data, {
                "data": (data_length,
                         _spans(block_size, data_length, data_extents, overflow, cnid, EXTENT_DATA)),
                "rsrc": (rsrc_length,
                         _spans(block_size, rsrc_length, rsrc_extents, overflow, cnid, EXTENT_RSRC)),
            })
            node.type, node.creator, node.flags, v, h = struct.unpack_from(">4s4sHhh", rec, at + 48)
            node.y, node.x = v, h
            node.attributes = node.flags & 0x7E
            flags = struct.unpack_from(">H", rec, at + 2)[0]
            node.locked = bool(flags & 0x0001)
            if (node.type, node.creator) == (b"hlnk", b"hfs+"):
                links.append((node, struct.unpack_from(">I", rec, at + 44)[0]))
        else:
            continue
        crdate_, moddate_ = struct.unpack_from(">II", rec, at + 12)
        backup_ = struct.unpack_from(">I", rec, at + 28)[0]
        node.crdate, node.mddate, node.bkdate = map(
            _gmt_to_local, (crdate_, moddate_, backup_))
        cnids[cnid] = node
        children.append((parent, name, cnid, node, bool(fdflags & INVISIBLE)))
    hidden = next((cnid for parent, name, cnid, node, invisible in children
                   if parent == ROOT_FOLDER_ID and name == "\0\0\0\0HFS+ Private Data"
                   and isinstance(node, machfs.Folder)), None)
    inodes = {name: node for parent, name, cnid, node, invisible in children
              if hidden is not None and parent == hidden}
    targets = {id(link): inodes.get(f"iNode{n}", link) for link, n in links}
    for parent, name, cnid, node, invisible in children:
        if invisible or cnid == hidden or parent == hidden:
            continue
        if parent != ROOT_PARENT_ID:
            folder = cnids.get(parent)
            if folder is None:
                raise ValueError(
                    f"its catalog's file {name!r} is in folder {parent}, which it does not hold")
            # long names are kept whole (machfs holds them); only names Mac Roman can't
            # spell, or a folder already has, get cut short and a '#' - node ID
            folder[_mac_name(name, cnid, folder, 1 << 30)] = targets.get(id(node), node)
    if ROOT_FOLDER_ID not in cnids:
        raise ValueError("its catalog holds no root folder")
    seen, todo = set(), [cnids[ROOT_FOLDER_ID]]
    while todo:                                         # a folder that is in itself?
        folder = todo.pop()
        if id(folder) in seen:
            raise ValueError("a folder on it is inside itself")
        seen.add(id(folder))
        todo.extend(v for v in folder.values() if isinstance(v, machfs.Folder))
    _link_aliases(crdate, cnids)
    vol = machfs.Volume()
    vol.update(cnids[ROOT_FOLDER_ID])
    for name in ("Desktop", "Desktop DB", "Desktop DF"):
        vol.pop(name, None)
    vol.crdate, vol.mddate, vol.bkdate = crdate, _gmt_to_local(mddate), _gmt_to_local(bkdate)
    vol.name = volume_name or "untitled"
    return vol
