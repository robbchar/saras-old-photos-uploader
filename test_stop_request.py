import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import stop_request
from stop_request import stop_request_on_interrupt

PROJECT_ROOT = Path(__file__).resolve().parent


def test_nothing_is_requested_until_an_interrupt_arrives():
    with stop_request_on_interrupt() as request:
        assert request.requested is False


def test_the_first_interrupt_asks_for_a_stop_and_says_so(capfd):
    with stop_request_on_interrupt() as request:
        signal.raise_signal(signal.SIGINT)

        assert request.requested is True
    assert capfd.readouterr().err == (
        "\ninterrupt received: stopping after the current item. Interrupt again to stop now.\n"
    )


def test_the_second_interrupt_stops_at_once(capfd):
    with pytest.raises(KeyboardInterrupt):
        with stop_request_on_interrupt():
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
    capfd.readouterr()


def test_the_previous_handler_comes_back_afterwards():
    before = signal.getsignal(signal.SIGINT)

    with stop_request_on_interrupt():
        assert signal.getsignal(signal.SIGINT) is not before

    assert signal.getsignal(signal.SIGINT) is before


def test_the_previous_handler_comes_back_after_a_second_interrupt(capfd):
    before = signal.getsignal(signal.SIGINT)

    with pytest.raises(KeyboardInterrupt):
        with stop_request_on_interrupt():
            signal.raise_signal(signal.SIGINT)
            signal.raise_signal(signal.SIGINT)
    capfd.readouterr()

    assert signal.getsignal(signal.SIGINT) is before


@pytest.mark.skipif(
    not hasattr(signal, "SIGBREAK"), reason="SIGBREAK is Windows-only; the Mac needs only SIGINT"
)
def test_on_windows_a_break_asks_for_a_stop_too(capfd):
    with stop_request_on_interrupt() as request:
        signal.raise_signal(getattr(signal, "SIGBREAK"))

        assert request.requested is True
    capfd.readouterr()


def test_it_refuses_to_run_off_the_main_thread():
    """signal.signal() only works on the main thread; the guard says so clearly
    instead of letting a cryptic ValueError escape from a worker thread."""
    errors = []

    def enter_off_thread():
        try:
            with stop_request_on_interrupt():
                pass
        except Exception as exc:  # noqa: BLE001 - the test inspects the type it caught
            errors.append(exc)

    worker = threading.Thread(target=enter_off_thread)
    worker.start()
    worker.join()

    assert len(errors) == 1
    assert isinstance(errors[0], RuntimeError)
    assert "main thread" in str(errors[0])


def test_a_stderr_that_cannot_be_written_still_records_the_request(monkeypatch):
    def broken_write(fd, data):
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(stop_request, "os", SimpleNamespace(write=broken_write))

    with stop_request_on_interrupt() as request:
        signal.raise_signal(signal.SIGINT)

        assert request.requested is True


@pytest.mark.skipif(
    not hasattr(signal, "SIGBREAK"), reason="SIGBREAK is Windows-only; the Mac needs only SIGINT"
)
def test_on_windows_the_previous_break_handler_comes_back_afterwards():
    sigbreak = getattr(signal, "SIGBREAK")
    before = signal.getsignal(sigbreak)

    with stop_request_on_interrupt():
        assert signal.getsignal(sigbreak) is not before

    assert signal.getsignal(sigbreak) is before


def test_request_stop_sends_platform_signal(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    stop_request.request_stop(4321)
    expected = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT
    assert sent == [(4321, expected)]


def test_request_stop_makes_a_real_child_stop_gracefully(tmp_path):
    marker = tmp_path / "marker.txt"
    child_py = tmp_path / "child.py"
    child_py.write_text(
        "import sys, time\n"
        f"sys.path.insert(0, {str(PROJECT_ROOT)!r})\n"
        "import stop_request\n"
        "with stop_request.stop_request_on_interrupt() as stop:\n"
        "    for _ in range(200):\n"
        "        if stop.requested:\n"
        f"            open({str(marker)!r}, 'w').write('stopped'); break\n"
        "        time.sleep(0.05)\n",
        encoding="ascii",
    )
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    child = subprocess.Popen([sys.executable, str(child_py)], creationflags=flags)
    try:
        # Known, accepted trade-off (carried from the brief): a fixed sleep to let the
        # child's signal handler install before we signal it. Flaky-in-theory, not
        # worth replacing with a readiness handshake for a one-shot integration test.
        time.sleep(1.0)
        stop_request.request_stop(child.pid)
        child.wait(timeout=10)
        assert marker.read_text() == "stopped"
    finally:
        # Unconditional kill+wait (matches test_upload_lock.py:215-217): reaps the
        # child even when the wait above already timed out, so this can never leave
        # a zombie/defunct process behind.
        child.kill()
        child.wait(timeout=10)
