#!/usr/bin/env python3
"""
One-off: Book Pickleball Open Play 4.25+ on Mon March 23 at 8:30 PM.
Registration opens: March 15, 2026 at 10:30 PM EST.

Steps:
  1. Verify login
  2. Find the 8:30 PM session on March 23 schedule page
  3. Sleep until T-2 (10:28 PM)
  4. Re-verify login, confirm Tim selected
  5. Load schedule page, hold class link
  6. At T-0 (10:30 PM), instant JS click → Reserve → Finish
  7. macOS notification
"""
from __future__ import annotations

import logging
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

TARGET_DATE = date(2026, 3, 23)
SESSION_KEYWORD = "3.75+"       # must appear in session text (case-insensitive)
SESSION_TIME = "8:30"           # used to disambiguate if multiple 4.0+ sessions
OPEN_TIME = datetime(2026, 3, 15, 22, 30, 0)   # 10:30 PM EST tonight

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from src.config import load_config
from src.auth import get_authenticated_context, _do_login
from src.utils import dismiss_cookie_popup


def _notify(session_name: str, booked: bool) -> None:
    status = "Booked" if booked else "Waitlisted"
    msg = f"Mon Mar 23 — {session_name}"
    title = f"Lifetime {status} ✓"
    try:
        subprocess.run(["osascript", "-e",
            f'display notification "{msg}" with title "{title}" sound name "Glass"'],
            timeout=3)
    except Exception:
        pass


def _select_tim(page) -> None:
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
                logger.info("  Selected participant: Tim")
            elif "mark" in label and is_checked:
                cb.evaluate("""el => {
                    el.checked = false;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }""")
                logger.info("  Deselected participant: Mark")
        except Exception:
            continue


def _is_logged_in(page) -> bool:
    try:
        page.goto("https://my.lifetime.life/account/my-reservations.html",
                  wait_until="networkidle", timeout=15000)
        dismiss_cookie_popup(page)
        if "login" in page.url.lower():
            return False
        login_btn = page.locator('a:has-text("Log In"), button:has-text("Log In")').first
        return not login_btn.is_visible(timeout=2000)
    except Exception:
        return False


def _schedule_url() -> str:
    return (
        "https://my.lifetime.life/clubs/ny/penn-1/classes.html"
        "?teamMemberView=true&mode=week&selectedDate=2026-03-23&interest=Pickleball"
    )


def _find_class_link(page):
    """Find the 8:30 PM 4.0+ session link on the schedule page. Returns locator or None."""
    try:
        date_str = "2026-03-23"
        day_idx = page.evaluate(f"""() => {{
            const radios = [...document.querySelectorAll('.planner-date-radio-input')];
            return radios.findIndex(r => r.value === '{date_str}');
        }}""")
        if day_idx == -1:
            logger.warning("Day column not found for March 23")
            return None, None

        day_col = page.locator(".calendar .day").nth(day_idx)
        entries = day_col.locator('[data-testid="classCell"]').all()
        logger.info("  Found %d entries on March 23", len(entries))

        for entry in entries:
            try:
                text = entry.inner_text().strip()
                if SESSION_KEYWORD.lower() not in text.lower():
                    continue
                if SESSION_TIME not in text:
                    continue
                link = entry.locator(
                    '[data-testid="reserveLink"], [data-testid="classLink"]'
                ).first
                if link.count() > 0:
                    session_name = text.split("\n")[0].strip()
                    logger.info("  Found session: %s", session_name)
                    href = link.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = "https://my.lifetime.life" + href
                    return link, session_name
            except Exception:
                continue
        logger.warning("  8:30 PM 4.0+ session not found on schedule page")
        return None, None
    except Exception as e:
        logger.error("  Error scanning schedule page: %s", e)
        return None, None


def main() -> int:
    cfg = load_config()
    cfg.headless = False  # always headed for this one-off

    logger.info("=== One-off booking: March 23 8:30 PM 4.0+ ===")
    logger.info("Registration opens at: %s", OPEN_TIME.strftime("%I:%M:%S %p"))

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        _, browser, context, page = get_authenticated_context(cfg, playwright_instance=pw)
        try:
            # Step 1: verify login
            logger.info("Step 1 — verifying login...")
            if not _is_logged_in(page):
                logger.error("Not logged in. Aborting.")
                return 1
            logger.info("  Logged in ✓")

            # Step 2: find session on schedule page
            logger.info("Step 2 — loading March 23 schedule page...")
            page.goto(_schedule_url(), wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)

            class_link, session_name = _find_class_link(page)
            if class_link is None:
                logger.error("Session not found on schedule page. Is it visible yet?")
                return 1

            # Find details URL for pre-warm
            details_url = class_link.get_attribute("href") or ""
            if details_url and not details_url.startswith("http"):
                details_url = "https://my.lifetime.life" + details_url

            # Step 3: sleep until T-2
            t_minus_2 = OPEN_TIME - timedelta(minutes=2)
            wait_secs = (t_minus_2 - datetime.now()).total_seconds()
            if wait_secs > 0:
                logger.info("Step 3 — sleeping %.0fs until T-2 (%s)...",
                            wait_secs, t_minus_2.strftime("%I:%M:%S %p"))
                time.sleep(wait_secs)

            # Step 4: re-verify login, select Tim on detail page
            logger.info("Step 4 — re-verifying login at T-2...")
            if not _is_logged_in(page):
                logger.warning("Session expired — re-logging in...")
                if not _do_login(page, cfg.email, cfg.password):
                    logger.error("Re-login failed.")
                    return 1

            if details_url:
                logger.info("Step 4 — pre-warming detail page, selecting Tim...")
                page.goto(details_url, wait_until="domcontentloaded", timeout=10000)
                dismiss_cookie_popup(page)
                _select_tim(page)

            # Step 5-6: reload schedule page, re-acquire class link
            logger.info("Step 5 — reloading schedule page...")
            page.goto(_schedule_url(), wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)
            class_link, session_name = _find_class_link(page)
            if class_link is None:
                logger.error("Session link disappeared from schedule page!")
                return 1
            logger.info("Step 6 — holding class link, waiting for T-0...")

            # Step 7: sleep until T-0
            wait_secs = (OPEN_TIME - datetime.now()).total_seconds()
            if wait_secs > 0:
                logger.info("Step 7 — sleeping %.3fs until T-0 (%s)...",
                            wait_secs, OPEN_TIME.strftime("%I:%M:%S %p"))
                time.sleep(wait_secs)

            # Step 8: T-0 — instant JS click
            logger.info("Step 8 — T-0: clicking session link...")
            class_link.evaluate("el => el.click()")

            # Step 9: detail page — click Reserve
            logger.info("Step 9 — waiting for detail page...")
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            _select_tim(page)
            reserve_btn = page.wait_for_selector(
                '[data-testid="reserveButton"]', state="attached", timeout=8000)
            btn_text = reserve_btn.inner_text().strip()
            logger.info("  Clicking: '%s'", btn_text)
            reserve_btn.evaluate("el => el.click()")

            # Step 10: pending reservation — click Finish / Join Waitlist
            logger.info("Step 10 — waiting for reservation page...")
            page.wait_for_url(lambda url: "/account/reservations" in url, timeout=15000)
            confirm_btn = page.wait_for_selector(
                'button:has-text("Finish"), a:has-text("Finish"), '
                'button:has-text("Join Waitlist"), a:has-text("Join Waitlist"), '
                'button:has-text("Done"), a:has-text("Done")',
                state="visible", timeout=5000)
            confirm_text = confirm_btn.inner_text().strip()
            logger.info("  Clicking: '%s'", confirm_text)
            confirm_btn.evaluate("el => el.click()")

            # Step 11: confirm
            page.wait_for_load_state("networkidle", timeout=8000)
            booked = "waitlist" not in confirm_text.lower()
            logger.info("Step 11 — %s! Final URL: %s",
                        "BOOKED" if booked else "WAITLISTED", page.url)
            _notify(session_name or "Pickleball 4.0+", booked=booked)
            return 0

        except Exception as e:
            logger.exception("Unexpected error: %s", e)
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
