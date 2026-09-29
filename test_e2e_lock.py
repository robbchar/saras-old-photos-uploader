import random
import secrets
from datetime import datetime, timedelta, timezone

import pytest

from e2e_lock import LockHeld, LockHolder, LockLost, RehearsalLock, RunIdentity, acquire_lock
from e2e_sheet import LOCK_TAB
from fake_sheets import TARGET, FakeSheets, http_error

LEASE = timedelta(minutes=30)
START = datetime(2026, 9, 24, 20, 0, 0, tzinfo=timezone.utc)
THIS_RUN = RunIdentity(host="this-host", pid=111, checkout="C:/checkouts/this", log_dir="C:/tmp/this/logs")
OTHER_RUN = RunIdentity(host="other-host", pid=222, checkout="C:/checkouts/other", log_dir="C:/tmp/other/logs")


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, minutes: float) -> None:
        self.now += timedelta(minutes=minutes)


def lock_holder(sheets: FakeSheets) -> LockHolder | None:
    return LockHolder.from_rows(sheets.rows(LOCK_TAB))


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def sheets() -> FakeSheets:
    return FakeSheets()


def test_acquiring_a_free_lock_writes_this_run_into_the_lock_tab(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == LockHolder(THIS_RUN, started=START, checked_in=START, expires=START + LEASE)
    assert lock.took_over_from is None


def test_acquiring_refuses_while_another_run_holds_an_unexpired_lock(sheets, clock):
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    clock.advance(minutes=29)

    with pytest.raises(LockHeld) as held:
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    for detail in ("another e2e rehearsal", "other-host", "pid 222", "C:/checkouts/other", "C:/tmp/other/logs", "2026-09-24T20:30:00Z"):
        assert detail in str(held.value)
    assert sheets.tabs[LOCK_TAB] == other.tab_id
    assert lock_holder(sheets) == other.holder


def test_acquiring_takes_over_an_expired_lock(sheets, clock):
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    clock.advance(minutes=30)

    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock.took_over_from == other.holder
    assert sheets.tabs[LOCK_TAB] == lock.tab_id != other.tab_id
    assert lock_holder(sheets) == lock.holder


def test_a_takeover_never_reuses_the_expired_tabs_id(sheets, clock, monkeypatch):
    sheets.put_tab(LOCK_TAB, 42, LockHolder(OTHER_RUN, START, START, START + LEASE).rows())
    clock.advance(minutes=45)
    draws = iter([41, 99])
    monkeypatch.setattr(secrets, "randbelow", lambda _limit: next(draws))

    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock.tab_id == 100


def test_tab_ids_do_not_repeat_under_a_seeded_random(sheets, clock):
    state = random.getstate()
    try:
        random.seed(1)
        first = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
        first.release()
        random.seed(1)

        assert acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock).tab_id != first.tab_id
    finally:
        random.setstate(state)


def test_two_runs_racing_for_a_free_lock_leave_it_with_the_first(sheets, clock):
    others: list = []
    sheets.before_next_batch = lambda: others.append(acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock))

    with pytest.raises(LockHeld, match="other-host"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == others[0].tab_id
    assert lock_holder(sheets) == others[0].holder


def test_two_runs_that_draw_the_same_tab_id_still_leave_the_lock_with_the_first(sheets, clock, monkeypatch):
    draws = iter([41, 41])
    monkeypatch.setattr(secrets, "randbelow", lambda _limit: next(draws))
    others: list = []
    sheets.before_next_batch = lambda: others.append(acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock))

    with pytest.raises(LockHeld, match="other-host"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock_holder(sheets) == others[0].holder


def test_two_runs_racing_for_an_expired_lock_leave_it_with_the_first(sheets, clock):
    acquire_lock(sheets, TARGET, RunIdentity("dead-host", 333, "C:/checkouts/dead", "C:/tmp/dead/logs"), LEASE, clock)
    clock.advance(minutes=45)
    others: list = []
    sheets.before_next_batch = lambda: others.append(acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock))

    with pytest.raises(LockHeld, match="other-host"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock_holder(sheets) == others[0].holder


def test_a_takeover_planned_before_the_holder_checks_in_fails_naming_the_holder(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=31)
    sheets.before_next_batch = lock.check_in

    with pytest.raises(LockHeld, match="this-host"):
        acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == lock.holder


def test_a_takeover_that_meets_the_old_holders_own_release_fails_saying_to_re_run(sheets, clock):
    old = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    clock.advance(minutes=31)
    sheets.before_next_batch = old.release

    with pytest.raises(LockHeld, match="deleted while this run was taking it over; re-run"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)


def test_a_lock_released_while_it_is_being_read_is_judged_as_it_was_read(sheets, clock):
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    sheets.after_next_get = other.release

    with pytest.raises(LockHeld, match="other-host"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)


def test_acquiring_refuses_a_lock_tab_that_names_no_run(sheets, clock):
    sheets.put_tab(LOCK_TAB, 7, [["typed by hand"]])

    with pytest.raises(LockHeld, match="names no rehearsal.*delete the tab"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == 7


def test_acquiring_refuses_a_hand_made_lock_tab_with_a_blank_cell(sheets, clock):
    sheets.put_tab(LOCK_TAB, 7, [["", "typed by hand"]])

    with pytest.raises(LockHeld, match="names no rehearsal"):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)


@pytest.mark.parametrize("error", [http_error("Internal error encountered.", status=500), TimeoutError("timed out")])
def test_acquiring_reraises_an_error_that_is_not_a_race(sheets, clock, error):
    sheets.fail_next_batch = error

    with pytest.raises(type(error)):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert LOCK_TAB not in sheets.tabs


@pytest.mark.parametrize("error", [http_error("Internal error encountered.", status=500), TimeoutError("timed out")])
def test_acquiring_keeps_a_lock_whose_batch_landed_though_its_response_was_lost(sheets, clock, error):
    sheets.fail_after_next_batch = error

    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == lock.holder


def test_taking_over_keeps_a_lock_whose_batch_landed_though_its_response_was_lost(sheets, clock):
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)
    clock.advance(minutes=31)
    sheets.fail_after_next_batch = TimeoutError("timed out")

    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert lock.took_over_from == other.holder
    assert sheets.tabs[LOCK_TAB] == lock.tab_id


def test_acquiring_deletes_its_own_tab_when_it_cannot_tell_whether_the_batch_landed(sheets, clock):
    def lose_the_network() -> None:
        sheets.fail_next_get = TimeoutError("timed out")

    sheets.before_next_batch = lose_the_network
    sheets.fail_after_next_batch = TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    assert LOCK_TAB not in sheets.tabs


def test_checking_in_extends_the_lease_from_now_under_a_new_tab_id(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    first_tab_id = lock.tab_id
    clock.advance(minutes=10)

    lock.check_in()

    now = START + timedelta(minutes=10)
    assert lock_holder(sheets) == LockHolder(THIS_RUN, started=START, checked_in=now, expires=now + LEASE)
    assert sheets.tabs[LOCK_TAB] == lock.tab_id != first_tab_id


def test_a_run_that_checked_in_still_refuses_another_run_past_its_first_lease(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=20)
    lock.check_in()
    clock.advance(minutes=20)

    with pytest.raises(LockHeld, match="this-host"):
        acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)


def test_checking_in_after_a_takeover_names_the_new_holder_and_leaves_its_lock_alone(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=31)
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)

    with pytest.raises(LockLost, match="lost the Test Sheet lock.*took it over.*other-host.*collision"):
        lock.check_in()

    assert lock_holder(sheets) == other.holder


def test_checking_in_after_the_lock_tab_was_deleted_says_so(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.delete_tab(LOCK_TAB)

    with pytest.raises(LockLost, match="'E2E Lock' tab was deleted"):
        lock.check_in()

    assert LOCK_TAB not in sheets.tabs


def test_checking_in_after_the_lock_tab_was_replaced_by_hand_says_it_names_no_rehearsal(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.delete_tab(LOCK_TAB)
    sheets.put_tab(LOCK_TAB, 7, [["typed by hand"]])

    with pytest.raises(LockLost, match="replaced with one that names no rehearsal"):
        lock.check_in()

    assert sheets.tabs[LOCK_TAB] == 7


@pytest.mark.parametrize("error", [http_error("Internal error encountered.", status=500), TimeoutError("timed out")])
def test_checking_in_reraises_an_error_that_is_not_a_lost_lock(sheets, clock, error):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.fail_next_batch = error

    with pytest.raises(type(error)):
        lock.check_in()

    assert sheets.tabs[LOCK_TAB] == lock.tab_id


def test_checking_in_keeps_a_swap_that_landed_though_its_response_was_lost(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=10)
    sheets.fail_after_next_batch = TimeoutError("timed out")

    lock.check_in()

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == lock.holder


def check_in_with_an_unknown_outcome(sheets: FakeSheets, lock: RehearsalLock) -> None:
    """The swap lands, then its response and the re-read are both lost."""

    def lose_the_network() -> None:
        sheets.fail_next_get = TimeoutError("timed out")

    sheets.before_next_batch = lose_the_network
    sheets.fail_after_next_batch = TimeoutError("timed out")
    with pytest.raises(TimeoutError):
        lock.check_in()


def test_a_check_in_after_one_whose_outcome_was_unknown_goes_through(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    check_in_with_an_unknown_outcome(sheets, lock)

    lock.check_in()

    assert sheets.tabs[LOCK_TAB] == lock.tab_id
    assert lock_holder(sheets) == lock.holder


def test_releasing_after_a_check_in_whose_outcome_was_unknown_deletes_the_newer_tab(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    check_in_with_an_unknown_outcome(sheets, lock)

    lock.release()

    assert LOCK_TAB not in sheets.tabs


def test_releasing_deletes_the_lock_tab_so_the_next_run_can_start(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)

    lock.release()

    assert LOCK_TAB not in sheets.tabs
    assert acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock).took_over_from is None


def test_releasing_after_a_takeover_leaves_the_new_holders_lock(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    clock.advance(minutes=31)
    other = acquire_lock(sheets, TARGET, OTHER_RUN, LEASE, clock)

    with pytest.raises(LockLost, match="other-host"):
        lock.release()

    assert sheets.tabs[LOCK_TAB] == other.tab_id


def test_releasing_after_the_lock_tab_was_deleted_says_so(sheets, clock):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.delete_tab(LOCK_TAB)

    with pytest.raises(LockLost, match="'E2E Lock' tab was deleted"):
        lock.release()


@pytest.mark.parametrize("error", [http_error("Internal error encountered.", status=500), TimeoutError("timed out")])
def test_releasing_succeeds_when_the_delete_landed_though_its_response_was_lost(sheets, clock, error):
    lock = acquire_lock(sheets, TARGET, THIS_RUN, LEASE, clock)
    sheets.fail_after_next_batch = error

    lock.release()

    assert LOCK_TAB not in sheets.tabs


def test_holder_rows_read_back_as_the_same_holder():
    holder = LockHolder(THIS_RUN, started=START, checked_in=START, expires=START + LEASE)

    assert LockHolder.from_rows(holder.rows()) == holder


@pytest.mark.parametrize(("field", "value"), [("pid", "one hundred"), ("expires", "tomorrow")])
def test_holder_rows_with_a_value_that_does_not_parse_name_no_holder(field, value):
    rows = LockHolder(THIS_RUN, started=START, checked_in=START, expires=START + LEASE).rows()

    assert LockHolder.from_rows([[name, value if name == field else text] for name, text in rows]) is None
