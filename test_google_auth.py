import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from google.auth.exceptions import RefreshError, TransportError
from google.oauth2 import service_account

import google_auth
from google_auth import (
    DEFAULT_SERVICE_ACCOUNT_KEY_PATH,
    SCOPES,
    AuthUnavailable,
    load_service_account_credentials,
    service_account_email,
)

SERVICE_ACCOUNT_EMAIL = "sheets-sync@example-project.iam.gserviceaccount.com"


def _service_account_key(private_key_pem, **overrides):
    key = {
        "type": "service_account",
        "project_id": "example-project",
        "private_key_id": "key-id",
        "private_key": private_key_pem,
        "client_email": SERVICE_ACCOUNT_EMAIL,
        "client_id": "1234567890",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
    key.update(overrides)
    return key


def _write_key(path, content):
    path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")


@pytest.fixture(scope="module")
def private_key_pem():
    """A throwaway RSA key, generated once per module; generation is the slow part."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode("ascii")


def test_service_account_key_loads_and_authenticates_up_front(tmp_path, monkeypatch, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))
    refreshed = []
    monkeypatch.setattr(
        service_account.Credentials, "refresh", lambda self, request: refreshed.append(self)
    )

    credentials = load_service_account_credentials(key_path)

    assert credentials.service_account_email == SERVICE_ACCOUNT_EMAIL
    assert refreshed == [credentials]


def test_service_account_credentials_carry_the_sheets_scope(tmp_path, monkeypatch, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))
    monkeypatch.setattr(service_account.Credentials, "refresh", lambda self, request: None)

    credentials = load_service_account_credentials(key_path)

    assert credentials.scopes == SCOPES


def test_missing_service_account_key_names_the_expected_path(tmp_path):
    key_path = tmp_path / "google-service-account.json"

    with pytest.raises(AuthUnavailable, match="missing service account key") as exc:
        load_service_account_credentials(key_path)

    assert str(key_path) in str(exc.value)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("", id="empty_file"),
        pytest.param("{not valid json", id="malformed_json"),
        pytest.param("{}", id="missing_fields"),
        pytest.param("[1, 2]", id="json_list"),
        pytest.param(
            json.dumps({"installed": {"client_id": "id", "client_secret": "secret"}}),
            id="oauth_client_secret",
        ),
        pytest.param(json.dumps(_service_account_key("not a pem")), id="corrupt_private_key"),
    ],
)
def test_unusable_key_file_raises_an_actionable_error(tmp_path, content):
    key_path = tmp_path / "key.json"
    _write_key(key_path, content)

    with pytest.raises(AuthUnavailable, match="not a readable service account key") as exc:
        load_service_account_credentials(key_path)

    assert exc.value.transient is False


def test_unreadable_key_path_names_a_permissions_problem(tmp_path):
    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(tmp_path)

    assert "owner and permissions" in str(exc.value)


def test_google_rejecting_the_key_is_reported_as_a_credential_problem(
    tmp_path, monkeypatch, private_key_pem
):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))

    def _rejected(self, request):
        raise RefreshError("invalid_grant: Invalid JWT Signature.")

    monkeypatch.setattr(service_account.Credentials, "refresh", _rejected)

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert "rejected the service account key" in str(exc.value)
    assert "network problem" not in str(exc.value)
    assert "clock" in str(exc.value)
    assert exc.value.transient is False


def test_retryable_token_failure_is_reported_as_temporary(tmp_path, monkeypatch, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))

    def _unavailable(self, request):
        raise RefreshError("503 backend error", retryable=True)

    monkeypatch.setattr(service_account.Credentials, "refresh", _unavailable)

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert "temporary" in str(exc.value)
    assert "rejected" not in str(exc.value)
    assert exc.value.transient is True


class _TokenResponse:
    def __init__(self, status, body):
        self.status = status
        self.headers = {}
        self.data = body.encode("utf-8")


def _token_endpoint_answering(monkeypatch, status, response_body):
    """Fakes only the HTTP transport, so google-auth's own error classification runs."""
    calls = []

    def request(**kwargs):
        calls.append(kwargs["url"])
        return _TokenResponse(status, response_body)

    monkeypatch.setattr(google_auth, "Request", lambda: request)
    # google-auth backs off between its own retries of 500/503/504/408/429.
    monkeypatch.setattr("google.auth._exponential_backoff.time.sleep", lambda seconds: None)
    return calls


GOOGLE_FRONT_END_502_PAGE = (
    "<!DOCTYPE html><html lang=en><title>Error 502 (Server Error)!!1</title>"
    "<p><b>502.</b> That's an error.<p>The server encountered a temporary error.</html>"
)


@pytest.mark.parametrize(
    ("status", "response_body"),
    [
        pytest.param(502, GOOGLE_FRONT_END_502_PAGE, id="502_html"),
        pytest.param(502, json.dumps({"error": "backend_error"}), id="502_json"),
        pytest.param(501, "Not Implemented", id="501_plain_text"),
    ],
)
def test_a_server_error_google_auth_does_not_retry_is_still_temporary(
    tmp_path, monkeypatch, private_key_pem, status, response_body
):
    """google-auth's retry list is 500, 503, 504, 408 and 429, so a 502 comes out
    retryable=False. A 5xx is the server failing, never a verdict on the key."""
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))
    _token_endpoint_answering(monkeypatch, status, response_body)

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert exc.value.transient is True
    assert "rejected" not in str(exc.value)
    assert f"HTTP {status}" in str(exc.value)


def test_a_server_error_names_its_status_not_its_html_page(tmp_path, monkeypatch, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))
    _token_endpoint_answering(monkeypatch, 502, GOOGLE_FRONT_END_502_PAGE)

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert "<html" not in str(exc.value)


def test_a_server_error_google_auth_retried_names_its_status(tmp_path, monkeypatch, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))
    calls = _token_endpoint_answering(monkeypatch, 503, GOOGLE_FRONT_END_502_PAGE)

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert len(calls) > 1, "google-auth should have retried the 503 itself"
    assert exc.value.transient is True
    assert "HTTP 503" in str(exc.value)


def test_google_rejecting_the_key_over_http_stays_a_credential_problem(
    tmp_path, monkeypatch, private_key_pem
):
    """What the token endpoint really sends for a bad key: a 4xx with a JSON OAuth error."""
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))
    _token_endpoint_answering(
        monkeypatch,
        400,
        json.dumps({"error": "invalid_grant", "error_description": "Invalid JWT Signature."}),
    )

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert exc.value.transient is False
    assert "rejected the service account key" in str(exc.value)
    assert "invalid_grant" in str(exc.value)


def test_unreachable_google_is_reported_as_a_network_problem(tmp_path, monkeypatch, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))

    def _unreachable(self, request):
        raise TransportError("Failed to establish a new connection")

    monkeypatch.setattr(service_account.Credentials, "refresh", _unreachable)

    with pytest.raises(AuthUnavailable) as exc:
        load_service_account_credentials(key_path)

    assert "network problem" in str(exc.value)
    assert "rejected" not in str(exc.value)
    assert exc.value.transient is True


def test_default_service_account_key_path_is_anchored_to_the_project_root(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    project_root = Path(google_auth.__file__).resolve().parent

    assert DEFAULT_SERVICE_ACCOUNT_KEY_PATH == project_root / ".ignored" / "google-service-account.json"


def test_service_account_email_reads_the_address_from_the_key(tmp_path, private_key_pem):
    key_path = tmp_path / "key.json"
    _write_key(key_path, _service_account_key(private_key_pem))

    assert service_account_email(key_path) == SERVICE_ACCOUNT_EMAIL


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="missing_file"),
        pytest.param("{not valid json", id="malformed_json"),
        pytest.param("[1, 2]", id="json_list"),
        pytest.param("{}", id="no_client_email"),
    ],
)
def test_service_account_email_is_none_when_the_key_cannot_say(tmp_path, content):
    key_path = tmp_path / "key.json"
    if content is not None:
        _write_key(key_path, content)

    assert service_account_email(key_path) is None
