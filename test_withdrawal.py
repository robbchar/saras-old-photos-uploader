import pytest

from withdrawal import (
    BULK_WITHDRAW_LIMIT,
    SyncAction,
    WithdrawnValue,
    parse_withdrawn,
    read_withdrawn_cell,
    sync_action,
    withdrawn_error,
)


def test_read_withdrawn_cell_returns_the_value_and_any_row_error():
    assert read_withdrawn_cell({"withdrawn": "Yes"}) == (WithdrawnValue.YES, None)
    assert read_withdrawn_cell({}) == (WithdrawnValue.NO, None)
    assert read_withdrawn_cell({"withdrawn": " maybe "}) == (
        WithdrawnValue.BROKEN,
        withdrawn_error(" maybe "),
    )


@pytest.mark.parametrize(
    "cell", [None, "", "   ", "no", "No", "N", "n", "false", "FALSE", "False", "0", " no "]
)
def test_these_cells_mean_keep(cell):
    assert parse_withdrawn(cell) is WithdrawnValue.NO


@pytest.mark.parametrize(
    "cell", ["yes", "YES", "Yes", "y", "Y", "true", "TRUE", "x", "X", "1", "  yes\t"]
)
def test_these_cells_mean_withdraw(cell):
    assert parse_withdrawn(cell) is WithdrawnValue.YES


@pytest.mark.parametrize("cell", ["maybe", "2", "yes please", "nope", "-", "withdrawn", "o"])
def test_anything_else_is_broken(cell):
    assert parse_withdrawn(cell) is WithdrawnValue.BROKEN


def test_the_broken_message_names_the_column_the_value_and_the_spellings():
    message = withdrawn_error(" maybe ")

    assert message.startswith("'withdrawn' is 'maybe', which reads as neither yes nor no")
    assert "yes, y, true, x or 1" in message
    assert "no, n, false, 0 or a blank cell" in message
    assert message.isascii()


@pytest.mark.parametrize(
    ("value", "files_removed", "action"),
    [
        (WithdrawnValue.YES, False, SyncAction.WITHDRAW),
        (WithdrawnValue.NO, True, SyncAction.RESTORE),
        (WithdrawnValue.YES, True, SyncAction.UPDATE),
        (WithdrawnValue.NO, False, SyncAction.UPDATE),
    ],
)
def test_only_a_disagreement_with_ia_withdrawn_moves_files(value, files_removed, action):
    assert sync_action(value, files_removed) is action


def test_a_broken_value_has_no_sync_action():
    with pytest.raises(ValueError):
        sync_action(WithdrawnValue.BROKEN, files_removed=False)


def test_the_bulk_limit_is_ten():
    assert BULK_WITHDRAW_LIMIT == 10
