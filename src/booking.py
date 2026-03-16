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
    Return True if:
      - the session hasn't started yet, AND
      - the booking window has already opened (session is within 8 days)
    Sessions more than 8 days out haven't opened for booking yet.
    """
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        return True  # can't determine — let the detail page decide
    now = datetime.now()
    booking_open_time = start_dt - timedelta(days=7, hours=22)
    # Window must be open (booking_open_time <= now) and session not yet started
    return booking_open_time <= now < start_dt


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
    test_mode: bool = False,
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

            if not test_mode:
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
# Participant selection + spinner wait
# ---------------------------------------------------------------------------

def _wait_for_spinner(page: Page, timeout_ms: int = 5000) -> None:
    """Wait for any loading spinner to disappear before interacting with the page."""
    try:
        page.wait_for_selector(
            '[class*="spinner"], [class*="loading"], [class*="loader"]',
            state="hidden",
            timeout=timeout_ms,
        )
    except Exception:
        pass  # no spinner found or already gone


def _select_participant(page: Page) -> None:
    """
    Ensure Tim is checked and Mark is unchecked.
    Only acts on checkboxes that are in the wrong state — avoids
    dispatching events that could reset a pre-selected form.
    """
    for cb in page.locator('[data-testid="participantCheckBox"]').all():
        try:
            label = cb.evaluate("""el => {
                const label = el.closest('label') || el.parentElement;
                return label ? label.innerText.trim() : '';
            }""").lower()
            is_checked = cb.is_checked()

            if "tim" in label:
                if not is_checked:
                    cb.evaluate("""el => {
                        el.checked = true;
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                    }""")
                    logger.debug("Selected participant: Tim")
                else:
                    logger.debug("Participant Tim already selected — leaving as-is")
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

def _try_click_reserve(page: Page) -> bool:
    """
    Wait for spinner, verify Tim is selected, then click Reserve if available.
    Returns True if booking confirmed, False otherwise.
    """
    _wait_for_spinner(page)
    _select_participant(page)
    # Prefer Reserve over Waitlist — collect all buttons and sort by priority
    reserve_btn = None
    waitlist_btn = None
    for btn in page.locator('[data-testid="reserveButton"]').all():
        try:
            text = btn.inner_text().strip().lower()
            if text == "reserve" and reserve_btn is None:
                reserve_btn = btn
            elif "waitlist" in text and waitlist_btn is None:
                waitlist_btn = btn
        except Exception:
            continue

    target = reserve_btn or waitlist_btn
    if target is None:
        return False

    try:
        text = target.inner_text().strip()
        logger.info("    Clicking button: '%s'", text)
        target.evaluate("el => el.click()")

        # Wait for navigation away from the class details page (up to 15s)
        try:
            page.wait_for_url(
                lambda url: "/account/reservations" in url,
                timeout=15000,
            )
        except Exception:
            pass

        logger.info("    URL after click: %s", page.url)

        # If we didn't land on the reservation page at all, something went wrong
        if "/account/reservations" not in page.url:
            logger.warning("    Unexpected URL after click: %s", page.url)
            return False

        # Look for a Finish/Done/Close button to complete the pending reservation
        finish_btn = page.locator(
            'button:has-text("Finish"), a:has-text("Finish"), '
            'button:has-text("Done"), a:has-text("Done")'
        ).first
        try:
            finish_btn.wait_for(state="visible", timeout=5000)
            logger.info("    Clicking Finish button.")
            finish_btn.evaluate("el => el.click()")
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            logger.info("    URL after Finish: %s", page.url)
        except Exception:
            # No Finish button — reservation confirmed automatically
            logger.info("    No Finish button needed — reservation confirmed.")

        return True

    except Exception as e:
        logger.warning("    _try_click_reserve error: %s", e)
        return False


def _book_on_details_page(
    page: Page,
    details_url: str,
    open_time: Optional[datetime] = None,
    poll_timeout_seconds: int = 300,
) -> bool:
    """
    Booking strategy:
    1. Navigate to the class detail page and select Tim.
    2. Sleep until 3 seconds before the booking window opens.
    3. Enter a tight reload loop with no delays — hammering the page so we catch
       the Reserve button the instant the server enables it.
    4. Click Reserve → handle Pending Reservation → click Finish.

    Starting 3s early ensures we are mid-reload at the exact open moment,
    not starting a reload after it. No reactive wait — Lifetime's pages are
    server-rendered and do not update the button state without a reload.
    """
    try:
        page.goto(details_url, wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)
        _select_participant(page)

        # If the window is already open, book immediately
        if _try_click_reserve(page):
            return True

        if open_time is not None:
            wait_secs = (open_time - datetime.now()).total_seconds()
            if wait_secs > poll_timeout_seconds:
                logger.info(
                    "    Registration opens in %.0fs — beyond poll timeout, skipping.",
                    wait_secs,
                )
                return False

            # Sleep until 3 seconds before open — wake up early so a reload is
            # already in flight the moment the server flips to "Reserve"
            wake_secs = max(0.0, wait_secs - 3.0)
            if wake_secs > 0:
                logger.info(
                    "    Sleeping %.0fs — waking at %s (3s before open).",
                    wake_secs,
                    (open_time - timedelta(seconds=3)).strftime("%I:%M:%S %p"),
                )
                _time.sleep(wake_secs)
            logger.info(
                "    Rapid reload loop started — open time %s.",
                open_time.strftime("%I:%M:%S %p"),
            )

        # Tight reload loop — no sleep between attempts.
        # Stop after 30s past open_time; if Reserve hasn't appeared by then it won't.
        deadline_mono = _time.monotonic() + 30
        attempt = 0
        while _time.monotonic() < deadline_mono:
            attempt += 1
            page.goto(details_url, wait_until="domcontentloaded", timeout=8000)
            logger.debug("    Reload #%d @ %s", attempt, datetime.now().strftime("%H:%M:%S.%f")[:-3])
            if _try_click_reserve(page):
                logger.info("    Booked on reload #%d.", attempt)
                return True

        logger.warning("    Reserve button did not appear within 30s of open time.")
        return False

    except Exception as e:
        logger.warning("Details-page booking failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main booking loop
# ---------------------------------------------------------------------------

def book_slots(page: Page, cfg: Config, dry_run: bool = False, test_mode: bool = False) -> List[BookingResult]:
    """
    Two-phase execution:

    Phase 1 — Discovery (fast, schedule pages only):
      Scan every target date, collect all bookable sessions and their open times.
      The browser then goes completely idle — no further requests are made.

    Phase 2 — Sleep until 60s before the earliest registration opens, then book:
      Navigate to each class detail page and use wait_for_function to react the
      millisecond the Reserve button becomes clickable.
    """
    results: List[BookingResult] = []
    targets = _target_dates(cfg)

    if not targets:
        logger.info("No target dates found within the booking horizon.")
        return results

    # ------------------------------------------------------------------
    # Phase 1: Discovery
    # ------------------------------------------------------------------
    existing_reservations = _fetch_existing_reservations(page)
    all_sessions: List[Tuple[date, str, str, str]] = []  # (date, name, url, text)

    for target_date in targets:
        logger.info("Checking %s (%s)...", target_date, target_date.strftime("%A"))

        if not _navigate_to_date(page, cfg, target_date):
            continue

        sessions = _find_matching_sessions(page, cfg, target_date, existing_reservations, test_mode=test_mode)

        if not sessions:
            logger.info("  %s — no bookable sessions found.", target_date)
            continue

        for session_name, details_url, session_text in sessions:
            all_sessions.append((target_date, session_name, details_url, session_text))
            if test_mode:
                break  # only need the first match for an e2e test

        if test_mode and all_sessions:
            break  # stop scanning once we have one session to test with

    if not all_sessions:
        return results

    # Dry-run: report and exit without booking
    if dry_run:
        for target_date, session_name, _, _ in all_sessions:
            results.append(BookingResult(
                target_date=target_date,
                session_name=session_name,
                success=True,
                message="[DRY RUN] Session found but not booked.",
                dry_run=True,
            ))
            logger.info("  [DRY RUN] %s — available: %s", target_date, session_name)
        return results

    # ------------------------------------------------------------------
    # Sleep until 60 seconds before the earliest registration opens.
    # Registration for a session opens at: session_start - 7 days - 22 hours.
    # The browser is completely idle during this wait — no requests made.
    # ------------------------------------------------------------------
    open_times = []
    for target_date, _, _, session_text in all_sessions:
        start_dt = _parse_session_start(session_text, target_date)
        if start_dt is not None:
            open_times.append(start_dt - timedelta(days=7, hours=22))

    if open_times:
        earliest_open = min(open_times)
        wake_time = earliest_open - timedelta(seconds=60)
        wait_secs = (wake_time - datetime.now()).total_seconds()

        if wait_secs > 0:
            logger.info(
                "Discovery complete. Registration opens at %s. "
                "Browser idle — sleeping %.0fs until %s (60s early).",
                earliest_open.strftime("%Y-%m-%d %I:%M:%S %p"),
                wait_secs,
                wake_time.strftime("%I:%M:%S %p"),
            )
            _time.sleep(wait_secs)
            logger.info("Waking up — navigating to class detail pages now.")

    # ------------------------------------------------------------------
    # Phase 2: Booking
    # ------------------------------------------------------------------
    for target_date, session_name, details_url, session_text in all_sessions:
        logger.info("  Booking %s — %s...", target_date, session_name)
        start_dt = _parse_session_start(session_text, target_date)
        open_time = (start_dt - timedelta(days=7, hours=22)) if start_dt else None
        booked = _book_on_details_page(page, details_url, open_time=open_time)
        results.append(BookingResult(
            target_date=target_date,
            session_name=session_name,
            success=booked,
            message="Booking confirmed." if booked else "Booking failed.",
        ))

        if booked:
            logger.info("  SUCCESS: %s — %s", target_date, session_name)
            existing_reservations.add(session_name.lower())
            end_dt = _parse_session_end(session_text, target_date)
            add_to_calendar(session_name, start_dt, end_dt, timezone=cfg.calendar_timezone)
        else:
            logger.warning("  FAILED: %s — %s", target_date, session_name)

    return results
