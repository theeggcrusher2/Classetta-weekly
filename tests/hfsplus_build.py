"""Build a tiny HFS Plus disk image in memory, for the tests to read
back.  Only what the reader looks at is written: the volume header,
the allocation bitmap, the catalog B-tree (header node plus as many
leaves as the records want), and each file's forks, laid out one block
after another."""
import math
import struct
import unicodedata

HFS_PLUS_BLOCK = 512
ROOT_PARENT_ID, ROOT_CNID = 1, 2
FIRST_CNID = 16

FOLDER, FILE, FOLDER_THREAD, FILE_THREAD = 0x0001, 0x0002, 0x0003, 0x0004
INVISIBLE = 0x4000


def _utf16(name):
    return unicodedata.normalize("NFD", name).encode("utf-16-be")


def _key(parent, name_bytes):
    name_len = len(name_bytes) // 2
    key_len = 4 + 2 + len(name_bytes)
    return struct.pack(">HIH", key_len, parent, name_len) + name_bytes


def _fork(logical, extents):
    fork = struct.pack(">QII", logical, 65536, sum(c for _, c in extents))
    for i in range(8):
        fork += struct.pack(">II", *(extents[i] if i < len(extents) else (0, 0)))
    return fork


def _permissions():
    return struct.pack(">IIBBHI", 0, 0, 0, 0, 0o100644, 0)


def _folder_record(cnid, valence, created, mod, frflags):
    data = struct.pack(">H I I", 0, valence, cnid)     # flags, valence, folderID
    data += struct.pack(">IIIII", created, mod, mod, mod, mod)
    data += _permissions()
    data += struct.pack(">4h H hh h", 0, 0, 0, 0, frflags, 0, 0, 0)   # frRect, frFlags, frLocation, frView
    data += bytes(16)                                  # DXInfo
    data += struct.pack(">II", 0, 0)                   # textEncoding, reserved
    return data


def _file_record(cnid, created, mod, ftype, creator, fdflags, flags,
                 data_extents, data_len, rsrc_extents, rsrc_len):
    data = struct.pack(">H I I", flags, 0, cnid)          # flags, reserved1, fileID
    data += struct.pack(">IIIII", created, mod, mod, mod, mod)   # create, contentMod, attrMod, access, backup
    data += _permissions()
    data += struct.pack(">4s4sHhhH", ftype, creator, fdflags, 0, 0, 0)   # FInfo
    data += bytes(16)                                                     # FXInfo
    data += struct.pack(">II", 0, 0)                                      # textEncoding, reserved2
    data += _fork(data_len, data_extents) + _fork(rsrc_len, rsrc_extents)
    return data


def _thread_record(record_type, parent, name):
    name_bytes = _utf16(name)
    return struct.pack(">h I", 0, parent) + struct.pack(">H", len(name_bytes) // 2) + name_bytes


def _node(node_type, records, node_size=HFS_PLUS_BLOCK):
    node = bytearray(node_size)
    node[8] = node_type & 0xFF
    struct.pack_into(">H", node, 10, len(records))
    offsets, at = [], 14
    for rec in records:
        node[at:at + len(rec)] = rec
        offsets.append(at)
        at += len(rec)
    offsets.append(at)
    table = node_size - 2 * len(offsets)
    struct.pack_into(f">{len(offsets)}H", node, table, *offsets[::-1])
    return node


def _header_node(depth, root, leaf_records, first_leaf, last_leaf, node_size, total_nodes):
    rec = struct.pack(">H I I I I H H I I H I BB I", depth, root, leaf_records,
                      first_leaf, last_leaf, node_size, 516, total_nodes, 0, 0,
                      node_size, 0, 0xCF, 0)
    rec = rec.ljust(88, b"\0") + bytes(64)           # reserved3
    return _node(1, [rec])


def _btree(records, node_size=HFS_PLUS_BLOCK):
    """header node plus leaves holding records (sorted), linked leaf to leaf."""
    leaves, current, current_size = [], [], 14
    for rec in records:
        table_size = 2 * (len(current) + 2)
        if current and current_size + len(rec) > node_size - table_size:
            leaves.append(current)
            current, current_size = [], 14
        current.append(rec)
        current_size += len(rec)
    if current or not leaves:
        leaves.append(current)
    nodes = len(leaves) + 1
    leaf_nodes = []
    for index, leaf in enumerate(leaves, 1):
        node = _node(0xFF, leaf, node_size)
        struct.pack_into(">I", node, 0, index + 1 if index < len(leaves) else 0)
        leaf_nodes.append(node)
    header = _header_node(1, 1, sum(len(l) for l in leaves), 1, len(leaves), node_size, nodes)
    return b"".join([bytes(header), *[bytes(n) for n in leaf_nodes]])




def _parent_id(entries, name):
    if name is None:
        return ROOT_CNID
    for entry in entries:
        if entry["name"] == name:
            return entry["id"]
    raise KeyError(f"no folder named {name!r} in the entries")


def _records(volume_name, entries, created):
    """Every catalog record as (sort key, record bytes): the root folder,
    the entries themselves, the folder threads.  Record sizes do not
    depend on where extents sit, so the catalog can be measured before
    blocks are laid out."""
    children = {}
    for entry in entries:
        parent = _parent_id(entries, entry.get("parent"))
        children[parent] = children.get(parent, 0) + 1

    records = []
    root_data = _folder_record(ROOT_CNID, children.get(ROOT_CNID, 0), created, created, 0)
    records.append((ROOT_PARENT_ID, "", FOLDER, root_data))
    for entry in entries:
        parent = _parent_id(entries, entry.get("parent"))
        if entry["kind"] == "folder":
            data = _folder_record(entry["id"], children.get(entry["id"], 0), created, created,
                                  entry.get("frflags", 0))
            records.append((parent, entry["name"], FOLDER, data))
            records.append((entry["id"], "", FOLDER_THREAD,
                            _thread_record(FOLDER_THREAD, parent, entry["name"])))
        else:
            flags = 0x0002 | (0x0001 if entry.get("locked") else 0)
            data = _file_record(entry["id"], created, created,
                                entry.get("type", b"\0\0\0\0"), entry.get("creator", b"\0\0\0\0"),
                                entry.get("fdflags", 0), flags,
                                entry.get("_data_extents", []), len(entry.get("data", b"")),
                                entry.get("_rsrc_extents", []), len(entry.get("rsrc", b"")))
            records.append((parent, entry["name"], FILE, data))
    records.append((ROOT_CNID, "", FOLDER_THREAD,
                    _thread_record(FOLDER_THREAD, ROOT_PARENT_ID, volume_name)))
    packed = []
    for parent, name, record_type, data in records:
        name_bytes = _utf16(name)
        key = _key(parent, name_bytes)
        packed.append((parent, name_bytes, key + struct.pack(">H", record_type) + data))
    packed.sort(key=lambda item: (item[0], item[1]))
    return [record for _, _, record in packed]



def hfsplus_image(volume_name, entries, created=0xA0000000, block_size=HFS_PLUS_BLOCK):
    """The image bytes.  entries are dicts: kind "folder" or "file"; folders
    take name, parent (a folder's name, or None for the root), frflags; files
    take name, parent, type, creator, data, rsrc, fdflags, locked.  Dates
    are the Mac epoch."""
    entries = [dict(entry) for entry in entries]
    cnid = FIRST_CNID
    for entry in entries:
        entry["id"] = cnid
        cnid += 1

    def fork_counts(entry):
        d = entry.get("data", b"")
        r = entry.get("rsrc", b"")
        return (math.ceil(len(d) / block_size) if d else 0,
                math.ceil(len(r) / block_size) if r else 0)

    # Extent counts are fixed per entry, so measure the catalog with
    # placeholder extents, then lay the blocks out for real.
    for entry in entries:
        if entry["kind"] != "file":
            continue
        d_count, r_count = fork_counts(entry)
        entry["_data_extents"] = [(0, d_count)] if d_count else []
        entry["_rsrc_extents"] = [(0, r_count)] if r_count else []
    catalog = _btree(_records(volume_name, entries, created), node_size=block_size)
    catalog_blocks = math.ceil(len(catalog) / block_size)

    out = 5 + catalog_blocks
    for entry in entries:
        if entry["kind"] != "file":
            continue
        d_count, r_count = fork_counts(entry)
        entry["_data_extents"] = [(out, d_count)] if d_count else []
        entry["_rsrc_extents"] = [(out + d_count, r_count)] if r_count else []
        out += d_count + r_count
    catalog = _btree(_records(volume_name, entries, created), node_size=block_size)
    total_blocks = 5 + catalog_blocks + sum(
        sum(c for _, c in e["_data_extents"] + e["_rsrc_extents"])
        for e in entries if e["kind"] == "file")

    img = bytearray(total_blocks * block_size)
    bitmap = bytearray(block_size)
    for block in range(total_blocks):
        bitmap[block // 8] |= 0x80 >> (block % 8)
    img[3 * block_size:4 * block_size] = bitmap
    img[5 * block_size:5 * block_size + len(catalog)] = catalog
    for entry in entries:
        if entry["kind"] != "file":
            continue
        for key, extents in (("data", entry["_data_extents"]), ("rsrc", entry["_rsrc_extents"])):
            payload, at = entry.get(key, b""), 0
            for start, count in extents:
                img[start * block_size:(start + count) * block_size] = \
                    payload[at:at + count * block_size].ljust(count * block_size, b"\0")
                at += count * block_size
    used = total_blocks
    img[1024:1536] = _volume_header(volume_name, entries, created, block_size,
                                    total_blocks, len(catalog), used, cnid, catalog_blocks)
    return bytes(img)


def _volume_header(volume_name, entries, created, block_size,
                   total_blocks, catalog_len, used_blocks, next_cnid, catalog_blocks):
    files = sum(1 for e in entries if e["kind"] == "file")
    folders = sum(1 for e in entries if e["kind"] == "folder")
    vh = b"H+" + struct.pack(">H", 4)                # signature, version
    vh += struct.pack(">I", 0)                      # attributes: clean unmount, not journaled
    vh += b"H+Lx"                                   # lastMountedVersion
    vh += struct.pack(">I", 0)                      # journalInfoBlock
    vh += struct.pack(">IIII", created, created, 0, created)  # create, modify, backup, checked
    vh += struct.pack(">II", files, folders)
    vh += struct.pack(">IIII", block_size, total_blocks, total_blocks - used_blocks, 0)
    vh += struct.pack(">III", 65536, 65536, next_cnid)        # rsrc clump, data clump, next cnid
    vh += struct.pack(">I", 1)                                # write count
    vh += struct.pack(">Q", 0)                                # encodings bitmap
    vh += bytes(32)                                           # finder info
    vh += _fork(block_size, [(3, 1)])                         # allocation file
    vh += _fork(0, [])                                        # extents overflow file
    vh += _fork(catalog_len, [(5, catalog_blocks)])           # catalog file
    vh += _fork(0, [])                                        # attributes file
    vh += _fork(0, [])                                        # startup file
    return vh.ljust(512, b"\0")
