"""Shared browser utilities."""

from __future__ import annotations

import logging
from playwright.sync_api import Page

logger = logging.getLogger(__name__)


def dismiss_cookie_popup(page: Page) -> None:
    """
    Click 'Accept All' on the OneTrust cookie consent banner if it is visible.
    Safe to call on every page — does nothing if the banner isn't present.
    """
    try:
        btn = page.locator('button:has-text("Accept All")').first
        if btn.is_visible(timeout=2000):
            btn.click()
            logger.debug("Dismissed cookie consent popup.")
    except Exception:
        pass  # banner not present, nothing to do
