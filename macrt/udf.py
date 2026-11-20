"""
Read-only UDF 1.01-1.02 volumes (ECMA-167 / ISO/IEC 13346, the format old
DVDs carry when they carry no HFS or ISO 9660 view), presented the way
macrt.hfsplus presents an HFS Plus one: machfs folders and files at the
volume's root, each file's data left on the disc until it's wanted.

The volume descriptor walk: an Anchor Volume Descriptor Pointer (tag 2)
at logical block 256 names the extent holding the volume descriptor set;
the Logical Volume Descriptor (tag 6) among them names, in its
logicalVolContentsUse long_ad, the block holding the File Set Descriptor
(tag 256/0x0100), whose rootDirectoryICB long_ad names the root directory.
The File Set is the same whether found through the LVD or read at the
location the LVD announces; the LVD route is the robust one when the disc
was closed with more than one file set.

Supported:
  * one logical volume, partitions of type 1 (Type1PartitionMap) mapped to
    Partition Descriptors (tag 5); physical block = (partitionStarting-
    Location + LBN) * LogicalBlockSize
  * File Entries (tag 261/0x0105) and Extended File Entries (tag 266/0x010A)
  * ICB strategy type 4 only (the plain one; 4096 is WORM-only)
  * allocation descriptors: long_ad (16 bytes) and short_ad (8 bytes);
    file data may also live inside the File Entry itself (AD type 3,
    "in-ICB"), which is presented as a span into the image like any other
  * OSTA CS0 names: compression id 8 (one byte per character, latin-1-ish)
    and 16 (two bytes per character, effectively big-endian); long names
    kept whole
  * ECMA-167 timestamps, 12 bytes (typeAndTimezone + Int16 year + 8 bytes),
    in both the File Entry's access/modification/attr times (FE) and the
    EFE's access/modification/create/attr times; converted to Mac 1904-epoch
    seconds; fields that do not parse become 0
  * hidden FIDs (file characteristic 0x01) are left out of the view, the
    way HFS+ invisible files are; deleted FIDs (0x04) and metadata FIDs
    (0x10, e.g. a VAT file) are skipped likewise

Refused by name, all as plain ValueError so load_volume's try/except can
fall through (macrt.macfile's UDF wiring is a separate step):
  * "the UDF disc is unsupported (it carries UDF 2.00)" -- the LVD Domain
    Identifier's UDFRevision suffix is 0x0200 or later
  * "the UDF disc is unsupported (it is written with a virtual allocation table)"
    -- a type 2 partition map whose ident is "*UDF Virtual Partition"
  * "the UDF disc is unsupported (it carries a sparable partition map)"
    -- a type 2 partition map whose ident is "*UDF Sparable Partition"
  * "the UDF disc is unsupported (it carries a metadata partition map)"
    -- a type 2 partition map whose ident is "*UDF Metadata Partition"
    (UDF 2.5+), any other type 2 map is refused likewise
  * "the UDF disc is unsupported (it carries ICB strategy type N)" -- ICB
    strategy other than type 4
  * "the UDF disc is unsupported (it carries allocation extent descriptors)"
    -- extent flag 0xC0000000 chaining to another allocation extent
  * "the UDF disc is unsupported (it carries sparse extents)" -- extent flags
    0x40000000/0x80000000 (unallocated); there is no on-disk analogue to map
    onto OnDisk's spans
  * "the UDF disc is damaged (...)" -- a descriptor tag whose checksum or
    CRC-16-ITU-T (poly 0x1021, seed 0) does not verify, a tag whose location
    is not the block it was read from, or a descriptor sequence with no
    Logical Volume Descriptor
  * "it is not a UDF disc" -- nothing recognisable at sector 256

Layout references: ECMA-167 2nd edition, part 1 (7.2 descriptor tag, 7.3
timestamp, 7.4 entity identifier), part 3 (10.2 AVDP, 10.5 Partition
Descriptor, 10.6 LVD) and part 4 (14.1 File Set Descriptor, 14.4 File
Identifier Descriptor, 14.6 ICB Tag, 14.9 File Entry, 14.17 Extended File
Entry); UDF 2.01 section 2.1.1 (OSTA CS0) and 2.2.4.4 (logicalVolContentsUse
is the long_ad locating the File Set).
"""
import calendar
import struct
import unicodedata
from collections.abc import Iterator

import machfs

from .macfile import OnDisk, _gmt_to_local, _mac_name, MAC_1904

SECTOR = 2048                           # UDF logical block size; AVDP is at sector 256
AVDP_LBN = 256
VRS_FIRST_LBN, VRS_LAST_LBN = 16, 32    # Volume Recognition Sequence lives here
TAG_PVD, TAG_AVDP = 0x0001, 0x0002                       # 1, 2
TAG_IUVD, TAG_PD, TAG_LVD, TAG_USD, TAG_TD = 0x0004, 0x0005, 0x0006, 0x0007, 0x0008     # 4..8
TAG_FSD, TAG_FID, TAG_FE, TAG_EFE = 0x0100, 0x0101, 0x0105, 0x010A
AD_SHORT, AD_LONG, AD_EXT, AD_IN_ICB = 0, 1, 2, 3       # icbTag.flags & 0x0007
FE_DIR, FE_FILE = 4, 5                                  # icbTag.fileType
FID_HIDDEN, FID_DIRECTORY, FID_DELETED, FID_PARENT, FID_METADATA = 0x01, 0x02, 0x04, 0x08, 0x10
EXT_TYPE_MASK = 0xC0000000
EXT_NEXT_EXTENT_ALLOCDESCS = 0xC0000000                 # extent chaining to more allocation descriptors
EXT_NOT_RECORDED = 0x40000000                           # allocated, not recorded; or not allocated (0x80000000)
EXT_LENGTH_MASK = 0x3FFFFFFF
STRATEGY_PLAIN = 4                                      # kUdfStrategyB, the plain one
MAC_1904_EPOCH = MAC_1904
# descriptor tag: tagIdent le16, descVersion le16, tagChecksum u8, reserved u8,
# tagSerialNum le16, descriptorCRC le16, descriptorCRCLength le16, tagLocation le32
TAG = struct.Struct("<HHBBHHHI")
LONG_AD = struct.Struct("<I I H 6x")    # extLength le32, lb_addr (LBN le32, partitionRef le16), impUse 6
SHORT_AD = struct.Struct("<II")         # extLength le32, extPosition le32
FE_FIXED, EFE_FIXED = 176, 216          # bytes before extendedAttr[] in FE / EFE
FID_FIXED = 38                          # tag + fileVersionNum + chars + id len + icb long_ad + impUse len


def _crc_itu(buf: bytes) -> int:
    """CRC-16-ITU-T (ITU-T V.41, poly x16+x12+x5+1), seed 0, unreflected:
    what a descriptor tag's descriptorCRC shall hold.  Checked against the
    ECMA-167 note's example: CRC of #70 #6A #77 is #3299."""
    crc = 0
    for byte in buf:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _cs0_decode(raw: bytes) -> str:
    """An OSTA CS0 d-character string: first byte the compression id, 8
    literal latin-1-ish bytes per character or 16 two bytes per character
    (big-endian by construction).  254/255 mean an empty, unique name
    (only for deleted FIDs)."""
    if not raw:
        return ""
    cid = raw[0]
    if cid == 8:
        return raw[1:].decode("latin-1")
    if cid == 16:
        body = raw[1:]
        return "".join(chr(struct.unpack_from(">H", body, i)[0]) for i in range(0, len(body) - 1, 2))
    if cid in (254, 255):
        return ""
    raise ValueError(f"the UDF disc is damaged (it carries a file name with compression id {cid})")


def _dstring_cs0(field: bytes) -> str:
    """An ECMA-167 dstring whose content is CS0: its last byte is the
    count of bytes used.  All-zero means not recorded."""
    if not field or field[-1] == 0:
        return ""
    used = field[-1]
    if used > len(field):                # a length byte past the field: truncation
        used = len(field)
    try:
        return _cs0_decode(field[:used]).rstrip("\0")
    except ValueError:
        return field[:used].decode("latin-1", errors="replace").rstrip("\0")


def _timestamp(buf: bytes, at: int) -> int:
    """An ECMA-167 timestamp (12 bytes: typeAndTimezone le16, year Int16
    LE, then month day hour minute second centiseconds hundredsOf-
    Microseconds microseconds) as Mac 1904-epoch seconds.  All-zero means
    'not recorded'; fields out of range give 0.  UDF timestamps are local
    time (TR/112 2.1.1): the wall clock is converted as-is; for type 0
    (UTC) the local offset is applied, the way the HFS+ reader makes its
    GMT dates local.  The timezone offset in the field is ignored -- there
    is no timezone database here -- noted as an assumption."""
    try:
        type_and_tz, year = struct.unpack_from("<HH", buf, at)
        rest = buf[at + 4:at + 12]
        if len(rest) < 8:
            return 0
        if not (type_and_tz or year or any(rest)):
            return 0                          # all zero: date and time not specified
        month, day, hour, minute, second = rest[:5]
        if not (1 <= year <= 9999 and 1 <= month <= 12 and 1 <= day <= 31
                and hour <= 23 and minute <= 59 and second <= 60):
            return 0
        ty = (type_and_tz >> 12) & 0xF
        secs = calendar.timegm((year, month, day, hour, minute, second, 0, 0, -1)) + MAC_1904_EPOCH
        if ty == 0:                         # UTC: present it local, as HFS+ does for its GMT dates
            secs = _gmt_to_local(secs & 0xFFFFFFFF)
        return secs & 0xFFFFFFFF
    except (ValueError, OverflowError, struct.error):
        return 0


def _tag_checksum_ok(block: bytes) -> bool:
    """Bytes 0-3 and 5-15 of the tag sum modulo 256 to the tagChecksum
    (ECMA-167 3/7.2.3: bytes 0-3 and 5-15)."""
    return (sum(block[0:4]) + sum(block[5:16])) & 0xFF == block[4]


def _check_tag(block: bytes, lbn: int, what: str) -> tuple[int, int]:
    """Validate a descriptor tag: its checksum, its descriptorCRC over the
    bytes the CRC length names, and its location (the block it was read
    from).  Returns (tagIdent, descriptorCRCLength)."""
    if len(block) < 16:
        raise ValueError(f"the UDF disc is damaged ({what} at block {lbn} is too short for a tag)")
    if not _tag_checksum_ok(block):
        raise ValueError(f"the UDF disc is damaged ({what} at block {lbn} fails its tag checksum)")
    ident, version, _chk, _res, _serial, crc, crc_len, location = TAG.unpack_from(block, 0)
    if 16 + crc_len > len(block):
        raise ValueError(f"the UDF disc is damaged ({what} at block {lbn}'s descriptor CRC runs past its block)")
    if _crc_itu(block[16:16 + crc_len]) != crc:
        raise ValueError(f"the UDF disc is damaged ({what} at block {lbn} fails its descriptor CRC)")
    if location != lbn:
        raise ValueError(f"the UDF disc is damaged ({what} at block {lbn} is tagged for block {location})")
    return ident, crc_len


def _check_fid_tag(stream: bytes, off: int) -> tuple[int, int]:
    """The same checks as _check_tag for a File Identifier Descriptor, whose
    tag locations the owning extent does not track reliably (FIDs of a
    directory span blocks), so the location is not enforced.  Checksum and
    CRC are."""
    block = stream[off:]
    if len(block) < 16:
        raise ValueError("the UDF disc is damaged (a file identifier descriptor is too short for a tag)")
    if not _tag_checksum_ok(block):
        raise ValueError(f"the UDF disc is damaged (a file identifier descriptor at offset {off} fails its tag checksum)")
    ident, _version, _chk, _res, _serial, crc, crc_len, _loc = TAG.unpack_from(block, 0)
    if 16 + crc_len > len(block):
        raise ValueError(f"the UDF disc is damaged (a file identifier descriptor at offset {off}'s CRC runs past the directory)")
    if _crc_itu(block[16:16 + crc_len]) != crc:
        raise ValueError(f"the UDF disc is damaged (a file identifier descriptor at offset {off} fails its descriptor CRC)")
    return ident, crc_len


class _Reader:
    """The disc, its partitions and its blocks-of-2048 view."""

    def __init__(self, data):
        self.data = data
        self.block_size = SECTOR
        self.block_size_set = False
        # partition reference number -> (starting logical block, partition length in blocks)
        self.partitions: dict[int, tuple[int, int]] = {}

    # -- blocks -----------------------------------------------------------
    def _block(self, lbn: int) -> bytes:
        off = lbn * self.block_size
        if off + self.block_size > len(self.data):
            raise ValueError(f"the UDF disc is damaged (it ends before block {lbn} does)")
        return bytes(self.data[off:off + self.block_size])

    def _phys_off(self, lbn: int, part: int) -> int:
        base, length = self.partitions[part]
        if lbn >= length:
            raise ValueError(f"the UDF disc is damaged (it addresses block {lbn} of a partition {length} blocks long)")
        return (base + lbn) * self.block_size

    def _phys_block(self, lbn: int, part: int) -> bytes:
        off = self._phys_off(lbn, part)
        if off + self.block_size > len(self.data):
            raise ValueError(f"the UDF disc is damaged (it ends before block {lbn} of partition {part} does)")
        return bytes(self.data[off:off + self.block_size])

    # -- the anchor and the descriptor set --------------------------------
    def _vrs_seen(self) -> bool:
        """Whether a Volume Recognition Sequence (BEA01/NSR02/NSR03/TEA01,
        the sectors ECMA-167 2/8 puts at 16...31) is there: used only to
        tell 'not a UDF disc' from 'a damaged one'."""
        for lbn in range(VRS_FIRST_LBN, VRS_LAST_LBN + 1):
            off = lbn * self.block_size
            if off + 7 > len(self.data):
                break
            marker = bytes(self.data[off + 1:off + 6])
            if marker in (b"BEA01", b"NSR02", b"NSR03", b"TEA01"):
                return True
        return False

    def _anchor(self) -> tuple[int, int, int, int] | None:
        """The AVDP at block 256 as (main_bn, main_len, reserve_bn,
        reserve_len) in logical blocks, or None if what sits at 256 is
        not one."""
        off = AVDP_LBN * self.block_size
        if off + self.block_size > len(self.data):
            return None
        block = bytes(self.data[off:off + self.block_size])
        try:
            ident, _crc_len = _check_tag(block, AVDP_LBN, "its anchor volume descriptor pointer")
        except ValueError:
            return None
        if ident != TAG_AVDP:
            return None
        (main_len, main_bn, reserve_len, reserve_bn) = struct.unpack_from("<IIII", block, 16)
        if not main_len or not main_bn:
            return None
        return main_bn, main_len, reserve_bn, reserve_len

    def _vds_walk(self, bn: int, length: int, what: str) -> Iterator[tuple[bytes, int, int]]:
        """Yields (block, tagIdent, lbn) for each tagged descriptor in the
        extent, validating each tag."""
        blocks = (length + self.block_size - 1) // self.block_size  # extent_ad.extLength is bytes
        end = min(bn + blocks, (len(self.data) + self.block_size - 1) // self.block_size)
        for lbn in range(bn, end):
            off = lbn * self.block_size
            if off + self.block_size > len(self.data):
                break
            block = bytes(self.data[off:off + self.block_size])
            ident, _crc_len = _check_tag(block, lbn, what)
            yield block, ident, lbn

    # -- the volume descriptor set -----------------------------------------
    def _lvd_parse(self, block: bytes):
        """The fields the LVD is read for: block size, UDF revision, the
        logical volume identifier (the displayed name), the File Set's
        long_ad, and the partition maps."""
        block_size = struct.unpack_from("<I", block, 212)[0]
        if block_size != self.block_size:
            raise ValueError(f"the UDF disc is unsupported (it carries logical block size {block_size}, not {self.block_size})")
        ident = block[216:240]                      # domainIdent regid: flags, ident[23]
        ident_str = ident[1:].split(b"\0", 1)[0]
        udf_revision = None
        if ident_str == b"*OSTA UDF Compliant":
            suffix = block[216 + 24:216 + 32]       # regid identSuffix[8]
            udf_revision = struct.unpack_from("<H", suffix, 0)[0]
            if udf_revision >= 0x0200:
                major, minor = udf_revision >> 8, udf_revision & 0xFF
                raise ValueError(f"the UDF disc is unsupported (it carries UDF {major}.{minor:02d})")
        # all-zero domain ident: ignored, per ECMA-167 3/10.6.5
        vol_ident = _dstring_cs0(block[84:84 + 128])
        map_table_len, num_maps = struct.unpack_from("<II", block, 264)
        # logicalVolContentsUse is a long_ad at block 248: extLength, then LBN, then partitionRef
        fs_lbn = struct.unpack_from("<I", block, 252)[0]
        fs_part = struct.unpack_from("<H", block, 256)[0]
        maps_bytes = block[440:440 + map_table_len]
        maps = []
        offset, scanned = 0, 0
        while scanned < num_maps:
            if offset + 2 > len(maps_bytes):
                raise ValueError("the UDF disc is damaged (its partition maps run past the logical volume descriptor)")
            mtype, mlen = maps_bytes[offset], maps_bytes[offset + 1]
            if mlen == 0 or offset + mlen > len(maps_bytes):
                raise ValueError("the UDF disc is damaged (a partition map has no length)")
            if mtype == 1:
                if mlen < 6:
                    raise ValueError("the UDF disc is damaged (a type 1 partition map is too short)")
                _vol_seq, part_num = struct.unpack_from("<HH", maps_bytes, offset + 2)
                maps.append(("type1", part_num))
            elif mtype == 2:
                if mlen < 64:
                    raise ValueError("the UDF disc is damaged (a type 2 partition map is too short)")
                pident = maps_bytes[offset + 5:offset + 28].split(b"\0", 1)[0].decode("latin-1")
                if pident == "*UDF Virtual Partition":
                    raise ValueError("the UDF disc is unsupported (it is written with a virtual allocation table)")
                if pident == "*UDF Sparable Partition":
                    raise ValueError("the UDF disc is unsupported (it carries a sparable partition map)")
                if pident == "*UDF Metadata Partition":
                    raise ValueError("the UDF disc is unsupported (it carries a metadata partition map)")
                raise ValueError(f"the UDF disc is unsupported (it carries a partition map type 2, {pident!r})")
            else:
                raise ValueError(f"the UDF disc is unsupported (it carries a partition map of type {mtype})")
            offset += mlen
            scanned += 1
        return block_size, udf_revision, vol_ident, (fs_lbn, fs_part), maps

    def _vds_scan(self):
        """The prevailing MVD/LVD/PDs, main descriptor sequence first, then
        the reserve one if the main yields no LVD.  Terminating Descriptors
        close a sequence."""
        anchor = self._anchor()
        if anchor is None:
            if self._vrs_seen():
                raise ValueError("the UDF disc is damaged (its anchor volume descriptor pointer cannot be read)")
            raise ValueError("it is not a UDF disc")
        main_bn, main_len, reserve_bn, reserve_len = anchor
        pvd = lvd = None
        pvd_seq = lvd_seq = -1
        pds = {}
        seen_any = 0
        for bn, byte_len, what in ((main_bn, main_len, "main volume descriptor sequence"),
                                   (reserve_bn, reserve_len, "reserve volume descriptor sequence")):
            if not bn or not byte_len:
                continue
            closed = False
            for block, ident, lbn in self._vds_walk(bn, byte_len, what):
                seen_any += 1
                seq = struct.unpack_from("<I", block, 16)[0] if ident in (TAG_PVD, TAG_LVD, TAG_PD) else 0
                if ident == TAG_TD:
                    closed = True
                    break
                if ident == TAG_PVD:
                    if seq >= pvd_seq:
                        pvd, pvd_seq = block, seq
                elif ident == TAG_LVD:
                    if seq >= lvd_seq:
                        lvd, lvd_seq = block, seq
                elif ident == TAG_PD:
                    pds[struct.unpack_from("<H", block, 22)[0]] = block
                # VDP, IUVD, USD, LVID and anything else tagged: checksummed, unused
            if lvd is not None and pvd is not None:
                break               # prevailed; no need to walk the reserve
            if closed and lvd is not None:
                break
        if lvd is None:
            if seen_any:
                raise ValueError("the UDF disc is damaged (its volume descriptor sequence holds no logical volume descriptor)")
            raise ValueError("the UDF disc is damaged (its volume descriptor sequence cannot be read)")
        return pvd, lvd, pds

    def _partitions(self, maps, pds) -> dict[int, tuple[int, int]]:
        parts = {}
        for ref, (kind, part_num) in enumerate(maps):
            if kind != "type1":
                continue        # type 2 maps were refused at parse time
            pd = pds.get(part_num)
            if pd is None:
                raise ValueError(f"the UDF disc is damaged (no partition descriptor for partition number {part_num})")
            starting, length = struct.unpack_from("<II", pd, 188)
            parts[ref] = (starting, length)
        return parts

    # -- file entries --------------------------------------------------------
    def _file_entry(self, lbn: int, part: int) -> dict:
        if part not in self.partitions:
            raise ValueError(f"the UDF disc is damaged (it points at partition {part}, which it does not have)")
        block = self._phys_block(lbn, part)
        abs_off = self._phys_off(lbn, part)
        ident, _crc_len = _check_tag(block, lbn, "a file entry")
        if ident == TAG_FE:
            efe, fixed = False, FE_FIXED
        elif ident == TAG_EFE:
            efe, fixed = True, EFE_FIXED
        else:
            raise ValueError(f"the UDF disc is damaged (a file entry at block {lbn} is tagged {ident})")
        prior, strategy, _strat_param, _num_entries, _reserved, file_type, _parent_lbn, _parent_part, flags = struct.unpack_from("<IHHHBBIHH", block, 16)
        if strategy != STRATEGY_PLAIN:
            raise ValueError(f"the UDF disc is unsupported (it carries ICB strategy type {strategy})")
        ad_type = flags & 0x0007
        if ad_type == AD_EXT:
            raise ValueError("the UDF disc is unsupported (it carries allocation extent descriptors)")
        if ad_type not in (AD_SHORT, AD_LONG, AD_IN_ICB):
            raise ValueError(f"the UDF disc is damaged (a file entry at block {lbn} has allocation descriptor type {ad_type})")
        information_length = struct.unpack_from("<Q", block, 56)[0]
        if efe:
            access_off, mod_off, create_off, attr_off, unique_off, lea_off, lad_off = 80, 92, 104, 116, 200, 208, 212
        else:
            access_off, mod_off, create_off, attr_off, unique_off, lea_off, lad_off = 72, 84, None, 96, 160, 168, 172
        mddate = _timestamp(block, mod_off)
        entry = {
            "block": block,
            "lbn": lbn,
            "part": part,
            "abs_off": abs_off,
            "fixed": fixed,
            "efe": efe,
            "file_type": file_type,
            "ad_type": ad_type,
            "information_length": information_length,
            "unique_id": struct.unpack_from("<Q", block, unique_off)[0],
            "crdate": _timestamp(block, create_off) if create_off is not None else mddate,
            "mddate": mddate,
            "bkdate": _timestamp(block, attr_off),
            "ea_len": struct.unpack_from("<I", block, lea_off)[0],
            "ad_len": struct.unpack_from("<I", block, lad_off)[0],
        }
        entry["ad_off"] = fixed + entry["ea_len"]
        if entry["ad_off"] + entry["ad_len"] > len(block):
            raise ValueError(f"the UDF disc is damaged (a file entry at block {lbn}'s extents run past it)")
        return entry

    def _extent_spans(self, fe: dict) -> list[tuple[int, int]]:
        """Where a file's bytes are, as byte spans into the image.  Allocation
        descriptors are read relative to the file entry's own partition, the
        baseline-UDF case; long_ad partition references are not honoured."""
        if fe["ad_type"] == AD_IN_ICB:
            start = fe["abs_off"] + fe["ad_off"]
            return [(start, start + fe["information_length"])]
        spans = []
        ad_size = SHORT_AD.size if fe["ad_type"] == AD_SHORT else LONG_AD.size
        if fe["ad_len"] % ad_size:
            raise ValueError(f"the UDF disc is damaged (a file entry at block {fe['lbn']} has a ragged list of extents)")
        at = fe["ad_off"]
        for _ in range(fe["ad_len"] // ad_size):
            if fe["ad_type"] == AD_SHORT:
                ext_length, lbn = SHORT_AD.unpack_from(fe["block"], at)
            else:
                ext_length, lbn, _part_ref = LONG_AD.unpack_from(fe["block"], at)
            at += ad_size
            etype, length = ext_length & EXT_TYPE_MASK, ext_length & EXT_LENGTH_MASK
            if etype == EXT_NEXT_EXTENT_ALLOCDESCS:
                raise ValueError("the UDF disc is unsupported (it carries allocation extent descriptors)")
            if etype:
                raise ValueError("the UDF disc is unsupported (it carries sparse extents)")
            phys = self._phys_off(lbn, fe["part"])
            spans.append((phys, phys + length))
        return spans

    def _stream_bytes(self, fe: dict) -> bytes:
        spans = self._extent_spans(fe)
        return b"".join(bytes(self.data[a:b]) for a, b in spans)[:fe["information_length"]]

    # -- directories -----------------------------------------------------------
    def _fid_iter(self, stream: bytes) -> Iterator[tuple[int, int, int, bytes]]:
        off = 0
        while off + FID_FIXED <= len(stream):
            ident, _crc_len = _check_fid_tag(stream, off)
            if ident != TAG_FID:
                raise ValueError(f"the UDF disc is damaged (the descriptor at offset {off} in a directory is tagged {ident})")
            chars = stream[off + 18]
            name_len = stream[off + 19]
            icb_lbn, icb_part = struct.unpack_from("<IH", stream, off + 24)
            imp_len = struct.unpack_from("<H", stream, off + 36)[0]
            name_off = off + FID_FIXED + imp_len
            end = name_off + name_len
            if end > len(stream):
                raise ValueError(f"the UDF disc is damaged (a file identifier descriptor at offset {off} runs past the directory)")
            fid_len = (FID_FIXED + imp_len + name_len + 3) & ~3   # FIDs are 4-byte aligned (ECMA-167 4/14.4)
            if off + fid_len > len(stream):
                raise ValueError(f"the UDF disc is damaged (a file identifier descriptor at offset {off} ends past the directory)")
            yield chars, icb_lbn, icb_part, stream[name_off:end]
            off += fid_len

    def _walk(self, lbn: int, part: int, seen: set[tuple[int, int]]) -> machfs.Folder:
        if (part, lbn) in seen:
            raise ValueError("a folder on it is inside itself")
        seen.add((part, lbn))
        fe = self._file_entry(lbn, part)
        if fe["file_type"] != FE_DIR:
            raise ValueError(f"the UDF disc is damaged (the directory at block {lbn} is not a directory)")
        folder = machfs.Folder()
        folder.crdate, folder.mddate, folder.bkdate = fe["crdate"], fe["mddate"], fe["bkdate"]
        stream = self._stream_bytes(fe)
        for chars, icb_lbn, icb_part, name_raw in self._fid_iter(stream):
            if chars & (FID_PARENT | FID_DELETED | FID_METADATA):
                continue                      # '.', the parent, deleted entries, metadata (e.g. a VAT file)
            if chars & FID_HIDDEN:
                continue                      # invisible files are left out, as HFS+ ones are
            try:
                name = _cs0_decode(name_raw)
            except ValueError as e:
                raise ValueError(f"the UDF disc is damaged ({e})") from None
            if not name:
                continue                      # a 254/255 compression id: an empty, unique (deleted) name
            name = unicodedata.normalize("NFC", name)
            if icb_part not in self.partitions:
                raise ValueError(f"the UDF disc is damaged (a file identifier descriptor points at partition {icb_part}, which it does not have)")
            child = self._file_entry(icb_lbn, icb_part)
            cnid = child["unique_id"] & 0xFFFFFFFF
            if child["file_type"] == FE_DIR:
                node = self._walk(icb_lbn, icb_part, seen)
            elif child["file_type"] == FE_FILE:
                node = OnDisk(self.data, {
                    "data": (child["information_length"], self._extent_spans(child)),
                    "rsrc": (0, []),
                })
                node.crdate, node.mddate, node.bkdate = child["crdate"], child["mddate"], child["bkdate"]
            else:
                continue                      # symlinks, devices, EAs: not presented; macfile reads HFS-like files only
            folder[_mac_name(name, cnid, folder, 1 << 30)] = node
        return folder


def read_udf(data) -> machfs.Volume:
    """The UDF 1.x baseline volume data holds, as machfs reads an HFS volume
    one: the root folder's contents at the volume's root, each file's data
    left on the disc until it's wanted.  Raises ValueError (plain, so
    load_volume can fall through) with a named refusal for what it will not
    read; see the module docstring."""
    reader = _Reader(data)
    pvd, lvd, pds = reader._vds_scan()
    _block_size, _revision, vol_ident, (fs_lbn, fs_part), maps = reader._lvd_parse(lvd)
    reader.partitions = reader._partitions(maps, pds)
    if fs_part not in reader.partitions:
        raise ValueError(f"the UDF disc is damaged (its file set descriptor is in partition {fs_part}, which it does not have)")
    fs_block = reader._phys_block(fs_lbn, fs_part)
    ident, _crc_len = _check_tag(fs_block, fs_lbn, "its file set descriptor")
    if ident != TAG_FSD:
        raise ValueError(f"the UDF disc is damaged (its file set descriptor at block {fs_lbn} is tagged {ident})")
    # rootDirectoryICB is a long_ad at block 400: extLength, then LBN, then partitionRef
    root_lbn, root_part = struct.unpack_from("<IH", fs_block, 404)
    root = reader._walk(root_lbn, root_part, set())
    vol = machfs.Volume()
    vol.update(root)
    pvd_name = _dstring_cs0(pvd[24:24 + 32]) if pvd is not None else ""
    vol.name = vol_ident or pvd_name or "untitled"
    # Volume dates: the MVD's (tag 1) recording time, the FSD's as fallback;
    # a zero (unrecorded) timestamp leaves the volume's dates at 0.
    vol.crdate = vol.mddate = vol.bkdate = 0
    if pvd is not None:
        vol.crdate = vol.mddate = _timestamp(pvd, 376)
    if not vol.crdate:
        vol.crdate = vol.mddate = _timestamp(fs_block, 16)
    return vol
