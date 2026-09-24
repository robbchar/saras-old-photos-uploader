import json
from pathlib import Path

import pytest

from project_config import REQUIRED_KEYS, ConfigError, is_placeholder_sheet_id, load_project_config

REGISTRY = {
    "collection_key": "lcps",
    "projects": {
        "sarasoldphotos": {
            "description": "Sara's donated collection",
            "mediatype": "image",
            "ia_collection": "lcpsociety",
            "sheet_id": "REAL_SHEET",
            "test_sheet_id": "TEST_SHEET",
            "sheet_tab": "Sheet1",
            "files_dir": "D:/lcps/photos",
            "file_template": "{cd}/{file_on_array}",
            "required_for_upload": ["title"],
        }
    },
}


def _registry(**overrides):
    block = {
        "mediatype": "image",
        "ia_collection": "lcpsdigitalcollection",
        "sheet_id": "real",
        "test_sheet_id": "test",
        "sheet_tab": "Sheet1",
        "files_dir": "./data",
        "file_template": "{folder}/{name}",
        "required_for_upload": ["title", "theme"],
    }
    block.update(overrides)
    return {"collection_key": "lcps", "projects": {"p": block}}


def test_load_project_config_reads_the_block():
    config = load_project_config(REGISTRY, "sarasoldphotos")

    assert config.collection_key == "lcps"
    assert config.mediatype == "image"
    assert config.file_template == "{cd}/{file_on_array}"


def test_live_and_test_runs_select_different_sheets():
    """The safety rail: there is no flag that can point a live run at the test
    Sheet, or an identifier-writing test run at the real one."""
    config = load_project_config(REGISTRY, "sarasoldphotos")

    assert config.sheet_id_for(live=True) == "REAL_SHEET"
    assert config.sheet_id_for(live=False) == "TEST_SHEET"


@pytest.mark.parametrize(
    ("sheet_id", "expected"),
    [
        ("REPLACE_WITH_REAL_SHEET_ID", True),
        ("REPLACE_WITH_NEVER_LIVE", True),
        ("1fHBL6realSheetId", False),
        ("", False),
    ],
)
def test_is_placeholder_sheet_id(sheet_id, expected):
    assert is_placeholder_sheet_id(sheet_id) is expected


@pytest.mark.parametrize(
    ("sheet_id", "test_sheet_id", "live", "expected"),
    [
        ("REPLACE_WITH_REAL_SHEET_ID", "1test", True, True),
        ("REPLACE_WITH_REAL_SHEET_ID", "1test", False, False),
        ("1real", "REPLACE_WITH_TEST_SHEET_ID", False, True),
        ("1real", "REPLACE_WITH_TEST_SHEET_ID", True, False),
    ],
)
def test_sheet_id_is_placeholder_judges_only_the_sheet_for_the_mode(sheet_id, test_sheet_id, live, expected):
    config = load_project_config(_registry(sheet_id=sheet_id, test_sheet_id=test_sheet_id), "p")

    assert config.sheet_id_is_placeholder(live) is expected


def test_unknown_project_is_rejected_by_name():
    with pytest.raises(ConfigError, match="nosuchproject"):
        load_project_config(REGISTRY, "nosuchproject")


@pytest.mark.parametrize("project_id", ["astoria-maps", "astoria_maps", "AstoriaMaps"])
def test_project_id_off_the_identifier_scheme_is_rejected(project_id):
    """Minted identifiers must parse back; a hyphen here mints ones that don't."""
    registry = {"collection_key": "lcps", "projects": {project_id: REGISTRY["projects"]["sarasoldphotos"]}}

    with pytest.raises(ConfigError, match=rf"'{project_id}'.*lowercase letters and digits only"):
        load_project_config(registry, project_id)


@pytest.mark.parametrize("collection_key", ["lcps-org", "lcps_org", "LCPS", "lcps ", " lcps"])
def test_collection_key_off_the_identifier_scheme_is_rejected(collection_key):
    """Checked raw, so a stray space fails here rather than splitting minting from validation."""
    registry = {"collection_key": collection_key, "projects": REGISTRY["projects"]}

    with pytest.raises(ConfigError, match=r"collection_key.*lowercase letters and digits only"):
        load_project_config(registry, "sarasoldphotos")


def test_sibling_project_id_off_the_identifier_scheme_is_rejected():
    """Every registered project id is checked, not just the one being run."""
    block = REGISTRY["projects"]["sarasoldphotos"]
    registry = {"collection_key": "lcps", "projects": {"sarasoldphotos": block, "astoria-maps": block}}

    with pytest.raises(ConfigError, match=r"'astoria-maps'.*lowercase letters and digits only"):
        load_project_config(registry, "sarasoldphotos")


def test_off_scheme_error_names_the_offending_characters():
    registry = {"collection_key": "Lcps ", "projects": REGISTRY["projects"]}

    with pytest.raises(ConfigError) as raised:
        load_project_config(registry, "sarasoldphotos")

    assert "found ' ', 'L'" in str(raised.value)
    assert "hyphens" not in str(raised.value)


def test_off_scheme_error_explains_hyphens_when_there_is_one():
    registry = {"collection_key": "lcps-org", "projects": REGISTRY["projects"]}

    with pytest.raises(ConfigError, match=r"found '-' \(hyphens separate an identifier's parts\)"):
        load_project_config(registry, "sarasoldphotos")


def test_missing_required_key_names_the_key_and_the_project():
    registry = {"collection_key": "lcps", "projects": {"p": {"mediatype": "image"}}}

    with pytest.raises(ConfigError, match="'p'.*sheet_id"):
        load_project_config(registry, "p")


def test_identical_sheet_ids_are_rejected():
    """Pointing both at one Sheet would let a test run write identifiers into
    the real Sheet - the exact combination the two-ID design exists to prevent."""
    registry = {
        "collection_key": "lcps",
        "projects": {
            "p": {
                **REGISTRY["projects"]["sarasoldphotos"],
                "sheet_id": "SAME",
                "test_sheet_id": "SAME",
            }
        },
    }

    with pytest.raises(ConfigError, match="must differ"):
        load_project_config(registry, "p")


def test_missing_collection_key_is_rejected():
    """collection_key is a top-level registry field, not in a project block.
    Missing it should raise ConfigError, not KeyError."""
    registry = {"projects": {"p": REGISTRY["projects"]["sarasoldphotos"]}}

    with pytest.raises(ConfigError, match="collection_key"):
        load_project_config(registry, "p")


def test_non_string_collection_key_is_rejected():
    """collection_key must be a string. Non-string values should be caught
    as ConfigError, not silently accepted."""
    registry = {
        "collection_key": 123,
        "projects": {"p": REGISTRY["projects"]["sarasoldphotos"]},
    }

    with pytest.raises(ConfigError, match="collection_key.*string"):
        load_project_config(registry, "p")


def test_empty_string_collection_key_is_rejected():
    """collection_key must be a non-empty string."""
    registry = {
        "collection_key": "   ",
        "projects": {"p": REGISTRY["projects"]["sarasoldphotos"]},
    }

    with pytest.raises(ConfigError, match="collection_key"):
        load_project_config(registry, "p")


def test_non_string_project_value_is_rejected():
    """Project block values must be strings. Non-string values should raise
    ConfigError before reaching .strip()."""
    registry = {
        "collection_key": "lcps",
        "projects": {
            "p": {
                **REGISTRY["projects"]["sarasoldphotos"],
                "mediatype": 42,
            }
        },
    }

    with pytest.raises(ConfigError, match="mediatype.*string"):
        load_project_config(registry, "p")


def test_shipped_registry_json_loads_against_the_current_required_keys():
    """Pin the registry file and REQUIRED_KEYS together so the two cannot drift
    apart silently: adding a required key without adding it to the shipped
    registry, or removing a key from the registry, both fail here.

    Deliberately asserts nothing about the *values*. Those are operational
    settings that get filled in with real Sheet IDs and collection names as the
    project is configured, so asserting placeholders would make this test fail
    the moment the tool starts being used for real."""
    registry_path = Path(__file__).parent / "projects_registry.json"
    with open(registry_path) as f:
        registry = json.load(f)

    config = load_project_config(registry, "sarasoldphotos")

    for key in REQUIRED_KEYS:
        assert getattr(config, key), f"shipped registry has an empty '{key}'"


def test_required_for_upload_is_loaded_as_a_tuple():
    config = load_project_config(_registry(), "p")
    assert config.required_for_upload == ("title", "theme")


def test_missing_required_for_upload_is_an_error():
    registry = _registry()
    del registry["projects"]["p"]["required_for_upload"]
    with pytest.raises(ConfigError) as exc:
        load_project_config(registry, "p")
    assert str(exc.value) == (
        "project 'p' is missing required registry key: required_for_upload. "
        "It lists the normalized column names a human must fill in before a "
        "row can be uploaded, e.g. [\"title\", \"theme\"]. There is "
        "deliberately no default - a project inheriting another project's "
        "readiness rules by silence is worse than stating them."
    )


def test_empty_required_for_upload_is_an_error():
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=[]), "p")
    assert str(exc.value) == "project 'p': required_for_upload must name at least one column"


def test_required_for_upload_must_be_a_list_not_a_string():
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload="title"), "p")
    assert str(exc.value) == "project 'p': required_for_upload must be a list, got 'str'"


def test_required_for_upload_entry_must_be_a_string():
    """Covers the per-entry isinstance branch (project_config.py's
    `not isinstance(name, str) or not name.strip()` check), which the
    original five tests never exercised: every entry passed to
    load_project_config was already a valid string."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=["title", 123]), "p")
    assert str(exc.value) == (
        "project 'p': every required_for_upload entry must be a non-empty "
        "string, got 123"
    )


def test_required_for_upload_entry_must_not_be_blank():
    """The other half of the same branch: a whitespace-only entry is still
    truthy, so it takes the .strip() check specifically, not just a
    non-string check, to reject it."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=["title", "   "]), "p")
    assert str(exc.value) == (
        "project 'p': every required_for_upload entry must be a non-empty "
        "string, got '   '"
    )


def test_raw_header_text_is_rejected_with_the_normalization_rule_explained():
    """The file_template lesson: an un-normalized name produced an error that
    read as 'your Sheet is wrong' when the Sheet was fine."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(required_for_upload=["Architectura Style"]), "p")
    assert str(exc.value) == (
        "project 'p': required_for_upload entry 'Architectura Style' is raw "
        "header text, not a normalized column name - use 'architectura_style'. "
        "This is the same rule file_template follows: the Sheet's headers are "
        "normalized (lowercased, punctuation dropped, spaces to underscores) "
        "before anything matches against them, so the registry must name the "
        "normalized form. Your Sheet is fine; the registry entry is not."
    )


@pytest.mark.parametrize("block", ["see other file", ["mediatype"], 42, None])
def test_a_project_block_that_is_not_an_object_is_a_config_error_not_a_traceback(block):
    """A hand-edited registry whose project value is a string or a list - a
    botched merge, a half-finished edit - used to reach block.get() and raise
    AttributeError as a bare traceback, while every neighbouring shape
    problem produces a ConfigError naming the fix. Both cmd_validate and
    upload_from_sheet call this before any Sheet I/O, so that traceback was
    the operator's first contact with the tool."""
    registry = {"collection_key": "lcps", "projects": {"p": block}}

    with pytest.raises(ConfigError) as exc:
        load_project_config(registry, "p")

    assert "project 'p' must be an object" in str(exc.value)
    assert type(block).__name__ in str(exc.value)


def test_a_projects_value_that_is_not_an_object_is_a_config_error():
    registry = {"collection_key": "lcps", "projects": ["p"]}

    with pytest.raises(ConfigError) as exc:
        load_project_config(registry, "p")

    assert "'projects' must be an object" in str(exc.value)


def test_photo_extensions_defaults_when_absent():
    """Optional-with-a-default, unlike required_for_upload which refuses to
    default. Extensions describe what a photo file IS; required_for_upload
    encodes editorial policy that should not be inherited by silence."""
    from project_config import DEFAULT_PHOTO_EXTENSIONS

    config = load_project_config(_registry(), "p")
    assert config.photo_extensions == DEFAULT_PHOTO_EXTENSIONS
    assert ".pdf" not in config.photo_extensions


def test_photo_extensions_are_normalized_to_lowercase_with_a_dot():
    config = load_project_config(_registry(photo_extensions=["JPG", ".TIFF"]), "p")
    assert config.photo_extensions == (".jpg", ".tiff")


def test_photo_extensions_must_be_a_list_of_strings():
    with pytest.raises(ConfigError, match="photo_extensions"):
        load_project_config(_registry(photo_extensions="jpg"), "p")
    with pytest.raises(ConfigError, match="photo_extensions"):
        load_project_config(_registry(photo_extensions=[1]), "p")


def test_photo_extensions_may_not_be_empty():
    """An empty list would silently exclude every file on the drive."""
    with pytest.raises(ConfigError, match="at least one"):
        load_project_config(_registry(photo_extensions=[]), "p")


def test_batch_column_defaults_to_none_when_absent():
    """Optional with NO default column name. A project that never scopes its
    runs by theme has no such column, and guessing one would make --batch
    silently match nothing rather than refuse."""
    config = load_project_config(_registry(), "p")
    assert config.batch_column is None


def test_batch_column_is_loaded_and_stripped():
    config = load_project_config(_registry(batch_column="  theme  "), "p")
    assert config.batch_column == "theme"


def test_batch_column_must_be_a_string():
    with pytest.raises(ConfigError, match="batch_column"):
        load_project_config(_registry(batch_column=["theme"]), "p")


def test_a_present_but_blank_batch_column_is_rejected():
    """Present-and-empty is a half-finished edit, not "no batch column" -
    absent already means that. Left alone it would make every --batch run
    read a column named '' and match nothing."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(batch_column="   "), "p")
    assert str(exc.value) == (
        "project 'p': batch_column must be a non-empty normalized column name, "
        "got '   '. Remove the key entirely if this project's runs are never "
        "scoped to a batch."
    )


def test_batch_column_raw_header_text_is_rejected_with_the_normalization_rule():
    """Same rule required_for_upload and file_template follow: the registry
    names the normalized column, not the Sheet's header text."""
    with pytest.raises(ConfigError) as exc:
        load_project_config(_registry(batch_column="Theme / Subject"), "p")
    assert str(exc.value) == (
        "project 'p': batch_column 'Theme / Subject' is raw header text, not a "
        "normalized column name - use 'theme_subject'. This is the same rule "
        "file_template follows: the Sheet's headers are normalized (lowercased, "
        "punctuation dropped, spaces to underscores) before anything matches "
        "against them, so the registry must name the normalized form. Your Sheet "
        "is fine; the registry entry is not."
    )


def test_the_shipped_registry_batches_this_project_by_theme():
    """The Sheet column an operator scopes a run to with --batch. Asserted
    against the shipped file because a rename there silently turns every
    --batch run into a refusal."""
    registry = json.loads(Path("projects_registry.json").read_text(encoding="utf-8"))

    config = load_project_config(registry, "sarasoldphotos")

    assert config.batch_column == "theme"


def test_log_tabs_are_off_unless_the_registry_names_them():
    """Absent means off, with no default tab name. A default would make a run
    create a tab in someone's Sheet they never asked for - and for the older
    of the two projects this pipeline serves, a Sheet nobody has looked at in
    months."""
    registry = _registry()

    config = load_project_config(registry, "p")

    assert config.upload_log_tab is None
    assert config.sync_log_tab is None


def test_log_tabs_are_read_from_the_registry():
    registry = _registry(upload_log_tab="Upload Log", sync_log_tab="Sync Log")

    config = load_project_config(registry, "p")

    assert config.upload_log_tab == "Upload Log"
    assert config.sync_log_tab == "Sync Log"


def test_a_blank_log_tab_name_is_off_rather_than_a_tab_with_no_name():
    """Emptying the value is how an operator turns mirroring off without
    editing the registry's shape. Left as "" it would reach the Sheets API as
    a request to create a tab called nothing."""
    registry = _registry(upload_log_tab="   ")

    config = load_project_config(registry, "p")

    assert config.upload_log_tab is None


def test_a_log_tab_may_not_collide_with_the_metadata_tab():
    """The one configuration mistake with a permanent cost: telemetry
    appended onto the canonical metadata columns. Refused at load, since by
    the time a run is appending it is far too late."""
    registry = _registry(upload_log_tab="Sheet1")

    with pytest.raises(ConfigError) as caught:
        load_project_config(registry, "p")

    assert "upload_log_tab" in str(caught.value)
    assert "Sheet1" in str(caught.value)
