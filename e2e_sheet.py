"""Test-only writes to the Test Sheet for the e2e rehearsal (test_e2e_rehearsal.py).

Every write takes an E2ESheet, and only check_reset_allowed builds one.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ia_bulk import PLACEHOLDER_SHEET_ID_PREFIX
from sheet_client import SheetClient, column_letter, quote_tab

E2E_PROJECT = "e2e"


class ResetRefused(Exception):
    """The guard found a reason not to write to this Sheet."""


@dataclass(frozen=True)
class E2ESheet:
    """A Sheet the guard cleared for rewriting. Build it only via check_reset_allowed."""

    sheet_id: str
    data_tab: str
    upload_log_tab: str
    sync_log_tab: str

    @property
    def log_tabs(self) -> tuple[str, str]:
        return (self.upload_log_tab, self.sync_log_tab)


def _read_registry(path: Path) -> dict[str, Any]:
    try:
        registry = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ResetRefused(f"cannot read registry {path}: {exc}") from exc
    if not isinstance(registry, dict) or not isinstance(registry.get("projects"), dict):
        raise ResetRefused(f"registry {path} has no projects map")
    return registry


def _required(block: dict[str, Any], key: str, project: str) -> str:
    value = str(block.get(key) or "").strip()
    if not value:
        raise ResetRefused(f"project '{project}' has no {key}")
    return value


def check_reset_allowed(
    e2e_registry_path: Path, live_registry_path: Path, project: str = E2E_PROJECT
) -> E2ESheet:
    """Fails closed: any doubt about either registry refuses."""
    e2e_registry = _read_registry(e2e_registry_path)
    live_registry = _read_registry(live_registry_path)

    block = e2e_registry["projects"].get(project)
    if not isinstance(block, dict):
        raise ResetRefused(f"{e2e_registry_path} has no project '{project}'")

    own_live_id = str(block.get("sheet_id") or "")
    if not own_live_id.startswith(PLACEHOLDER_SHEET_ID_PREFIX):
        raise ResetRefused(
            f"'{project}' sheet_id must stay a {PLACEHOLDER_SHEET_ID_PREFIX}... placeholder so it can never run --live"
        )

    target = E2ESheet(
        sheet_id=_required(block, "test_sheet_id", project),
        data_tab=_required(block, "sheet_tab", project),
        upload_log_tab=_required(block, "upload_log_tab", project),
        sync_log_tab=_required(block, "sync_log_tab", project),
    )
    # The reset deletes the log tabs; one named like the data tab would take the data tab with it.
    if target.data_tab in target.log_tabs:
        raise ResetRefused(f"'{project}' names the data tab '{target.data_tab}' as a log tab")

    live_ids = {
        str(live_block.get("sheet_id") or "").strip()
        for live_block in live_registry["projects"].values()
        if isinstance(live_block, dict)
    }
    if target.sheet_id in live_ids:
        raise ResetRefused(f"{target.sheet_id} is a live sheet_id in {live_registry_path}; refusing to write to it")
    return target


def tab_ids(service: Any, sheet_id: str) -> dict[str, int]:
    response = service.spreadsheets().get(spreadsheetId=sheet_id, fields="sheets.properties(sheetId,title)").execute()
    return {sheet["properties"]["title"]: sheet["properties"]["sheetId"] for sheet in response.get("sheets", [])}


def reset_test_sheet(service: Any, target: E2ESheet, grid: list[list[str]]) -> None:
    """Replace the data tab with `grid` and delete each log tab that exists, so the next run recreates them."""
    # A real Sheet has thousands of rows; the Test Sheet never holds more than the fixture.
    existing_rows = len(SheetClient(service, target.sheet_id, target.data_tab).read_grid())
    if existing_rows > len(grid):
        raise ResetRefused(
            f"{quote_tab(target.data_tab)} has {existing_rows} rows, more than the {len(grid)}-row fixture; refusing to clear it"
        )

    values = service.spreadsheets().values()
    values.clear(spreadsheetId=target.sheet_id, range=quote_tab(target.data_tab), body={}).execute()
    values.update(
        spreadsheetId=target.sheet_id,
        range=f"{quote_tab(target.data_tab)}!A1",
        valueInputOption="RAW",
        body={"values": grid},
    ).execute()

    existing = tab_ids(service, target.sheet_id)
    deletions = [{"deleteSheet": {"sheetId": existing[tab]}} for tab in target.log_tabs if tab in existing]
    if deletions:
        service.spreadsheets().batchUpdate(spreadsheetId=target.sheet_id, body={"requests": deletions}).execute()


def set_cell(service: Any, target: E2ESheet, row_number: int, column_index: int, value: str) -> None:
    """`row_number` is the Sheet's 1-based row; `column_index` is 0-based, as in the header list."""
    cell = f"{quote_tab(target.data_tab)}!{column_letter(column_index)}{row_number}"
    service.spreadsheets().values().update(
        spreadsheetId=target.sheet_id, range=cell, valueInputOption="RAW", body={"values": [[value]]}
    ).execute()


def load_fixture_grid(path: Path) -> list[list[str]]:
    """`{"header": [...], "rows": [{column: value}]}` to a grid; unnamed columns are blank."""
    fixture = json.loads(path.read_text(encoding="utf-8"))
    header: list[str] = fixture["header"]
    grid = [list(header)]
    for row in fixture["rows"]:
        unknown = sorted(set(row) - set(header))
        if unknown:
            raise ValueError(f"fixture row names columns not in the header: {unknown}")
        grid.append([row.get(column, "") for column in header])
    return grid


def pad_grid(grid: list[list[str]], width: int) -> list[list[str]]:
    return [row + [""] * (width - len(row)) for row in grid]
