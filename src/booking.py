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
    r"(\d{1,2}:\d{2})\s*(?:(AM|PM)\s+)?to\s+\d{1,2}:\d{2}\s*(AM|PM)",
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
    end_ampm = m.group(3)        # e.g. "AM" or "PM" at the end

    # If start has its own AM/PM, use it; otherwise infer from end AM/PM
    ampm = (start_ampm or end_ampm or "AM").upper()

    try:
        dt = datetime.strptime(f"{target_date} {time_str} {ampm}", "%Y-%m-%d %I:%M %p")
        return dt
    except ValueError:
        return None


def _is_future_session(session_text: str, target_date: date) -> bool:
    """
    Fix 1: Return True only if the session hasn't started yet.
    If the start time can't be parsed, assume future (don't block it).
    """
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        logger.debug("Could not parse start time from: %s", session_text[:60])
        return True  # give benefit of the doubt
    return start_dt > datetime.now()


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
        _time.sleep(1)
        return True
    except Exception as e:
        logger.error("Could not load schedule page: %s", e)
        return False


def _select_day(page: Page, target_date: date) -> bool:
    """
    Click the day radio for target_date to filter the week view to that day.
    The radio inputs are visually hidden, so we use JS to click them.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    try:
        clicked = page.evaluate(f"""() => {{
            const radio = document.querySelector('.planner-date-radio-input[value="{date_str}"]');
            if (!radio) return false;
            radio.click();
            return true;
        }}""")
        if clicked:
            _time.sleep(0.5)
            return True
        logger.warning("Day radio not found for %s", date_str)
        return False
    except Exception as e:
        logger.warning("Could not click day radio for %s: %s", date_str, e)
        return False


def _find_matching_sessions(
    page: Page,
    cfg: Config,
    target_date: date,
    existing_reservations: Set[str],
) -> List[Tuple[str, Locator]]:
    """
    Return (session_name, reserve_button) for every session on the page that:
      - matches a keyword (with exclusions)
      - has a "Reserve" CTA (not Waitlist / Cancel / already-reserved)
      - hasn't already started (Fix 1)
      - isn't already in existing_reservations (Fix 2)
    """
    matches: List[Tuple[str, Locator]] = []

    try:
        entries = page.locator('[data-testid="classCell"]').all()
        logger.debug("Found %d session entries on page", len(entries))

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

                # Only book if the CTA says exactly "Reserve" — not Waitlist, Cancel, etc.
                cta = entry.locator(".card-cta").first
                if not cta.is_visible():
                    continue
                cta_text = cta.inner_text().strip()
                if cta_text.lower() != "reserve":
                    logger.debug("Skipping (CTA=%r): %s", cta_text, session_text[:60])
                    continue

                # Fix 2: skip if already reserved
                session_name = session_text.split("\n")[0].strip()
                if _already_reserved(session_name, existing_reservations):
                    logger.info("Already reserved, skipping: %s", session_name)
                    continue

                logger.info("Found bookable session: %s", session_name)
                matches.append((session_name, cta))

            except Exception:
                continue

    except Exception as e:
        logger.debug("Error scanning sessions: %s", e)

    return matches


def _confirm_booking(page: Page) -> bool:
    """Click through the confirmation modal after pressing Reserve."""
    try:
        confirm_btn = page.locator(
            'button:has-text("Confirm"), button:has-text("Complete"), '
            'button:has-text("Yes"), [class*="confirm"]'
        ).first
        confirm_btn.wait_for(state="visible", timeout=8000)
        confirm_btn.click()
        _time.sleep(1)

        success = page.locator(
            '[class*="success"], [class*="confirmation"], '
            ':has-text("confirmed"), :has-text("booked")'
        ).first
        if success.is_visible(timeout=5000):
            return True

        logger.warning("No success indicator found after confirmation.")
        return False
    except Exception as e:
        logger.warning("Confirmation step failed: %s", e)
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

        if not _select_day(page, target_date):
            logger.warning("Could not filter to day %s, proceeding anyway.", target_date)

        sessions = _find_matching_sessions(page, cfg, target_date, existing_reservations)

        if not sessions:
            logger.info("  %s — no bookable sessions found.", target_date)
            continue

        for session_name, reserve_btn in sessions:
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
            reserve_btn.click()
            _time.sleep(0.8)

            booked = _confirm_booking(page)
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
            else:
                logger.warning("  FAILED: %s — %s", target_date, session_name)

    return results
