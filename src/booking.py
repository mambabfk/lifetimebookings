"""Discover available pickleball slots and book them."""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional

from playwright.sync_api import Page

from .config import Config

logger = logging.getLogger(__name__)

# Lifetime's court booking lives under this base URL.
# NOTE: The exact path/query params must be confirmed by inspecting the live site
# while logged in.  Navigate to the court booking page and copy the URL here.
BOOKING_BASE_URL = "https://my.lifetime.life/classes/court-reservations.html"

DAY_NAMES = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


@dataclass
class BookingResult:
    target_date: date
    target_time: str
    success: bool
    message: str
    dry_run: bool = False


def _random_delay(lo: float = 0.5, hi: float = 1.5) -> None:
    time.sleep(random.uniform(lo, hi))


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


def _navigate_to_booking_page(page: Page, cfg: Config) -> bool:
    """
    Navigate to the court reservation page for the configured sport/club.

    TODO: Once you've identified the exact URL structure for your club,
    update BOOKING_BASE_URL above and refine the selectors below.
    """
    logger.info("Navigating to court booking page...")
    try:
        page.goto(BOOKING_BASE_URL, wait_until="networkidle", timeout=20000)
        _random_delay()
        return True
    except Exception as e:
        logger.error("Could not reach booking page: %s", e)
        return False


def _select_club(page: Page, club_name: str) -> bool:
    """
    Select the target club from a dropdown or filter if the page shows multiple clubs.

    IMPORTANT: Selectors here are placeholders — inspect the live page and update.
    """
    try:
        # Try a club/location filter dropdown
        club_selector = '[data-testid="club-filter"], select[name*="club"], select[id*="club"], select[id*="location"]'
        if page.locator(club_selector).count() > 0:
            page.select_option(club_selector, label=club_name)
            _random_delay()
            logger.debug("Selected club: %s", club_name)
        else:
            # Some pages show club tabs or buttons
            club_btn = page.locator(f'button:has-text("{club_name}"), [data-club*="{club_name}"]').first
            if club_btn.is_visible():
                club_btn.click()
                _random_delay()
                logger.debug("Clicked club button: %s", club_name)
        return True
    except Exception as e:
        logger.warning("Could not select club '%s': %s", club_name, e)
        return False


def _select_date(page: Page, target_date: date) -> bool:
    """
    Navigate the calendar/date picker to the target date.

    IMPORTANT: Selectors here are placeholders — inspect the live page and update.
    """
    date_str = target_date.strftime("%Y-%m-%d")
    label_str = target_date.strftime("%-m/%-d/%Y")  # e.g. 3/15/2026
    try:
        # Look for a date input or calendar day cell
        date_input = page.locator('input[type="date"]').first
        if date_input.is_visible():
            date_input.fill(date_str)
            _random_delay()
            return True

        # Try clicking a calendar cell by aria-label or data-date
        day_cell = page.locator(
            f'[data-date="{date_str}"], [aria-label*="{label_str}"], td:has-text("{target_date.day}")'
        ).first
        if day_cell.is_visible():
            day_cell.click()
            _random_delay()
            return True

        logger.warning("Could not find date picker element for %s", date_str)
        return False
    except Exception as e:
        logger.warning("Could not select date %s: %s", date_str, e)
        return False


def _find_slot(page: Page, target_time: str, cfg: Config) -> Optional[object]:
    """
    Scan the page for an available slot at target_time for the configured sport.

    Returns the Playwright Locator for the booking button if found, else None.

    IMPORTANT: The selectors below are placeholders.  You need to:
    1. Log in manually and navigate to the booking page.
    2. Open DevTools → Inspector, find the time slot rows and the "Book" button.
    3. Replace the selectors below with what you find.
    """
    # Normalize time for display matching: "07:00" → "7:00 AM"
    hour, minute = map(int, target_time.split(":"))
    am_pm = "AM" if hour < 12 else "PM"
    display_hour = hour if hour <= 12 else hour - 12
    display_hour = 12 if display_hour == 0 else display_hour
    time_display_variants = [
        f"{display_hour}:{minute:02d} {am_pm}",
        f"{display_hour}:{minute:02d}{am_pm}",
        target_time,
    ]

    sport = cfg.sport.lower()

    try:
        # Look for rows/cards that mention the sport and the time
        for time_str in time_display_variants:
            # Slot container: adjust selector after inspecting the real page
            slot = page.locator(
                f'[class*="slot"]:has-text("{time_str}"), '
                f'[class*="reservation"]:has-text("{time_str}"), '
                f'tr:has-text("{time_str}")'
            ).first

            if not slot.is_visible(timeout=2000):
                continue

            # Check it's for the right sport
            slot_text = slot.inner_text().lower()
            if sport not in slot_text and "court" not in slot_text:
                continue

            # Look for a bookable/available button within this slot
            book_btn = slot.locator(
                'button:has-text("Book"), button:has-text("Reserve"), '
                'a:has-text("Book"), [class*="book-btn"]'
            ).first

            if book_btn.is_visible():
                logger.info("Found available slot at %s", time_str)
                return book_btn

        logger.debug("No available slot found at %s", target_time)
        return None

    except Exception as e:
        logger.debug("Error searching for slot at %s: %s", target_time, e)
        return None


def _confirm_booking(page: Page) -> bool:
    """
    Click through any confirmation dialog that appears after pressing Book.

    IMPORTANT: Update selectors after inspecting the live confirmation modal.
    """
    try:
        confirm_btn = page.locator(
            'button:has-text("Confirm"), button:has-text("Complete"), '
            'button:has-text("Yes"), [class*="confirm"]'
        ).first
        confirm_btn.wait_for(state="visible", timeout=8000)
        _random_delay(0.3, 0.7)
        confirm_btn.click()
        _random_delay(1.0, 2.0)

        # Check for a success indicator
        success = page.locator(
            '[class*="success"], [class*="confirmation"], '
            ':has-text("confirmed"), :has-text("booked")'
        ).first
        if success.is_visible(timeout=5000):
            return True

        logger.warning("No success indicator found after confirmation click.")
        return False
    except Exception as e:
        logger.warning("Confirmation step failed: %s", e)
        return False


def book_slots(page: Page, cfg: Config, dry_run: bool = False) -> List[BookingResult]:
    """
    Main booking loop: iterate target dates × preferred times, book each available slot.
    """
    results: List[BookingResult] = []
    targets = _target_dates(cfg)

    if not targets:
        logger.info("No target dates found within the booking horizon.")
        return results

    if not _navigate_to_booking_page(page, cfg):
        return results

    for target_date in targets:
        logger.info("Checking %s (%s)...", target_date, target_date.strftime("%A"))

        # Re-navigate for each date to get a fresh view
        if not _navigate_to_booking_page(page, cfg):
            continue
        _select_club(page, cfg.club_name)
        if not _select_date(page, target_date):
            logger.warning("Skipping %s — could not set date.", target_date)
            continue

        _random_delay()

        for target_time in cfg.preferred_times:
            book_btn = _find_slot(page, target_time, cfg)

            if book_btn is None:
                result = BookingResult(
                    target_date=target_date,
                    target_time=target_time,
                    success=False,
                    message="No available slot found.",
                    dry_run=dry_run,
                )
                results.append(result)
                logger.info("  %s @ %s — not available", target_date, target_time)
                continue

            if dry_run:
                result = BookingResult(
                    target_date=target_date,
                    target_time=target_time,
                    success=True,
                    message="[DRY RUN] Slot found but not booked.",
                    dry_run=True,
                )
                results.append(result)
                logger.info("  %s @ %s — [DRY RUN] available", target_date, target_time)
                continue

            # Attempt to book
            logger.info("  Booking %s @ %s...", target_date, target_time)
            _random_delay(0.5, 1.0)
            book_btn.click()
            _random_delay()

            booked = _confirm_booking(page)
            result = BookingResult(
                target_date=target_date,
                target_time=target_time,
                success=booked,
                message="Booking confirmed." if booked else "Booking failed after confirmation step.",
                dry_run=False,
            )
            results.append(result)

            if booked:
                logger.info("  SUCCESS: %s @ %s booked.", target_date, target_time)
            else:
                logger.warning("  FAILED: %s @ %s booking failed.", target_date, target_time)

            _random_delay(1.0, 2.0)

    return results
