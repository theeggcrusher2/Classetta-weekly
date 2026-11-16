"""Tests for NDIF and UDIF disk images: detection, browsing, failure paths.

Fixtures are written here with small spec-faithful writers that follow
libmodi's analysis of the Apple disk image types, Jonathan Levin's
"Demystifying the DMG File Format" and ndif2raw.
"""

import binascii
import machfs
import plistlib
import pytest
import struct
import zlib

from macrt import dmg
from macrt.dmg import DmgError
from macrt.macfile import image_kind, is_disk_image, load_volume
from macrt.resfork import Resource, build_fork

SECTOR = 512
KOLY_SIZE = 512

ZERO_FILL = 0x00000000
UNCOMPRESSED = 0x00000001
IGNORED = 0x00000002
COMMENT = 0x7FFFFFFE
CHUNK_ADC = 0x80000004
CHUNK_ZLIB = 0x80000005
CHUNK_BZIP2 = 0x80000006
CHUNK_LZFSE = 0x80000007
CHUNK_LZMA = 0x80000008
TERMINATOR = 0xFFFFFFFF

NDIF_ZERO = 0
NDIF_RAW = 2
NDIF_KEN_CODE = 128
NDIF_ADC = 131
NDIF_TERMINATOR = 255


def _volume_raw(name="Apple Disk") -> bytes:
    v = machfs.Volume()
    v.name = name
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", bytearray(b"ndif payload " * 100)
    v["readme.txt"] = f
    return v.write(size=1024 * 1024)


def _mish(runs, count, *, start=0, data_offset=0, version=1):
    table = bytearray(204)
    struct.pack_into(">4sI", table, 0, b"mish", version)
    struct.pack_into(">QQQ", table, 8, start, count, data_offset)
    struct.pack_into(">II", table, 32, 1, 1)               # buffers, descriptors
    struct.pack_into(">I", table, 200, len(runs))
    for r in runs:
        table += struct.pack(">IIQQQQ", r["type"], 0, r["start"], r["count"],
                             r["offset"], r["size"])
    return bytes(table)


def _udif(raw, chunks=None, *, early=False, segment_count=1, variant=1,
          extra_tables=0, corrupt_xml=False):
    nsec = len(raw) // SECTOR
    if chunks is None:
        chunks = [{"type": UNCOMPRESSED, "start": 0, "count": nsec,
                   "data": raw}]
    payload = bytearray()
    runs = []
    for ch in chunks:
        blob = ch.get("data", b"")
        offset = len(payload)
        payload += blob
        runs.append({"type": ch["type"], "start": ch["start"], "count": ch["count"],
                     "offset": offset, "size": ch.get("size", len(blob))})
    table = _mish(runs, nsec)
    tables = [{"Attributes": "0x01234567", "CFName": "Whole Disk",
               "ID": "-1", "Name": "whole disk (Apple_HFS : 2)", "Data": table}]
    tables += [dict(tables[0]) for _ in range(extra_tables)]
    xml = plistlib.dumps({"resource-fork": {"blkx": tables}})
    if corrupt_xml:
        xml = b"this is not a property list"
    data_fork = bytes(payload)
    if early:
        trailer_at, xml_offset = KOLY_SIZE, KOLY_SIZE + len(data_fork)
    else:
        trailer_at, xml_offset = len(data_fork) + len(xml), len(data_fork)
    trailer = bytearray(KOLY_SIZE)
    trailer[0:4] = b"koly"
    struct.pack_into(">III", trailer, 4, 4, KOLY_SIZE, 0)       # version, size, flags
    if early:
        struct.pack_into(">QQQQQ", trailer, 16, 0,
                         KOLY_SIZE, len(data_fork), 0, 0)       # rdfo, dfo, dfl, rfo, rrl
    else:
        struct.pack_into(">QQQQQ", trailer, 16, 0,
                         0, len(data_fork), 0, 0)
    struct.pack_into(">II16s", trailer, 56, 1, segment_count, bytes(16))
    struct.pack_into(">QQ", trailer, 216, xml_offset, len(xml))
    struct.pack_into(">I", trailer, 488, variant)
    struct.pack_into(">Q", trailer, 492, nsec)
    if early:
        return bytes(trailer) + data_fork + xml
    return data_fork + xml + bytes(trailer)


def _bcem(disk_blocks, chunks, *, version=12, name=b"Disk", fsid=1,
          largest=None, backing_offset=0, crc32=0):
    body = bytearray(128 + 12 * len(chunks))
    struct.pack_into(">HHB", body, 0, version, fsid, len(name))
    body[5:68] = name.ljust(63, b"\0")
    struct.pack_into(">IIIII", body, 68, disk_blocks,
                     largest if largest is not None else disk_blocks,
                     backing_offset, crc32, 0)
    struct.pack_into(">I", body, 124, len(chunks))
    at = 128
    for logical, ctype, boffset, bsize in chunks:
        body[at], body[at + 1], body[at + 2] = (logical >> 16) & 0xFF, \
                                               (logical >> 8) & 0xFF, logical & 0xFF
        body[at + 3] = ctype
        struct.pack_into(">II", body, at + 4, boffset, bsize)
        at += 12
    return bytes(body)


def _ndif(raw, chunks, *, version=12):
    """The data fork and bcem resource bytes a NDIF image carries."""
    data_fork = b"".join(ch[4] if len(ch) > 4 else b"" for ch in chunks)
    bc = _bcem(len(raw) // SECTOR, [tuple(c[:4]) for c in chunks], version=version)
    return data_fork, bc


def _fork(bcem):
    return build_fork([Resource(b"bcem", 128, bcem)])


def _fork_no_bcem():
    return build_fork([Resource(b"STR ", 128, b"\x02hi")])


def _macfile(data, rsrc=b"", name="disk.dmg"):
    from macrt.macfile import MacFile
    return MacFile(name, data, rsrc, b"????", b"????", 0, "k", None)


def _macbinary(name, data, rsrc, ftype=b"rohd", creator=b"ddsk"):
    h = bytearray(128)
    h[1] = len(name)
    h[2:2 + len(name)] = name.encode("mac_roman")
    h[65:69], h[69:73] = ftype, creator
    struct.pack_into(">II", h, 83, len(data), len(rsrc))
    h[101], h[122], h[123] = 0x21, 129, 129
    struct.pack_into(">H", h, 124, binascii.crc_hqx(bytes(h[:124]), 0))
    pad = lambda b: b + bytes(-len(b) % 128)
    return bytes(h) + pad(data) + pad(rsrc)


def _adc_literals(raw):
    out = bytearray()
    for at in range(0, len(raw), 128):
        blob = raw[at:at + 128]
        out.append(0x80 | (len(blob) - 1))
        out += blob
    return bytes(out)


# --- detection ----------------------------------------------------------


def test_udif_is_detected_by_its_trailer_or_head():
    raw = _volume_raw()
    img = _udif(raw)
    assert dmg.is_udif(img) and is_disk_image(img)
    early = _udif(raw, early=True)
    assert dmg.is_udif(early) and is_disk_image(early)
    assert not dmg.is_udif(b"not a disk image at all")
    assert not dmg.is_ndif_fork(b"")


def test_ndif_is_detected_by_its_bcem_resource():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    assert dmg.is_ndif_fork(_fork(bcem))
    assert not dmg.is_ndif_fork(_fork_no_bcem())
    assert not dmg.is_ndif_fork(b"not a resource fork at all")


def test_a_udif_image_is_named_by_kind():
    raw = _volume_raw()
    assert image_kind(_macfile(_udif(raw))) == "UDIF disk image"
    assert image_kind(_macfile(_udif(raw, early=True))) == "UDIF disk image"


def test_an_ndif_image_is_named_by_kind():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    f = _macfile(data_fork, _fork(bcem), name="Disk.img")
    assert image_kind(f) == "NDIF disk image"


# --- browsing -----------------------------------------------------------


def test_an_uncompressed_udif_opens_as_a_volume():
    raw = _volume_raw()
    vol = load_volume(_udif(raw))
    assert vol.name == "Apple Disk"
    assert vol["readme.txt"].data == b"ndif payload " * 100


def test_an_early_udif_with_the_trailer_first_opens():
    raw = _volume_raw()
    vol = load_volume(_udif(raw, early=True))
    assert vol.name == "Apple Disk"


def test_a_zlib_udif_opens_as_a_volume():
    raw = _volume_raw()
    img = _udif(raw, [{"type": CHUNK_ZLIB, "start": 0, "count": len(raw) // SECTOR,
                       "data": zlib.compress(raw)}])
    vol = load_volume(img)
    assert vol.name == "Apple Disk"
    assert vol["readme.txt"].data == b"ndif payload " * 100


def test_an_adc_udif_of_literals_opens_as_a_volume():
    raw = _volume_raw()
    img = _udif(raw, [{"type": CHUNK_ADC, "start": 0, "count": len(raw) // SECTOR,
                       "data": _adc_literals(raw)}])
    vol = load_volume(img)
    assert vol.name == "Apple Disk"
    assert vol["readme.txt"].data == b"ndif payload " * 100


def test_unallocated_udif_sectors_read_zeros():
    raw = _volume_raw()
    half = len(raw) // 2
    img = _udif(raw, [{"type": UNCOMPRESSED, "start": half // SECTOR,
                       "count": len(raw) // SECTOR - half // SECTOR, "data": raw[half:]}])
    out = dmg.read_udif(img)
    assert out[:half] == b"\0" * half
    assert out[half:] == raw[half:]


def test_a_udif_zero_fill_chunk_reads_zeros():
    raw = _volume_raw()
    img = _udif(raw, [{"type": ZERO_FILL, "start": 0, "count": len(raw) // SECTOR}])
    assert dmg.read_udif(img) == b"\0" * len(raw)


def test_a_udif_comment_and_ignored_chunk_read_zeros():
    raw = _volume_raw()
    img = _udif(raw, [{"type": COMMENT, "start": 0, "count": 1},
                      {"type": IGNORED, "start": 1, "count": len(raw) // SECTOR - 1}])
    assert dmg.read_udif(img) == b"\0" * len(raw)


def test_a_macbinary_wrapped_ndif_opens_as_a_volume(tmp_path):
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    (tmp_path / "Disk.img.bin").write_bytes(_macbinary("Disk.img", data_fork, _fork(bcem)))
    vol = load_volume(tmp_path / "Disk.img.bin")
    assert vol.name == "Apple Disk"
    assert vol["readme.txt"].data == b"ndif payload " * 100


def test_an_ndif_opens_from_its_forks():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    out = dmg.read_ndif(data_fork, _fork(bcem))
    assert out == raw


def test_an_ndif_zero_chunk_and_raw_chunk():
    raw = _volume_raw()
    half = len(raw) // 2
    half_blocks = half // SECTOR
    data_fork, bcem = _ndif(raw, [(0, NDIF_ZERO, 0, 0),
                                  (half_blocks, NDIF_RAW, 0, len(raw) - half, raw[half:])])
    out = dmg.read_ndif(data_fork, _fork(bcem))
    assert out[:half] == b"\0" * half
    assert out[half:] == raw[half:]


def test_an_adc_ndif_opens():
    raw = _volume_raw()
    blob = _adc_literals(raw)
    data_fork, bcem = _ndif(raw, [(0, NDIF_ADC, 0, len(blob), blob)])
    out = dmg.read_ndif(data_fork, _fork(bcem))
    assert out == raw


def test_a_kencode_ndif_is_refused_by_name():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_KEN_CODE, 0, 4, b"abcd")])
    with pytest.raises(DmgError, match="KenCode"):
        dmg.read_ndif(data_fork, _fork(bcem))


def test_an_ndif_terminator_chunk_ends_the_table():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw),
                                  (len(raw) // SECTOR, NDIF_TERMINATOR, 0, 0)])
    out = dmg.read_ndif(data_fork, _fork(bcem))
    assert out == raw


def test_adc_literal_command():
    stream = bytes([0x80 | 4]) + b"abcde"
    assert dmg._adc(stream, 5) == b"abcde"


def test_adc_16_bit_copy_command():
    stream = bytes([0x80 | 9]) + b"A" * 10 + bytes([0x40, 0x00, 0x00])
    assert dmg._adc(stream, 14) == b"A" * 14


def test_adc_10_bit_copy_command():
    stream = bytes([0x80 | 9]) + b"A" * 10 + bytes([0x00, 0x00])
    assert dmg._adc(stream, 13) == b"A" * 13


def test_adc_copy_overlapping_repeats():
    stream = bytes([0x80, ord("A"), 0x00, 0x00])   # 'A' then copy 3, distance 1
    assert dmg._adc(stream, 4) == b"AAAA"


def test_adc_copy_before_the_start_is_damaged():
    stream = bytes([0x40, 0x00, 0x05])                   # distance 6, nothing out yet
    with pytest.raises(DmgError, match="reaches before its chunk"):
        dmg._adc(stream, 4)


def test_adc_literal_past_the_end_is_damaged():
    stream = bytes([0x80 | 9, ord("A")])                 # claims 10, holds 1
    with pytest.raises(DmgError, match="runs off the end"):
        dmg._adc(stream, 10)


def test_adc_chunk_expansion_short_is_damaged():
    stream = bytes([0x80 | 4]) + b"abcde"                # 5 bytes, chunk wants 100
    with pytest.raises(DmgError, match="expanded to 5 of 100"):
        dmg._adc(stream, 100)


def test_a_udif_with_no_koly_is_not_one():
    with pytest.raises(DmgError, match="no koly trailer"):
        dmg.read_udif(b"not a udif image")


def test_a_udif_with_a_bad_version_is_refused():
    raw = _volume_raw()
    img = bytearray(_udif(raw))
    at = len(img) - KOLY_SIZE
    struct.pack_into(">I", img, at + 4, 5)                  # version 5
    with pytest.raises(DmgError, match="its version is 5"):
        dmg.read_udif(bytes(img))


def test_a_segmented_udif_is_refused():
    raw = _volume_raw()
    img = _udif(raw, segment_count=2)
    with pytest.raises(DmgError, match="split across files"):
        dmg.read_udif(img)


def test_a_udif_with_two_block_tables_is_refused():
    raw = _volume_raw()
    img = _udif(raw, extra_tables=1)
    with pytest.raises(DmgError, match="2 block tables"):
        dmg.read_udif(img)


def test_a_udif_without_a_blkx_table_is_damaged():
    raw = _volume_raw()
    img = _udif(raw, corrupt_xml=True)
    with pytest.raises(DmgError, match="property list won't parse"):
        dmg.read_udif(img)


def test_a_udif_with_the_xml_outside_the_file_is_damaged():
    raw = _volume_raw()
    img = bytearray(_udif(raw))
    at = len(img) - KOLY_SIZE
    struct.pack_into(">QQ", img, at + 216, len(img) * 2, 100)   # xml_offset, xml_length
    with pytest.raises(DmgError, match="runs off its end"):
        dmg.read_udif(bytes(img))


def test_a_udif_with_bzip2_is_refused_by_name(tmp_path):
    raw = _volume_raw()
    img = _udif(raw, [{"type": CHUNK_BZIP2, "start": 0, "count": len(raw) // SECTOR,
                       "data": b"BZh9fakebzip2"}])
    with pytest.raises(DmgError, match="bzip2"):
        load_volume(img)


def test_a_udif_with_lzfse_is_refused_by_name():
    raw = _volume_raw()
    img = _udif(raw, [{"type": CHUNK_LZFSE, "start": 0, "count": len(raw) // SECTOR,
                       "data": b"bvx2fake"}])
    with pytest.raises(DmgError, match="LZFSE"):
        load_volume(img)


def test_a_udif_with_lzma_is_refused_by_name():
    raw = _volume_raw()
    img = _udif(raw, [{"type": CHUNK_LZMA, "start": 0, "count": len(raw) // SECTOR,
                       "data": b"\xfd7zXZfake"}])
    with pytest.raises(DmgError, match="lzma"):
        load_volume(img)


def test_a_udif_with_an_unknown_chunk_type_is_refused():
    raw = _volume_raw()
    img = _udif(raw, [{"type": 0x80000009, "start": 0, "count": len(raw) // SECTOR,
                       "data": b"whatever"}])
    with pytest.raises(DmgError, match="0x80000009"):
        load_volume(img)


def test_a_udif_with_a_chunk_past_the_end_is_damaged():
    raw = _volume_raw()
    img = _udif(raw, [{"type": UNCOMPRESSED, "start": 0, "count": len(raw) // SECTOR,
                       "data": raw[:100], "size": len(raw) + 500}])
    with pytest.raises(DmgError, match="runs past its end"):
        dmg.read_udif(img)


def test_a_udif_with_a_short_uncompressed_chunk_is_damaged():
    raw = _volume_raw()
    img = _udif(raw, [{"type": UNCOMPRESSED, "start": 0, "count": len(raw) // SECTOR,
                       "data": raw[:100], "size": 100}])
    with pytest.raises(DmgError, match="too small"):
        dmg.read_udif(img)


def test_a_udif_with_a_zlib_chunk_that_does_not_decompress_is_damaged():
    raw = _volume_raw()
    img = _udif(raw, [{"type": CHUNK_ZLIB, "start": 0, "count": len(raw) // SECTOR,
                       "data": b"not a zlib stream"}])
    with pytest.raises(DmgError, match="won't decompress"):
        dmg.read_udif(img)


def test_a_udif_with_a_chunk_past_the_disk_is_damaged():
    raw = _volume_raw()
    nsec = len(raw) // SECTOR
    img = _udif(raw, [{"type": ZERO_FILL, "start": nsec - 1, "count": 2000}])
    with pytest.raises(DmgError, match="runs past its disk"):
        dmg.read_udif(img)


def test_an_ndif_with_a_missing_resource_fork_is_damaged():
    raw = _volume_raw()
    data_fork, _ = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    with pytest.raises(DmgError, match="resource fork is missing"):
        dmg.read_ndif(data_fork, b"")


def test_an_ndif_with_a_fork_but_no_bcem_is_refused():
    raw = _volume_raw()
    data_fork, _ = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    with pytest.raises(DmgError, match="no bcem resource"):
        dmg.read_ndif(data_fork, _fork_no_bcem())


def test_an_ndif_with_a_damaged_fork_is_refused():
    raw = _volume_raw()
    data_fork, _ = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)])
    with pytest.raises(DmgError, match="resource fork is damaged"):
        dmg.read_ndif(data_fork, b"not a fork at all" + b"\0" * 300)


def test_an_ndif_with_an_unknown_version_is_refused():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, NDIF_RAW, 0, len(raw), raw)], version=7)
    with pytest.raises(DmgError, match="its version is 7"):
        dmg.read_ndif(data_fork, _fork(bcem))


def test_an_ndif_with_a_cut_short_chunk_table_is_damaged():
    raw = _volume_raw()
    body = _bcem(len(raw) // SECTOR, [(0, NDIF_RAW, 0, len(raw))])
    body = body[:128 + 4]                                       # one full chunk, minus 8 bytes
    with pytest.raises(DmgError, match="chunk table is cut short"):
        dmg.read_ndif(raw, _fork(body))


def test_an_ndif_with_an_unknown_chunk_type_is_refused_by_number():
    raw = _volume_raw()
    data_fork, bcem = _ndif(raw, [(0, 42, 0, 4, b"abcd")])
    with pytest.raises(DmgError, match="chunk type 42"):
        dmg.read_ndif(data_fork, _fork(bcem))


# __more__
