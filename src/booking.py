"""Discover and book pickleball sessions on Lifetime Fitness."""

from __future__ import annotations

import logging
import re
import subprocess
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
    try:
        return datetime.strptime(
            f"{target_date} {m.group(3)} {m.group(4).upper()}", "%Y-%m-%d %I:%M %p"
        )
    except ValueError:
        return None


def _session_open_time(session_text: str, target_date: date) -> Optional[datetime]:
    """Return the datetime when booking opens: session_start - 7d22h."""
    start = _parse_session_start(session_text, target_date)
    return (start - timedelta(days=7, hours=22)) if start else None


def _is_in_booking_window(session_text: str, target_date: date) -> bool:
    """True if the booking window is already open and the session hasn't started."""
    start_dt = _parse_session_start(session_text, target_date)
    if start_dt is None:
        return True
    now = datetime.now()
    booking_open_time = start_dt - timedelta(days=7, hours=22)
    return booking_open_time <= now < start_dt


def _is_allowed_time(session_text: str, target_date: date) -> bool:
    """False for weekday sessions starting between 10:30 AM and 5:00 PM."""
    if target_date.weekday() >= 5:
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
        for el in page.locator(
            '[data-testid*="reservation"], [class*="reservation-item"], '
            '[class*="my-reservation"], [class*="upcoming-reservation"]'
        ).all():
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
    """Ensure Tim is checked and Mark is unchecked."""
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
                    logger.debug("Participant Tim already selected.")
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
# Login check
# ---------------------------------------------------------------------------

def _is_logged_in(page: Page) -> bool:
    """
    Navigate to the reservations page and confirm the session is fully authenticated.
    Checks both URL (no redirect to /login) and absence of a 'Log In' button,
    catching partially-expired sessions that pass the URL check but fail on class pages.
    """
    try:
        page.goto(_reservations_url(), wait_until="networkidle", timeout=15000)
        dismiss_cookie_popup(page)
        if "login" in page.url.lower():
            return False
        login_btn = page.locator('a:has-text("Log In"), button:has-text("Log In")').first
        return not login_btn.is_visible(timeout=2000)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# macOS notification
# ---------------------------------------------------------------------------

def _notify_macos(session_name: str, target_date: date, booked: bool = True) -> None:
    status = "Booked" if booked else "Waitlisted"
    msg = f"{target_date.strftime('%a %b %-d')} — {session_name}"
    title = f"Lifetime {status} ✓"
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{msg}" with title "{title}" sound name "Glass"'],
            timeout=3,
        )
        logger.info("Notification sent: %s — %s", title, msg)
    except Exception as e:
        logger.debug("Notification failed: %s", e)


# ---------------------------------------------------------------------------
# Steps 9–11: Reserve → Finish (called after landing on detail page)
# ---------------------------------------------------------------------------

def _execute_booking(page: Page, session_name: str, target_date: date) -> bool:
    """
    Step 9:  Wait for detail page load, select Tim, click Reserve or Add to Waitlist.
    Step 10: Wait for pending reservation page, click Finish or Join Waitlist.
    Step 11: Confirm #registrationConfirmation, send macOS notification.

    All clicks use JavaScript (no mouse simulation overhead).
    """
    try:
        # Step 9 — detail page
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        _select_participant(page)

        reserve_btn = page.wait_for_selector(
            '[data-testid="reserveButton"]',
            state="attached",
            timeout=8000,
        )
        btn_text = reserve_btn.inner_text().strip()
        logger.info("    Step 9 — clicking: '%s'", btn_text)
        reserve_btn.evaluate("el => el.click()")

        # Step 10 — pending reservation page
        page.wait_for_url(lambda url: "/account/reservations" in url, timeout=15000)
        logger.info("    Step 10 — reservation page: %s", page.url)

        confirm_btn = page.wait_for_selector(
            'button:has-text("Finish"), a:has-text("Finish"), '
            'button:has-text("Join Waitlist"), a:has-text("Join Waitlist"), '
            'button:has-text("Done"), a:has-text("Done")',
            state="visible",
            timeout=5000,
        )
        confirm_text = confirm_btn.inner_text().strip()
        logger.info("    Step 10 — clicking: '%s'", confirm_text)
        confirm_btn.evaluate("el => el.click()")

        # Step 11 — confirm
        page.wait_for_load_state("networkidle", timeout=8000)
        logger.info("    Step 11 — final URL: %s", page.url)

        booked = "waitlist" not in confirm_text.lower()
        _notify_macos(session_name, target_date, booked=booked)
        return True

    except Exception as e:
        logger.warning("    Booking execution failed: %s", e)
        return False


# ---------------------------------------------------------------------------
# Main booking flow for a single session
# ---------------------------------------------------------------------------

def _book_session(
    page: Page,
    cfg: Config,
    session_name: str,
    details_url: str,
    session_text: str,
    target_date: date,
) -> bool:
    """
    Optimized booking flow:

    1.  Calculate open_time = session_start - 7d22h
    2.  Sleep quietly until T-2 minutes (browser completely idle)
    3.  Verify still logged in — re-login immediately if not
    4.  Navigate to detail page, verify Tim is selected
    5.  Navigate to schedule page, get fully loaded
    6.  Locate and hold the class link element on the schedule page
    7.  Sleep until exactly T-0 (browser idle, no requests)
    8.  T-0: instant JS click on class link → browser navigates to detail page
    9.  Instant JS click Reserve (or Add to Waitlist)
    10. Instant JS click Finish (or Join Waitlist)
    11. Confirm complete + macOS notification
    """
    from .auth import _do_login  # local import to avoid circular dependency

    open_time = _session_open_time(session_text, target_date)

    if open_time:
        # ── Step 2: sleep until T-2 ──────────────────────────────────────
        t_minus_2 = open_time - timedelta(minutes=2)
        wait_secs = (t_minus_2 - datetime.now()).total_seconds()
        if wait_secs > 0:
            logger.info(
                "Step 2 — sleeping %.0fs until T-2 (%s). Browser idle.",
                wait_secs, t_minus_2.strftime("%I:%M:%S %p"),
            )
            _time.sleep(wait_secs)

        # ── Step 3: verify login ──────────────────────────────────────────
        logger.info("Step 3 — verifying session at T-2...")
        if not _is_logged_in(page):
            logger.warning("Session expired — re-logging in...")
            if not _do_login(page, cfg.email, cfg.password):
                logger.error("Re-login failed — aborting.")
                return False
            logger.info("Re-login successful.")

        # ── Step 4: verify Tim on detail page ────────────────────────────
        logger.info("Step 4 — verifying participant selection...")
        page.goto(details_url, wait_until="domcontentloaded", timeout=10000)
        dismiss_cookie_popup(page)
        _select_participant(page)

        # ── Steps 5–6: schedule page + locate class link ──────────────────
        logger.info("Step 5 — loading schedule page...")
        page.goto(_classes_url(cfg, target_date), wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)

        day_idx = _day_column_index(page, target_date)
        class_link = None

        if day_idx != -1:
            day_col = page.locator(".calendar .day").nth(day_idx)
            for entry in day_col.locator('[data-testid="classCell"]').all():
                try:
                    if session_name.lower() in entry.inner_text().lower():
                        link = entry.locator(
                            '[data-testid="reserveLink"], [data-testid="classLink"]'
                        ).first
                        if link.count() > 0:
                            class_link = link
                            break
                except Exception:
                    continue

        if class_link is None:
            logger.warning("Step 6 — class link not found; will navigate directly at T-0.")

        logger.info("Step 6 — schedule page ready. Waiting for T-0...")

        # ── Step 7: sleep until T-0 ───────────────────────────────────────
        wait_secs = (open_time - datetime.now()).total_seconds()
        if wait_secs > 0:
            logger.info(
                "Step 7 — sleeping %.3fs until T-0 (%s).",
                wait_secs, open_time.strftime("%I:%M:%S %p"),
            )
            _time.sleep(wait_secs)

        # ── Step 8: T-0 — instant click ───────────────────────────────────
        logger.info("Step 8 — T-0: clicking class link...")
        if class_link is not None:
            class_link.evaluate("el => el.click()")
        else:
            # Fallback: direct navigation
            page.goto(details_url, wait_until="domcontentloaded", timeout=10000)

    else:
        # Booking window already open — navigate directly
        logger.info("Booking window already open — navigating to detail page.")
        page.goto(details_url, wait_until="domcontentloaded", timeout=10000)
        dismiss_cookie_popup(page)

    # Steps 9–11
    return _execute_booking(page, session_name, target_date)


# ---------------------------------------------------------------------------
# Main booking loop
# ---------------------------------------------------------------------------

def book_slots(
    page: Page,
    cfg: Config,
    dry_run: bool = False,
    test_mode: bool = False,
) -> List[BookingResult]:
    """
    Phase 1 — Discovery: scan schedule pages, collect all bookable sessions.
    Phase 2 — Booking: for each session (sorted by open time), run full flow.
    """
    results: List[BookingResult] = []
    targets = _target_dates(cfg)

    if not targets:
        logger.info("No target dates found within the booking horizon.")
        return results

    # Phase 1: Discovery
    existing_reservations = _fetch_existing_reservations(page)
    all_sessions: List[Tuple[date, str, str, str]] = []

    for target_date in targets:
        logger.info("Checking %s (%s)...", target_date, target_date.strftime("%A"))
        if not _navigate_to_date(page, cfg, target_date):
            continue
        sessions = _find_matching_sessions(
            page, cfg, target_date, existing_reservations, test_mode=test_mode
        )
        if not sessions:
            logger.info("  %s — no bookable sessions found.", target_date)
            continue
        for session_name, details_url, session_text in sessions:
            all_sessions.append((target_date, session_name, details_url, session_text))
            if test_mode:
                break
        if test_mode and all_sessions:
            break

    if not all_sessions:
        return results

    # Dry-run: report and exit
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

    # Phase 2: sort by open time, book each session
    all_sessions.sort(
        key=lambda s: _session_open_time(s[3], s[0]) or datetime.max
    )

    for target_date, session_name, details_url, session_text in all_sessions:
        logger.info("Booking %s — %s...", target_date, session_name)
        booked = _book_session(page, cfg, session_name, details_url, session_text, target_date)
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
            add_to_calendar(session_name, start_dt, end_dt, timezone=cfg.calendar_timezone)
        else:
            logger.warning("  FAILED: %s — %s", target_date, session_name)

    return results
