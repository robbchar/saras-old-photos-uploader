"""Service-account credentials for the Sheets API.

The Sheet must be shared, as Editor, with the key's client_email. See docs/DECISIONS.md,
"The Sheet is reached as a service account, not as a person"."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport import Response
from google.auth.transport.requests import Request
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Anchored to this file's directory so the working directory never changes which key is read.
_PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SERVICE_ACCOUNT_KEY_PATH = _PROJECT_ROOT / ".ignored" / "google-service-account.json"


class AuthUnavailable(Exception):
    """`transient` is True only when a retry could succeed unchanged (network, Google busy)."""

    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient


class _StatusRecordingRequest:
    """Remembers the token endpoint's last HTTP status, which RefreshError does not carry."""

    def __init__(self, request: Request) -> None:
        self._request = request
        self.last_status: int | None = None

    def __call__(self, *args: Any, **kwargs: Any) -> Response:
        response = self._request(*args, **kwargs)
        self.last_status = response.status
        return response


def _is_server_error(status: int | None) -> bool:
    # google-auth retries only 500/503/504 of these; a 502 is no more the key's fault.
    return status is not None and status >= 500


def load_service_account_credentials(key_path: Path) -> service_account.Credentials:
    """Load the key and fetch a token now, so a bad key fails before any Sheet work."""
    if not key_path.exists():
        raise AuthUnavailable(
            f"missing service account key at {key_path}. Create a JSON key for the "
            "service account in the Google Cloud console (IAM & Admin -> Service "
            "Accounts -> Keys) and save it there."
        )
    try:
        credentials = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=SCOPES
        )
    except OSError as exc:
        raise AuthUnavailable(
            f"could not read the service account key at {key_path} ({exc}). Check the "
            "file's owner and permissions."
        ) from exc
    except (ValueError, AttributeError) as exc:
        # ValueError: bad JSON, missing fields, or a bad PEM; AttributeError: JSON that is not an object.
        raise AuthUnavailable(
            f"the file at {key_path} is not a readable service account key ({exc}). "
            "Replace it with the JSON key downloaded from the Google Cloud console."
        ) from exc

    request = _StatusRecordingRequest(Request())
    try:
        credentials.refresh(request)
    except TransportError as exc:
        raise AuthUnavailable(
            f"could not reach Google to authenticate ({exc}). This is a network "
            "problem, not a credential one - check the connection and re-run.",
            transient=True,
        ) from exc
    except RefreshError as exc:
        status = request.last_status
        if exc.retryable or _is_server_error(status):
            # The status, not the body: a non-JSON error body is a whole HTML page.
            reason = f"HTTP {status}" if status is not None else str(exc)
            raise AuthUnavailable(
                f"Google could not issue a token right now ({reason}). This is a temporary "
                "problem, not a credential one - re-run in a few minutes.",
                transient=True,
            ) from exc
        raise AuthUnavailable(
            f"Google rejected the service account key at {key_path} ({exc}). Check that "
            "this computer's clock is correct; otherwise the key or the service account may "
            "have been deleted or disabled in the Google Cloud console - create a new key "
            "and save it there."
        ) from exc
    return credentials


def service_account_email(key_path: Path) -> str | None:
    """The address the Sheet must be shared with, or None when the key cannot say."""
    try:
        email = json.loads(key_path.read_text(encoding="utf-8")).get("client_email")
    except (OSError, ValueError, AttributeError):
        return None
    return email if isinstance(email, str) and email else None
