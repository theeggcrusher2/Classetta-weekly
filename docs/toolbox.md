# What works

Part of [the documentation](README.md).

macrt answers more than 700 Toolbox and Operating System routines. Here
they are, a manager at a time, grouped by what they're for. Each says
what macrt does, and where that differs from a Mac, what the difference
is and which program needed it. [How macrt works](architecture.md)
explains the machinery underneath.

- [Memory and code](#memory-and-code): [Memory Manager](#memory-manager), [Segment Loader](#segment-loader), [Resource Manager](#resource-manager)
- [Drawing](#drawing): [QuickDraw](#quickdraw), [Font Manager](#font-manager), [Colour windows](#colour-windows), [Palette Manager](#palette-manager), [Color Picker Package (`Pack12`)](#color-picker-package-pack12), [Icons](#icons)
- [The user interface](#the-user-interface): [Window Manager](#window-manager), [Menu Manager](#menu-manager), [Help Manager](#help-manager), [Event Manager](#event-manager), [Control Manager](#control-manager), [Dialog Manager](#dialog-manager), [List Manager](#list-manager), [TextEdit](#textedit), [Scrap Manager](#scrap-manager), [Desk Manager](#desk-manager)
- [Files and devices](#files-and-devices): [File Manager](#file-manager), [Device Manager](#device-manager), [Alias Manager](#alias-manager), [Standard File](#standard-file)
- [Processes and communication](#processes-and-communication): [Process Manager and MultiFinder](#process-manager-and-multifinder), [Apple Event Manager](#apple-event-manager), [Edition Manager](#edition-manager), [Component Manager](#component-manager), [Communications Toolbox utilities](#communications-toolbox-utilities), [PPC Toolbox](#ppc-toolbox), [Thread Manager](#thread-manager)
- [Numbers and text](#numbers-and-text): [SANE](#sane), [International Utilities](#international-utilities), [Script Manager](#script-manager)
- [The machine, printing and sound](#the-machine-printing-and-sound): [The hardware](#the-hardware), [Printing Manager](#printing-manager), [Sound](#sound)
- [Other managers](#other-managers)
- [Not yet](#not-yet)

## Memory and code

### Memory Manager

Handles, pointers, heap zones, purging, the system heap, and
MultiFinder's temporary memory, from a zone of its own above the
applications' partitions. A block that grows, with `SetPtrSize` or
`SetHandleSize`, grows where it is when the relocatable blocks after it
can be moved or purged out of the way, and blocks are a multiple of 2
bytes long on a 68000 and of 4 on later processors, as the ROM makes
them.

`MoveHHi` moves a block up to the first block above it that can't move,
reading the zone's headers only that far, so that a title that moves
handles up hundreds of times a second (GolfMaster!, in a zone of some
800 blocks) isn't slowed by the size of its zone; a header there that a
program has changed itself makes it read the whole zone back first.

`ResrvMem` makes room low in the zone, moving handles up out of the way,
so that a block a program keeps locked doesn't split the room it needs
for the large ones it allocates next, as Stratego's offscreen pixel maps
need. A program's `NewPtr` makes room low the same way, even when a free
block higher up would hold the pointer, though it doesn't grow the zone
to do it: Excel 1.5 jumps from the 128K block it asks for into its first
segment with 16-bit offsets. macrt's own pointers go in the first free
block that holds them.

`MaxMem` purges every purgeable block that isn't locked before it
compacts the zone, and `PurgeMem` purges them, lowest first, only until
there's room, moving nothing; a resource or segment purged is read in
again the next time it's asked for. `PurgeSpace` purges nothing: it
counts what purging every unlocked purgeable block would free, with
what's already free and what the zone could still grow by, in all and in
the longest run of such blocks next to each other, as Inside Macintosh
IV's Memory Manager has it. HyperCard 1.2.5 asks it at every turn of its
event loop.

When a block doesn't fit, the zone grows if it can (the application
heap, up to `ApplLimit`); failing that, it's compacted, and only if that
isn't enough are unlocked purgeable blocks purged, lowest first, as
Inside Macintosh: Memory's "Heap Purging and Compaction" has it. Last,
the grow-zone function a program installs with `SetGrowZone` or
`InitZone` is called with the block's physical size, and `GZRootHnd`
holds the handle being resized, which it isn't to move or purge (0 for a
new block); for as long as it says it freed something, the zone is
compacted and purged again and it's asked again (Inside Macintosh:
Memory, "MyGrowZone"). Fine Artist installs one: after its NEXT button
it's called twice, and says it freed 25,966 and then 157,490 bytes.

### Segment Loader

`LoadSeg`, `UnloadSeg` (given an address in the
segment or one of its jump-table entries, as Pascal's `@` gives; an
entry may be called through an address with flags in its top byte,
which a 68000 ignores),
`GetAppParms`, `ExitToShell` and `_Launch`.

### Resource Manager

The resource chain, from the application down to the System file;
`OpenResFile` for files next to the application; adding, changing and
writing resources; reading and writing part of one in its file
(`ReadPartialResource`, `WritePartialResource`, `SetResourceSize`); and
an application's own patch on `_ResourceStub`, through which it reads
resources in, as some installers, and applications compressed by
AutoDoubler, do to decompress theirs.

When an application has patched `GetResource` (or `Get1Resource`), the
resources macrt's own managers get for it, such as a dialog's `DLOG` and
`DITL`, a picture item's `PICT` and a control's `CDEF`, are got through
its patch, as `_LoadSeg`'s segments are: Prince of Destruction keeps its
dialogs and pictures compressed in a format of its own, and its patch
expands them.

A resource asked for by ID that isn't found leaves `ResError` `noErr`,
as Microsoft File and Director's projectors count on: Foo Castle asks
for a `VWCI` for each member of its cast, which few have. One asked for
by name is `resNotFound` only when the last file looked in (the System
file, or the current one for `Get1NamedResource`) has its type.
`RGetResource` looks where `GetResource` does: macrt's ROM has no
resources of its own.

Compressed resources are expanded as they're read in, by macrt or by the
`dcmp` they name (see [A real System
file](running-apps.md#a-real-system-file)), and `SizeResource` gives
their size expanded. A resource read in goes in the system heap if its
`resSysHeap` attribute says so, and otherwise in the current zone when
that's the system heap or one the program made with `InitZone`, as an
`INIT` sets it to keep what it reads; else in the heap of the
application whose file it's from.

## Drawing

### QuickDraw

#### In black and white

- ports, regions, patterns, polygons, every shape, text, measured
  by `MeasureText` too, and `CopyBits`;
- a port's own `textProc` and `txMeasProc`, which draw and measure
  its text in place of `StdText` and `StdTxMeas`, and its own
  `bitsProc`, which `CopyBits` into the port's `portBits` calls in
  place of `StdBits`, as HyperCard 2.2's AddColor has it put its
  colour over a card;
- a port's own `rectProc`, `rRectProc`, `ovalProc`, `arcProc`,
  `polyProc` and `rgnProc`, which draw its shapes in place of
  `StdRect` and the rest, TextEdit's erasing included (its
  `lineProc` isn't called);
- pictures, played and recorded, spooled from and to disk through a
  port's own `getPicProc` and `putPicProc` too; the Picture
  Utilities' `GetPictInfo` and `RecordPictInfo` read a picture as
  macrt's `DrawPicture` does, through the current port's
  `getPicProc` when it has its own, as Jigsaw reads the picture it
  makes a puzzle of. A picture drawn at another size than its frame,
  or one whose extended version 2 header gives a higher resolution
  it was recorded at, has its positions, its text sizes and the extra
  width its `SpExtra` gives each space all scaled to the destination,
  as the MayaQuest ReadMe's 2304-dpi pages need;
- clipping to a port's visRgn and clipRgn, and to the edges of its
  bit map, but not to its portRect, which Inside Macintosh I
  (QuickDraw) has drawing kept within too: Excel 4.0 draws its
  toolbar's buttons in a port whose portRect it copies from a window
  it made with no size, having set the port's regions to the buttons'
  pixel maps. A window's visRgn is within its portRect anyway.
  `ScrollRect` keeps to the portRect, which Inside Macintosh I
  names for it;
- cursors: the program's cursor (`SetCursor`) is your pointer's shape
  over its windows, or over the screen with `--host screen`, and
  `HideCursor`, `ShowCursor` and `ObscureCursor` hide and show it.
  The standard arrow is your own pointer, and the menu bar keeps it.
  The host's pointer can't invert what's under it, so a cursor's
  inverting pixels are black;
- `CopyMask`, `SeedFill` and `CalcMask`, and in colour `SeedCFill`
  and `CalcCMask`, which ask a program's own `matchProc` which pixels
  are edges when it gives one (Director's projectors draw their
  sprites' matte ink through `CalcCMask`);
- `Random`, from `randSeed`; a program can seed it from `RndSeed`,
  in low memory, which is different each time macrt starts.

#### In colour

- colour calls, mapped to black and white on a Mac Plus;
- on a Mac II (`--model ii`), a colour screen: its graphics device,
  which `DeviceLoop` passes to a program's drawing procedure with its
  depth, its pixel map and colour table, which a program can change with
  `SetEntries` and the Color Manager's other calls (`ProtectEntry`
  keeps an entry from `SetEntries`, and `ReserveEntry` from being
  matched to a colour, for the device's `gdID`, which `SetClientID`
  sets). What QuickDraw draws is in the nearest of its
  colours to the eight classic ones. `GetCTable` gives the standard
  colour tables of 1, 4 and 8 bits, the greys (32 plus the depth) and
  the standard tables with the highlight colour (64 plus the depth), as
  Inside Macintosh: Imaging With QuickDraw lists them, and for any other
  ID a copy of the `clut` resource. `DisposeCTable` is `DisposeHandle`,
  `MemErr` and all, for a handle in no heap too: The Secret Island of
  Dr. Quandary copies a pixel map from its own data, colour table
  `$FFFFFFFF` and all, and disposes of that table;
- colour ports (`OpenCPort`, `CGrafPort`s), which draw in RGB colours
  (`RGBForeColor` and the rest) and with pixel patterns (`NewPixPat`,
  `GetPixPat` from a `ppat` resource, `MakeRGBPat`, `PenPixPat`,
  `FillCRect` and the other `FillC` calls), and `GetCPixel` and
  `SetCPixel`. `ColorBit` sets the port's colour plane for a printer,
  and changes nothing on the screen. An RGB pattern draws as the
  nearest colour the screen has, not dithered. A picture plays its pixel patterns and RGB
  colours in a colour port, and `OpenPicture` in one records a version
  2 picture, which keeps them; `CopyBits` into it is recorded in black
  and white;
- a program's own search procedures (`AddSearch`, `DelSearch`), which
  `Color2Index` asks for a colour's index before the screen's inverse
  table, the last added first, and which `CopyBits` asks for the
  indexes of a black-and-white source's black and white in a colour
  port: NetTrek draws its ships in each side's colours through one;
- colour cursors (`GetCCursor` from a `crsr` resource, `SetCCursor`,
  `DisposeCCursor`), shown in their colours on a screen of more than
  2 bits a pixel, and as their black-and-white cursor on one of 1 or 2;
- offscreen pixel maps (`NewPixMap`, `CopyPixMap`, `SetPortPix`): a
  colour port draws on one of 2 to 8 bits in pixel values, its
  foreground and background colours' indexes, and `CopyBits` copies
  pixel values as they are between pixel maps of one colour table, and
  matches colours between others and to the screen. In a source mode
  but Copy (`srcOr`, `notSrcBic` and the rest), in a black foreground
  and a white background, it combines the source's values with the
  destination's bit by bit, as Inside Macintosh V has the modes apply
  to a colour source's 1s and 0s: FloorTiles masks its coloured tiles
  offscreen that way;
- the transparent mode (36): a pen, a fill or text drawn in it, and a
  black-and-white source copied in it, leave out what's in the
  background colour, the 0 bits and, when the foreground colour is the
  background colour too, the 1 bits; a pixel pattern leaves out its
  pixels of the background colour. macrt draws an arithmetic mode with
  the pattern bit set (40 to 47) as the arithmetic mode itself, so the
  Maxis CD-ROM Sampler, which paints white rectangles in mode 44 in white
  on white over its room each frame, leaves the room as it is. Between
  pixel maps whose colour tables have one seed, a colour port's
  `CopyBits` in the transparent mode copies pixel values as they are,
  as in `srcCopy`, but for the background colour's index, which it
  leaves out: TerraTopia gives its offscreen pixel map the screen's
  seed but not its colours, and draws its sprites in it that way;
- a pixel map of 2 to 8 bits with no colour table (a `pmTable` of NIL,
  or a table with no colours): macrt draws in it, and copies from it, in
  the current device's colours if the device is of its depth, else in
  the standard colours of its depth (four greys, for 2 bits). The
  Discovery Demo draws its pictures in such a pixel map, then copies
  them out through another pixel map of the same pixels that has the
  screen's colour table;
- 32-Bit QuickDraw's offscreen graphics worlds (`NewGWorld`,
  `SetGWorld`, `GetGWorld`, `UpdateGWorld`, `DisposeGWorld`,
  `GetGWorldPixMap`, `LockPixels` and the rest of `QDExtensions`, and
  `NewScreenBuffer`): a colour port onto pixels of its own, with a
  graphics device of its own, so that what's drawn in it is in the
  nearest of its own colour table's colours. Made with depth 0, a world
  is like the screen its rectangle (a global one) is on, and is that
  rectangle's size, with its top left at 0,0. Made with `noNewDevice` on
  a program's device, a world has that device's depth, and, given no
  colour table, the device's own: so what's drawn in it is in the
  device's pixel values, and `CopyBits` copies them to the screen as
  they are, indexes reserved for animation too (Just Grandma and Me
  draws its pages that way). `UpdateGWorld` given a device gives a world
  that device's depth and colour table, whatever depth and table it's
  given besides, and a world made on that device has the device's own
  table then (the Flashback demo updates its worlds that way every frame,
  passing a table whose colours it changes). `SetGWorld` with no device
  makes the main screen's device current for a port on the screen, as
  for a window a program goes back to from a world. `Gestalt`'s `'qd  '`
  on a Mac II is `gestalt32BitQD13` ($230), the value that says the
  system supports "the System 7 version of Color QuickDraw" (Inside
  Macintosh: Imaging With QuickDraw, Color QuickDraw, "Using Color
  QuickDraw"), as macrt is a System 7; given $220, The Multimedia
  Workshop said it needs 32-Bit QuickDraw. `QD32Trap` ($AB03) is there
  for a program that looks for it with `GetTrapAddress` instead, as Pico
  does. A
  `QDExtensions` call is the selector in D0's low word, so an old
  program's `moveq` of a selector alone reaches it too. How it differs
  from a Mac's: a world's pixels
  are never purged (`GetPixelsState` says what a program asked for),
  and `UpdateGWorld` scales an image with `stretchPix` but doesn't
  dither it (`ditherPix`).

### Font Manager

Font names and numbers (`GetFontName`, `GetFNum`),
`RealFont`, `SetFractEnable` and `SetFScaleDisable`, and `FMSwapFont`,
which picks the bitmap font or your computer's font for a request,
fills in its output record and leaves the global width table for it in
the system heap, where `WidthTabHandle`, `WidthPtr` and `WidthListHand`
find it (Inside Macintosh IV, the Font Manager): each character's
width as QuickDraw draws it, Microsoft Word 4 adding up its lines from
them. `FontMetrics` gives `GetFontInfo`'s measures in fixed point, and
the table's handle. There are no outline fonts (`IsOutline`), but
`RealFont` says a standard family is there at any size if the disk's
System file or Fonts folder has it as an outline font, since your
computer's fonts draw those families at any size;

### Colour windows

`NewCWindow` and `GetNewCWindow` make a window
whose port is a colour port, its colours in its auxiliary record
(`SetWinColor`, `GetAuxWin`, a `wctb` resource), or the default
record's, from the application's `wctb` 0. Its content is erased in its
content colour, as it is when the window is shown. On a colour Mac any
window's frame is drawn in its table's colours, as near as the screen
has them: its outline, the line under its title bar, its boxes and its
shadow in the frame colour, its title in the text colour, its stripes
in the highlight colour and the title bar behind them in the title bar
colour, which is how macrt reads the parts' names; a dialog box's
white band is in the content colour. A rounded window's frame is black
and white.

### Palette Manager

A colour window's palette (`NewPalette`, `GetNewPalette` from a `pltt`
resource, and the one `GetNewCWindow` gives a window from the `pltt` of
its ID) gives the screen its colours when the window comes to the front:
each tolerant colour an index of its own, changed if it isn't close
enough, and each animated colour an index reserved for it, which nothing
else is drawn in, so that `AnimateEntry` and `AnimatePalette` change
what's drawn in it at once. `PmForeColor` and `PmBackColor` draw in a
palette's colours, and an application can have a default palette for
windows without one. `GetPalette` of -1 gives that default: the one the
application set, else the System file's `pltt` 0, else one of two
entries, white and black, made the first time it's asked for (Inside
Macintosh: Advanced Color Imaging on the Mac OS, Palette Manager,
"Designating a Default Palette for Your Application").

`CopyPalette` copies entries from one palette to another, and
`ResizePalette` changes how many a palette has: an entry added is black
and courteous, and one cut off gives up the index it reserved.

A PixMap in the screen's colour table that's copied to a window
(`CopyBits`) is put on the screen as its pixels' values, an animated
index's too, so that a picture copied while its colours are all black
appears as they're animated: Lemmings fades its screens in that way.
Nigel's World sets its colours this way. How it differs from a Mac's: -
there's one screen to give colours to, and no update events for a change
of colours, which aren't needed: what's on the screen is shown in its
new colours; - an animated colour takes the highest index no other entry
needs (an explicit one, `pmAnimated + pmExplicit`, the index of its
entry), rather than the one least used by the windows on the screen; -
the screen's colour table may have one colour more than once, but what
macrt shows of a window is in colours, not indexes, so an entry whose
colour another has (an index reserved for animation, or any but the
first of others alike) is shown a step from it (in red, green or blue,
out of 255), and `Index2Color` gives that colour. A port whose
foreground or background index has the colour it asks for keeps that
index, so a program can set the screen's entries all black with
`RestoreEntries`, draw, and fade in what it drew, as Zoop does; - an
entry inhibited from a depth (`pmInhibitC8` and the rest) is left off
only a colour screen of that depth, macrt's screen being a colour one.

### Color Picker Package (`Pack12`)

Colours converted between RGB,
CMY, HSL and HSV (`RGB2HSV` and the rest, `Fix2SmallFract` and
`SmallFract2Fix`), and `GetColor`, which asks with your computer's own
colour dialog, the program's prompt as its title. The dialog keeps 8
bits of each component, so a colour changed in it comes back to that
precision.

### Icons

`ICON`, `SICN`, `cicn` and icon families, through the mask; a
`cicn` in its colours when the screen has more than 2 bits a pixel.
`ForEachIconDo` calls a program's icon action function for each type
of icon its selector names, and keeps what the function leaves in the
suite.

## The user interface

### Window Manager

The standard window kinds, with dragging, growing, zooming and update
regions; between `BeginUpdate` and `EndUpdate` only what's in the update
region is drawn.

A window with the application's own `WDEF`, a resource or code it put in
`windowDefProc` itself, is that code's to shape and draw: the window
takes the shape of the structure region it works out, shows the frame it
draws, and its `wHit` answer says where a click is. macrt has the `WDEF`
shape and draw a window whose frame needs it, after the host has moved,
shown or brought forward a window, at the end of the application's next
Window, Dialog or Event Manager call of those that make, show, move,
size, highlight or close windows, or that a program waits in, and as
`ModalDialog` waits; not in the middle of other calls, such as a
`BlockMove` that Excel 2.2a's interpreter makes, whose `WDEF` is its own
interpreted code. An application's `WDEF` that passes messages to the
System's `WDEF` 0 or 1, as Excel's do, gets macrt's frames. A `WDEF`
written before Color QuickDraw finds a colour window's bounds where a
black-and-white window's `portBits.bounds` are, as it works out the
window's regions. A colour `WDEF` that draws in the colour Window
Manager port (`GetCWMgrPort`), as BMAPEdit's does, draws its frame there
as well as in `WMgrPort`.

The desk is drawn in `DeskPattern`, or with Color QuickDraw in the pixel
pattern `SetDeskCPat` gives it, when `PaintOne` is called with no
window, as Desktop Patterns does. What of the menu bar a program has put
in `GrayRgn` is desk then too, as HyperCard's `hide menuBar` has it,
until the bar is drawn again.

An application starts with `WWExist` and `QDExist` set, saying the
Window Manager and QuickDraw aren't set up yet; `InitGraf` and
`InitWindows` clear them. Burtonia's THINK C console sets up the Toolbox
only if they say it isn't. `InitWindows` makes the Window Manager port
the current port the first time a program calls it. Called again, as
Disk Copy's licence agreement does after setting its dialog as the port,
it leaves the current port as it is.

A window stays where the Window Manager put it when the program sets its
`portRect` itself without `SetOrigin`: what it draws goes where
`portBits.bounds` puts it, and the window keeps that part of the screen.
Ricochet sets the `portRect` to the half of its window it draws in next.

### Menu Manager

`MENU` and `MBAR` resources, key equivalents, marks, styles and
`AppendResMenu`. Hierarchical menus: a submenu inserted with
`InsertMenu(menu, hierMenu)` goes in the menu list's hierarchical
section, not the bar (Inside Macintosh V, the Menu Manager), and an item
with `hMenuCmd` ($1B) as its key opens the submenu its mark names, to
its right. `MenuSelect` and `MenuKey` return the submenu's ID with its
items.

After a `MenuSelect` that chooses nothing, `MenuChoice` gives the menu
ID and number of the disabled item the mouse was released over in a menu
the host draws, a dividing line included, and 0 otherwise (Inside
Macintosh: Macintosh Toolbox Essentials, Menu Manager); after `MenuKey`
it's 0.

A menu with the application's own `MDEF`, a resource or code it put in
`menuProc` itself, is that code's to size, draw and track: the menu
pulled down shows what it draws, in the Window Manager port, and its
`mChooseMsg` answer says which item the mouse is over. `PopUpMenuSelect`
puts a pop-up menu, its own `MDEF`'s or a standard one, with the chosen
item at the point given, below the menu bar, and `CalcMenuSize` asks the
`MDEF` for the menu's size. An `MDEF` that sets the port's font and
passes messages to the System's `MDEF` 0, as Theldrow's does, gets its
menus drawn in that font.

With Color QuickDraw (`--model ii`), the menu colour table in
`MenuCInfo` colours the bar, its titles and the items the host draws.
`InitMenus` makes it, with the entries of the `mctb` 0, and
`ClearMenuBar` and `GetNewMBar` empty it; `GetMenu`, and so
`GetNewMBar`, adds the entries of the `mctb` with the menu's ID, as
Glider 4's menus have, and `GetMCEntry`, `SetMCEntries`, `DelMCEntries`,
`GetMCInfo`, `SetMCInfo` and `DispMCInfo` work on the table (Inside
Macintosh V, the Menu Manager). A title's entry gives its colour, the
bar's behind it, its items' and its menu's background, in that order, as
Digital Messiah's grey-on-black menus have them. A highlighted item
swaps its text and background colours.

A title of five bytes, $01 and an icon suite's handle, shows as the
suite's small icon, as Apple's Technical Q&A TB 06 has a program make
one: BBEdit 3.1 has two such menus.

As on System 7, the Application menu, and the Help menu once an
application has one, come after the application's own menus in its menu
list, whatever it does to them: HyperCard 2's `doMenu "Hide Others"`
looks for its item there.

### Help Manager

System 7's Help menu. `HMGetHelpMenuHandle` gives an
application its own copy of it, with About Balloon Help and Show
Balloons, for it to append its items to (Inside Macintosh: More
Macintosh Toolbox, 3-83). Once it has, the menu's balloon is at the
right of the bar, and `MenuSelect` and `MenuKey` return
`kHMHelpMenuID` (-16490) with one of its items. Balloons aren't shown:
`HMGetBalloons` is false, as with Show Balloons off, and the two
standard items are dimmed. `HMShowBalloon` and `HMShowMenuBalloon` say
help is off (`hmHelpDisabled`); the balloons' font and size, and the
`hdlg` and `hmnu` resource IDs an application sets, it gets back.

### Event Manager

`GetNextEvent`, `WaitNextEvent`, `EventAvail`, `Button`, `GetKeys`,
`PostEvent`, `PPostEvent` and `FlushEvents`. `PPostEvent` gives the
program a pointer to the posted event's queue entry (Inside Macintosh:
Macintosh Toolbox Essentials, Event Manager): in macrt an entry in the
system heap that holds that event alone, from which its message, when,
where and modifiers are read back when it's taken. Troggle Trouble Math
Demo writes a pointer and a flag into the entry of an event it posts
itself.

macrt's tick clock, which `TickCount` and `Ticks` give, starts a minute
in (3600 ticks), macrt's own choice, not at 0, so that a program
launched as macrt starts doesn't find it has only just begun: the
Quantum Leap Game Collection's Castles Demo, while `TickCount` was under
180, ran past the end of a list of its own and over its own records, and
quit; it shows its title now.

`GetNextEvent` with nothing to give returns at once, unless it's asked
again in the same tick: then it waits for the next, so that a program's
event loop leaves the host the processor. `WaitNextEvent` with no time
to sleep does the same. The title screen of the 500 Mac Games CD's
Lemmings calls it so, posts itself an event, takes it and asks again,
round and round: that kept one of the host's processors going until
`WaitNextEvent` waited too, and takes about 6% of one now. Of that CD's
titles, those still running that took at least half a processor at the
end of a survey's wait went from 70 to 38 with it. `WaitNextEvent` with
a mouse region the mouse is out of, an empty one included, gives a
mouse-moved event when it has nothing else to give, and a null event
between two: asked again after one, it gives a null event when its time
to sleep runs out, by the next tick at the latest. While the mouse stays
where it is, macrt gives one of each a tick: asked again in the null
event's tick, `WaitNextEvent` waits for the next tick, or for input,
first. SimpleText and the 500 Mac Games CD's Darkwood pass an empty
region and ask again as soon as they have the event: each kept one of
the host's processors going, and takes under a fifth of one now. The
Secret Island of Dr. Quandary passes an empty region it never changes,
starts only on a null event, and takes more than a tick to ask again:
given a mouse-moved event each time, its window stayed white.

A program that waits by calling `TickCount`, `Button`, `EventAvail`,
`GetMouse`, `StillDown`, `GetKeys`, `WaitMouseUp`, `OSEventAvail`,
`GetOSEvent` or `SystemTask` over and over, with nothing else between,
as Lemmings and Airborne! do, leaves it the processor too: after 2 ms of
that in a row, it waits for the next tick or for input, which is what
it's asking about. `FlushEvents` between such calls, as that CD's
Runaround and Run call it between asking `EventAvail` for a key, keeps
the program asking, but doesn't wait itself, so that what comes in while
it waits isn't thrown away: each took a processor, and takes about 14%
of one now. So does `KeyTranslate`, which that CD's Space Junkie calls
between `GetKeys` on its title screen, the same each time: that took a
processor, and takes about 14% of one now. So do calls that change
nothing anyone sees, or only say where things are, as ArcMac calls
`GetPort` and `SetPort` between `SystemTask`, `EventAvail` and
`TickCount`, and IAGO `PtInRect` between `Button` and `StillDown`; and
so do locking a handle and unlocking it again, and priming a Time
Manager task, which macrt runs at the tick after it's due whether the
program waits or not. Opening Night, on a Mac IIx, locks and unlocks the
handles of its timers and primes a task for the next while it asks
`TickCount`, `OSEventAvail` and `GetMouse`: in the last second of a
60-second wait on its window, it took 99% of one of the host's
processors, and takes 22% now. With that and the pace of mouse-moved
events, the CD's titles at least half busy at the end of a survey's wait
went from 38 to 21. One that does a little work between such calls still
has 2 ms of the host each tick, many ticks of a Mac's work.

A program may wait with no call at all, in a loop of its own, for a
count its VBL or Time Manager task keeps, as Putt-Putt & Fatty Bear's
Activity Pack does on its menu and in its activities. When the processor
goes round a few instructions that store nothing and read only memory,
and comes back to where it began with every register as it was, nothing
they read can change until a tick, unless the program has a VIA
interrupt handler of its own, and macrt waits for the tick there. The
Activity Pack took all of one of the host's processors, and takes about
a fifth of one now. With `--speed` neither is done: there the time a
program takes is the point.

An update event a program passes over comes each time it asks, until it
draws the window with `BeginUpdate` and `EndUpdate`, as on a Mac; Cap'n
Magneto's title screen looks only for a click and never does. Asked
again in the same tick for an update it was given and hasn't begun,
`GetNextEvent` and `WaitNextEvent` wait for the next tick, or for input,
first.

`WaitNextEvent` gets its event through a program's patch on
`GetNextEvent`, as on a Mac: HyperCard 2 hands its main loop a click it
noticed while a script ran that way. `StillDown` is false once the
mouse-up is in the queue, and `WaitMouseUp`, like the Toolbox's tracking
calls (`TrackControl`, `TEClick`, `DragGrayRgn` and a window's drag and
grow), takes the mouse-up that ended the press out of the queue (Inside
Macintosh I, Toolbox Event Manager).

`KeyTranslate` uses the US layout whatever `KCHR` it's given, and
`GetScriptManagerVariable(smKCHRCache)` points to that layout as a
`KCHR`.

### Control Manager

Buttons, check boxes, radio buttons and scroll bars, whose titles may
have several lines separated by returns, each line of a button's
centered and those of a check box or radio button left-justified, the
lines a font's leading apart and centered together in the control
(Inside Macintosh IV, The Control Manager), plus the application's own
68k `CDEF`s, including code it puts in a control record's
`contrlDefProc` itself, and a `CDEF` 0 or 1 of its own, which is found
before the System's and takes its place (Flash 2 draws its buttons with
one).

Pop-up menus (`CDEF` 63, procIDs 1008 to 1023, as System 7 has them) are
macrt's too, with or without a System file: a title, then a box with the
chosen item and a down arrow. `TrackControl` pops the menu up over the
box with that item checked, and the item chosen becomes the value. The
variations work: `popupFixedWidth`, `popupUseAddResMenu` and
`popupUseWFont`, as do the title's style and justification (Inside
Macintosh VI, the Control Manager), and `Gestalt`'s `pop!` says they're
there.

Controls have auxiliary records with colour tables (`SetCtlColor`,
`GetAuxCtl`, from `AuxCtlHead`) for an application's own `CDEF` to draw
with; macrt's standard controls are drawn in black and white. With Color
QuickDraw, `InitWindows` makes the default records for windows and
controls, at the end of their lists (Inside Macintosh V).

### Dialog Manager

Modal and modeless dialogs, alerts, `ParamText`, filter procedures and
the item calls, with edit fields edited through TextEdit. On a colour
machine a `GetNewDialog` dialog is a colour window when there's a `dctb`
of its ID, and an alert when there's an `actb`, in that table's colours,
as `NewColorDialog`'s are, and takes the `ictb` of its item list's ID
(Inside Macintosh V): its text items are drawn in their text styles'
fonts, faces, sizes and colours, as Mac MakeOver's labels are, and its
controls get their colour tables. Without a `dctb` or an `actb` it's an
old grafPort, as Inside Macintosh V says, which programs that draw
straight into a dialog's `portBits` count on (Pinball Arcade).
`NewDialog`'s are black-and-white ones.

On a colour machine an icon item whose `ICON` has a `cicn` of the same
ID is drawn as that colour icon (Inside Macintosh: Macintosh Toolbox
Essentials, Finder Interface, "The Icon Resource"). The item's handle is
then the colour icon record `GetCIcon` makes of it, macrt's own choice,
which a program can give to `DisposeCIcon`. Any icon item whose handle
is a record `GetCIcon` made is drawn as a colour icon, as Odell Down
Under has its alerts' icons, set with `SetDialogItem`. `DisposeCIcon`
leaves a handle that's in no heap alone, as `DisposeHandle` does.

### List Manager

Lists drawn by `LDEF` 0 or the application's own
`LDEF`, with clicks, Shift and Command to extend the selection,
dragging, autoscrolling, scroll bars and double clicks. Cells are kept
where the ROM keeps them, so code that reads a list's record and its
data directly sees what it expects.

### TextEdit

Editing, selection, line breaking, scrolling, styled text
and `TETextBox`; and the routine at `TEDoText`, for programs that find,
highlight, draw and place the caret through it themselves. A record's
`highHook` and `caretHook` are called as TextEdit's own, `_InvertRect`,
is: the rectangle's address on the stack, for the hook to pop.

### Scrap Manager

The desk scrap. Text in it is shared with your
computer's clipboard, both ways. `UnloadScrap` writes it to the
Clipboard File in the System Folder, as a Mac does.

### Desk Manager

Desk accessories, the `DRVR`s an application lists in its Apple menu
with `AddResMenu`, from the System file (`--system`) or its own file.
`OpenDeskAcc` opens one for the application that picks it, as a driver.
Its window then takes clicks through `SystemClick`, keys and updates
through `SystemEvent`, which `GetNextEvent` and `WaitNextEvent` call,
and the Edit menu through `SystemEdit`; `SystemTask` gives it time, and
a pick from its own menu goes to it. It closes from its close box, with
`CloseDeskAcc`, or when the application quits.

The Event Manager calls `SystemEvent` and `SystemTask` through the trap
table, so a patch of either sees the calls: After Dark's `INIT` gets its
time that way. With `SEvtEnb` cleared, the Event Manager doesn't call
`SystemEvent`, but a program's own call to it still goes through: THINK
C's console clears it and calls `SystemEvent` itself, for the keys typed
in its window.

macrt's own System file has no desk accessories. `AddResMenu` lists them
in alphabetical order, as a Mac Plus does (Inside Macintosh IV-55); a
128K or 512K Mac listed them in the System file's order, so a game that
opens a desk accessory by its place in the menu may open another one, as
Fokker Triplane's Help does.

## Files and devices

### File Manager

Files, folders and volumes, the HFS catalog calls, working directories,
the `FSSpec` calls and `FindFolder`. See [Documents and
saving](running-apps.md#documents-and-saving).

A volume's reference number, with no directory ID, names the volume's
root directory, the default volume's too, and a partial pathname given
with one starts there (Inside Macintosh: Files, "Identifying Files,
Directories, and Volumes" and Table 2-10; Technical Note FL 22, "HFS
Ruminations"); only 0 names the default directory. Over the Reich,
launched from a folder on its CD, finds the files of its CD so.

`PBCatSearch` searches a volume's catalog by the criteria Inside
Macintosh: Files gives it, names or parts of names, attributes, Finder
information, lengths, dates and parent folders, and `PBHGetVolParms`
says every volume can be searched so: System 7.5's Find File searches
with it. A search goes to the end of the catalog, or until it has as
many as were asked for, whatever `ioSearchTime` says, and goes on from
`ioCatPosition` at the next call, or says the catalog has changed since.
One that has as many as were asked for says `noErr`, even at the
catalog's last entry, and one that comes to the end with fewer says
`eofErr` (Inside Macintosh: Files, "File Manager Reference"): Big
Science Comics, looking for its preferences file, takes `eofErr` for not
found.

To `PBSetVol`, `PBHSetVol` and `PBOpenWD`, a name with no colon that
isn't a folder in the directory may be a volume's, as `SetVol`'s
`volName` is (Inside Macintosh: Files, "File Manager Reference"), even
where a file in the directory has the name: Solitaire Antics gives
`SetVol` back the name `GetVol` gave it, and its application, in the
folder that is its volume, has the volume's name. If no volume has it
either, the call says `fnfErr` where nothing in the directory has the
name, as the folder isn't there, and `dirNFErr` where a file has it: Kid
Works 2 makes its KW Users folder when `PBOpenWD` says `fnfErr`.

A CD-ROM's volume is locked by hardware, bit 7 of `vcbAtrb` and
`ioVAtrb` (Inside Macintosh: Files, "Volume Control Blocks"; Technical
Note FL 530, "File Manager Volume Handling Q&As"); bit 15, the software
lock, is the one `PBSetVInfo` sets. A change to a volume locked by
hardware (making, deleting, renaming or moving a file or folder,
changing a file's information, opening a file to write, or making a
resource file) says `wPrErr`, and one to a volume locked by software
`vLckdErr`; a file on either opened with `fsCurPerm` is opened to be
read, so a resource file opened with `OpenResFile` is read-only.

By macrt's own rule, every volume says it has 32,767 allocation blocks
of 4,096 bytes, 24,576 of them free, and no fork grows past what's free.
A CD's volume, in its VCB and to `PBGetVInfo` and `PBHGetVInfo`, says
what its disc's MDB does instead: how many allocation blocks it has,
their size and how many are free, as nothing can be written to it. Over
the Reich looks for its CD by its size and its having no blocks free.

A folder on your computer is a volume named after the folder. If a
volume already has that name, macrt, by its own rule, puts a number
after it, `Slam Dunk Typing 2`, so that a full pathname, which finds the
first volume of the name it starts with, finds the folder's files: a
program copied from a CD into a folder named as the CD, and run with the
CD's image, finds itself there.

### Device Manager

An application's own drivers, the `DRVR` resources in its file. `Open`
finds one by name and puts it in the unit table, and `Read`, `Write`,
`Control`, `Status`, `KillIO` and `Close` call its routines,
synchronously, asynchronously or immediately; the driver finishes each
request through `IODone`, which calls the request's completion routine.

`Open` finds the System file's drivers too, from `--system` or the
disk's System (see [A real System
file](running-apps.md#a-real-system-file)), except where macrt has its
own: the Sound Driver, `.Print` and `.Sony` (see [The
hardware](#the-hardware)), and the serial ports' `.AIn`, `.AOut`, `.BIn`
and `.BOut`, which have nothing plugged in, so what's written to them
goes nowhere and nothing comes in to read: an asynchronous read waits,
its `ioResult` 1, until `KillIO` or `Close` takes it out with
`abortErr`. The ports' use bytes, `PortAUse` and `PortBUse`, say no
driver has either. AppleTalk's drivers aren't opened, as macrt hasn't
its hardware.

The unit table and the first drivers' device control entries are made
low in the System heap, under 32K, as a Mac makes them at startup:
Speller Bee's speech driver keeps its registers a word each. The table
has 128 entries, as the reserved unit numbers in Inside Macintosh:
Devices go up to 127, with 48 to 127 for slot and other drivers. Disk
Copy puts its own driver in one of those.

Of macrt's own drivers, two kinds have a device control entry in the
table: the screen's video driver (see [The hardware](#the-hardware)) and
the driver of a disk image mounted as a CD-ROM, `.AppleCD` (see [Disk
images](running-apps.md#disk-images)), at the reference number its drive
gives. `Open` finds `.AppleCD` by name, already open. Its `Control`
calls are the ones Technical Note DV 22, "CD-ROM Driver Calls", gives,
for a disc with one data track: `ReadTOC`'s first five forms, the audio
calls refused with `paramErr`, `AudioControl` and `ReadAudioVolume`; the
Note's other calls get `controlErr`.

A program can make a device control entry of its own, for a driver in
its memory, with `DriverInstall` (`_DrvrInstall`), and take it out again
with `DriverRemove`, as THINK C's console does for its console window:
Burtonia's. While a program waits for a synchronous request a driver
hasn't finished, the host waits for each tick, and has the processor in
between.

### Alias Manager

Alias records in System 7's format, found again
after their file or folder has been moved or renamed, and brought up to
date; `MatchAlias` and `GetAliasInfo`; `ResolveAliasFile`, which follows
the Finder's alias files; and Apple event coercions between an alias
and an `FSSpec`. There are no file servers, so an alias whose disk
isn't there doesn't resolve.

### Standard File

The Open and Save dialogs, filtered by file type and by the
application's own filter procedure. A custom dialog's own items (its
buttons, check boxes, radio buttons, pop-up menus, text and edit text)
are shown under the standard ones, and what it draws itself (user items,
pictures and icons) is shown as it draws it; its `dlgHook` is called as
each is clicked or typed in. MacWrite's choice of formats in its Save
dialog is one, and Microsoft File's name for a new datafile another.

In System 7's `CustomGetFile`, the reply record says what's selected
while the dialog is up, so a hook can let the user pick a folder: when
the hook answers one of its own items with Open, the folder selected, or
with nothing selected the folder shown, is returned. The hook can say
where the dialog starts (`sfHookChangeSelection`), sends
`sfHookGoToDesktop` for the Desktop button, and hears `sfHookNullEvent`
when a folder is shown and each time the selection changes, not between
them. Spoydworks Solitaire finds its Games & Decks folder this way. The
original calls (`SFGetFile`, `SFPGetFile`) do the same, as macrt's own
rule, with the folder's name in the `SFReply`, `good` TRUE and no file
type.

While a dialog is up, `SFSaveDisk` and `CurDirStore` say the folder
shown, the current directory Inside Macintosh: Files describes under
"Setting the Current Directory", and a hook that changes them and
returns 101 has the folder they name shown, as Technical Note FL 33
("Standard File Customization") says. Kid Works 2's Select Current
Directory button reads `CurDirStore` and answers Open.

A hook that changes the types in its type list and returns
`sfHookRebuildList` has the list made again with the new types, as
FlexReader II's icons for its three kinds of file do. The modal filter
and activation procedures aren't called.

## Processes and communication

### Process Manager and MultiFinder

The process calls, temporary memory
and `SetFrontProcess`. The `FSSpec` `GetProcessInformation` gives for
an application is its file's: the volume, the folder it's in and its
name (Inside Macintosh: Processes, Process Manager).

### Apple Event Manager

Descriptors, lists, records, and Apple events
with their parameters and attributes; the handler tables, object
accessors and object callbacks, kept though nothing resolves object
specifiers; and coercions between numbers, text and four-character
codes, plus the application's own coercion handlers. An application gets the Open
Application event at launch (see [What happens at
launch](running-apps.md#what-happens-at-launch)), can look through what's waiting with
`GetSpecificHighLevelEvent`, and can send events to itself, which is
how recordable applications carry out their own commands.

### Edition Manager

Publishers and subscribers, registered across all
the applications running, and editions written and read through the
application's edition opener, the standard one or its own, with the
standard formats (`TEXT`, `PICT`, `snd `) or any others. When a
publisher's edition changes, its automatic subscribers get a Section
Read event; a publisher whose container has gone gets an empty one and
a Section Write event; `GoToPublisherSection` sends a Section Scroll.
These are Apple events, sent only to applications whose `SIZE` says they
take high-level events. An edition container's data fork is in macrt's
own format, since Inside Macintosh doesn't document Apple's, so macrt
finds no formats in one a real Mac wrote. The Publish and Subscribe
dialogs are Standard File's, the publisher's with a preview of the
edition; the options dialog is macrt's own. The `itemOffset` an expanded
dialog's hook is given counts the items before the expansion: Standard
File's own (12 for a publisher, 9 for a subscriber), or the options
dialog's OK and Cancel (2). None of the dialogs uses `where` or a filter
procedure. `GoToPublisherSection` opens an unregistered publisher's
document only if its application is already running, since macrt
doesn't launch applications itself. `Gestalt`'s `edtn` says the
Edition Manager is there.

### Component Manager

Components registered from a `thng` resource
or by the program, found by their description, opened, called and
closed, with their own storage and A5 world, captured and delegated to.
The Sound control panel's sub-panels are components, in its own file.

### Communications Toolbox utilities

`AppendDITL`, `CountDITL` and
`ShortenDITL`, which add items to a dialog and take them away again, as
the Sound control panel does for each sub-panel. The rest of the
Communications Toolbox isn't there.

### PPC Toolbox

`PPCInit`, and ports opened and closed, as System 7's
applications do at startup, though nothing can connect to them. The
Process Manager names each application's own port. `Gestalt`'s `ppc `
says the toolbox is there and initialised, with no network, and `os  `
that `LaunchApplication` takes an `FSSpec` and can launch an
application without quitting.

### Thread Manager

Cooperative threads within one application (`ThreadDispatch`,
`$ABF2`), each with its own stack in the application heap, which take
turns as they yield to each other. The application's own code is its
main thread. `NewThread` makes a thread whose entry is called with its
parameter; when the entry returns, its result goes where `threadResult`
points and the thread ends. `YieldToThread`, `SetThreadState`,
`DisposeThread` (of another thread or of the thread itself),
`GetCurrentThread`, `GetThreadState`, the premade threads of
`CreateThreadPool`, the stack sizes, critical sections, and terminator
and switcher procedures are all there. A thread can yield from
anywhere, including from a filter or grow-zone procedure that macrt's
glue has called, and picks up where it left off: macrt runs each
thread's code on a host thread of its own, only one at a time.
Preemptive threads run as cooperative ones do, only when a thread
yields to them. macrt switches threads only when one yields, and never
at interrupt time, during a tick's tasks, or while a `'dcmp'` runs: a
yield then returns `threadProtocolErr`. A thread made with a stack size
of 0 gets a 24 KB stack, macrt's own default. `SetThreadScheduler` and
the calls macrt hasn't got return `unimpErr`, with their arguments
popped. `Gestalt`'s `thds` says the Thread Manager is there.

## Numbers and text

### SANE

`FP68K`'s arithmetic, comparisons, conversions and
environment calls, correctly rounded in every rounding direction and
precision, with the Apple Numerics Manual's exceptions, NaN codes and
halts; `Elems68K`'s logarithms, exponentials, powers, financial
functions, trigonometry and random numbers; and `PStr2Dec`, `CStr2Dec`
and `Dec2Str`.

### International Utilities

Dates and times written out as the `INTL`
resources say, `IUMetric`, `IUGetIntl`, `IUSetIntl`, and string
comparison and equality.

### Script Manager

The Roman script only. Character types, upper and
lower case and accents (`Transliterate`, `UppercaseText` and the like),
words (`FindWord`), line breaks (`StyledLineBreak`), `TruncString`,
justified text (`MeasureJust`, `DrawJust`, `Pixel2Char`), and the
Script Manager's variables. `Gestalt`'s `scr#` says there's one script.

## The machine, printing and sound

### The hardware

What programs find when they reach past the Toolbox to the machine, as
the first Macintosh games do (see [Games from
1984](running-apps.md#games-from-1984)):

- **The screen buffer.** The bitmap at `ScrnBase` mirrors the windows.
  What QuickDraw draws is written there, and what a program writes there
  shows in the window it lands on, at its next trap or within a tick if
  it calls none. What a program writes where no window is shows on the
  desk, which only `--host screen` has.
- **A Mac 512K's screen.** With `--size 512x342` the screen buffer is at
  `$7A700`, where a 512K or a Plus has it, for games that write there
  without asking `ScrnBase`. Most games from 1984 and 1985 expect that
  screen. Its alternate buffer is `$8000` below, at `$72700`. An
  application launched with `Launch`'s page option finds it in
  `CurPageOption`, and while the VIA's `vPage2` bit is clear the desk and
  the menu bar show the alternate buffer rather than the main one. The
  windows go on showing what's drawn in them. A Plus's RAM repeats through
  its 4 MB of addresses, so the screens are at `$3FA700` and `$3F2700` as
  well, and Apache Strike's flight writes them there. `BufPtr`, the top of what an
  application may use, and `ScrnBase` are each application's own. For an
  ordinary launch both are the screen's base, `$7A700`. An application
  launched with the alternate screen (a negative page option) gets the
  memory a Plus with 4 MB gives it: its partition ends just under
  `$3F2700`, with its A5 world under the alternate screen, and its
  `ScrnBase` and `BufPtr` are both `$3FA700`, as are its QuickDraw's
  `screenBits.baseAddr` and the bit maps of the ports and windows it
  opens. MacRacquetball launches itself again until `BufPtr` is far
  enough over its A5, Arkanoid's demo stops unless `BufPtr` is
  `ScrnBase`, and Yeager's AFT launches itself again until its A5 is
  under `screenBits.baseAddr` less `$8000`. When that room is taken, as
  when Arkanoid's launcher, which is in it, launches the game, the
  partition goes lower, with its top `$8000` kept for the alternate
  buffers, and gets the same `ScrnBase` and `BufPtr`. A partition above
  `$400000` has `BufPtr` at its own top and `ScrnBase` at `$7A700`.
- **The VIA**, at `$EFE1FE`: the mouse button, the vertical blanking flag
  at each tick, the one-second flag, the two timers, and which screen
  buffer is shown. Above it, where a Plus looks for a test ROM, there is
  none, and reading there is no bus error. On a Plus, `$C00000`, where
  macrt's Mac II has its Apple Sound Chip (`ASCBase`), is empty the same
  way: it reads 0, and what's written there goes nowhere. Excel 1.5
  reads two words there and compares them with 'RA'.
- **The exception vectors.** What the System leaves in them is in a page
  of ROM over the top of RAM, at `ROMBase`, as on a Mac. A program can put
  its own handler in any of them, for a `TRAP`, dividing by zero or `CHK`,
  and it comes back past the instruction that raised it. A handler for the
  VIA's interrupts, at `$64`, is interrupted at each tick and once a
  second, from the next tick on; it goes on to the ROM's, as
  Flight Simulator's does, to clear them. When the program quits, the
  vectors it took are the System's again. The A-line vector, `$28`, holds
  the ROM's trap dispatcher, and the start-up code that puts it there is
  in the ROM too, `LEA` then `MOVE.L A0,$28`, for programs such as Lotus
  Jazz that look for it there to put it back; a jump to the dispatcher
  with a trap's address stacked does the trap.
- **The drives.** A floppy disk's image is in drive 1, the internal
  drive, with the floppy disk driver's reference number, -5. macrt's own
  disk is a hard disk, from drive 3. Flight Simulator looks for its disk
  in drive 1. Each drive is in the drive queue at `DrvQHdr`, with the
  flags before its element: a floppy drive's disk in place, a hard
  disk's 8 (it can't be ejected).
- **The floppy disk driver**, `.Sony`. `Read` and `Write` with reference
  number -5 read and write the disk in drive 1 or 2 by sector, as copy
  protection and disk tools do, with each sector's 12-byte tag at
  `TagData` and in the buffer `Control` 8 gives it; `Status` 8 is the
  drive's. What's written stays in memory, as with the File Manager's
  writes to a disk image. A program can put its own routine in the 64K
  ROM's `RdData` vector to read what's on the disk after a sector's
  address field; macrt calls it with a MOOF image's bytes there coming in
  through the IWM, the floppy controller, at `$DFE1FF`, and with A3
  pointing at the ROM's table of the six bits each byte from the disk
  stands for (indexed by the byte, $FF for one that stands for none), as
  MacGammon's routine expects. The routine can
  go on to the ROM's, which `RdData` held, with A1 pointing at the five
  marks the data field begins and ends with: it reads the field into the
  buffer at A0, or compares it with the buffer while `DskVerify` is set.
- **What the Event Manager keeps in low memory:** the mouse (`Mouse` and
  `RawMouse`; and `MTemp`, which counts its movement up, for a program
  that clears it to see how far it has moved) and its button (`MBState`),
  the keys held down (`KeyMap`, which `GetKeys` reads; an arrow key by
  both a Mac Plus's code and ADB's) and the last key
  pressed (`KeyLast`, `KeyTime`). They follow the mouse with the button up
  too. A program can move the mouse itself, writing `RawMouse` and setting
  `CrsrNew`, and macrt's cursor task copies it to `Mouse` at the next tick
  and clears `CrsrNew`, which Pirates! waits for. While the program has
  the cursor hidden, the mouse stays where it put it and goes on from
  there as the host's pointer moves, which is how Crystal Quest steers;
  with the cursor showing, it's back under the pointer when that moves.
  A mouse the program has moved stays on the screen: it stops at the
  edge, and comes back from it as soon as the pointer turns round. The
  events it's in say where it is, not where the pointer is, so Labyrinth
  of Crete, which draws its own cursor there, takes its clicks where its
  cursor is.
- **`--speed`**, for games that run as fast as the processor goes. A
  modern computer runs them hundreds of times too fast; `--speed 1` is
  about a Mac Plus. Each instruction is counted by the cycles it would
  take a 68000, and the 68k code is held to the 6 million a second a
  Plus's gets through, its RAM shared with the video and the sound. A
  game that times its frames by the ticks, as Downhill Racer does, sees
  them take as long as on a Plus. `TickCount` is what `Ticks` holds, as
  the ROM's is, and `Ticks` changes at each tick, so a program that reads
  one and then the other sees the same count.

On a Mac II (`--model ii` or `iix`):

- The screen's video driver (`.Display_Video_Apple_TFB`, reference
  number -49), whose `Control` and `Status` calls set and get its mode
  (1, 2, 4 or 8 bits a pixel, up to the depth macrt started the screen
  at with `--depth`), its colours (`SetEntries`, `GetEntries`,
  `SetGray`), and fill it with grey (`GrayPage`). `InitGDevice`,
  `HasDepth` and `SetDepth` change the depth through it, and the ports
  on the screen go with it. An old `GrafPort`, or a `BitMap` whose
  pixels are the screen's, draws on a colour screen at its depth, in
  its colours, as Color QuickDraw's do. There's one page. Its gamma
  table starts linear, and `SetGamma` puts a copy of a program's own in
  its place (Designing Cards and Drivers, the gamma table data
  structure), though the colours shown aren't corrected by it. It has
  a device control entry in the unit table, with its card's slot fields
  (Inside Macintosh V, the Device Manager): Powerball takes the slot
  from it for `SlotVInstall`.
  Its calls still go to macrt's own driver.
- The screen's card, in slot 9, and the Slot Manager, which reads its
  declaration ROM. The ROM is macrt's own, laid out as Designing Cards
  and Drivers for the Macintosh Family describes a card's: its format
  block, with a CRC that checks, the board's sResource and the screen's
  (`catDisplay`, `typVideo`, `drSwApple`, and a `DrHW` of 0, macrt's own),
  whose driver is the video driver above, and a mode list for each mode
  the driver has, with its video parameters. Programs find it with
  `SNextTypeSRsrc` and the other searches, and read it with `SReadByte`,
  `SFindStruct`, `SGetBlock` and the rest, as the CyberFlix demos (Jump
  Raven, Lunicus, Western) do to see which depths the screen has.
  `SReadInfo`, `SReadFHeader`, `SFindDevBase` (the screen's pixels),
  the card's parameter RAM and the Slot Resource Table's calls are
  there too. The calls only the Operating System makes, and those that
  load and run a card's own code (`SGetDriver`, `SExec`), aren't. A Mac
  Plus has no slots: `GetTrapAddress` says the Slot Manager isn't
  there, and it finds no cards.
- The ADB Manager's device table (`CountADBs`, `GetIndADB`,
  `GetADBInfo`, `SetADBInfo`), with the keyboard at address 2 and the
  mouse at 3. macrt's keyboard and mouse come from the host, so their
  service routines, which a program can read and replace, are never
  called; `ADBOp` and `ADBReInit` aren't there. On a Mac Plus,
  `GetTrapAddress` says the ADB Manager isn't there.

The timing globals `TimeDBRA`, `TimeSCCDB` and `TimeSCSIDB`: the number
of times a DBRA can be executed a millisecond, and the SCC and the SCSI
chip accessed (Inside Macintosh: Operating System Utilities, Start
Manager, "Global Timing Variables"). macrt's figures are its own: with
`--speed`, how many times round a loop of a DBRA, and of a `TST.B (A0)`
and a DBRA, its cycle model lets the core go in a millisecond (600 and
333 at `--speed 1`); without it, 32767, the most a word holds as
programs take it, signed, since the core goes round faster than that.
Sesame Street Letters draws every other row of its scenes while
`TimeDBRA` is 5004 or less; with it 0, as macrt had it before, Letters
and Numbers both left every other row black.

### Printing Manager

The calls of System 6 and 7's `PrGlue`, and the
printer resource file and `.Print` driver that earlier applications
call through, printing to a PDF (see [Printing](running-apps.md#printing)). `PrGeneral`
answers that it can't do what's asked of it, as a printer without those
features does.

### Sound

The Sound Driver's square-wave, four-tone and free-form sounds, and
samples a program puts in the Mac Plus sound buffer, played through your
computer's sound output (see [Sound](running-apps.md#sound)). The Sound
Manager's original calls, `SndPlay` and the channels and their commands,
play through the same buffer, and so do the samples a program writes to
a Mac II's Apple Sound Chip; `SndSoundManagerVersion` says it's version
2.0, System 7's, or 3.0 on a Mac II when the disk's System is 7.5 or
later, and has its double buffering (`SndPlayDoubleBuffer`),
`SndManagerStatus`, `SndChannelStatus`, the system beep's state and the
sound preferences.

Play from disk (`SndStartFilePlay`, `SndPauseFilePlay`,
`SndStopFilePlay`) plays AIFF and AIFF-C files and the sampled sounds of
`'snd '` resources, with their audio selections, and calls the program's
completion routine when the sound ends. A resource's sound is read from
its file, or from its handle if the program has it in memory, with no
handle made for it, so the play leaves the program's heap as it was.

While a program waits for a sound to be done, as `SndPlay` and
`SndStartFilePlay` do when they aren't asynchronous, and
`SndDisposeChannel` and `SndStopFilePlay` when they aren't to be quiet
now, the host waits for each tick, and has the processor in between: the
500 Mac Games CD's Galaxis plays its sounds so while it waits for the
user, and took one of the host's processors, and takes about 4% of one
now.

The Sound Input Manager finds no device to record from, as on a Mac Plus
or a Mac II, which have none, but its calls that make a header for
recorded sound to go after (`SetupSndHeader`, `SetupAIFFHeader`) work,
for uncompressed, MAC3 and MAC6 sound, and Gestalt says it's there.
Speech isn't there yet.

## Other managers

Gestalt, `SysEnvirons`, the Notification Manager (Gestalt's `nmgr` says
it's there; a request's message is shown at once, and its response
procedure called at the next `GetNextEvent` or `WaitNextEvent` once the
alert has been dismissed), the date and time, `SysBeep`, VBL tasks
(`VInstall` and `VRemove`), run once a tick while their application runs
or waits, and the Time Manager (`InsTime`, `InsXTime`, `PrimeTime` and
`RmvTime`).

The Deferred Task Manager's `DTInstall`, which a program can also call
through `jDTInstall`, queues a deferred task in its application's own
queue, as `VInstall` does a VBL task, and answers a record of another
`qType` with `vTypErr` (Inside Macintosh: Processes, Deferred Task
Manager). macrt runs each task once, taking it out of the queue first,
with its `dtParm` in A1: one queued by a VBL or Time Manager task after
that tick's tasks, still at the interrupt's priority, and one the
program queues itself at the next tick. Bolo 0.99.2 and Bikaka 1.4 ask
`GetTrapAddress` whether it's there.

Below the interrupted program's stack, a VBL task, a Time Manager task,
a deferred task or a sound channel's callback finds what the interrupt
stacked before calling it: the processor's exception frame and the
registers its handler keeps, A0-A3 and D0-D3 (Inside Macintosh II-200).
Factory's sound callback writes a word above its return address.

Gestalt's hardware attributes (`hdwr`) are the model's: a Mac Plus has a
VIA, an SCC and SCSI, and a Mac II a second VIA and the Apple Sound Chip
as well, which Digital Messiah looks for and writes its music to.

On a Mac II the Slot Manager's VBL tasks (`SlotVInstall`, `SlotVRemove`)
run as `VInstall`'s do, the Start Manager's `GetVideoDefault` names the
screen's card, and `AttachVBL` accepts that card's slot for the primary
monitor and answers any other with `slotNumErr` (Inside Macintosh:
Processes, Vertical Retrace Manager); a Mac Plus has no slots, and
`GetTrapAddress` says these calls aren't there, so a program that looks
for them first, as NetTrek does, leaves them alone.

`HWPriv`'s cache calls, with no caches to flush. The Package Manager's
`InitPack`, `InitAllPacks`, `DILoad` and `DIUnload` have nothing to do,
every package being always there.

## Not yet

- Apple events and PPC sessions between applications;
- anything plugged into the serial ports, and AppleTalk;
- in custom Open and Save dialogs, the application's event filter and
  System 7's activation procedures;
- speech.

An application that calls these gets on as far as it can without them (see
[When it stops](running-apps.md#when-it-stops)).
