"""Login to my.lifetime.life and persist browser session."""

from __future__ import annotations

import logging
import time
import random
from pathlib import Path
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from .config import Config
from .utils import dismiss_cookie_popup

logger = logging.getLogger(__name__)

LOGIN_URL = "https://my.lifetime.life/login.html"


def _random_delay(lo: float = 0.5, hi: float = 1.5) -> None:
    time.sleep(random.uniform(lo, hi))


def _do_login(page: Page, email: str, password: str) -> bool:
    """Fill login form and submit. Returns True on success."""
    logger.info("Navigating to login page...")
    page.goto(LOGIN_URL, wait_until="networkidle")
    dismiss_cookie_popup(page)
    _random_delay()

    # Fill username/email — Lifetime uses name="username" / id="account-username"
    email_selector = '#account-username'
    page.wait_for_selector(email_selector, timeout=15000)
    page.fill(email_selector, email)
    _random_delay(0.3, 0.7)

    # Fill password
    password_selector = '#account-password'
    page.fill(password_selector, password)
    _random_delay(0.3, 0.8)

    # Submit
    submit_selector = 'button[type="submit"]'
    page.click(submit_selector)

    # Wait for navigation or error
    try:
        page.wait_for_url(lambda url: "login" not in url, timeout=15000)
        logger.info("Login successful.")
        return True
    except Exception:
        # Check for error message on page
        error_text = page.locator('[class*="error"], [class*="alert"], [role="alert"]').first
        if error_text.is_visible():
            msg = error_text.inner_text()
            logger.error(f"Login failed: {msg}")
        else:
            logger.error("Login failed: did not redirect away from login page.")
        return False


def get_authenticated_context(cfg: Config, playwright_instance=None) -> tuple:
    """
    Return (playwright, browser, context, page) with an authenticated session.

    Reuses storage_state.json if it exists and the session is still valid.
    Falls back to a fresh login and saves the new state.

    The caller is responsible for closing the browser when done.
    """
    pw = playwright_instance or sync_playwright().start()
    if cfg.headless:
        browser: Browser = pw.chromium.launch(headless=True)
    else:
        # Use installed Chrome so the window appears and focuses like a normal browser
        browser: Browser = pw.chromium.launch(
            headless=False,
            channel="chrome",
            slow_mo=300,  # slow actions down slightly so they're visible
            args=["--start-maximized"],
        )

    storage_path: Path = cfg.storage_state_path

    if storage_path.exists():
        logger.info("Loading saved session from %s", storage_path)
        context: BrowserContext = browser.new_context(storage_state=str(storage_path))
        page: Page = context.new_page()

        # Validate session: load reservations page, then confirm we're actually
        # authenticated by checking for a "Log In" button. A partially-expired
        # session can load the reservations page without redirecting to /login
        # but still shows "Log in to Reserve" on class pages.
        try:
            page.goto("https://my.lifetime.life/account/my-reservations.html",
                      wait_until="networkidle", timeout=15000)
            dismiss_cookie_popup(page)
            logged_in = (
                "login" not in page.url.lower()
                and not page.locator('a:has-text("Log In"), button:has-text("Log In")').first.is_visible(timeout=2000)
            )
            if logged_in:
                logger.info("Existing session is valid.")
                return pw, browser, context, page
            else:
                logger.info("Saved session expired, re-logging in.")
        except Exception as e:
            logger.warning("Session check failed (%s), re-logging in.", e)

        page.close()
        context.close()

    # Fresh login
    context = browser.new_context()
    page = context.new_page()
    success = _do_login(page, cfg.email, cfg.password)

    if not success:
        page.close()
        context.close()
        browser.close()
        if playwright_instance is None:
            pw.stop()
        raise RuntimeError("Authentication failed. Check your credentials in .env.")

    # Persist session
    context.storage_state(path=str(storage_path))
    logger.info("Session saved to %s", storage_path)

    return pw, browser, context, page
