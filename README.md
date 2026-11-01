# macrt

A runtime for classic Mac OS (System 1–7) applications with a PyQt6 front end.

macrt runs an application's own 68k code on an emulated CPU, catches its
A-line traps, and answers them with a Toolbox written in Python, with Qt
showing its windows and menus. There is no disassembly or translation
step. The program runs as it did on a Mac, and the Toolbox it calls into
is ours. Each application's windows sit on your desktop next to your
other programs.

![Three 68k test applications running side by side](docs/screenshots/test-apps.png)

## What runs

Real 68k applications run: TeachText, Microsoft Excel, MacWrite, MacPaint
and Word 1.0 from 1984, and games such as Creepy Castle and Glider+
among them. [Titles tried so far](docs/titles.md) lists each one tried
and how far it gets.

- **The processor** is unicorn's 68020, in 24-bit mode, which tells
  programs it's a Mac Plus's 68000 (or, with `--model ii`, a Mac II's
  68020 and 68881, and with `--model iix` a Mac IIx's 68030 and 68882)
  and stacks that processor's exception frames. The few 68020 and 68881
  instructions unicorn doesn't have run in Python.
- **More than 700 Toolbox and Operating System routines**, from the
  Memory Manager to the Sound Manager:
  - Memory, Resource, Window, Menu, Event, Control, Dialog, List,
    Scrap, File, Device and Process Managers, TextEdit and Standard File;
  - QuickDraw, with real regions, pictures and icons, and on a Mac II
    Color QuickDraw: a colour screen and its video driver, colour ports,
    pixel patterns, offscreen GWorlds, the Palette Manager and the Color
    Picker;
  - the Apple Event Manager, for the Open Application event and an
    application's events to itself, and the Edition Manager's publishers
    and subscribers;
  - SANE's arithmetic, conversions and elementary functions, the
    International Utilities and the Script Manager for Roman text;
  - the Printing Manager, which prints to a PDF;
  - the Sound Driver's three synthesizers, the Sound Manager's channels
    and sampled sounds, and the Mac Plus's sound buffer, played through
    your computer's speakers;
  - MultiFinder-style switching between several applications at once.
- **Files** in MacBinary, BinHex, AppleSingle and AppleDouble, the
  emulators' side files, HFS, HFS Plus and MFS disk images, MOOF floppy
  images and CD-ROM images. An application run from a folder on your
  computer saves its documents there.
- **Not there yet:** Apple events and PPC sessions between
  applications; anything plugged into the serial ports, and AppleTalk;
  the event filter and activation procedures of custom Open and Save
  dialogs; speech. Applications that need them get as far as they can
  without them.

## Quick start

```sh
python -m pip install -e .              # PyQt6, unicorn, rsrcfork, machfs
python -m macrt tests/apps/Hello.bin    # one of the small test applications
```

If Hello opens a window and a menu bar, everything macrt needs is
installed. Then:

```sh
python -m macrt App.bin                         # MacBinary, BinHex, AppleDouble…
python -m macrt "System 6.0.8.dsk" --app TeachText
python -m macrt One.bin Two.hqx                 # several, side by side
python -m macrt App.bin --view                  # its menus and windows, without its code
python -m macrt App.bin --trace-traps           # log every trap it calls
```

[Running a Mac application](docs/running-apps.md) has the whole story.

## The demo

With no file, macrt runs two guest applications written in Python against
the same Toolbox API, which show off the window chrome:

```sh
python -m macrt                          # host windows, one global menu bar
python -m macrt --menubar window         # each window carries its app's menu bar
python -m macrt --host screen            # everything inside one 1-bit Mac screen
python -m macrt --host screen --size 512x342 --classic-menus
```

**Sampler** opens every window kind from its *Windows* menu. **Ticker** is
a clock, so there's a second app to switch to; `--single` leaves it out.
Ctrl stands in for ⌘ (Command), Alt for Option.

| Emulated screen, global menu bar | Host windows, menu bar under each title bar |
|---|---|
| ![](docs/screenshots/screen-global.png) | ![](docs/screenshots/desktop-window.png) |

## Documentation

[The documentation](docs/README.md) starts with a guide to which page to
read for what. In short:

- **[Running a Mac application](docs/running-apps.md):** what to install,
  getting an application to macrt with its resource fork, every option,
  and what to do when it stops.
- **[Titles tried so far](docs/titles.md):** the applications run so far,
  how far each gets, and what to give it on the command line.
- **[What works](docs/toolbox.md):** the Toolbox and Operating System,
  a manager at a time, with where macrt differs from a Mac and why.
- **[How macrt works](docs/architecture.md):** the processor, the trap
  dispatcher, memory, drawing, resources and files, and the source tree,
  for readers who know the 68k and the Mac's insides.
- **[Working on macrt](docs/development.md):** the tests, the 68k test
  applications, and adding a Toolbox routine.
- **[Surveying many applications](docs/survey.md):** running hundreds of
  titles headless and comparing the results before and after a change.

## Next

1. Suspend and resume when a host window gains or loses focus, so the
   clipboard follows without an extra click.
2. What [Not yet](docs/toolbox.md#not-yet) lists.
3. More real applications, fixing whatever each one reaches first (see
   [the ones tried so far](docs/titles.md)).
