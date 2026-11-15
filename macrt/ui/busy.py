"""
macrt.ui.busy - A busy window for work that runs for minutes.

Decoding a 200 MB archive member's fork on the GUI thread froze the
Filer until the decode finished — and an OS-level not-responding
dialog with it, since Qt never saw a paint event.  This runs the work
on a worker thread and keeps the GUI's event loop live behind a busy
window.  A declared-empty fork costs nothing, so ordinary members are
decoded in milliseconds with no window ever shown.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import Future

from PyQt6.QtCore import QEventLoop, Qt, QTimer
from PyQt6.QtWidgets import QProgressDialog

# How long the work may run before a busy window is shown; below this
# the GUI thread returns before the window can flash up.
SHOW_AFTER_MS = 300


def run_busy(parent, title: str, message: str, work):
    """Run ``work()`` off the GUI thread behind a busy window.

    Returns its result, or None when the user stopped waiting — the
    work keeps running to completion in the background, and its result
    stays in the archive's cache for the next touch, so stopping early
    is never wasted work.  A failure raises, as the caller's usual
    open path does (the archive decoders already convert their errors
    to ValueError).
    """
    future: Future = Future()

    def _worker():
        try:
            future.set_result(work())
        except BaseException as error:      # noqa: BLE001 - re-raised here
            future.set_exception(error)

    threading.Thread(target=_worker, daemon=True).start()

    dialog = QProgressDialog(message, "Stop Waiting", 0, 0, parent)
    dialog.setWindowTitle(title)
    dialog.setRange(0, 0)                    # busy: the decode gives no count
    dialog.setWindowModality(Qt.WindowModality.WindowModal)
    dialog.setAutoReset(False)
    dialog.setAutoClose(False)

    started = time.monotonic()
    shown = False
    loop = QEventLoop()

    def _poll():
        nonlocal shown
        if future.done():
            loop.quit()
            return
        if not shown and time.monotonic() - started >= SHOW_AFTER_MS / 1000:
            dialog.show()
            shown = True

    timer = QTimer()
    timer.timeout.connect(_poll)
    timer.start(100)
    dialog.canceled.connect(loop.quit)
    if not future.done():
        loop.exec()
    timer.stop()
    dialog.close()

    if future.done():
        return future.result()
    return None                     # stopped waiting; the work runs on
