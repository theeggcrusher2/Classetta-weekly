"""The Thread Manager (glue_threads) apart from an application: the
routines' results, and the dispatcher's.  Threads.bin, in
test_m68k_apps, runs threads that take turns."""

import struct
from types import SimpleNamespace

from macrt.m68k import glue_threads as th

from guest import Processor


def processor():
    p = Processor()
    p.time_tasks, p.expanding = SimpleNamespace(running=None), False
    p.cpu.sr = 0x2000                   # not at interrupt time
    p.machine.ticking = lambda: False   # nor the ticker's
    return p


def test_a_routine_macrt_hasnt_got_pops_its_arguments_and_is_unimplemented(qapp):
    """The selector's high byte says how many words of arguments it has."""
    p = processor()
    p.run(bytes.fromhex("4267" "2F3C00000001" "303C0209" "ABF2" "3E1F"))
    assert p.cpu.d(7) & 0xFFFF == 0xFFFC and p.cpu.sp == p.stack_top     # unimpErr
    assert p.missing == ["ThreadDispatch selector $0209"]


def test_new_thread_from_68k_and_its_state(qapp):
    """NewThread's seven arguments, in order; the thread made is ready,
    or stopped with kNewSuspend, and the one running is the main thread."""
    p = processor()
    made, state, current = p.ptr(bytes(4)), p.ptr(bytes(2)), p.ptr(bytes(4))
    p.run(bytes.fromhex("4267" "2F3C00000001" "2F3C00001234" "2F3C00000000" "2F3C00001000"
                        "2F3C00000001" "2F3C00000000") + struct.pack(">HI", 0x2F3C, made)
          + bytes.fromhex("303C0E03" "ABF2" "3E1F"))
    assert p.cpu.d(7) & 0xFFFF == 0 and p.cpu.sp == p.stack_top and not p.missing
    tid = p.mem.r32(made)
    assert tid == 3
    tm = th._manager(p)
    assert (tm.threads[tid].entry, tm.threads[tid].size) == (0x1234, 0x1000)
    assert th.GetThreadState(p, tm, tid, state) == 0 and p.mem.r16(state) == th.STOPPED
    assert th.GetCurrentThread(p, tm, current) == 0
    assert p.mem.r32(current) == th.kApplicationThreadID
    assert th.GetThreadState(p, tm, 99, state) == th.threadNotFoundErr


def test_a_thread_is_switched_to_only_when_it_may_be(qapp):
    """Not at interrupt time, during a tick's tasks or while a 'dcmp'
    runs: a yield then is threadProtocolErr.  Nor to a stopped thread;
    a yield to no thread in particular, with none ready, comes back at once."""
    p = processor()
    tm = th._manager(p)
    made = p.ptr(bytes(4))
    assert th.NewThread(p, tm, th.kCooperativeThread, 0x1234, 0, 0, 0, 0, made) == 0
    tid = p.mem.r32(made)
    assert th.YieldToThread(p, tm, tid) == (0, tm.threads[tid])
    p.cpu.sr = 0x2700
    assert th.YieldToThread(p, tm, tid) == th.threadProtocolErr
    p.cpu.sr = 0x2000
    p.expanding = True
    assert th.YieldToThread(p, tm, tid) == th.threadProtocolErr
    p.expanding = False
    assert th.SetThreadState(p, tm, tid, th.STOPPED, 0) == 0
    assert th.YieldToThread(p, tm, tid) == th.threadProtocolErr
    assert th.YieldToThread(p, tm, th.kNoThreadID) == 0
    assert th.YieldToThread(p, tm, th.kApplicationThreadID) == 0     # itself


def test_premade_threads_and_disposing_of_one_recycled(qapp):
    """CreateThreadPool's stacks are taken by NewThread with
    kUsePremadeThread; DisposeThread with recycleThread gives one back,
    the result where NewThread's threadResult points."""
    p = processor()
    tm = th._manager(p)
    count, made, result = p.ptr(bytes(2)), p.ptr(bytes(4)), p.ptr(bytes(4))
    assert th.CreateThreadPool(p, tm, th.kCooperativeThread, 2, 0x2000) == 0
    assert th.GetFreeThreadCount(p, tm, th.kCooperativeThread, count) == 0
    assert p.mem.r16(count) == 2
    assert th.GetSpecificFreeThreadCount(p, tm, th.kCooperativeThread, 0x4000, count) == 0
    assert p.mem.r16(count) == 0
    assert th.NewThread(p, tm, th.kCooperativeThread, 0x1234, 0, 0x4000,
                        th.kUsePremadeThread, 0, made) == th.threadTooManyReqsErr
    assert p.mem.r32(made) == th.kNoThreadID
    assert th.NewThread(p, tm, th.kCooperativeThread, 0x1234, 0, 0x1000,
                        th.kUsePremadeThread, result, made) == 0
    tid = p.mem.r32(made)
    assert th.GetFreeThreadCount(p, tm, th.kCooperativeThread, count) == 0
    assert p.mem.r16(count) == 1
    assert th.DisposeThread(p, tm, tid, 0x5678, True) == 0
    assert p.mem.r32(result) == 0x5678 and tid not in tm.threads
    assert th.GetFreeThreadCount(p, tm, th.kCooperativeThread, count) == 0
    assert p.mem.r16(count) == 2
    assert th.DisposeThread(p, tm, th.kApplicationThreadID, 0, False) == th.threadProtocolErr
    assert th.DisposeThread(p, tm, tid, 0, False) == th.threadNotFoundErr
