"""Discover available pickleball sessions and book them by keyword."""

from __future__ import annotations

import logging
import re
import time as _time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import List, Optional, Set, Tuple

from playwright.sync_api import Page

from .config import Config
from .notifier import add_to_calendar
from .utils import dismiss_cookie_popup

logger = logging.getLogger(__name__)

DAY_NAMES = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}

# "7:00 to 8:30 AM"  or  "10:00 AM to 12:00 PM"
_TIME_RE = re.compile(
    r"(\d{1,2}:\d{2})\s*(?:(AM|PM)\s+)?to\s+(\d{1,2}:\d{2})\s*(AM|PM)",
    re.IGNORECASE,
)

# "Registration will be open on March 15th at 7:00 PM"
_REG_OPENS_RE = re.compile(
    r"registration will be open on (\w+\s+\d+)(?:st|nd|rd|th)?\s+at\s+(\d{1,2}:\d{2}\s*(?:AM|PM))",
    re.IGNORECASE,
)

# JavaScript that resolves True the moment the Reserve button is clickable
_RESERVE_READY_JS = """() => {
    const btns = [...document.querySelectorAll('[data-testid="reserveButton"]')];
    return btns.some(btn =>
        btn.offsetParent !== null &&
        !btn.disabled &&
        btn.innerText.trim().toLowerCase() === 'reserve'
    );
}"""


@dataclass
class BookingResult:
    target_date: date
    session_name: str
    success: bool
    message: str
    dry_run: bool = False


# ---------------------------------------------------------------------------
# URL helpers
# ---------------------------------------------------------------------------

def _club_url_slug(club_name: str) -> str:
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


# ---------------------------------------------------------------------------
# Session filtering helpers
# ---------------------------------------------------------------------------

def _session_matches(session_text: str, cfg: Config) -> bool:
    """Return True if session_text matches a keyword and passes all exclusions."""
    text = session_text.lower()
    for excl in cfg.session_exclusions:
        if excl in text:
            return False
    for kw in cfg.session_keywords:
        if kw in text:
            return True
    return False


def _parse_session_start(session_text: str, target_date: date) -> Optional[datetime]:
    m = _TIME_RE.search(session_text)
    if not m:
        return None
    time_str, start_ampm, end_ampm = m.group(1), m.group(2), m.group(4)
    ampm = (start_ampm or end_ampm or "AM").upper()
    try:
        return datetime.strptime(f"{target_date} {time_str} {ampm}", "%Y-%m-%d %I:%M %p")
    except ValueError:
        return None


def _parse_session_end(session_text: str, target_date: date) -> Optional[datetime]:
    m = _TIME_RE.search(session_text)
    if not m:
        return None
    end_time_str, end_ampm = m.group(3), m.group(4)
    try:
        return datetime.strptime(
            f"{target_date} {end_time_str} {end_ampm.upper()}", "%Y-%m-%d %I:%M %p"
        )
    except ValueError:
        return None


def _is_in_booking_window(session_text: str, target_date: date) -> bool:
    """
    Return True if the session's booking window is now open (or will open soon).
    Booking opens at session_start - 7 days - 22 hours.
    """
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        return True  # can't determine — let the detail page decide
    return start_dt >= datetime.now() + timedelta(days=7, hours=22)


def _is_allowed_time(session_text: str, target_date: date) -> bool:
    """Return False for weekday sessions that start between 10:30 AM and 5:00 PM."""
    if target_date.weekday() >= 5:  # Saturday=5, Sunday=6
        return True
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        return True
    blocked_start = start_dt.replace(hour=10, minute=30, second=0, microsecond=0)
    blocked_end = start_dt.replace(hour=17, minute=0, second=0, microsecond=0)
    return not (blocked_start <= start_dt < blocked_end)


def _target_dates(cfg: Config) -> List[date]:
    today = date.today()
    preferred_weekdays = {DAY_NAMES[d] for d in cfg.preferred_days}
    return [
        today + timedelta(days=offset)
        for offset in range(cfg.booking_horizon_days + 1)
        if (today + timedelta(days=offset)).weekday() in preferred_weekdays
    ]


# ---------------------------------------------------------------------------
# Existing reservations
# ---------------------------------------------------------------------------

def _fetch_existing_reservations(page: Page) -> Set[str]:
    reserved: Set[str] = set()
    try:
        page.goto(_reservations_url(), wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)

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

        if not reserved:
            for el in page.locator('[class*="card"], [class*="item"]').all():
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
    name_lower = session_name.lower()
    for existing_text in existing:
        if name_lower in existing_text or existing_text in name_lower:
            return True
        if ":" in name_lower:
            distinctive = name_lower.split(":", 1)[1].strip()
            if len(distinctive) > 6 and distinctive in existing_text:
                return True
    return False


# ---------------------------------------------------------------------------
# Schedule page navigation + session discovery
# ---------------------------------------------------------------------------

def _navigate_to_date(page: Page, cfg: Config, target_date: date) -> bool:
    url = _classes_url(cfg, target_date)
    logger.info("Loading schedule for %s: %s", target_date, url)
    try:
        page.goto(url, wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)
        return True
    except Exception as e:
        logger.error("Could not load schedule page: %s", e)
        return False


def _day_column_index(page: Page, target_date: date) -> int:
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
    """Return (session_name, details_url, session_text) for every bookable session."""
    matches: List[Tuple[str, str, str]] = []

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

            if not _session_matches(session_text.lower(), cfg):
                continue

            if not _is_in_booking_window(session_text, target_date):
                logger.debug("Skipping — booking window not yet open: %s", session_text[:60])
                continue

            if not _is_allowed_time(session_text, target_date):
                logger.debug("Skipping — weekday daytime: %s", session_text[:60])
                continue

            # classLink is always present even before the booking window opens
            link = entry.locator('[data-testid="reserveLink"], [data-testid="classLink"]').first
            if not link.is_visible():
                continue
            details_url = link.get_attribute("href") or ""
            if not details_url:
                continue
            if not details_url.startswith("http"):
                details_url = "https://my.lifetime.life" + details_url

            title_el = entry.locator(".planner-entry-title").first
            if title_el.count() > 0:
                session_name = title_el.inner_text().strip()
            else:
                lines = [l.strip() for l in session_text.split("\n") if l.strip()]
                session_name = next(
                    (l for l in lines if not _TIME_RE.match(l)),
                    lines[0] if lines else session_text[:60],
                )

            if _already_reserved(session_name, existing_reservations):
                logger.info("Already reserved, skipping: %s", session_name)
                continue

            logger.info("Found bookable session: %s", session_name)
            matches.append((session_name, details_url, session_text))

        except Exception:
            continue

    return matches


# ---------------------------------------------------------------------------
# Participant selection
# ---------------------------------------------------------------------------

def _select_participant(page: Page) -> None:
    """
    Check Tim's checkbox, uncheck Mark's.
    Uses JS to set checked state + dispatch change event so Vue/React picks it up.
    Bypasses CSS visibility/disabled constraints.
    """
    for cb in page.locator('[data-testid="participantCheckBox"]').all():
        try:
            label = cb.evaluate("""el => {
                const label = el.closest('label') || el.parentElement;
                return label ? label.innerText.trim() : '';
            }""").lower()
            is_checked = cb.is_checked()

            if "tim" in label and not is_checked:
                cb.evaluate("""el => {
                    el.checked = true;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }""")
                logger.debug("Selected participant: Tim")
            elif "mark" in label and is_checked:
                cb.evaluate("""el => {
                    el.checked = false;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }""")
                logger.debug("Deselected participant: Mark")
        except Exception:
            continue


# ---------------------------------------------------------------------------
# Registration open time detection
# ---------------------------------------------------------------------------

def _get_registration_open_time(page: Page) -> Optional[datetime]:
    """
    If the page shows a 'Registration will be open on [date] at [time]' banner,
    parse and return that datetime. Returns None if banner is absent or unparseable.
    """
    try:
        banner = page.locator(':text("Registration will be open")').first
        if not banner.is_visible(timeout=500):
            return None
        text = banner.inner_text()
        m = _REG_OPENS_RE.search(text)
        if not m:
            return None
        date_str = m.group(1).strip()   # e.g. "March 15"
        time_str = m.group(2).strip()   # e.g. "7:00 PM"
        return datetime.strptime(
            f"{date_str} {datetime.now().year} {time_str}", "%B %d %Y %I:%M %p"
        )
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Class detail page booking
# ---------------------------------------------------------------------------

def _book_on_details_page(page: Page, details_url: str, poll_timeout_seconds: int = 300) -> bool:
    """
    Navigate to the class detail page early and wait on-page for the Reserve
    button to become clickable — no reloading while waiting. Playwright's
    wait_for_function polls the live DOM via MutationObserver, so we react
    within milliseconds of the button appearing.

    If the booking window opens at a known future time we log it and wait;
    if it's already open we click immediately.
    """
    try:
        page.goto(details_url, wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)

        # Pre-select Tim early in case checkboxes are already enabled
        _select_participant(page)

        deadline_mono = _time.monotonic() + poll_timeout_seconds

        # Check for a "Registration will be open on X at Y" banner
        open_time = _get_registration_open_time(page)
        if open_time is not None:
            wait_secs = (open_time - datetime.now()).total_seconds()
            if wait_secs > poll_timeout_seconds:
                logger.info(
                    "    Registration opens in %.0fs — beyond timeout, skipping.", wait_secs
                )
                return False
            if wait_secs > 0:
                logger.info(
                    "    Registration opens at %s — watching page for Reserve button "
                    "(%.1fs away)...",
                    open_time.strftime("%I:%M:%S %p"),
                    wait_secs,
                )

        # Stay on the page and watch the live DOM — no reloads.
        # wait_for_function resolves the instant the JS expression returns true,
        # giving us sub-100ms reaction time from when the button becomes available.
        remaining_ms = int((deadline_mono - _time.monotonic()) * 1000)
        logger.info("    Watching for Reserve button (up to %ds)...", poll_timeout_seconds)

        button_appeared = False
        try:
            page.wait_for_function(_RESERVE_READY_JS, timeout=remaining_ms)
            button_appeared = True
        except Exception:
            # wait_for_function timed out — do one reload as last-chance fallback
            logger.debug("    wait_for_function timed out; trying one reload.")
            page.reload(wait_until="networkidle", timeout=15000)
            dismiss_cookie_popup(page)

        # Select Tim (may have reset after reload or on registration open)
        _select_participant(page)

        # Find and click the Reserve button
        for btn in page.locator('[data-testid="reserveButton"]').all():
            try:
                if (
                    btn.is_visible()
                    and btn.is_enabled()
                    and btn.inner_text().strip().lower() == "reserve"
                ):
                    logger.info("    Reserve button available — clicking now.")
                    btn.click()
                    try:
                        page.wait_for_url(
                            lambda url: "/account/reservations" in url, timeout=10000
                        )
                        return True
                    except Exception:
                        if "/account/reservations" in page.url:
                            return True
                        logger.warning("    Unexpected URL after booking: %s", page.url)
                        return False
            except Exception:
                continue

        if not button_appeared:
            logger.warning("    Reserve button never became available within %ds.", poll_timeout_seconds)
        else:
            logger.warning("    Reserve button was detected but could not be clicked.")
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
      - find all sessions matching keywords that are bookable
      - book them all (or report in dry-run mode)
    """
    results: List[BookingResult] = []
    targets = _target_dates(cfg)

    if not targets:
        logger.info("No target dates found within the booking horizon.")
        return results

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
                message="Booking confirmed." if booked else "Booking failed.",
            ))

            if booked:
                logger.info("  SUCCESS: %s — %s", target_date, session_name)
                existing_reservations.add(session_name.lower())
                start_dt = _parse_session_start(session_text, target_date)
                end_dt = _parse_session_end(session_text, target_date)
                add_to_calendar(
                    session_name, start_dt, end_dt, timezone=cfg.calendar_timezone
                )
            else:
                logger.warning("  FAILED: %s — %s", target_date, session_name)

    return results
