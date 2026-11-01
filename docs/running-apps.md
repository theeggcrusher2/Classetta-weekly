# Running a Mac application

Part of [the documentation](README.md).

macrt runs classic Mac applications, the programs written for 68000-family
Macs and System 1 to 7, as windows on your desktop next to your own
programs. It reads the application's resources and loads its 68k code
into an emulated processor. Python answers the Toolbox and Operating
System calls that code makes, and PyQt6 draws the result.

This page covers what to install, how to get an application to macrt in a
form it can read, how to run it, and what to do when it stops.

- [Quick start](#quick-start)
- [What you need](#what-you-need)
- [Which applications run](#which-applications-run)
- [Getting the application to macrt](#getting-the-application-to-macrt)
- [Running it](#running-it)
- [What happens at launch](#what-happens-at-launch)
- [What works](toolbox.md)
- [When it stops](#when-it-stops)
- [Surveying many applications](survey.md), on a page of its own
- [Writing a test application](development.md#the-test-applications)


## Quick start

```sh
python -m pip install -e .              # PyQt6, unicorn, rsrcfork, machfs
python -m macrt tests/apps/Hello.bin
```

Hello is one of the small test applications in `tests/apps`, written in 68k
assembly. It opens a window and puts up a menu bar; File ▸ Quit ends it, as
does ⌘Q (Ctrl+Q on a PC keyboard). If Hello runs, everything macrt needs is
installed.

Then run your own application:

```sh
python -m macrt MacPaint.bin
python -m macrt "System 6.0.8.dsk" --app TeachText
```

`pip install` also puts a `macrt` command on your path, so `macrt
MacPaint.bin` does the same thing. With no file, macrt runs its two
built-in demo programs instead.


## What you need

- **Python 3.11 or later.**
- **PyQt6** (6.5 or later) for the windows and menus.
- **unicorn** (2.1 or later), the 68k processor emulator.
- **rsrcfork** and **machfs**, which read resource forks and HFS disks'
  B*-trees. (The rest of an HFS disk, and HFS Plus and MFS disks, macrt
  reads itself.)

`pip install -e .` installs all four. unicorn has wheels for Linux, macOS
and Windows.

- **To unpack Mac archives** (StuffIt, Compact Pro and so on), get The
  Unarchiver's command-line tool, `unar`: `apt install unar` or `brew
  install unar`.
- **To run without a display** (a server or CI), set
  `QT_QPA_PLATFORM=offscreen`.

### Fonts

The chrome looks right with Chicago (system font) and Geneva (application
font). These aren't bundled. Put `.ttf`/`.otf` files in `macrt/ui/fonts/` and
they load at startup. *ChicagoFLF* is a freely licensed Chicago clone. With a
Mac font present, text is drawn without antialiasing for the crisp 1-bit look.
Without one, macrt falls back to Helvetica at 13px with antialiasing on, because
outline fonts look broken without it. `theme.TEXT_ANTIALIAS = True/False`
forces either. An application's own fonts are drawn from their bitmaps
(see [Text and fonts](architecture.md#text-and-fonts)).

### Wayland and macOS

- **Wayland** doesn't let apps place their own windows. With `--host
  desktop` window drags go through the compositor (`startSystemMove`), but
  windows can't be placed exactly. Use `QT_QPA_PLATFORM=xcb` (XWayland) or
  `--host screen`.
- **macOS** already has a global menu bar at the top of the screen, so use
  `--menubar window` or `--host screen` there.
- With `--host desktop`, the shaped windows and their 1-pixel shadows rely
  on `setMask`, which works without a compositor.


## Which applications run

| What you have | What macrt does |
| --- | --- |
| A 68k application: type `APPL` with `CODE` resources, as for a Mac Plus, SE, II, Classic, LC or a 68k PowerBook | runs it |
| A fat application, with both 68k and PowerPC code | runs the 68k code |
| A PowerPC-only application: a `cfrg` resource and no `CODE` | shows its menus, windows and dialogs from its resources, without running it |
| A self-expanding application, compressed by AutoDoubler | runs it: the application's own decompressor expands its code and resources as they load |
| An application built with MPW's far code model, its `CODE` segments starting with `$FFFF` | runs it when the runtime linked into it loads those segments itself, as Flash 2's does with `ReadPartialResource`; one whose jump table starts with far entries is refused at launch |
| A desk accessory, in the System file or the application's own file | opens it from the application's Apple menu, or wherever the application lists it; it can't be run by itself |
| A control panel: a `cdev` file with code of its own | runs it in a window of its own, as System 7's Finder did (see [Control panels](#control-panels)); a control panel that's an application, as System 7.5's Desktop Patterns is, runs as one |
| Extensions (`INIT` files) | runs a control panel's own `INIT` resources as the panel opens, as a Mac runs them as it starts up (see [Control panels](#control-panels)); an extension that's a file of its own isn't run |

How far an application gets depends on the Toolbox calls it makes (see
[What works](toolbox.md)). Programs that keep to the Toolbox do best:
word processors, utilities, and games that draw through QuickDraw.
Programs that need Color QuickDraw are less likely to work. Games from
the Mac's first years, which draw straight into the screen and keep time
by its hardware, run with `--host screen --size 512x342`; see [Games from
1984](#games-from-1984).

### Tried so far

[Applications tried so far](titles.md) lists the titles tried, where
they came from, how far each gets, and what to give it on the command
line. When you try another, the list of missing calls it prints when it
quits (see [When it stops](#when-it-stops)) is what to report.


## Getting the application to macrt

A Mac file has two forks. The **data fork** is what every other computer
sees. The **resource fork** holds almost everything that makes an
application: its code (`CODE` resources), menus, windows, dialogs, icons
and strings.

If you copy a Mac application to a PC or a Unix machine the ordinary way,
or download it from somewhere that wasn't careful, the resource fork is
lost. What's left is a file of zero bytes, or a little data, that nothing
can run.

So the application has to arrive in a form that carries both forks. macrt
reads all the common ones, and you don't have to say which one you have;
it works it out from the contents:

| Form | Looks like | Where it comes from |
| --- | --- | --- |
| MacBinary I, II or III | `App.bin` | downloads; most Mac transfer programs could write it |
| BinHex 4.0 | `App.hqx` | downloads, Info-Mac and Usenet archives |
| AppleSingle | one file holding both forks | A/UX, some archives |
| AppleDouble | `App` next to `._App`, `.AppleDouble/App` or `App.rsrc` | `unar`, netatalk, macOS copying to a FAT disk |
| Basilisk II and SheepShaver shared folders | `App` next to `.rsrc/App` and `.finf/App` | the emulators' host folder |
| Executor | `App` next to `%App` | Executor's host folder |
| a resource fork on its own | `App.rsrc`, or any file that is one | `DeRez`/`Rez` tooling, resource editors |
| a macOS file | `App` itself, through `App/..namedfork/rsrc` | a Mac running macOS |
| an HFS, HFS Plus or MFS disk image | `Disk.dsk`, `.img`, `.image`, `.hfs`, `.iso`, `.cdr`, `.toast` | raw and DiskCopy 4.2 floppy images (800K and 1.4MB HFS, 400K MFS), with each sector's 12 tag bytes or without them, HFS and HFS Plus CD images, partitioned hard disk images. A CD's image, or any other of 16 MB or more, is mapped rather than read in, and a file's forks are read from it only when they're wanted, so only what's used of it is in memory. A raw CD image, which keeps each of the disc's 2352-byte sectors whole, is read in as the 2048 bytes of data in each. |
| a MOOF floppy image | `Disk.moof` | Applesauce, imaging a 400K or 800K floppy's tracks bit by bit |

For AppleDouble and the emulator folders, give macrt the data-fork file
(`App`), and keep the side file where it is.

### Checking a file

```sh
python -m macrt Game.bin --list
```

`--list` prints the file's type and creator and every resource it holds,
then exits. For menus, windows, dialogs, `SIZE` and `vers` resources it
also says what they contain. It's the quickest way to check that an
application arrived with its resource fork: a `CODE` line means there is
68k code to run.

```
$ python -m macrt tests/apps/Hello.bin --list
Hello: application, type 'APPL', creator 'HELO'
  data fork 0 bytes, resource fork 1,007 bytes, 6 resources
  CODE      0       24  purgeable,locked,preload '' jump table, 1 entries
  CODE      1      460  purgeable          'Main'
  MENU    128       40                     Apple: About Hello…, -
  MENU    129       29                     File: Quit
  WIND    128       24                     'Hello', 260x160, procID 0
  SIZE     -1       10                     256K preferred, 128K minimum
```

Each line gives the resource's type, ID, size in bytes, attributes and
name, then what it holds.

If macrt says the file *has no resource fork*, the fork was lost on the way.
Go back to the archive or disk image it came from. If it says the
*resource fork is in its data fork*, something copied the file without
keeping its two forks apart, and a Mac couldn't launch it either. McBug on
both BMUG PD-ROM discs was mastered like this.

### Archives

Most Mac software online is in an archive: `.sit` (StuffIt), `.cpt`
(Compact Pro), `.sea` (self-extracting), `.sit.hqx`, or a `.zip` with a
`__MACOSX` folder. macrt doesn't unpack archives; it tells you when you've
given it one. Unpack it with The Unarchiver, which keeps the resource
forks:

```sh
unar Game.sit.hqx               # decodes the BinHex and unstuffs what's inside
python -m macrt "Game Folder/Game"
```

On Linux and Windows, `unar` writes each resource fork, with the file's
type and creator, as an AppleDouble file named `App.rsrc` next to `App`.
An application's data fork is usually empty, and then `unar` writes only
`App.rsrc`. Give macrt either `App` or `App.rsrc`; both find the same
file. On macOS, `unar` writes real resource forks.

Don't use `unzip` on a zip from a Mac: it leaves the resource forks in the
`__MACOSX` folder, away from their files. `unar` puts them back together.

For example, Vector 2.5, a small program from the Info-Mac archive:

```sh
$ curl -O https://ftp.funet.fi/pub/mac/info-mac/game/arc/vector-25.hqx
$ python -m macrt vector-25.hqx
macrt: vector-25.hqx is a StuffIt archive: unpack it first, for example with The Unarchiver (unar vector-25.hqx)
$ unar vector-25.hqx
$ ls "vect_250 folder"
Vector 2.5 Doc  Vector 2.5.rsrc
$ python -m macrt "vect_250 folder/Vector 2.5"
```

### Disk images

Many collections come as disk images: floppies, CD-ROMs and hard disks.
macrt reads HFS, HFS Plus and MFS volumes directly, so there's nothing to
extract:

```sh
python -m macrt "System 6.0.8.dsk"                  # a disk with one application
python -m macrt "System 6.0.8.dsk" --app TeachText
python -m macrt Tools.img --app "Tools:Disk Tools:Disk First Aid"
python -m macrt Paint.dsk                           # a 400K MFS floppy from 1984
```

`--app` takes the application's name, or its Mac path with folders
separated by colons, starting from the disk's name or from any folder
below it. Names are matched without regard to case. Run a disk that holds
several applications without `--app`, and macrt lists them:

```
$ python -m macrt mac6-0-8.dsk
macrt: mac6-0-8.dsk holds 7 applications; pick one with --app:
  Mac System 6.0.8:Applications:Microsoft Excel:Microsoft Excel
  Mac System 6.0.8:Games:Creepy Castle:Creepy Castle™
  Mac System 6.0.8:Games:Operation Tsunami:Operation Tsuanmi
  Mac System 6.0.8:LabelWriter II Folder:LabelWriter II App
  Mac System 6.0.8:LabelWriter II Folder:TeachText
  Mac System 6.0.8:System Folder:PrintMonitor
  Mac System 6.0.8:TeachText
```

When two applications have the same name, as the two TeachTexts do here,
`--app TeachText` takes the one nearest the top of the disk
(`Mac System 6.0.8:TeachText`). Give more of the path to get the other:
`--app "LabelWriter II Folder:TeachText"`.

#### Image formats

macrt reads raw images and DiskCopy 4.2 images, including images of
partitioned disks (an Apple partition map with an HFS or HFS Plus
partition). A CD's image can also keep the disc's sectors whole, 2352
bytes each, starting with their sync and header (ECMA-130); macrt takes
the 2048 bytes of data in each (after the header in a mode 1 sector, and
after 8 more bytes in a mode 2 one) and mounts the result as a CD-ROM,
whatever the image is called.

macrt can't read compressed images yet: NDIF (DiskCopy 6) and UDIF
(`.dmg`, `.smi`). Convert them to a raw image first.

#### CD-ROM images

An image whose name ends in `.iso`, `.cdr` or `.toast` is mounted as a
CD-ROM, whatever its size. Its driver has a device control entry in the
unit table, named `.AppleCD`, the Apple CD-ROM driver's name in Technical
Note DV 22, "CD-ROM Driver Calls", so a program that checks its disc's
driver by name finds it, as Lode Runner: The Legend Returns does. The
image is taken to be the disc's one data track, starting at 00:02:00: the
driver gives its table of contents (`ReadTOC`), answers the audio calls
with `paramErr`, as the Note has them for a data track, and keeps the
volume `AudioControl` sets for `ReadAudioVolume`. macrt doesn't make up
audio tracks an image hasn't got, so a program that plays music from its
disc's audio tracks gets those errors.

The volume says it's locked by hardware: bit 7 of its attributes
(`vcbAtrb` in its volume control block, and `ioVAtrb` from `PBGetVInfo`),
"Set if the volume is locked by hardware" in Inside Macintosh: Files
("Volume Control Blocks"), is set, as Technical Note FL 530 ("File Manager
Volume Handling Q&As") says a CD-ROM's is, and macrt takes no changes to
it: making, deleting, renaming or moving a file or folder, changing a
file's information or opening one to write says `wPrErr`, the File
Manager's error for a volume locked by hardware, and a file opened with
`fsCurPerm` is opened to be read. Treasure Galaxy! tells its disc from a
hard disk by whether it can make a file on it.

An image named any other way (`.img`, `.dsk`, `.image`, `.hfs`) is a
floppy up to 1.4MB and a hard disk above that, with no driver in the unit
table, since an HFS CD-ROM's image holds nothing that says it came from a
CD.

#### HFS Plus

HFS Plus is the format Technote 1150 ("HFS Plus Volume Format") describes,
introduced with Mac OS 8.1; some later CDs of earlier software use it.
macrt reads an HFS Plus volume, or one inside an HFS wrapper (TN1150's "HFS
Wrapper"), into the same form as an HFS one, so a program sees it as an
ordinary HFS volume. A hard link (TN1150's "Hard Links") is the file it
links to, and the folder of linked files is left out. Dates are given in
local time, as HFS keeps them (TN1150's "HFS Plus Dates"). An HFS Plus name
can be 255 Unicode characters, and an HFS one is 31 characters of Mac Roman
with no colon in it: a name that can't be written that way, or that would
then be the same as another in its folder, is cut short and given its
file's or folder's catalog node ID in hexadecimal, as in
`A Name Far Too Long For An H#16`, so that every file has a name of its
own. That rule is macrt's own.

#### Floppies, track by track

It also reads Applesauce's MOOF images of 400K and 800K floppies. These
keep each track as the bits the drive reads, as the moof-a-day
collection keeps its floppies; macrt decodes them back to sectors, the
way a Mac's floppy drive does, when it opens one. A sector that can't be
read is left as zeros, and a program that reads it through the floppy disk
driver is told it can't be read, as on a Mac. A few keep a track instead
as the flux changes Applesauce timed going by, where bits can't say what's
there; macrt reads those as a Mac's floppy controller would. MOOF images
of 1.4MB floppies aren't read: convert them to a raw image first.

A raw or DiskCopy image of a 400K or 800K floppy has only the sectors,
not the tracks, and macrt gives the drive the tracks a Mac's format
would have written for them: each sector's address field and data
field, two sectors apart round the track. A program that reads the disk
itself finds what it would on a copy of the disk: MacGolf's copy
protection reads a sector with a routine of its own that reads only the
sector's marks, and leaves the sector where it was. Since the image
didn't keep the marks, the ROM's routine reads a data field with whatever
marks a program gives it: The Slide Show Magician reads its key file
through marks its master disk has there.

#### MFS disks

MFS disks are the 400K floppies of System 1 to 4, from 1984 to 1987: the
first MacPaint and MacWrite disks, and most early games, came on them.
MFS has no folders of its own. The folders you saw in the Finder were kept
in its Desktop file, and applications never saw them, so macrt puts every
file at the top of the disk and `--app` takes just the name. An
application on an MFS disk sees an ordinary HFS volume, as it would have
on a Mac Plus.

#### Disks that start up into a game

A few early games took the Finder's place on their disk, so that a Mac
started from it went straight into the game. Given a disk without
`--app`, macrt runs the program the disk starts up into, as its boot
blocks name it, when that isn't Apple's Finder, and otherwise the disk's
only application.

#### Copy protection

Some early programs check that they're on their original floppy, by
reading marks a copier can't copy. From a MOOF image, which keeps what's
on each track:

- A check that reads through the floppy disk driver gets what's on the
  original, and so does one that puts its own routine in the driver's
  place to read past a sector's address field, as Excel 1.0 does, or to
  give the ROM's routine marks of its own to look for, as Dollars and
  $ense and PFS:FILE do.
- A check that times the disk as it turns, through the ROM's routines that
  find each sector's address field as it comes round and step the head, as
  Mac Attack's does, sees the original's timing: the disk turns at the
  speed its zone of tracks did.
- Where a track goes on long with no flux change, the drive reads noise
  there, different bits each time, and a check that reads it twice, as
  Q-Sheet's does, sees that it's the original; a few zeros together the
  drive reads as written, so a check that counts a turn of a track whose
  bytes are further apart than a disk's own, as The Ancient Art of War's
  does, counts what the original gives.
- A check that steps the head itself and counts the bytes the floppy
  controller reads from the track it moved to, as PageMaker's does a track
  in from its key sector, sees them go by as the disk turns, and one that
  times them with the VIA's timer, as Jazz's does, counts what the
  original gives: the VIA and the drives keep one clock.
- A check whose own routine reads a sector bit by bit as the floppy
  controller shifts them in, as Shufflepuck Cafe's does, sees the
  original's bits too. It stops the shifting for as long as its own
  instructions take, and macrt counts what each instruction would take a
  Mac, so the same bits go by unread as on the original.
- A check with its own routine to find a sector's address field, as
  Dragonworld's has, is called for each sector the driver reads.
- A key track whose flux changes come a bit cell and a half apart, as
  Fraction Action's, MathTalk's and KidTalk's do, reads differently each
  time, as on the original: the floppy controller reads each such change
  as one cell after the last or two, as it comes a little early or late,
  and their check reads the track twice and sees the difference that a
  copy, written as bits, can't have.
- A check that steps the head through the drive's registers, waits through
  the ROM's WakeUp and expects a given sector next, as Moebius's does,
  sees the skew from one track to the next that the original has: the
  drive says at once that a step is over, and the disk driver's step and
  settle times, which WakeUp waits for, are 12 and 30 ms. Those are
  macrt's choice, within what such a check allows, not times known from
  the ROM.

Other checks that drive the floppy drive's hardware themselves still fail:
the program shows its own "damaged disk" or "not an original" alert, or
asks for its master disk.

### Where to find software

Use software you have the right to run. Apple's own system disks, such as
System 6.0.8, are a good first test: they include TeachText and a few
small utilities. The Info-Mac archive, mirrored for example at
`https://ftp.funet.fi/pub/mac/info-mac/`, holds thousands of freeware and
shareware programs as `.hqx` files: unpack them with `unar` as above.
Small black-and-white programs are the likeliest to run. The Internet
Archive keeps images of many original 400K floppies, as raw `.dsk` or
DiskCopy images that macrt opens as they are, and as MOOF images in its
moof-a-day collection, about 150 early Mac titles. Its Macintosh Software
Library holds many more, as DiskCopy images (`.dc42`, `.img`) or raw
`.dsk` files; where an item has several, one named `00playable` is the
disk to start from.


## Running it

```sh
python -m macrt FILE... [--app NAME]... [options]
```

| Option | What it does |
| --- | --- |
| `--app NAME` | Picks the application on a disk image, by name or Mac path. A document's name launches the application that made it, with the document open (see [Opening a document](#opening-a-document)). Repeat it to run several. |
| `--list` | Lists the file's resources and exits. |
| `--view` | Shows the application's menus, windows and dialogs from its resources, without running its code. Useful when the code won't run. |
| `--trace-traps` | Logs every Toolbox and OS trap the application calls. |
| `--system FILE` | Uses a real System file, or a disk image holding one, in place of macrt's own small one. See below. |
| `--host desktop` | Each Mac window is a window on your desktop, with one menu bar for the front application. This is the default. |
| `--host screen` | Everything is drawn inside one window that is the Mac's screen. |
| `--menubar global` | One menu bar, the front application's. This is the default. |
| `--menubar window` | A menu bar under each window's title bar. |
| `--size WxH` | The Mac screen's size, such as `512x342` or `640x480`. The default is your screen's size with `--host desktop`, and 640×480 with `--host screen`. |
| `--speed N` | Runs 68k code at about N times a Mac Plus's speed, by the cycles its instructions would take a 68000. Games from 1984 time themselves by how fast the processor goes, and are unplayable at full speed; `--speed 1` is a Mac Plus. The Toolbox's routines, which macrt runs itself, aren't slowed, and above about 3 the counting can't keep up. The default is as fast as it goes. |
| `--memory K` | Gives the application K kilobytes of memory, in place of what its `SIZE` resource asks for, as the memory size in the Finder's Get Info does. A `SIZE` resource from before MultiFinder can ask for too little: the Finder of the time gave an application all the memory there was. A control panel runs in the memory of the program that hosts it, so its size goes to the application it launches from its own file, as After Dark 4.0's Demo does. |
| `--no-sound` | Keeps the Mac's sound off your computer's speakers. See [Sound](#sound). |
| `--record-sound FILE.wav` | Writes the Mac's sound to a WAV file as it plays, as well. |
| `--model plus` | Says the Mac is a Mac Plus, with a 68000 and no floating-point unit. This is the default. Programs from before the Mac II need it: a 68000's exception frames are smaller than a 68020's, and MacWrite 4.5 pops its own. |
| `--model ii` | Says the Mac is a Mac II, with a 68020 and a 68881, for a program that needs one. The instructions run are the same either way. Some games pick how to draw by the processor: on a 68000 they draw straight into the screen's memory as if it were a 512×342 Mac's, which on a bigger screen puts their picture in the wrong place. Give them `--model ii`, or `--size 512x342`. A Mac II's screen is in colour: see `--depth`. |
| `--model iix` | Says the Mac is a Mac IIx, with a 68030 and a 68882, for a program that asks for one, such as Fine Artist; otherwise it's the Mac II above, with its colour screen. The instructions run are still the same, and Gestalt says there's no memory management unit. |
| `--depth N` | With `--model ii` or `iix`, the screen's bits a pixel: 1, 2, 4 or 8 for that many bits of an index into the screen's colour table, 16 or 32 for colours themselves. The default is 8, 256 colours. A Mac Plus's is 1, black and white. |
| `--row-bytes N` | How many bytes apart the screen's rows are in its memory at the depth it starts at, as if its memory were wider than what shows; when a program changes the depth, they're as many pixels apart as before (with `--depth 8 --row-bytes 1024`, 128 bytes at 1 bit a pixel, 512 at 4). QuickDraw says how far apart they are (`screenBits.rowBytes`, and the `rowBytes` of the screen's `PixMap`), and most programs that draw straight into the screen ask it; a few take the rows to be some fixed number of bytes apart instead, and draw each row in the wrong place unless the screen's are so far apart. The default is as few as a row needs: 80 for a 640-pixel row at 1 bit a pixel, 640 at 8. |
| `--classic-menus` | Menus stay open only while the mouse button is held, as before System 7. |
| `--rainbow` | Shows the striped Apple logo in the menu bar. |
| `--single` | With no file, runs only the demo's Sampler, not its Ticker too. |

Quit an application from its own File menu, or close macrt. Ctrl+C in the
terminal quits macrt and everything running in it.

On a PC keyboard, **Ctrl** is the Mac's Command key (⌘), **Alt** is Option,
and the Windows or Super key is Control. On a Mac keyboard, the keys are
the Mac's own.

The keys have the key codes of the modelled Mac's keyboard. A Mac Plus's
arrow keys are $46, $42, $4D and $48, the codes its keypad's +, *, / and =
send with Shift; a Mac II's ADB keyboard has $7B to $7E for them. The
keypad's keys have codes of their own, apart from the same digits on the
main row, for games that steer by the keypad. While an arrow key is down,
it is in the `KeyMap` by both keyboards' codes, since games written for
the one keyboard read the other's.

### Several applications at once

```sh
python -m macrt Hello.bin Dialogs.bin
python -m macrt mac6-0-8.dsk --app TeachText --app "Creepy Castle™"
```

Every file you name is launched, in order. When disk images are among
them, each `--app` is looked for on the images in turn and taken from the
first one that has it. Without `--app`, each image gives its only
application, unless an application file is among the files you name: the
images are then its disks, as a CD is for a copy of its program on the
Mac's own disk.

An image nothing is launched from, one with no application on it or, with
`--app`, none of the names, is a disk of documents. Every image is in a
drive from the start, the first in the internal drive, so a game on two
floppies finds its second disk when it asks for it by name, or puts it in
through a volume control block of its own, as Uninvited does:

```sh
python -m macrt "BDC 1.moof" "BDC 2.moof" --app "Beyond Dark Castle" --host screen
```

A program that ejects a floppy gets it back a second later, as if you had
put it back in. The drive then posts a disk-inserted event, and the disk
is mounted again. A game that ejects its disk and waits for a disk, as
Seven Cities of Gold does for its map disk, goes on with the same disk.

A game on more disks than there are drives ejects a disk, unmounts it
(`_UnmountVol`) and asks for another. Then a dialog asks which of the
disk images you named goes in that drive, the next one in turn chosen.
Insert puts in the disk you choose, and Put Back puts back the disk that
came out. If the disk you choose is in another drive, it comes out of
that drive, and the disk it replaces goes in there, unmounted. Without a
display to ask on, the next disk in turn goes in. Wizardry VI is played
this way:

```sh
python -m macrt A.moof B.moof C.moof D.moof E.moof F.moof --app Bane --host screen
```

The applications run side by side, as under MultiFinder. Each has its own
memory partition, A5 world, low-memory globals and menu bar. The menu bar
belongs to the front application, and clicking another application's
window brings that application to the front.

An application that launches another one (with `_Launch`) gets it the same
way. A `_Launch` without `launchContinue` replaces the application, as on a
Mac. System 7's `LaunchApplication` works too: its parameter block names
the application with an `FSSpec`, and gets back the new process's serial
number and the partition sizes from its `SIZE` resource.

### Opening a document

`--app` can name a document on a disk image instead of an application.
macrt then launches the application that made it, the one whose creator
the document has, from the same disk if it's there or else from the other
images, and opens the document with it, as double-clicking the document
does in the Finder. Mac Pro Football's game is a document of this kind:
a program for Microsoft BASIC's run-time, which is the application.

```sh
python -m macrt "Mac Pro Football.dc42" "Team Files.dc42" --app MSFL --host screen
```

The application finds the document in its Finder information, the handle
that `_CountAppFiles` and `_GetAppFiles` read (also at 16(A5)), with the
message to open it rather than print it. Each document is listed with a
working directory for the folder it's in. A System 7 application that
handles Apple events gets an Open Documents event for it instead of Open
Application.

A HyperCard stack is a document of HyperCard's. The Manhole's disk has
only its stacks, so give HyperCard's disk after it:

```sh
python -m macrt TheManhole.img Culture.img --app "the Manhole" --host screen
```

Documents made by the same application, named one after another, open in
one launch of it, as when they're selected together in the Finder. An
`--app` naming the application itself is a launch of its own, with no
documents, unless documents follow it: a document whose creator is an
application named before it opens in that one. This picks which of two
HyperCards opens a stack:

```sh
python -m macrt StrangerThings.img HypercardBootVolume.img \
    --app "HyperCard 2.4:HyperCard" --app "Stranger Things" --host screen
```

### Documents and saving

An application starts in its own folder, as when the Finder opens it, and
its Open and Save dialogs start there too. These are macrt's own dialogs,
not the application's. Where a file it saves ends up depends on where the
application came from:

| The application came from | What happens to files it writes |
| --- | --- |
| a folder on your computer | They're written into that folder. A file read as MacBinary or AppleSingle is written back the same way. Any other file is written as its data fork under its own name. Its resource fork, type and creator go in the side file it already had, such as `unar`'s `name.rsrc`, or for a new file in an AppleDouble `._name`. macrt reads all of these back the next time. Symbolic links in the folder aren't followed: applications don't see what they lead to, and nothing is read or written through one. A side file that isn't the file's own, such as a `name.rsrc` that is a file of its own, is left alone. |
| a disk image | The changes are kept in memory and lost when macrt quits. The image file itself is never changed. A CD's image takes no changes at all (see [CD-ROM images](#cd-rom-images)). |

A BinHex file is text, not a Mac file, so macrt treats it as locked. Some
applications keep their settings in their own resource fork, and can't
save them while they run from a `.hqx`. Unpack it with `unar` first.

Applications also see a startup disk called **Macintosh HD** with a System
Folder. Preferences files they write there last only until macrt quits.

### Printing

macrt's printer prints to a PDF. Page Setup and Print show macrt's own
dialogs rather than a printer driver's:

- **Page Setup** picks the paper (US Letter, US Legal or A4) and whether
  the page is tall or wide.
- **Print** picks all the pages or a range of them, the number of copies,
  and the PDF to save. It's named after the front window, in the folder
  you started macrt from (`Untitled.pdf`, or `Untitled 2.pdf` if that's
  taken). Choose… picks another place.

When the PDF is written, macrt says so:

```
macrt: printed 2 pages to /home/you/Untitled.pdf
```

The printer says it's an ImageWriter that prints at 72 dots per inch, so
every application draws its pages with QuickDraw, as it does on the screen.
The PDF's pages are black and white at 288 dots per inch. Applications from
1984 find the printer through their own glue code, as they did on a Mac
(see [What works](toolbox.md)). MacPaint prints a bitmap at a time,
straight to the printer driver, and shows no Print dialog. Its PDF gets the
name the dialog would have given it. The printer is the same with a real
System file (`--system`), whose own printer drivers would drive a serial
port.

### A real System file

Without `--system`, applications run with macrt's own small System file.
It has what applications most often ask a System file for: the standard
window, menu and control definitions, the packages (`PACK` 0 to 15), the
stop, note and caution icons (`ICON` 0 to 2), the standard cursors
(`CURS` 1 to 4: I-beam, cross, plus and watch) and the US international
resources (`INTL` 0 and 1). It also has the pattern list (`PAT#` 0) that
paint programs show as their palette, and the desktop's gray and light
gray (`PAT ` 16 and 17). Its icons, cursors and patterns are
drawn for macrt. They're kept in memory, not in a file, but as with a
real System file's, one a program releases is read in again when it asks
for it: Aladdin Activity Centre releases the watch cursor and gets it
again with `GetCursor`. Some applications look for resources that only a real
System file has, such as other icons, strings (`STR#`), the fonts
themselves and the Script Manager's `itl` resources.

When the applications come from a disk image that has a System file of
its own, macrt's System gets that file's fonts of the families it hasn't
got, up to 128K of them: programs drew in the fonts of the System they
started up from, and some brought their own there, as Mac Attack's tanks
and landscape are characters in fonts of its disk's System. Fonts of the
standard families stay macrt's, but the monospaced ones, Monaco and
Courier, are measured with the widths of that System's strikes of them,
as with a real System file (below): Hacker types each letter in Monaco 9
and its cursor after it, then backs up six pixels, Monaco's width, to
type the next letter over the cursor. And lines in a standard family
are as far apart as that System's strikes of it set them: `GetFontInfo`
gives their ascent, descent and leading rather than those of your
computer's font, a 16-pixel line for Chicago 12, so that The Venice
Project's prompt goes under the box it asks you to type in, not across
it.

It gets that System's drivers too, those named with a period, such as
a speech driver, and the resources they load that aren't of the kinds a
System file of Apple's has, up to 128K: Speller Bee speaks its words
through the `.SAY` driver its disk's System has, which loads its code
from `OVLY` resources there.

And it gets that System's decompressors, its `dcmp` resources. Many of
System 7's resources are compressed, as System 7.5's utilities' are:
macrt expands those it can itself, and for the others runs the `dcmp`
that the resource's header names, on the 68k, as the Resource Manager
did.

The System version that Gestalt, `SysEnvirons` and the low-memory
`SysVersion` give is that System's, from its `vers` resource, or a real
System file's, given with `--system`, from 7.0.1 to 7.6.1: System 7.5's
utilities look for 7.5. Without one, or with an earlier System, it's
7.0.1.

For everything else, give them a real System file:

```sh
python -m macrt Game.bin --system "System 6.0.8.dsk"
python -m macrt Game.bin --system "System Folder/System"
```

The System file's resources then sit at the bottom of every
application's resource chain, as on a Mac, and its desk accessories are
in the Apple menu. Chicago, Geneva and the other standard fonts are
still drawn with your computer's fonts, but measured with the widths of
the System file's own bitmap fonts, at the sizes it has them, so that
text lines up as it did on a Mac. The fonts in the suitcases in the
Fonts folder beside the System file are the System's too, up to 192K of
their bitmap fonts (not their TrueType outlines): System 7.5's System
file has Geneva only at 9 and 12 points, and Courier and Helvetica only
in that folder, which HyperCard Player's stacks ask for. (A standard
font the application carries in its own file, at the size asked for, is drawn from its
bitmap, as Balance of Power's New York is, and Gemstone Warrior's
Chicago, which it makes the application font by setting `ApFontID`. An
application's own Chicago 12 is the one its menus are drawn in, as on a
Mac: The Fool's Errand's menu titles are Roman numerals in its font.)
Its 68k definition procedures and packages aren't run: windows, menus and
the standard controls are always drawn by macrt, and the package calls
are always macrt's. Anything above
that the real System file lacks, macrt adds.

The Finder beside the System file, if there is one, is in the System
Folder with it, where a program that opens it by name finds it from its
own folder, and changes to it stay in memory, as the System file's do:
FinderColors, from BMUG's PD-ROM Volume 2, opens it to read and change
the colours of the Finder's Color menu. macrt doesn't run the Finder;
applications start as `--app` names them.

A System 7 file's System heap has room to grow, as a real one's has
("the system heap expands dynamically to meet your request", Inside
Macintosh: Memory, "Allocating Memory at Startup Time"): up to 1.5 MB,
with applications' partitions above it, where macrt's own System keeps
to 512K. HomePublisher marks its code segments to be loaded into the
System heap, and couldn't start in 512K.

### Control panels

A control panel, a `cdev` file in the Control Panels folder, runs as
System 7's Finder ran one: in a window of its own, with the panel's code
called for whatever happens there (Inside Macintosh: More Macintosh
Toolbox, Control Panels). Name it with `--app`, from a disk with
System 7 on it:

```sh
python -m macrt "System 7.5.3.img" --model ii --depth 8 --size 640x480 \
    --app "System Folder:Control Panels:Date & Time"
```

The window is made of the rectangles in the panel's `nrct` resource, its
text in the font its `finf` names, or in 9-point Geneva, the application
font, without one. Its close box, File › Close or
File › Quit closes it, and the Edit menu's commands go to the panel. A
panel whose `mach` resource says to ask it whether it can run on this
Macintosh is asked, and one that says no isn't opened: macrt says it
"can't be used with this Macintosh".

A panel with `INIT` resources of its own, the code a Mac runs from it as
it starts up, has them run first, each called as at startup (Inside
Macintosh IV, The System Resource File, "Initialization Resources").
One that stops with a system error is left out, as if it hadn't been
loaded: the traps it patched are put back, and what it gave Gestalt
goes. If the panel then stops as it opens, or later in code its `INIT`
resources left behind, it's opened again without them, as on a Mac
started with its extensions off, and macrt's log says so. Traps an
`INIT` patched with code in the system heap stay patched for the
applications launched after; a patch in the panel's own memory goes
when the panel closes. Extensions that are files of their own aren't
run.

From System 7.5.3's disk, with the options above:

| Control panel | What happens |
| --- | --- |
| Color, Date & Time, Easy Access, Extensions Manager, General Controls, Keyboard, Map, Memory, Mouse, Numbers, Sound, Startup Disk, Text | Run. Sound lists the alert sounds, and its pop-up menu switches to its other views, which are components in its own file (see [What works](toolbox.md)). Startup Disk lists the disks. |
| Apple Menu Options, CloseView, WindowShade | Run, their `INIT` resources run first, and open to their settings. |
| AutoRemounter, Control Strip, Macintosh Easy Open, PC Exchange | Open, but each says first that it isn't installed: their `INIT` resources call on parts of the System that macrt hasn't got, and stop, so the panel runs without them. |
| Speech | Opens, but says it needs text-to-speech or speech recognition software, which macrt hasn't got. |
| Launcher | Opens its window, with nothing in it. It shows what is in the Launcher Items folder of the System Folder in use, which is macrt's own on Macintosh HD, not the disk's, so it makes an empty one. |
| 601 Processor Upgrade, Auto Power On/Off, Brightness, Monitors, Password Security, PowerBook, PowerBook Display, PowerBook Setup, Power Macintosh Card, Screen, Serial Switch, Trackpad | macrt says each can't be used with this Macintosh, a Mac II with macrt's screen. |
| File Sharing Monitor, Labels, Sharing Setup, Users & Groups, Views | Refused: they have no code of their own for macrt to run. |
| Desktop Patterns, Energy Saver, Monitors & Sound | Aren't `cdev` files. Desktop Patterns is an application, and runs as one (see [Tried so far](titles.md)). |

A control panel on a disk of its own runs the same way. ScreenChooser 3.1
(`ScreenChooser.img --app ScreenChooser`, with the options above) shows
its shareware notice, and OK opens its General Options: its check boxes
check and uncheck, its Quick Help says what each is for, and Help shows
its user's guide. Backdropper and Set Backdrop, beside it, say
ScreenChooser isn't running: they're programs of their own, and
ScreenChooser's `INIT` resources run only as its control panel opens.

After Dark 2 (`AfterDark.img --app "After Dark"`, with the options
above) opens with its modules listed, Flying Toasters picked. Demo
shows the module, its toasters flying across the screen, and a click
brings the panel back.

After Dark 3.0 (`AfterDark32.img --app "System Folder:Control
Panels:After Dark 3.0"`, with the options above) says first that it
can't run, that it needs System 6.0.7 and MultiFinder: its `INIT`
resources look for MultiFinder by asking `GetTrapAddress` for
`_OSDispatch`, which macrt says isn't there (see [What
works](toolbox.md)). Its panel runs all the same. It says the copy is
unregistered, and OK opens it with its modules listed, Flying Toasters
Pro picked. Demo shows the module, its toasters flying across the
screen around the panel's settings. The modules' libraries go in
temporary memory.

After Dark 4.0's demo disk (`AfterDark40.img --app "System
Folder:Control Panels:After Dark 9" --memory 4096`, with the options
above) says first, for the same reason, that it needs System 7.0 or
later, and its panel opens with its modules listed. Demo launches the
application in the panel's own file, and the panel quits, as a program
that calls `Launch` does. Flying Toasters needs 256 colours, and about
3.3 MB for its toasters, which it takes from the application's heap
when there's no `_OSDispatch` for temporary memory. That application's
`SIZE` resource asks for 600K, and the module says it's out of memory,
so `--memory` gives the file a bigger memory size, as Get Info would.
With it, the toasters fly, the words of their song along the bottom of
the screen, with the module's settings in front.

The Simpsons Screen Saver's disk (`AfterDarkSimpsons.img --app "System
Folder:Control Panels:After Dark 9"`, with the options above) has
After Dark 4.0's panel with modules of its own, in the Simpsons folder
of the panel's pop-up menu. Their library takes temporary memory
through `_OSDispatch`, so they run without `--memory`. Demo shows
Chalkboard's lines being written on the board, Lisa's Mood Swings'
Lisa playing her saxophone, and Itchy and Scratchy on a desktop of
their own. That desktop's folders and Trash are the System's icons:
with `--system` given the same disk they show, and with macrt's own
System only their names do. Itchy & Scratchy asks for 2 MB of
temporary memory at once. With `--memory 4096` there's less than that
outside the panel's partition, and its screen stays black. The
folder's other modules run too: How to Draw sketches Maggie, Homer
Eats eats the desktop from the bottom up, Physics bounces the family's
heads, Simpsons Trivia asks its questions, and Snowball I floats on
clouds with a halo.

### Games from 1984

The first Macintosh games often go around the Toolbox. They draw
straight into the screen, read the mouse button from the hardware, and
keep time by the processor's speed or by the VIA's timers.

macrt gives them what they reach for: the screen buffer at `ScrnBase`, a
Mac 512K's screen where a 512K has it, the VIA, the exception vectors,
the floppy disk driver and the drives, and the Event Manager's
low-memory record of the mouse and keyboard. [The
hardware](toolbox.md#the-hardware) says how each one works. `--speed`
holds the processor to about a Mac Plus's pace, for games that time
themselves by how fast it goes.

So a game on a 400K floppy runs like this:

```sh
python -m macrt "Mac 128K Games Vol. 1/Frogger.DSK" --host screen --size 512x342 --speed 1
```

A game that doesn't use menus may draw over the menu bar, in the top 20
rows of the screen. As on a Mac, the bar is in the screen buffer, so what
the game draws there, white or black, shows over the bar until it calls
`DrawMenuBar`. A window that reaches up over the bar doesn't draw there,
as its visRgn leaves out the bar, as the desk (`GrayRgn`) does. A game
that means its window to cover the bar puts the bar back in the visRgn
itself, as Indiana Jones and the Last Crusade does, and then what it
draws there shows over the bar in the same way. A game's windows go on
showing the main screen buffer while the VIA shows the alternate one.

### Sound

A Mac 128K, 512K or Plus makes its sound from a buffer in memory, at
`SoundBase`: at each tick, 370 samples from it go to the speaker, while
the VIA has the speaker on, at the volume in the VIA's low bits. macrt
plays the buffer the same way, through your computer's sound output.
The volume starts at 7, the loudest, in the VIA, in `SdVolume` and in
parameter RAM. What's in the buffer comes from three places:

- **The Sound Driver**, `.Sound`, reference number -4. `StartSound`, a
  `Write` to it, plays a sound through one of its three synthesizers:
  square-wave, tones of a pitch, a loudness and a length; four-tone,
  four waveforms played together, as MusicWorks plays its songs; and
  free-form, samples at a rate. An asynchronous sound plays while the
  program goes on, those after it waiting their turn, and its completion
  routine is called when it's done. `StopSound`, a `KillIO`, stops them.
- **The Sound Manager** (Inside Macintosh V), which System 6 and 7
  programs call: `SndPlay` plays a `'snd '` resource, and sound channels
  take commands from the program, `SndDoCommand` queueing them and
  `SndDoImmediate` sending them at once. At each tick, each channel's
  synthesizer makes the next frame, and they're added to the Sound
  Driver's. The note synthesizer plays notes, from a sine to a square
  wave as the timbre goes up; the wave table synthesizer plays the
  program's own waveform; the sampled sound synthesizer plays sound
  headers' samples (`bufferCmd`), and notes on an instrument from one
  (`soundCmd`), looped while the note lasts. `waitCmd`, `callBackCmd`,
  `pauseCmd`, `ampCmd`, `rateCmd` and the rest are there, and so are
  modifiers, the program's own, from `SndAddModifier`. `SndPlay` with no
  channel, or not asynchronously, waits for its sound to end, as does
  `SndDisposeChannel` without `quietNow`, with the ticks going on.
  MineSweeper, Glider, Glypha, Shadow Keep and Tesserae play their sounds
  this way. Play from disk, `SndStartFilePlay`, plays an AIFF or AIFF-C
  file the program has open, or the sampled sound of a `'snd '`
  resource, all of it or the seconds its audio selection says;
  `SndPauseFilePlay` pauses and resumes it, `SndStopFilePlay` stops it or
  waits for its end, `SndChannelStatus` gives its times, and the
  program's completion routine is called when it ends, however it ends.
- **The program itself**, writing its samples into the buffer at each
  tick, as Dark Castle, Crystal Quest and SmoothTalker's speech do. A
  program needn't start at the buffer's first word: counting from 0,
  Creepy Castle starts at word 10, Prince of Persia at 37, Dark Castle
  at 40, Crystal Quest at 42 and Tetris at 140, each going round to just
  before where it started. macrt plays each frame from where the program
  starts writing it, which it looks at twice a second, by watching one
  tick's writes to the buffer.

On a Mac II, a program can also write its samples to the Apple Sound
Chip, at the address in `ASCBase`, as Digital Messiah does with its
music, 370 of them at each tick from its VBL task. What it writes to the
chip's first 1K goes into one queue of samples and its second 1K into
another, each keeping up to 1K; at each tick the speaker plays the next
370 from them, the two averaged while both have some, with the sound
buffer's. macrt's Mac Plus has no chip, and its `ASCBase` is 0.

`--no-sound` keeps the sound off your speakers, and `--record-sound
FILE.wav` writes it to a WAV file as well: 8-bit mono, 22,200 samples a
second. A sound output that doesn't take 22,200 samples a second gets
them at its own rate, each of its samples on the line between the two
of the Mac's it comes between.

How it differs from a Mac:

- macrt's ticks are 60 a second, a Mac's 60.15, so the samples play at
  22,200 a second rather than 22,255, and the Sound Driver's tones are 4
  cents flat.
- The ticks, and so the sound, go on while the application's code runs
  and while it waits: in `Delay`, in `WaitNextEvent` with time to sleep,
  in a menu, a modal dialog or a window being dragged. Its VBL tasks and
  Time Manager tasks run at those ticks too, but the VIA's interrupts
  wait for the program's own code to run again, since they would come in
  the middle of the call it waits in. Under MultiFinder, the ticks that
  run are those of the application that has the processor, or had it
  last, each application's VBL tasks being its own.
- When the host is late for some ticks, as it may be when it's busy, the
  VBL tasks and the sound make them up all at once, up to a third of a
  second of them. After another application's turn, or a time with
  interrupts masked, they make up a tenth of a second at most, and after
  a tick that left no VBL task counting down and no sound to make, none:
  a task installed then counts from its first tick. A longer gap is one
  tick.
- The alternate sound buffer isn't played, and timer 1 doesn't turn the
  speaker on and off by itself, as some programs have it do through the
  VIA.
- `SysBeep` is your computer's own beep. MIDI, which goes out through
  the serial ports, plays nothing.
- The Sound Manager's synthesizers stand in for Apple's `'snth'`
  resources, which aren't run, and nor are modifiers that are resources.
  Its sounds are the Plus's, in one channel, so stereo's sides are added
  together; compressed sounds are silent, as is what `Exp1to3` and
  `Exp1to6` expand MACE to, and `syncCmd` doesn't wait for
  other channels. A channel's commands past its full queue wait outside
  it, so a program never waits for room.
- Up to 64 channels can be open at once, a limit of macrt's own; past it
  `SndNewChannel` returns `notEnoughHardware` (-201). DeadEnd counts the
  channels it can have by asking for one more until it's refused.
- Play from disk reads the whole sound from the file when it starts,
  rather than a buffer at a time through the program's buffer.
- The Apple Sound Chip is macrt's own: where it is, the two queues and
  what the speaker takes from them are what Digital Messiah needs, not
  taken from Apple's description of the chip. What a program writes to
  the rest of it, from $800, is kept and reads back, but isn't heard: a
  volume or a way of playing it sets there makes no difference.


## What happens at launch

1. **Memory.** macrt reads the application's `SIZE` resource (or, without
   one, the sizes from the Finder's Get Info) and gives it a partition of
   that size in an 8 MB Mac, and 24K more for its bigger stack (see [The
   stack](architecture.md#the-stack)). `--memory` gives it a size of your
   own. An application that asks for more than is free is refused, and
   macrt says how much it needs.
2. **The A5 world.** `CODE 0` describes the application's globals, which
   sit below A5, and the jump table, which sits above it. macrt builds them
   as the Segment Loader did and loads `CODE 1`. The other segments load
   when the code first calls into them.
3. **The processor.** unicorn's 68020, in 24-bit mode, starts at the
   first jump-table entry. Gestalt, `SysEnvirons` and the low-memory
   globals say it's a Mac Plus, with a 68000 and no floating-point unit,
   and the exception frames it stacks are a 68000's (see `--model`).
   The few instructions of a 68020 and a 68881 that unicorn doesn't run,
   or runs otherwise than Motorola's manual says (`CHK2` and `CMP2`,
   `PACK` and `UNPK`, packed decimal `FMOVE`, an `FMOVE` of immediate
   data to the floating-point control registers, `FSAVE` and `FRESTORE`)
   stop it, and macrt runs them in Python (`macrt/m68k/instructions.py`).
4. **Traps.** Every A-line trap the code executes (`_NewWindow`,
   `_DrawString`, `_WaitNextEvent` and so on) stops the processor. The
   matching Python routine then reads its arguments from the 68k stack or
   registers and does what Inside Macintosh says that routine does.
   - Windows and menus become Qt windows and menus.
   - QuickDraw draws into an image for each window, which Qt shows.
   - The Mac's data structures (window records, handles, regions and so
     on) are real, in the Mac's memory, so code that reads them directly
     still works.
5. **Events.** Mouse clicks, keys, and update and activate events come from
   Qt and are queued for `GetNextEvent` and `WaitNextEvent`. An application
   waiting in `WaitNextEvent` lets the others run. One whose `SIZE`
   resource says it takes high-level events finds an Open Application
   Apple event waiting, as System 7's Finder sends when it opens an
   application without documents.

[How macrt works](architecture.md) goes through each of these in more
depth: the processor, the trap dispatcher, memory, drawing and resources.


## What works

macrt answers more than 700 Toolbox and Operating System routines.
[What works](toolbox.md) goes through them a manager at a time, from the
Memory Manager to the Sound Manager, and lists what isn't there yet.


## When it stops

### A call macrt doesn't implement

```
macrt: MyApp: SetDeskCPat (trap $AA47) isn't implemented; skipped
```

The routine is known but has nothing behind it yet: here `SetDeskCPat`,
which sets a colour desktop pattern. A routine that comes through a
package or dispatch trap (`Pack3`, `QDExtensions` and the rest) is named by the
routine its selector picks rather than the trap. macrt takes its
arguments off the stack and returns 0, or `unimpErr` (-4) for a routine
that returns an error code. Many applications carry on regardless, so each
missing call is reported only once. When the application quits, macrt
counts them up:

```
macrt: MyApp called traps macrt doesn't implement: SetDeskCPat (1), HMGetHelpMenuHandle (1)
```

That list is the most useful thing to report: it says exactly what an
application is waiting for.

### A system error

When the application does something it can't recover from, macrt stops it
and shows the classic system error alert. The other applications keep
running. The terminal gets the details:

```
macrt: MyApp stopped: system error 3 (illegal instruction): at $00002A
  PC 0000002A  SR 2000
  D0-D7 00000000 0000000C 00000000 00000000 00000000 00000000 00000000 00000000
  A0-A7 00000000 00084A10 00000000 00000000 00000000 001840F8 0017FE40 0017FE2C
  last traps: GetNextEvent at $08F2A4, …, Get1Resource at $0899C2, HLock at $0899CA
```

The first line says what went wrong. Here, MyApp jumped to $00002A, in
low memory, which isn't code (see the table below). Below it are the
registers and the last 16 traps, newest last, with the address each was
called from.

When the first line only names an exception, the last traps usually show
the cause. Often a call shortly before was skipped, and the application
went on with a value that call should have filled in.

| ID | Name | What it usually means here |
| --- | --- | --- |
| 0 | macrt itself failed | A bug in macrt, not in the application. A Python traceback comes with it. |
| 1 | bus error | The application read or wrote memory outside the Mac's 8 MB, or passed a bad pointer to a trap. Usually it's a handle from a call that failed earlier. |
| 2 | address error | It jumped to an odd address. |
| 3 | illegal instruction | It ran into data: often a call through a NIL procedure pointer, or a segment that isn't code. |
| 4 | zero divide | It divided by zero, often by a value a skipped call should have set. |
| 5 | check trap | A `CHK` instruction failed (Pascal range checking). |
| 6 | overflow trap | A `TRAPV` instruction failed. |
| 7 | privilege violation | It ran an instruction that isn't allowed. |
| 8 | trace trap | The trace bit was set. |
| 10 | line 1111 trap | An F-line instruction that the 68020 and its 68881 don't have, such as an MMU instruction. Floating-point instructions themselves work, whichever `--model` it is. |
| 11 | miscellaneous exception | Any other processor exception. |
| 12 | unimplemented trap | A trap macrt doesn't even know the name of, or a private call. PrintMonitor's private MultiFinder calls stop it here. |
| 15 | segment loader error | A `CODE` segment that's missing or compressed, or one built for the far code model that the application's own runtime doesn't load. |
| 25 | out of memory | A Toolbox call couldn't get the memory it had to have. Most calls report `memFullErr` instead and let the application decide. |

### Looking closer

- `--trace-traps` logs every trap as it's called, with the address it was
  called from. Use it to see what an application was doing before it
  stopped, or why it's sitting still. A package call is logged with the
  routine it's for, by Apple's name:

  ```
  macrt: Fly Swatter: FP68K FMULS at $1033F6
  macrt: Microsoft Excel: IUMagString at $1193A6
  ```
- `--view` shows the application's menus, windows and dialogs from its
  resources without running any code. If you mainly want to see what an
  application looked like, this always works. An application with no 68k
  code gets the same. `viewer.py`'s ResourceViewer does what the
  application's startup would do, through the same Toolbox calls:
  `GetNewMBar` or `GetMenu` for the menu bar, then `GetNewWindow` for the
  first `WIND`. A *Resources* menu opens the other windows, dialogs and
  alerts.
- `--list` shows what the application is made of. An `ALRT` or `STR#`
  near the error often says what the application was trying to tell you.
