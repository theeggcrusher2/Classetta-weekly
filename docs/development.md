# Working on macrt

Part of [the documentation](README.md).

This page is for changing macrt: setting it up to work on, the tests and
what each one covers, the small 68k applications the tests run, and how
a Toolbox routine is added. [How macrt works](architecture.md) explains
the pieces this page refers to.

- [Setting up](#setting-up)
- [Running the tests](#running-the-tests)
- [What the tests cover](#what-the-tests-cover)
- [The test applications](#the-test-applications)
- [Adding a Toolbox routine](#adding-a-toolbox-routine)
- [Checking a change against real titles](#checking-a-change-against-real-titles)


## Setting up

```sh
python -m pip install -e '.[dev]'       # macrt, and pytest and pytest-xdist
```

The tests need no display: they run Qt offscreen
(`QT_QPA_PLATFORM=offscreen`, which `tests/conftest.py` sets unless
it's set already). To rebuild the test applications you also need
binutils for m68k (`apt install binutils-m68k-linux-gnu`); the built
applications are checked in, so the tests themselves don't.


## Running the tests

```sh
pytest                      # all of them
pytest -n auto              # with pytest-xdist, from .[dev]: on every core
pytest tests/test_qdraw.py  # one file
```

The code is kept to flake8's checks at 100 columns:

```sh
flake8 --max-line-length 100 macrt tests --exclude macrt/m68k/trapdefs.py
```

`macrt/m68k/trapdefs.py` is generated (see [Adding a Toolbox
routine](#adding-a-toolbox-routine)), and isn't edited by hand.


## What the tests cover

Most areas have two kinds of test. One calls macrt's Python model
directly, and checks the records it leaves in guest memory byte for byte
against Inside Macintosh. The other, a `test_*_traps.py`, calls the
traps as a program does, with the arguments on the 68k stack or in
registers, and checks what comes back there. `tests/guest.py` gives
those a process with a Toolbox and enough of a `Runner` to call trap
routines from Python without a processor.

| Area | Files | What they check |
| --- | --- | --- |
| The processor | `test_cpu.py`, `test_cycles.py` | the status register as code sees it, stopping a run at an instruction's end, the speed limit, `TRAPcc` and tracing; the 68000's instruction timings from the M68000 User's Manual |
| The machine under the Toolbox | `test_hardware.py`, `test_screen.py`, `test_adb.py`, `test_scsi.py`, `test_slots.py`, `test_video.py`, `test_startup.py`, `test_shutdown.py` | the VIA, the 512K Mac's screen and the Event Manager's low-memory record of the mouse and keyboard; the screen buffer mirroring the windows; the ADB device table; an empty SCSI bus; the screen card's declaration ROM; the video driver and changing depth; the startup disk; the Shutdown Manager |
| Memory and records | `test_memory.py`, `test_records.py`, `test_toolbox.py`, `test_os_traps.py` | RAM, the Memory Manager and the low-memory globals; the memory map, partitions, windows and menus byte for byte; the OS traps: the Memory Manager's errors, the trap table, Gestalt and `SysEnvirons`, OS events, the clock, VBL tasks, parameter RAM |
| Resources | `test_resources.py`, `test_resource_traps.py`, `test_decompressors.py` | maps in guest memory, purging, templates and the calls built on them; what the traps take and leave; compressed resources expanded by their `dcmp`, and what macrt's System takes from the disk's |
| Files and disks | `test_files.py`, `test_file_system.py`, `test_filecalls.py`, `test_filemgr.py`, `test_cdrom.py`, `test_aliases.py`, `test_drivers.py` | MacBinary, BinHex, AppleSingle/Double, emulator side files, HFS, HFS Plus, MFS and MOOF images (built in `tests/builders.py`); the File Manager's volumes, catalog and host folders; its parameter-block, `HFSDispatch` and `FSSpec` calls; CD-ROM images and their driver; alias records; a program's own drivers |
| QuickDraw and colour | `test_qdraw.py`, `test_qd_traps.py`, `test_pict.py`, `test_pictinfo.py`, `test_icons.py`, `test_color.py`, `test_gdevice.py`, `test_cport.py`, `test_gworld.py`, `test_palette.py`, `test_picker.py`, `test_cursors.py`, `test_desk.py` | drawing bit by bit in 1-bit bitmaps, and regions against QuickDraw's own format; pictures and the Picture Utilities; icons; the colour calls on a 1-bit screen; a Mac II's colour screen, colour ports, pixel patterns, GWorlds, palettes and the Color Picker; cursors; the desk's pattern |
| Fonts | `test_fonts.py`, `test_bitfonts.py` | the standard families and font menus; an application's own bitmap fonts and the System's pattern list |
| Windows, menus and controls | `test_window_traps.py`, `test_wdefs.py`, `test_chrome.py`, `test_display.py`, `test_menu_tracking.py`, `test_mdefs.py`, `test_menucolors.py`, `test_popups.py`, `test_control_traps.py`, `test_controls.py` | the Window Manager's traps; a program's own `WDEF` and the System's for 68k callers; frame geometry and `wHit` codes; real (synthetic) mouse and key input; menus followed by the mouse, `MDEF`s and menu colours; pop-up menu controls; the Control Manager |
| Dialogs, text and lists | `test_dialog_traps.py`, `test_dialogs.py`, `test_textedit.py`, `test_scrap.py`, `test_lists.py`, `test_stdfile_dialog.py`, `test_stdfile_traps.py`, `test_notification.py` | the Dialog Manager; TextEdit's records, line breaks and drawing; the scrap and the host clipboard; the List Manager; Standard File's dialog and its traps; the Notification Manager |
| Processes and communication | `test_process_mgr.py`, `test_aevents.py`, `test_aevent_traps.py`, `test_editions.py`, `test_edition_traps.py`, `test_components.py`, `test_threads.py`, `test_timemgr.py` | MultiFinder's `_OSDispatch` calls; Apple event data, coercions and the Apple Event Manager's traps; the Edition Manager between two applications; the Component Manager; the Thread Manager; the Time Manager |
| Numbers and text | `test_sane.py`, `test_intl.py`, `test_script.py`, `test_numformat.py` | SANE against the Apple Numerics Manual and IEEE's special cases, and its packages from 68k code; the International Utilities; the Script Manager; number format strings |
| Printing and sound | `test_printing.py`, `test_print_traps.py`, `test_sound.py`, `test_sound_manager.py` | print records, the printer resource file, the `.Print` driver and the PDF; the Sound Driver's synthesizers and the speaker; the Sound Manager's channels |
| Whole applications | `test_m68k_apps.py`, `test_viewer.py`, `test_survey.py` | the test applications below on the emulated CPU; the command line, the resource listing and ResourceViewer; the survey tool, with a small survey of the test applications |

`test_display.py` drives the real input path with synthetic Qt mouse and
key events, in both menu-bar modes. It covers:

- the close box (including releasing outside it), dragging, growing, and
  zooming in and out;
- selecting a back window;
- press-drag-release menus, sticky menus, disabled items and ⌘-keys;
- switching apps, the Application menu (switch, Hide Others), the Help
  menu, per-window bars and the bar-only window;
- quitting, and a window that follows a poked record.

## The test applications

`tests/apps` holds small 68k applications written in assembly, each
for one part of the Toolbox or the machine. The tests run them on the
emulated processor and drive them with synthetic clicks and keys:

| Application | What it does |
| --- | --- |
| Hello | sets up the Toolbox, puts up a menu bar and a window, and runs the usual event loop until File ▸ Quit |
| Controls | a push button, a check box, two radio buttons and a scroll bar scrolled through an action procedure |
| Dialogs | a modal dialog from `DLOG` 128, with an edit field, a user item and a hidden item, then a stop alert |
| Edit | a TextEdit window: typing, clicking, shift-extending, and Cut, Copy and Paste through the TextEdit and desk scraps |
| Lists | a List Manager list of twenty cells with a scroll bar, clicked and double-clicked |
| Menus | its own menu bar definition procedure, `MBDF` 128 (`mbdf.s`), installed by `InitProcMenu` |
| Frames | its own window definition procedure, `WDEF` 128 (`wdef.s`) |
| Launcher | answers a Gestalt selector of its own, then `_Launch`es Hello in its place |
| Desk | opens the desk accessory Tally (`tally.s`) from its Apple menu, and has the Desk Manager pass it its events |
| Driver | opens its own driver, `.Echo` (`echo.s`), and calls it synchronously and asynchronously; `.Shy` (`shy.s`) refuses to open |
| Panel | a control panel, `cdev` -4064, for the control panel host |
| AEvents | high-level-event aware: handles the Open Application event, sends itself an event with a list in it, and quits by Apple event |
| ResStub | patches `_ResourceStub` and reads resources through its patch |
| OSPatch | patches an OS trap twice, once going on to the original and once unwinding the dispatcher's frame itself |
| VBL | a VBL task that runs every other tick, during `Delay` too, until it's removed |
| Deferred | deferred tasks, queued by a VBL task through `jDTInstall` and by the application with `_DTInstall` |
| Timer | Time Manager tasks, one priming itself again every 10 ms |
| Tempo | its own routine for the VIA's timer 1 in `Lvl1DT` |
| Vectors | its own handlers in the exception vectors: `TRAP #1`, dividing by zero, `CHK` and the VIA's interrupts |
| Sounds | the Sound Driver's square-wave, four-tone and free-form sounds, then samples written straight into the sound buffer |
| Threads | the Thread Manager's threads, yielding to each other |
| Poll | `GetNextEvent` and `WaitNextEvent` with nothing to give, timed |
| Spin | waits by asking `TickCount` and `Button` over and over, as early games do |
| Unanswered | an update event the program never answers, as a title screen's loop leaves it |

The comment at the top of each `.s` file says in more detail what it
does and why.

To build them you need binutils for m68k (`apt install
binutils-m68k-linux-gnu`). Then run:

```sh
python tests/apps/build.py
```

That assembles each `.s` file and wraps the code, with the resources it
needs, into a MacBinary file next to it. The built `.bin` files are checked
in, so the tests don't need the assembler. `tests/apps/traps.inc` defines
the trap words. Each application is one code segment, and the end of
`build.py` lists the resources each one gets. To try one, run it like any
other application:

```sh
python -m macrt tests/apps/Dialogs.bin --trace-traps
```

Keep what you need in registers the way Inside Macintosh's "Using
Assembly Language" says: a Toolbox routine called through the stack keeps
only A2-A6 and D3-D7. In macrt one that has no result leaves 0 in D0, or
the Resource Manager's leave `ResError` there, but for `ReleaseResource`,
which leaves D0 as it was.


## Adding a Toolbox routine

Every trap's layout, where its arguments are and what it returns, comes
from `macrt/m68k/trapdefs.py`, which `tools/gen_trapdefs.py` generates
from the Multiversal Interfaces:

```sh
git clone https://github.com/autc04/multiversal
python tools/gen_trapdefs.py multiversal
```

A routine is a Python function in one of the `macrt/m68k/glue_*.py`
files, one for each manager, registered with `@trap` under its Inside
Macintosh name. It gets the trap context, a `Runner`, and the arguments
as Python ints (a `Point` for a Point), in the order Inside Macintosh
lists them, and returns the result:

```python
@trap
def TickCount(t):
    """Ticks, as the ROM's has it: what the program reads there itself
    goes on from it."""
    t.process.events.timed()
    return t.mem.r32(lowmem.Ticks)
```

The dispatcher pops the arguments and puts the result where the caller
expects it. An argument the interfaces give as an output register isn't
passed: the function returns the result and then that register's value.
`@raw` registers a function for a trap word that works the registers and
the stack itself. `macrt/m68k/dispatch.py` says more, and [How macrt
works](architecture.md#traps) says what happens around the call.

Write the test with it: a `test_*_traps.py` calls the trap as a program
does and checks what it leaves in memory and the registers. Where Inside
Macintosh or a Technical Note says what the routine does, cite it by
book and chapter in the docstring; where macrt has to decide something
the documentation doesn't say, say that the rule is macrt's own.


## Checking a change against real titles

The tests say a change does what it means to. Whether it helps or harms
real software is for a survey to say: run a list of titles before and
after the change and compare the two (see [Surveying many
applications](survey.md)). A title's entry in [Titles tried so
far](titles.md) says how far it got the last time it was run, and what
to give it on the command line.
