"""The Sound Manager (macrt.m68k.sound_manager, glue_sound): its
synthesizers, its channels' queues at each tick, and SndPlay's 'snd '
resources, played through the sound buffer."""

import math
import struct
import time

import pytest

from macrt.m68k import dispatch, runtime, sound_manager as sm
from macrt.m68k.hardware import vBufB
from macrt.m68k.sound import FRAME, SILENCE, SILENT_FRAME, vSndEnb
from macrt.macfile import MacFile, MemoryFolder
from macrt.resfork import Resource
from macrt.toolbox import files as fm
from macrt.toolbox import lowmem
from macrt.toolbox.files import FileManager

from guest import Guest, Processor

SILENT = bytes([SILENCE])


@pytest.fixture
def g(qapp):
    return Guest()


def command(c: int, p1: int = 0, p2: int = 0) -> bytes:
    return struct.pack(">HHI", c, p1 & 0xFFFF, p2 & 0xFFFFFFFF)


def header(samples: bytes, rate: int = sm.rate22khz, loop=(0, 0), base: int = 60) -> bytes:
    """A standard sound header, its samples after it."""
    return struct.pack(">5IBB", 0, len(samples), rate, *loop, 0, base) + samples


def channel(g, synth: int = sm.sampledSynth, callback: int = 0) -> int:
    out = g.ptr(bytes(4))
    assert g("SndNewChannel", out, synth, 0, callback) == 0
    return g.mem.r32(out)


def do(g, chan: int, c: int, p1: int = 0, p2: int = 0, no_wait: bool = False) -> int:
    return g("SndDoCommand", chan, g.ptr(command(c, p1, p2)), no_wait)


def tick(g) -> bytes:
    """A tick's frame, as the speaker would play it from the sound buffer."""
    g.machine.sound_manager.retrace(g)
    base = g.mem.r32(lowmem.SoundBase)
    return g.mem.read(base, 2 * FRAME)[0::2]


def speaker_on(g) -> bool:
    return not g.machine.via.regs[vBufB] & vSndEnb


# -- what the synthesizers play --------------------------------------------------
def test_samples_play_once_at_their_step_or_loop():
    s = sm.Samples(bytes(range(10)), 1.0)
    assert s.take(4) == (bytes(range(4)), False)
    assert s.take(10) == (bytes(range(4, 10)), True)
    assert sm.Samples(bytes(range(4)), 0.5).take(10) == (bytes([0, 0, 1, 1, 2, 2, 3, 3]), True)
    # Looped, it goes back to the loop's start each time it reaches the end.
    looped = sm.Samples(bytes(range(10)), 1.0, loop=(2, 6))
    assert looped.take(12) == (bytes([0, 1, 2, 3, 4, 5, 2, 3, 4, 5, 2, 3]), False)
    # At half the amplitude, half as far from silence.
    assert sm.Samples(bytes([0x80, 0xC0, 0x40]), 1.0, 127).take(3)[0] == bytes([0x80, 0x9F, 0x60])


def test_a_tone_repeats_its_waveform_at_its_frequency():
    tone = sm.Tone(sm.note_wave(0), sm.MAC_RATE / 64)    # a period every 64 samples
    out, ended = tone.take(128)
    assert not ended and out[:64] == out[64:]
    assert max(out) == 255 and min(out) == 1                # a sine, about silence
    # Timbre 255 is all but a square wave.
    assert sum(x in (1, 255) for x in sm.note_wave(255)) > 200
    assert sm.hz(69) == 440 and sm.hz(0x01B800) == 440      # A, as a note and in 16.8


def test_frames_are_added_about_silence():
    a, b = bytes([0x90, 0xF0, 0x10]), bytes([0x90, 0xF0, 0x10])
    assert sm.mix([a]) == a
    assert sm.mix([a, b]) == bytes([0xA0, 0xFF, 0x00])
    assert sm.mix([SILENT_FRAME, SILENT_FRAME]) == SILENT_FRAME


def test_a_snd_resource_s_synthesizer_and_commands():
    sound_cmd = sm.soundCmd | sm.dataPointerFlag
    one = struct.pack(">hh hI h", 1, 1, sm.sampledSynth, 0x80, 1) + command(sound_cmd, 0, 20)
    # A soundCmd no note follows is played too, as a buffer.
    assert sm.parse_snd(one) == (sm.sampledSynth, 0x80, [
        (sound_cmd, 0, 20), (sm.bufferCmd | sm.dataPointerFlag, 0, 20)])
    two = struct.pack(">hhh", 2, 0, 1) + command(sm.bufferCmd | sm.dataPointerFlag, 0, 14)
    assert sm.parse_snd(two) == (0, 0, [(sm.bufferCmd | sm.dataPointerFlag, 0, 14)])
    # An instrument, with notes for it, isn't.
    notes = struct.pack(">hhh", 2, 0, 2) + command(sound_cmd, 0, 22) + command(sm.noteCmd, 960, 60)
    assert len(sm.parse_snd(notes)[2]) == 2
    # Past the last note, the first soundCmd is
    after = struct.pack(">hhh", 2, 0, 4) + command(sound_cmd, 0, 22) + \
        command(sm.noteCmd, 960, 60) + command(sm.restCmd, 0, 0) + command(sound_cmd, 0, 30)
    assert sm.parse_snd(after)[2][-2:] == [(sound_cmd, 0, 30),
                                           (sm.bufferCmd | sm.dataPointerFlag, 0, 30)]
    assert sm.parse_snd(b"snd") is None and sm.parse_snd(struct.pack(">hh", 3, 0)) is None
    # in time that grows with the commands, not their square
    many = struct.pack(">hhh", 2, 0, 8000) + command(sound_cmd, 0, 30) * 7999 + \
        command(sm.noteCmd, 960, 60)
    start = time.perf_counter()
    assert len(sm.parse_snd(many)[2]) == 8000
    assert time.perf_counter() - start < 1


def test_a_sound_header_s_samples(g):
    at = g.ptr(header(bytes(range(40)), loop=(10, 30), base=72))
    assert sm.read_header(g.mem, at) == (bytes(range(40)), sm.rate22khz, (10, 30), 72)
    # An extended header's 16-bit stereo: the channels' high bytes, as offset
    # binary, averaged.
    frames = struct.pack(">4h", 0x1000, 0x3000, -0x1000, -0x3000)
    ext = struct.pack(">5IBB", 0, 2, sm.rate22khz, 0, 0, sm.extSH, 60) + struct.pack(">I", 2)
    ext += bytes(48 - len(ext)) + struct.pack(">H", 16) + bytes(14) + frames
    assert sm.read_header(g.mem, g.ptr(ext))[0] == bytes([0xA0, 0x60])
    compressed = header(bytes(8))[:20] + bytes([0xFE, 60]) + bytes(8)
    assert sm.read_header(g.mem, g.ptr(compressed)) is None


# -- channels, a tick at a time --------------------------------------------------
def test_a_buffer_plays_then_the_commands_after_it(g):
    called = []
    g.procs[0x1234] = lambda chan, cmd: called.append(struct.unpack(">HHI", g.mem.read(cmd, 8)))
    chan = channel(g, callback=0x1234)
    assert g.mem.r32(chan + sm.CALLBACK) == 0x1234 and g.mem.r16(chan + sm.QLENGTH) == 128
    samples = bytes(i * 7 % 251 + 2 for i in range(500))
    assert do(g, chan, sm.bufferCmd, 0, g.ptr(header(samples))) == 0
    assert do(g, chan, sm.callBackCmd, 1, 2) == 0
    assert not called and not speaker_on(g)

    assert tick(g) == samples[:370] and speaker_on(g) and not called
    assert tick(g) == samples[370:] + SILENT * 240
    assert called == [(sm.callBackCmd, 1, 2)]
    assert tick(g) == SILENT_FRAME and not speaker_on(g)    # done: the speaker's as it was


def test_a_buffer_plays_at_its_rate_times_rate_cmd_s(g):
    chan = channel(g)
    do(g, chan, sm.rateCmd, 0, 0x8000)                       # half as fast
    do(g, chan, sm.bufferCmd, 0, g.ptr(header(bytes(range(100, 250)), rate=2 * sm.rate22khz)))
    assert tick(g)[:150] == bytes(range(100, 250))           # at twice the rate: a sample a sample


def test_notes_last_their_duration(g):
    called = []
    g.procs[0x40] = lambda chan, cmd: called.append(len(called))
    chan = channel(g, sm.noteSynth, callback=0x40)
    do(g, chan, sm.noteCmd, 200, 69)                         # A for 100 ms: 2225 samples
    do(g, chan, sm.callBackCmd)
    frames = [tick(g) for _ in range(6)]
    assert not called
    assert all(len(set(f)) > 2 for f in frames)              # a tone throughout
    frame = tick(g)
    assert called == [0] and frame[2225 - 6 * FRAME:] == SILENT * (7 * FRAME - 2225)
    assert tick(g) == SILENT_FRAME


def test_a_wait_holds_the_commands_after_it(g):
    called = []
    g.procs[0x40] = lambda chan, cmd: called.append(1)
    chan = channel(g, callback=0x40)
    do(g, chan, sm.waitCmd, 2000)                            # a second: 60.15 ticks
    do(g, chan, sm.callBackCmd)
    for _ in range(60):
        g.machine.sound_manager.retrace(g)
    assert not called
    g.machine.sound_manager.retrace(g)
    assert called == [1]


def test_a_sampled_instrument_plays_notes_at_its_pitch(g):
    chan = channel(g)
    wave = bytes(SILENCE + (40 if i < 8 else -40) for i in range(16))
    instrument = g.ptr(header(wave, loop=(0, 16), base=60))
    do(g, chan, sm.soundCmd, 0, instrument)
    do(g, chan, sm.noteCmd, 2000, 72)                        # an octave up: twice as fast
    frame = tick(g)
    assert frame[:16] == wave[0::2] * 2


def test_a_full_queue(g):
    out = g.ptr(bytes(4))
    own = g.ptr(bytes(36 + 2 * 8))
    g.mem.w32(out, own)
    assert g("SndNewChannel", out, sm.noteSynth, 0, 0) == sm.badChannel     # no qLength
    g.mem.w16(own + sm.QLENGTH, 1)                           # room for one
    assert g("SndNewChannel", out, sm.noteSynth, 0, 0) == 0 and g.mem.r32(out) == own
    assert do(g, own, sm.waitCmd, 100) == 0
    assert do(g, own, sm.waitCmd, 100, no_wait=True) == sm.queueFull
    assert do(g, own, sm.waitCmd, 100) == 0                  # it waits its turn
    g.machine.sound_manager.retrace(g)
    assert g.mem.r16s(own + sm.QHEAD) == 0                   # the second, queued
    assert g("SndDisposeChannel", own, True) == 0
    assert g.heap.ptr_size(own) == 52                        # the program's own, still
    assert do(g, own, sm.waitCmd, 100) == sm.badChannel


def test_there_are_only_so_many_channels(g):
    """DeadEnd counts the channels it can have by asking for one more until
    SndNewChannel refuses."""
    chans = [channel(g) for _ in range(sm.MAX_CHANNELS)]
    out = g.ptr(bytes(4))
    assert g("SndNewChannel", out, sm.sampledSynth, 0, 0) == sm.notEnoughHardware
    assert g.mem.r32(out) == 0
    assert g("SndDisposeChannel", chans.pop(), True) == 0
    assert g("SndNewChannel", out, sm.sampledSynth, 0, 0) == 0


def test_immediate_commands_go_ahead_of_the_queue(g):
    chan = channel(g, sm.noteSynth)
    do(g, chan, sm.waitCmd, 4000)
    do(g, chan, sm.noteCmd, 4000, 60)
    assert g("SndDoImmediate", chan, g.ptr(command(sm.freqCmd, 0, 69))) == 0
    assert len(set(tick(g))) > 2                             # it plays, though the queue waits
    g("SndDoImmediate", chan, g.ptr(command(sm.quietCmd)))
    assert tick(g) == SILENT_FRAME


def test_modifiers_see_each_command_first(g):
    seen = []

    def modifier(chan, cmd, stub):
        c = struct.unpack(">HHI", g.mem.read(cmd, 8))
        seen.append(c[:2])
        if c[0] == sm.callBackCmd:                           # dropped
            g.mem.write(cmd, command(sm.nullCmd))
        return c[0] == sm.waitCmd                            # and one more after a wait

    called = []
    g.procs[0x80] = modifier
    g.procs[0x40] = lambda chan, cmd: called.append(1)
    chan = channel(g, callback=0x40)
    assert g("SndAddModifier", chan, 0x80, 0, 77) == 0
    assert seen == [(sm.initCmd, 0)]
    stub = g.mem.r32(chan + sm.FIRST_MOD)
    assert g.mem.r32(stub + sm.STUB_CODE) == 0x80
    do(g, chan, sm.callBackCmd)
    do(g, chan, sm.waitCmd, 10)
    g.machine.sound_manager.retrace(g)
    assert seen[1:] == [(sm.callBackCmd, 0), (sm.waitCmd, 10), (sm.requestNextCmd, 1)]
    assert not called
    assert g("SndDisposeChannel", chan, True) == 0
    assert seen[-1] == (sm.freeCmd, 0)


def double_header(g, buffers, proc: int, channels: int = 1, size: int = 8) -> int:
    return g.ptr(struct.pack(">hhhhIIII", channels, size, 0, 0, sm.rate22khz, *buffers, proc))


def double_buffer(g, samples: bytes, flags: int = sm.dbBufferReady) -> int:
    return g.ptr(struct.pack(">IIII", len(samples), flags, 0, 0) + samples)


def test_a_double_buffer_plays_its_buffers_in_turn(g):
    """Maelstrom mixes its own sound into two buffers: each, once played,
    goes back to its doubleBack to be filled again, till the last."""
    given = []

    def double_back(chan, buf):
        given.append(buf)
        n = len(given)
        if n < 3:
            g.mem.write(buf + 16, bytes([0x80 + n]) * 300)
            g.mem.w32(buf, 300)
            g.mem.w32(buf + 4, sm.dbBufferReady | (sm.dbLastBuffer if n == 2 else 0))

    g.procs[0x2000] = double_back
    chan = channel(g)
    first, second = double_buffer(g, bytes([0x10]) * 300), double_buffer(g, bytes([0x20]) * 300)
    assert g("SndPlayDoubleBuffer", chan, double_header(g, (first, second), 0x2000)) == 0
    assert tick(g) == bytes([0x10]) * 300 + bytes([0x20]) * 70
    assert given == [first]
    assert tick(g) == bytes([0x20]) * 230 + bytes([0x81]) * 140
    assert tick(g) == bytes([0x81]) * 160 + bytes([0x82]) * 210
    assert tick(g) == bytes([0x82]) * 90 + SILENT * 280
    assert given[:3] == [first, second, first]
    assert g.machine.sound_manager.channel(chan).voice is None
    assert not g.mem.r32(second + 4) & sm.dbBufferReady


def test_a_double_buffer_not_ready_yet_is_waited_for_in_silence(g):
    chan = channel(g)
    first, second = double_buffer(g, bytes([0x10]) * 300), double_buffer(g, bytes(300), 0)
    g.procs[0x2000] = lambda chan, buf: None
    assert g("SndPlayDoubleBuffer", chan, double_header(g, (first, second), 0x2000)) == 0
    assert tick(g) == bytes([0x10]) * 300 + SILENT * 70
    assert tick(g) == SILENT * FRAME
    g.mem.write(second + 16, bytes([0x30]) * 300)
    g.mem.w32(second + 4, sm.dbBufferReady | sm.dbLastBuffer)
    assert tick(g) == bytes([0x30]) * 300 + SILENT * 70
    assert tick(g) == SILENT * FRAME


def test_each_double_buffer_goes_back_as_it_s_played_however_many_a_tick(g):
    """Digital Messiah gives its double buffers no frames till its music
    starts, then plays them again with SndPlayDoubleBuffer: by then each
    buffer played has been back to its doubleBack, and is ready."""
    given = []

    def nothing_yet(chan, buf):
        given.append(buf)
        g.mem.w32(buf + 4, sm.dbBufferReady)

    def music(chan, buf):
        g.mem.write(buf + 16, bytes([0x90]) * 300)
        g.mem.w32(buf, 300)
        g.mem.w32(buf + 4, sm.dbBufferReady)

    g.procs[0x2000] = nothing_yet
    chan = channel(g)
    bufs = tuple(g.ptr(struct.pack(">IIII", 0, sm.dbBufferReady, 0, 0) + bytes(300))
                 for _ in range(2))
    head = double_header(g, bufs, 0x2000)
    assert g("SndPlayDoubleBuffer", chan, head) == 0
    assert tick(g) == SILENT * FRAME
    assert len(given) == sm.MAX_BUFFERS
    assert all(g.mem.r32(buf + 4) & sm.dbBufferReady for buf in bufs)
    g.procs[0x2000] = music
    assert g("SndPlayDoubleBuffer", chan, head) == 0
    assert tick(g) == bytes([0x90]) * FRAME


def test_a_double_buffer_it_can_t_play(g):
    chan = channel(g)
    bufs = (double_buffer(g, bytes(8)), double_buffer(g, bytes(8)))
    assert g("SndPlayDoubleBuffer", chan, double_header(g, bufs, 0, channels=3)) == sm.badFormat
    assert g("SndPlayDoubleBuffer", chan, double_header(g, bufs, 0, size=12)) == sm.badFormat
    assert g("SndPlayDoubleBuffer", chan, double_header(g, (0, 0), 0)) == -50   # paramErr
    assert g("SndPlayDoubleBuffer", 0x1234, double_header(g, bufs, 0)) == sm.badChannel


def test_the_version_and_what_is_available(g):
    assert g("SndSoundManagerVersion") == 0x02008000         # 2.0, final; System 7.0.1's
    cmd = g.ptr(command(sm.availableCmd))
    assert g("SndControl", sm.sampledSynth, cmd) == 0 and g.mem.r16(cmd + 2) == 1
    g.mem.write(cmd, command(sm.availableCmd, 0, sm.initStereo))
    assert g("SndControl", sm.sampledSynth, cmd) == 0 and g.mem.r16(cmd + 2) == 0
    g.mem.write(cmd, command(sm.availableCmd))
    assert g("SndControl", 2, cmd) == 0 and g.mem.r16(cmd + 2) == 0  # no synthesizer 2


@pytest.mark.parametrize("model, version", [("ii", 0x03008000), ("plus", 0x02008000)])
def test_system_7_5_s_sound_manager_is_3_0_with_a_sound_chip(qapp, model, version):
    g = Guest(model=model)
    g.system.version = 0x0753
    assert g("SndSoundManagerVersion") == version


def test_a_channel_s_status(g):
    chan = channel(g, sm.noteSynth)
    status = g.ptr(b"\xff" * 24)
    assert g("SndChannelStatus", chan, 24, status) == 0
    assert g.mem.read(status, 24) == bytes(24)
    do(g, chan, sm.noteCmd, 2000, 60)
    g.machine.sound_manager.retrace(g)
    g("SndChannelStatus", chan, 24, status)
    assert g.mem.r8(status + 12) == 1                        # scChannelBusy
    assert g("SndChannelStatus", 0, 24, status) == sm.badChannel


def test_the_manager_s_status_and_the_system_beep(g, monkeypatch):
    channel(g, sm.noteSynth)
    status = g.ptr(b"\xff" * 8)
    assert g("SndManagerStatus", 6, status) == 0
    assert g.mem.read(status, 8) == struct.pack(">hhh", 100, 1, 0) + b"\xff\xff"
    beeps = []
    monkeypatch.setattr(g.tb, "SysBeep", beeps.append)
    state = g.ptr(b"\xff\xff")
    g("SndGetSysBeepState", state)
    assert g.mem.r16(state) == 1                             # sysBeepEnable
    assert g("SndSetSysBeepState", 0) == 0
    g("SysBeep", 5)
    g("SndGetSysBeepState", state)
    assert g.mem.r16(state) == 0 and beeps == []
    assert g("SndSetSysBeepState", 2) == -50                 # paramErr
    assert g("SndSetSysBeepState", 1) == 0
    g("SysBeep", 5)
    assert beeps == [5]


@pytest.mark.parametrize("selector", [0x03140008, 0x00140008])
def test_sound_dispatch_finds_a_routine_with_or_without_its_arguments_size(qapp, selector):
    """Later interfaces put the size of the arguments in D0's high byte,
    older ones don't."""
    p = Processor()
    status = p.ptr(bytes(6))
    p.run(bytes.fromhex("4267" "3F3C0006" "4879") + struct.pack(">IHI", status, 0x203C, selector)
          + bytes.fromhex("A800" "3E1F"))                    # SndManagerStatus(6, status)
    assert p.cpu.d(7) & 0xFFFF == 0 and p.cpu.sp == p.stack_top and not p.missing
    assert p.mem.read(status, 6) == struct.pack(">hhh", 100, 0, 0)


def test_a_sound_manager_3_routine_macrt_hasnt_got_is_unimplemented(qapp):
    """Its selector says how many words of arguments to pop; the OSErr it
    returns says it isn't there (the Sound control panel's routine $50 of
    the Sound Input Manager)."""
    p = Processor()
    p.run(bytes.fromhex("3F3C1234" "4879") + struct.pack(">IHI", 0x5678, 0x203C, 0x02500014)
          + bytes.fromhex("A800" "3E1F"))
    assert p.cpu.d(7) & 0xFFFF == 0xFFFC and p.cpu.sp == p.stack_top     # unimpErr
    assert p.cpu.d(0) == 0xFFFFFFFC and p.missing


def test_the_error_sound_s_procedure_and_a_resource_that_isn_t_a_sound(g):
    g("ErrorSound", 0x4321)
    assert g.process.world.r32(0xA9C) == 0x4321
    assert g("SndPlay", 0, g.handle(b"snd"), False) == sm.badFormat
    assert g("SndPlay", 0, 0, False) == sm.resProblem
    assert g("SndPlay", 0x1234, g.handle(b"snd"), False) == sm.badChannel


# -- SndPlay, from 68k code ------------------------------------------------------
class Player(Processor):
    """A Processor whose ticks come, while the Sound Manager is busy, as
    the runtime's do, and when the ROM's loop waits for one."""

    def __init__(self, resources=()):
        super().__init__(resources)
        self.cpu.sound_waiting = self.machine.sound_manager.busy
        self.frames = []

    def run(self, code: bytes, ticks: int = 600) -> None:
        start = self.ptr(code)
        self.cpu.pc = start
        for _ in range(100_000):
            ev = self.cpu.run(start + len(code))
            if ev.kind == "trap":
                self.cpu.pc = ev.pc + 2
                dispatch.dispatch(self, ev.code, ev.pc)
            elif ev.kind in ("interrupted", "exception"):
                if ev.kind == "exception":      # the ROM's loop waits for the tick: here it is
                    assert ev.pc == self.machine.sound_wait + runtime.SOUND_WAIT_TICK, ev
                    self.cpu.pc = ev.pc + 2
                self.frames.append(tick(self))
                assert len(self.frames) < ticks
            else:
                break
        assert ev.kind == "until", ev


def snd_play(chan: int, handle: int, asynch: bool = False) -> bytes:
    """SndPlay(chan, handle, asynch); its result in D0."""
    return bytes.fromhex("554f"                          # subq.w #2,sp
                         "2f3c") + struct.pack(">I", chan) + bytes.fromhex(
                         "2f3c") + struct.pack(">I", handle) + bytes.fromhex(
                         "3f3c") + struct.pack(">H", 0x100 if asynch else 0) + bytes.fromhex(
                         "a805"                          # _SndPlay
                         "301f")                         # move.w (sp)+,d0


@pytest.fixture
def p(qapp):
    player = Player()
    yield player
    player.cpu.close()


def format_2(samples: bytes) -> bytes:
    return struct.pack(">hhh", 2, 0, 1) + command(sm.bufferCmd | sm.dataPointerFlag, 0, 14) \
        + header(samples)


def test_snd_play_waits_for_its_sound_on_a_channel_of_its_own(p):
    samples = bytes(i % 200 + 20 for i in range(1000))
    h = p.handle(format_2(samples))
    p.run(snd_play(0, h))
    assert p.cpu.d(0) & 0xFFFF == 0
    heard = b"".join(p.frames)
    assert heard[:1000] == samples and len(p.frames) == 3
    assert not p.machine.sound_manager.channels                 # freed once it's done
    assert not p.heap.get_state(h) & 0x80                       # and unlocked


def test_snd_play_on_a_channel_waits_only_if_it_s_synchronous(p):
    chan = channel(p, sm.sampledSynth)
    samples = bytes(i % 200 + 20 for i in range(2000))
    h = p.handle(format_2(samples))
    p.run(snd_play(chan, h, asynch=True))
    assert not p.frames                                         # queued, and on it went
    assert p.heap.get_state(h) & 0x80                           # locked, while it plays
    p.run(snd_play(chan, h))
    assert len(p.frames) == 11                                  # both, one after the other
    assert b"".join(p.frames)[:4000] == samples * 2
    assert not p.heap.get_state(h) & 0x80                       # played: as it was
    assert p.machine.sound_manager.channel(chan) is not None    # the program's, still


def test_dispose_channel_waits_for_what_s_queued(p):
    chan = channel(p, sm.noteSynth)
    do(p, chan, sm.noteCmd, 200, 60)                            # 2225 samples: 7 ticks
    code = bytes.fromhex("554f" "2f3c") + struct.pack(">I", chan) + bytes.fromhex(
        "4267"                                                  # clr.w -(sp): quietNow FALSE
        "a801" "301f")                                          # _SndDisposeChannel
    p.run(code)
    assert len(p.frames) == 7 and not p.machine.sound_manager.channels


def test_sound_preferences_are_kept(g):
    """What a sound component saves with SetSoundPreference, by type and
    name, GetSoundPreference gives back in the handle it's passed."""
    kind = struct.unpack(">I", b"ssnd")[0]
    name, other = g.ptr(b"\x07Speaker"), g.ptr(b"\x05Other")
    settings = g.handle(b"\0" * 2)
    assert g("GetSoundPreference", kind, name, settings) == -192    # resNotFound
    assert g("SetSoundPreference", kind, name, g.handle(b"volume 7")) == 0
    assert g("GetSoundPreference", kind, name, settings) == 0
    assert g.tb.contents(settings) == b"volume 7"
    assert g("GetSoundPreference", kind, other, settings) == -192
    assert g("GetSoundPreference", kind + 1, name, settings) == -192
    assert g("SetSoundPreference", kind, name, 0) == -109           # nilHandleErr
    assert g("GetSoundPreference", kind, name, 0) == -109


def test_the_volumes_are_kept(g):
    level = g.ptr(bytes(4))
    for get, put in (("GetSysBeepVolume", "SetSysBeepVolume"),
                     ("GetDefaultOutputVolume", "SetDefaultOutputVolume")):
        assert g(get, level) == 0 and g.mem.r32(level) == 0x01000100      # kFullVolume
        assert g(put, 0x00800040) == 0
        assert g(get, level) == 0 and g.mem.r32(level) == 0x00800040


def test_there_is_no_sound_input_device(g):
    """Neither a Mac Plus nor a Mac II has one to record from."""
    name, icon, ref = g.ptr(bytes(256)), g.ptr(bytes(4)), g.ptr(bytes(4))
    assert g("SPBVersion") == 0x02008000
    assert g("SPBGetIndexedDevice", 1, name, icon) == -221          # siBadSoundInDevice
    assert g("SPBOpenDevice", g.ptr(b"\x00"), 1, ref) == -221
    assert g("SndRecord", 0, 0, 0, g.ptr(bytes(4))) == -220         # siNoSoundInHardware
    assert g("SndRecordToFile", 0, 0, 0, 1) == -220
    assert g("SPBCloseDevice", 1) == -229                           # siBadRefNum
    spb, long = g.ptr(bytes(42)), g.ptr(bytes(4))
    assert g("SPBRecord", spb, False) == -221
    assert g("SPBRecordToFile", 1, spb, True) == -221
    for routine in ("SPBPauseRecording", "SPBResumeRecording", "SPBStopRecording"):
        assert g(routine, 1) == -221
    assert g("SPBGetRecordingStatus", 1, *[long] * 6) == -221
    assert g("SPBGetDeviceInfo", 1, 0x636F6E74, long) == -221      # 'cont'
    assert g("SPBSetDeviceInfo", 1, 0x636F6E74, long) == -221
    assert g("SPBMillisecondsToBytes", 1, long) == -221
    assert g("SPBBytesToMilliseconds", 1, long) == -221
    assert g("SPBSignInDevice", -40, g.ptr(b"\x04Mike")) == -221
    assert g("SPBSignOutDevice", -40) == -221


def code(kind: bytes) -> int:
    return struct.unpack(">I", kind)[0]


def test_setup_snd_header_for_one_channel_of_8_bit_samples(g):
    """A format 1 'snd ': the sampled synthesizer, a bufferCmd for the
    header after it, and a standard header, the samples to go after it;
    the handle isn't resized."""
    snd, length = g.handle(bytes(200)), g.ptr(bytes(2))
    assert g("SetupSndHeader", snd, 1, sm.rate22khz, 8, code(b"NONE"), 60, 1000, length) == 0
    assert g.mem.r16(length) == 42 and len(g.tb.contents(snd)) == 200
    data = g.tb.contents(snd)
    assert data[:20] == struct.pack(">hhhIh", 1, 1, sm.sampledSynth, 0, 1) + command(
        sm.dataPointerFlag | sm.bufferCmd, 0, 20)
    assert data[20:42] == header(b"", base=60)[:4] + struct.pack(">I", 1000) + header(
        b"", base=60)[8:]
    assert sm.parse_snd(data)[2] == [(sm.dataPointerFlag | sm.bufferCmd, 0, 20)]
    out = g.ptr(bytes(4))
    assert g("GetSoundHeaderOffset", snd, out) == 0 and g.mem.r32(out) == 20


def test_setup_snd_header_extended_and_compressed(g):
    """Extended for 16-bit or stereo samples, its frames the bytes over
    each frame's; compressed for MACE, its frames the packets."""
    snd, length = g.handle(bytes(100)), g.ptr(bytes(2))
    rate = 44100 << 16
    assert g("SetupSndHeader", snd, 2, rate, 16, code(b"NONE"), 60, 4000, length) == 0
    assert g.mem.r16(length) == 84
    h = g.tb.contents(snd)[20:84]
    assert struct.unpack(">IiI", h[:12]) == (0, 2, rate) and h[20] == sm.extSH and h[21] == 60
    assert struct.unpack(">I", h[22:26])[0] == 1000                         # frames
    assert h[26:36] == extended(44100) and struct.unpack(">h", h[48:50])[0] == 16
    assert sm.read_header(g.mem, g.mem.r32(snd) + 20)[1] == rate
    assert g("SetupSndHeader", snd, 1, sm.rate22khz, 8, code(b"MAC3"), 60, 300, length) == 0
    h = g.tb.contents(snd)[20:84]
    assert h[20] == 0xFE and struct.unpack(">I", h[22:26])[0] == 150         # cmpSH, 2-byte packets
    assert h[40:44] == b"MAC3" and struct.unpack(">4h", h[56:64]) == (3, 16, 0, 8)
    assert g("SetupSndHeader", snd, 1, sm.rate22khz, 8, code(b"MAC6"), 60, 300, length) == 0
    assert struct.unpack(">I", g.tb.contents(snd)[42:46])[0] == 300
    assert g("SetupSndHeader", snd, 1, sm.rate22khz, 8, code(b"ABCD"), 60, 0, length) == \
        sm.siInvalidCompression
    small = g.handle(bytes(50))
    assert g("SetupSndHeader", small, 2, rate, 16, code(b"NONE"), 60, 0, length) == -50
    assert g.tb.contents(small) == bytes(50)


def test_get_sound_header_offset_of_a_format_2_resource(g):
    out = g.ptr(bytes(4))
    assert g("GetSoundHeaderOffset", g.handle(format_2(b"\x80" * 10)), out) == 0
    assert g.mem.r32(out) == 14
    assert g("GetSoundHeaderOffset", g.handle(struct.pack(">hh", 2, 0) + bytes(4)), out) == \
        sm.badFormat
    assert g("GetSoundHeaderOffset", g.handle(b"\x00\x07"), out) == sm.badFormat


def test_unsigned_fixed_mul_div(g):
    """The interfaces' value, multiplier and divisor: a rate scaled."""
    assert g("UnsignedFixedMulDiv", 22254 << 16, 3 << 16, 2 << 16) == 33381 << 16
    assert g("UnsignedFixedMulDiv", 0xFFFF0000, 2 << 16, 1 << 16) == 0xFFFFFFFF
    assert g("UnsignedFixedMulDiv", 1 << 16, 1 << 16, 0) == 0xFFFFFFFF


@pytest.mark.parametrize("routine", ["Exp1to3", "Exp1to6"])
def test_mace_sound_expands_to_silence(g, routine):
    """As many samples as the packets expand to, 6 each, of one channel:
    how MACE compresses isn't published."""
    out = g.ptr(bytes(20))
    g(routine, g.ptr(bytes(8)), out, 3, 0, 0, 2, 1)
    assert g.mem.read(out, 20) == SILENT * 18 + bytes(2)


# -- play from disk ----------------------------------------------------------------
def extended(x: float) -> bytes:
    """x as an 80-bit SANE extended, as AIFF's sample rate is."""
    m, e = math.frexp(x)
    return struct.pack(">HQ", e - 1 + 16383, int(m * 2 ** 64))


def chunk(kind: bytes, data: bytes) -> bytes:
    return kind + struct.pack(">I", len(data)) + data + bytes(len(data) & 1)


def aiff(samples: bytes, channels: int = 1, bits: int = 8, compression: bytes | None = None,
         frames: int | None = None, before: bytes = b"") -> bytes:
    """An AIFF file, or with compression an AIFF-C one, of two's
    complement samples; before goes ahead of its chunks."""
    if frames is None:
        frames = len(samples) // (channels * ((bits + 7) // 8))
    comm = struct.pack(">hIh", channels, frames, bits) + extended(sm.MAC_RATE)
    if compression is not None:
        comm += compression + bytes(2)                  # its name: none
    body = (b"AIFC" if compression is not None else b"AIFF") + before \
        + chunk(b"COMM", comm) + chunk(b"SSND", bytes(8) + samples)
    return b"FORM" + struct.pack(">I", len(body)) + body


def test_an_aiff_file_s_samples():
    """Two's complement samples, played as offset binary ones, their high
    bytes for 16-bit ones and channels averaged; chunks in any order, an
    odd one padded."""
    data, rate = sm.parse_aiff(aiff(bytes([0x00, 0x7F, 0x80, 0xFF])))
    assert data == bytes([0x80, 0xFF, 0x00, 0x7F]) and rate == pytest.approx(sm.MAC_RATE)
    stereo = aiff(bytes.fromhex("12345678" "80000000"), channels=2, bits=16)
    assert sm.parse_aiff(stereo)[0] == bytes([(0x92 + 0xD6) // 2, (0x00 + 0x80) // 2])
    assert sm.parse_aiff(aiff(b"\x10\x20", compression=b"NONE"))[0] == b"\x90\xa0"
    swapped = aiff(b"\x10\x20", before=chunk(b"NAME", b"odd")).replace(
        b"AIFF", b"AIFF" + chunk(b"SSND", bytes(8) + b"\x30"), 1)
    assert sm.parse_aiff(swapped)[0] == b"\xb0"       # the first SSND, after an odd NAME
    assert sm.parse_aiff(aiff(bytes(10), compression=b"MAC3", frames=30)) == (
        SILENT * 30, pytest.approx(sm.MAC_RATE))
    assert sm.parse_aiff(aiff(bytes(10), compression=b"ABCD")) == sm.siInvalidCompression
    assert sm.parse_aiff(b"RIFF" + bytes(20)) == sm.badFileFormat
    assert sm.parse_aiff(b"FORM\x00\x00\x00\x04AIFF") == sm.badFileFormat     # no COMM
    rate = extended(sm.MAC_RATE)
    for bad in (struct.pack(">HQ", 0x7FFF, 1 << 63), struct.pack(">HQ", 0x7FFF, 3 << 62)):
        assert sm.parse_aiff(aiff(bytes(10)).replace(rate, bad)) == sm.badFileFormat  # ∞, NaN


def open_file(g, data: bytes) -> int:
    files = FileManager(g.system.files, None, g.system.files.dir_for(
        MemoryFolder([MacFile("Sound", data=data, type=b"AIFF", creator=b"TEST")])))
    return files.open(0, 0, "Sound", False, fm.fsRdPerm).refnum


def file_play(g, chan: int, refnum: int, res: int = 0, selection: int = 0,
              completion: int = 0, asynch: bool = True) -> int:
    return g("SndStartFilePlay", chan, refnum, res, 0, 0, selection, completion, asynch)


def test_setup_aiff_header_writes_at_the_mark(g):
    """FORM, COMM and SSND, and FVER for AIFF-C, at the mark, which is
    left after them, the sound to go after: SndStartFilePlay plays it."""
    files = FileManager(g.system.files, None, g.system.files.dir_for(
        MemoryFolder([MacFile("Rec", data=b"", type=b"AIFF", creator=b"TEST")])))
    refnum = files.open(0, 0, "Rec", False, fm.fsRdWrPerm).refnum
    assert g("SetupAIFFHeader", refnum, 1, sm.rate22khz, 8, code(b"NONE"), 0, 0) == 0
    first = files.get_fpos(refnum)
    files.set_fpos(refnum, fm.fsFromStart, 0)
    assert g("SetupAIFFHeader", refnum, 1, sm.rate22khz, 8, code(b"NONE"), 4, 0) == 0
    assert files.get_fpos(refnum) == first == 54
    files.write(refnum, bytes([0x00, 0x7F, 0x80, 0xFF]))
    data = bytes(g.system.files.fcb(refnum).fork)
    assert data[:12] == b"FORM" + struct.pack(">I", 50) + b"AIFF"
    assert data[12:38] == chunk(b"COMM", struct.pack(">hIh", 1, 4, 8) + extended(sm.MAC_RATE))
    assert sm.parse_aiff(data) == (bytes([0x80, 0xFF, 0x00, 0x7F]), pytest.approx(sm.MAC_RATE))
    chan = channel(g)
    assert file_play(g, chan, refnum) == 0
    files.set_fpos(refnum, fm.fsFromStart, 0)
    assert g("SetupAIFFHeader", refnum, 2, sm.rate22khz, 8, code(b"MAC6"), 9, 0) == 0
    data = bytes(g.system.files.fcb(refnum).fork)
    assert data[8:24] == b"AIFCFVER" + struct.pack(">II", 4, 0xA2805140)
    assert data[24:32] == b"COMM" + struct.pack(">I", 34)
    assert struct.unpack(">hIh", data[32:40]) == (2, 4, 8)                  # 9 bytes: 4 frames
    assert data[50:66] == b"MAC6\x0bMACE 6-to-1"
    assert struct.unpack(">I", data[4:8])[0] == 4 + 12 + 42 + 16 + 10      # with the pad byte
    assert g("SetupAIFFHeader", refnum, 1, sm.rate22khz, 8, code(b"ABCD"), 0, 9) == \
        sm.siInvalidCompression


def test_a_file_plays_on_a_channel_then_its_completion_is_called(g):
    done = []
    g.procs[0x2000] = done.append
    chan = channel(g)
    samples = bytes(i % 100 for i in range(500))
    assert file_play(g, chan, open_file(g, aiff(samples)), completion=0x2000) == 0
    status = g.ptr(bytes(24))
    assert tick(g) == samples[:FRAME].translate(sm._FLIP) and not done
    g("SndChannelStatus", chan, 24, status)
    start, end, now, busy, _, paused = struct.unpack(">3IBBB", g.mem.read(status, 15))
    assert (start, end, now) == (0, round(500 / sm.MAC_RATE * 0x10000),
                                 round(FRAME / sm.MAC_RATE * 0x10000))
    assert busy and not paused
    assert tick(g) == samples[FRAME:].translate(sm._FLIP) + SILENT * (2 * FRAME - 500)
    assert done == [chan]
    g("SndChannelStatus", chan, 24, status)
    assert g.mem.read(status, 24) == bytes(24)


def test_a_file_play_paused_and_resumed(g):
    chan = channel(g)
    assert g("SndPauseFilePlay", chan) == sm.channelNotBusy
    samples = bytes(range(0x10, 0x10 + 200)) * 3
    assert file_play(g, chan, open_file(g, aiff(samples))) == 0
    tick(g)
    assert g("SndPauseFilePlay", chan) == 0
    assert tick(g) == SILENT * FRAME and tick(g) == SILENT * FRAME
    status = g.ptr(bytes(24))
    g("SndChannelStatus", chan, 24, status)
    assert g.mem.r8(status + 12) == 1 and g.mem.r8(status + 14) == 1     # busy, and paused
    assert g("SndPauseFilePlay", chan) == 0
    assert tick(g) == samples[FRAME:].translate(sm._FLIP) + SILENT * (2 * FRAME - 600)


def test_a_file_play_stopped_or_disposed_of_calls_its_completion(g):
    done = []
    g.procs[0x2000] = done.append
    chan = channel(g)
    refnum = open_file(g, aiff(bytes(2000)))
    assert g("SndStopFilePlay", chan, True) == 0                # nothing to stop
    assert file_play(g, chan, refnum, completion=0x2000) == 0
    assert file_play(g, chan, refnum, completion=0x2000) == sm.channelBusy
    tick(g)
    assert g("SndStopFilePlay", chan, True) == 0 and done == [chan]
    assert tick(g) == SILENT * FRAME
    assert file_play(g, chan, refnum, completion=0x2000) == 0
    assert g("SndDisposeChannel", chan, True) == 0 and done == [chan, chan]
    assert not g.machine.sound_manager.channels


def test_a_file_play_s_selection_and_what_it_can_t_play(g):
    chan = channel(g)
    samples = bytes(range(250))
    refnum = open_file(g, aiff(samples))
    seconds = [round(n / sm.MAC_RATE * 0x10000) for n in (100, 200)]
    assert file_play(g, chan, refnum, selection=g.ptr(struct.pack(">I2i", 0, *seconds))) == 0
    assert tick(g) == samples[100:200].translate(sm._FLIP) + SILENT * (FRAME - 100)
    backwards = g.ptr(struct.pack(">I2i", 0, *reversed(seconds)))
    assert file_play(g, chan, refnum, selection=backwards) == -50   # paramErr
    everything = g.ptr(struct.pack(">I2i", 0xFFFF, *reversed(seconds)))    # unitTypeNoSelection
    assert file_play(g, chan, refnum, selection=everything) == 0
    assert tick(g) == samples.translate(sm._FLIP) + SILENT * (FRAME - 250)
    assert file_play(g, 0, refnum) == sm.badChannel             # its own channel: synchronous
    assert file_play(g, 0x1234, refnum) == sm.badChannel
    assert file_play(g, chan, 0x7FF0) == -51                    # rfNumErr
    assert g("SndStopFilePlay", chan, True) == 0
    assert file_play(g, chan, open_file(g, b"not a sound")) == sm.badFileFormat


def test_a_snd_resource_plays_from_disk(qapp):
    """Read from its file, with no handle made for it in the heap; or if
    the program has it in memory, from its handle."""
    samples = bytes(i % 200 + 20 for i in range(300))
    g = Guest([Resource(b"snd ", 128, format_2(samples))])
    chan = channel(g)
    free = g.heap.free_bytes()
    assert file_play(g, chan, 0, 128) == 0
    assert g.heap.free_bytes() == free
    assert tick(g) == samples + SILENT * (FRAME - 300)
    assert file_play(g, chan, 0, 129) == sm.resProblem
    h = g("GetResource", code(b"snd "), 128)
    g.mem.write(g.mem.r32(h) + 14 + 22, bytes(range(40, 140)))
    assert file_play(g, chan, 0, 128) == 0
    assert tick(g) == bytes(range(40, 140)) + samples[100:] + SILENT * (FRAME - 300)


def test_a_file_play_with_no_channel_waits_for_its_end(p):
    """Its own channel, in the application's heap, freed once it's done."""
    samples = bytes(i % 100 for i in range(1000))
    refnum = open_file(p, aiff(samples))
    p.run(bytes.fromhex("554f" "42a7"                          # its result; chan NIL
                        "3f3c") + struct.pack(">H", refnum & 0xFFFF) + bytes.fromhex(
                        "4267" "42a7" "42a7" "42a7" "42a7"     # resNum, buffer, selection...
                        "4267"                                 # async FALSE
                        "203c0d000008" "a800"                  # _SoundDispatch: SndStartFilePlay
                        "301f"))
    assert p.cpu.d(0) & 0xFFFF == 0
    assert b"".join(p.frames)[:1000] == samples.translate(sm._FLIP) and len(p.frames) == 3
    assert not p.machine.sound_manager.channels
