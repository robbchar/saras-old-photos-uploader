"""The first interrupt asks a run to stop after its current item; the second stops it at once.

Ctrl-C sends SIGINT. The upload page's server (#29) stops a Windows child
with CTRL_BREAK, which Python receives as SIGBREAK."""
from __future__ import annotations

import os
import signal
from collections.abc import Iterator
from contextlib import contextmanager
from types import FrameType

STOP_NOTICE = b"interrupt received: stopping after the current item. Interrupt again to stop now.\n"


class StopRequest:
    """Set by the first interrupt; the run checks it between items."""

    def __init__(self) -> None:
        self.requested = False

    def handle(self, signum: int, frame: FrameType | None) -> None:
        if self.requested:
            raise KeyboardInterrupt
        self.requested = True
        # os.write, not print: a signal can land mid-print, and buffered streams aren't reentrant.
        os.write(2, STOP_NOTICE)


def _interrupt_signals() -> list[int]:
    sigbreak = getattr(signal, "SIGBREAK", None)
    return [signal.SIGINT] if sigbreak is None else [signal.SIGINT, sigbreak]


@contextmanager
def stop_request_on_interrupt() -> Iterator[StopRequest]:
    """Installs the handler for the block, then restores whatever was there before."""
    request = StopRequest()
    previous = {signum: signal.signal(signum, request.handle) for signum in _interrupt_signals()}
    try:
        yield request
    finally:
        for signum, handler in previous.items():
            # None means the old handler wasn't set from Python; the default is the closest match.
            signal.signal(signum, signal.SIG_DFL if handler is None else handler)
