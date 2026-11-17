"""
What the Toolbox traps' glue shares: where the QuickDraw global thePort
is, the port it points at, the heap a block is in, and traps that do
nothing here.
"""

from __future__ import annotations

from ..toolbox.quickdraw import GrafPort


def _noop(name: str):
    """A trap that does nothing here: the host does it, or it doesn't apply."""
    def call(t, *args):
        pass
    call.__name__ = name
    return call


def _the_port_var(t) -> int:
    """Where the QuickDraw global thePort is: 0(A5) points at it."""
    return t.mem.r32(t.process.partition.a5) & t.mem.system.addr_mask


def _port(t) -> GrafPort | None:
    p = t.mem.r32(_the_port_var(t)) & t.mem.system.addr_mask
    return t.system.port_at(p) if p else None


def _heap(t, addr: int):
    """The heap a block is in; the application's for one that's in none."""
    return t.system.find_heap(addr) or t.process.heap
