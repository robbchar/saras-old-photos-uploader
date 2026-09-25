import signal
import threading
from types import SimpleNamespace

import pytest

import stop_request
from stop_request import stop_request_on_interrupt


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
