"""projects_registry.schema.json against the registries and against
load_project_config, the authoritative check."""
import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from project_config import REQUIRED_KEYS, ConfigError, load_project_config

REPO_ROOT = Path(__file__).parent
SCHEMA_PATH = REPO_ROOT / "projects_registry.schema.json"
SHIPPED_REGISTRIES = [
    REPO_ROOT / "projects_registry.json",
    REPO_ROOT / "e2e_fixtures" / "registry.json",
]

VALID_REGISTRY = {
    "collection_key": "lcps",
    "projects": {
        "p": {
            "mediatype": "image",
            "ia_collection": "somecollection",
            "sheet_id": "REAL_SHEET",
            "test_sheet_id": "TEST_SHEET",
            "sheet_tab": "Sheet1",
            "files_dir": "./data",
            "file_template": "{folder}/{file_name}",
            "required_for_upload": ["title"],
        }
    },
}


def _schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validator() -> Draft202012Validator:
    schema = _schema()
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _schema_errors(registry: dict) -> list[str]:
    return [error.message for error in _validator().iter_errors(registry)]


def _project_schema() -> dict:
    return _schema()["$defs"]["project"]


def _registry_with(**project_changes) -> dict:
    """VALID_REGISTRY with project 'p' changed; a value of None deletes the key."""
    registry = copy.deepcopy(VALID_REGISTRY)
    block = registry["projects"]["p"]
    for key, value in project_changes.items():
        if value is None:
            block.pop(key, None)
        else:
            block[key] = value
    return registry


@pytest.mark.parametrize("registry_path", SHIPPED_REGISTRIES, ids=lambda path: path.name)
def test_shipped_registry_matches_the_schema(registry_path):
    registry = json.loads(registry_path.read_text(encoding="utf-8"))

    assert _schema_errors(registry) == []


@pytest.mark.parametrize("registry_path", SHIPPED_REGISTRIES, ids=lambda path: path.name)
def test_shipped_registry_points_editors_at_the_schema(registry_path):
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    schema_file = (registry_path.parent / registry["$schema"]).resolve()

    assert schema_file == SCHEMA_PATH.resolve()


def test_schema_requires_exactly_the_keys_the_loader_requires():
    required = set(_project_schema()["required"])

    assert required == {*REQUIRED_KEYS, "required_for_upload"}


def test_valid_registry_passes_both_schema_and_loader():
    assert _schema_errors(VALID_REGISTRY) == []
    load_project_config(VALID_REGISTRY, "p")


BROKEN_REGISTRIES = {
    "missing sheet_id": _registry_with(sheet_id=None),
    "blank files_dir": _registry_with(files_dir="   "),
    "non-string mediatype": _registry_with(mediatype=5),
    "missing required_for_upload": _registry_with(required_for_upload=None),
    "empty required_for_upload": _registry_with(required_for_upload=[]),
    "raw header in required_for_upload": _registry_with(required_for_upload=["Title"]),
    "empty photo_extensions": _registry_with(photo_extensions=[]),
    "blank batch_column": _registry_with(batch_column=""),
    "raw header batch_column": _registry_with(batch_column="Theme Name"),
    "non-string live_sheet_tab": _registry_with(live_sheet_tab=3),
    "uppercase project id": {
        "collection_key": "lcps",
        "projects": {"Photos": VALID_REGISTRY["projects"]["p"]},
    },
    "hyphenated collection_key": {**VALID_REGISTRY, "collection_key": "lc-ps"},
    "missing collection_key": {"projects": VALID_REGISTRY["projects"]},
}


@pytest.mark.parametrize(
    "registry", BROKEN_REGISTRIES.values(), ids=BROKEN_REGISTRIES.keys()
)
def test_schema_and_loader_both_reject(registry):
    project_id = next(iter(registry["projects"]))

    assert _schema_errors(registry) != []
    with pytest.raises(ConfigError):
        load_project_config(registry, project_id)


def test_schema_rejects_a_misspelled_optional_key():
    """The loader ignores unknown keys, so a typo there silently means "unset"."""
    registry = _registry_with(upload_log_tabs="Upload Log")

    assert _schema_errors(registry) != []


@pytest.mark.parametrize(
    "optional_keys",
    [
        {"description": "any text"},
        {"photo_extensions": ["jpg", ".TIF"]},
        {"batch_column": "theme"},
        {"live_sheet_tab": "Live", "test_sheet_tab": "Test"},
        {"upload_log_tab": "", "sync_log_tab": "Sync Log"},
    ],
    ids=lambda keys: ",".join(keys),
)
def test_schema_and_loader_both_accept_the_optional_keys(optional_keys):
    registry = _registry_with(**optional_keys)

    assert _schema_errors(registry) == []
    load_project_config(registry, "p")
