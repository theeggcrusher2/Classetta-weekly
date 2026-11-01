"""
The Sound Manager's traps (Inside Macintosh V-473): the channels and
their synthesizers are macrt.m68k.sound_manager's, which plays them.
SysBeep is the Toolbox's, ErrorSound the Dialog Manager's.  Sound
compressed with MACE expands to silence.

The Sound Input Manager's (Inside Macintosh: Sound) find no device to
record from, as on a Mac Plus or a Mac II, which have none, and no driver
signs in as one; its routines that build an 'snd ' resource's or an AIFF
file's header for sound to go after work.
"""

from __future__ import annotations

import struct

from ..memory import ADDRESS_MASK
from ..toolbox.files import FileError, paramErr
from ..toolbox.memmgr import memFullErr, nilHandleErr, noErr
from ..toolbox.resources import resNotFound
from . import sound_manager as sm
from .dispatch import trap
from .sound import SILENCE, gestaltHasASC

badChannel = sm.badChannel
unitTypeSeconds = 0
SCSTATUS_SIZE = 24
SMSTATUS_SIZE = 6
sysBeepDisable, sysBeepEnable = 0, 1
siNoSoundInHardware, siBadSoundInDevice, siBadRefNum = -220, -221, -229
# NumVersions: 2.0, the Sound Manager of System 6.0.7 and System 7; and
# 3.0, which every System 7.5 has (Apple's Technical Q&A QTPC 03) where
# there is an Apple Sound Chip, not on a Mac Plus (Inside Macintosh: Sound)
VERSION, VERSION_3 = 0x02008000, 0x03008000
SYSTEM_7_5 = 0x0750


def _command(t, cmd: int) -> sm.Command:
    return struct.unpack(">HHI", t.mem.read(cmd & ADDRESS_MASK, 8))


@trap
def SndSoundManagerVersion(t):
    system = t.system
    if system.version >= SYSTEM_7_5 and system.model.hardware >> gestaltHasASC & 1:
        return VERSION_3
    return VERSION


@trap
def SndNewChannel(t, chan_ptr, synth, init, user_routine):
    """A channel record, unless the program passed one of its own, which
    has to say how long its queue is.  Past macrt's MAX_CHANNELS open at
    once there are no more: DeadEnd counts how many it can have by asking
    for one more each time until it's refused."""
    if len(t.machine.sound_manager.channels) >= sm.MAX_CHANNELS:
        return sm.notEnoughHardware
    chan = t.mem.r32(chan_ptr) & ADDRESS_MASK
    owned = not chan
    if owned:
        chan = t.process.heap.new_ptr(sm.CHANNEL_SIZE, clear=True)
        if not chan:
            return memFullErr
        t.mem.w16(chan + sm.QLENGTH, sm.STD_QLENGTH)
        t.w32(chan_ptr, chan)
    elif t.mem.r16s(chan + sm.QLENGTH) <= 0:
        return badChannel
    t.mem.w32(chan + sm.CALLBACK, user_routine)
    t.machine.sound_manager.new_channel(t, chan, synth, owned)
    return noErr


@trap
def SndAddModifier(t, chan, modifier, id, init):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.add_modifier(t, ch, modifier & ADDRESS_MASK, init)
    return noErr


@trap
def SndDoCommand(t, chan, cmd, no_wait):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    return noErr if ch.put(t.mem, _command(t, cmd), wait=not no_wait) else sm.queueFull


@trap
def SndDoImmediate(t, chan, cmd):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.do_immediate(t, ch, _command(t, cmd))
    return noErr


@trap
def SndPlay(t, chan, snd, asynch):
    manager = t.machine.sound_manager
    ch = None
    if chan:
        ch = manager.channel(chan)
        if ch is None:
            return badChannel
    if not snd:
        return sm.resProblem
    return manager.play(t, ch, snd & ADDRESS_MASK, bool(asynch))


@trap
def SndStartFilePlay(t, chan, f_ref_num, res_num, buffer_size, the_buffer, selection,
                     completion, asynch):
    """Play from disk: an AIFF or AIFF-C file the program has open, or
    the sampled sound of a 'snd ' resource, read at once rather than
    through the buffer the program may give.  Given no channel, the play
    is synchronous, or badChannel."""
    manager = t.machine.sound_manager
    ch = None
    if chan:
        ch = manager.channel(chan)
        if ch is None:
            return badChannel
    elif asynch:
        return badChannel
    seconds = None
    if selection:
        unit, sel_start, sel_end = struct.unpack(">Iii", t.mem.read(selection & ADDRESS_MASK, 12))
        if unit == unitTypeSeconds:
            if sel_start > sel_end:
                return paramErr
            seconds = (sel_start / 0x10000, sel_end / 0x10000)
    if f_ref_num:
        try:
            sound = sm.parse_aiff(t.system.files.fcb(f_ref_num).fork)
        except FileError as e:
            return e.code
    else:
        sound = _resource_sound(t, res_num)
    if isinstance(sound, int):
        return sound
    data, rate = sound
    return manager.play_file(t, ch, data, rate, seconds, completion & ADDRESS_MASK, bool(asynch))


def _resource_sound(t, res_num: int) -> tuple[bytes, float] | int:
    """The sampled sound of the 'snd ' resource res_num, and its samples a
    second; silent if it's compressed.  It's read from its file, or its
    handle if it's in memory, with no handle made for it: a play from disk
    leaves the heap as it was.  OutNumbered! CD plays a 926K one in a
    heap of about 2M, and needs the room for the 'ASEQ' resources it
    loads next."""
    data = t.process.resources.peek("snd ", res_num)
    if data is None:
        return sm.resProblem
    parsed = sm.parse_snd(data)
    if parsed is None:
        return sm.badFormat
    for cmd, _, p2 in parsed[2]:
        if cmd & ~sm.dataPointerFlag in (sm.bufferCmd, sm.soundCmd):
            sound = sm.read_header(sm.InData(data), p2) if cmd & sm.dataPointerFlag else \
                sm.read_header(t.mem, p2)
            if sound is None:
                return b"", sm.MAC_RATE
            if not sound[1]:
                return sm.badFormat
            return sound[0], sound[1] / 0x10000
    return sm.badFormat


@trap
def SndPauseFilePlay(t, chan):
    """The play from disk paused, or if it is, resumed."""
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    return t.machine.sound_manager.pause_file(ch)


@trap
def SndStopFilePlay(t, chan, quiet_now):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.stop_file(t, ch, bool(quiet_now))
    return noErr


@trap
def SndPlayDoubleBuffer(t, chan, params):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    return t.machine.sound_manager.play_double(ch, params & ADDRESS_MASK)


@trap
def SndControl(t, synth, cmd):
    """availableCmd: whether a synthesizer can do what init asks, which
    is anything but stereo; versionCmd: its version."""
    c, p1, p2 = _command(t, cmd)
    if c == sm.availableCmd:
        p1 = int(synth in sm.SYNTHS and p2 & sm.initStereo != sm.initStereo)
    elif c == sm.versionCmd:
        p2 = sm.SYNTH_VERSION
    t.mem.write(cmd & ADDRESS_MASK, struct.pack(">HHI", c, p1, p2))
    return noErr


@trap
def SndChannelStatus(t, chan, length, status):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.mem.write(status, t.machine.sound_manager.status(ch)[:min(max(length, 0), SCSTATUS_SIZE)])
    return noErr


@trap
def SndManagerStatus(t, length, status):
    """An SMStatus: the most load it takes, its channels and the load
    they make, which macrt doesn't measure."""
    smstatus = struct.pack(">hhh", 100, len(t.machine.sound_manager.channels), 0)
    t.mem.write(status, smstatus[:min(max(length, 0), SMSTATUS_SIZE)])
    return noErr


@trap
def SndGetSysBeepState(t, state):
    t.w16(state, int(t.machine.sound_manager.sys_beep))


@trap
def SndSetSysBeepState(t, state):
    if state not in (sysBeepDisable, sysBeepEnable):
        return paramErr
    t.machine.sound_manager.sys_beep = state == sysBeepEnable
    return noErr


# The system beep's volume and the default for sound out, each a long:
# the right side's in the high word, the left's in the low, kFullVolume
# ($0100) at most (Inside Macintosh: Sound, the Sound Manager).  They're
# kept for whoever asks; what's played isn't made softer by them.
@trap
def GetSysBeepVolume(t, level):
    t.w32(level, t.machine.sound_manager.volumes["beep"])
    return noErr


@trap
def SetSysBeepVolume(t, level):
    t.machine.sound_manager.volumes["beep"] = level & 0xFFFFFFFF
    return noErr


@trap
def GetDefaultOutputVolume(t, level):
    t.w32(level, t.machine.sound_manager.volumes["output"])
    return noErr


@trap
def SetDefaultOutputVolume(t, level):
    t.machine.sound_manager.volumes["output"] = level & 0xFFFFFFFF
    return noErr


@trap
def SndDisposeChannel(t, chan, quiet_now):
    ch = t.machine.sound_manager.channel(chan)
    if ch is None:
        return badChannel
    t.machine.sound_manager.dispose(t, ch, bool(quiet_now))
    return noErr


def _settings_heap(t, settings: int):
    """The heap of a handle a program passed, or None."""
    heap = t.system.find_heap(settings)
    return heap if heap is not None and heap.is_handle(settings & ADDRESS_MASK) else None


@trap
def SetSoundPreference(t, kind, name, settings):
    """A sound component's settings, by resource type and name, for
    GetSoundPreference to give back (Inside Macintosh: Sound, the Sound
    Manager).  macrt keeps them while it runs, not in a file in the
    Preferences folder."""
    heap = _settings_heap(t, settings)
    if heap is None or not heap.deref(settings):
        return nilHandleErr
    t.machine.sound_manager.preferences[kind & 0xFFFFFFFF, t.pstr(name)] = \
        t.mem.read(heap.deref(settings), heap.handle_size(settings))
    return noErr


@trap
def GetSoundPreference(t, kind, name, settings):
    """The settings saved with that type and name, in the handle the
    caller passes, resized to fit; without any, the Resource Manager's
    error for a resource that isn't there."""
    data = t.machine.sound_manager.preferences.get((kind & 0xFFFFFFFF, t.pstr(name)))
    if data is None:
        return resNotFound
    heap = _settings_heap(t, settings)
    if heap is None:
        return nilHandleErr
    if not heap.set_handle_size(settings, len(data)):
        return memFullErr
    t.mem.write(heap.deref(settings), data)
    return noErr


@trap
def GetSoundHeaderOffset(t, snd, offset):
    """Where in an 'snd ' resource its sound header is: the one its first
    soundCmd or bufferCmd with dataPointerFlag points to (Inside
    Macintosh: Sound, the Sound Manager)."""
    heap = _settings_heap(t, snd)
    parsed = sm.parse_snd(t.tb.contents(snd)) if heap is not None and heap.deref(snd) else None
    for cmd, _, p2 in parsed[2] if parsed is not None else ():
        if cmd in (sm.dataPointerFlag | sm.soundCmd, sm.dataPointerFlag | sm.bufferCmd):
            t.mem.w32(offset & ADDRESS_MASK, p2)
            return noErr
    return sm.badFormat


@trap
def UnsignedFixedMulDiv(t, value, multiplier, divisor):
    """value times multiplier, divided by divisor, all unsigned Fixed
    numbers, as the interfaces name them: for the Fixed scale cancels, the
    product's bits over the divisor's.  macrt's own: a result too big, or
    dividing by 0, is the biggest there is."""
    if not divisor:
        return 0xFFFFFFFF
    return min(value * multiplier // divisor, 0xFFFFFFFF)


# =========================================================================
# MACE: sound compressed 3:1 and 6:1
# =========================================================================
def _expand(t, out: int, cnt: int) -> None:
    """Silence, as much as cnt packets expand to: 6 samples each, of the
    one channel asked for (Inside Macintosh: Sound, the Sound Manager).
    How MACE compresses sound isn't published, so macrt can't expand it."""
    cnt &= 0xFFFFFFFF
    if 0 < cnt and cnt * 6 <= ADDRESS_MASK + 1:
        t.mem.write(out & ADDRESS_MASK, bytes([SILENCE]) * (cnt * 6))


@trap
def Exp1to3(t, in_buffer, out_buffer, cnt, in_state, out_state, channels, which):
    _expand(t, out_buffer, cnt)


@trap
def Exp1to6(t, in_buffer, out_buffer, cnt, in_state, out_state, channels, which):
    _expand(t, out_buffer, cnt)


# =========================================================================
# The Sound Input Manager
# =========================================================================
@trap
def SPBVersion(t):
    return VERSION


@trap
def SPBGetIndexedDevice(t, count, name, icon):
    return siBadSoundInDevice


@trap
def SPBOpenDevice(t, name, permission, ref):
    return siBadSoundInDevice


@trap
def SPBCloseDevice(t, ref):
    return siBadRefNum


def _no_device(name: str):
    """One of the routines that act on an open device, or sign one in or
    out: there's none."""
    def impl(t, *args) -> int:
        return siBadSoundInDevice
    impl.__name__ = name
    return impl


for _name in ("SPBRecord", "SPBRecordToFile", "SPBPauseRecording", "SPBResumeRecording",
              "SPBStopRecording", "SPBGetRecordingStatus", "SPBGetDeviceInfo",
              "SPBSetDeviceInfo", "SPBMillisecondsToBytes", "SPBBytesToMilliseconds",
              "SPBSignInDevice", "SPBSignOutDevice"):
    trap(_no_device(_name))


@trap
def SndRecord(t, filter_proc, corner, quality, snd):
    return siNoSoundInHardware


@trap
def SndRecordToFile(t, filter_proc, corner, quality, refnum):
    return siNoSoundInHardware


# Building headers for recorded sound (Inside Macintosh: Sound, the Sound
# Input Manager): the compressions macrt knows, each packet's bytes
MACE_PACKETS = {b"MAC3": 2, b"MAC6": 1}
MACE_IDS = {b"MAC3": 3, b"MAC6": 4}                 # threeToOne, sixToOne
MACE_NAMES = {b"MAC3": b"MACE 3-to-1", b"MAC6": b"MACE 6-to-1"}
stdSH, cmpSH = 0x00, 0xFE
SND_PREFIX = 20                                     # a format 1 'snd ' up to its header
AIFCVersion1 = 0xA2805140


def _frames(channels: int, size: int, kind: bytes, length: int) -> int | None:
    """How many sample frames length bytes of sound make, each a sample
    or, compressed, a packet per channel; None for a compression macrt
    doesn't know (it has only MACE's)."""
    if kind == b"NONE":
        each = max((size + 7) // 8, 1)
    elif kind in MACE_PACKETS:
        each = MACE_PACKETS[kind]
    else:
        return None
    return length // (each * max(channels, 1))


def _extended(rate: int) -> bytes:
    """An unsigned Fixed sample rate as an 80-bit extended, exactly."""
    if not rate:
        return bytes(10)
    n = rate.bit_length()
    return struct.pack(">HQ", 16383 + n - 17, rate << (64 - n))


@trap
def SetupSndHeader(t, snd, channels, rate, size, compression, base, length, header_len):
    """A format 1 'snd ' resource's start, in the handle, for the sound to
    go after: the sampled synthesizer, a bufferCmd and a sound header,
    standard for one channel of 8-bit samples, extended for others, and
    compressed for MACE.  macrt's own: paramErr for no handle or one too
    small for the header."""
    kind = struct.pack(">I", compression & 0xFFFFFFFF)
    rate &= 0xFFFFFFFF
    length &= 0xFFFFFFFF
    frames = _frames(channels, size, kind, length)
    if frames is None:
        return sm.siInvalidCompression
    data = struct.pack(">hhhIhHHI", 1, 1, sm.sampledSynth, 0, 1,
                       sm.dataPointerFlag | sm.bufferCmd, 0, SND_PREFIX)
    common = struct.pack(">IiIIIBB", 0, channels, rate, 0, 0, 0, base & 0xFF)
    if kind == b"NONE" and channels == 1 and size == 8:
        data += struct.pack(">IIIIIBB", 0, length, rate, 0, 0, stdSH, base & 0xFF)
    elif kind == b"NONE":
        data += common[:20] + bytes([sm.extSH]) + common[21:] + struct.pack(
            ">I10sIIIhhIII", frames, _extended(rate), 0, 0, 0, size, 0, 0, 0, 0)
    else:
        data += common[:20] + bytes([cmpSH]) + common[21:] + struct.pack(
            ">I10sI4sIIIhhhh", frames, _extended(rate), 0, kind, 0, 0, 0,
            MACE_IDS[kind], 8 * MACE_PACKETS[kind], 0, size)
    heap = _settings_heap(t, snd)
    if heap is None or not heap.deref(snd) or heap.handle_size(snd) < len(data):
        return paramErr
    t.mem.write(heap.deref(snd), data)
    t.mem.w16(header_len & ADDRESS_MASK, len(data))
    return noErr


@trap
def SetupAIFFHeader(t, refnum, channels, rate, size, compression, length, frames):
    """An AIFF file's chunks, or for compressed sound an AIFF-C file's,
    up to the sound's, written at the file's mark: the sound goes after.
    Given no number of frames, it's worked out from the bytes."""
    kind = struct.pack(">I", compression & 0xFFFFFFFF)
    length &= 0xFFFFFFFF
    counted = _frames(channels, size, kind, length)
    if counted is None:
        return sm.siInvalidCompression
    comm = struct.pack(">hIh", channels, frames & 0xFFFFFFFF or counted, size) \
        + _extended(rate & 0xFFFFFFFF)
    form = b"AIFF"
    if kind != b"NONE":
        name = bytes([len(MACE_NAMES[kind])]) + MACE_NAMES[kind]
        comm += kind + name + bytes(len(name) & 1)
        form = b"AIFC" + b"FVER" + struct.pack(">II", 4, AIFCVersion1)
    chunks = form + b"COMM" + struct.pack(">I", len(comm)) + comm \
        + b"SSND" + struct.pack(">III", 8 + length, 0, 0)
    head = b"FORM" + struct.pack(">I", (len(chunks) + length + (length & 1)) & 0xFFFFFFFF)
    try:
        t.process.files.write(refnum, head + chunks)
    except FileError as e:
        return e.code
    return noErr
