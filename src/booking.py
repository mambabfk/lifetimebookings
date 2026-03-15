"""Discover available pickleball sessions and book them by keyword."""

from __future__ import annotations

import logging
import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Set, Tuple

from playwright.sync_api import Locator, Page

from .config import Config
from .notifier import add_to_calendar
from .utils import dismiss_cookie_popup

logger = logging.getLogger(__name__)

DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Time pattern: "7:00 to 8:30 AM" or "10:00 AM to 12:00 PM"
_TIME_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*(?:(AM|PM)\s+)?to\s+(\d{1,2}:\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)


@dataclass
class BookingResult:
    target_date: date
    session_name: str
    success: bool
    message: str
    dry_run: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _club_url_slug(club_name: str) -> str:
    """Convert 'PENN 1' -> 'penn-1' for use in Lifetime URLs."""
    return club_name.lower().replace(" ", "-")


def _classes_url(cfg: Config, target_date: date) -> str:
    slug = _club_url_slug(cfg.club_name)
    date_str = target_date.strftime("%Y-%m-%d")
    return (
        f"https://my.lifetime.life/clubs/ny/{slug}/classes.html"
        f"?teamMemberView=true&mode=week&selectedDate={date_str}&interest=Pickleball"
    )


def _reservations_url() -> str:
    return "https://my.lifetime.life/account/my-reservations.html"


def _session_matches(session_text: str, cfg: Config) -> bool:
    """Return True if session_text matches a keyword and is not excluded."""
    text = session_text.lower()
    for excl in cfg.session_exclusions:
        if excl in text:
            return False
    for kw in cfg.session_keywords:
        if kw in text:
            return True
    return False


def _parse_session_start(session_text: str, target_date: date) -> Optional[datetime]:
    """
    Parse the session start time from text like '7:00 to 8:30 AM' or '10:00 AM to 12:00 PM'.
    Returns a datetime combining target_date + start time, or None if unparseable.
    """
    m = _TIME_RE.search(session_text)
    if not m:
        return None

    time_str = m.group(1)        # e.g. "7:00" or "10:00"
    start_ampm = m.group(2)      # e.g. "AM" if written as "10:00 AM to ..."
    end_ampm = m.group(4)        # e.g. "AM" or "PM" at the end

    # If start has its own AM/PM, use it; otherwise infer from end AM/PM
    ampm = (start_ampm or end_ampm or "AM").upper()

    try:
        dt = datetime.strptime(f"{target_date} {time_str} {ampm}", "%Y-%m-%d %I:%M %p")
        return dt
    except ValueError:
        return None


def _parse_session_end(session_text: str, target_date: date) -> Optional[datetime]:
    """
    Parse the session end time from text like '7:00 to 8:30 AM' or '10:00 AM to 12:00 PM'.
    Returns a datetime combining target_date + end time, or None if unparseable.
    """
    m = _TIME_RE.search(session_text)
    if not m:
        return None

    end_time_str = m.group(3)    # e.g. "8:30" or "12:00"
    end_ampm = m.group(4)        # e.g. "AM" or "PM"

    try:
        dt = datetime.strptime(f"{target_date} {end_time_str} {end_ampm.upper()}", "%Y-%m-%d %I:%M %p")
        return dt
    except ValueError:
        return None


def _is_future_session(session_text: str, target_date: date) -> bool:
    """
    Return True only if the session starts at least 2 hours from now.
    If the start time can't be parsed, assume eligible (don't block it).
    """
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        logger.debug("Could not parse start time from: %s", session_text[:60])
        return True  # give benefit of the doubt
    return start_dt >= datetime.now() + timedelta(days=7, hours=22)


def _is_allowed_time(session_text: str, target_date: date) -> bool:
    """
    Return False for weekday (Mon-Fri) sessions that start between 10:30 AM and 5:00 PM.
    Weekend sessions and sessions outside that window are always allowed.
    If the start time can't be parsed, assume allowed.
    """
    if target_date.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        return True
    blocked_start = start_dt.replace(hour=10, minute=30, second=0, microsecond=0)
    blocked_end = start_dt.replace(hour=17, minute=0, second=0, microsecond=0)
    return not (blocked_start <= start_dt < blocked_end)


def _target_dates(cfg: Config) -> List[date]:
    """Return dates within the booking horizon that fall on a preferred day."""
    today = date.today()
    preferred_weekdays = {DAY_NAMES[d] for d in cfg.preferred_days}
    dates = []
    for offset in range(cfg.booking_horizon_days + 1):
        candidate = today + timedelta(days=offset)
        if candidate.weekday() in preferred_weekdays:
            dates.append(candidate)
    return dates


# ---------------------------------------------------------------------------
# Fix 2: Fetch existing reservations
# ---------------------------------------------------------------------------

def _fetch_existing_reservations(page: Page) -> Set[str]:
    """
    Navigate to the My Reservations page and return a set of session name
    substrings that are already booked, for duplicate detection.

    The set contains lowercased session title tokens so we can do a substring
    match against session cards we find on the schedule page.
    """
    reserved: Set[str] = set()
    try:
        page.goto(_reservations_url(), wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)
        _time.sleep(1)

        # Grab all reservation entries — selector TBD after inspecting the page
        entries = page.locator(
            '[data-testid*="reservation"], [class*="reservation-item"], '
            '[class*="my-reservation"], [class*="upcoming-reservation"]'
        ).all()

        for el in entries:
            try:
                text = el.inner_text().strip().lower()
                if text:
                    reserved.add(text)
            except Exception:
                continue

        # Fallback: grab any text that looks like a session title
        if not reserved:
            cards = page.locator('[class*="card"], [class*="item"]').all()
            for el in cards:
                try:
                    text = el.inner_text().strip().lower()
                    if "pickleball" in text and len(text) < 300:
                        reserved.add(text)
                except Exception:
                    continue

        logger.info("Found %d existing reservation(s).", len(reserved))
    except Exception as e:
        logger.warning("Could not fetch existing reservations: %s", e)

    return reserved


def _already_reserved(session_name: str, existing: Set[str]) -> bool:
    """
    Fix 2: Return True if session_name appears to be in the existing reservations set.
    Uses substring matching since the reservation page may show slightly different text.
    """
    name_lower = session_name.lower()
    # Check if any word chunk (>8 chars) of session_name appears in an existing entry
    for existing_text in existing:
        if name_lower in existing_text or existing_text in name_lower:
            return True
        # Partial match on the distinctive part of the title (after the colon)
        if ":" in name_lower:
            distinctive = name_lower.split(":", 1)[1].strip()
            if len(distinctive) > 6 and distinctive in existing_text:
                return True
    return False


# ---------------------------------------------------------------------------
# Page navigation + session discovery
# ---------------------------------------------------------------------------

def _navigate_to_date(page: Page, cfg: Config, target_date: date) -> bool:
    url = _classes_url(cfg, target_date)
    logger.info("Loading schedule for %s: %s", target_date, url)
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)
        _time.sleep(1)
        return True
    except Exception as e:
        logger.error("Could not load schedule page: %s", e)
        return False


def _day_column_index(page: Page, target_date: date) -> int:
    """
    Return the 0-based index of the .day column for target_date by matching
    the radio input values, which are ordered the same as the .day divs.
    Returns -1 if not found.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    return page.evaluate(f"""() => {{
        const radios = [...document.querySelectorAll('.planner-date-radio-input')];
        return radios.findIndex(r => r.value === '{date_str}');
    }}""")


def _find_matching_sessions(
    page: Page,
    cfg: Config,
    target_date: date,
    existing_reservations: Set[str],
) -> List[Tuple[str, str, str]]:
    """
    Return (session_name, details_url, session_text) for every session on the page that:
      - belongs to the target_date day column
      - matches a keyword (with exclusions)
      - has a "Reserve" CTA link (not Waitlist / Cancel)
      - hasn't already started (Fix 1)
      - isn't already in existing_reservations (Fix 2)
    """
    matches: List[Tuple[str, str, str]] = []

    # Scope to the correct day column so we never pick up sessions from other days
    day_idx = _day_column_index(page, target_date)
    if day_idx == -1:
        logger.warning("Day column not found for %s — skipping", target_date)
        return matches

    day_col = page.locator(".calendar .day").nth(day_idx)
    entries = day_col.locator('[data-testid="classCell"]').all()
    logger.debug("Day column %d for %s: %d entries", day_idx, target_date, len(entries))

    for entry in entries:
        try:
            session_text = entry.inner_text().strip()
            session_lower = session_text.lower()

            # Keyword match
            if not _session_matches(session_lower, cfg):
                continue

            # Fix 1: skip sessions that have already started
            if not _is_future_session(session_text, target_date):
                logger.debug("Skipping past session: %s", session_text[:60])
                continue

            # Skip weekday sessions between 10:30 AM and 5:00 PM
            if not _is_allowed_time(session_text, target_date):
                logger.debug("Skipping daytime weekday session: %s", session_text[:60])
                continue

            # Get details URL from reserveLink or classLink (classLink is always
            # present, even before the booking window opens — lets us pre-navigate)
            link = entry.locator('[data-testid="reserveLink"], [data-testid="classLink"]').first
            if not link.is_visible():
                continue
            details_url = link.get_attribute("href") or ""
            if not details_url:
                continue
            if not details_url.startswith("http"):
                details_url = "https://my.lifetime.life" + details_url

            # Extract session title
            title_el = entry.locator(".planner-entry-title").first
            if title_el.count() > 0:
                session_name = title_el.inner_text().strip()
            else:
                lines = [l.strip() for l in session_text.split("\n") if l.strip()]
                session_name = next((l for l in lines if not _TIME_RE.match(l)), lines[0] if lines else session_text[:60])

            # Fix 2: skip if already reserved
            if _already_reserved(session_name, existing_reservations):
                logger.info("Already reserved, skipping: %s", session_name)
                continue

            logger.info("Found bookable session: %s", session_name)
            matches.append((session_name, details_url, session_text))

        except Exception:
            continue

    return matches


def _book_on_details_page(page: Page, details_url: str, poll_timeout_seconds: int = 300) -> bool:
    """
    Navigate to the class details page, wait for the Reserve button to become
    available (polls up to poll_timeout_seconds), then click it immediately.

    Pre-navigating here before the booking window opens means we're waiting at
    the door — the moment "Reserve" appears we click it, beating bots that
    navigate from scratch.
    """
    try:
        page.goto(details_url, wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)
        _time.sleep(0.5)

        # Ensure Tim (first participant) is checked
        checkboxes = page.locator('[data-testid="participantCheckBox"]').all()
        if checkboxes and not any(cb.is_checked() for cb in checkboxes):
            checkboxes[0].check()
            _time.sleep(0.2)

        # Poll for a visible "Reserve" button (not "Add to Waitlist")
        # Reload every 2 seconds to pick up server-side state changes
        deadline = _time.monotonic() + poll_timeout_seconds
        logger.info("    Waiting for Reserve button (up to %ds)...", poll_timeout_seconds)

        while _time.monotonic() < deadline:
            btns = page.locator('[data-testid="reserveButton"]').all()
            for btn in btns:
                try:
                    if btn.is_visible() and btn.is_enabled() and btn.inner_text().strip().lower() == "reserve":
                        logger.info("    Reserve button available — clicking now.")
                        btn.click()
                        _time.sleep(2)
                        if "/account/reservations" in page.url:
                            return True
                        logger.warning("Unexpected URL after booking: %s", page.url)
                        return False
                except Exception:
                    continue

            # Not available yet — reload and try again
            _time.sleep(2)
            page.reload(wait_until="networkidle", timeout=15000)
            dismiss_cookie_popup(page)

        logger.warning("Reserve button never became available within %ds.", poll_timeout_seconds)
        return False

    except Exception as e:
        logger.warning("Details-page booking failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main booking loop
# ---------------------------------------------------------------------------

def book_slots(page: Page, cfg: Config, dry_run: bool = False) -> List[BookingResult]:
    """
    For each target date within the horizon:
      - load the schedule page
      - select that specific day
      - find all sessions matching keywords that are future + not already reserved
      - book them all (or report in dry-run mode)
    """
    results: List[BookingResult] = []
    targets = _target_dates(cfg)

    if not targets:
        logger.info("No target dates found within the booking horizon.")
        return results

    # Fix 2: fetch existing reservations once up front
    existing_reservations = _fetch_existing_reservations(page)

    for target_date in targets:
        logger.info("Checking %s (%s)...", target_date, target_date.strftime("%A"))

        if not _navigate_to_date(page, cfg, target_date):
            continue

        sessions = _find_matching_sessions(page, cfg, target_date, existing_reservations)

        if not sessions:
            logger.info("  %s — no bookable sessions found.", target_date)
            continue

        for session_name, details_url, session_text in sessions:
            if dry_run:
                results.append(BookingResult(
                    target_date=target_date,
                    session_name=session_name,
                    success=True,
                    message="[DRY RUN] Session found but not booked.",
                    dry_run=True,
                ))
                logger.info("  [DRY RUN] %s — available: %s", target_date, session_name)
                continue

            logger.info("  Booking %s — %s...", target_date, session_name)
            booked = _book_on_details_page(page, details_url)
            results.append(BookingResult(
                target_date=target_date,
                session_name=session_name,
                success=booked,
                message="Booking confirmed." if booked else "Booking failed after confirmation.",
            ))

            if booked:
                logger.info("  SUCCESS: %s — %s", target_date, session_name)
                # Add to known reservations so we don't double-book within this run
                existing_reservations.add(session_name.lower())
                # Add to Google Calendar
                start_dt = _parse_session_start(session_text, target_date)
                end_dt = _parse_session_end(session_text, target_date)
                add_to_calendar(session_name, start_dt, end_dt, timezone=cfg.calendar_timezone)
            else:
                logger.warning("  FAILED: %s — %s", target_date, session_name)

    return results
