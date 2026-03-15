"""macOS desktop notifications and Google Calendar events."""

from __future__ import annotations

import logging
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

APP_TITLE = "Lifetime Pickleball Booker"

_SCOPES = ["https://www.googleapis.com/auth/calendar"]
_ROOT = Path(__file__).parent.parent
_CREDENTIALS_PATH = _ROOT / "credentials.json"
_TOKEN_PATH = _ROOT / "token.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize(s: str) -> str:
    """Escape backslashes and double-quotes for AppleScript string literals."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _get_calendar_service():
    """Return an authenticated Google Calendar API service, running OAuth if needed."""
    creds: Optional[Credentials] = None
    if _TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_PATH), _SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not _CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    f"Google Calendar credentials not found at {_CREDENTIALS_PATH}.\n"
                    "Download OAuth 2.0 Desktop credentials from Google Cloud Console "
                    "and save as credentials.json."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDENTIALS_PATH), _SCOPES)
            creds = flow.run_local_server(port=0)
        _TOKEN_PATH.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Desktop notifications
# ---------------------------------------------------------------------------

def _notify(message: str, title: str, subtitle: str = "") -> None:
    subtitle_part = f' subtitle "{_sanitize(subtitle)}"' if subtitle else ""
    script = (
        f'display notification "{_sanitize(message)}" '
        f'with title "{_sanitize(title)}"{subtitle_part}'
    )
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.warning("osascript notification failed: %s", e.stderr.decode().strip())
    except FileNotFoundError:
        logger.warning("osascript not found — not running on macOS?")


def notify_success(message: str) -> None:
    _notify(message, title=APP_TITLE, subtitle="Booking Confirmed")


def notify_failure(message: str) -> None:
    _notify(message, title=APP_TITLE, subtitle="Booking Failed")


def notify_summary(successes: int, failures: int) -> None:
    if successes == 0 and failures == 0:
        _notify("No target slots were available.", title=APP_TITLE, subtitle="Run Complete")
    elif successes > 0:
        msg = f"Booked {successes} slot(s)."
        if failures:
            msg += f" {failures} failed."
        _notify(msg, title=APP_TITLE, subtitle="Run Complete")
    else:
        _notify(f"{failures} slot(s) could not be booked.", title=APP_TITLE, subtitle="Run Complete")


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------

def add_to_calendar(
    session_name: str,
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
    timezone: str = "America/New_York",
    location: str = "PENN 1, Lifetime Fitness",
) -> None:
    """Create a Google Calendar event with 60-min and 15-min popup reminders."""
    if start_dt is None:
        logger.warning("Cannot add calendar event — start time unknown for: %s", session_name)
        return
    if end_dt is None:
        end_dt = start_dt + timedelta(minutes=90)

    event = {
        "summary": session_name,
        "location": location,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": timezone},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": timezone},
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": 60},
                {"method": "popup", "minutes": 15},
            ],
        },
    }

    try:
        service = _get_calendar_service()
        service.events().insert(calendarId="primary", body=event).execute()
        logger.info(
            "Google Calendar event created: %s on %s",
            session_name,
            start_dt.strftime("%Y-%m-%d %I:%M %p"),
        )
    except Exception as e:
        logger.warning("Failed to create Google Calendar event: %s", e)
