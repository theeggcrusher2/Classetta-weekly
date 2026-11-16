"""Tests for DART and DiskMaker floppy images: a chunk-length table over
40-sector chunks, 'fast' RLE or stored, the tag bytes stripped on the
way out.  The writers here follow the format notes at
ciderpress2.com/formatdoc/DART-notes.html: big-endian, a 4-byte header,
lengths in words for 'fast', 20960 or 0xffff for stored, zero past the
disk's end.
"""

import machfs
import pytest
import struct

from macrt import dart
from macrt.dart import DartError
from macrt.macfile import MacFile, image_kind, is_disk_image, load_volume

SECTOR = 512
DATA = 40 * SECTOR                 # the 512-byte blocks a chunk holds
CHUNK = DATA + 40 * 12             # plus their 12-byte tags


def _volume_raw(size=819200) -> bytes:
    v = machfs.Volume()
    v.name = "Apple Disk"
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", bytearray(b"dart payload " * 100)
    v["readme.txt"] = f
    return v.write(size=size)


def _fast_encode(chunk: bytes) -> bytes:
    """A 'fast' RLE encoding: runs of three or more like words repeat,
    the rest copy, every count a signed 16-bit word count."""
    out = bytearray()
    at, n = 0, len(chunk)
    while at < n:
        word = chunk[at:at + 2]
        rep = 1
        while at + rep * 2 + 2 <= n and chunk[at + rep * 2:at + rep * 2 + 2] == word:
            rep += 1
        if rep >= 3:
            take = min(rep, 0x7FFF)
            out += struct.pack(">h", -take) + word
            at += take * 2
            continue
        start = at
        at += 2
        while at < n:
            rep = 1
            while at + rep * 2 + 2 <= n and chunk[at + rep * 2:at + rep * 2 + 2] == chunk[at:at + 2]:
                rep += 1
            if rep >= 3:
                break
            at += 2
        pos, words = start, (at - start) // 2
        while words:
            take = min(words, 0x7FFF)
            out += struct.pack(">h", take) + chunk[pos:pos + take * 2]
            pos += take * 2
            words -= take
    return bytes(out)


def _dart(raw: bytes, *, cmp_=0, typ=1, stored_at=(), tags=b"") -> bytes:
    """DART-wrap a 512-sector stream, zero-padded to whole chunks."""
    assert len(raw) % DATA == 0
    size = len(raw) // 1024
    lens, chunks = [], []
    for i in range(0, len(raw), DATA):
        body = raw[i:i + DATA] + (tags if tags else b"\0" * (CHUNK - DATA))
        if i // DATA in stored_at:
            chunks.append(body)
            lens.append(CHUNK if cmp_ == 2 else 0xffff)
        elif cmp_ == 0:
            enc = _fast_encode(body)
            chunks.append(enc)
            lens.append(len(enc) // 2)
        else:
            chunks.append(body)
            lens.append(CHUNK)
    n = 72 if size >= 1400 else 40
    table = list(lens) + [0] * (n - len(lens))
    head = bytes([cmp_, typ]) + struct.pack(">H", size) + struct.pack(f">{n}H", *table)
    return head + b"".join(chunks)


def _hand_dart(lens, chunks, cmp_=0, typ=1, size=400) -> bytes:
    table = list(lens) + [0] * (40 - len(lens))
    head = bytes([cmp_, typ]) + struct.pack(">H", size) + struct.pack(">40H", *table)
    return head + b"".join(chunks)


# --- telling -----------------------------------------------------------

def test_a_fast_image_and_a_stored_one_are_both_dart():
    raw = _volume_raw()
    assert dart.is_dart(_dart(raw))
    assert dart.is_dart(_dart(raw, cmp_=2))
    assert not dart.is_dart(b"")
    assert not dart.is_dart(b"not a disk image at all, just text")
    assert not dart.is_dart(_volume_raw()[:64])
    assert not dart.is_dart(_dart(raw)[:84])            # the table cut short
    assert not dart.is_dart(b"\x00\x01\x01\x90" + b"\0" * 76)   # header only


def test_the_kind_column_says_dart():
    d = _dart(_volume_raw(409600))
    assert is_disk_image(d)
    f = MacFile("Guided Tour Mac 128", d, b"", b"DMdf", b"DskM", 0, "Guided Tour Mac 128")
    assert image_kind(f) == "DART disk image"
    assert is_disk_image(f.head(524296))


# --- reading -----------------------------------------------------------

@pytest.mark.parametrize("size,typ", [(409600, 1), (819200, 1), (1474560, 16)])
def test_a_image_round_trips_through_the_tags(size, typ):
    raw = _volume_raw(size)
    for cmp_ in (0, 2):
        out = dart.read_image(_dart(raw, cmp_=cmp_, typ=typ, tags=b"\xA5" * 480))
        assert out == raw                      # the tags are gone, the sectors stay


def test_a_fast_image_with_stored_chunks_round_trips():
    raw = _volume_raw()
    out = dart.read_image(_dart(raw, stored_at=(1, 3, 39)))
    assert out == raw


def test_a_dart_image_mounts_its_volume():
    vol = load_volume(_dart(_volume_raw()))
    assert vol.name == "Apple Disk"
    assert sorted(n for n, _ in vol.items()) == ["readme.txt"]


# --- refusals, by name --------------------------------------------------

@pytest.mark.parametrize("typ,word", [(2, "a Lisa floppy"), (3, "an Apple II floppy"),
                                      (17, "an MS-DOS 720K floppy"), (18, "an MS-DOS 1400K floppy")])
def test_a_non_mac_disk_is_refused_by_name(typ, word):
    with pytest.raises(DartError, match=f"it is {word}, not a Mac floppy"):
        dart.read_image(_dart(_volume_raw(), typ=typ))


def test_best_compression_is_refused_by_name():
    with pytest.raises(DartError, match="'best' LZHUF compression isn't read"):
        dart.read_image(_dart(_volume_raw(), cmp_=1))


# --- damage -------------------------------------------------------------

def test_a_chunk_cut_short_says_so():
    img = _dart(_volume_raw())
    with pytest.raises(DartError, match=r"its chunk \d+ runs past its end"):
        dart.read_image(img[:-100])


@pytest.mark.parametrize("chunk,lens,match", [
    (b"\x00\x01\xAA\xBB", [2], "a chunk's RLE runs short"),
    (b"\x00\x05\xAA\x00", [2], "a chunk's RLE copy runs past its end"),
    (b"\xff\xfe", [1], "a chunk's RLE pattern is missing"),
    (b"\x00\x00\xAA\xBB", [2], "a chunk's RLE run count is zero"),
])
def test_rle_damage_says_what_is_wrong(chunk, lens, match):
    with pytest.raises(DartError, match=match):
        dart.read_image(_hand_dart(lens, [chunk]))


def test_a_run_that_overflows_the_chunk_says_so():
    enc = struct.pack(">h", -10481) + b"\0\0"
    with pytest.raises(DartError, match="overflows to 20962 bytes"):
        dart.read_image(_hand_dart([len(enc) // 2], [enc]))


def test_a_stored_chunk_of_the_wrong_length_says_so():
    with pytest.raises(DartError, match="its stored chunk 0 is 100 bytes, not 20960"):
        dart.read_image(_hand_dart([100], [b"\0" * 100], cmp_=2))


def test_chunks_shorter_than_the_header_says_so():
    d = bytearray(_dart(_volume_raw(409600), cmp_=2))
    struct.pack_into(">H", d, 2, 800)          # the header promises 800K, holds 400K
    with pytest.raises(DartError, match="its chunks hold 409600 bytes, not its 819200"):
        dart.read_image(bytes(d))
