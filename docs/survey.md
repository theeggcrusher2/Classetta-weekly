# Surveying many applications

Part of [the documentation](README.md).

`python -m macrt.survey` runs Mac applications with no windows on your
desktop and reports what each did. It's how macrt is tested against
real software: a list of hundreds of titles, run before and after a
change, shows what the change fixed and what it broke.

- [A list of runs](#a-list-of-runs)
- [Steps](#steps)
- [Waiting until the screen settles](#waiting-until-the-screen-settles)
- [Running the list](#running-the-list)
- [What each run leaves](#what-each-run-leaves)
- [The summary](#the-summary)
- [Comparing two surveys](#comparing-two-surveys)
- [One run on its own](#one-run-on-its-own)

## A list of runs

A survey runs a list of applications, several at a time, and reports
on each one: whether it's
still running, has quit, or stopped with a system error, which calls it
made that macrt doesn't implement, and what its screen looked like. It
takes a list of runs as JSON:

```json
{"defaults": {"wait": 8},
 "runs": [
  {"file": "Disks/Dark Castle.dsk", "app": "Dark Castle", "steps": "s title;c 320,240;w 5"},
  {"file": "Disks/Crystal Quest.dsk", "model": "ii", "depth": 8},
  {"name": "word", "file": "Word 1.0.dsk", "app": "Microsoft Word",
   "steps": "k Hello\\nthere;s typed;C q;w 2"}
 ]}
```

```sh
python -m macrt.survey run titles.json --out survey1 --jobs 4
```

A run's fields are the command line's options: `file` (an application, a
disk image or a list of them), `app`, `model`, `depth`, `size`,
`row_bytes`, `memory`, `system`, `speed`, `settle`, `trace` and
`outside_port_rect`. `name` is the run's folder in the output folder, by
default the application's name. Paths are relative to the list's folder.
`defaults` fills in what a run leaves out.

## Steps

Each run waits `wait` seconds after launching (6 by default), then does its
`steps`, separated by `;`:

| Step | What it does |
| --- | --- |
| `w 2.5` | wait 2.5 seconds |
| `W 10` | wait until the screen has settled (below), 10 seconds at most |
| `c 320,240` | click at h 320, v 240 on the Mac's screen |
| `d 10,10 50,50 90,10` | press at the first point, drag through the others, let go at the last |
| `k some text` | type it a key at a time; `\n` is Return, `\t` Tab, `\;` a semicolon, `\\` a backslash |
| `K Return` or `K Left,1.5` | press a key by its name, holding it that many seconds |
| `C q` | Command-Q |
| `s title` | save a screenshot, `title.png` |
| `O` | OK the alert macrt shows for a system error |

While one of macrt's own dialogs is up (Standard File's, an alert), `k`,
`K` and `C` type into it, as the keyboard would, so `k Player 1\n` names a
file in a Save dialog and saves it.

## Waiting until the screen settles

Most titles have finished drawing long before a wait is over. With
`"settle": true` (or `--settle`), the wait after launching ends once the
screen has settled, and a `W` step's wait does the same. macrt looks at the
screen ten times a second. It has settled once every process has been
asking for input for the last 1.5 seconds, and the last 0.75 seconds
showed the same pictures as the 0.75 seconds before, two at most, so that
a blinking caret doesn't keep it waiting. A process is asking for input
while it waits for an event in WaitNextEvent or GetNextEvent, or while it
asks over and over whether there's one (EventAvail, a GetNextEvent that
returns at once) or whether the mouse button or a key is down (Button,
StillDown, GetKeys). But for a quarter of a second after it reads the
clock with TickCount or Delay, it isn't, whether it waits for events
between or not: a screen it shows until it's time to go on isn't waiting
for the user. One of macrt's own dialogs being up counts as asking too.
`settled` in `result.json` is how many seconds after launching the wait
ended, or `null` if it went on to the end.

That's a guess at when a title is waiting for the user, and it can be
wrong both ways:

- A program that times a screen by reading the Ticks global itself, by
  the times of the events it gets, or by counting them, looks as if it's
  waiting for the user: the wait can end on a screen that would have moved
  on by itself.
- A title that animates, or reads the clock as it waits for the user,
  never settles, and takes the whole wait, as a plain one would.

On the 472 runs of a list of the 500 Mac Games CD's titles, with waits of
8 seconds and two runs at a time, the survey took 2066 seconds with plain
waits and 975 with settled ones; 381 of the waits ended early. Every run
had the same outcome either way, and made the same unimplemented calls.
80 end screenshots differed from a plain run's, where two plain runs
differed in 67, mostly in random play, a blinking caret or label, or an
animation caught at another moment. Five titles' waits ended on a screen
they would have left by themselves 1 to 4 seconds later, as the first
case above says.

## Running the list

The whole list is checked first, so a mistake in a field or a step stops
the survey before any run starts. Each run is a process of its own, so one
that crashes Python can't stop the others. A run has as long as its wait
and steps take and two minutes more, unless it says `timeout` or
`--timeout` does. One that takes longer is stopped, and its threads' stacks
go in its log.

A survey of hundreds of titles takes a while. If it's stopped part way,
run it again with `--resume`: the runs that finished, those with a
`result.json`, are kept, the rest run, and the summary counts them all.
`--only NAME` runs just the run of that name; repeat it for several.

## What each run leaves

Each run's folder has `result.json`, `log.txt` with all macrt printed, its
screenshots, and `end.png`, the screen when the steps are done (unless it
had quit). A screenshot shows a menu pulled down, and macrt's own dialogs
(Standard File's, its alerts) over the Mac's screen. Its outcome is one of
these:

| Outcome | Meaning |
| --- | --- |
| running | still running when the steps were done |
| quit | quit by itself, or when told to |
| system error | stopped with a system error; `result.json` has its ID and message |
| internal error | macrt itself failed: a bug in macrt, with a Python traceback |
| faulted | its records broke macrt's drawing (a bad handle in a window record, say), so macrt stopped it |
| no code | the file has resources but nothing to run (`--view` shows them) |
| can't open | the file couldn't be read, or isn't an application |
| timeout | took longer than its time |
| crashed | the process ended without a result: see `log.txt` |

`result.json` also lists each process's windows, front to back, with
their titles. A dialog or alert left up gives its text as well, ParamText's
strings put in, so a title that is "running" but waiting at its own alert
("This program needs 512K", say) shows what it asked. `host_dialogs` lists
macrt's own dialogs left up, Standard File's or an alert, with their
titles and what they say, since they aren't the Mac's windows: a program
that asks at once for a file to open has no window of its own to show.
`first_window` is how many seconds after launching its first window came
up, and `settled`, with `settle`, when the wait settled. `busy` is the
share of one of the host's processors the run took, all its threads, in
the last second of that wait: near 0 for a title that waits for events,
near 1 for one that keeps asking or drawing while it waits.

With `"outside_port_rect": true` (or `--outside-port-rect`),
`outside_port_rect` lists the ports the title drew in outside their
portRect, which macrt doesn't clip drawing to (see
[QuickDraw](toolbox.md#quickdraw)): one entry a port, portRect, bit map's
bounds and QuickDraw routine of macrt's that drew, with the port's
address, how many `times`, and the `box` round where, in the port's
coordinates. macrt keeps a window's visRgn within its
portRect, so a window shows up only if the program has changed its visRgn
itself. Before changing how macrt clips, survey a list with it: the
titles listed are the ones the change could alter.

## The summary

The summary at the end, also in `report.json`, counts the outcomes and
ranks the calls macrt doesn't implement by how many applications make
them. That's the list to work from: implementing the call at its top
helps the most titles. Of the titles still running, it names those with no
window to show, and gives the text of the dialog or alert each other one is
waiting at, if any, its own or macrt's (a Standard File dialog's title
first): most are opening screens and shareware notices, but an
error ("can't find the file", say) stands out without a look at every
screenshot. Then it names the runs slowest to show a window, where making
macrt faster would be felt first, and last the runs still running that
were at least half busy at the end of their wait, busiest first: titles
that keep the host's processor going while they wait for the user. With
`outside_port_rect`, it names the runs that drew outside a portRect last.
`python -m macrt.survey report survey1` prints the summary again.

## Comparing two surveys

To see what a change to macrt changed, run the same list before and
after it and compare the two:

```sh
python -m macrt.survey compare survey1 survey2
```

```
Dark_Castle: running -> system error
word: unimplemented -HMGetHelpMenuHandle
word/typed.png: 812 pixels differ in (14, 40, 210, 52)
```

That names each run whose outcome changed, the calls it no longer makes
or newly makes, and each screenshot that differs, with the box round the
pixels that do. It exits 1 when there are differences and 0 when there
are none, so a script can use it. A title whose screen moves by itself (a
game's animation, a clock) differs from run to run: take its screenshots
where it stands still, or after `w` long enough for it to.

## One run on its own

`one` runs a single application in the survey's own process, with the
same options as a run in a list, given on the command line, and leaves
its folder as a run in a list does:

```sh
python -m macrt.survey one "Disks/Dark Castle.dsk" --app "Dark Castle" \
    --wait 8 --settle --steps "s title;c 320,240;w 5" --out survey1/dark
```

Its options are `--app`, `--model`, `--depth`, `--size`, `--row-bytes`,
`--memory`, `--system`, `--speed`, `--wait`, `--settle`, `--steps`,
`--trace-traps` and `--outside-port-rect`. It's the quickest way to
look again at one title from a survey, or to try steps out before
putting them in a list.

`report` takes `--top N` for how many of the calls macrt doesn't
implement it lists (20 by default).
