#!/usr/bin/env python3
"""
Lifetime Fitness Pickleball Auto-Booker
----------------------------------------
Usage:
    python book.py              # Book upcoming slots per config
    python book.py --headed     # Override headless=False for debugging
    python book.py --dry-run    # Print available slots without booking
    python book.py --headed --dry-run
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup — must happen before importing src modules so handlers attach
# ---------------------------------------------------------------------------

def _setup_logging(log_file: str, verbose: bool = False) -> None:
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S"

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_path))
    except OSError as e:
        print(f"Warning: could not open log file {log_path}: {e}", file=sys.stderr)

    logging.basicConfig(level=level, format=fmt, datefmt=datefmt, handlers=handlers)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Lifetime Fitness Pickleball Auto-Booker")
    parser.add_argument("--headed", action="store_true", help="Run browser in non-headless mode (for debugging)")
    parser.add_argument("--dry-run", action="store_true", help="Find available slots but do not book")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Import after argparse so --help works without dependencies installed
    from src.config import load_config
    from src.auth import get_authenticated_context
    from src.booking import book_slots
    from src.notifier import notify_summary, notify_failure

    # Load config first so we can set up logging with the right log file
    try:
        cfg = load_config()
    except (ValueError, FileNotFoundError) as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 1

    _setup_logging(cfg.log_file, verbose=args.verbose)
    logger = logging.getLogger(__name__)

    # CLI flags override config
    if args.headed:
        cfg.headless = False

    mode = "DRY RUN" if args.dry_run else "LIVE"
    logger.info("=== Lifetime Pickleball Booker starting (%s) ===", mode)
    logger.info("Club: %s | Sport: %s | Days: %s | Keywords: %s | Exclusions: %s",
                cfg.club_name, cfg.sport, cfg.preferred_days,
                cfg.session_keywords, cfg.session_exclusions)

    pw = None
    browser = None
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            try:
                _, browser, context, page = get_authenticated_context(cfg, playwright_instance=pw)
            except RuntimeError as e:
                logger.error("Authentication error: %s", e)
                notify_failure(str(e))
                return 1

            try:
                results = book_slots(page, cfg, dry_run=args.dry_run)
            finally:
                browser.close()

    except Exception as e:
        logger.exception("Unexpected error: %s", e)
        notify_failure(f"Unexpected error: {e}")
        return 1

    # Summarize results
    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success and not r.dry_run]
    dry_run_found = [r for r in results if r.dry_run and r.success]

    if args.dry_run:
        logger.info("--- DRY RUN RESULTS ---")
        if dry_run_found:
            for r in dry_run_found:
                logger.info("  AVAILABLE: %s — %s", r.target_date, r.session_name)
        else:
            logger.info("  No available sessions found matching your keywords.")
    else:
        logger.info("--- BOOKING RESULTS ---")
        for r in successes:
            logger.info("  BOOKED: %s — %s", r.target_date, r.session_name)
        for r in failures:
            logger.info("  FAILED: %s — %s: %s", r.target_date, r.session_name, r.message)
        notify_summary(len(successes), len(failures))

    logger.info("=== Run complete ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
