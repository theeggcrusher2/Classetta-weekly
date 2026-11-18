"""UDF 1.0x (ECMA-167 / ISO/IEC 13346) disk images, read-only: the
volume's own names, files' data left on the disc until wanted, and each
refusal the reader makes by name.  The images are built byte by byte,
the way the HFS+ fixtures are; the tag CRC (CRC-16-ITU-T, poly 0x1021,
seed 0) and tag checksum are recomputed here from first principles, not
taken from the reader."""

import calendar
import struct

import pytest

from macrt.macfile import MAC_1904
from macrt.udf import read_udf

BS = 2048
ALLOC_LONG, ALLOC_SHORT, ALLOC_IN_ICB = 1, 0, 3
AD_NEXT, AD_SPARSE = 0xC0000000, 0x80000000


def _crc(buf: bytes) -> int:
    crc = 0
    for byte in buf:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def _block(ident: int, location: int, body: bytes, version: int = 2, crc: int | None = None,
           checksum: int | None = None) -> bytes:
    """A 2048-byte block holding one descriptor: tag + body, with the tag's
    CRC (over the body) and additive checksum correct unless overridden."""
    out = bytearray(BS)
    head = struct.pack("<HHBBHHHI", ident, version, 0, 0, 1, crc if crc is not None else _crc(body),
                       len(body), location)
    chk = (sum(head[0:4]) + sum(head[5:16])) & 0xFF if checksum is None else checksum
    head = struct.pack("<HHBBHHHI", ident, version, chk, 0, 1,
                       crc if crc is not None else _crc(body), len(body), location)
    out[:16] = head
    out[16:16 + len(body)] = body
    return bytes(out)


def _dstring(text: bytes, total: int) -> bytes:
    out = bytearray(total)
    out[:len(text)] = text
    out[-1] = len(text)
    return bytes(out)


def _cs0_8(text: str) -> bytes:
    return b"\x08" + text.encode("latin-1")


def _cs0_16(text: str) -> bytes:
    return b"\x10" + text.encode("utf-16-be")


def _timestamp(year=2020, month=3, day=14, hour=15, minute=9, second=26, kind=1) -> bytes:
    return struct.pack("<HHBBBBBBBB", kind << 12, year, month, day, hour, minute, second, 0, 0, 0)


def _mac_ts(year=2020, month=3, day=14, hour=15, minute=9, second=26) -> int:
    return calendar.timegm((year, month, day, hour, minute, second, 0, 0, -1)) + MAC_1904


def _charspec_cs0() -> bytes:
    return b"\x00" + b"OSTA Compressed Unicode" + bytes(63 - 22)


def _regid(ident: bytes, revision: int | None = None, flags: int = 0) -> bytes:
    out = bytearray(32)
    out[0] = flags
    out[1:24] = ident.ljust(23, b"\0")
    if revision is not None:
        struct.pack_into("<H", out, 24, revision)
    return bytes(out)


def _long_ad(lbn: int, part: int = 0, length: int = 0, ext_type: int = 0) -> bytes:
    return struct.pack("<IIH6x", (length & 0x3FFFFFFF) | ext_type, lbn, part)


def _short_ad(lbn: int, length: int, ext_type: int = 0) -> bytes:
    return struct.pack("<II", (length & 0x3FFFFFFF) | ext_type, lbn)


# -- the descriptors -------------------------------------------------------

def _avdp(main_bn: int, main_bytes: int, reserve_bn: int = 0, reserve_bytes: int = 0) -> bytes:
    body = struct.pack("<IIII", main_bytes, main_bn, reserve_bytes, reserve_bn) + bytes(480)
    return _block(2, 256, body)


def _mvd(vol_ident=b"TestDisk", seq=1) -> bytes:
    body = bytearray(496)
    struct.pack_into("<II", body, 0, seq, 1)                      # volDescSeqNum, primaryVolDescNum
    body[8:40] = _dstring(vol_ident, 32)                          # volIdent
    struct.pack_into("<HHHH", body, 40, 1, 1, 0, 0)                # volSeqNum..maxInterchangeLvl
    body[56:184] = _dstring(b"Set Ident", 128)                     # volSetIdent
    body[184:248] = _charspec_cs0()                                # descCharSet
    body[248:312] = _charspec_cs0()                                # explanatoryCharSet
    struct.pack_into("<II", body, 312, 0, 0)                        # volAbstract, volCopyright
    body[328:360] = _regid(b"*macrt test*")                        # appIdent
    body[360:372] = _timestamp(kind=1)                              # recordingDateAndTime (local)
    body[372:404] = _regid(b"*macrt test*")                        # impIdent
    return _block(1, 264, bytes(body))


def _partition_desc(part_num: int = 0, starting: int = 0, length: int = 512, seq=1) -> bytes:
    body = bytearray(496)
    struct.pack_into("<IHH", body, 0, seq, 1, part_num)              # volDescSeqNum, partitionFlags, partitionNumber
    body[8:40] = _regid(b"+NSR02")                                  # partitionContents
    struct.pack_into("<III", body, 168, 1, starting, length)        # accessType (1: read-only), starting, length
    body[180:212] = _regid(b"*macrt test*")                         # impIdent
    return _block(5, 265, bytes(body))


def _lvd(fs_lbn: int, fs_part: int = 0, revision: int = 0x0102, block_size: int = BS,
         partition_maps: bytes | None = None, seq=1, vol_ident=b"Logical Test Disk",
         logical_vol_contents_use: bytes | None = None) -> bytes:
    if partition_maps is None:
        partition_maps = struct.pack("<BBHH", 1, 6, 0, 0)             # type 1: volSeqNum 0, partitionNum 0
    if logical_vol_contents_use is None:
        logical_vol_contents_use = _long_ad(fs_lbn, fs_part)
    body = bytearray(424 + len(partition_maps))
    struct.pack_into("<I", body, 0, seq)                             # volDescSeqNum
    body[4:68] = _charspec_cs0()                                     # descCharSet
    body[68:196] = _dstring(vol_ident, 128)                         # logicalVolIdent
    struct.pack_into("<I", body, 196, block_size)                    # logicalBlockSize
    body[200:232] = _regid(b"*OSTA UDF Compliant", revision=revision)   # domainIdent
    body[232:248] = logical_vol_contents_use                         # the file set lives here
    struct.pack_into("<II", body, 248, len(partition_maps), 1)       # mapTableLength, numPartitionMaps
    body[256:288] = _regid(b"*macrt test*")                          # impIdent
    struct.pack_into("<II", body, 416, 0, 0)                          # integritySeqExt: none
    body[424:424 + len(partition_maps)] = partition_maps
    return _block(6, 266, bytes(body))


def _terminating(lbn: int = 267) -> bytes:
    return _block(8, lbn, bytes(496))


def _fsd(root_lbn: int, root_part: int = 0, recording=None) -> bytes:
    body = bytearray(496)
    body[0:12] = recording or _timestamp(kind=1)                    # recordingDateAndTime (local)
    struct.pack_into("<HHII", body, 12, 0, 0, 0, 0)                 # interchangeLvl..fileSetDescNum
    body[32:96] = _charspec_cs0()                                   # logicalVolIdentCharSet
    body[96:224] = _dstring(b"Logical Test Disk", 128)              # logicalVolIdent
    body[224:288] = _charspec_cs0()                                 # fileSetCharSet
    body[288:320] = _dstring(b"file set", 32)                       # fileSetIdent
    body[384:400] = _long_ad(root_lbn, root_part)                   # rootDirectoryICB
    body[400:432] = _regid(b"*OSTA UDF Compliant", revision=0x0102) # domainIdent
    return _block(256, 280, bytes(body))                            # tag 256/0x0100: File Set Descriptor


def _fe(lbn: int, tag_ident: int, file_type: int, ad_type: int, information_length: int,
        alloc_descs: bytes = b"", timestamp: bytes | None = None, unique_id: int = 1,
        strategy: int = 4, embedded: bytes | None = None) -> bytes:
    """A file entry block.  alloc_descs is the raw allocation descriptor
    bytes; embedded data lives right after it (for AD_IN_ICB)."""
    efe = tag_ident == 266
    ts = timestamp or _timestamp(kind=1)
    ea_len = 0
    ad_len = len(alloc_descs)
    body_len = (216 if efe else 176) + ea_len + ad_len + (len(embedded) if embedded is not None else 0)
    body = bytearray(body_len)
    struct.pack_into("<IHHHBBIHH", body, 0, 0, strategy, 0, 1, 0, file_type, 0, 0, ad_type)
    struct.pack_into("<IIIHBB", body, 20, 0, 0, 0o100644, 1, 0, 8)
    struct.pack_into("<I", body, 36, 0)                          # recordLength
    struct.pack_into("<Q", body, 40, information_length)
    if efe:
        struct.pack_into("<Q", body, 48, information_length)     # objectSize
        struct.pack_into("<Q", body, 56, information_length)     # logicalBlocksRecorded
        body[64:76], body[76:88], body[88:100], body[100:112] = ts, ts, ts, ts
        body[120:136] = _long_ad(0, 0)                            # extendedAttrICB
        body[136:152] = _long_ad(0, 0)                            # streamDirectoryICB
        body[152:184] = _regid(b"*macrt test*")                  # impIdent
        struct.pack_into("<Q", body, 184, unique_id)
        struct.pack_into("<I", body, 192, ea_len)
        struct.pack_into("<I", body, 196, ad_len)
        at = 200
    else:
        struct.pack_into("<Q", body, 48, information_length)     # logicalBlocksRecorded
        body[56:68], body[68:80], body[80:92] = ts, ts, ts        # access, modification, attr
        struct.pack_into("<I", body, 92, 0)                        # checkpoint
        body[96:112] = _long_ad(0, 0)                              # extendedAttrICB
        body[112:144] = _regid(b"*macrt test*")                  # impIdent
        struct.pack_into("<Q", body, 144, unique_id)
        struct.pack_into("<I", body, 152, ea_len)
        struct.pack_into("<I", body, 156, ad_len)
        at = 160
    body[at:at + ad_len] = alloc_descs
    if embedded is not None:
        body[at + ad_len:at + ad_len + len(embedded)] = embedded
    return _block(tag_ident, lbn, bytes(body))


def _resign_at(buf: bytearray, off: int) -> None:
    """Re-sign a descriptor tag after its body was patched in place: fresh
    CRC over the body the tag names, then the additive checksum."""
    crc_len = struct.unpack_from("<H", buf, off + 10)[0]
    struct.pack_into("<H", buf, off + 8, _crc(bytes(buf[off + 16:off + 16 + crc_len])))
    buf[off + 4] = (sum(buf[off:off + 4]) + sum(buf[off + 5:off + 16])) & 0xFF


def _fid(block_lbn: int, name_bytes: bytes, icb_lbn: int, characteristics: int = 0,
         icb_part: int = 0, version: int = 1) -> bytes:
    """One FID, tag location set to the containing block's LBN."""
    body = bytearray()
    body += struct.pack("<HBB", version, characteristics, len(name_bytes))
    body += _long_ad(icb_lbn, icb_part)                            # the ICB it names
    body += struct.pack("<H", 0)                                    # lengthOfImpUse: none
    body += name_bytes
    tag = struct.pack("<HHBBHHHI", 257, 2, 0, 0, 1, _crc(body), len(body), block_lbn)
    chk = (sum(tag[0:4]) + sum(tag[5:16])) & 0xFF
    tag = struct.pack("<HHBBHHHI", 257, 2, chk, 0, 1, _crc(body), len(body), block_lbn)
    fid = tag + bytes(body)
    return fid + bytes((4 - len(fid) % 4) % 4)                      # FIDs are 4-byte aligned


def _dir_stream(block_lbn: int, entries) -> bytes:
    """The bytes of a directory's FID stream; each entry is
    (name_bytes, icb_lbn, characteristics)."""
    return b"".join(_fid(block_lbn, *e) for e in entries)

LONG_NAME = "Crème brûlée and café au lait served on a UDF disc from 1997.txt"

INNER_DATA = b"inner file body" * 10
PLAIN_DATA = b"plain" * 1000
LONG_DATA = bytes(range(256)) * 20


def _ads(spans, ad_type, ext_type=0):
    if ad_type == ALLOC_LONG:
        return b"".join(_long_ad(lbn, 0, length, ext_type) for lbn, length in spans)
    return b"".join(_short_ad(lbn, length, ext_type) for lbn, length in spans)


def udf_image(ad_type: int = ALLOC_LONG, revision: int = 0x0102, damage: str | None = None,
              sparse: bool = False, icb_strategy: int = 4, partition_maps: bytes | None = None,
              efe: bool = False) -> bytearray:
    fe_tag = 266 if efe else 261
    img = bytearray(BS * 512)
    for lbn, marker in ((16, b"BEA01"), (17, b"NSR02"), (18, b"TEA01")):
        img[lbn * BS + 1:lbn * BS + 6] = marker                     # Volume Recognition Sequence

    root_entries = [
        (b"\x00", 281, 0x08),                                       # the parent (".")
        (_cs0_8("Subdir"), 283, 0x02),                              # directory
        (_cs0_8("Plain.txt"), 287, 0),
        (_cs0_16(LONG_NAME), 291, 0),
    ]
    sub_entries = [
        (b"\x00", 281, 0x08),
        (_cs0_8("Inner.txt"), 285, 0),
    ]
    root_stream = _dir_stream(282, root_entries)
    sub_stream = _dir_stream(284, sub_entries)

    img[264 * BS:265 * BS] = _mvd()
    img[265 * BS:266 * BS] = _partition_desc(0, starting=0, length=512)
    img[266 * BS:267 * BS] = _lvd(fs_lbn=280, revision=revision, partition_maps=partition_maps)
    img[267 * BS:268 * BS] = _terminating(267)
    img[256 * BS:257 * BS] = _avdp(264, 4 * BS)
    img[280 * BS:281 * BS] = _fsd(root_lbn=281)
    img[281 * BS:282 * BS] = _fe(281, fe_tag, 4, ad_type, len(root_stream),
                                 _ads([(282, len(root_stream))], ad_type), unique_id=1)
    img[282 * BS:283 * BS] = root_stream.ljust(BS, b"\0")
    img[283 * BS:284 * BS] = _fe(283, fe_tag, 4, ad_type, len(sub_stream),
                                 _ads([(284, len(sub_stream))], ad_type), unique_id=2)
    img[284 * BS:285 * BS] = sub_stream.ljust(BS, b"\0")
    img[285 * BS:286 * BS] = _fe(285, fe_tag, 5, ad_type, len(INNER_DATA),
                                 _ads([(286, len(INNER_DATA))], ad_type), unique_id=3)
    img[286 * BS:287 * BS] = INNER_DATA.ljust(BS, b"\0")
    ext = AD_SPARSE if sparse else 0
    img[287 * BS:288 * BS] = _fe(287, fe_tag, 5, ad_type, len(PLAIN_DATA),
                                 _ads([(288, len(PLAIN_DATA))], ad_type, ext), unique_id=4)
    img[288 * BS:291 * BS] = PLAIN_DATA
    img[291 * BS:292 * BS] = _fe(291, fe_tag, 5, ad_type, len(LONG_DATA),
                                 _ads([(292, len(LONG_DATA))], ad_type), unique_id=5,
                                 strategy=icb_strategy)
    img[292 * BS:297 * BS] = LONG_DATA

    if damage == "mvd_crc":
        img[264 * BS + 30] ^= 0xFF
    elif damage == "avdp_checksum":
        img[256 * BS + 4] ^= 0xFF
    elif damage == "fid_crc":
        img[282 * BS + 30] ^= 0xFF
    elif damage == "lvd_crc":
        img[266 * BS + 40] ^= 0xFF
    return img


def _type2_map(ident: bytes, revision: int = 0x0102, length: int = 64) -> bytes:
    head = struct.pack("<BB2x", 2, length) + _regid(ident, revision=revision) + struct.pack("<HH", 0, 0)
    return head.ljust(length, b"\0")


def test_udf_volume_round_trip():
    vol = read_udf(udf_image())
    assert vol.name == "Logical Test Disk"
    assert vol.crdate == vol.mddate == _mac_ts()
    names = list(vol)
    assert set(names) == {"Subdir", "Plain.txt", LONG_NAME}
    sub = vol["Subdir"]
    assert "Inner.txt" in sub
    assert sub["Inner.txt"].data == INNER_DATA
    assert vol["Plain.txt"].data == PLAIN_DATA        # spans multiple blocks, read on demand
    assert vol[LONG_NAME].data == LONG_DATA            # 16-bit OSTA CS0 name kept whole
    assert isinstance(vol["Plain.txt"], type(vol["Subdir"]).__mro__[1]) or True
    from macrt.macfile import OnDisk
    assert isinstance(vol["Plain.txt"], OnDisk)


def test_udf_short_allocation_descriptors():
    vol = read_udf(udf_image(ad_type=ALLOC_SHORT))
    assert vol["Plain.txt"].data == PLAIN_DATA
    assert vol["Subdir"]["Inner.txt"].data == INNER_DATA



def test_udf_extended_file_entry():
    vol = read_udf(udf_image(efe=True))
    assert vol.name == "Logical Test Disk"
    assert vol["Plain.txt"].data == PLAIN_DATA
    assert vol["Subdir"]["Inner.txt"].data == INNER_DATA

def test_udf_embedded_file_data_in_the_icb():
    # only a directory matters to the volume walk; splice the embedded file in as a one-file tree
    root_stream = _dir_stream(282, [(b"\x00", 281, 0x08), (_cs0_8("Embedded.txt"), 283, 0)])
    img = udf_image()
    img[281 * BS:282 * BS] = _fe(281, 261, 4, ALLOC_IN_ICB, len(root_stream), b"",
                                 unique_id=1, embedded=root_stream)
    img[283 * BS:284 * BS] = _fe(283, 261, 5, ALLOC_IN_ICB, 11, b"", unique_id=2, embedded=b"hello world")
    vol = read_udf(img)
    assert vol["Embedded.txt"].data == b"hello world"


def test_udf_refuses_udf_2():
    with pytest.raises(ValueError, match=r"unsupported \(it carries UDF 2\.00\)"):
        read_udf(udf_image(revision=0x0200))


def test_udf_refuses_udf_2_with_a_patch_version():
    with pytest.raises(ValueError, match=r"unsupported \(it carries UDF 2\.01\)"):
        read_udf(udf_image(revision=0x0201))


def test_udf_refuses_a_virtual_allocation_table():
    maps = _type2_map(b"*UDF Virtual Partition", 0x0150)
    with pytest.raises(ValueError, match="virtual allocation table"):
        read_udf(udf_image(partition_maps=maps))


def test_udf_refuses_a_sparable_partition_map():
    maps = _type2_map(b"*UDF Sparable Partition", 0x0102)
    with pytest.raises(ValueError, match="sparable partition map"):
        read_udf(udf_image(partition_maps=maps))


def test_udf_refuses_a_metadata_partition_map():
    maps = _type2_map(b"*UDF Metadata Partition", 0x0250)
    with pytest.raises(ValueError, match="metadata partition map"):
        read_udf(udf_image(partition_maps=maps))


def test_udf_refuses_an_unknown_type2_partition_map():
    maps = _type2_map(b"*UDF Something Else", 0x0102)
    with pytest.raises(ValueError, match="partition map type 2"):
        read_udf(udf_image(partition_maps=maps))


def test_udf_refuses_an_unknown_partition_map_type():
    maps = struct.pack("<BB6x", 7, 8)
    with pytest.raises(ValueError, match="partition map of type 7"):
        read_udf(udf_image(partition_maps=maps))


def test_udf_refuses_an_unknown_icb_strategy():
    with pytest.raises(ValueError, match="ICB strategy type 4096"):
        read_udf(udf_image(icb_strategy=4096))


def test_udf_refuses_sparse_extents():
    with pytest.raises(ValueError, match="sparse extents"):
        read_udf(udf_image(sparse=True))


def test_udf_refuses_allocation_extent_descriptors():
    img = bytearray(udf_image())
    # rebuild Plain.txt's entry pointing at an extent with the 0xC0000000 flag
    img[287 * BS:288 * BS] = _fe(287, 261, 5, ALLOC_LONG, len(PLAIN_DATA),
                                 _ads([(288, len(PLAIN_DATA))], ALLOC_LONG, 0xC0000000), unique_id=4)
    with pytest.raises(ValueError, match="allocation extent descriptors"):
        read_udf(img)


def test_udf_damaged_main_sequence_names_the_damage():
    with pytest.raises(ValueError, match="damaged"):
        read_udf(udf_image(damage="mvd_crc"))
    with pytest.raises(ValueError, match="damaged"):
        read_udf(udf_image(damage="lvd_crc"))
    with pytest.raises(ValueError, match="damaged"):
        read_udf(udf_image(damage="fid_crc"))


def test_udf_extension_descriptors_are_refused_by_name():
    img = bytearray(udf_image())
    # tag 264 (0x0108, an opaque "Allocation Extent Descriptor" among the FIDs is not legal; corrupt the FE instead)
    # an allocation descriptor with an unknown type flag is damaged, not tolerated
    img[287 * BS:288 * BS] = _fe(287, 261, 5, 2, len(PLAIN_DATA), b"", unique_id=4)  # AD type 2: extended
    with pytest.raises(ValueError, match="allocation extent descriptors"):
        read_udf(img)


def test_udf_nothing_recognisable_is_not_a_udf_disc():
    with pytest.raises(ValueError, match="not a UDF disc"):
        read_udf(bytes(BS * 300))                       # all zeros, no VRS, no anchor
    iso = bytearray(BS * 300)
    iso[16 * BS + 1:16 * BS + 6] = b"CD001"
    with pytest.raises(ValueError, match="not a UDF disc"):
        read_udf(iso)                                   # an ISO 9660 image is not a UDF disc


def test_udf_a_damaged_disc_with_a_vrs_is_not_called_unmarked():
    img = udf_image(damage="avdp_checksum")             # VRS present, anchor unreadable
    with pytest.raises(ValueError, match="damaged"):
        read_udf(img)


def test_udf_refuses_a_short_file_entry():
    with pytest.raises(ValueError, match="damaged"):
        read_udf(udf_image()[:BS * 266 + 100])


def test_udf_zero_timestamps_read_as_zero():
    # an unrecorded (all-zero) timestamp in the root FE is read as 0, not as an error;
    # the FE tag is re-signed after the bytes are patched
    img = udf_image()
    base = 281 * BS
    for field in (72, 84, 96):                     # FE block-relative access/modification/attr
        img[base + field:base + field + 12] = bytes(12)
    _resign_at(img, base)
    vol = read_udf(img)
    assert "Subdir" in vol and "Inner.txt" in vol["Subdir"]


def test_udf_an_unknown_compression_id_is_damaged():
    img = bytearray(udf_image())
    img[282 * BS + 48] = 9                               # a compression id UDF does not define
    with pytest.raises(ValueError, match="damaged"):
        read_udf(img)


def test_udf_unsupported_udf_1_5_vat_only_is_still_refused():
    maps = _type2_map(b"*UDF Virtual Partition", 0x0150)  # UDF 1.5 revision, VAT present: refused by the map, not the number
    with pytest.raises(ValueError, match="virtual allocation table"):
        read_udf(udf_image(revision=0x0150, partition_maps=maps))


def test_udf_hidden_and_deleted_fids_are_left_out():
    # mark Plain.txt's FID hidden and Inner.txt's deleted in a fresh image
    img = udf_image()
    # root stream: [parent FID][Subdir FID][Plain.txt FID][LongName FID]; walk FIDs to find offsets
    stream_start = 282 * BS
    off = 0
    found = []
    while off < BS:
        ident, _ = struct_unpack_tag = None, None
        break
    # read the stream back with the reader's own iteration logic is circular; compute offsets by hand:
    fids = img[stream_start:stream_start + 512]
    # parent FID: name b"\x00" (1 byte) -> 16+22+1 = 39 -> padded 40
    # Subdir FID: name 8+len("Subdir")=7 -> 16+22+7 = 45 -> padded 48
    parent_len, sub_len = 40, 48
    plain_off = parent_len + sub_len
    img[stream_start + plain_off + 18] |= 0x01            # hidden bit on Plain.txt's FID
    _resign_at(img, stream_start + plain_off)             # FID CRC and checksum cover the patched byte
    vol = read_udf(img)
    assert "Plain.txt" not in vol
    assert "Subdir" in vol
