"""macOS desktop notifications via osascript."""

from __future__ import annotations

import logging
import subprocess

logger = logging.getLogger(__name__)

APP_TITLE = "Lifetime Pickleball Booker"


def _notify(message: str, title: str, subtitle: str = "") -> None:
    subtitle_part = f' subtitle "{subtitle}"' if subtitle else ""
    script = f'display notification "{message}" with title "{title}"{subtitle_part}'
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        logger.warning("osascript notification failed: %s", e.stderr.decode().strip())
    except FileNotFoundError:
        logger.warning("osascript not found — not running on macOS?")


def notify_success(message: str) -> None:
    """Send a success desktop notification."""
    _notify(message, title=APP_TITLE, subtitle="Booking Confirmed")


def notify_failure(message: str) -> None:
    """Send a failure desktop notification."""
    _notify(message, title=APP_TITLE, subtitle="Booking Failed")


def notify_summary(successes: int, failures: int) -> None:
    """Send a summary notification after a booking run."""
    if successes == 0 and failures == 0:
        _notify("No target slots were available.", title=APP_TITLE, subtitle="Run Complete")
    elif successes > 0:
        msg = f"Booked {successes} slot(s)."
        if failures:
            msg += f" {failures} failed."
        _notify(msg, title=APP_TITLE, subtitle="Run Complete")
    else:
        _notify(f"{failures} slot(s) could not be booked.", title=APP_TITLE, subtitle="Run Complete")
