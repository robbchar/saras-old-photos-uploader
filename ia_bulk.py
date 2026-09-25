"""Bulk validate/upload/sync-metadata CLI for Internet Archive, driven by a
project's Google Sheet (read live). See docs/ARCHITECTURE.md for the
identifier scheme this script assumes."""
from __future__ import annotations

import argparse
import functools
import io
import json
import os
import random
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Callable, Iterator, Protocol, Sequence, TypeVar

import googleapiclient.discovery
import internetarchive
import requests
from urllib3.util.retry import Retry
from googleapiclient.errors import HttpError

import deployment
import google_auth
import launch_agent
import log_tab
import platform_probe
import upload_lock
from column_map import (
    ColumnMap,
    FileResolutionError,
    IA_SYNC_HASH_COLUMN,
    TemplateError,
    candidate_path,
    check_column_map,
    check_file_template,
    check_grid_shape,
    grid_to_rows,
    resolve_file,
    template_fields,
)
from ia_fields import PIPELINE_OWNED_FIELDS, metadata_to_send, suggest_standard_fields
from identifiers import RowState, classify_row, next_identifiers, parse_identifier
from project_config import (
    ConfigError,
    ProjectConfig,
    is_placeholder_sheet_id,
    load_project_config,
)
from reconcile import AmbiguousMatch, Proposal, propose_match
from sheet_client import CellUpdate, SheetClient, column_letter
from sync_state import (
    MissingSyncColumns,
    SyncColumns,
    locate_sync_columns,
    stamp_updates,
    sync_hash,
)

# Shared by build_deployment_checks and cmd_setup - one computed root, not two.
REPO_ROOT = Path(__file__).resolve().parent

# Deliberately excludes "identifier", "file" and "title" - do not add any of
# them back.
#
# "identifier": on the Sheet path this is ordinary donor metadata (the
# real Sheet's own `Identifier` column holds an archival reference like
# "CD 1 01 53 58 1 Central SS"), not the tool's minted identifier - that
# lives in `ia_identifier`, which is never required here either: a blank
# ia_identifier is the normal starting state for every new row
# (RowState.UNASSIGNED), not an error. See docs/DECISIONS.md, "Tool-owned
# Sheet columns are all `ia_`-prefixed".
#
# "file" is left out entirely, not merely reclassified. By the time
# validate_rows sees a Sheet row, cmd_validate has already turned
# `file_template` plus the row's own columns into a candidate path and
# resolved it against `files_dir` (see resolve_file() in column_map.py),
# recording the outcome in a FileOutcomes: resolved (a real, disk-verified
# `file` value), blank (nobody named a file yet - a readiness fact), or
# broken (a name was given and didn't resolve - an error, via
# outcomes.errors). A required-columns check for `file` here would just
# be a second, cruder way of asking the same question FileOutcomes already
# answered precisely, producing a duplicate "missing required column
# 'file'" line behind the resolver's own, more actionable message. See
# docs/DECISIONS.md, "A file is found by resolution, not by constructing a
# path".
#
# "title" is also gone, but reclassified rather than dropped: it moved to
# the registry's required_for_upload (see ProjectConfig), because it is
# ordinary human-filled metadata - a blank title means "nobody has
# catalogued this row yet" (readiness), not "this row is broken"
# (validity). `required_columns` here is now reserved for what is
# structurally guaranteed to exist independent of any human filling
# anything in.
#
# NOT to be confused with validate_rows' check_file_exists / is_file()
# check below, a completely different mechanism that stays untouched: it
# is a disk-level safety net on the resolved `file` value, not a
# required-columns check, and removing it is a different (and wrong)
# change from the one this constant's shrink makes.
SHEET_REQUIRED_COLUMNS = ("mediatype",)
CHUNK_SIZE = 500
# Internet Archive's per-account daily item cap. CHUNK_SIZE covers the
# 500-items-per-run half of the limit in `.claude/CLAUDE.md` ("IA batch
# limits: 500 items per upload run, 5000/day"); this covers the other half,
# which nothing enforced. Applies in test mode too - a rehearsal uploads to
# test_collection through the same account and spends the same quota.
DAILY_ITEM_CAP = 5000
TEST_COLLECTION = "test_collection"
TEST_IDENTIFIER_PREFIX = "zztest-"
UNDATED_PLACEHOLDER = "[n.d.]"

# The four columns this tool writes. All `ia_`-prefixed so they cannot collide
# with a header a Sheet author already uses - the real LCPS Sheet's own
# `Identifier` column holds the donor's archival reference, and an unprefixed
# `identifier` column would have been overwritten by the first upload. See
# docs/DECISIONS.md, "Tool-owned Sheet columns are all `ia_`-prefixed".
IA_IDENTIFIER_COLUMN = "ia_identifier"
IA_UPLOADED_COLUMN = "ia_uploaded"
IA_URL_COLUMN = "ia_url"
IA_IDENTIFIER_BIB_COLUMN = "ia_identifier_bib"
WRITE_BACK_COLUMNS = (
    IA_IDENTIFIER_COLUMN,
    IA_UPLOADED_COLUMN,
    IA_URL_COLUMN,
    IA_IDENTIFIER_BIB_COLUMN,
)
ITEM_URL_PREFIX = "https://archive.org/details/"
# upload_row() strips these two keys from the metadata it sends, so a column
# normalizing to one of them never reaches Internet Archive whatever the
# receipt might otherwise imply. `file` is the local path, not metadata.
# `identifier` IS Internet Archive's own item identifier, so a Sheet column of
# that name - on the real Sheet, the donor's archival reference - cannot be
# uploaded under it. Named here so the receipt can say so out loud instead of
# listing a field that silently never ships.
DROPPED_BY_UPLOAD_ROW = frozenset({"identifier", "file"})

_ChunkItem = TypeVar("_ChunkItem")


def chunk_rows(
    rows: list[_ChunkItem], chunk_size: int = CHUNK_SIZE
) -> "Iterator[list[_ChunkItem]]":
    for start in range(0, len(rows), chunk_size):
        yield rows[start : start + chunk_size]


DEFAULT_REGISTRY = "projects_registry.json"


def load_registry(registry_path: str | Path) -> dict:
    with open(registry_path, encoding="utf-8") as f:
        return json.load(f)


def check_identifier(
    identifier: str,
    row_number: int,
    registry: dict,
    project_id: str,
    seen_identifiers: dict[str, int],
    column_name: str,
) -> list[str]:
    """project_id is the run's own --project, and it has no default on
    purpose. This function used to ask only whether a prefix belonged to
    SOME project in the registry, which accepted `lcps-otherproject-00099`
    on a `--project astoriaphotos` run (issue #2) - an item filed under
    another project's numbering, quietly, under a name that can never be
    renamed. A default here would let the next call site re-introduce
    exactly that, so every caller has to say which project it means.

    column_name names the column being checked in every message, and has
    no default either. The Sheet path passes "ia_identifier" - on a Sheet
    that has BOTH its own `Identifier` column
    (donor metadata, untouched by this tool) and `ia_identifier` (the
    tool's minted one), a message that just says "identifier" leaves a
    volunteer unable to tell which column to go fix. Naming the actual
    column is exactly what Part A's `ia_` prefix exists to make possible."""
    identifier = identifier.strip()
    if not identifier:
        return [f"missing required column '{column_name}'"]

    errors: list[str] = []
    # identifiers.parse_identifier is the single decoder for the scheme - see
    # its docstring for why this file no longer carries a second copy of the
    # pattern.
    parsed = parse_identifier(identifier)
    if parsed is None:
        errors.append(
            f"{column_name} '{identifier}' does not match scheme COLLECTIONKEY-PROJECTID-NUMBER"
        )
    else:
        collection_key, identifier_project, _number = parsed
        collection_matches = collection_key == registry.get("collection_key")
        project_registered = identifier_project in registry.get("projects", {})
        if not (collection_matches and project_registered):
            errors.append(
                f"{column_name} prefix '{collection_key}-{identifier_project}' not found in "
                "project registry"
            )
        elif identifier_project != project_id:
            # Kept distinct from the "not found" message above because the
            # two are different mistakes with different fixes: an
            # unregistered prefix means the identifier is wrong, while a
            # registered-but-other prefix means --project may be the thing
            # that is wrong. Both are named so the operator can tell which.
            errors.append(
                f"{column_name} '{identifier}' belongs to project "
                f"'{identifier_project}', but this run is --project {project_id}"
            )

    if identifier in seen_identifiers:
        errors.append(f"{column_name} '{identifier}' duplicates row {seen_identifiers[identifier]}")
    else:
        seen_identifiers[identifier] = row_number

    return errors


class Readiness(Enum):
    """Whether a human has filled in the fields a row needs before it can be
    uploaded. Deliberately NOT a member of RowState: RowState answers "has
    this been minted and uploaded", readiness answers "has a person filled
    it in", and a not-ready row IS RowState.UNASSIGNED. They are different
    questions, not alternatives - see docs/DECISIONS.md."""

    READY = "ready"
    NOT_READY = "not_ready"


class UploadVerdict(Enum):
    """Validity crossed with readiness; NOT_READY beats INVALID (see
    format_lifecycle_summary). Only READY rows are uploadable, and
    classify_row() still decides which of those `upload` targets (not DONE)."""

    READY = "ready"
    INVALID = "invalid"
    NOT_READY = "not_ready"


@dataclass
class RowValidation:
    row_number: int
    identifier: str
    errors: list[str] = field(default_factory=list)
    # Two sources, in a fixed order: the blank required_for_upload columns
    # validate_rows finds first, then the blank file_template cells
    # validate_sheet_grid extends on afterward (see FileOutcomes.blank).
    # Order matters to callers that group or count by name (Tasks 7-8), not
    # just to this list's own contents.
    missing_fields: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors

    @property
    def readiness(self) -> Readiness:
        """Derived, never stored: `missing_fields` being non-empty IS what
        not-ready means, so a second stored field could only drift from it."""
        return Readiness.NOT_READY if self.missing_fields else Readiness.READY

    @property
    def verdict(self) -> UploadVerdict:
        if self.readiness is Readiness.NOT_READY:
            return UploadVerdict.NOT_READY
        return UploadVerdict.READY if self.is_valid else UploadVerdict.INVALID


def validate_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    project_id: str,
    *,
    required_columns: tuple[str, ...],
    identifier_column: str,
    check_file_exists: bool = True,
    required_for_upload: tuple[str, ...] = (),
) -> list[RowValidation]:
    """project_id is the run's own --project. It is threaded through to
    check_identifier and used nowhere else here - see that function for why
    it is required rather than defaulted (issue #2).

    required_columns and identifier_column have no defaults: the old ones
    described the retired CSV schema, and on the Sheet they require the
    donor's `identifier` column. The Sheet path passes SHEET_REQUIRED_COLUMNS
    (see that constant's comment) and IA_IDENTIFIER_COLUMN.

    check_file_exists defaults to True, and the Sheet path passes True (see
    SHEET_REQUIRED_COLUMNS' comment): by the time this runs, cmd_validate
    has already resolved each row's 'file' against disk via resolve_file(),
    so this check is a redundant safety net there rather than the primary
    signal, which is fine - it costs one cheap is_file() stat per row.

    identifier_column is "ia_identifier" on the Sheet path - after Task 9
    the Sheet's own 'identifier' column holds the donor's original archival
    reference (e.g. "CD 1 01 53 58 1 Central SS"), not a minted IA
    identifier, and running check_identifier's COLLECTIONKEY-PROJECTID-
    NUMBER regex against a donor reference fails every row for the wrong
    reason - which is exactly what happened against the real Sheet before
    this fix. See docs/DECISIONS.md, "Tool-owned Sheet columns are all
    `ia_`-prefixed".

    required_for_upload defaults to (). The Sheet path passes
    config.required_for_upload - a blank one of these is a READINESS fact
    (nobody has catalogued this row yet), not a validation error, which is
    the whole reason it is recorded on missing_fields rather than folded
    into `errors` alongside required_columns."""
    seen_identifiers: dict[str, int] = {}
    results: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1
        identifier = (row.get(identifier_column) or "").strip()

        errors: list[str] = []

        for column in required_columns:
            if not (row.get(column) or "").strip():
                errors.append(f"missing required column '{column}'")

        if identifier:
            errors.extend(
                check_identifier(
                    identifier,
                    row_number,
                    registry,
                    project_id,
                    seen_identifiers,
                    identifier_column,
                )
            )

        if check_file_exists:
            file_value = (row.get("file") or "").strip()
            if file_value:
                file_path = Path(files_dir) / file_value
                if not file_path.is_file():
                    errors.append(f"file not found: {file_path}")

        missing_fields = [
            column for column in required_for_upload if not (row.get(column) or "").strip()
        ]

        results.append(
            RowValidation(
                row_number=row_number,
                identifier=identifier,
                errors=errors,
                missing_fields=missing_fields,
            )
        )

    return results


def validate_sheet_rows(
    rows: list[dict[str, str]],
    files_dir: str | Path,
    registry: dict,
    project_id: str,
    required_for_upload: tuple[str, ...] = (),
) -> list[RowValidation]:
    """The Sheet path's answer, named. Its three choices used to travel as
    loose parameters at every call site:

    - the tool's minted identifier lives in `ia_identifier`, never
      `identifier` (which on the real Sheet is the donor's own archival
      reference and would fail the COLLECTIONKEY-PROJECTID-NUMBER regex on
      every row);
    - `ia_identifier` is not required, because blank is the normal starting
      state of a new row - RowState.UNASSIGNED, not an error;
    - a blank required_for_upload column (title, say) is a readiness fact
      recorded on missing_fields, not a validation error - see
      SHEET_REQUIRED_COLUMNS' comment for why that split exists."""
    return validate_rows(
        rows,
        files_dir,
        registry,
        project_id,
        required_columns=SHEET_REQUIRED_COLUMNS,
        check_file_exists=True,
        identifier_column=IA_IDENTIFIER_COLUMN,
        required_for_upload=required_for_upload,
    )


_GRID_SHAPE_ROW_NUMBER_RE = re.compile(r"^row (\d+) ")


def sheet_structure_validation(column_map: ColumnMap, grid: list[list[str]]) -> list[RowValidation]:
    """check_column_map catches two headers that normalize to the same IA
    field name - which would silently overwrite one column's data across
    every row - and headers that normalize to an empty field name. Those are
    genuinely header-level problems, so they're filed under row 1.

    check_grid_shape catches a data row longer than the header, whose excess
    cells otherwise vanish without a trace - but that is a problem with a
    SPECIFIC data row, not with the header. check_grid_shape's own message
    already names the real row number (e.g. "row 3 has 1 more field(s)...");
    filing it under row 1 anyway - as an earlier version of this function
    did - puts a row-3 problem under the heading a volunteer reads as "the
    header row", which is actively confusing. So each shape-error message is
    parsed for the row number it already names and filed there instead,
    producing one RowValidation per affected row.

    check_grid_shape's message format is a private contract between these
    two functions, not a public interface - if that wording ever changes
    such that the leading "row N " prefix disappears, _GRID_SHAPE_ROW_NUMBER_RE
    simply fails to match and the message is filed under row 1 as a safe
    fallback rather than raising."""
    results: list[RowValidation] = []

    header_errors = check_column_map(column_map)
    if header_errors:
        results.append(RowValidation(row_number=1, identifier="", errors=header_errors))

    for message in check_grid_shape(grid):
        match = _GRID_SHAPE_ROW_NUMBER_RE.match(message)
        row_number = int(match.group(1)) if match else 1
        results.append(RowValidation(row_number=row_number, identifier="", errors=[message]))

    return results


def format_field_receipt(column_map: ColumnMap) -> str:
    """Printed before anything permanent happens, so the transformation from
    Sheet header to IA field name is reviewable by a human.

    The "not uploaded" sections are not decoration. A Sheet column named
    `Identifier` (the real one has one, holding the donor's archival reference)
    normalizes to `identifier`, which upload_row strips because that name is
    Internet Archive's own item identifier. The receipt used to list it among
    the fields that would upload, which was simply untrue - and a receipt an
    operator learns to disbelieve is worse than no receipt.

    There are two different reasons a column does not ship, and collapsing
    them into one list would recreate that same untruth in a quieter form:

    - `identifier` and `file` are dropped outright (DROPPED_BY_UPLOAD_ROW):
      one is Internet Archive's own item identifier, the other a local path.
    - `mediatype` and `collection` ARE sent, but with a value this tool
      generates - upload_from_sheet overwrites row['mediatype'] from the
      registry and upload_row sets metadata['collection'] unconditionally, so
      a Sheet column of either name has its own value silently discarded.
      ia_fields.PIPELINE_OWNED_FIELDS is the existing definition of that set,
      reused here rather than restated, so the receipt and the
      rename-suggestion logic cannot disagree about which names are the
      tool's."""
    all_fields = column_map.uploadable_fields()
    fields = [
        name
        for name in all_fields
        if name not in DROPPED_BY_UPLOAD_ROW and name not in PIPELINE_OWNED_FIELDS
    ]
    dropped = [name for name in all_fields if name in DROPPED_BY_UPLOAD_ROW]
    # `identifier` is in both sets; it is listed under "reserves these names"
    # only, which is the more precise reason of the two.
    generated = [
        name
        for name in all_fields
        if name in PIPELINE_OWNED_FIELDS and name not in DROPPED_BY_UPLOAD_ROW
    ]

    lines = ["will upload these metadata fields:"]
    lines.append("  " + ", ".join(fields) if fields else "  (none)")
    if dropped:
        lines.append("NOT uploaded - Internet Archive reserves these names:")
        lines.append(f"  {', '.join(dropped)}")
    if generated:
        lines.append(
            "uploaded with a value this tool generates - the column's own value is IGNORED:"
        )
        lines.append(f"  {', '.join(generated)}")
    if column_map.held_back:
        lines.append("held back (LCPS Internal):")
        lines.append("  " + ", ".join(column_map.held_back))
    return "\n".join(lines)


CONSOLE_ERROR_WIDTH = 300


def _elide(text: str, width: int) -> str:
    if len(text) > width:
        # ASCII, deliberately: a console codepage without U+2026 would print it as an escape.
        return text[:width - 3] + "..."
    return text


def format_row_error(exc: Exception) -> str:
    """A failing row's error, as one line to print underneath it.

    "1 error(s)" plus a path to a JSONL file was the entire console output
    for a failed row. This tool is meant to be run by volunteers who are
    comfortable with spreadsheets and not with code, and the information
    already existed - the log's `error` field held the full message, it just
    never reached the screen.

    Whitespace is collapsed because Internet Archive's S3 failures carry a
    multi-line XML body; dumped raw under a progress line it swamps the
    [N/M] rhythm the operator is reading. Truncated for the same reason. The
    log keeps the complete text, which is what the log is for.
    """
    return _elide(" ".join(str(exc).split()), CONSOLE_ERROR_WIDTH)


def _pluralize(count: int, noun: str) -> str:
    """Headline counts reach ~3,000 on the real Sheet, so thousands are
    separated. Any raw count printed beside a _pluralize line must use the
    same {:,} format, or adjacent lines disagree about how a number looks."""
    return f"{count:,} {noun}" if count == 1 else f"{count:,} {noun}s"


def format_lifecycle_summary(rows: list[dict[str, str]], row_results: list[RowValidation]) -> str:
    """row_results must be validate_rows()'s own output for these exact
    rows, in the same order (one result per row) - NOT the combined report
    that also includes sheet_structure_validation()'s row-1/shape entries,
    which are not aligned with `rows` at all. Passing a mismatched list
    would make zip() silently truncate to the shorter one rather than
    raising, so a caller-side wiring mistake would produce plausible-looking
    but wrong counts instead of an obvious failure - which is exactly the
    kind of silent wrongness this function exists to prevent, so the length
    is checked explicitly instead.

    Counts are cross-referenced against row_results rather than
    classify_row() alone, for EVERY bucket, not just "ready": a row that
    classify_row() would call DONE or RESERVED but that actually fails
    validation (a duplicate identifier, an unregistered project prefix, a
    now-missing title) is not "already uploaded" or "will retry" just
    because it has the shape of one - RESERVED in particular makes a
    forward-looking promise ("will retry under existing identifier") that a
    row failing identifier validation cannot keep.

    Validity itself splits three ways, not two: a row is either READY
    (catalogued and passes validation), invalid (catalogued but fails
    validation), or NOT_READY (missing one or more required_for_upload/
    file_template fields - see RowValidation.readiness). Crossed with
    classify_row()'s three states that makes nine buckets, not six. A row
    that is BOTH not-ready and carrying validation errors (e.g. an
    uncatalogued row whose filename is also a typo) is counted ONCE, under
    not-ready: not-ready takes precedence over invalid, because "nobody has
    filled this in yet" is the more useful thing to tell an operator than a
    validation error that will most likely resolve itself the moment the
    row is catalogued. Every row falls into exactly one of UNASSIGNED/DONE/
    RESERVED and then exactly one of ready/invalid/not_ready, so the nine
    counts below always sum to len(rows).

    Only non-zero buckets render, except the three per-state headline lines
    ("ready to upload"/"already uploaded"/"reserved but unconfirmed"), which
    always print - even as "0" - so the three lifecycle states themselves
    are never silently absent from the report. When both a state's
    not_ready and invalid counts are non-zero, not_ready prints first: it is
    normally the far larger of the two (an unfilled-in row rather than a
    broken one) and is not an error, so it reads before the more alarming
    invalid line rather than after it."""
    if len(rows) != len(row_results):
        raise ValueError(
            f"format_lifecycle_summary: got {len(rows)} row(s) but {len(row_results)} "
            "row_results - they must be the same length, in the same order. Pass "
            "validate_rows()'s own return value here, not the combined report (which "
            "also carries sheet_structure_validation()'s row-1/shape entries)."
        )

    counts: dict[tuple[RowState, UploadVerdict], int] = {
        (state, bucket): 0
        for state in (RowState.UNASSIGNED, RowState.DONE, RowState.RESERVED)
        for bucket in UploadVerdict
    }

    # The results themselves, not merely a tally: each not-ready line renders
    # its own missing-field detail from its own rows (see
    # format_missing_field_lines), so the bucket has to keep them.
    buckets: dict[tuple[RowState, UploadVerdict], list[RowValidation]] = {key: [] for key in counts}
    for row, result in zip(rows, row_results):
        state = classify_row(row)
        bucket = result.verdict
        buckets[(state, bucket)].append(result)
        counts[(state, bucket)] += 1

    lines = [
        f"{_pluralize(counts[(RowState.UNASSIGNED, UploadVerdict.READY)], 'row')} ready to upload "
        "(no identifier yet)"
    ]
    if counts[(RowState.UNASSIGNED, UploadVerdict.NOT_READY)]:
        lines.append(
            f"{_pluralize(counts[(RowState.UNASSIGNED, UploadVerdict.NOT_READY)], 'row')} not yet "
            "assigned an identifier and not yet catalogued (missing required fields) - "
            "waiting on data entry, not blocked by an error"
        )
        lines.extend(
            format_missing_field_lines(buckets[(RowState.UNASSIGNED, UploadVerdict.NOT_READY)])
        )
    if counts[(RowState.UNASSIGNED, UploadVerdict.INVALID)]:
        lines.append(
            f"{_pluralize(counts[(RowState.UNASSIGNED, UploadVerdict.INVALID)], 'row')} not yet "
            "assigned an identifier but failed validation - see the errors above; will "
            "not be uploaded until fixed"
        )

    lines.append(f"{counts[(RowState.DONE, UploadVerdict.READY)]:,} already uploaded")
    if counts[(RowState.DONE, UploadVerdict.NOT_READY)]:
        lines.append(
            f"{_pluralize(counts[(RowState.DONE, UploadVerdict.NOT_READY)], 'row')} already "
            "uploaded but missing required fields - a required column was cleared after upload; "
            "needs a human to look, not an automatic retry"
        )
        lines.extend(
            format_missing_field_lines(buckets[(RowState.DONE, UploadVerdict.NOT_READY)])
        )
    if counts[(RowState.DONE, UploadVerdict.INVALID)]:
        lines.append(
            f"{_pluralize(counts[(RowState.DONE, UploadVerdict.INVALID)], 'row')} already "
            "uploaded but now fail validation - see the errors above; this needs a human to "
            "look, not an automatic retry"
        )

    lines.append(
        f"{counts[(RowState.RESERVED, UploadVerdict.READY)]:,} reserved but unconfirmed - "
        "will retry under existing identifier"
    )
    if counts[(RowState.RESERVED, UploadVerdict.NOT_READY)]:
        lines.append(
            f"{_pluralize(counts[(RowState.RESERVED, UploadVerdict.NOT_READY)], 'row')} reserved "
            "but not yet catalogued (missing required fields) - waiting on data entry before "
            "it can retry"
        )
        lines.extend(
            format_missing_field_lines(buckets[(RowState.RESERVED, UploadVerdict.NOT_READY)])
        )
    if counts[(RowState.RESERVED, UploadVerdict.INVALID)]:
        lines.append(
            f"{_pluralize(counts[(RowState.RESERVED, UploadVerdict.INVALID)], 'row')} reserved but "
            "invalid - see the errors above; will NOT retry automatically until fixed"
        )

    return "\n".join(lines)


def _format_result_lines(results: list[RowValidation]) -> list[str]:
    """The [STATUS]/error-line half of a report, without the trailing
    "N/M rows passed" count - factored out so a caller that needs to show
    structural errors WITHOUT a misleading pass/fail count next to them
    (see cmd_validate's no-data-rows branch: "0/1 rows passed" reads as
    nonsense when the "1" is a synthetic entry standing in for zero real
    rows) can reuse the exact same formatting `format_report` uses."""
    lines: list[str] = []
    for result in results:
        status = "PASS" if result.is_valid else "FAIL"
        # A blank identifier is the normal state of an unassigned Sheet row
        # (see RowState.UNASSIGNED), not a special case worth restating the
        # row number for - "[PASS] row 2 (row 2)" said nothing "[PASS] row 2"
        # didn't already say.
        label = f" {result.identifier}" if result.identifier else ""
        # A not-ready row (missing_fields non-empty) gets a marker appended
        # after the label, distinct from the [PASS]/[FAIL] status: readiness
        # and validity are different questions (see Readiness's docstring),
        # so a row can be "[FAIL] ... (not yet catalogued)" - broken AND
        # uncatalogued - without the marker implying the errors below it are
        # what "not yet catalogued" means.
        marker = "  (not yet catalogued)" if result.missing_fields else ""
        lines.append(f"[{status}] row {result.row_number}{label}{marker}")
        for error in result.errors:
            lines.append(f"    - {error}")
    return lines


# How many separate row ranges a listing prints before it summarizes the rest.
# Compression already handles the shape this Sheet actually has - the
# uncatalogued backlog is one long contiguous block of appended skeleton rows -
# so this cap only bites on the pathological case: hundreds of scattered single
# rows, where no two are adjacent and nothing can be collapsed.
MAX_LISTED_ROW_RANGES = 8


def format_row_numbers(numbers, max_ranges: int = MAX_LISTED_ROW_RANGES) -> str:
    """"row 189", or "rows 12-14, 40, 42-43" - the rows behind a count.

    Ranges rather than a capped list of numbers. On the real Sheet a
    per-field count runs to thousands, and those rows are overwhelmingly one
    contiguous block; "rows 190-3036" is both shorter than ten numbers and a
    truncation, and tells the operator strictly more. A flat list would have
    to be cut off long before it said anything useful.

    Input is sorted and de-duplicated here rather than at the call sites:
    callers collect row numbers while walking results in whatever order those
    come in, and missing_fields' two sources can name the same row twice."""
    ordered = sorted(set(numbers))
    if not ordered:
        return ""

    ranges: list[tuple[int, int]] = []
    for number in ordered:
        if ranges and number == ranges[-1][1] + 1:
            ranges[-1] = (ranges[-1][0], number)
        else:
            ranges.append((number, number))

    shown = ranges[:max_ranges]
    # No thousands separators on the numbers themselves, unlike every count
    # in this report: a row number is something the operator types into
    # Sheets' own go-to-row box, which shows 3036, not 3,036 - and a comma
    # inside a range collides with the comma separating the ranges
    # ("rows 12-3,036, 4,001").
    listing = ", ".join(
        f"{start}" if start == end else f"{start}-{end}" for start, end in shown
    )
    dropped = len(ranges) - len(shown)
    if dropped:
        # "ranges", not "rows": the number of rows behind them is not what was
        # dropped, and saying "rows" would read as a row count that disagrees
        # with the count this listing is attached to.
        listing += f", and {dropped:,} more range{'' if dropped == 1 else 's'}"

    label = "row" if len(ordered) == 1 else "rows"
    return f"{label} {listing}"


def format_missing_field_lines(
    row_results: list[RowValidation], indent: str = "    "
) -> list[str]:
    """The per-field detail under one not-ready count: which field is missing,
    from how many rows, and which rows.

    Takes a SUBSET of a run's results - the not-ready rows of one lifecycle
    state - and is called once per such state by format_lifecycle_summary,
    rather than once for the whole report. It used to render a standalone
    block below the summary, headed by its own "N rows not yet catalogued"
    total. That header was the sum across the three lifecycle states that can
    hold a not-ready row, but on the ordinary Sheet only one of them is
    non-zero, so it read as a verbatim repeat of the line just above it. See
    docs/decisions/READINESS.md, "The missing-field detail belongs to the
    count above it".

    Splitting per state is not merely tidier: an uncatalogued row and a row
    whose title was cleared AFTER it uploaded need different work, and one
    merged "2 missing title" would hide that.

    This is also the measurement that sizes a planned follow-up tool - a
    script that fills filenames in from disk. If most not-ready rows are
    missing only a filename, that script closes most of the gap; if most are
    missing a title, it barely helps. Since the split, that reading is per
    state rather than one global total, which in practice is the same number:
    the other two states are almost always empty.

    The field names come from whatever is actually in each result's
    missing_fields, not a hardcoded list - so this stays correct when a
    project's required_for_upload changes, without this function needing to
    change alongside it.

    The counts OVERLAP - a row missing two fields appears in both of their
    counts - so the closing parenthetical noting that is load-bearing, not
    decoration: adjacent numbers are read as a partition (as if they summed
    to the total above them) unless something says otherwise, and here they
    don't sum to it. It names this block's own total, never the run's."""
    not_ready = [result for result in row_results if result.missing_fields]
    if not not_ready:
        return []

    # Rows per field, not merely a count per field: a not-ready row prints as
    # [PASS] in the report above (a blank cell is not an error), so it is
    # indistinguishable at a glance from the thousands of rows that are simply
    # fine. This is the only place that bucket can be picked out at all.
    rows_by_field: dict[str, list[int]] = {}
    for result in not_ready:
        for name in result.missing_fields:
            rows_by_field.setdefault(name, []).append(result.row_number)

    lines = [
        f"{indent}{len(numbers):,} missing {name}: {format_row_numbers(numbers)}"
        for name, numbers in sorted(
            rows_by_field.items(), key=lambda item: (-len(item[1]), item[0])
        )
    ]
    if any(len(result.missing_fields) > 1 for result in not_ready):
        lines.append(
            f"{indent}(a row missing more than one field appears in more than one count "
            f"above, so these do not sum to {len(not_ready):,})"
        )
    return lines


def format_report(results: list[RowValidation]) -> str:
    lines = _format_result_lines(results)
    passed = sum(1 for r in results if r.is_valid)
    lines.append("")
    lines.append(f"{passed}/{len(results)} rows passed")
    return "\n".join(lines)


def utc_timestamp() -> str:
    """Every timestamp this tool records, in ISO-8601 UTC with an explicit Z.

    UTC for the same reason run_stamp() uses it, applied to the values that
    outlive the run: local time repeats an hour during the DST fall-back
    transition, so a run spanning it stamps a later chunk with an earlier
    wall-clock time than an earlier one. `ia_uploaded` is the permanent record
    of when an archival item was published, and the log is what any later
    audit reads - both were naive local time, with no offset to
    reconstruct the real instant from afterwards.

    The trailing Z is not decoration: without it the string is ambiguous, and
    the ambiguity is only discoverable by knowing which machine wrote it and
    what its clock was set to that day."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def open_log(log_dir: str | Path, command_name: str) -> Path:
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    # UTC here too, so a directory listing sorts in the order the runs
    # actually happened - see utc_timestamp().
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return log_dir / f"{command_name}-{timestamp}.jsonl"


def log_run_header(
    log_path: str | Path,
    config: ProjectConfig,
    column_map: ColumnMap,
    live: bool,
    dry_run: bool,
    limit: int | None = None,
    chunk_size: int = CHUNK_SIZE,
    batch: str | None = None,
) -> None:
    """The first line written to a Sheet-path run's log. `head -1 <log>` then
    answers "what did this run send, under what field names, and what did it
    require before a row could go out" - the exact question that gets asked
    months later, once an identifier is already permanent and the Sheet has
    moved on and no longer shows what it looked like at upload time.

    `columns` and `held_back` come straight from the ColumnMap: `columns`
    maps EVERY header the Sheet had that run (not only the uploaded ones) to
    its normalized field name, and `held_back` names which of those were
    excluded as `(LCPS Internal)` - a receipt has to show what was left out,
    not only what went through. `required_for_upload` is the project's
    readiness rule at the time of the run: which normalized columns had to be
    non-blank for a row to be in scope at all. All three can change between
    runs (the Sheet gaining a column, a registry edit) even though none of
    them changes per row within one run, which is why this is written once
    per log rather than being documented once somewhere else.

    `limit` and `chunk_size` (Task 12) round out the same reconstructability
    goal: a run that stopped after --limit rows, or that used a non-default
    --chunk-size, cannot be explained later by the rest of this record alone.
    Both default to "the run's own default" (None / CHUNK_SIZE) so the three
    tests that call this directly without passing them still get a header
    that says so explicitly, rather than omitting the fields.

    `batch` is there for the same reason and is the strongest case of the
    three: a scoped run uploads a fraction of the ready rows and looks, in
    every other field of this record, exactly like a run that found little to
    do. `batch_column` is written beside it even on an unscoped run, since
    the value alone means nothing without the column it was matched against -
    and that column can change in the registry between runs.

    `collection` and `sheet_id` both name what the run actually used, not
    what the registry configures - a test run targets TEST_COLLECTION and the
    test Sheet, and a receipt that named the real ones would describe a run
    that never happened.

    Deliberately excludes anything that isn't safe to keep around in a log
    file indefinitely: no credentials, no tokens, no filesystem paths outside
    the project. `sheet_id` is the one Google identifier here, and it already
    appears in this command's ordinary console output."""
    entry = {
        "record": "run_header",
        "timestamp": utc_timestamp(),
        "project": config.project_id,
        "live": live,
        "dry_run": dry_run,
        "sheet_id": config.sheet_id_for(live),
        # The collection this run actually targeted, not the one configured
        # for it: in test mode every item goes to TEST_COLLECTION, and a
        # header naming the real, permanent collection for a run whose items
        # went somewhere else states the wrong thing about where they are. It
        # was inferable from `live` beside it - but only by a reader who
        # already knows that rule, and this record exists precisely so a
        # reader months later does not have to. Branches on `live` for the
        # same reason sheet_id_for() does, right above.
        "collection": config.ia_collection if live else TEST_COLLECTION,
        "files_dir": config.files_dir,
        "file_template": config.file_template,
        "columns": dict(column_map.field_names),
        "held_back": list(column_map.held_back),
        "required_for_upload": list(config.required_for_upload),
        "limit": limit,
        "chunk_size": chunk_size,
        "batch": batch,
        "batch_column": config.batch_column,
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def log_result(
    log_path: str | Path,
    identifier: str,
    file_value: str,
    status: str,
    live: bool,
    error: str | None = None,
    uploaded_as: str | None = None,
    http_status: int | None = None,
) -> None:
    entry = {
        "identifier": identifier,
        "file": file_value,
        "status": status,
        "error": error,
        # From parsed_status_code(), never the message; None when IA sent none.
        "http_status": http_status,
        "uploaded_as": uploaded_as,
        "live": live,
        "timestamp": utc_timestamp(),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def run_stamp() -> str:
    """A lowercase, IA-identifier-safe stamp unique to this invocation of the
    script - e.g. "20260819t144907". Computed ONCE per run and threaded
    through every effective_identifier() call that run makes, never
    recomputed per row: a run's test items must group together under one
    stamp, not scatter across however many rows it processed. (A re-run is
    a separate invocation with its own stamp, so its items land under a
    second stamp - correct, not a bug: done-ness is tracked by the Sheet's
    `ia_uploaded` column, never by the stamped identifier.)

    Uses UTC (time.gmtime), not local time: local time repeats an hour's
    worth of timestamps during the DST fall-back transition, which would
    make two rehearsals started an hour apart during that transition mint
    the same stamp - defeating the reason this function exists.

    See docs/DECISIONS.md, "Test identifiers carry a per-run stamp" - without
    this, a test run's identifiers were a pure function of the real ones, so a
    fresh Sheet (which always mints from 00001) reproduced the exact same test
    identifiers every time. Internet Archive never releases an identifier and
    test_collection darkens items after ~30 days, so every rehearsal after the
    first collided with a darkened item and failed outright."""
    return time.strftime("%Y%m%dt%H%M%S", time.gmtime())


def effective_identifier(identifier: str, live: bool, stamp: str) -> str:
    """`stamp` is required, not defaulted - a default would leave the
    collision described in run_stamp()'s docstring reachable again, and every
    caller of this function lives in this same file.

    Live is untouched: a live identifier is the permanent, public address of
    an archival item and must stay a pure function of the Sheet, so the
    live branch deliberately never looks at `stamp`. A stamp reaching a live
    identifier would be the worst outcome this function could produce."""
    if live:
        return identifier
    return f"{TEST_IDENTIFIER_PREFIX}{stamp}-{identifier}"


# is_rate_limit_error() looks ONLY at parsed status-code INTEGERS, never at
# str(exc) or any server-supplied text. An earlier version of this function
# scanned str(exc) for "status 429"/"status 503" substrings; that is unsafe
# in both directions and was fixed after review found the gap:
#
# - a 404 (or anything else) whose body happens to mention "status 503" -
#   a mirrored error, a proxied message, an echoed request - would
#   misclassify as a rate limit and wrongly stop the whole run.
# - a plain substring test also matches "status 5031" or "status 42900":
#   digits that merely CONTAIN 503/429 as a substring, not equal to them.
#
# A false positive here is worse than a miss: it halts a batch mid-flight,
# on a command that creates permanent items, for a reason that is not real.
#
# Two structured sources are checked instead, both verified by reading
# source rather than guessed:
#
# 1. UploadFailed.status_code - set by upload_row() below, in this file,
#    from the real, parsed `response.status_code` whenever
#    internetarchive.upload() returns a not-ok Response. See UploadFailed's
#    own docstring.
#
# 2. exc.response.status_code - requests.exceptions.RequestException (the
#    base of HTTPError) stores whatever Response object it is given as
#    `.response` in its own __init__ (`self.response = kwargs.pop
#    ("response", None)` - verified by reading requests' source directly,
#    not assumed). Tracing the pinned internetarchive's (traced on 5.10.1,
#    re-checked on 5.11.1) Item.upload_file() - the method upload_row() actually reaches via
#    internetarchive.upload() -> Item.upload() - shows that on a real S3
#    failure it catches the resulting HTTPError and re-raises via
#    `raise type(exc)(error_msg, response=exc.response, request=exc.request)`.
#    The MESSAGE there is rebuilt from the S3 XML body's <Message>/
#    <Resource> text (see get_s3_xml_text() in internetarchive/utils.py) and
#    loses the numeric status and the S3 <Code> (e.g. "SlowDown") entirely -
#    but `response=exc.response` is passed through UNCHANGED, so
#    `.response.status_code` still holds the real, original status even
#    though the text does not. This is what lets the check below catch a
#    live rate limit surfacing through the real library's own exception,
#    not only upload_row()'s own not-ok-Response branch.
#
# 503 is Internet Archive's documented S3 overload signal (the pinned
# internetarchive's, traced on 5.10.1 and re-checked on 5.11.1, own `ia upload --retries` help text: "Number of
# times to retry request if S3 returns a 503 SlowDown error"). 429 is not
# IA-upload-specific documentation, but session.py's default urllib3 Retry
# status_forcelist ([429, 500, 501, 502, 503, 504]) shows the library's own
# authors also treat it as rate-limit-adjacent.
#
# No --live run has ever happened, so no real rate-limit response has ever
# been captured - both sources above are verified against the installed
# library's SOURCE, not against actual IA behavior. Neither reachable
# exception in this codebase's own upload path lacks a structured status
# (see UploadFailed and the HTTPError tracing above), so there is no
# text-based fallback: the rate-limit decision never depends on
# server-supplied text, only on a parsed integer. A miss (an exception with
# neither attribute, or a genuinely different status) just logs one more
# ordinary failure and the run continues - --limit remains the
# operator-controlled backstop either way. See docs/DECISIONS.md,
# "Rate-limit detection uses a parsed status code, never message text".
#
# That "neither reachable exception" claim had one hole until 2026-09-02: the
# metadata GET inside internetarchive.upload(), whose status the library
# stripped before it ever reached here. Closed structurally - see IA_RETRY
# just below and the __context__ walk in parsed_status_code() - rather than by
# relaxing the no-message-text rule.
RATE_LIMIT_STATUS_CODES = (429, 503)

# internetarchive builds its own retrying HTTP adapter for archive.org, with
# urllib3's default `raise_on_status=True`. That default is why a rate limit
# on the metadata endpoint used to be invisible here. Verified against a local
# server answering real status codes, not reasoned about:
#
#   raise_on_status=True  (library default) - 429/500/503 exhaust urllib3's
#     three attempts and surface as requests.exceptions.RetryError. No
#     Response object is ever produced, so there is no status to read
#     anywhere, in the exception or its chain.
#   raise_on_status=False (this policy)     - the same three attempts happen,
#     but the final Response is RETURNED rather than raised through. It then
#     meets get_metadata()'s own resp.raise_for_status(), which produces a
#     normal HTTPError carrying that Response - and with it the real status.
#
# The retrying itself is unchanged: same total, same forcelist, same backoff.
# Only how the give-up is reported changes. Everything else is copied from
# the pinned internetarchive's session.mount_http_adapter() (traced on 5.10.1,
# re-checked on 5.11.1) so that replacing the
# library's policy does not silently alter what it retries or how often - and
# a test pins that equality against a session the library builds itself, so
# a future version changing its defaults fails loudly rather than quietly.
# How long a server-supplied Retry-After may hold a single call.
#
# urllib3 honours Retry-After by sleeping the requested duration with no
# ceiling: Retry.sleep_for_retry() calls time.sleep(retry_after) directly, and
# DEFAULT_BACKOFF_MAX (120s) bounds only the exponential path, not this one.
# So `Retry-After: 3600` on a 503 would sleep an hour inside one call, three
# times over, and the operator would see a run that had simply stopped
# producing output.
#
# Ignoring the header entirely would be worse - it is the server telling us
# precisely what it wants. But this tool already has a better answer than
# waiting for a long one: a 429/503 stops the run so the operator resumes
# later. So the header is honoured up to this bound, and anything longer
# becomes "stop the run" rather than "sleep through the afternoon".
RETRY_AFTER_MAX_SECONDS = 30.0


class BoundedRetryAfter(Retry):
    """urllib3's Retry, with a ceiling on how long a Retry-After header may
    make one call sleep. See RETRY_AFTER_MAX_SECONDS above for why.

    Subclassing rather than passing urllib3's own `retry_after_max=` because
    that argument only exists in very recent urllib3 (absent through at least
    2.6.0), and pinning that tightly would constrain an environment whose
    urllib3 comes in as a transitive dependency of requests. Overriding the
    accessor works on every version.

    urllib3 does not reuse the Retry object it is given - it counts down by
    calling increment(), which rebuilds through new() as `type(self)(...)`.
    That preserves this subclass, so the ceiling survives every retry rather
    than only applying to the first. A test pins that."""

    def get_retry_after(self, response) -> float | None:
        retry_after = super().get_retry_after(response)
        if retry_after is None:
            return None
        return min(retry_after, RETRY_AFTER_MAX_SECONDS)


IA_RETRY = BoundedRetryAfter(
    total=3,
    connect=3,
    read=3,
    redirect=False,
    allowed_methods=["POST", "HEAD", "GET", "OPTIONS"],
    status_forcelist=[429, 500, 501, 502, 503, 504],
    backoff_factor=1,
    respect_retry_after_header=True,
    raise_on_status=False,
)

# Passed to every internetarchive entry point this file calls. They all accept
# it through their `**get_item_kwargs`, which reaches get_session().
IA_HTTP_ADAPTER_KWARGS = {"max_retries": IA_RETRY}


def parsed_status_code(exc: Exception) -> int | None:
    """The HTTP status Internet Archive really returned, as the integer some
    layer already parsed - or None when the exception carries no status at
    all (a connection reset, a read timeout, or one of this file's own
    guards).

    Both callers below - is_rate_limit_error() and is_retryable_ia_error() -
    read the status through here rather than each reaching for the
    attributes themselves, so the rule that neither may fall back to
    `str(exc)` is stated in exactly one place. The two sources are
    UploadFailed.status_code (set in this file) and `.response.status_code`
    on a requests exception; the long comment above RATE_LIMIT_STATUS_CODES
    explains why both are trustworthy and why message text is not.

    The chain is walked because internetarchive's session.get_metadata()
    re-raises every failure as `type(exc)(error_msg)` - a fresh exception of
    the same class, built from the message alone - which drops `.response`.
    `internetarchive.upload()` reads an item's metadata before transferring
    anything, so that is a live path to a real rate limit, and stripped of
    its status a 503 read as an ordinary failure while the run ground on
    through the rest of the chunk. Python's implicit chaining still holds the
    ORIGINAL exception, `.response` and all, as the stripped copy's
    `__context__` - so the status is recoverable structurally, and this
    function never has to fall back to reading the message text that
    `type(exc)(error_msg)` did preserve.

    The outermost status wins: an exception raised while handling an older
    one reports its own failure, not the one underneath it."""
    seen: set[int] = set()
    current: BaseException | None = exc
    # __context__ can form a cycle - CPython only breaks the one it can see
    # when setting the context, and one can also be assigned directly - and an
    # unguarded walk would hang the run rather than fail a row.
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        status_code = getattr(current, "status_code", None)
        if status_code is None:
            status_code = getattr(getattr(current, "response", None), "status_code", None)
        if status_code is not None:
            return status_code
        current = current.__context__
    return None


def is_rate_limit_error(exc: Exception) -> bool:
    return parsed_status_code(exc) in RATE_LIMIT_STATUS_CODES


class UploadFailed(RuntimeError):
    """Raised by upload_row() below when internetarchive.upload() returns a
    not-ok Response. Subclasses RuntimeError (rather than plain Exception)
    so the pre-existing `pytest.raises(RuntimeError, match="503")` caller
    keeps working unchanged.

    Carries `status_code` as the PARSED INTEGER `response.status_code` -
    never reconstructed from the message text later - specifically so
    is_rate_limit_error() can look at the real, structured value Internet
    Archive returned instead of scanning `response.text`, which is
    arbitrary server-supplied prose that might itself contain a string like
    "status 503" for an unrelated reason (a mirrored error, a proxy
    message) even when the real status was something else entirely. See
    is_rate_limit_error()'s own comment and docs/DECISIONS.md, "Rate-limit
    detection uses a parsed status code, never message text"."""

    def __init__(self, message: str, *, status_code: int | None):
        super().__init__(message)
        self.status_code = status_code


# Retry exists for one specific gap. The pinned internetarchive (traced on
# 5.10.1, re-checked on 5.11.1) mounts a retrying HTTP adapter - urllib3 Retry(total=3, connect=3, read=3,
# backoff_factor=1) - in ArchiveSession.__init__, but ONLY on archive.org,
# and deliberately not on s3.us.archive.org (session.py: "Don't mount on
# s3.us.archive.org, only archive.org! IA-S3 requires a more complicated
# retry workflow"). So metadata reads and modify_metadata POSTs already get
# three transport-level attempts, while the S3 file transfer - the slowest
# call this tool makes, minutes long for a 10 MB photograph on a domestic
# link - gets none. Item.upload_file()'s own `retries` argument would not
# close that gap either: it defaults to `retries or 0` and only ever fires
# on a 503, never on a timeout. See docs/decisions/QUOTA-AND-RUNS.md,
# "Retry covers transport failures, never refusals".
#
# 5xx statuses worth repeating. 503 and 429 are deliberately ABSENT: they
# are Internet Archive saying "slow down", and this tool already answers
# that with something stronger than a retry - is_rate_limit_error() stops
# the whole run after the current chunk's confirm write so the operator
# resumes later. Retrying them here would delay that stop for every
# rate-limited row while making the overload marginally worse.
RETRYABLE_STATUS_CODES = (500, 502, 504)

# Transport failures, which arrive as an exception with no status at all
# because no HTTP response was ever completed. requests.exceptions.Timeout
# covers both ConnectTimeout and ReadTimeout - the latter is the failure
# reported in the issue this was written for.
RETRYABLE_EXCEPTIONS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout)

# Three attempts, not more. A row that fails all three is logged as an
# ordinary failure and picked up by the next run - re-running is already the
# supported recovery, and an identifier is never burned by a failed attempt
# (see docs/decisions/QUOTA-AND-RUNS.md). Deeper retries would mostly buy
# longer waits before reaching that same outcome.
RETRY_ATTEMPTS = 3
RETRY_BASE_SECONDS = 2.0
RETRY_MAX_SECONDS = 30.0

# Whatever the retried operation returns, returned unchanged to its caller.
_RetryResult = TypeVar("_RetryResult")


def is_retryable_ia_error(exc: Exception) -> bool:
    """Whether repeating this call could plausibly succeed.

    A parsed status decides on its own when there is one: a server that
    answered 403 Access Denied, or rejected a metadata field with a 400,
    will answer identically to an identical request, so retrying only
    lengthens the walk to the same refusal. Only when no status exists at
    all does the exception's type get a say, and then only for the transport
    failures listed above.

    Anything unrecognized is NOT retryable. This file's own guards -
    upload_row()'s blank-filename ValueError, the unprepared-Request
    RuntimeError, MetadataUnchanged - land here, and so would a bug; none of
    them is made truer by a second attempt."""
    status_code = parsed_status_code(exc)
    if status_code is not None:
        return status_code in RETRYABLE_STATUS_CODES
    return isinstance(exc, RETRYABLE_EXCEPTIONS)


def retry_delay(attempt: int) -> float:
    """Seconds to wait before attempt number `attempt + 1`, counting from 0.

    Equal jitter: half of a ceiling that doubles each time, plus a random
    share of the other half. The randomness matters because a chunk's rows
    fail in lockstep when archive.org is briefly unwell, and a fixed backoff
    would send them all back at the same instant. The fixed half matters
    because full jitter can draw a delay near zero, and a wait that does not
    wait cannot outlast the slowdown it exists for."""
    ceiling = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2**attempt))
    return ceiling / 2 + random.uniform(0, ceiling / 2)


def retry_ia_call(operation: Callable[[], _RetryResult], describe: str) -> _RetryResult:
    """Run `operation`, repeating it through transient failures.

    The exception from the final attempt is re-raised UNCHANGED rather than
    wrapped: every caller's `except Exception` branch logs `str(exc)` and
    hands the object to is_rate_limit_error(), so wrapping it would both
    change what the log records and hide the parsed status the run-stopping
    decision reads.

    Each retry prints a line, because the alternative is a run that appears
    hung for seconds at a time with no indication that anything is being
    handled. `describe` names the call so the message stands on its own in
    sync-metadata's output, which has no per-row progress line above it."""
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return operation()
        except Exception as exc:
            is_last_attempt = attempt == RETRY_ATTEMPTS - 1
            if is_last_attempt or not is_retryable_ia_error(exc):
                raise
            delay = retry_delay(attempt)
            print(
                f"    - {describe}: attempt {attempt + 1} of {RETRY_ATTEMPTS} failed "
                f"({format_row_error(exc)}); retrying in {delay:.1f}s"
            )
            time.sleep(delay)
    # Unreachable: the loop either returns or raises on its last attempt.
    raise AssertionError(f"{describe} exhausted its retries without raising")


def upload_row(row: dict, target_identifier: str, collection: str, files_dir: str | Path) -> None:
    file_name = (row.get("file") or "").strip()
    if not file_name:
        # Defence in depth against the single most damaging outcome in this
        # system. `Path(files_dir) / ""` is files_dir ITSELF, and
        # internetarchive's Item.upload() iterates a directory argument, so a
        # blank name would send the whole data tree recursively into one
        # permanent, unrenameable item. plan_upload_targets already refuses to
        # plan such a row (it is NOT_READY); this raise makes the path
        # unreachable by construction rather than by an upstream caller's
        # discipline. Raising is safe for a run in flight: SheetUploadRun
        # catches it per row, logs a failure and moves on.
        raise ValueError(
            f"upload of '{target_identifier}' refused: the row has no 'file' value, and "
            "uploading a blank filename would send the entire files_dir recursively into "
            "one permanent item"
        )
    file_path = Path(files_dir) / file_name
    metadata = {
        key: (value or "").strip()
        for key, value in row.items()
        if key not in ("identifier", "file") and (value or "").strip()
    }
    metadata["date"] = (row.get("date") or "").strip() or UNDATED_PLACEHOLDER
    metadata["collection"] = collection

    def send() -> None:
        """The retried unit. It covers the not-ok-Response check as well as
        the call, so a 500 that arrives as a Response is retried on the same
        terms as one that arrives as an exception.

        Repeating the transfer is safe because `checksum=True` makes
        Internet Archive skip a file whose MD5 already matches the item's -
        so a retry after a timeout that had in fact landed re-sends nothing
        and creates no duplicate. Nor can a retry burn an identifier: the
        target identifier is chosen before this function is reached and is
        the same on every attempt."""
        responses = internetarchive.upload(
            target_identifier,
            files=[str(file_path)],
            metadata=metadata,
            verbose=True,
            checksum=True,
            http_adapter_kwargs=IA_HTTP_ADAPTER_KWARGS,
        )
        for response in responses:
            # internetarchive.upload() is typed to return Request | Response;
            # a Request is only ever returned when debug=True, which we never
            # pass, so this always holds at runtime. Narrowing it explicitly
            # keeps response.ok/.status_code/.text type-checker-clean.
            if isinstance(response, requests.Request):
                raise RuntimeError(
                    f"upload of '{target_identifier}' returned an unprepared Request instead of "
                    "a Response - this should be unreachable since debug is never passed"
                )
            if not response.ok:
                raise UploadFailed(
                    f"upload of '{target_identifier}' failed with status {response.status_code}: {response.text}",
                    status_code=response.status_code,
                )

    retry_ia_call(send, f"upload of '{target_identifier}'")


class MetadataUnchanged(Exception):
    pass


def update_metadata_row(row: dict, target_identifier: str) -> None:
    """Blank cells are dropped entirely, not sent as empty strings - a
    blank cell must mean "leave this field alone", not "clear it". To
    actually delete an existing field on the IA item, put the literal
    value REMOVE_TAG in that cell; the internetarchive library (and the
    official `ia` CLI's `--modify field:REMOVE_TAG`) treats that string as
    a delete sentinel and issues a metadata "remove" op for the field."""
    metadata = metadata_to_send(row)

    def send() -> None:
        """The retried unit, matching upload_row()'s. Repeating a metadata
        update is safe because it is a full statement of the fields to set,
        not an increment - applying it twice leaves the item exactly where
        applying it once does.

        MetadataUnchanged escapes on the first attempt rather than being
        retried: is_retryable_ia_error() has no status to act on for it and
        does not recognize the type, and it is in any case a normal outcome
        the sync loop counts separately, not a failure."""
        response = internetarchive.modify_metadata(
            target_identifier, metadata=metadata, http_adapter_kwargs=IA_HTTP_ADAPTER_KWARGS
        )
        # See the matching narrowing comment in upload_row(): modify_metadata()
        # is typed to return Request | Response, but a Request is only ever
        # returned when debug=True, which we never pass.
        if isinstance(response, requests.Request):
            raise RuntimeError(
                f"metadata update of '{target_identifier}' returned an unprepared Request instead of "
                "a Response - this should be unreachable since debug is never passed"
            )
        if not response.ok:
            try:
                error_message = json.loads(response.text).get("error", "")
            except (ValueError, AttributeError):
                error_message = ""
            if error_message == "no changes to _meta.xml":
                raise MetadataUnchanged(target_identifier)
            raise RuntimeError(
                f"metadata update of '{target_identifier}' failed with status {response.status_code}: {response.text}"
            )

    retry_ia_call(send, f"metadata update of '{target_identifier}'")


def build_sheets_service(key_path: Path):
    """The only place credentials are loaded and `googleapiclient.discovery.build` is called."""
    credentials = google_auth.load_service_account_credentials(key_path)
    return googleapiclient.discovery.build("sheets", "v4", credentials=credentials)


def build_sheet_client(config: ProjectConfig, live: bool) -> SheetClient:
    """The seam tests monkeypatch. Raises PlaceholderSheetId before loading credentials."""
    sheet_id = config.require_real_sheet_id(live)
    service = build_sheets_service(google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH)
    return SheetClient(service, sheet_id, config.sheet_tab)


@dataclass(frozen=True)
class FileOutcomes:
    """Two distinct failures that used to be one. `errors` are rows that
    asserted a file and were wrong; `blank` are rows that asserted nothing.
    Kept apart HERE because resolve_sheet_files blanks row['file'] on
    failure, so nothing downstream can tell them apart afterwards."""

    errors: dict[int, str]
    blank: dict[int, list[str]]


@dataclass(frozen=True)
class FileSurvey:
    """What the Sheet says versus what is on the drive.

    `claimed` is built before any matching runs, and `unclaimed` is its
    complement. That ordering is the whole safety property: a file another
    row already resolves to can never be proposed to a second row, which is
    the misattribution hazard in issue #1.

    `unresolved` holds only rows that ASSERTED a filename and were wrong.
    Rows whose file_template cells are blank are counted in `not_ready` and
    nowhere else - see survey_files()."""

    unresolved: dict[int, str]
    wanted: dict[int, str]
    claimed: set[str]
    unclaimed: dict[str, list[str]]
    not_ready: list[int]


def claim_key(folder_and_name: str) -> str:
    """The one spelling of "some row has already taken this file".

    `claimed` holds `<folder cell as typed>/<real disk name>`: the name half
    comes back from resolve_file() exactly as it is on disk, but the folder
    half is whatever the Sheet cell said. On Windows - a case-insensitive
    filesystem - `SOP CD 1` and `sop cd 1` are ONE folder holding ONE
    photograph, so keyed by the raw cell those two rows get two disjoint
    namespaces and the check that stops two rows being pointed at one file
    silently misses. os.path.normcase folds that difference on Windows and
    nothing on Linux, where two such folders really are two folders - but
    it goes by platform CONVENTION, not the actual filesystem, and on macOS
    the convention (identity) disagrees with the default filesystem (APFS
    is case-insensitive too), so the fold is done here instead. On the rare
    case-sensitive Mac volume that fold merely withholds a same-name-
    different-case file from auto-proposal - erring on the quiet side,
    where the un-folded key errs by letting two rows take one file.

    Every producer and every consumer of `claimed` goes through here: a set
    half of whose members are normalized is worse than one that is not
    normalized at all."""
    if sys.platform == "darwin":
        return folder_and_name.casefold()
    return os.path.normcase(folder_and_name)


def survey_files(rows: list[dict[str, str]], config: ProjectConfig) -> FileSurvey:
    """Resolve every row, then work out which files nothing points at.

    A row with a blank file_template cell is NOT unresolved - it is
    not-ready, the same split resolve_sheet_files() draws between `errors`
    and `blank`. Nobody asserted a file, so there is nothing to be wrong and
    nothing a proposal could be matched against. Filing those as unresolved
    made reconcile-files raise one dead prompt per uncatalogued row: on the
    real Sheet that is ~2,900 prompts with no proposal and no candidates,
    burying the ~150-300 genuinely fixable rows - the exact failure
    docs/decisions/READINESS.md exists to prevent. They are counted in
    `not_ready` so the run can say how many it passed over in one line.

    Deliberately does not mutate rows, unlike resolve_sheet_files(): this runs
    before any decision is made, and a row's cells must still read as the
    operator wrote them when they are shown one."""
    listing_cache: dict[Path, list[str]] = {}
    fields = template_fields(config.file_template)
    unresolved: dict[int, str] = {}
    wanted: dict[int, str] = {}
    claimed: set[str] = set()
    folders: set[str] = set()
    not_ready: list[int] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        folder = (row.get(fields[0]) or "").strip() if fields else ""
        name_field = fields[-1] if fields else ""

        blank = [name for name in fields if not (row.get(name) or "").strip()]
        if blank:
            not_ready.append(row_number)
            continue

        # After the blank check: a folder only an uncatalogued row names has
        # no row that could be prompted about it, so listing it is a disk
        # scan whose result nothing reads.
        folders.add(folder)
        try:
            claimed.add(
                claim_key(
                    resolve_file(
                        config.files_dir,
                        candidate_path(config.file_template, row),
                        listing_cache,
                    )
                )
            )
        except FileResolutionError:
            unresolved[row_number] = folder
            wanted[row_number] = (row.get(name_field) or "").strip()

    unclaimed: dict[str, list[str]] = {}
    for folder in sorted(f for f in folders if f):
        directory = Path(config.files_dir) / folder
        if not directory.is_dir():
            continue
        unclaimed[folder] = sorted(
            entry.name
            for entry in directory.iterdir()
            if entry.is_file()
            and entry.suffix.lower() in config.photo_extensions
            and claim_key(f"{folder}/{entry.name}") not in claimed
        )
    return FileSurvey(
        unresolved=unresolved,
        wanted=wanted,
        claimed=claimed,
        unclaimed=unclaimed,
        not_ready=not_ready,
    )


def scan_unclaimed_files(
    claimed: set[str], config: ProjectConfig
) -> tuple[dict[str, list[str]], list[str]]:
    """Every photo file under files_dir that no row claims, by folder.

    NOT the same map as survey_files' `unclaimed`, and the difference is the
    point: that one only lists folders some catalogued row already names,
    because reconcile can only prompt about rows that exist. Append is the
    opposite case - a brand-new donor folder with zero rows is exactly what
    it exists to pick up - so this walks every subdirectory of files_dir.

    The second return value names photo files sitting at the top of
    files_dir, outside any folder. A folder/name template cannot represent
    them, so no row can be appended for them - but they must be COUNTED,
    not silently invisible, or a stray file at the drive root never gets
    catalogued and nobody is ever told. Folders with nothing unclaimed are
    omitted entirely: an empty list would still render a folder heading."""
    unclaimed: dict[str, list[str]] = {}
    outside: list[str] = []
    for entry in sorted(Path(config.files_dir).iterdir(), key=lambda e: e.name):
        if entry.is_file():
            if entry.suffix.lower() in config.photo_extensions:
                outside.append(entry.name)
            continue
        names = sorted(
            child.name
            for child in entry.iterdir()
            if child.is_file()
            and child.suffix.lower() in config.photo_extensions
            and claim_key(f"{entry.name}/{child.name}") not in claimed
        )
        if names:
            unclaimed[entry.name] = names
    return unclaimed, outside


@dataclass(frozen=True)
class Decision:
    action: str          # "accept" | "reject" | "stop"
    filename: str        # the RESOLVED name, empty unless accepting
    # How the operator answered, not what they answered. [y] accepts the
    # proposal; [e] is a name they typed - which resolve_file() may well
    # resolve to the proposed file anyway. Comparing the two strings cannot
    # tell those apart, and the decision log has to.
    typed: bool = False


def prompt_for_decision(
    row_number: int,
    folder: str,
    wanted: str,
    proposal: Proposal | None,
    unclaimed: list[str],
    config: ProjectConfig,
    claimed: set[str],
    read_line=input,
) -> Decision:
    """Ask about one row and return what the operator decided.

    `read_line` is injected so tests drive this without a terminal.

    A typed name goes through the same resolve_file() every other path uses,
    so it must resolve to exactly one real file AND that file must not
    already be claimed. That is what keeps typing from becoming a new way to
    introduce the error being fixed - and it means a name typed without its
    extension, or in the wrong case, resolves anyway."""
    shown = wanted or "(blank)"
    print(f"row {row_number}  '{shown}'  does not resolve in '{folder}'")
    keys = "[y] accept   " if proposal else ""
    if proposal:
        print(f"       proposed: '{proposal.filename}'   ({proposal.reason})")
    # Reprinted on every path that lands the operator back at '>' from
    # somewhere else - after [l]'s listing or a failed [e] the keys have
    # scrolled away, and a bare '>' does not say which prompt this is.
    keys_line = f"       {keys}[n] not this one   [e] type it   [l] list unclaimed   [q] stop"
    print(keys_line)

    while True:
        answer = read_line("       > ").strip().lower()
        if answer == "q":
            return Decision(action="stop", filename="")
        if answer == "n":
            return Decision(action="reject", filename="")
        if answer == "y" and proposal:
            return Decision(action="accept", filename=proposal.filename)
        if answer == "l":
            if unclaimed:
                for name in unclaimed:
                    print(f"           {name}")
            else:
                print(f"           nothing unclaimed in '{folder}'")
            print(keys_line)
            continue
        if answer == "e":
            typed = read_line("       filename> ").strip()
            if not typed:
                print(keys_line)
                continue
            try:
                resolved = resolve_file(config.files_dir, f"{folder}/{typed}", {})
            except FileResolutionError as exc:
                print(f"           {exc}")
                print(keys_line)
                continue
            name = resolved.rpartition("/")[2]
            if claim_key(resolved) in claimed:
                print(f"           '{name}' is already used by another row")
                print(keys_line)
                continue
            return Decision(action="accept", filename=name, typed=True)
        print("           expected y, n, e, l or q")


def resolve_sheet_files(rows: list[dict[str, str]], config: ProjectConfig) -> FileOutcomes:
    """Resolves each row's file against disk BEFORE validation runs, so a row
    either carries a real, disk-verified 'file' value (and the resolved name,
    which may differ from what the Sheet cell says - see resolve_file() - also
    becomes 'ia_identifier_bib') or is recorded here as one of two DIFFERENT
    outcomes: a row that named a file and was wrong (`errors`, carrying the
    resolver's own message), or a row that named nothing at all (`blank`,
    carrying which of file_template's cells are empty).

    Splitting the two here is the whole point. Below, on failure, the Sheet
    cell's raw, UNVERIFIED candidate must not survive as row['file'] - left in
    place it would coincidentally resolve as a literal path for the later disk
    check (or for an upload), silently masking the fact that resolution never
    actually confirmed this file exists. The cost of that blanking is that
    afterwards a row nobody filled in and a row with a typo'd filename are
    indistinguishable, both being row['file'] == "". This function is the last
    place the candidate still exists, so it is the only place the distinction
    can be drawn - and drawing it matters in one direction especially: a
    broken row misfiled as blank is downgraded from "fix me" to "nobody has
    got to it yet", which is how it stops ever being fixed.

    Shared by `validate` and `upload` deliberately: the value `upload` records
    in `ia_identifier_bib` has to be the same resolved name `validate` showed
    the operator, and two copies of this loop would eventually disagree.

    Rows resolving to the SAME file are all errors - every row in the group,
    not just the ones after the first, because the tool usually cannot know
    which row is the wrong one and flagging all but one silently elects a
    winner. The exception is a group holding exactly one row that has already
    uploaded: that row's identifier is permanent and its row is the only link
    between the identifier and its metadata, so it is named as the one to
    keep rather than offered for deletion alongside the others. Two
    rows claiming one photograph would mint two permanent identifiers for it,
    and - issue #1 - identical file_template cells are identical fingerprints,
    which is exactly when the mid-run-edit guard stops being able to tell the
    rows apart. Keyed on claim_key() of the RESOLVED path, not the raw cells:
    the resolver is deliberately forgiving (case, extension), so two rows can
    spell one disk file differently, and the raw cells would miss them."""
    listing_cache: dict[Path, list[str]] = {}
    errors: dict[int, str] = {}
    blank: dict[int, list[str]] = {}
    resolved_rows: dict[int, str] = {}
    claims: dict[str, list[int]] = {}
    fields = template_fields(config.file_template)

    for offset, row in enumerate(rows):
        row_number = offset + 2  # header is row 1

        blank_cells = [name for name in fields if not (row.get(name) or "").strip()]
        if blank_cells:
            # At least one cell the template needs is empty, so no candidate
            # path can be built and there is nothing to resolve - which is
            # what makes the old "matching ''" message unreachable rather
            # than merely rare. Never an error even when the OTHER cells are
            # filled: a blank cell is a not-yet-answered question, not a
            # wrong answer. len(blank_cells) < len(fields) is what tells a
            # partially-filled row from an untouched one, so the report can
            # name the specific missing cell instead of lumping the two
            # together.
            blank[row_number] = blank_cells
            row["file"] = ""
            continue

        candidate = candidate_path(config.file_template, row)
        try:
            resolved = resolve_file(config.files_dir, candidate, listing_cache)
        except FileResolutionError as exc:
            errors[row_number] = str(exc)
            row["file"] = ""
            continue
        row["file"] = resolved
        row[IA_IDENTIFIER_BIB_COLUMN] = resolved
        resolved_rows[row_number] = resolved
        claims.setdefault(claim_key(resolved), []).append(row_number)

    # Second pass, because the FIRST row of a duplicate group is already
    # resolved and recorded by the time the second one reveals the conflict.
    for claimants in claims.values():
        if len(claimants) < 2:
            continue
        # "The tool cannot know which row is the wrong one" stops being true
        # the moment exactly one claimant has already uploaded: its identifier
        # is permanent, and its Sheet row is the only thing tying that
        # identifier to its metadata - including the ia_url `sync-metadata`
        # reads to find its targets. Left symmetrical, the message invited
        # deleting precisely that row. So the DONE claimant is named as the
        # one to keep, and only the others are offered for deletion. Two DONE
        # claimants is a worse problem than this function can adjudicate (one
        # photograph already holds two permanent identifiers), so it falls
        # back to the symmetrical wording rather than electing a winner.
        uploaded = [n for n in claimants if classify_row(rows[n - 2]) is RowState.DONE]
        keeper = uploaded[0] if len(uploaded) == 1 else None
        for row_number in claimants:
            others = [n for n in claimants if n != row_number]
            label = "row" if len(others) == 1 else "rows"
            listed = ", ".join(str(n) for n in others)
            if keeper == row_number:
                remedy = (
                    f"this row has already uploaded, so it is the one to keep - fix or "
                    f"delete {label} {listed} instead"
                )
            elif keeper is not None:
                remedy = (
                    f"row {keeper} has already uploaded and must be kept, so delete this "
                    "row, or point it at the right file"
                )
            else:
                remedy = "delete the duplicate row, or point it at the right file"
            errors[row_number] = (
                f"resolves to '{resolved_rows[row_number]}' - the same file as {label} "
                f"{listed}. Two rows cannot claim one photograph: {remedy}"
            )
            # The same invariant as the failure paths above: a row filed in
            # `errors` keeps no 'file' (or resolved bib) that a later disk
            # check or an upload could coincidentally use.
            row = rows[row_number - 2]
            row["file"] = ""
            row[IA_IDENTIFIER_BIB_COLUMN] = ""

    return FileOutcomes(errors=errors, blank=blank)


def split_structure_results(
    structure_results: list[RowValidation], row_count: int
) -> tuple[list[RowValidation], list[RowValidation]]:
    """Split sheet_structure_validation()'s output into (header-level,
    per-data-row) - the two halves every Sheet command treats differently: a
    bad header stops the whole run, a bad row is skipped.

    check_grid_shape and validate_rows both number rows `offset + 2`, so
    `row_number - 2` indexes the data rows exactly. The bounds check is
    load-bearing, not defensive: a bare `row_number - 2` would quietly fold
    row 1 - where check_column_map's header defects are filed - into the LAST
    data row via negative indexing."""
    header_level: list[RowValidation] = []
    per_row: list[RowValidation] = []
    for entry in structure_results:
        index = entry.row_number - 2
        (per_row if 0 <= index < row_count else header_level).append(entry)
    return header_level, per_row


def validate_sheet_grid(
    rows: list[dict[str, str]],
    registry: dict,
    config: ProjectConfig,
    structure_results: list[RowValidation],
    outcomes: FileOutcomes,
) -> tuple[list[RowValidation], list[RowValidation]]:
    """Returns (header_results, row_results). row_results holds exactly one
    entry per row in `rows`, in the same order - which is what
    format_lifecycle_summary requires and what lets a caller pair a row with
    its verdict by index.

    A structural problem with one specific data row (a long row) belongs IN
    that row's own result, not in a second entry printed beside it. Filing it
    separately made the row appear twice with opposite verdicts ("[FAIL] row
    9" from check_grid_shape, "[PASS] row 9" from validate_rows), inflated
    format_report's denominator past the number of data rows the Sheet
    actually has, and - worst - left the row counted as "ready to upload" by
    the lifecycle summary, which reads row_results alone. check_grid_shape and
    validate_rows both number rows `offset + 2`, so row_number - 2 indexes
    row_results exactly. Anything outside that range is header-level and keeps
    its own row-1 entry (as does _GRID_SHAPE_ROW_NUMBER_RE's row-1 fallback) -
    the bounds check is load-bearing, not defensive: a bare row_number - 2
    would quietly fold row 1 into the LAST data row via negative indexing."""
    row_results = validate_sheet_rows(
        rows,
        config.files_dir,
        registry,
        config.project_id,
        required_for_upload=config.required_for_upload,
    )

    for row_number, message in outcomes.errors.items():
        # The resolver's own message: it names the folder and the name that
        # was looked for, which is the actionable part. There is no longer a
        # generic "missing required column 'file'" line behind it - `file`
        # left required_columns entirely once FileOutcomes became the sole
        # source of truth for whether a row's file resolved (see
        # SHEET_REQUIRED_COLUMNS' comment), so this is the whole story for a
        # broken-filename row now, not merely the first line of it.
        row_result = row_results[row_number - 2]
        row_result.errors = [message] + row_result.errors

    for row_number, blank_cells in outcomes.blank.items():
        # Blank template cells are a readiness fact, not an error: nobody
        # asserted a file, so there is nothing to be wrong. Extended onto
        # missing_fields alongside any blank required_for_upload columns
        # validate_sheet_rows already put there, required_for_upload names
        # first - see missing_fields' own ordering note on RowValidation.
        #
        # De-duplicated (dict.fromkeys preserves that order) because the two
        # sources can legitimately name the same column: an operator may list
        # a file_template column in required_for_upload - e.g.
        # ["title", "file_name"] - and check_required_for_upload accepts it,
        # because it IS a real column in the Sheet. Left duplicated, one
        # not-ready row reported "2 missing file_name" and dragged in the
        # overlap parenthetical that says the counts do not sum.
        row_result = row_results[row_number - 2]
        row_result.missing_fields = list(
            dict.fromkeys(row_result.missing_fields + blank_cells)
        )

    header_results, row_structure_results = split_structure_results(
        structure_results, len(row_results)
    )
    for entry in row_structure_results:
        # structural errors first: a long row's mis-attributed values are
        # the likely cause of whatever content errors follow.
        row_result = row_results[entry.row_number - 2]
        row_result.errors = entry.errors + row_result.errors

    return header_results, row_results


def check_required_for_upload(config: ProjectConfig, column_map: ColumnMap) -> list[str]:
    """A required_for_upload name that matches no column makes EVERY row
    not-ready, so nothing ever uploads and the report reads "3,000 rows not
    yet catalogued" - which looks plausible. Silent, permanent, and
    self-consistent, which is why this is a hard error rather than a
    warning."""
    known = sorted(set(column_map.field_names.values()))
    return [
        f"required_for_upload names {name!r}, which is not a column in this Sheet. "
        f"Known columns: {', '.join(known)}"
        for name in config.required_for_upload
        if name not in known
    ]


# How many distinct values a "that batch matches nothing" message lists before
# it summarizes the rest. A theme column with hundreds of values would
# otherwise bury the message it is attached to.
MAX_LISTED_BATCH_VALUES = 20


class BatchScopeError(Exception):
    """--batch cannot be honored as typed.

    Every one of these is a refusal rather than a fallback, and they all guard
    the same failure: a --batch run that quietly matches every row, or quietly
    matches none. Both read as success. "Nothing to upload" in particular is
    indistinguishable from "this batch is already finished", so a typo'd value
    would look like a completed run.

    Carries the operator-facing message; the commands print it to stderr and
    exit non-zero."""


def fold_batch_value(value: str) -> str:
    """The matching rule: surrounding whitespace dropped, case folded.

    These cells are typed by hand into a Sheet, so 'Logging ', 'logging' and
    'LOGGING' are one batch. The cost is that two themes differing only in
    case can never be scoped apart, which is the right trade here: on a Sheet
    filled in by several people over months, a case difference is far more
    likely to be a typo than a distinction."""
    return value.strip().casefold()


def batch_column_for(config: ProjectConfig, batch_value: str, registry_path: str) -> str:
    """The half of --batch's validation that needs no Sheet: the value is not
    empty, and this project says which column holds a row's batch.

    Split out so a command can refuse a mistyped flag BEFORE reading the
    Sheet, resolving several thousand filenames against the drive and
    validating every row - the same fail-fast reasoning --limit and
    --chunk-size are read under."""
    value = batch_value.strip()
    if not value:
        raise BatchScopeError(
            '--batch needs the value to scope the run to, e.g. --batch "Logging". An '
            "empty value does not mean 'every row' - drop the flag entirely for that."
        )

    if config.batch_column is None:
        raise BatchScopeError(
            f"project '{config.project_id}' has no 'batch_column' in {registry_path}, so "
            "--batch has nothing to match against. Which column holds a row's batch is a "
            "per-project fact, so it lives in the registry rather than on the command "
            'line: add "batch_column": "<normalized column name>" to the project '
            "block. Refusing rather than running unfiltered - a --batch that uploaded "
            "every row would look exactly like a successful batch run."
        )

    return config.batch_column


def resolve_batch_scope(
    args,
    config: ProjectConfig,
    column_map: ColumnMap,
    rows: list[dict[str, str]],
) -> set[int] | None:
    """The row numbers --batch narrows this run to, or None when the flag was
    not passed. The single definition of scope `validate` and `upload` share -
    the two commands previewing and performing different sets of rows would
    make the preview worthless."""
    batch_value = getattr(args, "batch", None)
    if batch_value is None:
        return None
    return batch_row_numbers(rows, config, column_map, batch_value, args.registry)


def in_batch_scope(results: list[RowValidation], scope: set[int] | None) -> list[RowValidation]:
    """Row results this run is reporting on. Everything outside the batch is
    another run's business, including its uncatalogued rows."""
    if scope is None:
        return results
    return [result for result in results if result.row_number in scope]


def batch_row_numbers(
    rows: list[dict[str, str]],
    config: ProjectConfig,
    column_map: ColumnMap,
    batch_value: str,
    registry_path: str,
) -> set[int]:
    """The Sheet rows in scope for --batch, as row numbers (header is row 1).

    Row NUMBERS, not a filtered list of rows, and that is the whole point.
    Everything downstream of the Sheet read is positional - validate_rows and
    plan_upload_targets both number rows `offset + 2`, and several functions
    index back with `rows[row_number - 2]` - so compacting `rows` would
    silently renumber every row after the first gap. Worse, plan_upload_targets
    scans EVERY row for identifiers already spent; handed only one batch's
    rows it would re-mint numbers another batch is already holding, and
    identifiers are permanent. So the full list travels the whole way through
    and callers narrow what they report and upload using this set.

    Raises BatchScopeError for the four ways this cannot be honored; see that
    class for why none of them is a fallback."""
    column = batch_column_for(config, batch_value, registry_path)
    value = batch_value.strip()

    known = sorted(set(column_map.field_names.values()))
    if column not in known:
        # The same failure check_required_for_upload guards, reached a
        # different way: left alone this reads every row's batch as blank,
        # matches nothing, and reports the batch as already finished.
        raise BatchScopeError(
            f"project '{config.project_id}': batch_column names {column!r}, which is not "
            f"a column in this Sheet. Known columns: {', '.join(known)}"
        )

    wanted = fold_batch_value(value)
    scope: set[int] = set()
    # First-seen spelling per folded value, so the listing below de-duplicates
    # exactly the way matching does - 'Logging' and 'logging' are one entry,
    # not two, because they are one batch.
    present: dict[str, str] = {}
    for offset, row in enumerate(rows):
        cell = (row.get(column) or "").strip()
        if not cell:
            # A row nobody has catalogued yet has no batch, and must not join
            # whichever one happens to be running.
            continue
        folded = fold_batch_value(cell)
        present.setdefault(folded, cell)
        if folded == wanted:
            scope.add(offset + 2)

    if not scope:
        if not present:
            raise BatchScopeError(
                f"--batch {value!r} matches no row: the '{column}' column is empty in "
                "every row of this Sheet, so no row has been assigned a batch yet."
            )
        ordered = [present[key] for key in sorted(present)]
        shown = ordered[:MAX_LISTED_BATCH_VALUES]
        listing = ", ".join(repr(entry) for entry in shown)
        if len(ordered) > len(shown):
            listing += f", and {len(ordered) - len(shown)} more"
        raise BatchScopeError(
            f"--batch {value!r} matches no row in the '{column}' column. Values present: "
            f"{listing}. Check the spelling against the Sheet - an unmatched --batch would "
            "otherwise report 'nothing to upload', which is what a finished batch looks "
            "like."
        )

    return scope


class SheetSetupFailed(Exception):
    """A Sheet-path command could not get far enough to start its own work.

    Carries no message of its own - the operator-facing one is already on
    stderr by the time this is raised. Raising rather than returning a
    sentinel is what lets read_sheet() and validate_sheet_content() have a
    single return type, so a caller cannot forget to check one."""


@dataclass(frozen=True)
class SheetRead:
    """A project's Sheet, read and far enough along that a command can start.

    Holds the raw `grid` as well as `rows`: sheet_structure_validation needs
    the grid to see a data row longer than the header, which grid_to_rows has
    already truncated away by the time it produces rows. `client` is kept
    because `upload` writes back through the same one it read with."""

    registry: dict
    config: ProjectConfig
    live: bool
    client: SheetClient
    grid: list[list[str]]
    column_map: ColumnMap
    rows: list[dict[str, str]]
    structure_results: list[RowValidation]


def sheet_banner(config: ProjectConfig, live: bool) -> str:
    """The line every Sheet-path command prints once its project is loaded,
    before it reads the Sheet. Not the run's first line: main() dates the run
    first, and validate's --batch refusal comes ahead of this.

    The run mode is this project's core safety design - a rehearsal must
    never touch the real Sheet - so which spreadsheet and tab back it is
    printed unconditionally, not just on success. A human staring at a report
    has to be able to confirm at a glance that they are pointed where they
    think they are."""
    mode = "live" if live else "test"
    return (
        f"project '{config.project_id}': {mode} mode, "
        f"spreadsheet '{config.sheet_id_for(live)}', tab '{config.sheet_tab}'"
    )


def sheet_sharing_target() -> str:
    """Who the Sheet must be shared with, for error messages."""
    key_path = google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH
    return google_auth.service_account_email(key_path) or (
        f"the service account whose key is at {key_path}"
    )


NO_DATA_ROWS = (
    "the Sheet has no data rows (only a header, or nothing at all) - check that "
    "'sheet_tab' in the project's registry entry names the right tab, and that "
    "the Sheet has actually been populated and shared"
)


def read_sheet(args, registry: dict, config: ProjectConfig, live: bool, command: str) -> SheetRead:
    """Everything every Sheet-path command (validate, upload, sync-metadata,
    reconcile-files, append-rows) does between printing its banner and
    starting its own work.

    Deliberately does NOT print the banner or run the per-command flag
    checks. Those happen first and differ per command - `upload` validates
    --limit, for one - and moving the placeholder check ahead of them would
    change which complaint an operator sees when both are wrong.

    `command` appears in the placeholder message only ("before running
    upload"), which is the sole text that differed between the per-command
    copies this replaced."""
    sheet_id = config.sheet_id_for(live)
    mode = "live" if live else "test"

    if is_placeholder_sheet_id(sheet_id):
        print(
            f"the {mode}-mode spreadsheet ID for project '{config.project_id}' is still the "
            f"placeholder '{sheet_id}' - edit it in {args.registry} to the real Google Sheet ID "
            f"before running {command}.",
            file=sys.stderr,
        )
        raise SheetSetupFailed

    try:
        client = build_sheet_client(config, live)
    except google_auth.AuthUnavailable as exc:
        print(f"could not authenticate to Google Sheets: {exc}", file=sys.stderr)
        raise SheetSetupFailed from exc
    try:
        grid = client.read_grid()
    except HttpError as exc:
        print(
            f"could not read spreadsheet '{sheet_id}' tab '{config.sheet_tab}': {exc}. Check "
            f"that 'sheet_tab' in {args.registry} names the tab exactly (case-sensitive) as it "
            "appears in the Sheet, that the spreadsheet ID is correct, and that the Sheet has "
            f"been shared, as Editor, with {sheet_sharing_target()}.",
            file=sys.stderr,
        )
        raise SheetSetupFailed from exc

    column_map, rows = grid_to_rows(grid)

    # mediatype is a per-project constant, never a Sheet column - inject it
    # before validating so every row satisfies the required-column check
    # instead of failing on a column that was never meant to exist. Harmless
    # for sync-metadata, which never sends it: sheet_metadata_fields()
    # subtracts PIPELINE_OWNED_FIELDS, and Internet Archive will not change
    # an item's mediatype after upload anyway.
    for row in rows:
        row["mediatype"] = config.mediatype

    structure_results = sheet_structure_validation(column_map, grid)

    if not rows:
        # A dedicated branch, not just another row-1 structural error: an
        # empty read is far more likely to mean a wrong tab name, an
        # unpopulated copy of the Sheet, or a Sheet never actually shared
        # with the service account than a real project with zero
        # rows, and reporting that as success would defeat the purpose of
        # running the command at all. Handled separately from the normal
        # report (rather than folded into sheet_structure_validation's row-1
        # entry) specifically so the summary line never has to say
        # "0/1 rows passed" - that "1" would be a synthetic entry standing in
        # for zero real rows, which reads as nonsense arithmetic.
        if structure_results:
            print("\n".join(_format_result_lines(structure_results)))
            print()
        print(NO_DATA_ROWS)
        raise SheetSetupFailed

    return SheetRead(
        registry=registry,
        config=config,
        live=live,
        client=client,
        grid=grid,
        column_map=column_map,
        rows=rows,
        structure_results=structure_results,
    )


def validate_sheet_content(
    sheet: SheetRead, args
) -> tuple[list[RowValidation], list[RowValidation]]:
    """The registry-vs-Sheet checks and per-row validation `validate` and
    `upload` share. Returns (header_results, row_results).

    `sync-metadata` does not use this: it corrects rows that already
    uploaded, so a file_template that no longer matches the Sheet is not its
    problem, and re-resolving files would make a correction depend on the
    drive being attached.

    Callers that need row fingerprints (upload, for the mid-run-edit guard)
    must take them BEFORE calling this - resolve_sheet_files rewrites
    row['file'] to the resolved name, and the fingerprint has to be the raw
    cell to compare against a fresh read."""
    # A file_template naming a column the Sheet's header row doesn't have (a
    # registry typo, or a Sheet whose columns changed) is checked once, here,
    # rather than surfacing as the same resolution failure repeated on every
    # row. Deliberately after read_sheet's no-rows branch: an empty or
    # header-only Sheet already gets a more useful diagnostic.
    try:
        check_file_template(sheet.config.file_template, sheet.column_map)
    except TemplateError as exc:
        print(
            f"project '{sheet.config.project_id}': {exc} - fix 'file_template' in "
            f"{args.registry}",
            file=sys.stderr,
        )
        raise SheetSetupFailed from exc

    config_errors = check_required_for_upload(sheet.config, sheet.column_map)
    if config_errors:
        print("\n".join(config_errors), file=sys.stderr)
        print(
            f"fix required_for_upload in {args.registry} - as written, every row would "
            "be reported as not yet catalogued and nothing would ever upload",
            file=sys.stderr,
        )
        raise SheetSetupFailed

    # See docs/decisions/FILES-AND-METADATA.md, "A file is found by
    # resolution, not by constructing a path". Both commands run the
    # identical two steps, so the value `upload` records in
    # ia_identifier_bib is the one `validate` showed the operator.
    file_outcomes = resolve_sheet_files(sheet.rows, sheet.config)
    return validate_sheet_grid(
        sheet.rows, sheet.registry, sheet.config, sheet.structure_results, file_outcomes
    )


def build_deployment_checks(args, *, include_network: bool) -> list[deployment.Check]:
    """The one check list. `doctor` runs it; `setup` runs it and applies fix()."""
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    live = bool(args.live)
    repo_root = REPO_ROOT
    key_path = google_auth.DEFAULT_SERVICE_ACCOUNT_KEY_PATH
    install = install_command_for(args)

    checks = [
        deployment.python_version_check(sys.version_info[:2], install),
        deployment.dependencies_check(install),
        deployment.key_present_check(key_path),
        deployment.key_mode_check(key_path),
        deployment.ia_credentials_check(),
        deployment.ia_credentials_mode_check(),
        deployment.sheet_id_check(config, live, args.registry),
        deployment.drive_check(Path(config.files_dir)),
    ]

    if include_network:
        # A closure, not an import: build_sheets_service stays the only place
        # credentials are loaded, and deployment.py never imports ia_bulk.
        # Memoized because both Sheet checks share it: two clients meant two
        # token fetches and two full reads of a 10,000-row Sheet per `doctor`.
        cached_grid: dict[str, list[list[str]]] = {}

        def read_grid() -> list[list[str]]:
            # Reading a placeholder ID only earns a 404 and a misleading "share it" remedy.
            if config.sheet_id_is_placeholder(live):
                mode = "live" if live else "test"
                raise deployment.SheetNotChecked(f"the {mode}-mode sheet_id is still a placeholder")
            if "grid" not in cached_grid:
                cached_grid["grid"] = build_sheet_client(config, live).read_grid()
            return cached_grid["grid"]

        def sync_refusal(grid: list[list[str]]) -> str | None:
            # read_sheet's refusal first, then sync_from_sheet's, in the order the agent meets them.
            column_map, rows = grid_to_rows(grid)
            if not rows:
                return NO_DATA_ROWS
            refusal = sync_header_refusal(column_map, config, args.registry)
            if refusal is None:
                return None
            message, details = refusal
            return "; ".join([message, *details])

        checks.extend(
            [
                deployment.sheet_reachable_check(read_grid, sheet_sharing_target()),
                deployment.sync_columns_check(read_grid, sync_refusal),
            ]
        )

    spec = launch_agent.sync_agent_spec(repo_root, config.project_id, args.registry)
    checks.extend(
        [
            deployment.agent_plist_check(spec, Path.home(), install),
            deployment.agent_log_directory_check(spec, Path.home(), install),
            deployment.agent_loaded_check(spec, install),
        ]
    )
    return checks


def install_command_for(args) -> deployment.InstallCommand:
    """The ./install.sh line that repeats this run. install.sh runs setup from
    REPO_ROOT, so only a registry other than REPO_ROOT's own needs --registry."""
    registry = Path(args.registry).resolve()
    is_default = registry == REPO_ROOT / DEFAULT_REGISTRY
    return deployment.InstallCommand(args.project, None if is_default else registry)


# {command} is install_command_for(args).render(enable_agent=True).
ENABLE_AGENT_NEEDS_LIVE = (
    "--enable-agent loads an agent that runs `sync-metadata --live`, so it refuses to run "
    "without --live: without it setup would verify the TEST Sheet and then start an hourly "
    "live sync against a real Sheet whose ID, sharing and sync columns were never checked. "
    "Run: {command}"
)

ENABLE_AGENT_NEEDS_NETWORK = (
    "--enable-agent cannot be combined with --offline: the Sheet checks --offline skips are "
    "exactly the ones that gate enabling a live agent. Re-run on a machine with network. "
    "Run: {command}"
)

AGENT_NOT_ENABLED = (
    "{names} failed - the hourly sync agent was NOT enabled and nothing was loaded. Fix those "
    "[FAIL] lines above, then re-run: {command}"
)

AGENT_NOT_ENABLED_UNVERIFIED = (
    "the live Sheet could not be verified ({names} came back UNKNOWN, not PASS) - the hourly "
    "sync agent was NOT enabled and nothing was loaded. It would run `sync-metadata --live` "
    "unattended against a Sheet whose sharing and sync columns were never confirmed. Each "
    "UNKNOWN line above says why - most often no network, or Google briefly unavailable. "
    "Resolve that, then re-run: {command}"
)


def agent_not_enabled_message(
    blocking: list[str], unverified: list[str], install: deployment.InstallCommand
) -> str:
    """FAILs are named first: an UNKNOWN Sheet check is often only their
    consequence. A merely offline machine reaches the same reduced assurance
    --offline is refused for, so UNKNOWN on the two Sheet checks blocks too.
    This rule lives here, not in deployment.exit_code."""
    command = install.render(enable_agent=True)
    if not blocking:
        return AGENT_NOT_ENABLED_UNVERIFIED.format(names=" and ".join(unverified), command=command)
    message = AGENT_NOT_ENABLED.format(names=", ".join(blocking), command=command)
    if unverified:
        message += f" ({' and '.join(unverified)} came back UNKNOWN too, and must PASS as well.)"
    return message

AGENT_NOT_LOADED = (
    "the hourly sync agent was not loaded - see the message above. A plist written above still "
    "loads at this account's next login; `doctor --live` shows what is loaded now."
)

AGENT_ENABLED_DESPITE_FAILS = (
    "the hourly sync agent IS enabled: the [FAIL] lines above are for other work, not for it."
)


def enable_agent_refusal(args) -> str | None:
    """`--enable-agent` is the one flag that starts unattended live traffic, so
    it refuses rather than infers what the operator meant."""
    if not args.enable_agent:
        return None
    command = install_command_for(args).render(enable_agent=True)
    if not args.live:
        return ENABLE_AGENT_NEEDS_LIVE.format(command=command)
    if args.offline:
        return ENABLE_AGENT_NEEDS_NETWORK.format(command=command)
    return None


def load_sync_agent(args, announce: Callable[[str], None]) -> bool:
    """Write the plist and load the hourly agent, replacing an already-loaded
    one. True when launchd took it. Bootout first because launchd holds its own
    copy of the plist from bootstrap time, so a rewritten plist otherwise never
    takes effect."""
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    spec = launch_agent.sync_agent_spec(REPO_ROOT, config.project_id, args.registry)
    plist = launch_agent.plist_path(spec, Path.home())

    # RunAtLoad means bootstrapping starts a live sync immediately, so say so
    # before acting, not after.
    announce(f"loading {spec.label} for {platform_probe.current_user()}")
    announce("  this starts a live sync run now, and again at every login")

    # Written here and nowhere else: launchd loads every plist in LaunchAgents at login.
    try:
        announce(f"  {launch_agent.write_plist(spec, Path.home())}")
    except OSError as exc:
        announce(f"  could not write {plist} ({exc})")
        return False

    if platform_probe.launchctl_print(spec.label) is not None:
        announce(
            "  it is already loaded - unloading it first so the new plist takes effect; "
            "a sync running right now is stopped"
        )
        # Waited on even when bootout reports failure: it exits 36 ("in progress")
        # while a running job is still stopping. Only a job still listed is fatal.
        _unloaded, bootout_message = platform_probe.launchctl_bootout(spec.label)
        announce(f"  {bootout_message}")
        if not platform_probe.wait_until_unloaded(spec.label):
            announce(
                f"  {spec.label} was still registered "
                f"{platform_probe.UNLOAD_TIMEOUT_SECONDS:.0f}s after bootout - not loading over it"
            )
            return False

    loaded, bootstrap_message = platform_probe.launchctl_bootstrap(plist)
    announce(f"  {bootstrap_message}")
    return loaded


# What reading the registry can raise in `doctor` and `setup`.
# ValueError covers malformed JSON and a registry that is not UTF-8.
REGISTRY_READ_ERRORS = (ConfigError, ValueError, OSError)


def report_unreadable_registry(args, exc: Exception) -> int:
    # `doctor` and `setup` are the commands you talk someone through over the
    # phone; a traceback is the one output that helps nobody.
    print(f"could not read the project registry {args.registry}: {exc}", file=sys.stderr)
    return 1


def cmd_doctor(args) -> int:
    try:
        checks = build_deployment_checks(args, include_network=not args.offline)
    except REGISTRY_READ_ERRORS as exc:
        return report_unreadable_registry(args, exc)
    results = deployment.run_checks(checks)
    print(deployment.format_report(results))
    return deployment.exit_code(results)


def cmd_setup(args) -> int:
    refusal = enable_agent_refusal(args)
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

    changes: list[str] = []

    def announce(line: str) -> None:
        changes.append(line)
        print(line)

    try:
        checks = build_deployment_checks(args, include_network=not args.offline)
    except REGISTRY_READ_ERRORS as exc:
        return report_unreadable_registry(args, exc)
    results = deployment.converge(checks, announce)
    agent_failed = False

    if args.enable_agent:
        # "Verify first, then enable" is the whole reason --enable-agent is a
        # separate flag, so it consults the verification it just performed.
        blocking = deployment.agent_blocking_failures(results)
        unverified = deployment.unverified_sheet_checks(results)
        if blocking or unverified:
            print(deployment.format_report(results))
            print(
                agent_not_enabled_message(blocking, unverified, install_command_for(args)),
                file=sys.stderr,
            )
            return 1
        try:
            agent_failed = not load_sync_agent(args, announce)
        except REGISTRY_READ_ERRORS as exc:
            return report_unreadable_registry(args, exc)
        results = deployment.run_checks(checks)

    if not changes:
        if deployment.exit_code(results) == 0:
            print("nothing to change; this machine already matches the checkout.")
        else:
            print("nothing setup can change on its own; each [FAIL] below says what to do.")
    print(deployment.format_report(results))
    if agent_failed:
        print(AGENT_NOT_LOADED, file=sys.stderr)
        return 1
    if args.enable_agent and deployment.exit_code(results) != 0:
        print(AGENT_ENABLED_DESPITE_FAILS, file=sys.stderr)
    return deployment.exit_code(results)


def cmd_validate(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    live = bool(args.live)

    # Before any Sheet I/O, for the same reason `upload` reads --limit early:
    # a mistyped flag should not cost a full read and a full validation pass
    # first. The rest of --batch's checks need the Sheet and run below.
    try:
        if getattr(args, "batch", None) is not None:
            batch_column_for(config, args.batch, args.registry)
    except BatchScopeError as exc:
        print(exc, file=sys.stderr)
        return 1

    print(sheet_banner(config, live))
    print()

    try:
        sheet = read_sheet(args, registry, config, live, "validate")
        header_results, row_results = validate_sheet_content(sheet, args)
    except SheetSetupFailed:
        return 1

    column_map, rows = sheet.column_map, sheet.rows

    # `validate` previews what `upload` would do, so it must narrow to the
    # same rows through the same function. Rows and results are filtered as
    # PAIRS: format_lifecycle_summary requires one result per row in the same
    # order and checks the lengths, and the row numbers on the results are
    # still the Sheet's own, so a report still names the row an operator has
    # to go and edit.
    try:
        scope = resolve_batch_scope(args, config, column_map, rows)
    except BatchScopeError as exc:
        print(exc, file=sys.stderr)
        return 1
    if scope is not None:
        in_scope = [
            (row, result)
            for row, result in zip(rows, row_results)
            if result.row_number in scope
        ]
        rows = [row for row, _ in in_scope]
        row_results = [result for _, result in in_scope]

    results = header_results + row_results
    print(format_report(results))
    print()
    print(format_field_receipt(column_map))
    print()
    print(format_lifecycle_summary(rows, row_results))
    print()
    print("suggestions (advisory - nothing is changed automatically):")
    suggestions = suggest_standard_fields(column_map.uploadable_fields())
    if suggestions:
        for suggestion in suggestions:
            print(f"  '{suggestion.field_name}' -> '{suggestion.standard}': {suggestion.reason}")
    else:
        print("  (none)")

    return 0 if all(r.is_valid for r in results) else 1


class MissingWriteBackColumns(Exception):
    """The Sheet has no column for something `upload` must record. Checked
    once, before anything is uploaded, because a run that uploaded first and
    then discovered it had nowhere to record the identifier would leave items
    on Internet Archive the Sheet has no record of - the exact outcome the
    reserve-first ordering exists to prevent."""


@dataclass(frozen=True)
class SheetColumns:
    """Zero-based grid indexes of the four columns this tool writes."""

    ia_identifier: int
    ia_uploaded: int
    ia_url: int
    ia_identifier_bib: int

    def cell(self, column_index: int, row_number: int) -> str:
        return f"{column_letter(column_index)}{row_number}"


def locate_write_back_columns(column_map: ColumnMap) -> SheetColumns:
    """Every write-back column is required in ALL modes, including the default
    read-only one. A rehearsal that succeeds against a Sheet the real run would
    refuse is not a rehearsal, so the check does not vary with --live or
    --write-identifier."""
    indexes: dict[str, int] = {}
    for index, header in enumerate(column_map.headers):
        field_name = column_map.field_names[header]
        if field_name in WRITE_BACK_COLUMNS and field_name not in indexes:
            indexes[field_name] = index

    missing = [name for name in WRITE_BACK_COLUMNS if name not in indexes]
    if missing:
        raise MissingWriteBackColumns(
            f"the Sheet has no column(s) named {', '.join(missing)}. `upload` records what it "
            f"did in {', '.join(WRITE_BACK_COLUMNS)}; add them as header cells (any position, "
            "spelling exactly as shown) before uploading."
        )

    return SheetColumns(
        ia_identifier=indexes[IA_IDENTIFIER_COLUMN],
        ia_uploaded=indexes[IA_UPLOADED_COLUMN],
        ia_url=indexes[IA_URL_COLUMN],
        ia_identifier_bib=indexes[IA_IDENTIFIER_BIB_COLUMN],
    )


@dataclass(frozen=True)
class UploadTarget:
    """One row this run intends to upload.

    `identifier` is always the real, permanent one; `uploaded_as` is what
    actually goes over the wire, which is the same string only under --live.
    Both are kept because they answer different questions - the Sheet records
    the permanent identifier, while the URL has to point at the item that
    really exists."""

    row: dict[str, str]
    row_number: int
    identifier: str
    uploaded_as: str
    identifier_bib: str
    newly_minted: bool
    # What this row's file_template columns said when the run read the Sheet.
    # Re-checked against a fresh read before every write - see
    # split_moved_targets().
    source_fingerprint: str


def item_url(uploaded_as: str) -> str:
    return f"{ITEM_URL_PREFIX}{uploaded_as}"


def upload_timestamp() -> str:
    """Its own function so a test can pin it and assert a confirm batch as an
    exact ordered sequence rather than "a cell holding some string".

    Delegates to utc_timestamp() rather than formatting its own: this value
    lands in the Sheet's `ia_uploaded` column, which is the permanent record
    of when the item was published."""
    return utc_timestamp()


def reserve_updates(targets: list[UploadTarget], columns: SheetColumns) -> list[CellUpdate]:
    """Step 1 of the protocol: claim the minted numbers in the Sheet BEFORE
    anything is uploaded. A crash after this point leaves an unused gap in the
    sequence, which is harmless; a crash after uploading but before reserving
    would leave an item on Internet Archive the Sheet has no record of, and the
    next run's max+1 would mint that same number onto a different photograph -
    permanently.

    RESERVED rows are skipped: their identifier is already in the Sheet and
    rewriting it would be a no-op at best."""
    return [
        CellUpdate(columns.cell(columns.ia_identifier, target.row_number), target.identifier)
        for target in targets
        if target.newly_minted
    ]


def confirm_updates(
    targets: list[UploadTarget], columns: SheetColumns, uploaded_at: str
) -> list[CellUpdate]:
    """Step 3: record what actually happened. `ia_uploaded` is what turns a
    row DONE for every later run, so it is written only for rows whose upload
    genuinely succeeded.

    ia_identifier_bib carries the RESOLVED path, which routinely differs from
    the filename the Sheet holds (225 of 234 real rows carry no extension) -
    it records what was uploaded, not what someone typed."""
    updates: list[CellUpdate] = []
    for target in targets:
        updates.append(CellUpdate(columns.cell(columns.ia_uploaded, target.row_number), uploaded_at))
        updates.append(
            CellUpdate(columns.cell(columns.ia_url, target.row_number), item_url(target.uploaded_as))
        )
        updates.append(
            CellUpdate(
                columns.cell(columns.ia_identifier_bib, target.row_number), target.identifier_bib
            )
        )
    return updates


def cell_value(grid: list[list[str]], row_number: int, column_index: int) -> str:
    """A missing row or a short row reads as "" rather than raising - the
    Sheets API omits trailing empty cells, so a genuinely blank cell and a cell
    past the end of a row are the same thing."""
    index = row_number - 1
    if index < 0 or index >= len(grid):
        return ""
    row = grid[index]
    return (row[column_index] if column_index < len(row) else "").strip()


def sheet_row_fingerprints(
    rows: list[dict[str, str]], file_template: str
) -> dict[int, str]:
    """row_number -> the row's `file_template` candidate, as a fingerprint for
    "is this still the same row?".

    The template's columns are the right fingerprint for one specific reason:
    `upload` never writes them. Checking `ia_identifier` instead would be
    tautological on the reserve->confirm leg, because reserve is what put that
    value there - the check would be verifying its own write.

    `reconcile-files` is the one command that DOES write a file_template
    column (the filename cell, and only that one). It never runs inside an
    upload run, so it cannot make this check verify its own write; a
    reconciliation landing in the Sheet mid-upload instead makes that row
    fingerprint as moved, so `upload` skips it and it goes out on the next
    run - the safe direction, and the same outcome as any other human edit
    to the same cell.

    Must be computed from the RAW cells, before resolve_sheet_files() rewrites
    row['file'] to the resolved name: the comparison is against a fresh read of
    the Sheet, which has raw cells in it, and comparing a resolved name to a
    raw one would report every row as moved.

    A row whose template columns have vanished fingerprints as "" and can
    therefore never match, which is the safe direction."""
    fingerprints: dict[int, str] = {}
    for offset, row in enumerate(rows):
        try:
            fingerprints[offset + 2] = candidate_path(file_template, row)
        except (KeyError, IndexError):
            fingerprints[offset + 2] = ""
    return fingerprints


@dataclass(frozen=True)
class SheetSnapshot:
    """A fresh read of the Sheet, reduced to what the mid-run-edit guard needs:
    where the write-back columns are now, what each row's fingerprint is now,
    the grid itself for reading `ia_identifier` back, and every identifier the
    Sheet currently holds ANYWHERE - see claimed_identifiers."""

    columns: SheetColumns
    # Kept so sync-metadata can re-locate its own two columns in this same
    # read rather than parsing the grid a second time. upload compares
    # SheetColumns instead, which read_sheet_snapshot already derives.
    column_map: ColumnMap
    grid: list[list[str]]
    fingerprints: dict[int, str]
    # Every non-blank `ia_identifier` in the Sheet right now, whatever row it
    # is on. split_moved_targets only inspects a target's OWN row, which
    # cannot see a number claimed on a DIFFERENT row since this run read the
    # Sheet - and that is the case that mints a duplicate. See
    # check_claimed_identifiers().
    claimed_identifiers: frozenset[str]


class SheetReader(Protocol):
    """The one SheetClient method read_sheet_snapshot() needs."""

    def read_grid(self) -> list[list[str]]: ...


def read_sheet_snapshot(client: SheetReader, file_template: str) -> SheetSnapshot:
    grid = client.read_grid()
    column_map, rows = grid_to_rows(grid)
    return SheetSnapshot(
        columns=locate_write_back_columns(column_map),
        column_map=column_map,
        grid=grid,
        fingerprints=sheet_row_fingerprints(rows, file_template),
        claimed_identifiers=frozenset(
            identifier
            for row in rows
            if (identifier := (row.get(IA_IDENTIFIER_COLUMN) or "").strip())
        ),
    )


def check_claimed_identifiers(
    targets: list[UploadTarget], snapshot: SheetSnapshot
) -> str | None:
    """Returns a stop reason if any number this run minted has been claimed in
    the Sheet since the run read it, or None.

    plan_upload_targets mints the whole run's numbers up front from a single
    read, as max+1, max+2, ... That read can be hours old by the time the last
    chunk reserves. split_moved_targets checks each target's own row, so it
    catches "someone else took THIS row" - but a number written to a row this
    run is not targeting is invisible to it, and that is precisely the case
    that mints a duplicate: two Sheet rows carrying one permanent identifier,
    with internetarchive.upload() APPENDING files to the existing item rather
    than refusing, so two photographs end up in one unrenameable item.

    Stops the whole run rather than dropping the offending target. Every
    number this run holds came out of the same max+1 arithmetic over the same
    stale read, so one collision means the max was wrong and the rest are
    suspect too - dropping one and proceeding with its neighbours would be
    reserving numbers that are wrong for the same reason. Nothing has been
    reserved or uploaded at that point, so a rerun re-reads, re-mints from the
    real maximum, and proceeds.

    Only newly-minted targets are checked. A RESERVED row's identifier is
    already in the Sheet by definition - that is what RESERVED means - so
    including it here would stop every retry run on its own reservation."""
    collisions = sorted(
        target.identifier
        for target in targets
        if target.newly_minted and target.identifier in snapshot.claimed_identifiers
    )
    if not collisions:
        return None
    return (
        f"identifier(s) {', '.join(collisions)} were claimed in the Sheet after this run read "
        "it, so the numbers this run minted are no longer free. Nothing has been reserved or "
        "uploaded. Rerun to mint from the Sheet's current state"
    )


class MovedRowCandidate(Protocol):
    """The four attributes split_moved_targets() actually reads off a target.

    UploadTarget (reserve->confirm) and SyncTarget (sync-metadata's stamp
    write) both satisfy this by shape, not by inheritance - the guard was
    written once, against upload's leg, and SyncTarget reuses it verbatim
    (see its own docstring for why that reuse is sound). A nominal base
    class would have forced one of the two unrelated dataclasses to inherit
    from the other just to share four field names.

    Declared as read-only properties, not plain attributes: a plain
    attribute in a Protocol demands a setter as well as a getter, which a
    frozen dataclass - both UploadTarget and SyncTarget are frozen - can
    never offer. Reading is all this function ever does."""

    @property
    def row_number(self) -> int: ...
    @property
    def identifier(self) -> str: ...
    @property
    def newly_minted(self) -> bool: ...
    @property
    def source_fingerprint(self) -> str: ...


MovedRowCandidateT = TypeVar("MovedRowCandidateT", bound=MovedRowCandidate)


def split_moved_targets(
    targets: Sequence[MovedRowCandidateT], snapshot: SheetSnapshot, reserved_already: bool
) -> tuple[list[MovedRowCandidateT], list[MovedRowCandidateT]]:
    """Returns (still_at_their_row, moved).

    Generic over the caller's target type (bound to MovedRowCandidate above)
    so a caller passing list[SyncTarget] gets list[SyncTarget] back, rather
    than the list[UploadTarget] a non-generic signature would claim - the
    previous annotation was honest about upload's own call site and false
    about sync-metadata's.

    `targets` is typed as a Sequence, not a list: it is only ever read here
    (iterated once), and list's invariance would otherwise refuse a caller's
    list[UploadTarget] or list[SyncTarget] against a bare list[T] parameter -
    Sequence's covariance is what lets T solve to the caller's real type.

    Row numbers are positional. A human inserting or deleting a row shifts
    every row below it, and the run holds row numbers from a read that may be
    hours old on a full-collection run - the initial read fixes them, and the
    last chunk's reserve write uses them. So this check runs before BOTH
    writes, not just before the confirm.

    Two things must agree. The fingerprint (the file_template columns, which
    this tool never writes) says the row still describes the same photograph.
    `ia_identifier` says nobody else has claimed it: blank for a row about to
    be reserved, and equal to ours once reserved. Checking the identifier alone
    would be tautological after reserve; checking the fingerprint alone would
    miss a row someone else assigned a number to in the meantime.

    And the fingerprint only proves anything while exactly one row in the
    fresh read carries it (issue #1). With two rows resolving to one file, a
    shift leaves a MATCHING fingerprint at the target's position with a
    different physical row underneath, and the write lands on the wrong row -
    misattributing the item and leaving the planned row to be minted again
    next run. resolve_sheet_files() refuses duplicates present at the initial
    read, so a duplicated fingerprint here means one appeared mid-run; the
    target is filed as moved, the safe direction, and goes out on a rerun
    once the Sheet is untangled.

    That ambiguity check runs on the RESERVE leg only. After reserve, this
    run's own number is in the target's row and check_claimed_identifiers has
    already proved it unique across the whole Sheet, so the identifier
    comparison below is a complete proof of identity by itself: a shift puts a
    row that does NOT carry our number underneath. Vetoing a duplicated
    fingerprint there would withhold the confirm write for an edit that cannot
    have moved anything - an appended duplicate row shifts nothing - and the
    cost of that false positive is the worst outcome this tool has short of a
    wrong write: an item live on Internet Archive with no record in the
    Sheet."""
    duplicated: set[str] = set()
    if not reserved_already:
        seen: set[str] = set()
        for fingerprint in snapshot.fingerprints.values():
            if fingerprint in seen:
                duplicated.add(fingerprint)
            seen.add(fingerprint)

    still_there: list[MovedRowCandidateT] = []
    moved: list[MovedRowCandidateT] = []
    for target in targets:
        expected_identifier = (
            target.identifier if reserved_already or not target.newly_minted else ""
        )
        fingerprint_now = snapshot.fingerprints.get(target.row_number, "")
        matches = (
            bool(fingerprint_now)
            and fingerprint_now not in duplicated
            and fingerprint_now == target.source_fingerprint
            and cell_value(snapshot.grid, target.row_number, snapshot.columns.ia_identifier)
            == expected_identifier
        )
        if matches:
            still_there.append(target)
        else:
            moved.append(target)
    return still_there, moved


def sheet_metadata_fields(column_map: ColumnMap) -> frozenset[str]:
    """The normalized column names whose values this tool sends to Internet
    Archive as item metadata.

    One definition, shared by `upload` and `sync-metadata`, so the two cannot
    disagree about what a row means - a column that uploads but does not sync
    (or the reverse) would leave the Sheet and the item permanently out of
    step in a way neither command reports.

    Subtracts PIPELINE_OWNED_FIELDS as well as DROPPED_BY_UPLOAD_ROW.
    `mediatype` and `collection` are generated, and upload overwrites them
    anyway, so excluding them here changes nothing for upload - but for sync
    it matters twice over: Internet Archive will not change an item's
    mediatype after upload, and `collection` is membership, not metadata."""
    return (
        frozenset(column_map.uploadable_fields())
        - DROPPED_BY_UPLOAD_ROW
        - PIPELINE_OWNED_FIELDS
    )


def identifier_from_url(url: str) -> str | None:
    """The item identifier out of an `ia_url` cell, or None if the cell is not
    one of this tool's own URLs.

    `ia_url` is what upload's confirm write recorded, so in test mode it
    already carries THAT run's stamp; the Sheet is its own record of what
    landed where - no upload log is read back. Returns None rather than
    guessing at an unrecognised cell - a human having pasted something is far
    likelier than the URL prefix having changed."""
    url = url.strip()
    if not url.startswith(ITEM_URL_PREFIX):
        return None
    return url[len(ITEM_URL_PREFIX):].strip("/") or None


def item_project_id(uploaded_as: str, live: bool) -> str | None:
    """The PROJECTID of the item `uploaded_as` names, or None if it cannot be
    read as one of this tool's identifiers.

    A test item is `zztest-<stamp>-<identifier>` (see effective_identifier),
    so the real identifier is whatever follows the stamp - the stamp itself is
    dropped rather than parsed, since it is only ever the run's timestamp."""
    real = uploaded_as.strip()
    if not live and real.startswith(TEST_IDENTIFIER_PREFIX):
        _stamp, _sep, real = real[len(TEST_IDENTIFIER_PREFIX):].partition("-")
    parsed = parse_identifier(real)
    return parsed[1] if parsed else None


def sheet_upload_metadata(
    target: UploadTarget, uploadable: frozenset[str], mediatype: str
) -> dict[str, str]:
    """The row dict handed to upload_row.

    upload_row turns every key it is given (bar `identifier` and `file`) into
    an Internet Archive metadata field, and IA metadata is permanent - so the
    tool's own bookkeeping columns and anything a Sheet author marked (LCPS
    Internal) have to be filtered out HERE, before upload_row ever sees them.
    ColumnMap.uploadable_fields() is the single definition of what may be
    uploaded and already excludes both. It is passed in already computed
    (see SheetUploadRun.uploadable) rather than derived here: the column map
    is fixed for the whole run, and rebuilding the set per row made a
    10,000-row upload rebuild it 10,000 times.

    `identifier-bib` and `mediatype` are generated rather than read from a
    column - see docs/DECISIONS.md, "`identifier-bib` and `mediatype` are
    generated, not columns". The surviving test item
    zztest-lcps-sarahsoldphotos-00005 carries a permanently misspelled
    `indentifier-bib` because a header typo shipped once; a generated field
    name cannot do that."""
    metadata_row = {key: value for key, value in target.row.items() if key in uploadable}
    metadata_row["mediatype"] = mediatype
    metadata_row["identifier-bib"] = target.identifier_bib
    metadata_row["file"] = target.row["file"]
    return metadata_row


def plan_upload_targets(
    rows: list[dict[str, str]],
    row_results: list[RowValidation],
    config: ProjectConfig,
    live: bool,
    fingerprints: dict[int, str],
    stamp: str,
    scope: set[int] | None = None,
) -> list[UploadTarget]:
    """Decides what this run will upload and under which identifier.

    Numbers are minted for the whole run up front, before any chunk is
    reserved: minting is pure arithmetic with no side effect, and doing it once
    means a later chunk cannot re-mint an earlier chunk's numbers by reading a
    Sheet that has not been written yet.

    `existing` deliberately spans EVERY row, including rows that failed
    validation and rows already DONE - a number that appears anywhere in the
    Sheet is spent, whatever the state of the row holding it.

    `stamp` is computed once by the caller (run_stamp(), called once per
    upload_from_sheet() invocation) so every target this run plans - across
    every chunk SheetUploadRun.execute() later processes - shares one stamp.

    `scope` is --batch's row numbers, or None for an unscoped run. It narrows
    which rows become targets but deliberately NOT which rows `existing`
    scans: a number spent by any row in the Sheet is spent, whatever batch
    that row belongs to, and a scoped run that only looked at its own rows
    would mint another batch's numbers a second time. Filtering here rather
    than slicing the returned list is what keeps a batch's numbers
    contiguous - next_identifiers() takes max+1 and never refills, so a
    number minted for an out-of-scope row and then discarded would leave a
    permanent gap."""
    if len(rows) != len(row_results):
        raise ValueError(
            f"plan_upload_targets: got {len(rows)} row(s) but {len(row_results)} row_results - "
            "they must be the same length, in the same order."
        )

    existing = [row.get(IA_IDENTIFIER_COLUMN) or "" for row in rows]

    pending: list[tuple[int, dict[str, str], RowState]] = []
    for offset, (row, result) in enumerate(zip(rows, row_results)):
        # Not is_valid alone: an uncatalogued row is valid but NOT_READY, and uploading it
        # mints a permanent identifier with no title and a blank `file` (see upload_row).
        if result.verdict is not UploadVerdict.READY:
            continue
        if scope is not None and offset + 2 not in scope:
            continue
        state = classify_row(row)
        if state is RowState.DONE:
            continue
        pending.append((offset + 2, row, state))

    unassigned_count = sum(1 for _, _, state in pending if state is RowState.UNASSIGNED)
    minted = iter(
        next_identifiers(existing, config.collection_key, config.project_id, unassigned_count)
    )

    targets: list[UploadTarget] = []
    for row_number, row, state in pending:
        newly_minted = state is RowState.UNASSIGNED
        identifier = next(minted) if newly_minted else (row.get(IA_IDENTIFIER_COLUMN) or "").strip()
        targets.append(
            UploadTarget(
                row=row,
                row_number=row_number,
                identifier=identifier,
                uploaded_as=effective_identifier(identifier, live, stamp),
                identifier_bib=(row.get(IA_IDENTIFIER_BIB_COLUMN) or "").strip(),
                newly_minted=newly_minted,
                source_fingerprint=fingerprints.get(row_number, ""),
            )
        )
    return targets


def write_cells_if_any(client: SheetClient, updates: list[CellUpdate]) -> None:
    """An empty batch is not sent at all. SheetClient.write_cells already
    returns early on an empty list, but the call still shows up in any record
    of what this run did to the Sheet - and "this run issued exactly these
    writes, in this order" is the property the protocol is asserted on."""
    if updates:
        client.write_cells(updates)


@dataclass(frozen=True)
class VerifyOutcome:
    """The result of re-reading the Sheet before a write.

    `stop_reason` distinguishes "these particular rows moved" (None - carry on
    with the rest) from "something happened to the whole Sheet" (a message -
    stop the run). The second case cannot be expressed as a per-row verdict: a
    failed read or a shifted column is equally true of every remaining chunk,
    so continuing would re-read and re-report the entire Sheet on the way to
    the same conclusion."""

    ok: list[UploadTarget]
    moved: list[UploadTarget]
    stop_reason: str | None


@dataclass(frozen=True)
class SheetUploadRun:
    """Everything the reserve -> upload -> confirm loop needs that does not
    change from row to row."""

    client: SheetClient
    columns: SheetColumns
    column_map: ColumnMap
    mediatype: str
    file_template: str
    files_dir: str
    collection: str
    live: bool
    write_back: bool
    log_path: Path
    # Task 12: overridable per run via --chunk-size (upload_from_sheet reads
    # the module-level CHUNK_SIZE itself when the flag is absent, so this
    # still defaults to whatever CHUNK_SIZE is at call time - including a
    # test's own monkeypatched value).
    chunk_size: int = CHUNK_SIZE

    @functools.cached_property
    def uploadable(self) -> frozenset[str]:
        """Which normalized field names may be sent as IA metadata.

        Computed once per run, not once per row. The column map is fixed for
        the whole run - it is a field on this dataclass - so deriving this
        inside sheet_upload_metadata() meant a 10,000-row upload rebuilding
        the same set 10,000 times. cached_property works on a frozen
        dataclass because it writes through __dict__ rather than
        __setattr__."""
        return sheet_metadata_fields(self.column_map)

    def execute(self, targets: list[UploadTarget]) -> UploadSummary:
        """One chunk at a time: verify, reserve, upload, verify, confirm,
        having logged each row's outcome as it happened.

        Chunking is what keeps this inside the Sheets API's 60 writes per
        minute per user - a batch counts as one request, so ~10,000 rows cost
        about 40 requests instead of 10,000. It is also why the guard has to
        run per chunk: the last chunk's reserve write can be hours after the
        read that fixed its row numbers.

        `self.chunk_size` is read here rather than taken as chunk_rows'
        default so the chunk boundary is reachable in a test - a protocol
        that is only ever exercised with a single chunk is a protocol nobody
        has tested. It defaults to CHUNK_SIZE (see the field above) and is
        overridable per run via --chunk-size.

        A rate-limited row (is_rate_limit_error() matches its exception)
        stops the run after finishing this chunk's confirm write, rather
        than being logged as an ordinary failure and moving on to the next
        target. Every row this run already
        uploaded successfully, in this chunk or an earlier one, is still
        confirmed before returning: a rate limit must not leave a row
        RESERVED-but-unconfirmed, which would make the next run re-upload
        it under a second identifier."""
        # Counted and collected in the same step: every site that bumps a
        # number here already holds the target it belongs to, so the summary's
        # lists cost nothing beyond remembering what was in hand.
        tally = {"success": 0, "not_attempted": 0}
        failures: list[RowFailure] = []
        unconfirmed: list[RowFailure] = []
        skipped: list[RowFailure] = []
        # Set only once the run has stopped on it, not when a row first hits it.
        run_stopped_on_status: int | None = None

        def summary() -> UploadSummary:
            return UploadSummary(
                succeeded=tally["success"],
                failures=tuple(failures),
                unconfirmed=tuple(unconfirmed),
                not_attempted=tally["not_attempted"],
                rate_limit_status=run_stopped_on_status,
                skipped=tuple(skipped),
            )

        total = len(targets)
        position = 0
        # Targets that already have a verdict of any kind - uploaded, failed, or
        # reported as moved. `total - settled` is therefore exactly what a
        # run-stopping problem leaves unattempted, with nothing counted twice.
        settled = 0

        for chunk in chunk_rows(targets, self.chunk_size):
            # Every chunk gets a fresh timestamp. One timestamp for the whole
            # run would stamp chunk 20 with the time chunk 1 started, which on
            # a full-collection run is hours wrong.
            uploaded_at = upload_timestamp()
            working = chunk

            if self.write_back:
                outcome = self._verify(chunk, reserved_already=False)
                for target in outcome.moved:
                    tally["not_attempted"] += 1
                    settled += 1
                    # Nothing was sent for this row, which is what `skipped`
                    # means - the same word its per-row log record already uses.
                    skipped.append(
                        RowFailure(
                            identifier=target.identifier,
                            error=self._report_moved(
                                target, uploaded=False, cause=outcome.stop_reason
                            ),
                        )
                    )
                if outcome.stop_reason is not None or not self._write(
                    reserve_updates(outcome.ok, self.columns), "reserve"
                ):
                    tally["not_attempted"] += total - settled
                    return summary()
                working = outcome.ok

            succeeded: list[UploadTarget] = []
            rate_limit_status: int | None = None
            for target in working:
                position += 1
                settled += 1
                print(f"[{position}/{total}] uploading {target.uploaded_as} ({target.row['file']})")
                try:
                    upload_row(
                        sheet_upload_metadata(target, self.uploadable, self.mediatype),
                        target.uploaded_as,
                        self.collection,
                        self.files_dir,
                    )
                except Exception as exc:
                    # A rate limit is logged as an ordinary failure - the
                    # message is the server's either way - and additionally
                    # ends the run after this chunk's confirm write.
                    failures.append(RowFailure(identifier=target.identifier, error=str(exc)))
                    print(f"    - {format_row_error(exc)}")
                    self._log(target, "failure", error=str(exc), http_status=parsed_status_code(exc))
                    if is_rate_limit_error(exc):
                        rate_limit_status = parsed_status_code(exc)
                        break
                    continue
                tally["success"] += 1
                self._log(target, "success")
                succeeded.append(target)

            if self.write_back and succeeded:
                outcome = self._verify(succeeded, reserved_already=True)
                for target in outcome.moved:
                    unconfirmed.append(
                        RowFailure(
                            identifier=target.identifier,
                            error=self._report_moved(
                                target, uploaded=True, cause=outcome.stop_reason
                            ),
                        )
                    )
                if outcome.stop_reason is not None:
                    tally["not_attempted"] += total - settled
                    return summary()
                if not self._write(confirm_updates(outcome.ok, self.columns, uploaded_at), "confirm"):
                    for target in outcome.ok:
                        unconfirmed.append(
                            RowFailure(
                                identifier=target.identifier,
                                error="the Sheet write failed",
                            )
                        )
                        self._log(target, "unconfirmed", error="the Sheet write failed")
                    tally["not_attempted"] += total - settled
                    return summary()

            if rate_limit_status is not None:
                attempted = tally["success"] + len(failures)
                tally["not_attempted"] += total - settled
                run_stopped_on_status = rate_limit_status
                print(
                    f"stopped: Internet Archive asked us to slow down (HTTP {rate_limit_status}) "
                    f"after {_pluralize(attempted, 'item')}",
                    file=sys.stderr,
                )
                # The status alone cannot say which limit fired, so name both.
                print(
                    f"{tally['success']} uploaded this run - re-run later to resume: minutes to "
                    "hours if IA's queue is busy, tomorrow if today's 5,000 cap was reached"
                )
                return summary()

        return summary()

    def _verify(self, targets: list[UploadTarget], reserved_already: bool) -> VerifyOutcome:
        """Re-reads the Sheet and splits the targets into those still at the
        row this run planned for them and those that have moved. Runs before
        BOTH writes - see split_moved_targets for why the fingerprint, and not
        `ia_identifier`, is what makes the check meaningful.

        This step added two Sheets READS per chunk - roughly 80 across a
        full-collection run spanning hours - so one transient 503 among them is
        likely rather than exotic, and it must not end a run that has already
        created thousands of permanent Internet Archive items with a stack
        trace. Both failure modes below stop the run cleanly instead: the
        caller still prints the summary and the log path, and every affected
        row is logged."""
        try:
            snapshot = read_sheet_snapshot(self.client, self.file_template)
        except MissingWriteBackColumns as exc:
            return VerifyOutcome([], list(targets), f"a column this run writes to is gone: {exc}")
        except Exception as exc:
            return VerifyOutcome([], list(targets), f"the Sheet could not be re-read: {exc}")

        if snapshot.columns != self.columns:
            # A column was inserted, deleted or renamed. Every cached column
            # index is now wrong, so every write this run could make would land
            # in the wrong column - which is true for every remaining chunk,
            # not just this one, hence a stop_reason rather than a per-row
            # verdict that would re-read and re-report the whole Sheet 20 more
            # times on the way to the same conclusion.
            return VerifyOutcome(
                [],
                list(targets),
                "the Sheet's columns moved while this run was in progress, so every cell it "
                "would write now lands in the wrong column",
            )

        if not reserved_already:
            # Only on the reserve leg. After reserve, this run's own numbers
            # ARE in the Sheet - checking then would flag every one of them.
            collision = check_claimed_identifiers(targets, snapshot)
            if collision is not None:
                return VerifyOutcome([], list(targets), collision)

        ok, moved = split_moved_targets(targets, snapshot, reserved_already)
        return VerifyOutcome(ok, moved, None)

    def _report_moved(
        self, target: UploadTarget, uploaded: bool, cause: str | None = None
    ) -> str:
        """`cause` names a whole-Sheet problem (a failed read, a moved column)
        when there is one. Without it this said "row N is no longer the row
        this run planned for" for a COLUMN change too, which is wrong in kind
        and sends the operator to look at the wrong thing.

        The filename is on screen, not just in the log, because for a row that
        was never uploaded the identifier exists nowhere yet and the row number
        is precisely what has gone stale - `file` is the only durable handle
        the operator has left."""
        what_changed = cause or (
            f"row {target.row_number} is no longer the row this run planned for "
            f"'{target.identifier}' - the Sheet was edited while the run was in progress, so "
            "writing there would land on a different photograph"
        )
        outcome = (
            f"The item IS on Internet Archive as '{target.uploaded_as}' but is NOT recorded in "
            "the Sheet."
            if uploaded
            else "Nothing was uploaded for it."
        )
        message = (
            f"{what_changed}. {outcome} File: '{target.row['file']}' (planned row "
            f"{target.row_number}, identifier '{target.identifier}'). Rerun once the Sheet has "
            "settled."
        )
        print(message, file=sys.stderr)
        self._log(target, "unconfirmed" if uploaded else "skipped", error=message)
        # Returned so the run summary can carry the same sentence the log row
        # carries. Built once here, where the distinction between an uploaded
        # and a never-uploaded moved row is already drawn.
        return message

    def _write(self, updates: list[CellUpdate], step: str) -> bool:
        """A Sheets write failing (a 503, an expired token, a revoked share) is
        an ordinary operational event, not a reason to hand the operator a
        traceback in place of the run summary and the log path."""
        try:
            write_cells_if_any(self.client, updates)
        except Exception as exc:
            print(
                f"the Sheet {step} write failed: {exc}. Stopping here rather than uploading more "
                "items this run cannot record. Nothing is lost - rerun once the Sheet is "
                f"reachable and shared, as Editor, with {sheet_sharing_target()}, and every "
                "unrecorded row is picked up from where it stopped.",
                file=sys.stderr,
            )
            return False
        return True

    def _log(
        self,
        target: UploadTarget,
        status: str,
        error: str | None = None,
        http_status: int | None = None,
    ) -> None:
        log_result(
            self.log_path,
            target.identifier,
            target.row["file"],
            status,
            self.live,
            error=error,
            uploaded_as=target.uploaded_as,
            http_status=http_status,
        )


REMOVE_TAG_SENTINEL = "REMOVE_TAG"
# How much of a field value a dry run prints before eliding. Long enough for a
# typo to be visible in place, short enough that one changed field stays one
# readable pair of lines.
DRY_RUN_VALUE_WIDTH = 96
# Shared text kept before the first difference when a pair is shown from partway in.
DRY_RUN_DIFFERENCE_CONTEXT = 20


@dataclass(frozen=True)
class FieldChange:
    """One field a sync would alter, as the dry run prints it."""

    field_name: str
    now: str
    new: str
    # Why it is still a change when `now` and `new` read alike; None otherwise.
    note: str | None = None


def fetch_current_metadata(identifier: str) -> dict | None:
    """The metadata Internet Archive currently holds for an item, or None if
    it could not be read.

    Its own function so a test can pin it, and so the dry run's one network
    dependency is in a single place. Returns None rather than raising: a dry
    run that cannot reach one item should still report the other 9,999.
    """
    try:
        return dict(
            internetarchive.get_item(
                identifier, http_adapter_kwargs=IA_HTTP_ADAPTER_KWARGS
            ).metadata
        )
    except Exception:
        return None


def _display_text(value: object) -> str:
    """IA returns a list for a field that occurs more than once. Line breaks are
    escaped so a value keeps to its line; accents are composed and invisible
    characters dropped, since neither shows on screen."""
    text = "; ".join(str(part) for part in value) if isinstance(value, list) else str(value)
    visible = "".join(
        char for char in unicodedata.normalize("NFC", text) if unicodedata.category(char) != "Cf"
    )
    return visible.replace("\r", "\\r").replace("\n", "\\n")


def _render(value: object) -> str:
    return _elide(_display_text(value), DRY_RUN_VALUE_WIDTH)


def _first_visible_difference(current_text: str, new_text: str) -> tuple[int, int] | None:
    """Where each text first reads differently, any run of whitespace reading
    as one space; None when the two read alike."""
    current_words = list(re.finditer(r"\S+", current_text))
    new_words = list(re.finditer(r"\S+", new_text))
    current_end = new_end = 0
    for current_word, new_word in zip(current_words, new_words):
        if current_word.group() != new_word.group():
            shared = len(os.path.commonprefix([current_word.group(), new_word.group()]))
            return current_word.start() + shared, new_word.start() + shared
        current_end, new_end = current_word.end(), new_word.end()
    if len(current_words) == len(new_words):
        return None
    return current_end, new_end


def _hidden_by_cutoff(text: str, position: int) -> bool:
    return len(text) > DRY_RUN_VALUE_WIDTH and position >= DRY_RUN_VALUE_WIDTH - 3


def _window(text: str, position: int) -> str:
    start = max(0, position - DRY_RUN_DIFFERENCE_CONTEXT)
    return _elide(("..." if start else "") + text[start:], DRY_RUN_VALUE_WIDTH)


def _why_it_reads_unchanged(current: object, new: str) -> str | None:
    """None when the two visibly differ. ASCII, for the same reason as _elide."""
    current_text, new_text = _display_text(current), _display_text(new)
    if _first_visible_difference(current_text, new_text) is not None:
        return None
    if isinstance(current, list) and len(current) > 1:
        return (
            f"(Internet Archive holds {_pluralize(len(current), 'separate value')}; "
            "the sync replaces them with one)"
        )
    if current_text != new_text:
        return "(the two differ only in spaces)"
    return "(the two differ in a way that does not show on screen)"


def _render_change(field_name: str, current: object, new: str) -> FieldChange:
    """When the cutoff would hide where the two first read differently, both
    are shown from just before that point instead of from their start."""
    current_shown, new_shown = _render(current), _render(new)
    current_text, new_text = _display_text(current), _display_text(new)
    difference = _first_visible_difference(current_text, new_text)
    if difference is not None:
        current_at, new_at = difference
        if _hidden_by_cutoff(current_text, current_at) or _hidden_by_cutoff(new_text, new_at):
            current_shown, new_shown = _window(current_text, current_at), _window(new_text, new_at)
    return FieldChange(field_name, current_shown, new_shown, _why_it_reads_unchanged(current, new))


def metadata_changes(sheet_metadata: dict[str, str], remote: dict) -> list[FieldChange]:
    """What IA holds now and what the Sheet would make it, for the fields a
    sync would actually alter.

    Mirrors update_metadata_row's rules exactly, because a dry run that
    predicts something other than what the real run does is worse than no dry
    run. A blank cell is dropped there, so it means "leave this field alone"
    and is not a change here. REMOVE_TAG deletes there, so it shows as a
    deletion here - and only when the field actually exists on the item, since
    removing what is not present changes nothing.

    Raw values are compared, never their rendered text: two long values that
    differ only past the display cutoff are still a change, and a string
    against IA's list is one too, since the real run replaces the list."""
    changes: list[FieldChange] = []
    for field_name, value in sorted(metadata_to_send(sheet_metadata).items()):
        current = remote.get(field_name)
        if value == REMOVE_TAG_SENTINEL:
            if current is not None:
                changes.append(FieldChange(field_name, _render(current), "(deleted)"))
            continue
        if current is None:
            changes.append(FieldChange(field_name, "(not set)", _render(value)))
        elif current != value:
            changes.append(_render_change(field_name, current, value))
    return changes


def print_sync_dry_run(
    to_push: list[SyncTarget],
    already_synced: list[SyncTarget],
    problems: list[RowValidation],
) -> int:
    """Shows what a sync would CHANGE, not merely which fields it would send.

    The hash gate is reported FIRST, and only the rows that would actually be
    sent are read back from Internet Archive. That is both the honest preview
    - a dry run must mirror the write, including what it leaves untouched -
    and a large saving: on a steady-state Sheet this drops from one read per
    uploaded row to none.

    Nothing is stamped here. A dry run sends nothing, so there is nothing to
    record having sent; stamping would make the next real run skip rows this
    one only previewed."""
    total = len(to_push) + len(already_synced)
    # A blank hash means never stamped or deliberately cleared, not edited.
    never_stamped = sum(1 for target in to_push if not target.stored_hash)
    edited = len(to_push) - never_stamped
    # {:,} on the raw counts, not just on the _pluralize call - see that
    # function's docstring: adjacent numbers on one line must agree about how
    # a number looks.
    print(
        f"{_pluralize(total, 'uploaded row')}; {never_stamped:,} with no push on record, "
        f"{edited:,} changed since their last push, {len(already_synced):,} already in sync "
        "and would not be sent"
    )
    if not to_push:
        return 1 if problems else 0

    print(f"reading current metadata for {_pluralize(len(to_push), 'item')}...")
    print()

    changed = 0
    unreadable = 0
    for target in to_push:
        remote = fetch_current_metadata(target.uploaded_as)
        if remote is None:
            unreadable += 1
            print(
                f"  row {target.row_number}: {target.uploaded_as} - could not read its "
                "current metadata, so what would change is unknown"
            )
            continue

        changes = metadata_changes(target.metadata, remote)
        if not changes:
            continue

        changed += 1
        print(f"  row {target.row_number}: {target.uploaded_as}")
        for change in changes:
            print(f"      {change.field_name}")
            print(f"          now: {change.now}")
            print(f"          new: {change.new}")
            if change.note:
                print(f"          {change.note}")

    if changed or unreadable:
        print()
    unchanged = len(to_push) - changed - unreadable
    # {:,} on both raw counts, not just on the _pluralize call - see that
    # function's docstring: adjacent numbers on one line must agree about how
    # a number looks.
    print(
        f"{changed:,} of {_pluralize(len(to_push), 'item')} would change; "
        f"{unchanged:,} already match and would be reported as unchanged"
    )
    if unreadable:
        print(f"{_pluralize(unreadable, 'item')} could not be read")
    return 1 if problems else 0


def print_dry_run(
    targets: list[UploadTarget], columns: SheetColumns, write_back: bool, uploaded_at: str
) -> None:
    if not targets:
        print("nothing to upload")
        return

    print(f"would upload {_pluralize(len(targets), 'item')}:")
    for target in targets:
        if target.newly_minted:
            print(
                f"  row {target.row_number}: would mint '{target.identifier}' and upload it as "
                f"'{target.uploaded_as}' ({target.row['file']})"
            )
        else:
            print(
                f"  row {target.row_number}: would upload under its existing identifier "
                f"'{target.identifier}', as '{target.uploaded_as}' ({target.row['file']})"
            )
    print()

    if not write_back:
        print(
            "would write nothing to the Sheet - neither --live nor --write-identifier was passed"
        )
        return

    print("would write these cells:")
    for update in reserve_updates(targets, columns) + confirm_updates(targets, columns, uploaded_at):
        print(f"  {update.a1} = {update.value}")


def cmd_upload(args) -> int:
    # A dry run writes nothing, so it may preview while a real run is going.
    if getattr(args, "dry_run", False):
        return upload_from_sheet(args)
    holder = upload_lock.LockHolder(
        pid=os.getpid(),
        started_at=utc_timestamp(),
        project=args.project,
        batch=getattr(args, "batch", None),
        live=bool(args.live),
    )
    # QUOTA-AND-RUNS.md, "One upload runs at a time, enforced by `upload`".
    try:
        lock = upload_lock.acquire(upload_lock.UPLOAD_LOCK_PATH, holder)
    except upload_lock.UploadLockHeld as refusal:
        print(f"{refusal}.", file=sys.stderr)
        print(
            "Two uploads at once can upload the same rows twice. Let that run finish, or stop it "
            "where it was started, then run this again.",
            file=sys.stderr,
        )
        return 1
    with lock:
        return upload_from_sheet(args)


def upload_from_sheet(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)

    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))
    # --live always records. An item that exists on Internet Archive under a
    # permanent identifier the Sheet does not know about is precisely what the
    # reserve-first ordering exists to prevent, so there is no live-without-
    # write-back mode to opt into.
    write_back = live or bool(getattr(args, "write_identifier", False))
    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is uploaded, and nothing is written to the Sheet")
    elif write_back:
        print(f"results WILL be written back to spreadsheet '{config.sheet_id_for(live)}'")
    else:
        print(
            "nothing will be written back to the Sheet - pass --write-identifier to record "
            "identifiers there"
        )
    print()

    # Task 12: read and validate --limit/--chunk-size as early as possible -
    # before any Sheet I/O, field-receipt printing, or validation work - so a
    # typo'd flag fails fast instead of only surfacing after the run has
    # already done everything short of uploading.
    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        # Silently doing nothing is the trap here, not a crash: a limit of
        # zero (or negative) would slice plan_upload_targets()'s output down
        # to nothing, upload zero items, and still report the run as clean -
        # the operator would have no reason to suspect --limit was the cause.
        print(
            f"--limit must be a positive number of items, not {limit}. A run with nothing to "
            "upload is what dropping --limit already means - drop it instead of passing zero "
            "or a negative number.",
            file=sys.stderr,
        )
        return 1

    # getattr's default is looked up fresh on every call (it is an ordinary
    # function argument, not a class default evaluated once at import time),
    # so falling back to the module-level CHUNK_SIZE here - rather than
    # snapshotting it into make_upload_args()'s Namespace - is what keeps
    # every existing `monkeypatch.setattr("ia_bulk.CHUNK_SIZE", 1)` test
    # working: those tests never set args.chunk_size at all, so this always
    # sees whatever CHUNK_SIZE is right now.
    chunk_size = getattr(args, "chunk_size", None)
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if chunk_size <= 0:
        # Two different failure modes, both worse than a clean refusal:
        # chunk_rows()'s range(0, len(rows), chunk_size) raises ValueError
        # mid-run for 0 (a bare traceback in place of the run summary), and
        # silently produces ZERO chunks for a negative value - the run
        # uploads nothing and still reports success, exactly like the
        # --limit <= 0 case above.
        print(
            f"--chunk-size must be a positive number of items, not {chunk_size}. Zero raises "
            "inside chunk_rows(); a negative value silently produces zero chunks, uploading "
            "nothing while the run still reports success.",
            file=sys.stderr,
        )
        return 1

    # Read before any Sheet I/O for the same reason --limit and --chunk-size
    # are: a mistyped flag must not cost a full read, a full file-resolution
    # pass over the drive and a full validation first. The rest of --batch's
    # checks need the Sheet's own columns and run after it is read.
    try:
        if getattr(args, "batch", None) is not None:
            batch_column_for(config, args.batch, args.registry)
    except BatchScopeError as exc:
        print(exc, file=sys.stderr)
        return 1

    try:
        sheet = read_sheet(args, registry, config, live, "upload")
    except SheetSetupFailed:
        return 1

    client, column_map, rows = sheet.client, sheet.column_map, sheet.rows

    try:
        columns = locate_write_back_columns(column_map)
    except MissingWriteBackColumns as exc:
        print(f"project '{config.project_id}': {exc}", file=sys.stderr)
        return 1

    # Fingerprints come from the RAW cells, so they must be taken before
    # resolve_sheet_files() rewrites row['file'] to the resolved name. They are
    # what every later mid-run-edit check compares against.
    source_fingerprints = sheet_row_fingerprints(rows, config.file_template)

    try:
        header_results, row_results = validate_sheet_content(sheet, args)
    except SheetSetupFailed:
        return 1

    if header_results:
        # A header defect (two columns normalizing to the same IA field name,
        # a column normalizing to nothing) silently corrupts EVERY row, so
        # unlike a bad row it cannot be routed around by skipping it.
        print("\n".join(_format_result_lines(header_results)))
        print()
        print(
            "the Sheet's header row has problems that affect every row - refusing to upload "
            "anything until they are fixed",
            file=sys.stderr,
        )
        return 1

    # `upload` owns the run, not the data: it itemizes only rows it would
    # otherwise have uploaded (`blocked` - ready, but invalid) and gives one
    # contained line for the rest (`not_ready` - never yet catalogued, so
    # never in this run's scope to begin with). `validate` owns the
    # per-field breakdown of the backlog - repeating it here would make
    # `upload` loud about the ~2,900 uncatalogued rows on every single run,
    # which is the exact noise this split exists to stop.
    # --batch narrows the scope BEFORE anything counts it: an uncatalogued or
    # broken row in another batch is not this run's business to report, and
    # `validate --batch` shows exactly this same set through this same call.
    try:
        scope = resolve_batch_scope(args, config, column_map, rows)
    except BatchScopeError as exc:
        print(exc, file=sys.stderr)
        return 1
    reported = in_batch_scope(row_results, scope)

    blocked = [result for result in reported if result.verdict is UploadVerdict.INVALID]
    not_ready = [result for result in reported if result.verdict is UploadVerdict.NOT_READY]
    not_ready_broken = [result for result in not_ready if not result.is_valid]

    if blocked:
        print("\n".join(_format_result_lines(blocked)))
        print(
            f"{_pluralize(len(blocked), 'row')} failed validation and will be skipped; the rest "
            "are uploaded, and this command still exits non-zero so a partial run is never "
            "mistaken for a clean one"
        )
    if not_ready:
        # The sub-count is deliberately CONTAINED inside the not-ready
        # sentence - in parentheses, mid-sentence - rather than printed as
        # a second number beside it: two adjacent numbers read as a
        # partition (as if they summed to something) unless something says
        # otherwise, and a not-ready-and-broken row is a SUBSET of the
        # not-ready total, not a disjoint group next to it.
        line = f"{_pluralize(len(not_ready), 'row')} not yet catalogued"
        if not_ready_broken:
            verb = "has" if len(not_ready_broken) == 1 else "have"
            line += (
                f" ({len(not_ready_broken):,} of them also {verb} an unresolvable "
                "filename - run `validate` to see them)"
            )
        print(line)
    if blocked or not_ready:
        print()

    # `rows` and `row_results` stay whole here, with `scope` passed alongside:
    # plan_upload_targets reads every row for identifiers already spent, and
    # everything downstream of the Sheet read is positional, so a compacted
    # list would both renumber rows and re-mint another batch's numbers.
    targets = plan_upload_targets(
        rows, row_results, config, live, source_fingerprints, run_stamp(), scope=scope
    )

    # Task 12: --limit counts PLANNED targets (valid AND ready AND not
    # already done), not Sheet rows scanned - plan_upload_targets has
    # already done that filtering above, so slicing ITS OUTPUT here (never
    # `rows`/`row_results` before it runs - that would count raw Sheet rows
    # instead, a materially different and wrong reading, see
    # docs/DECISIONS.md) is what makes "--limit 100" mean "100 of the rows
    # actually in scope", not "stop after the first 100 rows read". Numbers
    # were minted for every pending row before this slice runs
    # (plan_upload_targets mints for the whole run up front - see its own
    # docstring), but minting is pure arithmetic with no side effect: a
    # target dropped here is never reserved, so its number is never spent
    # and next_identifiers() mints it again next run. `limit` and
    # `chunk_size` were already read and validated at the top of this
    # function.
    if limit is not None:
        targets = targets[:limit]

    # Checked AFTER --limit, so --limit is the ordinary way to satisfy it: a
    # Sheet with 9,000 ready rows is not an error, running at all 9,000 of
    # them in one day is. Refuses rather than silently capping - a run that
    # quietly stops short reads as a complete one, which is the same trap the
    # --limit <= 0 guard above exists to avoid.
    if len(targets) > DAILY_ITEM_CAP and not getattr(args, "allow_over_daily_cap", False):
        print(
            f"this run would upload {len(targets)} items, over Internet Archive's "
            f"{DAILY_ITEM_CAP}/day cap for the account. Pass --limit {DAILY_ITEM_CAP} (or "
            "less) and run again tomorrow for the rest; identifiers are minted fresh each "
            "run, so nothing is lost by splitting it. Pass --allow-over-daily-cap to "
            "override if you know this account's cap has been raised.",
            file=sys.stderr,
        )
        return 1

    collection = config.ia_collection if live else TEST_COLLECTION

    # `upload` is where something permanent happens, so it shows the same
    # field receipt `validate` does rather than assuming the operator ran
    # validate first and remembers what it said.
    print(format_field_receipt(column_map))
    print()

    if dry_run:
        print_dry_run(targets, columns, write_back, upload_timestamp())
        return 1 if blocked else 0

    if not targets:
        print("nothing to upload - every valid row is already marked uploaded")
        return 1 if blocked else 0

    log_path = open_log(args.log_dir, "upload")
    try:
        log_run_header(
            log_path,
            config,
            column_map,
            live,
            dry_run,
            limit=limit,
            chunk_size=chunk_size,
            batch=getattr(args, "batch", None),
        )
    except Exception as exc:
        # This record is a receipt for later, not part of the upload itself -
        # a run about to create permanent Internet Archive items must not be
        # stopped by a failure to write it. Same reasoning as _write() below:
        # a clean stderr message, never a traceback in place of the actual
        # work this command exists to do.
        print(
            f"could not write the run-header record to {log_path}: {exc}. Continuing without "
            "it - this only affects the log's own audit trail, not the upload that follows.",
            file=sys.stderr,
        )
    summary = SheetUploadRun(
        client=client,
        columns=columns,
        column_map=column_map,
        mediatype=config.mediatype,
        file_template=config.file_template,
        files_dir=config.files_dir,
        collection=collection,
        live=live,
        write_back=write_back,
        log_path=log_path,
        chunk_size=chunk_size,
    ).execute(targets).with_skipped(skipped_rows(blocked))

    lines = upload_summary_lines(summary)
    for line in lines:
        print(line)
    record = try_log_run_summary(log_path, summary, live)
    mirror_run_to_log_tab(client, config.upload_log_tab, log_path, record, lines[0])
    print(f"log written to {log_path}")
    return (
        1
        if (summary.failed or summary.unconfirmed or summary.not_attempted or summary.skipped)
        else 0
    )


@dataclass(frozen=True)
class SyncTarget:
    """One already-uploaded row whose Sheet metadata this run may push to
    Internet Archive."""

    row_number: int
    identifier: str        # the real, permanent one, from ia_identifier
    uploaded_as: str       # the item to actually send to, from ia_url
    metadata: dict[str, str]
    # What this row would send, hashed, as of the run's initial read; and
    # what `ia_sync_hash` held at that same moment. The row pushes when they
    # differ. Both captured at READ time: re-deriving content_hash at write
    # time would stamp a human edit made mid-run as already-synced, losing it
    # permanently. See docs/DECISIONS.md, "A row pushes only when its content
    # changed".
    content_hash: str = ""
    stored_hash: str = ""
    # This row's file_template candidate from the RAW cells, re-checked
    # against a fresh read before the stamp write - see split_moved_targets().
    source_fingerprint: str = ""
    # split_moved_targets() reads this. A sync target addresses a row that
    # uploaded under an earlier run, so it is never a number this run minted;
    # the guard is always called with reserved_already=True.
    newly_minted: bool = False


@dataclass(frozen=True)
class RowFailure:
    """One row that did not get its metadata onto Internet Archive, and why.

    The same shape serves both reasons a row can miss: a send Internet
    Archive refused, and a row this run declined to send at all. What
    separates them is which list of SyncSummary it lands in, not its own
    fields - a reader wanting only one kind reads only one list."""

    identifier: str
    error: str

    def as_record(self) -> dict[str, str]:
        return {"identifier": self.identifier, "error": self.error}


def skipped_rows(problems: list[RowValidation]) -> list[RowFailure]:
    """The rows a run declined to send, as summary entries.

    A RowValidation carries a list of errors; a row is skipped for the first
    thing wrong with it, so the reasons are joined rather than one being
    chosen. A row with a blank identifier still gets an entry: the reason is
    the useful half, and dropping the row entirely would make the summary's
    skipped list disagree with the count printed beside it."""
    return [
        RowFailure(identifier=problem.identifier, error="; ".join(problem.errors))
        for problem in problems
    ]


@dataclass(frozen=True)
class PushOutcome:
    """What sync's send loop did - one entry per row it actually sent.
    SyncSummary is where `succeeded` becomes `changed`.

    `failures` is the list, never a count beside it: `failed` is derived, so
    there is no second number to forget to bump."""

    succeeded: int = 0
    unchanged: int = 0
    failures: tuple[RowFailure, ...] = ()

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def pushed(self) -> int:
        return self.succeeded + self.unchanged + self.failed


@dataclass(frozen=True)
class SyncSummary:
    """One sync run, whole. The console's closing lines and the log's
    `run_summary` record are both rendered from this object, so the number a
    person reads and the number a program reads cannot drift apart.

    The three counts the summary is asked for mean:

    - `checked` - rows this run evaluated, every row it read. Subtracting
      `pushed` and the skipped rows from it leaves the rows not marked
      uploaded, which is what an unattended run needs to see to know it is
      looking at the whole Sheet and not a slice of it.
    - `pushed` - rows actually sent to Internet Archive.
    - `changed` - sends Internet Archive accepted as a change. `unchanged` is
      its "no changes to _meta.xml" answer, kept as its own count rather than
      folded in: that answer is the idempotence signal a full re-sync is run
      to see, and reading it as "nothing happened" gets it exactly backwards.

    `skipped` is separate from `outcome.failures` on purpose. A failure means
    the item was contacted and the send was refused; a skip means the row was
    never safely targetable and nothing was sent. Months later, that is the
    difference between "the item may be in a state I did not intend" and "the
    item was not touched" - a distinction a single flat list destroys."""

    checked: int
    outcome: PushOutcome
    skipped: tuple[RowFailure, ...] = ()
    # Rows this run read, found already in sync, and did not send. Its own
    # count rather than folded into `checked`: on the steady state it is
    # nearly the whole Sheet, and an operator watching an hourly job needs
    # "4,212 already in sync, 1 updated" to read as a working run rather than
    # as a run that did almost nothing.
    already_synced: int = 0

    @property
    def pushed(self) -> int:
        return self.outcome.pushed

    @property
    def changed(self) -> int:
        """A successful metadata send IS a change - PushOutcome's neutral
        `succeeded` becomes sync's own word for it here."""
        return self.outcome.succeeded

    @property
    def unchanged(self) -> int:
        return self.outcome.unchanged

    @property
    def failed(self) -> int:
        return self.outcome.failed

    def as_record(self, live: bool) -> dict:
        return {
            "record": "run_summary",
            "timestamp": utc_timestamp(),
            "live": live,
            "checked": self.checked,
            "pushed": self.pushed,
            "changed": self.changed,
            "unchanged": self.unchanged,
            "already_synced": self.already_synced,
            "failures": [failure.as_record() for failure in self.outcome.failures],
            "skipped": [skip.as_record() for skip in self.skipped],
        }


def sync_summary_lines(summary: SyncSummary) -> list[str]:
    """The run's closing lines for a person to read.

    Rendered from the same SyncSummary that log_run_summary() writes, and the
    only place sync formats those numbers, so what a person is
    told and what a program reads cannot drift apart - the reason this takes
    a summary rather than the counts it prints."""
    lines = [
        f"{summary.changed} item(s) updated successfully, {summary.unchanged} unchanged, "
        f"{summary.failed} error(s)"
    ]
    if summary.already_synced:
        lines.append(
            f"{_pluralize(summary.already_synced, 'row')} already in sync (unchanged since "
            "its last push, so nothing was sent)"
        )
    if summary.skipped:
        lines.append(
            f"{_pluralize(len(summary.skipped), 'row')} skipped (not safely targetable)"
        )
    return lines


@dataclass(frozen=True)
class UploadSummary:
    """One upload run, whole - upload's half of what SyncSummary does for
    sync-metadata. The console's closing lines and the log's `run_summary`
    record are both rendered from this object, so the number a person reads
    and the number a program reads cannot drift apart.

    Upload keeps its own vocabulary rather than borrowing sync's: there is no
    `unchanged` here, because an upload either created the item or did not.

    The ways a row can miss are kept apart, because months later they are
    three different phone calls:

    - `failures` - the send was attempted and Internet Archive refused it.
      Nothing was created, and the identifier is still unused.
    - `unconfirmed` - the file IS on Internet Archive but the Sheet was never
      marked. The dangerous one: a later run reads the row as un-uploaded and
      would upload the same photograph again under a second identifier.
    - `skipped` - nothing was sent. Either the row failed validation, or the
      Sheet was edited mid-run and the row no longer matched what this run
      planned for it.

    `not_attempted` is a count rather than a list, and it is the one number
    here that OVERLAPS the lists rather than partitioning against them: it is
    the console's own "the run stopped early" figure, which counts a row this
    run declined to touch whether or not that row also appears under
    `skipped`. Reading the counts as a partition and summing them is
    therefore wrong - `attempted` is the only derived total."""

    succeeded: int = 0
    failures: tuple[RowFailure, ...] = ()
    unconfirmed: tuple[RowFailure, ...] = ()
    not_attempted: int = 0
    # The parsed 429/503 the run stopped on; None when it did not stop on one.
    rate_limit_status: int | None = None
    skipped: tuple[RowFailure, ...] = ()

    @property
    def failed(self) -> int:
        return len(self.failures)

    @property
    def rate_limited(self) -> bool:
        return self.rate_limit_status is not None

    @property
    def attempted(self) -> int:
        """Rows actually sent to Internet Archive this run, refused or not.
        Derived rather than carried, so there is no second number to forget
        to bump - the rule PushOutcome.failed already follows."""
        return self.succeeded + self.failed

    def with_skipped(self, skipped: list[RowFailure]) -> UploadSummary:
        """The same run, plus the rows validation held back.

        Kept out of SheetUploadRun.execute() because those rows never entered
        it: they were rejected before a target was ever built, and a send loop
        that had to be told about rows it will not send would be the wrong
        shape. The caller holds both halves and joins them here.

        ADDS to the run's own skipped rows rather than replacing them.
        execute() has already collected the rows it declined to send because
        the Sheet was edited underneath the run, and those are the ones with
        no other record at run level - `not_attempted` counts them without
        naming them. Overwriting the list dropped exactly the rows a person
        opens the log tab to find."""
        return replace(self, skipped=self.skipped + tuple(skipped))

    def as_record(self, live: bool) -> dict:
        return {
            "record": "run_summary",
            "timestamp": utc_timestamp(),
            "live": live,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failures": [failure.as_record() for failure in self.failures],
            "unconfirmed": [entry.as_record() for entry in self.unconfirmed],
            "not_attempted": self.not_attempted,
            "rate_limited": self.rate_limited,
            "rate_limit_status": self.rate_limit_status,
            "skipped": [entry.as_record() for entry in self.skipped],
        }


def upload_summary_lines(summary: UploadSummary) -> list[str]:
    """The run's closing lines for a person to read.

    Rendered from the same UploadSummary that log_run_summary() writes, and
    the only place the upload path formats those numbers - which is why this
    takes the summary rather than the counts it prints."""
    lines = [f"{summary.succeeded} file(s) uploaded successfully, {summary.failed} error(s)"]
    if summary.unconfirmed:
        lines.append(
            f"{_pluralize(len(summary.unconfirmed), 'item')} uploaded but NOT recorded in the "
            "Sheet - each one is named on stderr above, and in the log"
        )
    if summary.not_attempted:
        lines.append(
            f"{_pluralize(summary.not_attempted, 'row')} not attempted - the run stopped early; "
            "see the reason on stderr above"
        )
    if summary.skipped:
        lines.append(f"{_pluralize(len(summary.skipped), 'row')} skipped (failed validation)")
    return lines


def log_run_summary(log_path: str | Path, record: dict) -> None:
    """The last line of a run's log: what the run did, in one record, without
    replaying the per-row lines above it.

    Appended like every other record rather than rewritten in place, so a run
    killed partway still leaves every intact row record behind it, whether
    or not this line was ever written."""
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def try_log_run_summary(
    log_path: str | Path, summary: SyncSummary | UploadSummary, live: bool
) -> dict:
    """log_run_summary(), but a write failure is reported rather than raised.

    The summary is a record OF the run, not a step IN it, and it is written
    last - by the time it fails, permanent metadata has already changed. A
    run reported as failed invites a rerun, so the one thing this must never
    do is turn a sync that reached Internet Archive into a crash. Same
    treatment log_run_header() already gets, and for the same reason.

    Returns the record it wrote - or tried to - so a caller mirroring the
    same run into the Sheet renders that one object rather than building a
    second one a second later. Two as_record() calls carry two timestamps,
    and a tab whose `when` matches no line in the JSONL defeats the very
    lookup the tab invites."""
    record = summary.as_record(live)
    try:
        log_run_summary(log_path, record)
    except Exception as exc:
        print(
            f"could not write the run-summary record to {log_path}: {exc}. The run itself "
            "completed; this affects only the log's own audit trail.",
            file=sys.stderr,
        )
    return record


def sync_run_is_worth_mirroring(summary: SyncSummary) -> bool:
    """Whether a sync run has anything to say in the Sheet's log tab.

    An hourly sync's steady state is "every row already matches its last
    push" - it sends nothing and finds nothing wrong. Mirrored, that would be
    a row an hour, roughly 9,000 a year, burying the handful of rows that
    report an actual problem in the tab whose entire value is that a person
    can scan it. Such a run is still fully recorded in its own JSONL, and
    per-row "did my edit land" is already answered by `ia_last_synced` in the
    Sheet itself.

    Upload has no equivalent rule: a run with nothing to upload returns
    before a log is even opened, so every upload run that gets this far did
    something worth a row."""
    return bool(summary.pushed or summary.failed or summary.skipped)


def mirror_run_to_log_tab(
    client: SheetClient,
    tab: str | None,
    log_path: Path,
    record: dict,
    headline: str,
) -> None:
    """Mirror the run's summary into the Sheet's log tab for this command, if
    the registry names one.

    `tab` is None for a project that has not asked for a log tab, and this
    does nothing at all in that case - no read, no create, no append. That is
    the default, so a Sheet only ever grows a tab its owner configured.

    The mirror is fed the very record written to the JSONL a line earlier -
    passed in, not rebuilt - so the tab and the file cannot disagree about
    what happened, down to the timestamp. It reuses the run's own client one tab over rather than
    authenticating again, and hands the tab writer a client that has no
    cell-write method: the Sheet -> Internet Archive direction stays
    one-directional by construction, not by care."""
    if not tab:
        return
    log_tab.mirror_run(client.append_only_tab(tab), record, run=log_path.name, headline=headline)


def plan_sync_targets(
    rows: list[dict[str, str]],
    column_map: ColumnMap,
    live: bool,
    project_id: str,
    file_template: str,
) -> tuple[list[SyncTarget], list[RowValidation]]:
    """Decides which rows this run will correct, and what it will send.

    project_id is the run's own --project, required here for the same reason
    check_identifier requires it (issue #2). This path targets whatever item
    `ia_url` names, so a cell pointing at another project's item does not
    merely misfile this project's row - it overwrites that project's
    metadata, so the check is made here.

    Scope is RowState.DONE and nothing else. An UNASSIGNED row has no item to
    correct, and a RESERVED row's upload never confirmed - correcting metadata
    on an item that may not exist is a different problem, and `upload` already
    retries those under their existing identifier.

    A row is sent only when its content changed. `content_hash` is what this
    row would send, hashed; `stored_hash` is what it last successfully sent,
    read out of `ia_sync_hash`. split_unchanged() compares them. This
    reverses the original decision that every DONE row is sent every run -
    that was correct for a hand-run command over a few hundred rows and does
    not survive ~4,000 rows on an hourly schedule, where it is ~4,000
    pointless writes an hour and a log in which a real edit is invisible. See
    docs/DECISIONS.md, "A row pushes only when its content changed".

    Blank cells are dropped by update_metadata_row, so a cleared cell means
    "leave this field alone" and REMOVE_TAG deletes. A Sheet cell cleared by
    accident can therefore never strip metadata from a permanent public item.

    Returns (targets, problems). A DONE row this run cannot safely target is a
    problem rather than a silent skip: the operator edited it expecting the
    edit to reach the site."""
    fields = sheet_metadata_fields(column_map)
    # Raw cells: sync never calls resolve_sheet_files(), so row['file'] has
    # not been rewritten and these compare correctly against a fresh read.
    fingerprints = sheet_row_fingerprints(rows, file_template)
    targets: list[SyncTarget] = []
    problems: list[RowValidation] = []

    for offset, row in enumerate(rows):
        row_number = offset + 2
        if classify_row(row) is not RowState.DONE:
            continue

        identifier = (row.get(IA_IDENTIFIER_COLUMN) or "").strip()
        uploaded_as = identifier_from_url(row.get(IA_URL_COLUMN) or "")

        if uploaded_as is None:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"row is marked uploaded but its '{IA_URL_COLUMN}' cell is blank or "
                        "does not look like an Internet Archive item URL, so there is no "
                        "item to correct - restore it from the upload log, or clear "
                        f"'{IA_UPLOADED_COLUMN}' to have `upload` do the row again"
                    ],
                )
            )
            continue

        # The Sheet is the mode boundary (test and live are different
        # spreadsheets), so a mismatch here means the wrong Sheet is in the
        # registry or someone pasted a URL across - either way, sending a
        # live correction to a test item, or the reverse, is not recoverable
        # by rerunning.
        is_test_item = uploaded_as.startswith(TEST_IDENTIFIER_PREFIX)
        if live and is_test_item:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"--live, but '{IA_URL_COLUMN}' points at the test item "
                        f"'{uploaded_as}'. Refusing to send a live correction to a "
                        "rehearsal item"
                    ],
                )
            )
            continue
        if not live and not is_test_item:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"test mode, but '{IA_URL_COLUMN}' points at the real item "
                        f"'{uploaded_as}'. Refusing to send a rehearsal correction to a "
                        "permanent item - pass --live if that is what you meant"
                    ],
                )
            )
            continue

        if item_project_id(uploaded_as, live) != project_id:
            problems.append(
                RowValidation(
                    row_number=row_number,
                    identifier=identifier,
                    errors=[
                        f"'{IA_URL_COLUMN}' points at item '{uploaded_as}', which does not "
                        f"belong to this run's --project {project_id}. Refusing to send this "
                        "project's metadata to another project's item"
                    ],
                )
            )
            continue

        metadata = {key: value for key, value in row.items() if key in fields}
        targets.append(
            SyncTarget(
                row_number=row_number,
                identifier=identifier,
                uploaded_as=uploaded_as,
                metadata=metadata,
                content_hash=sync_hash(metadata_to_send(metadata)),
                stored_hash=(row.get(IA_SYNC_HASH_COLUMN) or "").strip(),
                source_fingerprint=fingerprints.get(row_number, ""),
            )
        )

    return targets, problems


def split_unchanged(targets: list[SyncTarget]) -> tuple[list[SyncTarget], list[SyncTarget]]:
    """Returns (to_push, already_synced).

    Its own function rather than a filter inside plan_sync_targets, so the
    gate can be tested on its own and so the dry run can report both halves
    without re-deriving anything.

    A blank stored hash pushes. That covers a row that has never synced and a
    row whose `ia_sync_hash` cell an operator cleared on purpose - the
    documented lever for forcing a re-sync, and deliberately the same code
    path, since an operator clearing a cell should get exactly what a fresh
    row gets."""
    to_push: list[SyncTarget] = []
    already_synced: list[SyncTarget] = []
    for target in targets:
        (already_synced if target.content_hash == target.stored_hash else to_push).append(target)
    return to_push, already_synced


@dataclass(frozen=True)
class SheetSyncRun:
    """The push -> stamp loop, chunked. Mirrors SheetUploadRun, which does
    the same job for reserve -> upload -> confirm.

    Chunking is not an optimisation here, it is the interruption-tolerance
    design. The Mac this runs on sleeps and shuts down unpredictably,
    including mid-run, so no run is guaranteed to finish. One batchUpdate at
    the END of a run would stamp nothing when the run is killed and the next
    run would re-push everything; one write per row would blow through the
    Sheets API's 60 writes/minute/user. One batch per chunk is a single API
    request per chunk - about 8 for a 4,000-row re-sync - and a kill costs at
    most one chunk's stamps, whose rows simply push again next time."""

    client: SheetClient
    columns: SyncColumns
    file_template: str
    log_path: Path
    live: bool
    chunk_size: int = CHUNK_SIZE

    def execute(self, targets: list[SyncTarget]) -> PushOutcome:
        succeeded = 0
        unchanged = 0
        failures: list[RowFailure] = []
        total = len(targets)
        position = 0

        for chunk in chunk_rows(targets, self.chunk_size):
            stamped: list[tuple[int, str]] = []
            pushed: list[SyncTarget] = []

            for target in chunk:
                position += 1
                print(f"[{position}/{total}] updating metadata for {target.uploaded_as}")
                try:
                    update_metadata_row(target.metadata, target.uploaded_as)
                except MetadataUnchanged:
                    # Internet Archive saying "no changes to _meta.xml" means
                    # the item already matches the Sheet. That is a successful
                    # reconciliation and it stamps - leaving it unstamped
                    # would make exactly the rows this gating exists to quiet
                    # re-push on every run, forever.
                    unchanged += 1
                    stamped.append((target.row_number, target.content_hash))
                    pushed.append(target)
                    self._log(target, "unchanged")
                except Exception as exc:
                    # No stamp. A failed row retries next run, which is the
                    # whole recovery story for a transient Internet Archive
                    # error.
                    failures.append(RowFailure(identifier=target.identifier, error=str(exc)))
                    print(f"    - {format_row_error(exc)}")
                    self._log(target, "failure", error=str(exc), http_status=parsed_status_code(exc))
                else:
                    succeeded += 1
                    stamped.append((target.row_number, target.content_hash))
                    pushed.append(target)
                    self._log(target, "success")

            self._stamp(stamped, pushed)

        return PushOutcome(succeeded=succeeded, unchanged=unchanged, failures=tuple(failures))

    def _stamp(self, stamped: list[tuple[int, str]], pushed: list[SyncTarget]) -> None:
        """Records what this chunk pushed, in one batch, at the rows this run
        planned for - having first proved those are still the same rows.

        Identity is checked late; content was captured early. The hash
        written is the one computed when the row was READ (it arrives here in
        `stamped`), while whether the row is still the same row is decided
        now. Re-deriving the hash from the Sheet's current cells instead
        would stamp a human edit made during the run as already-synced, and
        that edit would be lost permanently with nothing to notice it."""
        if not stamped:
            return

        safe = self._verified(pushed)
        safe_rows = {target.row_number for target in safe}
        updates = stamp_updates(
            [(row, digest) for row, digest in stamped if row in safe_rows],
            self.columns,
            utc_timestamp(),
        )
        try:
            write_cells_if_any(self.client, updates)
        except Exception as exc:
            print(
                f"the Sheet stamp write failed: {exc}. The metadata IS on Internet Archive; "
                f"{_pluralize(len(updates) // 2, 'row')} will simply be sent again next run "
                "and reported as unchanged. Continuing.",
                file=sys.stderr,
            )

    def _verified(self, pushed: list[SyncTarget]) -> list[SyncTarget]:
        """The rows still at the position this run planned for them.

        reserved_already=True: this leg checks the fingerprint AND the
        `ia_identifier` cell. SHEET-PROTOCOL.md warns that checking
        `ia_identifier` can be tautological - it is, on upload's
        reserve->confirm leg, because reserve wrote that value moments
        earlier and the check would be verifying its own write.
        `sync-metadata` never writes `ia_identifier`: it reads it at the
        initial read and compares here, so nothing is circular.

        Every failure below skips the whole chunk's stamp rather than
        raising. By this point permanent public metadata has already changed,
        and an unstamped row costs one repeat next run - a stack trace in
        place of the run summary costs the operator the log path.

        All five messages below say "the metadata IS on Internet Archive":
        every one of them fires after the chunk already pushed, so an
        operator reading any single one must be told the same thing an
        operator reading any other one is told - an inconsistency here reads
        as "some of these failures mean the run failed" when none of them
        do."""
        try:
            snapshot = read_sheet_snapshot(self.client, self.file_template)
        except MissingWriteBackColumns as exc:
            # sync-metadata only READS these four columns - it never writes
            # them, unlike upload's own equivalent message this one used to
            # share verbatim. And by the time this runs, locate_write_back_
            # columns() has already passed once, at startup (sync_from_sheet),
            # so reaching this branch means the column was there when the run
            # began and disappeared while it was in progress.
            print(
                f"a column this run reads to confirm a row's identity is gone: {exc}. It was "
                "there when this run started. The metadata IS on Internet Archive; these rows "
                "are sent again next run and reported as unchanged. Nothing stamped this "
                "chunk.",
                file=sys.stderr,
            )
            return []
        except Exception as exc:
            print(
                f"the Sheet could not be re-read before stamping: {exc}. The metadata IS on "
                "Internet Archive; these rows re-push next run and report as unchanged.",
                file=sys.stderr,
            )
            return []

        try:
            columns_now = locate_sync_columns(snapshot.column_map)
        except MissingSyncColumns as exc:
            print(
                f"{exc} The metadata IS on Internet Archive; these rows are sent again next "
                "run and reported as unchanged. Nothing stamped this chunk.",
                file=sys.stderr,
            )
            return []
        if columns_now != self.columns:
            print(
                "the Sheet's columns moved while this run was in progress, so every cell it "
                "would stamp now lands in the wrong column. The metadata IS on Internet "
                "Archive; these rows are sent again next run and reported as unchanged. "
                "Nothing stamped this chunk.",
                file=sys.stderr,
            )
            return []

        still_there, moved = split_moved_targets(pushed, snapshot, reserved_already=True)
        for target in moved:
            if not target.source_fingerprint:
                # A row whose file_template columns were already blank at
                # read time fingerprints as "" (sheet_row_fingerprints()),
                # which can never match - so this row lands here on every
                # run regardless of whether anyone touched the Sheet. Telling
                # the operator "the Sheet was edited" below would send them
                # looking for an edit that may never have happened.
                print(
                    f"row {target.row_number} ('{target.identifier}') has no file_template "
                    "fingerprint to confirm it is still the same row it was when this run "
                    "started, so it is not stamped this run. The metadata IS on Internet "
                    "Archive; this row is sent again next run and reported as unchanged.",
                    file=sys.stderr,
                )
                continue
            print(
                f"row {target.row_number} is no longer the row this run read for "
                f"'{target.identifier}' - the Sheet was edited while the run was in progress, "
                "so stamping there would mark a different photograph as synced. The metadata "
                "IS on Internet Archive; this row is sent again next run and reported as "
                "unchanged.",
                file=sys.stderr,
            )
        return still_there

    def _log(
        self,
        target: SyncTarget,
        status: str,
        error: str | None = None,
        http_status: int | None = None,
    ) -> None:
        log_result(
            self.log_path,
            target.identifier,
            "",
            status,
            self.live,
            error=error,
            uploaded_as=target.uploaded_as,
            http_status=http_status,
        )


def cmd_sync_metadata(args) -> int:
    """The Sheet is read live and IS the correction: edit a description in the
    Sheet, run this, it is on the site."""
    return sync_from_sheet(args)


def sync_header_refusal(
    column_map: ColumnMap, config: ProjectConfig, registry_path: str
) -> tuple[str, list[str]] | None:
    """(message, detail lines) for the first header-row reason sync-metadata
    refuses a Sheet, or None. The `sync state columns` deployment check runs it
    too, after read_sheet's no-data-rows refusal, so --enable-agent refuses the
    Sheets the agent would."""
    # A header defect corrupts every row's field names identically, and unlike
    # upload there is no per-row way around it.
    header_errors = check_column_map(column_map)
    if header_errors:
        return (
            "the Sheet's header row has problems that affect every row - refusing to send "
            "metadata until they are fixed",
            header_errors,
        )

    # All three checks are before anything is sent, and all three apply in
    # test mode as well as live: a rehearsal that passes where the real run
    # refuses is a false negative on the one run an operator trusts.
    try:
        locate_sync_columns(column_map)
    except MissingSyncColumns as exc:
        return str(exc), []

    # _verified() re-checks this same thing before every stamp write, because
    # a column can vanish mid-run - but relying on that alone lets a Sheet
    # that never had ia_identifier_bib through the front door: ia_identifier
    # and ia_uploaded absent means no row classifies DONE (refused in
    # sync_from_sheet, at "no row is marked uploaded yet"), and ia_url absent
    # means every row is a reported problem, but ia_identifier_bib absent is
    # invisible there - rows plan, hash-gate, and push, and only then does
    # _verified() catch it, per chunk, forever, stamping nothing while
    # permanent metadata keeps going out. Checked here for the same reason
    # check_file_template is.
    try:
        locate_write_back_columns(column_map)
    except MissingWriteBackColumns as exc:
        return f"project '{config.project_id}': {exc}", []

    # The moved-row guard fingerprints a row by its file_template columns.
    # A template naming a column the Sheet lacks fingerprints EVERY row as
    # "", which never matches - so nothing would ever be stamped and every
    # row would re-push forever, silently. Header check only; no disk access.
    try:
        check_file_template(config.file_template, column_map)
    except TemplateError as exc:
        return f"project '{config.project_id}': {exc} - fix 'file_template' in {registry_path}", []

    return None


def sync_from_sheet(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)

    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))
    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is sent to Internet Archive")
    print()

    # Read and validated before any Sheet I/O, the same way and for the same
    # reason upload's own --chunk-size is (see cmd_upload): chunk_rows()'s
    # range(0, len(rows), chunk_size) raises ValueError for zero - but only
    # after open_log/log_run_header have already run, leaving a log with a
    # header and no summary - and silently yields zero chunks for a negative
    # value, so the run pushes nothing and still reports success.
    chunk_size = getattr(args, "chunk_size", None)
    if chunk_size is None:
        chunk_size = CHUNK_SIZE
    if chunk_size <= 0:
        print(
            f"--chunk-size must be a positive number of items, not {chunk_size}. Zero raises "
            "inside chunk_rows(); a negative value silently produces zero chunks, pushing "
            "nothing while the run still reports success.",
            file=sys.stderr,
        )
        return 1

    try:
        sheet = read_sheet(args, registry, config, live, "sync-metadata")
    except SheetSetupFailed:
        return 1

    column_map, rows = sheet.column_map, sheet.rows

    refusal = sync_header_refusal(column_map, config, args.registry)
    if refusal is not None:
        message, details = refusal
        if details:
            print("\n".join(f"    - {detail}" for detail in details))
        print(message, file=sys.stderr)
        return 1
    sync_columns = locate_sync_columns(column_map)

    targets, problems = plan_sync_targets(
        rows, column_map, live, config.project_id, config.file_template
    )

    if problems:
        print("\n".join(_format_result_lines(problems)))
        print(
            f"{_pluralize(len(problems), 'row')} marked uploaded but not safely targetable "
            "and will be skipped; this command still exits non-zero so a partial run is "
            "never mistaken for a clean one"
        )
        print()

    print(format_field_receipt(column_map))
    print()

    to_push, already_synced = split_unchanged(targets)

    if not targets:
        print("nothing to sync - no row is marked uploaded yet")
        return 1 if problems else 0

    if dry_run:
        return print_sync_dry_run(to_push, already_synced, problems)

    # From here every path is a real run - even the one that sends nothing -
    # so every path gets a log. A dry run above never reaches this line and
    # so still writes none, which is correct: it sent nothing to summarize.
    log_path = open_log(args.log_dir, "sync-metadata")
    try:
        log_run_header(log_path, config, column_map, live, dry_run)
    except Exception as exc:
        print(
            f"could not write the run-header record to {log_path}: {exc}. Continuing without "
            "it - this only affects the log's own audit trail.",
            file=sys.stderr,
        )

    if not to_push:
        # The steady state on an hourly schedule, and the console line must
        # stay one quiet sentence: a run that says nothing useful is a run
        # whose output stops being read. But this is the MOST common outcome
        # of a scheduled run, and "ran, found nothing to do" has to be
        # distinguishable from "did not run at all" - see OPERATIONS.md's
        # tail-the-latest-log recipe - so it still gets the same header and
        # summary record every other run writes, with the real (mostly zero)
        # numbers in it.
        summary = SyncSummary(
            checked=len(rows),
            outcome=PushOutcome(),
            skipped=tuple(skipped_rows(problems)),
            already_synced=len(already_synced),
        )
        record = try_log_run_summary(log_path, summary, live)
        nothing_to_sync = (
            f"nothing to sync - all {_pluralize(len(already_synced), 'uploaded row')} "
            "already match their last push"
        )
        if sync_run_is_worth_mirroring(summary):
            # The headline is this path's own line, not sync_summary_lines()'s
            # "0 updated, 0 unchanged, 0 error(s)": the tab should say what the
            # operator saw, and on this path they saw neither of those numbers.
            mirror_run_to_log_tab(
                sheet.client, config.sync_log_tab, log_path, record, nothing_to_sync
            )
        print(nothing_to_sync)
        print(f"log written to {log_path}")
        return 1 if problems else 0

    sync_run = SheetSyncRun(
        client=sheet.client,
        columns=sync_columns,
        file_template=config.file_template,
        log_path=log_path,
        live=live,
        chunk_size=chunk_size,
    )
    summary = SyncSummary(
        checked=len(rows),
        outcome=sync_run.execute(to_push),
        skipped=tuple(skipped_rows(problems)),
        already_synced=len(already_synced),
    )
    record = try_log_run_summary(log_path, summary, live)

    lines = sync_summary_lines(summary)
    for line in lines:
        print(line)
    if sync_run_is_worth_mirroring(summary):
        mirror_run_to_log_tab(sheet.client, config.sync_log_tab, log_path, record, lines[0])
    print(f"log written to {log_path}")
    return 1 if (summary.failed or summary.skipped) else 0


RECONCILE_FLUSH_EVERY = 25


@dataclass(frozen=True)
class PendingCorrection:
    """An accepted correction waiting to be written, carrying what its cell
    said when it was matched.

    `wanted` is the whole point of the dataclass: it is what flush() checks
    the row against on a fresh read before writing, so a Sheet edited mid
    session cannot land a filename on a photograph it was never about. Same
    role as `sheet_row_fingerprints()` for `upload`, one cell wide."""

    update: CellUpdate
    row_number: int
    wanted: str


def log_decision(log_path, row_number: int, folder: str, wanted: str, status: str,
                 chosen: str = "", reason: str = "", proposed: str = "",
                 matches: list[str] | None = None) -> None:
    """One line per row considered. Prompt-per-proposal leaves no record of
    what was decided; this is that record.

    `proposed` and `chosen` are separate on purpose. `chosen` is what was
    written, so it is empty on every path but an acceptance - and a rejected
    proposal with no record of WHAT was rejected cannot be reviewed later,
    which is half of what this log is for. `matches` does the same job for
    the ambiguous path, where the console names every candidate and the
    durable record used to name none. Every key is present on every line,
    empty where it does not apply, so a reader never has to know which
    statuses carry which fields."""
    entry = {
        "row": row_number, "folder": folder, "wanted": wanted,
        "status": status, "chosen": chosen, "proposed": proposed,
        "matches": list(matches or []), "reason": reason,
        "timestamp": utc_timestamp(),
    }
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def cmd_reconcile_files(args) -> int:
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))

    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is written to the Sheet")
    print()

    try:
        sheet = read_sheet(args, registry, config, live, "reconcile-files")
    except SheetSetupFailed:
        return 1

    # A bad row is skipped; a bad header stops the whole run - the same split
    # `upload` makes, for the same reason: a header defect corrupts every row
    # identically, so unlike a bad row it cannot be routed around. It is not
    # merely a missing warning here. Two headers normalizing to `file_name`
    # leave grid_to_rows reading the LAST of them (dict comprehension, last
    # key wins) while the write below targets the FIRST - so the correction
    # lands in a column nothing reads, the effective cell keeps its stale
    # value, and the run reports success. structural_rows are individual data
    # rows whose cells may be shifted relative to the header (check_grid_shape);
    # those are skipped rather than fatal.
    header_defects, structural_rows = split_structure_results(
        sheet.structure_results, len(sheet.rows)
    )
    if header_defects:
        print("\n".join(_format_result_lines(header_defects)))
        print()
        print(
            "the Sheet's header row has problems that affect every row - refusing to "
            "reconcile anything until they are fixed",
            file=sys.stderr,
        )
        return 1
    if structural_rows:
        print("\n".join(_format_result_lines(structural_rows)))
        print(
            f"{_pluralize(len(structural_rows), 'row')} above may have cells shifted "
            "against the header - skipped, since which cell a correction would land in "
            "is exactly what is in doubt"
        )
        print()

    name_field = template_fields(config.file_template)[-1]
    try:
        name_column = next(
            index for index, header in enumerate(sheet.column_map.headers)
            if sheet.column_map.field_names[header] == name_field
        )
    except StopIteration:
        print(
            f"the Sheet has no '{name_field}' column, which file_template names - "
            f"fix 'file_template' in {args.registry} or add the column.",
            file=sys.stderr,
        )
        return 1

    survey = survey_files(sheet.rows, config)
    skipped_rows = {entry.row_number for entry in structural_rows}
    to_review = [n for n in sorted(survey.unresolved) if n not in skipped_rows]
    if survey.not_ready:
        # One contained line, never one line (let alone one prompt) per row.
        # The real Sheet is ~3,000 rows of which ~2,900 carry no filename at
        # all; those are not-ready, not broken, and there is nothing an
        # operator could decide about them here. Same judgment `upload`
        # makes about the same rows - see docs/decisions/READINESS.md.
        print(f"{_pluralize(len(survey.not_ready), 'row')} not yet catalogued - "
              "no filename to reconcile, skipped")
    if not to_review:
        if survey.unresolved:
            # Every row that failed resolution was skipped above, so there is
            # nothing left to ask about - but saying "every row resolves"
            # here would be untrue.
            print("nothing left to reconcile - every row that named a file either "
                  "resolves or was skipped above")
        else:
            print("nothing to reconcile - every row with a filename resolves against the drive")
        return 0

    print(f"{_pluralize(len(to_review), 'row')} named a file that does not resolve")
    print()

    log_path = None if dry_run else open_log(args.log_dir, "reconcile-files")
    pending: list[PendingCorrection] = []
    accepted = stopped = 0

    def flush() -> bool:
        """Write what has been accepted, dropping anything whose row moved.

        This command has by far the longest read-to-write window in the tool:
        an interactive session over a Sheet several volunteers share can put
        an hour between the grid read that fixed `row_number` and the write
        that uses it, and one row inserted or deleted in that hour shifts
        every later write by one - silently putting a filename on the wrong
        photograph. `upload` already refuses to write through that window
        (sheet_row_fingerprints/read_sheet_snapshot/split_moved_targets);
        this is the same idea at a fraction of the cost, since reconcile
        knows exactly what each target cell said when it matched.

        A re-read that fails stops the run rather than writing unverified:
        the whole point is that an unchecked write here is the hazard."""
        nonlocal pending, accepted
        if not pending:
            return True
        try:
            grid = sheet.client.read_grid()
        except Exception as exc:
            print(
                f"could not re-read the Sheet to check the rows before writing: {exc}. "
                "Stopping here without writing - rerun to pick these up.",
                file=sys.stderr,
            )
            return False

        updates: list[CellUpdate] = []
        for correction in pending:
            now = cell_value(grid, correction.row_number, name_column)
            if now != correction.wanted:
                accepted -= 1
                print(f"row {correction.row_number}  the Sheet now says '{now}' where this "
                      f"run read '{correction.wanted}' - the row moved or was edited, so "
                      "the correction was NOT written")
                continue
            updates.append(correction.update)

        try:
            sheet.client.write_cells(updates)
        except Exception as exc:
            print(f"the Sheet write failed: {exc}. Stopping here.", file=sys.stderr)
            return False
        pending = []
        return True

    for row_number in to_review:
        folder = survey.unresolved[row_number]
        wanted = survey.wanted[row_number]
        # survey.unclaimed is a snapshot taken once, before any row in this
        # run was decided - it never shrinks on its own. Re-filter against
        # survey.claimed on every iteration (not just once before the loop):
        # accepting row N adds its file to `claimed` a few lines below, and
        # without this filter row N+1 in the same folder would still see
        # that same file as a candidate and could be proposed - and
        # accepted onto - it too. That is the exact misattribution FileSurvey's
        # own docstring promises cannot happen.
        candidates = [
            name for name in survey.unclaimed.get(folder, [])
            if claim_key(f"{folder}/{name}") not in survey.claimed
        ]
        try:
            proposal = propose_match(wanted, candidates)
            reason = proposal.reason if proposal else ""
        except AmbiguousMatch as exc:
            print(f"row {row_number}  '{wanted}'  matches {len(exc.matches)} files - "
                  f"leaving it alone: {', '.join(exc.matches)}")
            if log_path:
                log_decision(log_path, row_number, folder, wanted, "ambiguous",
                             matches=exc.matches)
            continue

        if dry_run:
            if proposal:
                print(f"row {row_number}  '{wanted}' -> '{proposal.filename}'  ({reason})")
            else:
                print(f"row {row_number}  '{wanted}'  no candidate in '{folder}'")
            continue

        decision = prompt_for_decision(
            row_number, folder, wanted, proposal, candidates, config, survey.claimed
        )
        if decision.action == "stop":
            stopped = 1
            if log_path:
                log_decision(log_path, row_number, folder, wanted, "stopped")
            break
        if decision.action == "reject":
            if log_path:
                log_decision(log_path, row_number, folder, wanted,
                             "rejected" if proposal else "no_candidate", reason=reason,
                             proposed=proposal.filename if proposal else "")
            continue

        accepted += 1
        survey.claimed.add(claim_key(f"{folder}/{decision.filename}"))
        pending.append(
            PendingCorrection(
                update=CellUpdate(
                    f"{column_letter(name_column)}{row_number}", decision.filename
                ),
                row_number=row_number,
                wanted=wanted,
            )
        )
        if log_path:
            # From how the operator answered, not from whether the two
            # strings happen to agree: a name typed at [e] that matches the
            # proposal is still a name a human typed, and a log that calls
            # it `accepted` claims the tool proposed something it did not.
            status = "typed" if decision.typed else "accepted"
            log_decision(log_path, row_number, folder, wanted, status,
                         chosen=decision.filename, reason=reason,
                         proposed=proposal.filename if proposal else "")
        if len(pending) >= RECONCILE_FLUSH_EVERY and not flush():
            return 1

    if not flush():
        return 1

    print()
    print(f"{accepted} filename(s) corrected")
    remaining = len(to_review) - accepted
    if remaining:
        print(f"{_pluralize(remaining, 'row')} still unresolved")
    if stopped:
        print("stopped early - rerun to pick up where this left off")
    if log_path:
        print(f"log written to {log_path}")
    return 0


def cmd_append_rows(args) -> int:
    """Append a skeleton row for every photo file no row claims.

    Writes ONLY the file_template columns - folder and filename. Every other
    column is the cataloguer's: this removes the transcription work, not the
    cataloguing work (issue #11's hard constraint). Appended rows resolve on
    the next survey, so their files are claimed and a rerun over an
    unchanged drive appends nothing - idempotence comes from the drive and
    the Sheet, not from any state this command keeps."""
    registry = load_registry(args.registry)
    config = load_project_config(registry, args.project)
    live = bool(args.live)
    dry_run = bool(getattr(args, "dry_run", False))

    print(sheet_banner(config, live))
    if dry_run:
        print("--dry-run: nothing is appended to the Sheet")
    print()

    try:
        sheet = read_sheet(args, registry, config, live, "append-rows")
    except SheetSetupFailed:
        return 1

    header_defects, structural_rows = split_structure_results(
        sheet.structure_results, len(sheet.rows)
    )
    if header_defects:
        print("\n".join(_format_result_lines(header_defects)))
        print()
        print(
            "the Sheet's header row has problems that affect every row - refusing to "
            "append anything until they are fixed",
            file=sys.stderr,
        )
        return 1
    if structural_rows:
        print("\n".join(_format_result_lines(structural_rows)))
        print()
        print(
            f"{_pluralize(len(structural_rows), 'row')} above may have cells shifted "
            "against the header. Reconcile can skip a suspect row because an operator "
            "approves its rows one at a time - but append trusts the whole survey at "
            "once, and a misread row can make the file it really means look unclaimed, "
            "which would append a second row for a photograph that already has one. Fix "
            "these rows in the Sheet, then rerun.",
            file=sys.stderr,
        )
        return 1

    fields = template_fields(config.file_template)
    if len(fields) < 2:
        print(
            f"file_template {config.file_template!r} has no folder part - append-rows "
            "builds rows from a <folder>/<name> layout and cannot express this "
            "project's files as rows.",
            file=sys.stderr,
        )
        return 1
    columns: dict[str, int] = {}
    for field_name in fields:
        try:
            columns[field_name] = next(
                index for index, header in enumerate(sheet.column_map.headers)
                if sheet.column_map.field_names[header] == field_name
            )
        except StopIteration:
            print(
                f"the Sheet has no '{field_name}' column, which file_template names - "
                f"fix 'file_template' in {args.registry} or add the column.",
                file=sys.stderr,
            )
            return 1

    survey = survey_files(sheet.rows, config)
    if survey.not_ready:
        print(f"{_pluralize(len(survey.not_ready), 'row')} not yet catalogued - "
              "they claim no file, left alone")
    if survey.unresolved:
        # The hard gate, with no override flag: to the survey, a typo'd row
        # and a missing row both look like an unclaimed file, so appending
        # past this would add a second row for a photograph one of these
        # rows already means - see docs/decisions/RECONCILIATION.md,
        # "Reconciliation ships before append".
        for row_number in sorted(survey.unresolved):
            print(f"row {row_number}  '{survey.wanted[row_number]}'  does not resolve "
                  f"in '{survey.unresolved[row_number]}'")
        print(
            f"{_pluralize(len(survey.unresolved), 'row')} named a file that does not "
            "resolve, and an unresolved row is indistinguishable from a missing row - "
            "appending now could add a second row for a photograph one of these rows "
            "already means. Run reconcile-files until every row above is fixed, or "
            "blank the filename cell of one that cannot be, to mark its row "
            "not-yet-catalogued.",
            file=sys.stderr,
        )
        return 1

    unclaimed, outside = scan_unclaimed_files(survey.claimed, config)
    if outside:
        print(f"{_pluralize(len(outside), 'photo file')} at the top of "
              f"'{config.files_dir}', outside any folder - file_template cannot express "
              f"a row for them, skipped: {', '.join(outside)}")

    to_append = [(folder, name) for folder, names in unclaimed.items() for name in names]
    if not to_append:
        print("nothing to append - every photo file in a folder is claimed by a row")
        return 0

    for folder, names in unclaimed.items():
        print(f"{folder}: {_pluralize(len(names), 'file')} with no row")

    folder_field, name_field = fields[0], fields[-1]
    if dry_run:
        # Shown as the two cells the real run would write, under the Sheet's
        # own header spellings - not a joined folder/name string the reader
        # has to mentally re-split. The dry run's whole job is confidence
        # about the exact write.
        folder_header = sheet.column_map.headers[columns[folder_field]]
        name_header = sheet.column_map.headers[columns[name_field]]
        quoted = [(f"'{folder}'", f"'{name}'") for folder, name in to_append]
        folder_width = max(len(folder_header), max(len(q) for q, _ in quoted))
        print()
        print("each row would carry exactly these two cells - every other column "
              "is left blank for the cataloguer:")
        print()
        print(f"  {folder_header.ljust(folder_width)}  {name_header}")
        for quoted_folder, quoted_name in quoted:
            print(f"  {quoted_folder.ljust(folder_width)}  {quoted_name}")
        print()
        print(f"--dry-run: {_pluralize(len(to_append), 'row')} would be appended")
        return 0

    width = len(sheet.column_map.headers)
    new_rows = []
    for folder, name in to_append:
        # Full header width with values at the template's own columns -
        # "first two cells" would write a folder name into whatever column
        # happens to come first.
        row = [""] * width
        row[columns[folder_field]] = folder
        row[columns[name_field]] = name
        new_rows.append(row)

    try:
        sheet.client.append_rows(new_rows)
    except Exception as exc:
        print(
            f"the Sheet append failed: {exc}. Nothing was logged as appended - rerun "
            "to try again; a rerun never duplicates rows that did land.",
            file=sys.stderr,
        )
        return 1

    # Logged only after the append succeeded: the log records what happened,
    # not what was attempted.
    log_path = open_log(args.log_dir, "append-rows")
    with open(log_path, "a", encoding="utf-8") as f:
        for folder, name in to_append:
            f.write(json.dumps({
                "folder": folder, "name": name, "status": "appended",
                "timestamp": utc_timestamp(),
            }) + "\n")

    print()
    print(f"{_pluralize(len(new_rows), 'row')} appended - folder and filename only; "
          "every other column is the cataloguer's")
    print(f"log written to {log_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # allow_abbrev=False on every parser: an old flag name must fail, not match its renamed flag as a prefix.
    parser = argparse.ArgumentParser(
        prog="ia_bulk",
        description=(
            "Validate, upload, and sync metadata for Internet Archive items from "
            "a project's Google Sheet (read live)."
        ),
        allow_abbrev=False,
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate",
        help="Validate a project's Sheet without uploading or writing anything",
        allow_abbrev=False,
    )
    validate_parser.add_argument("--project", required=True, help="Project ID from the registry")
    validate_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    validate_parser.add_argument(
        "--live",
        action="store_true",
        help="Read the project's real Sheet instead of its test Sheet",
    )
    validate_parser.add_argument(
        "--batch",
        default=None,
        help=(
            "Report only the rows whose registry-configured batch_column holds this value. "
            "Previews exactly the scope `upload --batch` would run, "
            "through the same code. Matching ignores case and surrounding whitespace."
        ),
    )

    upload_parser = subparsers.add_parser(
        "upload", help="Upload items from a project's Sheet", allow_abbrev=False
    )
    upload_parser.add_argument("--project", required=True, help="Project ID from the registry")
    upload_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    upload_parser.add_argument("--live", action="store_true", help="Target the real Sheet and the registry's real collection instead of the test Sheet and test_collection")
    upload_parser.add_argument(
        "--write-identifier",
        action="store_true",
        help="Write minted identifiers and results back to the TEST Sheet (--live always writes back)",
    )
    upload_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Upload nothing and write nothing; print the identifiers that would be minted and the cells that would be written",
    )
    upload_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    upload_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Upload at most this many items this run (must be positive). "
            "Counts rows actually in scope to upload - valid AND ready AND not already done - "
            "not every row scanned; on a Sheet with 2,900 uncatalogued rows and 150 ready ones, "
            "--limit 100 uploads 100 of the 150 ready rows, not the first 100 rows read. "
            "Combines with --chunk-size as 'this many total, batched this way': --limit 10 "
            "--chunk-size 3 uploads 10 items in chunks of 3, not 10 chunks of 3."
        ),
    )
    upload_parser.add_argument(
        "--batch",
        default=None,
        help=(
            "Upload only the rows whose registry-configured batch_column holds this value "
            "- the way a run is scoped to one theme. Only the value goes "
            "here: which column holds it is a per-project fact and lives in the registry's "
            "batch_column. Matching ignores case and surrounding whitespace. Narrows the "
            "scope before anything is counted, so --limit means 'this many OF THE BATCH'. "
            "A value no row carries is refused, never run as an empty upload."
        ),
    )
    upload_parser.add_argument(
        "--allow-over-daily-cap",
        action="store_true",
        help=(
            f"Upload more than Internet Archive's {DAILY_ITEM_CAP}/day account cap in one "
            "run. Only pass this if you know the cap has been raised for this account - "
            "otherwise the run is throttled partway through and stops mid-batch."
        ),
    )
    upload_parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE,
        help=(
            f"Items per reserve/upload/confirm batch (must be positive; "
            f"default {CHUNK_SIZE}, Internet Archive's own per-run item cap). Applied to "
            "whatever --limit leaves, not instead of it - see --limit's help for the exact "
            "combination."
        ),
    )

    sync_parser = subparsers.add_parser(
        "sync-metadata", help="Update metadata on already-uploaded items", allow_abbrev=False
    )
    sync_parser.add_argument("--project", required=True, help="Project ID from the registry")
    sync_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    sync_parser.add_argument("--live", action="store_true", help="Read the project's real Sheet and target the real, permanent items instead of the test Sheet and its zztest- rehearsal items")
    sync_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Send nothing; print the items that would be updated and which fields would go to each",
    )
    sync_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")
    sync_parser.add_argument(
        "--chunk-size",
        type=int,
        default=CHUNK_SIZE,
        help=(
            f"Rows per push/stamp batch (must be positive; default "
            f"{CHUNK_SIZE}). Each batch costs one Sheets write, and a run interrupted "
            "mid-way keeps every chunk it finished."
        ),
    )

    reconcile_parser = subparsers.add_parser(
        "reconcile-files",
        help="Find rows whose filename does not resolve against the drive and correct them",
        allow_abbrev=False,
    )
    reconcile_parser.add_argument("--project", required=True, help="Project ID from the registry")
    reconcile_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    reconcile_parser.add_argument("--live", action="store_true", help="Read and write the project's real Sheet instead of its test Sheet")
    reconcile_parser.add_argument("--dry-run", action="store_true", help="Print what would be proposed; prompt for nothing and write nothing")
    reconcile_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")

    append_parser = subparsers.add_parser(
        "append-rows",
        help="Append a skeleton row for every photo file on the drive that no row claims",
        allow_abbrev=False,
    )
    append_parser.add_argument("--project", required=True, help="Project ID from the registry")
    append_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    append_parser.add_argument("--live", action="store_true", help="Read and write the project's real Sheet instead of its test Sheet")
    append_parser.add_argument("--dry-run", action="store_true", help="Print the rows that would be appended and write nothing")
    append_parser.add_argument("--log-dir", default="logs", help="Directory to write the timestamped run log to")

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Check that this machine is set up to run the pipeline. Reads only; changes nothing",
        allow_abbrev=False,
    )
    doctor_parser.add_argument("--project", required=True, help="Project ID from the registry")
    doctor_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    doctor_parser.add_argument("--live", action="store_true", help="Check the project's real Sheet instead of its test Sheet")
    doctor_parser.add_argument("--offline", action="store_true", help="Skip the checks that need the network")

    setup_parser = subparsers.add_parser(
        "setup",
        help="Bring this machine to the state this checkout needs, then verify. Safe to re-run",
        allow_abbrev=False,
    )
    setup_parser.add_argument("--project", required=True, help="Project ID from the registry")
    setup_parser.add_argument("--registry", default=DEFAULT_REGISTRY, help="Path to the project registry JSON")
    setup_parser.add_argument("--live", action="store_true", help="Converge against the project's real Sheet instead of its test Sheet")
    setup_parser.add_argument("--offline", action="store_true", help="Skip the checks that need the network")
    setup_parser.add_argument(
        "--enable-agent",
        action="store_true",
        help=(
            "Load the hourly sync LaunchAgent for the account running this. Run it from the "
            "operating account, after the first live runs have been verified"
        ),
    )

    return parser


def start_run_output(command: str) -> None:
    """Called before a command loads anything, so a registry that will not load
    still fails under its own run's date. Line-buffered because the LaunchAgent
    sends stdout and stderr to one file, and block-buffered stdout would land
    after stderr written later. A character the console codepage lacks prints as
    an escape, as on stderr, instead of ending the run's output there."""
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(line_buffering=True, errors="backslashreplace")
    print(f"{utc_timestamp()} {command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    start_run_output(args.command)

    if args.command == "validate":
        return cmd_validate(args)
    if args.command == "upload":
        return cmd_upload(args)
    if args.command == "sync-metadata":
        return cmd_sync_metadata(args)
    if args.command == "reconcile-files":
        return cmd_reconcile_files(args)
    if args.command == "append-rows":
        return cmd_append_rows(args)
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "setup":
        return cmd_setup(args)

    parser.error(f"unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
