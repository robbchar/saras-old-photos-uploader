import json
from pathlib import Path

import pytest

from e2e_sheet import E2ESheet, ResetRefused, check_reset_allowed

TEST_SHEET_ID = "test-sheet-id"


def e2e_block(**overrides: str) -> dict[str, str]:
    block = {
        "sheet_id": "REPLACE_WITH_NEVER_LIVE",
        "test_sheet_id": TEST_SHEET_ID,
        "sheet_tab": "Test Sheet",
        "upload_log_tab": "Upload Log",
        "sync_log_tab": "Sync Log",
    }
    block.update(overrides)
    return block


def write_registries(
    tmp_path: Path, block: dict[str, str], live_projects: dict[str, dict[str, str]]
) -> tuple[Path, Path]:
    e2e_path = tmp_path / "e2e.json"
    live_path = tmp_path / "live.json"
    e2e_path.write_text(json.dumps({"collection_key": "lcps", "projects": {"e2e": block}}), encoding="utf-8")
    live_path.write_text(json.dumps({"collection_key": "lcps", "projects": live_projects}), encoding="utf-8")
    return e2e_path, live_path


def test_guard_returns_the_test_sheet_when_nothing_live_matches(tmp_path):
    e2e_path, live_path = write_registries(
        tmp_path, e2e_block(), {"photos": {"sheet_id": "real-sheet-id", "test_sheet_id": TEST_SHEET_ID}}
    )

    assert check_reset_allowed(e2e_path, live_path) == E2ESheet(
        sheet_id=TEST_SHEET_ID, data_tab="Test Sheet", upload_log_tab="Upload Log", sync_log_tab="Sync Log"
    )


def test_guard_allows_sharing_a_live_projects_test_sheet(tmp_path):
    e2e_path, live_path = write_registries(
        tmp_path, e2e_block(), {"photos": {"sheet_id": "REPLACE_WITH_REAL_SHEET_ID", "test_sheet_id": TEST_SHEET_ID}}
    )

    assert check_reset_allowed(e2e_path, live_path).sheet_id == TEST_SHEET_ID


def test_guard_refuses_a_live_sheet_id_in_any_project(tmp_path):
    e2e_path, live_path = write_registries(
        tmp_path,
        e2e_block(),
        {"photos": {"sheet_id": "real-sheet-id"}, "maps": {"sheet_id": TEST_SHEET_ID}},
    )

    with pytest.raises(ResetRefused, match="live sheet_id"):
        check_reset_allowed(e2e_path, live_path)


def test_guard_refuses_an_e2e_registry_with_a_real_sheet_id(tmp_path):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(sheet_id="some-real-id"), {})

    with pytest.raises(ResetRefused, match="placeholder"):
        check_reset_allowed(e2e_path, live_path)


@pytest.mark.parametrize("key", ["test_sheet_id", "sheet_tab", "upload_log_tab", "sync_log_tab"])
def test_guard_refuses_a_missing_setting(tmp_path, key):
    block = e2e_block()
    del block[key]
    e2e_path, live_path = write_registries(tmp_path, block, {})

    with pytest.raises(ResetRefused, match=key):
        check_reset_allowed(e2e_path, live_path)


def test_guard_refuses_an_unknown_project(tmp_path):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(), {})

    with pytest.raises(ResetRefused, match="no project 'other'"):
        check_reset_allowed(e2e_path, live_path, project="other")


def test_guard_refuses_an_unreadable_live_registry(tmp_path):
    e2e_path, _ = write_registries(tmp_path, e2e_block(), {})

    with pytest.raises(ResetRefused, match="cannot read"):
        check_reset_allowed(e2e_path, tmp_path / "missing.json")


def test_guard_refuses_a_live_registry_without_projects(tmp_path):
    e2e_path, live_path = write_registries(tmp_path, e2e_block(), {})
    live_path.write_text(json.dumps({"collection_key": "lcps"}), encoding="utf-8")

    with pytest.raises(ResetRefused, match="no projects"):
        check_reset_allowed(e2e_path, live_path)
