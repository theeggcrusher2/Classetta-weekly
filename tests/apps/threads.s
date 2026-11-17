| Threads: the Thread Manager.  The application, its main thread, makes a
| thread and yields to it until it's gone; the thread yields back, once
| from a grow-zone function the Memory Manager calls in its NewPtr, and
| returns a result.  A second, made stopped, can't be yielded to until
| it's made ready; disposed of while it waits, its terminator runs and
| it gets the result given.  A third disposes of itself, and a fourth is
| left waiting when the application quits.  Each logs a letter as it
| goes: M for the main thread, W, w, s and f for the others, G for the
| grow-zone function and t for the terminator.

        .include "traps.inc"

LOG             = -32           | the letters, in turn
LOGN            = -34           | how many
GESTALT         = -38           | Gestalt's 'thds'
MADE            = -42           | the first thread
RESULT          = -46           | and its result
ERR             = -48           | NewThread's
PARAM           = -52           | the parameter the first was given
WID             = -56           | GetCurrentThread in the first
MID             = -60           | and in the main thread, after
SPACE           = -64           | StackSpace in the first
GZ              = -66           | grow-zone calls
BIGP            = -70           | NewPtr's pointer
MADE2           = -74
RESULT2         = -78
STATE2          = -80           | GetThreadState, made stopped
YERR2           = -82           | YieldToThread to it then
DERR2           = -84           | DisposeThread's
TERMID          = -88           | the thread the terminator was given
TERMP           = -92           | and its parameter
MADE3           = -96
RESULT3         = -100
AFTER3          = -102          | set if DisposeThread of itself returned
MADE4           = -106
GLOBALS         = 106

kApplicationThreadID = 2
kCurrentThreadID = 1
threadNotFoundErr = -618

start:
        lea     -GLOBALS(a5), a0
        moveq   #GLOBALS/2-1, d0
1:      clr.w   (a0)+
        dbra    d0, 1b

        move.l  #0x74686473, d0         | 'thds'
        _Gestalt
        move.l  a0, GESTALT(a5)
        lea     growzone(pc), a0
        _SetGrowZone

        moveq   #'M', d0
        bsr     log
        pea     first(pc)
        move.l  #0x1234, d0
        moveq   #0, d1
        pea     RESULT(a5)
        pea     MADE(a5)
        bsr     new_thread
        move.w  d0, ERR(a5)
1:      move.l  MADE(a5), d0
        bsr     yield
        cmpi.w  #threadNotFoundErr, d0
        beq.s   2f
        moveq   #'M', d0
        bsr     log
        bra.s   1b
2:      subq.l  #2, sp
        pea     MID(a5)
        move.w  #0x0206, d0             | GetCurrentThread
        _ThreadDispatch
        addq.l  #2, sp

        pea     second(pc)
        moveq   #0, d0
        moveq   #1, d1                  | kNewSuspend
        pea     RESULT2(a5)
        pea     MADE2(a5)
        bsr     new_thread
        subq.l  #2, sp
        move.l  MADE2(a5), -(sp)
        pea     STATE2(a5)
        move.w  #0x0407, d0             | GetThreadState
        _ThreadDispatch
        addq.l  #2, sp
        move.l  MADE2(a5), d0
        bsr     yield
        move.w  d0, YERR2(a5)
        subq.l  #2, sp
        move.l  MADE2(a5), -(sp)
        pea     terminator(pc)
        move.l  #0x77, -(sp)
        move.w  #0x0611, d0             | SetThreadTerminator
        _ThreadDispatch
        addq.l  #2, sp
        subq.l  #2, sp
        move.l  MADE2(a5), -(sp)
        clr.w   -(sp)                   | kReadyThreadState
        clr.l   -(sp)
        move.w  #0x0508, d0             | SetThreadState
        _ThreadDispatch
        addq.l  #2, sp
        move.l  MADE2(a5), d0
        bsr     yield
        subq.l  #2, sp
        move.l  MADE2(a5), -(sp)
        move.l  #0x9ABC, -(sp)
        clr.w   -(sp)                   | not to recycle
        move.w  #0x0504, d0             | DisposeThread
        _ThreadDispatch
        move.w  (sp)+, DERR2(a5)
        moveq   #'M', d0
        bsr     log

        pea     third(pc)
        moveq   #0, d0
        moveq   #0, d1
        pea     RESULT3(a5)
        pea     MADE3(a5)
        bsr     new_thread
        move.l  MADE3(a5), d0
        bsr     yield
        moveq   #'M', d0
        bsr     log

        pea     fourth(pc)
        moveq   #0, d0
        moveq   #0, d1
        clr.l   -(sp)
        pea     MADE4(a5)
        bsr     new_thread
        move.l  MADE4(a5), d0
        bsr     yield
        moveq   #'M', d0
        bsr     log
        _ExitToShell

| The first: its parameter kept, a yield back, and another from the
| grow-zone function NewPtr calls for a block too big to have.
first:
        link    a6, #0
        move.l  8(a6), PARAM(a5)
        moveq   #'W', d0
        bsr     log
        subq.l  #2, sp
        pea     WID(a5)
        move.w  #0x0206, d0             | GetCurrentThread
        _ThreadDispatch
        addq.l  #2, sp
        _StackSpace
        move.l  d0, SPACE(a5)
        moveq   #kApplicationThreadID, d0
        bsr     yield
        moveq   #'W', d0
        bsr     log
        move.l  #0x01000000, d0
        _NewPtr
        move.l  a0, BIGP(a5)
        moveq   #'W', d0
        bsr     log
        move.l  #0x5678, 12(a6)         | its result
        unlk    a6
        movea.l (sp)+, a0               | its parameter popped
        addq.l  #4, sp
        jmp     (a0)

| Waits, yielding to the main thread, until it's disposed of.
second:
        moveq   #'w', d0
        bsr     log
1:      moveq   #kApplicationThreadID, d0
        bsr     yield
        bra.s   1b

| Disposes of itself.
third:
        moveq   #'s', d0
        bsr     log
        subq.l  #2, sp
        move.l  #kCurrentThreadID, -(sp)
        move.l  #0xDEF0, -(sp)
        clr.w   -(sp)
        move.w  #0x0504, d0             | DisposeThread
        _ThreadDispatch
        addq.l  #2, sp
        st      AFTER3(a5)
        movea.l (sp)+, a0               | its parameter popped
        addq.l  #4, sp
        jmp     (a0)

| Left waiting when the application quits.
fourth:
        moveq   #'f', d0
        bsr     log
1:      moveq   #kApplicationThreadID, d0
        bsr     yield
        bra.s   1b

| PROCEDURE terminator(thread: ThreadID; param: Ptr), which keeps both.
terminator:
        moveq   #'t', d0
        bsr     log
        movea.l (sp)+, a0
        move.l  (sp)+, TERMP(a5)
        move.l  (sp)+, TERMID(a5)
        jmp     (a0)

| FUNCTION MyGrowZone(cbNeeded: Size): LongInt, which yields to the
| main thread and frees nothing.
growzone:
        addq.w  #1, GZ(a5)
        moveq   #'G', d0
        bsr     log
        moveq   #kApplicationThreadID, d0
        bsr     yield
        movea.l (sp)+, a0
        addq.l  #4, sp
        clr.l   (sp)
        jmp     (a0)

| NewThread(kCooperativeThread, entry, D0, 0, D1, threadResult,
| threadMade), with entry, threadResult and threadMade on the stack:
| its OSErr in D0.
new_thread:
        movea.l (sp)+, a4
        movem.l (sp)+, a0/a2            | threadMade, threadResult
        movea.l (sp)+, a3               | entry
        subq.l  #2, sp
        move.l  #1, -(sp)
        move.l  a3, -(sp)
        move.l  d0, -(sp)
        clr.l   -(sp)
        move.l  d1, -(sp)
        move.l  a2, -(sp)
        move.l  a0, -(sp)
        move.w  #0x0E03, d0
        _ThreadDispatch
        move.w  (sp)+, d0
        jmp     (a4)

| YieldToThread(D0): its OSErr in D0.
yield:
        subq.l  #2, sp
        move.l  d0, -(sp)
        move.w  #0x0205, d0
        _ThreadDispatch
        move.w  (sp)+, d0
        rts

| D0's low byte onto the log.
log:
        move.w  LOGN(a5), d1
        lea     LOG(a5), a0
        move.b  d0, 0(a0,d1.w)
        addq.w  #1, LOGN(a5)
        rts
