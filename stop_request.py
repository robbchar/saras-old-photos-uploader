"""The first interrupt asks a run to stop after its current item; the second stops it at once.

Ctrl-C sends SIGINT. The upload page's server stops a Windows child
with CTRL_BREAK, which Python receives as SIGBREAK."""
from __future__ import annotations

import os
import signal
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

# Leading newline: IA's progress bar leaves the cursor mid-line.
STOP_NOTICE = b"\ninterrupt received: stopping after the current item. Interrupt again to stop now.\n"


class StopRequest:
    """Set by the first interrupt; the run checks it between items."""

    def __init__(self) -> None:
        self.requested = False

    def handle(self, signum: int, frame: FrameType | None) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        # os.write, not print: a signal can land mid-print, and buffered streams aren't reentrant.
        try:
            os.write(2, STOP_NOTICE)
        except OSError:
            pass  # A closed or full stderr must not turn a stop request into a failed item.


def _interrupt_signals() -> list[int]:
    sigbreak = getattr(signal, "SIGBREAK", None)
    return [signal.SIGINT] if sigbreak is None else [signal.SIGINT, sigbreak]


@contextmanager
def stop_request_on_interrupt() -> Iterator[StopRequest]:
    """Installs the handler for the block, then restores whatever was there before.

    Must run on the main thread: signal.signal() installs handlers only there.
    The upload page (#29) drives the run in its own process for this reason."""
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "stop_request_on_interrupt() must run on the main thread; run the "
            "upload in its own process, not a worker thread."
        )
    request = StopRequest()
    previous = {signum: signal.signal(signum, request.handle) for signum in _interrupt_signals()}
    try:
        yield request
    finally:
        for signum, handler in previous.items():
            # None means the old handler wasn't set from Python; the default is the closest match.
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)


def request_stop(pid: int) -> None:
    """Send one graceful-stop signal to a running upload child.

    POSIX: SIGINT. Windows: CTRL_BREAK_EVENT, which the child (spawned in its
    own process group) receives as SIGBREAK. The receiving side turns the
    first signal into a stop-after-the-current-item; a second is a hard stop.
    """
    sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
    os.kill(pid, sig)
