"""Resources built byte by byte, as ResEdit or Rez would leave them, and
the host-side wrappers Mac files travel in."""

import binascii
import struct

from macrt.macfile import BINHEX_CHARS
from macrt.toolbox import constants as k


def pstr(s):
    b = s.encode("mac_roman")
    return bytes([len(b)]) + b


def wind(bounds, title, proc_id=k.documentProc, visible=True, go_away=True, ref_con=0,
         position=None):
    d = struct.pack(">4hhBxBxI", *bounds, proc_id, visible, go_away, ref_con) + pstr(title)
    if position is not None:
        d += bytes(len(d) & 1) + struct.pack(">H", position)
    return d


def dlog(bounds, title, items_id, proc_id=k.dBoxProc, visible=True, go_away=False, ref_con=0):
    return struct.pack(">4hhBxBxIh", *bounds, proc_id, visible, go_away, ref_con,
                       items_id) + pstr(title)


def alrt(bounds, items_id, stages=0x5555, position=None):
    d = struct.pack(">4hhH", *bounds, items_id, stages)
    return d + (struct.pack(">H", position) if position is not None else b"")


def ditl_item(kind, rect, body):
    return bytes(4) + struct.pack(">4h", *rect) + bytes([kind, len(body)]) + body + \
        bytes(len(body) & 1)


def ditl(*items):
    return struct.pack(">h", len(items) - 1) + b"".join(ditl_item(*i) for i in items)


def menu(menu_id, title, items, mdef=0):
    """items: (text, command key) pairs; text starting with ( is disabled."""
    flags = 1
    body = b""
    for i, (text, key) in enumerate(items, start=1):
        if not text.startswith("("):
            flags |= 1 << i
        body += pstr(text.lstrip("(")) + bytes([0, ord(key) if key else 0, 0, 0])
    return struct.pack(">hhhhhI", menu_id, 0, 0, mdef, 0, flags) + pstr(title) + body + b"\0"


def size_res(preferred, minimum, flags=0):
    return struct.pack(">HII", flags, preferred, minimum)


def vers(short, long):
    return bytes([1, 0, 0x80, 0, 0, 0]) + pstr(short) + pstr(long)


def binhex(name, data, rsrc, ftype=b"APPL", creator=b"TEST", flags=0):
    """A BinHex 4.0 file, with runs packed so the decoder's RLE is used."""
    def crc(b):
        return b + struct.pack(">H", binascii.crc_hqx(b, 0))
    n = name.encode("mac_roman")
    raw = crc(bytes([len(n)]) + n + b"\0" + ftype + creator +
              struct.pack(">HII", flags, len(data), len(rsrc))) + crc(data) + crc(rsrc)
    packed, i = bytearray(), 0
    while i < len(raw):
        c = raw[i]
        run = 1
        while i + run < len(raw) and raw[i + run] == c and run < 255:
            run += 1
        if c == 0x90:
            packed += b"\x90\x00"
            run = 1
        elif run >= 3:
            packed += bytes([c, 0x90, run])
        else:
            packed += bytes([c]) * run
        i += run
    packed += bytes(-len(packed) % 3)
    chars = bytearray()
    for j in range(0, len(packed), 3):
        v = int.from_bytes(packed[j:j + 3], "big")
        chars += bytes(BINHEX_CHARS[v >> s & 63] for s in (18, 12, 6, 0))
    lines = [bytes(chars[j:j + 64]) for j in range(0, len(chars), 64)]
    return (b"(This file must be converted with BinHex 4.0)\r\r:" + b"\r".join(lines) + b":\r")


def mfs_image(volume_name, files, blocks=391, block_size=1024):
    """A 400K MFS floppy, laid out the way the Mac's Initialize left one:
    boot blocks, the MDB and its block map at block 2, the directory at 4,
    the allocation blocks from 16.  files are (name, type, creator, data,
    rsrc, flags, (v, h)), and then the entry's own flags if not $80.
    Blocks are handed out from the end of the disk backwards, so every
    fork longer than a block is a chain the reader has to follow through
    the map."""
    img = bytearray(800 * 512)
    links = {}
    free = list(range(blocks + 1, 1, -1))

    def put(fork):
        if not fork:
            return 0
        chain = [free.pop(0) for _ in range(-(-len(fork) // block_size))]
        for n, nxt in zip(chain, chain[1:] + [1]):
            links[n] = nxt
            at = (16 * 512) + (n - 2) * block_size
            img[at:at + block_size] = fork[:block_size].ljust(block_size, b"\0")
            fork = fork[block_size:]
        return chain[0]

    def physical(fork):
        return -(-len(fork) // block_size) * block_size

    entries = bytearray()
    for num, (name, ftype, creator, data, rsrc, flags, where, *entry) in enumerate(files, 1):
        n = name.encode("mac_roman")
        e = (bytes([entry[0] if entry else 0x80, 0]) + ftype + creator
             + struct.pack(">Hhhh", flags, *where, 0)
             + struct.pack(">IHII", num, put(data), len(data), physical(data))
             + struct.pack(">HII", put(rsrc), len(rsrc), physical(rsrc))
             + struct.pack(">II", 0xA0000000 + num, 0xA0001000 + num) + bytes([len(n)]) + n)
        e += bytes(len(e) & 1)
        if len(entries) // 512 != (len(entries) + len(e) - 1) // 512:
            entries += bytes(-len(entries) % 512)     # entries never cross a block
        entries += e
    img[4 * 512:4 * 512 + len(entries)] = entries
    vn = volume_name.encode("mac_roman")
    mdb = (b"\xd2\xd7"
           + struct.pack(">IIHHHHHIIHIH", 0x9A000000, 0, 0, len(files), 4, 12, blocks,
                         block_size, 8192, 16, len(files) + 1, len(free))
           + bytes([len(vn)]) + vn.ljust(27, b"\0"))
    vabm = bytearray(-(-blocks * 3 // 2))
    for n, nxt in links.items():
        i = n - 2
        at = i * 3 // 2
        if i % 2 == 0:
            vabm[at] = nxt >> 4
            vabm[at + 1] |= (nxt & 0xF) << 4
        else:
            vabm[at] |= nxt >> 8
            vabm[at + 1] = nxt & 0xFF
    img[1024:1024 + 64 + len(vabm)] = mdb + vabm
    return bytes(img)


class HFSPlusFolder:
    def __init__(self, cnid, parent, name, crdate=0):
        self.cnid, self.parent, self.name, self.crdate = cnid, parent, name, crdate


class HFSPlusFile:
    def __init__(self, cnid, parent, name, ftype=b"TEXT", creator=b"TEST", data=b"", rsrc=b"",
                 finder_flags=0, where=(0, 0), flags=0, crdate=0, mddate=0, special=0):
        self.cnid, self.parent, self.name = cnid, parent, name
        self.ftype, self.creator, self.data, self.rsrc = ftype, creator, data, rsrc
        self.finder_flags, self.where, self.flags = finder_flags, where, flags
        self.crdate, self.mddate, self.special = crdate, mddate, special


def _btree_nodes(records, node_size):
    """A B-tree file of Technote 1150's layout: its header node, then
    leaf nodes holding the records in the order given, each node as full
    as it goes, linked forward and back.  No index nodes: nothing reads
    them."""
    leaves, node = [], []
    for rec in records:
        used = 14 + sum(len(r) for r in node) + 2 * (len(node) + 2)
        if node and used + len(rec) > node_size:
            leaves.append(node)
            node = []
        node.append(rec)
    if node:
        leaves.append(node)

    def build(flink, blink, kind, height, recs):
        out = bytearray(node_size)
        struct.pack_into(">IIbBH", out, 0, flink, blink, kind, height, len(recs))
        at, offsets = 14, []
        for r in recs:
            offsets.append(at)
            out[at:at + len(r)] = r
            at += len(r)
        offsets.append(at)                          # where the free space starts
        for i, o in enumerate(offsets):
            struct.pack_into(">H", out, node_size - 2 * (i + 1), o)
        return bytes(out)

    count = len(leaves)
    header = struct.pack(">HIIIIHHII", 1 if count else 0, 1 if count else 0,
                         len(records), 1 if count else 0, count, node_size, 516,
                         count + 1, 0) + bytes(106 - 30)
    nodes = [build(1 if count else 0, 0, 1, 0, [header, bytes(128), bytes(8)])]
    for i, recs in enumerate(leaves, 1):
        nodes.append(build(i + 1 if i < count else 0, i - 1, -1, 1, recs))
    return b"".join(nodes)


def hfs_plus_image(volume_name, items, block_size=512, node_size=512, crdate=0, mddate=0,
                   extent_blocks=1 << 30, signature=b"H+"):
    """An HFS Plus volume (Technote 1150, "HFS Plus Volume Format"): the
    volume header at 1024, then the catalog and extents overflow B-trees,
    then each fork, in extents of at most extent_blocks blocks; a fork in
    more than eight has the rest in the extents overflow file.  items are
    HFSPlusFolders and HFSPlusFiles, under the top folder (CNID 2), whose
    name is the volume's.  Every folder and file has its thread record."""
    blocks = []                                    # each allocation block's bytes
    reserved = -(-2048 // block_size)              # the boot blocks and the volume header
    blocks.extend(bytes(block_size) for _ in range(reserved))
    overflow = []

    def place(data):
        """Blocks for data: a list of extents (start, count)."""
        need, extents = -(-len(data) // block_size), []
        while need:
            n = min(need, extent_blocks)
            extents.append((len(blocks), n))
            for i in range(n):
                chunk = data[:block_size]
                data = data[block_size:]
                blocks.append(chunk.ljust(block_size, b"\0"))
            blocks.append(bytes(block_size))       # a gap, so extents don't join up
            need -= n
        return extents

    def fork_data(data, cnid, kind):
        extents = place(data)
        first, rest = extents[:8], extents[8:]
        done = sum(c for _, c in first)
        while rest:
            more, rest = rest[:8], rest[8:]
            key = struct.pack(">HBxII", 10, kind, cnid, done)
            overflow.append(key + b"".join(struct.pack(">II", *e) for e in more).ljust(64, b"\0"))
            done += sum(c for _, c in more)
        recs = b"".join(struct.pack(">II", *e) for e in first).ljust(64, b"\0")
        return struct.pack(">QII", len(data), 0, sum(c for _, c in extents)) + recs

    def key(parent, name):
        u = name.encode("utf-16-be")
        return struct.pack(">HIH", 6 + len(u), parent, len(u) // 2) + u

    def thread(kind, parent, name):
        u = name.encode("utf-16-be")
        return struct.pack(">hHIH", kind, 0, parent, len(u) // 2) + u

    records = []
    top = HFSPlusFolder(2, 1, volume_name, crdate)
    for item in [top, *items]:
        if isinstance(item, HFSPlusFolder):
            value = (struct.pack(">hHIIIIIII", 1, 0, 0, item.cnid, item.crdate, item.crdate,
                                 0, 0, 0)
                     + bytes(16) + bytes(16) + bytes(16) + bytes(8))
            records.append(((item.parent, item.name), key(item.parent, item.name) + value))
            records.append(((item.cnid, ""),
                            key(item.cnid, "") + thread(3, item.parent, item.name)))
        else:
            perms = struct.pack(">IIBBHI", 0, 0, 0, 0, 0, item.special)
            info = struct.pack(">4s4sHhhH", item.ftype, item.creator, item.finder_flags,
                               *item.where, 0)
            value = (struct.pack(">hHIIIIIII", 2, item.flags, 0, item.cnid, item.crdate,
                                 item.mddate, 0, 0, 0)
                     + perms + info + bytes(16) + bytes(8)
                     + fork_data(item.data, item.cnid, 0) + fork_data(item.rsrc, item.cnid, 0xFF))
            records.append(((item.parent, item.name), key(item.parent, item.name) + value))
            records.append(((item.cnid, ""),
                            key(item.cnid, "") + thread(4, item.parent, item.name)))
    records.sort(key=lambda r: (r[0][0], r[0][1].lower()))
    catalog = _btree_nodes([r for _, r in records], node_size)
    extents = _btree_nodes(sorted(overflow), node_size)

    def special(data):
        at = len(blocks)
        for i in range(0, len(data), block_size):
            blocks.append(data[i:i + block_size].ljust(block_size, b"\0"))
        n = -(-len(data) // block_size)
        return struct.pack(">QII", len(data), 0, n) + struct.pack(">II", at, n).ljust(64, b"\0")

    xt, cat = special(extents), special(catalog)
    header = (signature + struct.pack(">HIII", 4, 0, 0, 0)
              + struct.pack(">IIII", crdate, mddate, 0, 0)
              + struct.pack(">IIIIIIIIIIQ", 0, 0, block_size, len(blocks), 0, 0, 0, 0, 100, 0, 0)
              + bytes(32) + bytes(80) + xt + cat + bytes(80) + bytes(80))
    img = bytearray(b"".join(blocks))
    img[1024:1024 + len(header)] = header
    return bytes(img)


def hfs_wrapper(embedded, block_size=512, start=8):
    """An HFS volume whose MDB says it holds an HFS Plus one (Technote
    1150's HFS wrapper): drEmbedSigWord and drEmbedExtent, the embedded
    volume starting start allocation blocks into the wrapper's."""
    first = 4                                      # drAlBlSt: allocation blocks start at sector 4
    count = -(-len(embedded) // block_size)
    img = bytearray(first * 512 + (start + count) * block_size)
    struct.pack_into(">2s", img, 1024, b"BD")
    struct.pack_into(">I", img, 1024 + 20, block_size)
    struct.pack_into(">H", img, 1024 + 28, first)
    struct.pack_into(">2sHH", img, 1024 + 0x7C, b"H+", start, count)
    at = first * 512 + start * block_size
    img[at:at + len(embedded)] = embedded
    return bytes(img)


# The 64 bytes a GCR floppy holds, for 0 to 63.
GCR = bytes.fromhex("96979A9B9D9E9FA6A7ABACADAEAFB2B3B4B5B6B7B9BABBBCBDBEBFCBCDCECFD3"
                    "D6D7D9DADBDCDDDEDFE5E6E7E9EAEBECEDEEEFF2F3F4F5F6F7F9FAFBFCFDFEFF")
SYNC = "1111111100" * 6                     # the drive's self-sync bytes: $FF and two zeros


def gcr_sector(track, side, number, data, tag=bytes(12), marks=b"\xd5\xaa\xad",
               end=b"\xde\xaa"):
    """One sector as the Sony driver writes it, as bits: its header, then
    its tag and data, three bytes to four of the disk's, each mixed with
    one of three running sums.  The data field's marks before and after
    it can be others, as a key disk's are."""
    def disk(values):
        return "".join(format(b, "08b") for b in values)

    fields = (track & 0x3F, number, track >> 6 | side << 5, 0x22 if side else 0x02)
    header = b"\xd5\xaa\x96" + bytes(GCR[v] for v in fields + (fields[0] ^ fields[1]
                                     ^ fields[2] ^ fields[3],)) + b"\xde\xaa"
    src = tag + data
    a = b = c = 0
    six = []
    for g in range(175):
        x, y = src[3 * g], src[3 * g + 1]
        z = src[3 * g + 2] if g < 174 else 0
        c = c << 1 | c >> 7
        carry, c = c & 1, c & 0xFF
        ex = x ^ c
        a += x + carry
        carry, a = a >> 8, a & 0xFF
        ey = y ^ a
        b += y + carry
        carry, b = b >> 8, b & 0xFF
        ez = z ^ b
        if g < 174:
            c = (c + z + carry) & 0xFF
        six += [ex >> 2 & 0x30 | ey >> 4 & 0x0C | ez >> 6, ex & 0x3F, ey & 0x3F, ez & 0x3F]
        if g == 174:
            six.pop()
    six += [a >> 2 & 0x30 | b >> 4 & 0x0C | c >> 6, a & 0x3F, b & 0x3F, c & 0x3F]
    body = marks + bytes([GCR[number]]) + bytes(GCR[v] for v in six) + end
    return SYNC + disk(header) + SYNC[:50] + disk(body) + SYNC


def moof_image(raw, sides=1, kind=None, tracks=None, crc=True, flux=None):
    """An Applesauce MOOF image of a 400K (or 800K, sides=2) floppy holding
    raw's sectors, each track's sectors in reverse and the track begun in
    the middle of one.  tracks maps (track, side) to bits to put in place
    of what's written there, or None to leave the track out; flux maps
    them to flux changes to keep as well, each the 125 ns ticks since the
    last."""
    tmap, fmap, trks, blocks = (bytearray(b"\xff" * 160), bytearray(b"\xff" * 160),
                                bytearray(160 * 8), bytearray())
    n = 0

    def put(where, track, side, data, count):
        nonlocal n
        data += bytes(-len(data) % 512)
        where[track * 2 + side] = n
        struct.pack_into("<HHI", trks, n * 8, 3 + len(blocks) // 512, len(data) // 512, count)
        blocks.extend(data)
        n += 1

    at = 0
    for track in range(80):
        for side in range(sides):
            count = 12 - track // 16
            bits = "".join(gcr_sector(track, side, s, raw[(at + s) * 512:(at + s + 1) * 512])
                           for s in reversed(range(count)))
            at += count
            bits = bits[3000:] + bits[:3000]
            bits = (tracks or {}).get((track, side), bits)
            if bits is not None:
                data = int(bits + "0" * (-len(bits) % 8), 2).to_bytes(-(-len(bits) // 8), "big")
                put(tmap, track, side, data, len(bits))
    largest = 0
    for (track, side), gaps in (flux or {}).items():
        data = b"".join(b"\xff" * (g // 255) + bytes([g % 255]) for g in gaps)
        put(fmap, track, side, data, len(data))
        largest = max(largest, -(-len(data) // 512))
    where = 3 + len(blocks) // 512 if flux else 0          # the FLUX chunk follows the tracks
    info = (bytes([1, kind or sides, 0, 0, 16]) + b"macrt tests".ljust(32) + bytes(3)
            + struct.pack("<HH", where, largest) + bytes(16))

    def chunk(name, body):
        return name + struct.pack("<I", len(body)) + body

    rest = (chunk(b"INFO", info) + chunk(b"TMAP", bytes(tmap))
            + chunk(b"TRKS", bytes(trks) + bytes(blocks)))
    assert len(rest) - len(blocks) + 12 == 3 * 512          # the tracks start at block 3
    if flux:
        rest += chunk(b"FLUX", bytes(fmap))
    return b"MOOF\xff\n\r\n" + struct.pack("<I", binascii.crc32(rest) if crc else 0) + rest


def font_rec(first, last, glyphs, missing, ascent, descent, kern_max=0, leading=0,
             final_ow=True):
    """A FONT or NFNT: a FontRec with the characters first to last.
    glyphs maps a character code to (rows, offset, width): its image as
    strings of # and ., ascent + descent of them, how far right of
    kernMax it's drawn, and how far it moves the pen; codes it hasn't got
    are the missing glyph, missing.  Without final_ow the owTable stops
    at the missing glyph's entry, as many real fonts' do."""
    height = ascent + descent
    order = [glyphs.get(c) for c in range(first, last + 1)] + [missing]
    locs, x, placed = [], 0, []
    for g in order:
        locs.append(x)
        if g:
            placed.append((x, g[0]))
            x += len(g[0][0])
    locs.append(x)
    row_words = max(-(-x // 16), 1)
    strike = bytearray(row_words * 2 * height)
    for left, rows in placed:
        for y, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch == "#":
                    strike[y * row_words * 2 + (left + i) // 8] |= 0x80 >> ((left + i) % 8)
    ows = [g[1] << 8 | g[2] if g else 0xFFFF for g in order] + [0xFFFF] * final_ow
    n = last - first + 3
    ow_loc = (26 + len(strike) + 2 * n - 16) // 2
    header = struct.pack(">HHHhhhhhHhhhH", 0x9000, first, last, max(g[2] for g in order if g),
                         kern_max, -descent, x, height, ow_loc, ascent, descent, leading,
                         row_words)
    return (header + strike + struct.pack(f">{n}H", *locs)
            + struct.pack(f">{len(ows)}H", *ows))
