"""Tests for qcow2 disk images: detection, browsing, failure paths.

The fixtures are written here by a small qcow2 writer that follows the
same public description the reader does (QEMU's docs/interop/qcow2.txt),
so a round-trip proves the reader against the format, not against
itself: the deflate streams come from the standard library, and the
header, tables and cluster descriptors are laid out by hand.
"""

import machfs
import pytest
import struct
import zlib

from macrt import qcow2
from macrt.macfile import image_kind, is_disk_image, load_volume
from macrt.qcow2 import Qcow2Error, is_qcow2

CLUSTER_BITS = 12
CLUSTER = 1 << CLUSTER_BITS
L2_ENTRIES = CLUSTER // 8
X = 62 - (CLUSTER_BITS - 8)                 # compressed offset's bit width


# --- the writer --------------------------------------------------------


def _deflate(raw: bytes) -> bytes:
    co = zlib.compressobj(9, zlib.DEFLATED, -15)     # raw, no zlib header
    return co.compress(raw) + co.flush()


def _qcow2_file(hunks: list[bytes], version: int = 3, compressed=(), zero=(),
                header_length: int = 104, compression_type: int = 0) -> bytes:
    """A qcow2 of one hunk per guest cluster.  `compressed` and `zero`
    hold the cluster numbers to pack that way; the rest are stored raw."""
    size = CLUSTER * len(hunks)
    l1_size = -(-size // (CLUSTER * L2_ENTRIES))
    l1_off, l2_off = 3 * CLUSTER, 4 * CLUSTER
    data_off = 5 * CLUSTER

    if version == 3:
        hdr = bytearray(header_length)
    else:
        hdr = bytearray(72)
    hdr[0:4] = b"QFI\xfb"
    struct.pack_into(">II", hdr, 4, version, 0)
    struct.pack_into(">QI", hdr, 8, 0, 0)
    struct.pack_into(">IQ", hdr, 20, CLUSTER_BITS, size)
    struct.pack_into(">IIQ", hdr, 32, 0, l1_size, l1_off)
    struct.pack_into(">QIQ", hdr, 48, CLUSTER, 1, 0)
    struct.pack_into(">IQ", hdr, 60, 0, 0)
    if version == 3:
        struct.pack_into(">QQQ", hdr, 72, 0, 0, 0)
        struct.pack_into(">II", hdr, 96, 4, header_length)
        if header_length > 104:
            hdr[104] = compression_type

    l1 = b"".join(struct.pack(">Q", l2_off | (1 << 63)) for _ in range(l1_size))
    entries = []
    blocks = []
    for i, hunk in enumerate(hunks):
        if i in zero:
            entries.append(1)                                   # the zero flag
            blocks.append(b"\0" * CLUSTER)                     # its host cluster stays free
        elif i in compressed:
            blob = _deflate(hunk)
            sectors = -(-len(blob) // 512)
            entries.append((data_off + i * CLUSTER) | (sectors - 1) << X | 1 << 62)
            blocks.append(blob)
        else:
            entries.append(data_off + i * CLUSTER | 1 << 63)
            blocks.append(hunk)
    l2 = b"".join(struct.pack(">Q", e) for e in entries)

    used = list(range(5)) + [5 + i for i in range(len(hunks))]  # header..l2, data
    refcounts = bytearray(CLUSTER)
    for c in used:
        refcounts[c * 2:c * 2 + 2] = struct.pack(">H", 1)

    out = bytearray(hdr)
    out += b"\0" * (CLUSTER - len(hdr))
    out += struct.pack(">Q", 2 * CLUSTER)                     # refcount table
    out += b"\0" * (CLUSTER - 8)
    out += refcounts
    out += l1 + b"\0" * (CLUSTER - len(l1))
    out += l2 + b"\0" * (CLUSTER - len(l2))
    for b in blocks:
        out += b + b"\0" * (CLUSTER - len(b))
    return bytes(out)


# --- fixtures ----------------------------------------------------------


def _volume_raw() -> bytes:
    v = machfs.Volume()
    v.name = "QCOW Disk"
    f = machfs.File()
    f.type, f.creator, f.data = b"TEXT", b"ttxt", bytearray(b"qcow payload " * 100)
    v["readme.txt"] = f
    return v.write(size=800 * 1024)


# --- detection ---------------------------------------------------------


def test_qcow2_is_detected_as_a_disk_image():
    raw = _volume_raw()
    img = _qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)])
    assert is_qcow2(img) and is_disk_image(img)
    assert not is_qcow2(b"not a qcow2 at all")


def _macfile(data: bytes):
    from macrt.macfile import MacFile
    return MacFile("disk.qcow2", data, None, None, b"????", b"????", 0, "k", None)


def test_a_qcow2_is_named_by_kind():
    raw = _volume_raw()
    img = _qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)])
    assert image_kind(_macfile(img)) == "qcow2 disk image"


# --- browsing ----------------------------------------------------------


def test_a_v3_qcow2_opens_as_a_volume():
    raw = _volume_raw()
    img = _qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)])
    vol = load_volume(img)
    assert vol.name == "QCOW Disk"
    assert vol["readme.txt"].data == b"qcow payload " * 100


def test_a_v2_qcow2_opens_as_a_volume():
    raw = _volume_raw()
    img = _qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)], version=2)
    vol = load_volume(img)
    assert vol.name == "QCOW Disk"
    assert vol["readme.txt"].data == b"qcow payload " * 100


def test_a_qcow2_with_compressed_clusters_opens():
    raw = _volume_raw()
    hunks = [raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]
    img = _qcow2_file(hunks, compressed=set(range(len(hunks))))
    vol = load_volume(img)
    assert vol.name == "QCOW Disk"
    assert vol["readme.txt"].data == b"qcow payload " * 100


def test_zero_flag_and_unallocated_clusters_read_zeros():
    raw = _volume_raw()
    hunks = [raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]
    img = _qcow2_file(hunks, zero={0})                 # hunk 0 reads as zeros
    out = qcow2.read_image(img)
    assert out[:CLUSTER] == b"\0" * CLUSTER           # the zero flag
    assert out[CLUSTER:2 * CLUSTER] == hunks[1]        # its neighbours are intact


# --- corruption and refusal --------------------------------------------


def _fixture() -> bytearray:
    raw = _volume_raw()
    return bytearray(_qcow2_file([raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]))


@pytest.mark.parametrize("mutate, match", [
    (lambda d: d.__setitem__(slice(0, 4), b"nope"), "signature"),
    (lambda d: struct.pack_into(">I", d, 4, 6), "version"),
    (lambda d: struct.pack_into(">I", d, 32, 1), "encrypted"),
    (lambda d: (struct.pack_into(">QI", d, 8, CLUSTER, 4)), "backing"),
    (lambda d: struct.pack_into(">Q", d, 72, 1), "dirty"),
    (lambda d: struct.pack_into(">Q", d, 72, 1 << 2), "external"),
    (lambda d: struct.pack_into(">Q", d, 72, 1 << 4), "extended L2"),
    (lambda d: struct.pack_into(">Q", d, 4 * CLUSTER, (len(d) + CLUSTER) | (1 << 63)), "ends early"),
    (lambda d: struct.pack_into(">Q", d, 4 * CLUSTER, 1 << 62), "deflate"),
    (lambda d: struct.pack_into(">Q", d, 4 * CLUSTER, 1 << 56), "reserved"),
])
def test_a_corrupt_or_unsupported_qcow2_is_refused(mutate, match):
    d = _fixture()
    mutate(d)
    with pytest.raises(Qcow2Error, match=match):
        qcow2.read_image(bytes(d))


def test_a_v2_image_with_a_zero_flag_is_refused():
    raw = _volume_raw()
    hunks = [raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]
    img = bytearray(_qcow2_file(hunks, version=2))
    struct.pack_into(">Q", img, 4 * CLUSTER, 1)        # the zero flag, in v2
    with pytest.raises(Qcow2Error, match="version 2"):
        qcow2.read_image(bytes(img))


def test_a_non_deflate_compression_type_is_refused():
    raw = _volume_raw()
    hunks = [raw[i:i + CLUSTER] for i in range(0, len(raw), CLUSTER)]
    img = _qcow2_file(hunks, header_length=112, compression_type=1)
    with pytest.raises(Qcow2Error, match="deflate"):
        qcow2.read_image(img)


def test_a_short_header_is_refused():
    with pytest.raises(Qcow2Error, match="signature"):
        qcow2.QCOW2(b"QFI\xfb" + b"\0" * 60)
