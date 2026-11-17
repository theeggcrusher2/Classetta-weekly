"""
The Thread Manager (ThreadDispatch, $ABF2): cooperative threads in one
application, each with a stack of its own, that take turns as they
yield to one another.  The application's own code, from its launch, is
its main thread, kApplicationThreadID.

While one thread runs, the others' registers are kept.  A call the glue
makes into 68k code, a dialog's filter procedure or a definition
procedure, holds its place on the host's stack until that code returns;
so that a thread can yield from anywhere, a filter procedure among
them, and be taken up there again, each thread's code runs on a host
thread of its own.  One runs at a time: a thread that yields hands the
processor to the next and waits until a thread hands it back.

The selector is in D0, the size of the arguments in words in its high
byte, and each routine's result an OSErr on the stack.  macrt's:

  NewThread          a thread whose entry is called with its parameter,
                     on a stack in the application heap (a size of 0
                     is macrt's DEFAULT_STACK), ready to run unless
                     kNewSuspend; when its entry returns, its result
                     goes where threadResult points and the thread ends
  DisposeThread      a thread ended, the result given going where its
                     NewThread's threadResult points; with recycleThread,
                     its stack is kept for another
  YieldToThread      to the thread named, kNoThreadID to the next ready
                     one, in turn, if there is one
  GetCurrentThread, GetThreadState, SetThreadState (and its EndCritical)
  ThreadBeginCritical, ThreadEndCritical   counted; as cooperative
                     threads switch only when they yield, nothing more
  CreateThreadPool, GetFreeThreadCount, GetSpecificFreeThreadCount
                     premade threads, whose stacks are taken at once
  GetDefaultThreadStackSize, ThreadCurrentStackSpace
  SetThreadTerminator, SetThreadSwitcher   procedures called as a thread
                     ends and as it's switched in or out

Preemptive threads run as cooperative ones do, when a thread yields to
them.  No thread is switched to from interrupt time, a tick's tasks or
a 'dcmp': a yield there returns threadProtocolErr.
"""

from __future__ import annotations

import itertools
import logging
import threading
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

from ..toolbox import lowmem
from ..toolbox.memmgr import memFullErr, noErr
from . import cpu as m68k
from .dispatch import pop_args, put_result, raw, unimpErr

log = logging.getLogger(__name__)

paramErr = -50
threadTooManyReqsErr = -617
threadNotFoundErr = -618
threadProtocolErr = -619

kNoThreadID, kCurrentThreadID, kApplicationThreadID = 0, 1, 2
kCooperativeThread, kPreemptiveThread = 1, 2
kNewSuspend, kUsePremadeThread, kCreateIfNeeded, kExactMatchThread = 1, 2, 4, 16
READY, STOPPED, RUNNING = 0, 1, 2           # kReadyThreadState and the others

DEFAULT_STACK = 0x6000                      # macrt's, for a stack size of 0
ENDING_WAIT = 5.0                           # seconds for a thread's host thread to end


class _Ended(BaseException):
    """Raised in a waiting thread's host thread as the thread's disposed
    of, to take the host thread's stack down."""


class _Disposed(BaseException):
    """Raised in the running thread's host thread once it has disposed of
    its own thread."""


@dataclass(eq=False)
class Thread:
    id: int
    state: int = READY
    style: int = kCooperativeThread
    entry: int = 0
    param: int = 0
    stack: int = 0                  # its stack's block; the main thread's is the application's
    size: int = 0
    result: int = 0                 # where its result goes, if anywhere
    ctx: object = None              # its registers, while another runs
    sp: int = 0                     # and its stack pointer
    host: threading.Thread | None = None    # once it has run
    wake: threading.Event = field(default_factory=threading.Event)
    ending: bool = False            # disposed of while it waited
    terminator: tuple[int, int] = (0, 0)    # a procedure and its parameter
    switch_in: tuple[int, int] = (0, 0)
    switch_out: tuple[int, int] = (0, 0)


class Threads:
    """An application's threads."""

    def __init__(self):
        # Made at the application's first call, from its own thread.
        main = Thread(kApplicationThreadID, RUNNING, host=threading.current_thread())
        self.threads: dict[int, Thread] = {main.id: main}
        self.main = self.current = main
        self.ids = itertools.count(kApplicationThreadID + 1)
        self.pool: list[tuple[int, int, int]] = []      # premade: style, stack, size
        self.critical = 0
        self.failure: BaseException | None = None       # for the main thread to raise
        self.clock_at = 0.0         # the application's processor time at the last switch


_managers: WeakKeyDictionary = WeakKeyDictionary()      # runner: Threads


def _manager(t) -> Threads:
    tm = _managers.get(t)
    if tm is None:
        tm = _managers[t] = Threads()
    return tm


def _thread(tm: Threads, tid: int) -> Thread | None:
    return tm.current if tid == kCurrentThreadID else tm.threads.get(tid & 0xFFFFFFFF)


def _next_ready(tm: Threads, after: Thread) -> Thread | None:
    """The ready thread after after, in turn."""
    order = list(tm.threads.values())
    i = order.index(after) if after in order else -1
    for th in order[i + 1:] + order[:max(i, 0)]:
        if th.state == READY and th is not after:
            return th
    return None


def _may_switch(t) -> bool:
    return not (t.cpu.sr & m68k.INTERRUPT_MASK or t.machine.ticking()
                or t.time_tasks.running is not None or t.expanding)


# =========================================================================
# Switching
# =========================================================================
def _switch(t, tm: Threads, to: Thread) -> None:
    """Hand the processor to thread to; this host thread waits until it's
    handed back."""
    me = tm.current
    if to is me:
        return
    _call(t, me.switch_out, me)
    if me.state == RUNNING:
        me.state = READY
    me.sp = t.cpu.sp
    me.ctx = t.cpu.context()
    _hand_to(t, tm, to)
    _wait(t, tm, me)


def _hand_to(t, tm: Threads, to: Thread) -> None:
    """Thread to runs: on its own host thread, which this one wakes or
    starts as the last thing it does."""
    cpu = t.cpu
    tm.current = to
    to.state = RUNNING
    tm.clock_at = t.cpu_time()
    if to.host is None:
        cpu.set_a(5, t.mem.r32(lowmem.CurrentA5))
        cpu.sp = (to.stack + to.size) & ~3
        cpu.push32(0)                       # its result's place
        cpu.push32(to.param)
        cpu.push32(t.machine.thread_exit)
        cpu.pc = to.entry
        to.host = threading.Thread(target=_host, args=(t, tm, to), daemon=True,
                                   name=f"{t.process.name} thread {to.id}")
        to.host.start()
    else:
        cpu.restore(to.ctx)
        to.ctx = None
        to.wake.set()


def _wait(t, tm: Threads, me: Thread) -> None:
    me.wake.wait()
    me.wake.clear()
    if me.ending:
        raise _Ended()
    failure, tm.failure = tm.failure, None
    if failure is not None:
        raise failure
    _resumed(t, tm, me)


def _resumed(t, tm: Threads, me: Thread) -> None:
    """This host thread has the processor: the application's processor
    time goes on from where it was."""
    from .runtime import thread_clock
    t.thread_id = threading.get_ident()
    clock = thread_clock()
    offset = tm.clock_at - clock()
    t.cpu_time = lambda: clock() + offset
    _call(t, me.switch_in, me)


def _host(t, tm: Threads, th: Thread) -> None:
    """A thread's host thread: its entry, called, and on its return the
    thread ended and the next run."""
    try:
        _resumed(t, tm, th)
        t.run_until(t.machine.thread_exit)
        _end(t, tm, th, t.cpu.pop32(), recycle=False)
    except _Disposed:
        pass
    except BaseException as e:      # noqa: BLE001 - for the main thread to raise
        if th.ending:
            return
        # The application stops: the registers stay this thread's, for the report.
        tm.failure = e
        tm.current = tm.main
        tm.main.wake.set()
        return
    _hand_to(t, tm, _next_ready(tm, th) or tm.main)


def _end(t, tm: Threads, th: Thread, result: int, recycle: bool) -> None:
    """Thread th is done with: its result where it goes, its terminator
    called, and its stack freed or kept for another."""
    if th.result:
        t.mem.w32(th.result, result)
    _call(t, th.terminator, th)
    del tm.threads[th.id]
    if recycle:
        tm.pool.append((th.style, th.stack, th.size))
    else:
        t.process.heap.dispose_ptr(th.stack)


def _call(t, proc: tuple[int, int], th: Thread) -> None:
    """pascal void proc(ThreadID thread, void *param), if there is one."""
    if proc[0]:
        t.call(proc[0], (("l", th.id), ("l", proc[1])))


def _take_down(th: Thread) -> None:
    """End the host thread of thread th, which waits, its stack's frames
    undone.  The caller keeps its registers."""
    if th.host is None or not th.host.is_alive():
        return
    th.ending = True
    th.wake.set()
    th.host.join(ENDING_WAIT)
    if th.host.is_alive():
        log.warning("thread %d's host thread didn't end", th.id)


def forget(t) -> None:
    """The application has quit: its threads' host threads end."""
    tm = _managers.pop(t, None)
    if tm is None:
        return
    for th in list(tm.threads.values()):
        if th is not tm.main:
            _take_down(th)


def stack_space(t) -> int | None:
    """What's left of the running thread's own stack; None in the main thread."""
    tm = _managers.get(t)
    if tm is None or tm.current is tm.main:
        return None
    return max(t.cpu.sp - tm.current.stack, 0)


# =========================================================================
# The routines
# =========================================================================
def _new_stack(t, tm: Threads, style: int, size: int, options: int) -> int | None:
    """A stack for a new thread, from the pool if asked; 0 if there's no
    room, None if no premade one will do and none's to be made."""
    if options & kUsePremadeThread:
        for i, (s, stack, n) in enumerate(tm.pool):
            if s == style and (n == size if options & kExactMatchThread else n >= size):
                del tm.pool[i]
                return stack
        if not options & kCreateIfNeeded:
            return None
    return t.process.heap.new_ptr(size)


def NewThread(t, tm, style, entry, param, size, options, result, made):
    if style not in (kCooperativeThread, kPreemptiveThread):
        return paramErr
    size = size or DEFAULT_STACK
    stack = _new_stack(t, tm, style, size, options)
    if not stack:
        if made:
            t.mem.w32(made, kNoThreadID)
        return threadTooManyReqsErr if stack is None else memFullErr
    th = Thread(next(tm.ids), STOPPED if options & kNewSuspend else READY, style, entry,
                param, stack, size, result)
    tm.threads[th.id] = th
    if made:
        t.mem.w32(made, th.id)
    return noErr


def DisposeThread(t, tm, tid, result, recycle):
    th = _thread(tm, tid)
    if th is None:
        return threadNotFoundErr
    if th is tm.main:
        return threadProtocolErr
    current = th is tm.current
    if current and not _may_switch(t):
        return threadProtocolErr
    _end(t, tm, th, result, bool(recycle))
    if current:
        raise _Disposed()               # its host thread hands the processor on
    cpu = t.cpu
    ctx = cpu.context()
    _take_down(th)
    cpu.restore(ctx)
    return noErr


def YieldToThread(t, tm, tid):
    me = tm.current
    if tid == kNoThreadID:
        to = _next_ready(tm, me)
        if to is None:
            return noErr
    else:
        to = _thread(tm, tid)
        if to is None:
            return threadNotFoundErr
        if to is me:
            return noErr
        if to.state != READY:
            return threadProtocolErr
    if not _may_switch(t):
        return threadProtocolErr
    return noErr, to


def GetCurrentThread(t, tm, p):
    t.mem.w32(p, tm.current.id)
    return noErr


def GetThreadState(t, tm, tid, p):
    th = _thread(tm, tid)
    if th is None:
        return threadNotFoundErr
    t.mem.w16(p, th.state)
    return noErr


def SetThreadState(t, tm, tid, state, suggested):
    """Stop a thread or make it ready, or run it now.  The running thread
    that stops or makes itself ready runs the suggested thread, or the
    next ready one, if there is one."""
    th = _thread(tm, tid)
    if th is None:
        return threadNotFoundErr
    if state not in (READY, STOPPED, RUNNING):
        return paramErr
    me = tm.current
    if th is not me:
        if state != RUNNING:
            th.state = state
            return noErr
        th.state = READY
        return (noErr, th) if _may_switch(t) else threadProtocolErr
    if state == RUNNING:
        return noErr
    to = _thread(tm, suggested) if suggested else None
    if to is None or to is me or to.state != READY:
        to = _next_ready(tm, me)
    if to is None:
        return noErr
    if not _may_switch(t):
        return threadProtocolErr
    me.state = state
    return noErr, to


def SetThreadStateEndCritical(t, tm, tid, state, suggested):
    tm.critical = max(tm.critical - 1, 0)
    return SetThreadState(t, tm, tid, state, suggested)


def ThreadBeginCritical(t, tm):
    tm.critical += 1
    return noErr


def ThreadEndCritical(t, tm):
    tm.critical = max(tm.critical - 1, 0)
    return noErr


def CreateThreadPool(t, tm, style, count, size):
    if style not in (kCooperativeThread, kPreemptiveThread):
        return paramErr
    size = size or DEFAULT_STACK
    made = []
    for _ in range(count):
        stack = t.process.heap.new_ptr(size)
        if not stack:
            for p in made:
                t.process.heap.dispose_ptr(p)
            return memFullErr
        made.append(stack)
    tm.pool.extend((style, p, size) for p in made)
    return noErr


def GetFreeThreadCount(t, tm, style, p):
    t.mem.w16(p, sum(1 for s, _, _ in tm.pool if s == style))
    return noErr


def GetSpecificFreeThreadCount(t, tm, style, size, p):
    size = size or DEFAULT_STACK
    t.mem.w16(p, sum(1 for s, _, n in tm.pool if s == style and n >= size))
    return noErr


def GetDefaultThreadStackSize(t, tm, style, p):
    t.mem.w32(p, DEFAULT_STACK)
    return noErr


def ThreadCurrentStackSpace(t, tm, tid, p):
    th = _thread(tm, tid)
    if th is None:
        return threadNotFoundErr
    sp = t.cpu.sp if th is tm.current else th.sp
    if th is tm.main:
        space = sp - t.process.heap.end
    elif th.host is None:
        space = th.size
    else:
        space = sp - th.stack
    t.mem.w32(p, max(space, 0))
    return noErr


def SetThreadTerminator(t, tm, tid, proc, param):
    th = _thread(tm, tid)
    if th is None:
        return threadNotFoundErr
    th.terminator = (proc, param)
    return noErr


def SetThreadSwitcher(t, tm, tid, proc, param, switching_in):
    th = _thread(tm, tid)
    if th is None:
        return threadNotFoundErr
    if switching_in:
        th.switch_in = (proc, param)
    else:
        th.switch_out = (proc, param)
    return noErr


# Selector: the routine, and its arguments as in trapdefs.
ROUTINES = {
    0x0501: (CreateThreadPool, "LHL"),
    0x0402: (GetFreeThreadCount, "Lp"),
    0x0615: (GetSpecificFreeThreadCount, "LLp"),
    0x0413: (GetDefaultThreadStackSize, "Lp"),
    0x0414: (ThreadCurrentStackSpace, "Lp"),
    0x0E03: (NewThread, "LpLLLpp"),
    0x0504: (DisposeThread, "LLB"),
    0x0205: (YieldToThread, "L"),
    0x0206: (GetCurrentThread, "p"),
    0x0407: (GetThreadState, "Lp"),
    0x0508: (SetThreadState, "LHL"),
    0x0512: (SetThreadStateEndCritical, "LHL"),
    0x000B: (ThreadBeginCritical, ""),
    0x000C: (ThreadEndCritical, ""),
    0x0611: (SetThreadTerminator, "LpL"),
    0x070A: (SetThreadSwitcher, "LpLB"),
}


@raw(0xABF2)
def ThreadDispatch(t, word):
    cpu = t.cpu
    selector = cpu.d(0) & 0xFFFF
    routine = ROUTINES.get(selector)
    if routine is None:
        t.unimplemented(f"ThreadDispatch selector ${selector:04X}", word)
        cpu.sp = cpu.sp + 2 * (selector >> 8)
        put_result(t.mem, cpu.sp, "e", unimpErr)
        return
    fn, codes = routine
    args, at = pop_args(t.mem, cpu.sp, codes)
    cpu.sp = at
    tm = _manager(t)
    out = fn(t, tm, *args)
    err, to = out if isinstance(out, tuple) else (out, None)
    put_result(t.mem, at, "e", err)
    if to is not None:
        _switch(t, tm, to)
