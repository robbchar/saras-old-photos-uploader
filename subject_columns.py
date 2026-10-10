"""Builds `subject` from the columns a project's registry lists.

The one place a column's header is not its IA field name: Internet Archive files an
item under a collection's topics by `subject`. See docs/decisions/FILES-AND-METADATA.md,
"`subject` is built from the columns the registry lists"."""
from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from column_map import RESERVED_FIELDS, ColumnMap
from ia_fields import IA_STANDARD_FIELDS, PIPELINE_OWNED_FIELDS

SUBJECT_FIELD = "subject"
SUBJECT_SEPARATOR = ";"


def split_subject_cell(text: str) -> list[str]:
    """One cell's values: split on ';', trimmed, empty pieces dropped. Commas never split."""
    return [piece for part in text.split(SUBJECT_SEPARATOR) if (piece := part.strip())]


def subject_values(row: Mapping[str, str], columns: Sequence[str]) -> list[str]:
    """Every listed cell's values in column order; an exact repeat keeps its first place."""
    values = (value for column in columns for value in split_subject_cell(row.get(column) or ""))
    return list(dict.fromkeys(values))


def subject_value(row: Mapping[str, str], columns: Sequence[str]) -> str | list[str]:
    """One value as text, as IA stores it; several as a list; none as a blank cell."""
    values = subject_values(row, columns)
    if len(values) == 1:
        return values[0]
    return values or ""


def check_subject_columns(
    columns: Sequence[str], column_map: ColumnMap, location_fields: Collection[str]
) -> list[str]:
    """Why these columns cannot feed `subject` from this Sheet, one line each.
    Sync removes every listed column from items, so only plain project columns qualify."""
    if not columns:
        return []
    known = set(column_map.field_names.values())
    held_back = {column_map.field_names[header] for header in column_map.held_back}
    problems: list[str] = []
    if SUBJECT_FIELD in known:
        problems.append(
            f"the Sheet has its own '{SUBJECT_FIELD}' column, and subject_columns builds "
            f"{SUBJECT_FIELD} from other columns - two sources for one field. Rename or "
            "remove that column"
        )
    for name in columns:
        reason = _unusable_reason(name, known, held_back, location_fields)
        if reason:
            problems.append(f"subject_columns names {name!r}, {reason}")
    return problems


def _unusable_reason(
    name: str, known: set[str], held_back: set[str], location_fields: Collection[str]
) -> str | None:
    if name not in known:
        return f"which is not a column in this Sheet. Known columns: {', '.join(sorted(known))}"
    if name in RESERVED_FIELDS or name in PIPELINE_OWNED_FIELDS:
        return "which this tool writes or generates itself - its cells never reach Internet Archive"
    if name in held_back:
        return "which is held back (LCPS Internal) - its cells never reach Internet Archive"
    if name in location_fields:
        return "which only locates each row's file - its cells never reach Internet Archive"
    if name in IA_STANDARD_FIELDS:
        return (
            f"which is Internet Archive's own '{name}' field - sync removes every listed "
            "column from items, so it would delete that field from every item"
        )
    return None
