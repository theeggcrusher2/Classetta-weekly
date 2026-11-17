# macrt's documentation

macrt runs classic Mac applications, the programs written for 68000-family
Macs and System 1 to 7, by running their own 68k code on an emulated
processor and answering their Toolbox and Operating System calls in Python.
These pages say how to use it, what it implements, and how it's built.

| Page | What it's for |
| --- | --- |
| [Running a Mac application](running-apps.md) | Installing macrt, getting an application to it with its resource fork, every option, and what to do when it stops |
| [Titles tried so far](titles.md) | The applications run so far, how far each gets, and what to give it on the command line |
| [What works](toolbox.md) | The Toolbox and Operating System a manager at a time: what macrt does, and where and why it differs from a Mac |
| [How macrt works](architecture.md) | The processor, the trap dispatcher, memory, drawing, resources and files, and the source tree |
| [Working on macrt](development.md) | The tests and what each covers, the 68k test applications, and adding a Toolbox routine |
| [Surveying many applications](survey.md) | Running hundreds of titles headless, and comparing two surveys to see what a change did |

## Where to start

**To run an application,** read [Running a Mac
application](running-apps.md) from the top: the [Quick
start](running-apps.md#quick-start) gets a test application running, and
[Getting the application to macrt](running-apps.md#getting-the-application-to-macrt)
covers the forms a Mac file comes in, from MacBinary to disk images. Then
look your title up in [Titles tried so far](titles.md), which often has
the command line it needs. If it stops, [When it
stops](running-apps.md#when-it-stops) says what macrt's messages mean.

**To see what macrt can do,** [What works](toolbox.md) goes through the
managers grouped by what they're for, from memory and drawing to files,
processes and sound, and ends with what isn't there yet.

**If you know the 68k and the Mac's insides,** start with [How macrt
works](architecture.md). It covers which processor a program sees and the
exception frames it gets, how an A-line trap word is decoded and
dispatched, the memory map, partitions and A5 worlds, how QuickDraw's
records stay real in guest memory while Qt shows the result, and how the
resource chain and the File Manager are built. [What works](toolbox.md)
then has the details a manager at a time, with the Inside Macintosh
chapter or Technical Note behind each behaviour, and which program needed
it. Where macrt has to decide something the documentation doesn't say, it
says so: those rules are macrt's own.

**To change macrt,** read [Working on macrt](development.md) for the
tests and the test applications, and [Surveying many
applications](survey.md) for checking a change against real software.
