#!/usr/bin/env python3
"""One-off: Book Pickleball Open Play 3.0-3.75 on Tue March 24 at 5:30 PM.
Registration opens: March 16, 2026 at 7:30 PM EST."""
from __future__ import annotations
import logging, subprocess, sys, time
from datetime import datetime, timedelta, date
from pathlib import Path

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger(__name__)

TARGET_DATE   = "2026-03-24"
SESSION_TIME  = "5:30"
SESSION_KW    = "3.0-3.75"
OPEN_TIME     = datetime(2026, 3, 16, 19, 30, 0)   # 7:30 PM EST tonight
SCHEDULE_URL  = (
    "https://my.lifetime.life/clubs/ny/penn-1/classes.html"
    "?teamMemberView=true&mode=week&selectedDate=2026-03-24&interest=Pickleball"
)

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from src.config import load_config
from src.auth import get_authenticated_context, _do_login
from src.utils import dismiss_cookie_popup


def _select_tim(page):
    for cb in page.locator('[data-testid="participantCheckBox"]').all():
        try:
            label = cb.evaluate("el => (el.closest('label')||el.parentElement)?.innerText?.trim()||''").lower()
            if "tim" in label and not cb.is_checked():
                cb.evaluate("el=>{el.checked=true;el.dispatchEvent(new Event('change',{bubbles:true}))}")
                logger.info("  Selected Tim")
            elif "mark" in label and cb.is_checked():
                cb.evaluate("el=>{el.checked=false;el.dispatchEvent(new Event('change',{bubbles:true}))}")
        except: pass


def _is_logged_in(page):
    try:
        page.goto("https://my.lifetime.life/account/my-reservations.html",
                  wait_until="networkidle", timeout=15000)
        dismiss_cookie_popup(page)
        if "login" in page.url.lower(): return False
        return not page.locator('a:has-text("Log In"),button:has-text("Log In")').first.is_visible(timeout=2000)
    except: return False


def _find_link(page, day_idx):
    day_col = page.locator(".calendar .day").nth(day_idx)
    for entry in day_col.locator('[data-testid="classCell"]').all():
        try:
            text = entry.inner_text().strip()
            if SESSION_KW.lower() in text.lower() and SESSION_TIME in text:
                link = entry.locator('[data-testid="reserveLink"],[data-testid="classLink"]').first
                if link.count() > 0:
                    href = link.get_attribute("href") or ""
                    if href and not href.startswith("http"):
                        href = "https://my.lifetime.life" + href
                    session_name = next((l.strip() for l in text.split("\n") if l.strip()), text[:60])
                    logger.info("  Found: %s | %s", session_name, href)
                    return link, href, session_name
        except: continue
    return None, None, None


def main():
    cfg = load_config()
    cfg.headless = False

    logger.info("=== One-off: March 24 5:30 PM 3.0-3.75 ===")
    logger.info("Window opens at: %s", OPEN_TIME.strftime("%I:%M:%S %p"))

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        _, browser, ctx, page = get_authenticated_context(cfg, playwright_instance=pw)
        try:
            # Step 1: find session + day index
            logger.info("Step 1 — finding session on schedule page...")
            page.goto(SCHEDULE_URL, wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)
            day_idx = page.evaluate(f"""() => {{
                const radios = [...document.querySelectorAll('.planner-date-radio-input')];
                return radios.findIndex(r => r.value === '{TARGET_DATE}');
            }}""")
            _, href, session_name = _find_link(page, day_idx)
            if not href:
                logger.error("Session not found — is it visible on the schedule?")
                return 1

            # Step 2: sleep until T-2
            t_minus_2 = OPEN_TIME - timedelta(minutes=2)
            wait_secs = (t_minus_2 - datetime.now()).total_seconds()
            if wait_secs > 0:
                logger.info("Step 2 — sleeping %.0fs until T-2 (%s)...", wait_secs, t_minus_2.strftime("%I:%M:%S %p"))
                time.sleep(wait_secs)

            # Step 3: verify login
            logger.info("Step 3 — verifying login...")
            if not _is_logged_in(page):
                logger.warning("Session expired — re-logging in...")
                if not _do_login(page, cfg.email, cfg.password):
                    logger.error("Re-login failed.")
                    return 1

            # Step 4: pre-warm detail page, select Tim
            logger.info("Step 4 — pre-warming detail page...")
            page.goto(href, wait_until="networkidle", timeout=15000)
            dismiss_cookie_popup(page)
            _select_tim(page)

            # Step 5: reload schedule, re-acquire link
            logger.info("Step 5 — reloading schedule page...")
            page.goto(SCHEDULE_URL, wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)
            class_link, href, session_name = _find_link(page, day_idx)
            if not class_link:
                logger.error("Session link disappeared!")
                return 1

            # Step 6: sleep until T-0
            wait_secs = (OPEN_TIME - datetime.now()).total_seconds()
            if wait_secs > 0:
                logger.info("Step 6 — sleeping %.3fs until T-0 (%s)...", wait_secs, OPEN_TIME.strftime("%I:%M:%S %p"))
                time.sleep(wait_secs)

            # Step 7: T-0 click
            logger.info("Step 7 — T-0: clicking session link...")
            class_link.evaluate("el => el.click()")

            # Step 8: wait for detail page, spinner, Reserve
            logger.info("Step 8 — waiting for detail page...")
            page.wait_for_load_state("networkidle", timeout=15000)
            _select_tim(page)
            logger.info("  Waiting for registration spinner...")
            page.wait_for_selector('[data-testid="sectionSpinner"]', state="hidden", timeout=15000)

            reserve_btn = page.locator('[data-testid="reserveButton"]').first
            btn_text = reserve_btn.inner_text().strip()
            logger.info("  Clicking: '%s'", btn_text)
            reserve_btn.evaluate("el => el.click()")

            # Step 9: Finish / Join Waitlist
            page.wait_for_url(lambda url: "/account/reservations" in url, timeout=15000)
            confirm_btn = page.wait_for_selector(
                'button:has-text("Finish"),a:has-text("Finish"),'
                'button:has-text("Join Waitlist"),a:has-text("Join Waitlist")',
                state="attached", timeout=5000)
            confirm_text = confirm_btn.inner_text().strip()
            logger.info("  Clicking: '%s'", confirm_text)
            confirm_btn.evaluate("el => el.click()")
            page.wait_for_load_state("networkidle", timeout=8000)

            booked = "waitlist" not in confirm_text.lower()
            logger.info("%s! %s", "BOOKED" if booked else "WAITLISTED", page.url)
            subprocess.run(["osascript", "-e",
                f'display notification "Tue Mar 24 5:30PM — {session_name}" '
                f'with title "Lifetime {"Booked" if booked else "Waitlisted"} ✓" sound name "Glass"'], timeout=3)
            return 0

        except Exception as e:
            logger.exception("Unexpected error: %s", e)
            return 1
        finally:
            browser.close()


if __name__ == "__main__":
    sys.exit(main())
