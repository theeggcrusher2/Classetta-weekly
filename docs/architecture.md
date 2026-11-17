# How macrt works

Part of [the documentation](README.md).

This page is for readers who know the 68000 family, the classic Mac OS
and its Toolbox, and want to know how macrt stands in for a Mac: what
runs the 68k code, how a trap gets to Python, where things sit in the
Mac's memory, and how the windows on your screen follow the records in
it. [What works](toolbox.md) then goes through the managers one at a
time, and [Running a Mac application](running-apps.md#what-happens-at-launch)
gives the steps of a launch.

- [The big picture](#the-big-picture)
- [The processor](#the-processor)
- [Traps](#traps)
- [Memory](#memory)
- [Drawing and the display](#drawing-and-the-display)
- [Resources and files](#resources-and-files)
- [Text and fonts](#text-and-fonts)
- [The source tree](#the-source-tree)


## The big picture

macrt runs an application's own 68k code on an emulated CPU, catches its
A-line traps, and answers them with a Toolbox written in Python, with Qt
widgets standing in for what QuickDraw used to draw. There is no
disassembly or translation step. The program runs as it did on a Mac, and
the Toolbox it calls into is ours.

```
guest thread (one per app)                         GUI thread
──────────────────────────                         ─────────────────────────────────
68k code on the CPU (m68k/cpu.py)
  └─ A-line trap ─▶ dispatcher (m68k/dispatch.py)
       └─ glue (m68k/glue_*.py)
            └─ Toolbox (toolbox/api.py) ─────────▶ GuiBridge.call ──▶ Display (ui/display.py)
                 WaitNextEvent ◀── EventQueue ◀──────────────────────── InputRouter (Qt events)
                 FindWindow, MenuSelect,                                     │
                 TrackGoAway, DragWindow… ◀── Future ◀── Trackers ◀──────────┘
```

**One processor, shared.** The `Machine` (`m68k/runtime.py`) is the CPU
and the trap stubs; every 68k application has a `Runner` on its own
thread. As under MultiFinder, one application runs at a time. It holds
the CPU while its code runs and gives it up in `WaitNextEvent`,
`GetNextEvent` and the other calls that wait. Whoever takes it next gets
its registers and its low-memory globals put back.

**Guest memory is the source of truth.** A window is a 156-byte
`WindowRecord` in the app's heap: title, port rectangles, regions, hilite
state, zoom states. A `MacWindowView` only draws whatever the record says.
Poke the record the way 68k code would and the widget follows on the next
sync.


## The processor

The processor is unicorn's 68020, in 24-bit mode. Gestalt, `SysEnvirons`
and the low-memory globals say it's a Mac Plus, with a 68000 and no
floating-point unit, and the exception frames it stacks are a 68000's.
With `--model ii` it's a Mac II's 68020 and 68881, and with
`--model iix` a Mac IIx's 68030 and 68882, for which Gestalt says
there's no memory management unit. The instructions run are the same
whichever it is: only what the program is told, and the exception frames,
differ. Programs from before the Mac II need a 68000's: its exception
frames are smaller than a 68020's, and MacWrite 4.5 pops its own.

The few instructions of a 68020 and a 68881 that unicorn doesn't run, or
runs otherwise than Motorola's manual says (`CHK2` and `CMP2`, `PACK`
and `UNPK`, packed decimal `FMOVE`, an `FMOVE` of immediate data to the
floating-point control registers, `FSAVE` and `FRESTORE`) stop it, and
macrt runs them in Python (`macrt/m68k/instructions.py`). Trace mode is
run an instruction at a time.

**Code that writes code.** The processor translates blocks of 68k code
and runs the translation. It notices a store into a block it has
translated, but not into the block it's running: a page is mapped to be
run from or written to, not both, and a store into the page it's running
from starts the processor again at the store (see `m68k/cpu.py`).
Microsoft Word sets a trap word a few instructions ahead and then runs it.

**Speed.** By default 68k code runs as fast as the host can run it. With
`--speed N`, each instruction is counted by the cycles it would take a
68000 (`m68k/cycles.py`), and the code is held to N times a Mac Plus's
pace, for games that time themselves by the processor (see [The
hardware](toolbox.md#the-hardware)).


## Traps

`cpu.run()` returns at every A-line trap. The dispatcher
(`m68k/dispatch.py`) looks up the trap's signature in
`m68k/trapdefs.py`, which is generated from the Multiversal interface
files: where its arguments are (Pascal stack or registers), its result,
and its selector for dispatched traps such as `_Pack3` or `_OSDispatch`.
Then it calls the Python routine registered under the trap's name with
`@trap`.

The dispatcher decodes the trap word in two ways:

- **Toolbox traps** (`$A800`-`$AFFF`) take Pascal arguments on the
  stack: space for the result first, then the arguments left to right,
  so the last is on top. The routine pops its arguments and leaves the
  result. Bit 10 is auto-pop: the trap was reached by a `JSR` to glue,
  and it returns to the address on top of the stack instead. The trap
  number is the low 10 bits.
- **OS traps** (`$A000`-`$A7FF`) take their arguments in registers,
  mostly A0 and D0, and return a result in D0 (and in A0 when bit 8 is
  set). Bits 9 and 10 are flags for the routine: SYS and CLEAR for the
  Memory Manager, HFS and ASYNC for the File Manager. The trap number is
  the low 8 bits. After the routine the dispatcher tests D0.W, so code
  can branch on the result.

A known trap with nothing behind it is logged and skipped: its
arguments are popped by its signature and it returns zero, or `unimpErr`
for one that returns an error code. One that isn't known at all stops
the application with system error 12, since there is no telling how much
of the stack is its (see [When it stops](running-apps.md#when-it-stops)).
On a Plus, whose 128K ROM took a Toolbox trap's number from bits 0-8 of
the word alone (Inside Macintosh IV, Using Assembly Language), a Toolbox
word with bit 9 set that names nothing is the trap in bits 0-8: Crozzle
0.8 calls `NewWindow` as `$AB13`.

**Patches.** `SetTrapAddress` patches work, and so does calling a trap
through the address `GetTrapAddress` gave. A patch on an OS trap is
entered as the Mac's dispatcher enters it, with the registers it saved on
the stack, since some patches unwind that frame themselves.
`GetTrapAddress` calls `_OSDispatch` unimplemented, although it works:
programs that look for MultiFinder that way, Microsoft's among them,
then make MultiFinder's private calls. Programs that want the Process
Manager or temporary memory ask Gestalt.

**Calling back.** Filter procedures, `CDEF`s, user items, action
procedures and patched traps are 68k code that the Toolbox calls.
`Runner.call()` runs a nested CPU loop until the routine returns.

[Working on macrt](development.md#adding-a-toolbox-routine) shows what a
trap routine looks like.


## Memory

`System` (in `toolbox/system.py`) owns 8 MB of emulated RAM:

```
$000000  low-memory globals: MemTop, ScrnBase, WindowList, MenuList, CurrentA5…
$003000  system heap: the Window Manager port, GrayRgn, the System file's
         resource map and the defproc stand-ins
$100000  one partition per app, sized by its SIZE resource (else 1 MB)
top      the screen buffer
```

A 512×342 screen is where a Mac 512K has it instead, at `$7A700` with the
alternate buffer at `$72700` below it. The system heap ends below both,
and the partitions take the rest of RAM above them. Games from 1984 write
there without asking `ScrnBase` (see [The
hardware](toolbox.md#the-hardware)).

Each partition is laid out like a classic Mac application space: heap at the
bottom, stack below the A5 world, A5 world at the top. The QuickDraw globals
(thePort, the patterns, the arrow cursor, screenBits) sit just below A5, and
0(A5) points to thePort, which is where `InitGraf` leaves it.

* **Memory Manager** (`memmgr.py`): pointers and handles with the 24-bit block
  header (tag byte, physical size, relative handle or zone pointer) and master
  pointers with their flag byte (locked, purgeable, resource). Code that looks
  at a header finds what it expects there. A locked handle never moves.
  When an allocation doesn't fit, the application
  heap grows toward `ApplLimit`, all the way when even that isn't enough:
  programs written before `MaxApplZone` ask for more than there is to get
  the whole heap, and MacWrite counts what's free by walking the block
  headers. A purged handle stays valid with a NIL master pointer, as
  `EmptyHandle` leaves it. [What works](toolbox.md#memory-manager) has
  the rest: compaction, purging, temporary memory and grow-zone
  functions.
* **Per-app globals** (`lowmem.py`): under MultiFinder some low-memory globals
  belong to the running app (`WindowList`, `MenuList`, `CurrentA5`, `ApplZone`…).
  Each app keeps its copy in a `World`, which is swapped in while that app has
  the CPU. The Toolbox always goes through the `World`, so it gets the right
  copy either way.
* **Records** (`windows.py`, `menus.py`, `quickdraw.py`): `GrafPort` (108 bytes),
  `WindowRecord` (156 bytes, chained through `nextWindow` from the app's
  `WindowList` global), `MenuInfo` (a handle: ID, enable flags, then menuData
  with the title and items), and the `MenuList` menu bar handle, all laid out
  as in Inside Macintosh. The Python classes are views over the bytes. There is
  one canonical view per port address, so a WindowPtr that comes back from guest
  code is the same object the UI knows.

### The stack

An application gets 32K of stack between `ApplLimit` and
`CurStackBase`, and `DefltStack` says so: The Fool's Errand takes what
that falls short of 32K from its heap. A `SIZE` resource's sizes, and
Get Info's, count 8K of it, the stack Inside Macintosh's "The Stack and
the Heap" says an application starts with; the other 24K comes on top,
so the heap is the one the sizes leave room for. GNU Chess Pro 1.0
halves its two tables until both fit: in a heap 8K bigger they fit at
160K each, and its board then can't get the memory it draws with.

`ReserveMem` grows the heap toward `ApplLimit` for what it reserves,
and all the way when that's not enough, as a 64K ROM's does for
programs that ask for more than there is in place of `MaxApplZone`.
`SetApplLimit` leaves the stack at least `DefltStack`, as the errata to
Inside Macintosh: Memory says it does ("Addendum to Description of
SetApplLimit"): a limit nearer `CurStackBase` than that, or past it,
becomes `DefltStack` below `CurStackBase`, where launch puts it. Macify
2.7 asks for 128K past `ApplZone` in its 75K partition, past the end of
it, and Bumptz Science Carnival for a limit at its stack pointer; each
then calls `MaxApplZone`, and a segment, or a handle moved high, would
otherwise land on the stack in use.


## Drawing and the display

* **QuickDraw** (`qdraw.py`) draws into an image for each window (its
  `Backing`, which the window shows), or, for any other port, into its
  portBits: a bitmap or pixel map in guest memory, so offscreen drawing
  and `CopyBits` work as on a Mac. Regions are real QuickDraw regions,
  read from and written back to their data in guest memory. Text moves
  the pen a whole number of pixels for each character, as on a Mac, so
  text that a word processor measures a character at a time lands where
  it draws it.
* **The screen buffer** (`screen.py`) mirrors the windows. What QuickDraw
  draws in a window is written to the buffer where the window is seen,
  and what a program writes in the buffer itself is drawn in the window
  it lands on, the next time macrt looks: at a trap, or from the CPU's
  clock once a tick while the program calls none.
* **The chrome is a puppet.** Qt never acts on a click by itself. Every click
  becomes a `mouseDown` event in the owning app's queue. The app calls
  `FindWindow` and then, say, `TrackGoAway`, and only then does the close box
  draw pressed. If the app never calls `DragWindow`, the window doesn't move,
  just as on a real Mac.
* **Blocking calls are trackers.** `MenuSelect`, `TrackGoAway`, `TrackBox`,
  `DragWindow` and `GrowWindow` start a tracker on the GUI thread and wait on a
  `Future` that the tracker settles from mouse events. If the button is already
  up by the time the app gets there, the tracker sees that, as `StillDown`
  would.
* **`ui/chrome.py` is the WDEF.** It is a set of pure functions for geometry,
  `wHit` codes and painting, covering document windows (with or without
  grow/zoom), `dBoxProc`, `plainDBox`, `altDBoxProc`, `movableDBoxProc` and
  `rDocProc` with its corner variations. Everything is drawn with whole-pixel
  `fillRect`. An application's own `WDEF` or `MDEF` is run as 68k code
  instead (`m68k/wdefs.py`, `m68k/mdefs.py`).

![](screenshots/window-kinds.png)

### Where the windows go

`DisplayConfig` (in `macrt/ui/display.py`) has two independent settings,
which the command line's `--host` and `--menubar` set:

| | `menu_bar="global"` | `menu_bar="window"` |
|---|---|---|
| **`host="desktop"`** | Mac windows are frameless host windows, with one menu bar across the top of the screen | Mac windows are host windows, each with its app's menu bar under the title bar, so they blend in with host apps |
| **`host="screen"`** | One host window holds a Mac screen (desktop pattern, rounded corners) with a classic menu bar | The same screen, with the menu bars in the windows |

**Global menu bar.** The bar shows the front app's menus. Clicking another
app's window brings that app forward (MultiFinder style). It gets suspend and
resume events, the bar switches to its menus, and the click itself is not passed
on as a mouseDown. With two or more apps running, the System 7 Application menu
appears at the right end (Hide *App*, Hide Others, Show All, then the app list).
`application_menu="always"|"never"` overrides that. An app that adds items to its
System 7 Help menu (`HMGetHelpMenuHandle`) has that menu to its left, under a
balloon.

**Per-window menu bar.** Every document window draws its app's bar under the
title bar. When a dialog is in front, the menus stay reachable through the app's
other windows. An app with no document windows open gets a small bar-only window
titled with its name, so its menus never disappear.

Other options: `screen_size`; `sticky_menus` (default on: a quick click on a
title leaves the menu open, Mac OS 8 style; off means strictly
press-drag-release); and `theme.RAINBOW_APPLE` for the System 7 color apple.


## Resources and files

`toolbox/resources.py` is the Resource Manager. As on a Mac, resource maps
live in the heap in their on-disk format, so 68k code that walks one finds
what it expects, and resources are read in through `_ResourceStub`, so an
application's patch on it, given a map's handle in A4 and an entry in A2,
can read them in its own way.

* **Files.** `macfile.load_file` reads a Mac file in whatever form the host
  keeps it: MacBinary I/II/III, BinHex 4.0, AppleSingle, AppleDouble
  (`._app`, `.AppleDouble/app`, or `app.rsrc` as The Unarchiver writes it),
  Basilisk II's `.rsrc/` and `.finf/`, Executor's `%app`, a bare `app.rsrc`,
  or macOS's `..namedfork/rsrc`. `open_application` is what the command line
  uses: it also opens disk images and refuses StuffIt and zip archives with a
  hint.
* **Disk images.** `load_volume` opens HFS disk images, raw, DiskCopy
  4.2, behind an Apple partition map or of a CD's whole 2352-byte
  sectors, into machfs's form, walking their catalog and extents trees
  with machfs's B*-tree reader but leaving each file's forks on the disk
  until they're read (a CD's image, or any of 16 MB or more, is mapped
  rather than read in, but for the data of a raw CD image's sectors). It
  reads MFS (400K) disks into the same form: their one flat directory,
  each fork followed through the block map. HFS Plus volumes, on their
  own, in an HFS wrapper or in an `Apple_HFS` or `Apple_HFSX` partition,
  are read into it too (`_read_hfs_plus`, from Technote 1150), with their
  dates made local, hard links made the files they link to, and names
  HFS can't hold cut short and given their catalog node ID. A MOOF image
  of a floppy's tracks is decoded to its sectors first (`macrt.moof`):
  the GCR bytes found in each track's bits, and each sector's three bytes
  to four of them, unmixed from the checksum that runs through them. The
  volume keeps its disk's sectors and their tags as `.disk`, for the
  floppy disk driver (`glue_sony`), and a MOOF image's tracks with them.
  A `Folder` finds files by Mac name, case-insensitively, and
  `OpenResFile` looks names up in the folder the app came from.
* **The chain.** Opening a file copies its map into a handle, fills in
  `nextMap` and the file reference number, and puts it at the top of the
  app's chain (`TopMapHndl`, `CurMap`). Every chain ends at the System file's
  map in the system heap (`SysMapHndl`, `SysMap` = 2). If no System file is
  given, an empty one is made. Either way the WDEF, MDEF, CDEF, LDEF and MBDF
  stand-ins are resources in it, so `GetResource('WDEF', 0)` works and a
  window's `windowDefProc` is a real resource handle. So are the standard
  resources in `sysres.py` that the file lacks: `PACK` 0 to 15 (each the
  package's trap with auto-pop, so calling into the package is the same
  as calling the trap), the alert icons `ICON` 0 to 2 and the cursors
  `CURS` 1 to 4.
* **Loading.** The entry in the map holds the resource's handle, so asking
  twice gives the same handle. Attributes set the master pointer flags:
  locked, purgeable, `resSysHeap` (loaded into the system heap) and
  `resPreload` (loaded at open). A purged resource is read again into the same
  handle. System 7 compressed resources are expanded through rsrcfork; one
  that won't expand gives `CantDecompress`, unless the `dcmp` its header
  names is there to run (`m68k/decompressors.py`).
  `SetResLoad(false)`, `Get1…` and `UseResFile` behave as documented.
* **Changes.** `AddResource`, `ChangedResource`, `RemoveResource`,
  `SetResInfo` and `SetResAttrs` work on the map in memory. `UpdateResFile`
  and `CloseResFile` write the fork back through the File Manager, which
  keeps it where [Documents and
  saving](running-apps.md#documents-and-saving) says a program's files go.
* **The application.** A launch opens the app's resource fork as its current
  file (`CurApRefNum`) and sizes its partition from its SIZE resource.
  Quitting closes every file the app opened.

`toolbox/templates.py` parses WIND, DLOG, ALRT (with its four stages), DITL,
CNTL, MBAR and STR#, and applies System 7 positioning words such as
`centerMainScreen` and `alertPositionMainScreen`. On top of these:
`GetNewWindow`, `GetMenu` (the MENU resource *is* the MenuInfo; its MDEF ID
becomes the MDEF's handle), `GetNewMBar`, `SetMenuBar`, `GetMenuBar`,
`GetString` and `GetIndString`. The Dialog and Control Managers build their
dialogs, alerts and controls from DLOG, ALRT, DITL and CNTL.

The same Toolbox serves guest applications written in Python, as the
demo's are:

```python
from macrt.macfile import load_file

app = load_file("Hello.bin")            # or ._Hello, Hello.rsrc, …
display.launch(app.name, guest, app=app)
# guest(tb): tb.SetMenuBar(tb.GetNewMBar(128)); w = tb.GetNewWindow(128) …
```


## Text and fonts

Your computer's fonts stand in for the standard families (Chicago,
Geneva, New York, Monaco and the rest); [Running a Mac
application](running-apps.md#fonts) says how to give macrt a Chicago
and a Geneva. A family an application brings with it, as a `FONT`, or
as an `NFNT` its `FOND` lists, is drawn from the font's own bitmap
(`toolbox/bitfont.py`). That matters because such fonts often hold
pictures rather than letters: MacPaint's tool palette is one, and games
keep sprites and score digits in them. A size the application doesn't
have is scaled from the nearest one it does. A standard family is drawn
from a strike too when the application carries its own of the size, as
on a Mac, where the application's file is looked in before the System's:
Balance of Power traces its New York 36 title letter by letter and needs
them where they were. Text in such a font is measured with the font's
own widths too, by `StringWidth`, `MeasureText`, TextEdit and
`TETextBox` alike, so lines break where the application expects. Bold,
italic, underline, outline and shadow are drawn as QuickDraw draws them.
What a Mac font has no width for, such as NUL and return in Chicago,
Geneva and Monaco, draws nothing, where a host font would draw a box.

A real System file, or the disk's own, also gives the standard families
its strikes' widths and line spacing (see [A real System
file](running-apps.md#a-real-system-file)).


## The source tree

```
macrt/
  memory.py    guest RAM (big-endian, 24-bit addresses) and typed record views
  resfork.py   the resource fork format, read (every offset checked) and written
  macfile.py   Mac files from the host: MacBinary, BinHex, AppleSingle/Double,
               emulator side files, HFS, HFS Plus and MFS disk images
  moof.py      Applesauce MOOF floppy images: each track's GCR bits decoded
               back to sectors
  process.py   one running guest application
  viewer.py    the resource listing, and ResourceViewer, which shows an
               app's menus and windows from its resources
  demo.py      the command line, and the demo's Sampler and Ticker
  survey.py    the survey tool (docs/survey.md)

  m68k/        running 68k code
    cpu.py           the processor: unicorn's core on the guest RAM
    instructions.py  what unicorn doesn't run, run in Python
    cycles.py        a 68000's instruction timings, for --speed
    runtime.py       Machine, Runner, system errors
    dispatch.py      the A-line trap dispatcher
    trapdefs.py      trap signatures, generated by tools/gen_trapdefs.py
    segload.py       CODE 0, the jump table, LoadSeg
    hardware.py      the VIA and the IWM
    sound.py         the Sound Driver, the speaker and the Apple Sound Chip
    sound_manager.py the Sound Manager's channels and synthesizers
    timemgr.py       the Time Manager's tasks
    wdefs.py, mdefs.py, popups.py
                     applications' own WDEFs and MDEFs; pop-up menu controls
    controlpanel.py  control panels ('cdev' files)
    decompressors.py running 'dcmp' resources
    glue_*.py        the trap routines, a file a manager: quickdraw, cport,
                     color, gworld, palette, picker, pict, pictinfo, icons,
                     fonts, windows, menus, menucolors, controls, dialogs,
                     lists, textedit, scrap, events, help, resources, files,
                     aliases, stdfile, devices, desk, sony, cdrom, scsi, slots,
                     video, adb, sound, process, threads, aevents, editions,
                     components, ppc, print, sane, intl, script, utilities,
                     os, common

  toolbox/     the Toolbox's data and logic, apart from the traps
    api.py           the Toolbox calls as a guest application sees them
    system.py        the memory map and partitions
    memmgr.py        heaps, pointers and handles
    lowmem.py        low-memory globals and each application's World
    resources.py     the Resource Manager; sysres.py, the System's own resources
    templates.py     WIND, DLOG, ALRT, DITL, CNTL, MBAR, STR#
    quickdraw.py     GrafPorts, the QuickDraw globals, regions
    qdraw.py         QuickDraw's drawing
    pict.py, pictutil.py
                     pictures, played and recorded; the Picture Utilities
    screen.py        the screen buffer
    gdevice.py, color.py, pixels.py
                     graphics devices, pixel maps and colour tables; pixels as
                     host images
    icons.py, masks.py, cursors.py, bitfont.py
                     icons; CopyMask, SeedFill and CalcMask; cursors; bitmap fonts
    windows.py, menus.py, controls.py, dialogs.py, lists.py, textedit.py,
    scrap.py, events.py
                     the managers' records and logic
    files.py, aliases.py
                     the File Manager; alias records
    printing.py      the Printing Manager's records and its PDF printer
    sane.py, intl.py, script.py, numformat.py
                     SANE; the International Utilities; the Script Manager;
                     number formats
    constants.py, types.py
                     constants named as in Inside Macintosh; Point and Rect

  ui/          the Qt side
    display.py       where Mac windows and menu bars go on the host
    window.py        the widgets that show a WindowRecord
    chrome.py        window frames: geometry, hit-testing, painting
    menubar.py       the menu bar and menus, and MenuSelect's tracking
    tracking.py      TrackGoAway, TrackBox, DragWindow, GrowWindow
    bridge.py        running Toolbox calls on the GUI thread
    keymap.py        Qt keys to the Mac's key codes
    theme.py         colours, fonts and metrics the chrome shares
    stdfile.py, printdlg.py, colordlg.py, diskdlg.py, editiondlg.py
                     macrt's own dialogs: Open and Save, printing, the Color
                     Picker, which disk to insert, the Edition Manager's options
    audio.py         the Mac's speaker through the host's sound output
    fonts/           .ttf and .otf files to stand in for the Mac's fonts

tests/         the tests, tests/apps' 68k applications (docs/development.md)
tools/         gen_trapdefs.py
```
