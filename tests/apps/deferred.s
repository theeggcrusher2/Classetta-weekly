| Deferred: a deferred task queued by a VBL task, through jDTInstall as an
| interrupt's task would, runs once its tick's tasks are done, with dtParm
| in A1, and once only.  One the application queues itself with
| _DTInstall runs at the next tick, with nothing else to run then.  A task
| that isn't a deferred task's qType isn't queued.

        .include "traps.inc"

VBL             = -14           | the VBLTask: qLink, qType, vblAddr, vblCount, vblPhase
DT              = -34           | a DeferredTask: qLink, qType, dtFlags, dtAddr, dtParm, dtReserved
DT2             = -54           | and another
RUNS            = -56           | the first task's runs
VRUNS           = -58           | the VBL task's
ORDER           = -60           | the VBL task's runs when the first task ran
RUNS2           = -62           | the second task's runs
BAD             = -64           | DTInstall's result for a VBLTask
GOOD            = -66           | and jDTInstall's for the first task
GOOD2           = -68           | and for the second
QUEUED          = -72           | the queue's qHead at the end
GLOBALS         = 72

TICKS           = 0x16A
DTQHEAD         = 0xD94         | DTQueue's qHead
JDTINSTALL      = 0xD9C
VTYPE           = 1
DTQTYPE         = 7

start:
        lea     DT(a5), a0
        clr.l   (a0)
        move.w  #VTYPE, 4(a0)
        _DTInstall
        move.w  d0, BAD(a5)

        lea     DT(a5), a0
        lea     deferred(pc), a1
        bsr.s   set_up
        lea     VBL(a5), a0
        clr.l   (a0)
        move.w  #VTYPE, 4(a0)
        lea     vbl_task(pc), a1
        move.l  a1, 6(a0)
        move.w  #1, 10(a0)
        clr.w   12(a0)
        move.w  #-1, GOOD(a5)
        _VInstall
        lea     RUNS(a5), a0
        bsr.s   wait
        bsr.s   five_ticks              | for any run more

        lea     DT2(a5), a0
        lea     deferred2(pc), a1
        bsr.s   set_up
        _DTInstall
        move.w  d0, GOOD2(a5)
        lea     RUNS2(a5), a0
        bsr.s   wait
        bsr.s   five_ticks
        move.l  DTQHEAD, QUEUED(a5)
        _ExitToShell

| The DeferredTask at A0, to call A1 with A5 its parameter.
set_up:
        clr.l   (a0)
        move.w  #DTQTYPE, 4(a0)
        clr.w   6(a0)
        move.l  a1, 8(a0)
        move.l  a5, 12(a0)
        clr.l   16(a0)
        rts

| Until the word at A0 isn't 0, a second at most.
wait:
        move.l  TICKS, d0
        addi.l  #60, d0
1:      tst.w   (a0)
        bne.s   2f
        cmp.l   TICKS, d0
        bhi.s   1b
2:      rts

five_ticks:
        move.l  TICKS, d0
        addq.l  #5, d0
1:      cmp.l   TICKS, d0
        bhi.s   1b
        rts

| A0 points at the task.  It doesn't set its count again, so it runs once.
vbl_task:
        movea.l a0, a2
        addq.w  #1, VRUNS-VBL(a2)
        lea     DT-VBL(a2), a0
        movea.l JDTINSTALL, a1
        jsr     (a1)
        move.w  d0, GOOD-VBL(a2)
        rts

deferred:
        addq.w  #1, RUNS(a1)
        move.w  VRUNS(a1), ORDER(a1)
        rts

deferred2:
        addq.w  #1, RUNS2(a1)
        rts
