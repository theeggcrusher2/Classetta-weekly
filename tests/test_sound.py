"""The Sound Driver's synthesizers, the speaker, and where its sound goes
(macrt.m68k.sound, macrt.ui.audio).  The driver itself, called from 68k
code, is test_m68k_apps' Sounds; its immediate writes are here."""

import struct
import wave
from types import SimpleNamespace

from PyQt6.QtMultimedia import QAudioFormat

from macrt.m68k import glue_devices, sound
from macrt.m68k.cpu import CPU
from macrt.m68k.hardware import HZ, vBufA, vBufB
from macrt.m68k.runtime import machine_for
from macrt.memory import RAM
from macrt.toolbox import lowmem
from macrt.toolbox.system import System
from macrt.ui.audio import Converter

from guest import Processor

SYSTEM_24 = SimpleNamespace(mmu_32_bit=False, addr_mask=0xFFFFFF)

FRAME = sound.FRAME


def runs(frame: bytes) -> list[int]:
    """How long each level lasts, in samples."""
    out, n = [], 1
    for a, b in zip(frame, frame[1:]):
        if a == b:
            n += 1
        else:
            out.append(n)
            n = 1
    return out + [n]


def test_a_square_wave_is_the_count_s_frequency_about_silence():
    count = 1958                                    # 783,360 / 1958: 400 Hz
    frame, phase = sound.square_wave(count, 200, 0)
    assert set(frame) == {28, 228}
    period = sound.RATE * count / HZ                # 55.5 samples
    assert all(abs(n - period / 2) <= 1 for n in runs(frame)[1:-1])
    # The next frame goes on where this one stopped.
    two = frame + sound.square_wave(count, 200, phase)[0]
    assert all(abs(n - period / 2) <= 1 for n in runs(two)[1:-1])
    assert sound.square_wave(0, 200, phase) == (sound.SILENT_FRAME, phase)
    assert set(sound.square_wave(count, 255, 0)[0]) == {0, 255}


def test_four_tones_are_added_and_divided_by_four():
    ramp = bytes(range(256))
    tones = [(0x10000, 0, ramp), (0x8000, 0, ramp), (0, 0x50000, ramp), (0, 0, sound.SILENT_WAVE)]
    frame, phases = sound.four_tone(tones)
    expect = bytes(((i & 0xFF) + (i // 2 & 0xFF) + 5 + 0x80) >> 2 for i in range(FRAME))
    assert frame == expect
    assert phases == [FRAME * 0x10000 & 0xFFFFFF, FRAME * 0x8000 & 0xFFFFFF, 0x50000, 0]


def test_a_free_form_sound_steps_through_its_bytes_then_is_silent():
    data = bytes(range(100))
    frame, position = sound.free_form(data, 0, 0, 0x8000, 100)
    assert frame[:6] == bytes([0, 0, 1, 1, 2, 2])
    assert frame[198:202] == bytes([99, 99, 0x80, 0x80])
    assert position == FRAME * 0x8000
    # From further on, with only the bytes it needs.
    frame, _ = sound.free_form(data[50:], 50, 50 << 16, 0x10000, 100)
    assert frame[:50] == data[50:] and set(frame[50:]) == {0x80}


class FakeVIA:
    def __init__(self):
        self.regs = bytearray(16)
        self.regs[vBufB] = sound.vSndEnb            # off
        self.regs[vBufA] = 7


class Frames:
    def __init__(self):
        self.frames = []

    def frame(self, samples):
        self.frames.append(samples)


def test_the_speaker_plays_the_buffer_s_high_bytes_while_it_s_on_at_its_volume():
    mem, via, out = RAM(SYSTEM_24, 0x10000), FakeVIA(), Frames()
    mem.w32(lowmem.SoundBase, 0x1000)
    mem.write(0x1000, bytes([0xC0, 0x11]) * FRAME)
    speaker = sound.Speaker(mem, via, [out])
    speaker.retrace()
    assert out.frames == []                         # off
    via.regs[vBufB] = 0
    speaker.retrace()
    assert out.frames == [bytes([0xC0]) * FRAME]
    via.regs[vBufA] = 0x48 | 3                      # the other bits aren't the volume
    speaker.retrace()
    assert out.frames[-1] == bytes([0x80 + 0x40 * 3 // 7]) * FRAME
    via.regs[vBufA] = 0
    speaker.retrace()
    assert out.frames[-1] == sound.SILENT_FRAME
    assert not sound.Speaker(mem, via).on           # nowhere for it to go


RING = bytes.fromhex("2078 0266"         # movea.l SoundBase,a0
                     "43E8 0014"         # lea 20(a0),a1: from sample 10
                     "7000"              # moveq #0,d0
                     "323C 0167"         # move.w #359,d1
                     "1280 5489 5200"    # move.b d0,(a1); addq.l #2,a1; addq.b #1,d0
                     "51C9 FFF8"         # dbra d1,*-6
                     "2248"              # movea.l a0,a1: then round to 0
                     "7209"              # moveq #9,d1
                     "1280 5489 5200"
                     "51C9 FFF8")
CLEAR = bytes.fromhex("2078 0266"        # movea.l SoundBase,a0
                      "323C 0171"        # move.w #369,d1
                      "10BC 0080 5488"   # move.b #$80,(a0); addq.l #2,a0
                      "51C9 FFF8")       # dbra d1,*-6


def test_a_frame_written_round_the_buffer_plays_from_where_it_starts():
    """Creepy Castle writes its samples from the buffer's 10th on, round to
    the 9th: the speaker watches a tick's stores and plays from there,
    and the Sound Driver's frames are put there too.  Silence written from
    the first sample doesn't say otherwise."""
    mem, via, out = RAM(SYSTEM_24, 0x20000), FakeVIA(), Frames()
    mem.w32(lowmem.SoundBase, 0x1000)
    mem.write(0x1000, bytes([0x80, 0]) * FRAME)
    mem.write(CODE, RING)
    mem.write(CODE + 0x100, CLEAR)
    via.regs[vBufB] = 0                             # on
    cpu = CPU(mem)

    def run(code: int, length: int) -> None:
        cpu.pc = code
        assert cpu.run(code + length).kind == "until"

    try:
        speaker = sound.Speaker(mem, via, [out], cpu=cpu)
        speaker.retrace()                           # the stores till the next are watched
        run(CODE, len(RING))
        speaker.retrace()
        written = bytes(n & 0xFF for n in range(FRAME))
        assert speaker.start == 10
        assert out.frames[-1] == written
        assert mem.r8(0x1000 + 2 * 10) == 0 and mem.r8(0x1000) == 360 & 0xFF
        speaker.retrace()                           # no stores, and unwatched: as it was
        assert speaker.start == 10 and out.frames[-1] == written
        tone = sound.square_wave(1958, 200, 0)[0]
        speaker.fill(tone)
        assert mem.r8(0x1000 + 2 * 10) == tone[0] and mem.r8(0x1000 + 2 * 9) == tone[-1]
        speaker.retrace()
        assert out.frames[-1] == tone
        while speaker._hook is None:                # till the next tick watched
            speaker.retrace()
        run(CODE + 0x100, len(CLEAR))
        speaker.retrace()
        assert out.frames[-1] == sound.SILENT_FRAME and speaker.start == 10
    finally:
        cpu.close()


def test_only_stores_of_each_sample_once_round_the_buffer_say_where_frames_start():
    in_order = [(10 + n) % FRAME for n in range(FRAME)]
    pairs = [(40 + (n ^ 1)) % FRAME for n in range(FRAME)]      # Dark Castle's, second first
    assert sound._round_from(in_order, 10) and not sound._round_from(in_order, 11)
    assert sound._round_from(pairs, 40) and not sound._round_from(pairs, 41)
    assert sound._round_from([(368 + n) % FRAME for n in range(FRAME)], 368)
    assert not sound._round_from(in_order[:-1], 10)                 # not all of them
    assert not sound._round_from(in_order[:-1] + [10], 10)          # one twice
    assert not sound._round_from(in_order[:200][::-1] + in_order[200:], 10)


def test_a_wave_file_holds_each_frame(tmp_path):
    path = tmp_path / "a.wav"
    w = sound.WaveFile(str(path))
    w.frame(bytes(i & 0xFF for i in range(FRAME)))
    w.frame(sound.SILENT_FRAME)
    with wave.open(str(path)) as r:                 # whole already
        assert (r.getnframes(), r.getframerate(), r.getsampwidth()) == (2 * FRAME, 22200, 1)
    w.close()
    w.frame(sound.SILENT_FRAME)                     # after it's closed: nothing
    with wave.open(str(path)) as r:
        assert r.readframes(FRAME)[:3] == bytes([0, 1, 2])


CODE = 0x10000


def test_the_sound_chip_plays_what_68k_code_writes_to_its_fifos():
    """Digital Messiah's VBL task writes its music to the Apple Sound Chip
    with MOVEM.L: the speaker plays it at the next tick, the two FIFOs'
    averaged, then silence, and nothing once they're empty.  What's written
    from $800 reads back; the FIFOs read 0."""
    mem, out = RAM(SYSTEM_24, 0x20000), Frames()
    chip = sound.ASC()
    cpu = CPU(mem)
    chip.map(cpu.uc)
    mem.write(CODE, bytes.fromhex(f"207C{sound.ASC_BASE:08X}"  # movea.l #ASC,a0
                                  "203C 0102 0304"          # move.l #$01020304,d0
                                  "223C 0506 0708"          # move.l #$05060708,d1
                                  "48D0 0003"               # movem.l d0-d1,(a0)
                                  "117C 0040 0400"          # move.b #$40,$400(a0)
                                  "117C 0001 0801"          # move.b #1,$801(a0)
                                  "1428 0801"               # move.b $801(a0),d2
                                  "1610"))                  # move.b (a0),d3
    try:
        cpu.set_d(2, 0xFFFFFFFF)
        cpu.set_d(3, 0xFFFFFFFF)
        cpu.pc = CODE
        assert cpu.run(CODE + 40).kind == "until"
        assert (cpu.d(2), cpu.d(3)) == (0xFFFFFF01, 0xFFFFFF00)
    finally:
        cpu.close()
    speaker = sound.Speaker(mem, FakeVIA(), [out], chip)    # its own sound off
    assert speaker.waiting
    speaker.retrace()
    first = bytes(range(1, 9)).ljust(FRAME, b"\x80")
    second = b"\x40".ljust(FRAME, b"\x80")
    assert out.frames == [bytes((a + b) >> 1 for a, b in zip(first, second))]
    assert not speaker.waiting
    speaker.retrace()
    assert len(out.frames) == 1


def test_a_full_fifo_drops_what_s_written_and_the_speaker_s_own_sound_is_added():
    mem, via, out = RAM(SYSTEM_24, 0x20000), FakeVIA(), Frames()
    chip = sound.ASC()
    cpu = CPU(mem)
    chip.map(cpu.uc)
    mem.write(CODE, bytes.fromhex(f"207C{sound.ASC_BASE:08X}"  # movea.l #ASC,a0
                                  "343C 04FF"               # move.w #$4FF,d2
                                  "1142 0400"               # move.b d2,$400(a0)
                                  "51CA FFFA"))             # dbra d2,*-4
    try:
        cpu.pc = CODE
        assert cpu.run(CODE + 18).kind == "until"
    finally:
        cpu.close()
    kept = bytes((0x4FF - n) & 0xFF for n in range(sound.FIFO))
    mem.w32(lowmem.SoundBase, 0x1000)
    mem.write(0x1000, bytes([0xC0, 0]) * FRAME)
    via.regs[vBufB] = 0                                     # on
    speaker = sound.Speaker(mem, via, [out], chip)
    for _ in range(4):
        speaker.retrace()
    chips = [kept[:FRAME], kept[FRAME:2 * FRAME], kept[2 * FRAME:].ljust(FRAME, b"\x80")]
    mixed = [bytes(min(255, s + 0x40) for s in f) for f in chips]
    assert out.frames == mixed + [bytes([0xC0]) * FRAME]   # then only the speaker's


def test_a_mac_ii_has_the_sound_chip_where_ascbase_says_and_a_plus_has_none(qapp):
    for model, base in (("ii", sound.ASC_BASE), ("plus", 0)):
        system = System(model=model)
        m = machine_for(system)
        try:
            assert system.mem.r32(lowmem.ASCBase) == base
            assert (m.asc is not None) is bool(base)
        finally:
            m.cpu.close()


def test_where_a_colour_machine_has_the_chip_a_plus_reads_zeros(qapp):
    """Excel 1.5, on a ROM older than the Mac II's, reads two words there and
    compares them with 'RA'; what's written there goes nowhere."""
    system = System(model="plus")
    m = machine_for(system)
    try:
        system.mem.write(CODE, bytes.fromhex(f"2039{sound.ASC_BASE + 8:08X}"   # move.l ASC+8,d0
                                             f"23C1{sound.ASC_BASE + 8:08X}"   # move.l d1,ASC+8
                                             f"2439{sound.ASC_BASE + 8:08X}"))  # move.l ASC+8,d2
        m.cpu.set_d(0, 0xFFFFFFFF)
        m.cpu.set_d(1, 0x12345678)
        m.cpu.pc = CODE
        assert m.cpu.run(CODE + 18).kind == "until"
        assert (m.cpu.d(0), m.cpu.d(2)) == (0, 0)
    finally:
        m.cpu.close()


def test_the_host_gets_the_changes_in_level_at_its_own_rate():
    fmt = QAudioFormat.SampleFormat
    c = Converter(sound.RATE, 1, fmt.Int16)
    held = struct.unpack(f"={FRAME}h", c(bytes([0xFF]) * FRAME))
    assert held[0] > 32000 and 0 < held[-1] < held[0]   # a level that holds drifts back
    assert struct.unpack("=3h", c(sound.SILENT_FRAME)[:6])[0] < 0
    # 48 kHz in stereo floats, in both channels: the level goes up on the
    # line from silence to the first $FF, a sample late.
    c = Converter(48000, 2, fmt.Float)
    out = c(sound.SILENT_FRAME + bytes([0xFF]) * FRAME)
    assert len(out) == 4 * 2 * 1600                 # 2 frames' worth at 48 kHz
    floats = struct.unpack(f"={2 * 1600}f", out)
    left = floats[0::2]
    assert left == floats[1::2]
    assert left[:801] == (0.0,) * 801
    assert 0.4 < left[801] < 0.5 and 0.9 < left[802] < left[803]
    assert c.align(4 * 2 * 3 + 5) == 4 * 2 * 3


def test_a_ramp_at_the_mac_s_rate_is_a_ramp_at_the_host_s():
    """Each of the host's samples is on the line between the two of the
    Mac's it comes between, so a ramp goes up as evenly at 44.1 or 48 kHz:
    holding each level for as long as it lasts would step it up unevenly,
    adding tones of its own."""
    for rate in (44100, 48000):
        c = Converter(rate, 1, QAudioFormat.SampleFormat.Float)
        out = c._resample([n / FRAME for n in range(FRAME)]) + c._resample([1.0] * FRAME)
        assert len(out) == 2 * FRAME * rate // sound.RATE
        ramp = out[3:FRAME * rate // sound.RATE]        # from its first sample on
        steps = [b - a for a, b in zip(ramp, ramp[1:])]
        assert max(steps) - min(steps) < 1e-9


def test_an_immediate_write_starts_its_sound_and_returns_at_once(qapp):
    """Apache Strike starts its engine's four tones, which last over two
    minutes, with an immediate Write, and changes them from its own VBL
    task: it isn't kept waiting for the sound to end, as a synchronous
    write's caller is."""
    p = Processor()
    machine = p.machine
    machine.sound_task = p.ptr(bytes.fromhex("4e75"))
    machine.wait_for_io = p.ptr(bytes.fromhex("4e75"))
    driver = sound.SoundDriver(machine)
    record = p.ptr(struct.pack(">H", 0x7FFF) + bytes(48))      # its duration, in ticks
    synth = p.ptr(struct.pack(">hI", sound.ftMode, record))
    try:
        for w, waits in ((0xA203, False), (0xA003, True)):     # _Write,IMMED; _Write
            pb = p.ptr(bytes(50))
            p.mem.w16(pb + glue_devices.ioRefNum, sound.SOUND_DRIVER)
            p.mem.w32(pb + sound.ioBuffer, synth)
            p.mem.w32(pb + sound.ioReqCount, 6)
            p.cpu.set_a(0, pb)
            p.cpu.set_d(0, 0xFFFF)
            p.cpu.pc = at = p.ptr(bytes(2))
            assert driver.call(p, w)
            assert p.mem.r16s(pb + glue_devices.ioResult) == 1     # playing, or queued
            assert p.cpu.pc == (machine.wait_for_io if waits else at)
            if not waits:
                assert p.cpu.d(0) == glue_devices.noErr
                assert driver.playing is not None and driver.playing.pb == pb
    finally:
        p.cpu.close()


def test_the_requests_are_in_the_dce_s_queue_and_kill_io_stops_what_completions_start(qapp):
    """The Sound Driver's requests are in the queue of its DCE, at
    SoundDCE.  Downhill Racer's StartSound glue copies the one playing
    and puts the copy in the queue's head and tail, and it's the copy
    that's done.  Its completion routine starts the next sound it kept,
    and KillIO stops that one too."""
    p = Processor()
    machine = p.machine
    machine.sound_task = p.ptr(bytes.fromhex("4e75"))
    driver = sound.SoundDriver(machine)
    driver.install(p.system.sys_heap)
    dce = p.mem.r32(lowmem.SoundDCE)
    record = p.ptr(struct.pack(">H", 0x7FFF) + bytes(48))      # its duration, in ticks
    synth = p.ptr(struct.pack(">hI", sound.ftMode, record))
    completion = p.ptr(bytes(2))
    first, copy, later, kill = (p.ptr(bytes(50)) for _ in range(4))
    called = []

    def call(pb: int, w: int) -> None:
        p.mem.w16(pb + glue_devices.ioRefNum, sound.SOUND_DRIVER)
        p.cpu.set_a(0, pb)
        assert driver.call(p, w)

    def write(pb: int) -> None:                 # an asynchronous Write
        p.mem.w32(pb + glue_devices.ioCompletion, completion)
        p.mem.w32(pb + sound.ioBuffer, synth)
        p.mem.w32(pb + sound.ioReqCount, 6)
        call(pb, 0xA403)

    def completed(addr: int, a0: int, d0: int) -> int:
        assert addr == completion
        called.append((a0, d0))
        if a0 == copy:
            write(later)
        return 0

    def queue_of(dce: int) -> tuple[int, int]:
        return p.mem.r32(dce + glue_devices.dCtlQHead), p.mem.r32(dce + glue_devices.dCtlQTail)

    p.call_with = completed
    try:
        write(first)
        assert queue_of(dce) == (first, first)
        assert p.mem.r16(first + glue_devices.qType) == glue_devices.ioQType
        p.mem.write(copy, p.mem.read(first, 50))
        p.mem.w32(dce + glue_devices.dCtlQHead, copy)
        p.mem.w32(dce + glue_devices.dCtlQTail, copy)
        call(kill, 0xA006)                      # _KillIO
        aborted = glue_devices.abortErr & 0xFFFFFFFF
        assert called == [(copy, aborted), (later, aborted)]
        assert [p.mem.r16s(pb + glue_devices.ioResult) for pb in (first, copy, later, kill)] \
            == [1, glue_devices.abortErr, glue_devices.abortErr, glue_devices.noErr]
        assert queue_of(dce) == (0, 0)
        assert driver.playing is None and not driver.queue
    finally:
        p.cpu.close()
