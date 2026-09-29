import re

import app_version

SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")


def test_app_version_is_plain_semver():
    assert SEMVER.fullmatch(app_version.APP_VERSION)
