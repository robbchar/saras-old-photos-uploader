import signal

import pytest

from stop_request import stop_request_on_interrupt


def test_nothing_is_requested_until_an_interrupt_arrives():
    with stop_request_on_interrupt() as request:
        assert request.requested is False


def test_the_first_interrupt_asks_for_a_stop_and_says_so(capfd):
    with stop_request_on_interrupt() as request:
        signal.raise_signal(signal.SIGINT)

        assert request.requested is True
    assert capfd.readouterr().err == (
        "interrupt received: stopping after the current item. Interrupt again to stop now.\n"
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
