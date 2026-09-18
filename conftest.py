import pytest

import google_auth


@pytest.fixture(scope="session")
def _missing_service_account_key_path(tmp_path_factory):
    return tmp_path_factory.mktemp("no-real-key") / "google-service-account.json"


@pytest.fixture(autouse=True)
def _no_test_reads_the_real_service_account_key(monkeypatch, _missing_service_account_key_path):
    """No test should ever reach the real key; point the default at a path that does not exist."""
    monkeypatch.setattr(
        google_auth, "DEFAULT_SERVICE_ACCOUNT_KEY_PATH", _missing_service_account_key_path
    )
