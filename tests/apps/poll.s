| Poll: GetNextEvent and WaitNextEvent with nothing to give.  The
| application waits for the Ticks to change and then calls GetNextEvent
| once, 30 times, as Hacker II does as it types: as on a Mac, where it
| returns at once, that takes 30 ticks, not 60.  Then it calls it 30 times
| one after another, which takes about 30 ticks too: asked again in the
| same tick, it waits for the next.  WaitNextEvent with no time to sleep
| is timed the same two ways, and then 30 times round a loop like the one
| at Lemmings' title, which posts itself an event, takes it and asks again.

        .include "traps.inc"

EVENT           = -16           | an EventRecord
ONCE            = -20           | ticks for GetNextEvent's 30 waits and looks
AGAIN           = -24           | ticks for its 30 looks one after another
WAIT_ONCE       = -28           | and for WaitNextEvent's
WAIT_AGAIN      = -32
POSTED          = -36           | ticks for the 30 rounds of posting, taking and asking
GLOBALS         = 36

TICKS           = 0x16A
TIMES           = 30
app1Evt         = 12

start:
        lea     look(pc), a2
        bsr.s   once
        move.l  d0, ONCE(a5)
        bsr.s   again
        move.l  d0, AGAIN(a5)
        lea     wait(pc), a2
        bsr.s   once
        move.l  d0, WAIT_ONCE(a5)
        bsr.s   again
        move.l  d0, WAIT_AGAIN(a5)
        lea     post(pc), a2
        bsr.s   again
        move.l  d0, POSTED(a5)
        _ExitToShell

| In D0, the ticks that calling (A2) 30 times takes, each once the Ticks
| have changed.
once:
        move.l  TICKS, d6
        moveq   #TIMES-1, d7
1:      move.l  TICKS, d0
2:      cmp.l   TICKS, d0
        beq.s   2b
        jsr     (a2)
        dbra    d7, 1b
        move.l  TICKS, d0
        sub.l   d6, d0
        rts

| And 30 times one after another.
again:
        move.l  TICKS, d6
        moveq   #TIMES-1, d7
1:      jsr     (a2)
        dbra    d7, 1b
        move.l  TICKS, d0
        sub.l   d6, d0
        rts

look:
        clr.w   -(sp)
        move.w  #-1, -(sp)              | everyEvent
        pea     EVENT(a5)
        _GetNextEvent
        addq.l  #2, sp
        rts

wait:
        clr.w   -(sp)
        move.w  #-1, -(sp)              | everyEvent
        pea     EVENT(a5)
        clr.l   -(sp)                   | no time to sleep
        clr.l   -(sp)                   | and no mouse region
        _WaitNextEvent
        addq.l  #2, sp
        rts

post:
        move.w  #app1Evt, a0
        moveq   #0, d0
        _PostEvent
        bsr.s   wait                    | the event
        bra.s   wait                    | and then nothing
