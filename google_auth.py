"""Service-account credentials for the Sheets API.

The Sheet must be shared, as Editor, with the key's client_email. See docs/DECISIONS.md,
"The Sheet is reached as a service account, not as a person"."""
from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request
from google.oauth2 import service_account

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Anchored to this file's directory so the working directory never changes which key is read.
_PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_SERVICE_ACCOUNT_KEY_PATH = _PROJECT_ROOT / ".ignored" / "google-service-account.json"


class AuthUnavailable(Exception):
    pass


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
    except (OSError, ValueError, AttributeError) as exc:
        # ValueError: bad JSON, missing fields, or a bad PEM; AttributeError: JSON that is not an object.
        raise AuthUnavailable(
            f"the file at {key_path} is not a readable service account key ({exc}). "
            "Replace it with the JSON key downloaded from the Google Cloud console."
        ) from exc

    try:
        credentials.refresh(Request())
    except TransportError as exc:
        raise AuthUnavailable(
            f"could not reach Google to authenticate ({exc}). This is a network "
            "problem, not a credential one - check the connection and re-run."
        ) from exc
    except RefreshError as exc:
        raise AuthUnavailable(
            f"Google rejected the service account key at {key_path} ({exc}). The key "
            "may have been deleted or disabled in the Google Cloud console - create a "
            "new one and save it there."
        ) from exc
    return credentials


def service_account_email(key_path: Path) -> str | None:
    """The address the Sheet must be shared with, or None when the key cannot say."""
    try:
        email = json.loads(key_path.read_text(encoding="utf-8")).get("client_email")
    except (OSError, ValueError, AttributeError):
        return None
    return email if isinstance(email, str) and email else None
