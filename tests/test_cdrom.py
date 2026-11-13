"""A disk image named as a CD-ROM's is mounted as one: its driver has an
entry in the unit table, named .AppleCD, and answers the Control calls
Technical Note DV 22, "CD-ROM Driver Calls", gives the Apple CD-ROM
driver, as a disc with one data track would (Lode Runner: The Legend
Returns)."""

import struct

import machfs
import pytest

from macrt.m68k import glue_cdrom as cd
from macrt.m68k import glue_devices as dev
from macrt.macfile import open_disks
from macrt.toolbox import files as fm

from guest import Processor
from test_drivers import call, entry, processor

OPEN, CONTROL = b"\xa0\x00", b"\xa0\x04"
SIZE = 4 * 1024 * 1024                  # bytes: 2048 CD blocks, 27 seconds 23 frames


def image(tmp_path, name: str, size: int = SIZE):
    v = machfs.Volume()
    v.name = "Disc"
    f = machfs.File()
    f.type, f.creator = b"APPL", b"Disc"
    v["Program"] = f
    path = tmp_path / name
    path.write_bytes(v.write(size, desktopdb=False, bootable=False))
    return open_disks([path])[1][0]


def mounted(tmp_path, name: str, size: int = SIZE, after: bool = True):
    """A processor with the image mounted, after the unit table is set up
    or before."""
    volume = image(tmp_path, name, size)
    if after:
        p = processor()
        return p, p.system.files.mount("Disc", fm.ImageStore(volume))
    p = Processor()
    vol = p.system.files.mount("Disc", fm.ImageStore(volume))
    p.machine.system = p.system
    p.machine.io_done = p.ptr(b"\x4e\x75")
    p.machine.drivers = {}
    dev.init(p.machine)
    return p, vol


@pytest.mark.parametrize("name", ["Disc.iso", "Disc.CDR", "Disc.toast"])
@pytest.mark.parametrize("after", [True, False])
def test_a_cd_images_driver_is_in_the_unit_table_as_applecd(qapp, tmp_path, name, after):
    p, vol = mounted(tmp_path, name, after=after)
    m = p.mem
    assert vol.cdrom and not vol.floppy and vol.driver <= -33
    dce = m.r32(entry(p, vol.driver)) & m.system.addr_mask
    assert m.r16s(dce + dev.dCtlRefNum) == vol.driver
    assert m.r16(dce + dev.dCtlFlags) & dev.dOpened
    assert m.r_pstr(m.r32(dce + dev.dCtlDriver) + dev.drvrName) == b".AppleCD"
    # OpenDriver finds it, already open.
    pb = p.ptr(bytes(50))
    m.w32(pb + 18, p.ptr(b"\x08.AppleCD"))
    assert call(p, OPEN, 0, pb) == dev.noErr
    assert m.r16s(pb + 24) == vol.driver


def test_a_small_cd_image_is_still_a_cd(qapp, tmp_path):
    p, vol = mounted(tmp_path, "Small.iso", 800 * 1024)
    assert vol.cdrom and not vol.floppy and entry(p, vol.driver)


@pytest.mark.parametrize("name", ["Disc.img", "Disc.dsk", "Disc.image"])
def test_other_images_drivers_arent(qapp, tmp_path, name):
    p, vol = mounted(tmp_path, name)
    assert not vol.cdrom and entry(p, vol.driver) == 0
    control(p, vol, cd.ReadTOC, b"\0\1")
    assert result(p) == cd.controlErr                   # a hard disk's driver has no calls


def control(p, vol, code: int, param: bytes = b""):
    pb = p.ptr(bytes(50))
    p.mem.write(pb + 24, struct.pack(">hh", vol.driver, code) + param)
    p.cpu.set_a(0, pb)
    p.run(CONTROL)
    return pb


def result(p) -> int:
    d0 = p.cpu.d(0)
    return d0 - (1 << 32 if d0 & 0x80000000 else 0)


def test_read_toc_gives_the_one_data_track(qapp, tmp_path):
    p, vol = mounted(tmp_path, "Disc.iso")
    m = p.mem
    pb = control(p, vol, cd.ReadTOC, struct.pack(">H", 1))
    assert result(p) == dev.noErr and m.read(pb + cd.csParam, 2) == b"\x01\x01"
    # The Lead Out: 2048 blocks after 00:02:00 is 00:29:23, in BCD.
    pb = control(p, vol, cd.ReadTOC, struct.pack(">H", 2))
    assert m.read(pb + cd.csParam, 4) == b"\x00\x29\x23\x00"
    buffer = p.ptr(bytes(8))
    pb = control(p, vol, cd.ReadTOC, struct.pack(">HLHB", 3, buffer, 8, 1))
    assert result(p) == dev.noErr and m.read(buffer, 8) == b"\x04\x00\x02\x00" + bytes(4)
    table = p.ptr(bytes(512))
    control(p, vol, cd.ReadTOC, struct.pack(">HL", 4, table))
    assert m.read(table, 21) == (b"\0" + b"\x04\xa0\x01\0\0" + b"\x04\xa1\x01\0\0"
                                 + b"\x04\xa2\x00\x29\x23" + b"\x04\x01\x00\x02\x00")
    pb = control(p, vol, cd.ReadTOC, struct.pack(">H", 5))
    assert m.read(pb + cd.csParam, 10) == b"\0\1\0\1\0\1\x04\x00\x02\x00"
    control(p, vol, cd.ReadTOC, struct.pack(">H", 7))
    assert result(p) == fm.paramErr


def test_audio_calls_are_refused_and_the_volume_is_kept(qapp, tmp_path):
    """The Note's audio calls on a data track are paramErr; AudioControl and
    ReadAudioVolume work with no audio to play."""
    p, vol = mounted(tmp_path, "Disc.iso")
    for code in cd.AUDIO:
        control(p, vol, code)
        assert result(p) == fm.paramErr
    pb = control(p, vol, cd.ReadAudioVolume)
    assert result(p) == dev.noErr and p.mem.r16(pb + cd.csParam) == cd.LOUDEST
    control(p, vol, cd.AudioControl, b"\x40\x80")
    pb = control(p, vol, cd.ReadAudioVolume)
    assert p.mem.r16(pb + cd.csParam) == 0x4080
    control(p, vol, 111)                                # one the Note has macrt hasn't
    assert result(p) == cd.controlErr


@pytest.mark.parametrize("name, attrib", [("Disc.iso", 0x0080), ("Disc.img", 0)])
@pytest.mark.parametrize("trap", [b"\xa0\x07", b"\xa2\x07"])      # PBGetVInfo, PBHGetVInfo
def test_a_cd_volume_is_locked_by_hardware(qapp, tmp_path, name, attrib, trap):
    """Bit 7 of a CD volume's attributes, "Set if the volume is locked by
    hardware" (Inside Macintosh: Files, "Volume Control Blocks"), as
    Technical Note FL 530 says a CD-ROM's is; bit 15 stays the software
    lock PBSetVInfo sets (Power Rangers Zeo's code checks bit 7 of its
    disc's)."""
    p, vol = mounted(tmp_path, name)
    m = p.mem
    pb = p.ptr(bytes(122))
    m.w16(pb + 22, vol.vref & 0xFFFF)
    assert call(p, trap, 0, pb) == dev.noErr
    assert m.r16(pb + 38) == attrib
    assert m.r16(vol.vcb + 18) == attrib                # vcbAtrb
    vol.locked = True
    p.system.files.update_volume(vol)
    assert call(p, trap, 0, pb) == dev.noErr
    assert m.r16(pb + 38) == 0x8000 | attrib
    assert m.r16(vol.vcb + 18) == 0x8000 | attrib


@pytest.mark.parametrize("trap", [b"\xa0\x07", b"\xa2\x07"])      # PBGetVInfo, PBHGetVInfo
def test_a_cd_volume_gives_its_own_blocks_and_free_ones(qapp, tmp_path, trap):
    """A CD's volume says how many allocation blocks it has, of what size,
    and how many are free, as the disc's MDB does: nothing written to it
    goes on the disc (Over the Reich looks for its CD by its size and its
    having none free)."""
    p, vol = mounted(tmp_path, "Disc.iso")
    blocks, size = struct.unpack_from(">HI", (tmp_path / "Disc.iso").read_bytes(), 1024 + 18)
    free, = struct.unpack_from(">H", (tmp_path / "Disc.iso").read_bytes(), 1024 + 34)
    assert blocks * size <= SIZE < (blocks + 1) * size + 1024 * 1024 and 0 < free < blocks
    m = p.mem
    pb = p.ptr(bytes(122))
    m.w16(pb + 22, vol.vref & 0xFFFF)
    assert call(p, trap, 0, pb) == dev.noErr
    assert struct.unpack_from(">HII", m.read(pb + 46, 10)) == (blocks, size, size)
    assert m.r16(pb + 62) == free                       # ioVFrBlk
    assert struct.unpack_from(">HII", m.read(vol.vcb + 26, 10)) == (blocks, size, size)
    assert m.r16(vol.vcb + 42) == free                  # vcbFreeBks


@pytest.mark.parametrize("trap", [b"\xa0\x07", b"\xa2\x07"])
def test_other_volumes_give_macrts_own_blocks(qapp, tmp_path, trap):
    p, vol = mounted(tmp_path, "Disc.img")
    m = p.mem
    pb = p.ptr(bytes(122))
    m.w16(pb + 22, vol.vref & 0xFFFF)
    assert call(p, trap, 0, pb) == dev.noErr
    assert struct.unpack_from(">HII", m.read(pb + 46, 10)) == (fm.VOLUME_BLOCKS, fm.BLOCK, fm.BLOCK)
    assert m.r16(pb + 62) == fm.FREE_BLOCKS
    assert m.r16(vol.vcb + 26) == fm.VOLUME_BLOCKS and m.r16(vol.vcb + 42) == fm.FREE_BLOCKS
