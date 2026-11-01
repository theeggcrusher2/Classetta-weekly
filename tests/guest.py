"""Calling trap implementations from Python, without a CPU: a process
with an application's resources and a Toolbox, and enough of a Runner
for the glue to work with."""

from macrt.m68k import dispatch, runtime
from macrt.m68k.cpu import CPU
from macrt.m68k.hardware import VIA
from macrt.m68k.sound import SILENT_FRAME, Speaker, SpeakerSwitch
from macrt.m68k.sound_manager import SoundManager
from macrt.macfile import MacFile, MemoryFolder
from macrt.process import Process
from macrt.resfork import build_fork
from macrt.toolbox import lowmem, qdraw
from macrt.toolbox.api import Toolbox
from macrt.toolbox.events import EventQueue
from macrt.toolbox.quickdraw import GrafPort
from macrt.toolbox.system import System
from macrt.toolbox.types import Point, Rect


class NoSoundDriver:
    """A Sound Driver that takes none of the Device Manager's calls, and
    so plays nothing."""

    active = False
    frame = SILENT_FRAME

    def __init__(self, machine):
        self.switch = SpeakerSwitch(machine)

    @staticmethod
    def call(t, w: int) -> bool:
        return False


class Machine:
    """Trap stubs at made-up addresses: only what they are matters.  The
    Sound Manager, with a sound buffer, a VIA for its speaker switch, and
    the ROM's loop for its waits, and an RTS for the ADB devices' service
    routines."""

    def __init__(self, system: System):
        self.mem = system.mem
        self.via = VIA()
        self.sound = NoSoundDriver(self)
        self.sound_manager = SoundManager(self)
        self.speaker = Speaker(self.mem, self.via)
        self.mem.w32(lowmem.SoundBase, system.sys_heap.new_ptr(runtime.SOUND_BUFFER, clear=True))
        self.sound_wait = system.sys_heap.new_ptr(len(runtime.SOUND_WAIT))
        self.mem.write(self.sound_wait, runtime.SOUND_WAIT)
        self.adb_service = system.sys_heap.new_ptr(2)
        self.mem.w16(self.adb_service, runtime.RTS)
        self.ejected = {}                   # no disk is out of its drive

    @staticmethod
    def stub_for(key: int) -> int:
        return 0x800000 + 4 * (key & 0xFFF)


class Guest:
    """The trap context (runtime.Runner's part of it) for a process with
    the resources in rsrc.  call() records 68k calls instead of making
    them, and runs the Python function in procs for the address if there
    is one."""

    rect = runtime.Runner.rect
    set_rect = runtime.Runner.set_rect
    point = runtime.Runner.point
    set_point = runtime.Runner.set_point
    str255 = runtime.Runner.str255
    pstr = runtime.Runner.pstr
    set_str255 = runtime.Runner.set_str255
    mem_err = runtime.Runner.mem_err
    set_mem_err = runtime.Runner.set_mem_err
    w16 = runtime.Runner.w16
    w32 = runtime.Runner.w32
    write_event = runtime.Runner.write_event
    window = runtime.Runner.window
    note_once = runtime.Runner.note_once
    menu = runtime.Runner.menu
    ostype = staticmethod(runtime.Runner.ostype)
    holding = True              # the machine: there's only this thread
    in_wdef = 0
    thread_id = None            # no thread runs the process: no grow-zone, 'dcmp' or patch runs
    on_own_thread = runtime.Runner.on_own_thread
    call_grow_zone = runtime.Runner.call_grow_zone

    def __init__(self, resources=(), sharing=None, **system):
        """sharing: another Guest, for an application on its machine."""
        self.system = sharing.system if sharing is not None else System(**system)
        self.mem = self.system.mem
        app = MacFile("Test App", rsrc=build_fork(list(resources)), type=b"APPL",
                      creator=b"TEST")
        MemoryFolder([app])
        self.process = Process(self.system, "Test App", lambda tb: None,
                               EventQueue(lambda: (Point(0, 0), 0)), app=app)
        self.tb = Toolbox(None, self.process)
        self.machine = Machine(self.system)
        self.reported = set()
        self.calls = []
        self.procs = {}
        self.missing_asked = []

    def asked_missing(self, name: str) -> None:
        self.missing_asked.append(name)

    def __call__(self, name: str, *args):
        """Call the trap name with args, as the dispatcher would."""
        return dispatch.IMPLEMENTATIONS[name](self, *args)

    def call(self, addr: int, args=(), result: str = ""):
        self.calls.append((addr, args))
        proc = self.procs.get(addr)
        return proc(*(v for _, v in args)) or 0 if proc else 0

    def take_mouse_up(self) -> None:
        self.tb.take_mouse_up()

    @property
    def heap(self):
        return self.process.heap

    def ptr(self, data: bytes) -> int:
        p = self.heap.new_ptr(max(len(data), 1), clear=True)
        self.mem.write(p, data)
        return p

    def handle(self, data: bytes) -> int:
        h = self.heap.new_handle(len(data))
        self.mem.write(self.heap.deref(h), data)
        return h

    def contents(self, h: int) -> bytes:
        return self.tb.contents(h)

    def rect_ptr(self, r) -> int:
        p = self.ptr(bytes(8))
        self.mem.w_rect(p, Rect(*r))
        return p

    def port(self, width: int = 64, height: int = 32):
        """A port on a fresh, white bitmap, made the current port; the port
        and the bitmap's address."""
        row_bytes = (width + 15) // 16 * 2
        bounds = Rect(0, 0, height, width)
        bits = self.heap.new_ptr(row_bytes * height, clear=True)
        port = GrafPort(self.mem, self.heap.new_ptr(GrafPort.SIZE))
        port.open(self.heap, self.system.screen_bits)
        port.port_bits_base = bits
        port.port_bits_row_bytes = row_bytes
        port.port_bits_bounds = bounds
        port.port_rect = bounds
        qdraw.write_rgn(self.heap, port.vis_rgn, qdraw.Region.rect(bounds))
        self.system.register(port)
        self.set_port(port)
        return port, bits

    def set_port(self, port: GrafPort) -> None:
        self.mem.w32(self.mem.r32(self.process.partition.a5), port.addr)

    def rows(self, port: GrafPort) -> list[str]:
        """A port's bitmap as strings of . and #."""
        rb, b = port.port_bits_row_bytes, port.port_bits_bounds
        out = []
        for y in range(b.height):
            row = self.mem.read(port.port_bits_base + y * rb, rb)
            out.append("".join("#" if row[x >> 3] & (0x80 >> (x & 7)) else "."
                               for x in range(b.width)))
        return out


class Processor(Guest):
    """A Guest with a CPU: run() executes 68k code, and its A-line traps
    go to the dispatcher as the runtime sends them."""

    def __init__(self, resources=(), sharing=None, **system):
        super().__init__(resources, sharing, **system)
        self.cpu = CPU(self.mem)
        self.stack_top = self.ptr(bytes(0x100)) + 0x100
        self.cpu.sp = self.stack_top
        self.missing = []

    def unimplemented(self, name, word):
        self.missing.append(name)

    def run(self, code: bytes) -> None:
        start = self.ptr(code)
        self.cpu.pc = start
        while (ev := self.cpu.run(start + len(code))).kind == "trap":
            auto_pop = dispatch.is_toolbox(ev.code) and ev.code & 0x400
            self.cpu.pc = self.cpu.pop32() if auto_pop else ev.pc + 2
            dispatch.dispatch(self, ev.code, ev.pc)
        assert ev.kind == "until", ev


class Room:
    """Room in the application and System heaps for only so many more
    blocks (a handle grown counts as one), what's asked for after that not
    there; and what was made, to see that a call that fails gives it back."""

    def __init__(self, monkeypatch, g):
        self.left, self.made = None, []
        for heap in (g.heap, g.system.sys_heap):
            for name in ("new_handle", "new_ptr", "set_handle_size"):
                monkeypatch.setattr(heap, name, self._wrap(heap, name, getattr(heap, name)))

    def limit(self, blocks: int | None) -> None:
        self.left, self.made = blocks, []

    def _wrap(self, heap, name, real):
        def wrapped(*args, **kwargs):
            if name == "set_handle_size" and args[1] <= heap.handle_size(args[0]):
                return real(*args, **kwargs)
            if self.left is not None:
                if self.left == 0:
                    return False if name == "set_handle_size" else 0
                self.left -= 1
            got = real(*args, **kwargs)
            if got and name != "set_handle_size":
                self.made.append((heap, got))
            return got
        return wrapped

    def given_back(self) -> bool:
        return not any(heap.is_handle(b) or heap.is_ptr(b) for heap, b in self.made)
