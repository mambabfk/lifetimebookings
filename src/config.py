"""Load and validate .env credentials and config.yaml preferences."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).parent.parent


@dataclass
class Config:
    # Credentials
    email: str
    password: str

    # Booking preferences
    club_name: str
    sport: str
    preferred_days: List[str]
    session_keywords: List[str]
    session_exclusions: List[str]
    booking_horizon_days: int
    headless: bool
    log_file: str

    calendar_timezone: str

    # Derived
    root_dir: Path = field(default_factory=lambda: ROOT)
    storage_state_path: Path = field(default_factory=lambda: ROOT / "storage_state.json")


def load_config(env_path: Path | None = None, config_path: Path | None = None) -> Config:
    env_path = env_path or ROOT / ".env"
    config_path = config_path or ROOT / "config.yaml"

    load_dotenv(env_path)

    email = os.getenv("LIFETIME_EMAIL", "").strip()
    password = os.getenv("LIFETIME_PASSWORD", "").strip()

    if not email or not password:
        raise ValueError(
            "LIFETIME_EMAIL and LIFETIME_PASSWORD must be set in .env\n"
            f"Copy .env.example to .env and fill in your credentials."
        )

    if not config_path.exists():
        raise FileNotFoundError(f"config.yaml not found at {config_path}")

    with config_path.open() as f:
        raw = yaml.safe_load(f)

    club_name = raw.get("club_name", "").strip()
    sport = raw.get("sport", "pickleball").strip()
    preferred_days = [d.lower() for d in raw.get("preferred_days", [])]
    session_keywords = [k.lower() for k in raw.get("session_keywords", [])]
    session_exclusions = [e.lower() for e in raw.get("session_exclusions", [])]
    booking_horizon_days = int(raw.get("booking_horizon_days", 7))
    headless = bool(raw.get("headless", True))
    log_file = raw.get("log_file", "logs/booking.log")
    calendar_timezone = raw.get("calendar_timezone", "America/New_York")

    if not club_name:
        raise ValueError("club_name must be set in config.yaml")
    if not preferred_days:
        raise ValueError("preferred_days must not be empty in config.yaml")
    if not session_keywords:
        raise ValueError("session_keywords must not be empty in config.yaml")

    valid_days = {"monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"}
    for day in preferred_days:
        if day not in valid_days:
            raise ValueError(f"Invalid day '{day}' in preferred_days. Must be a full lowercase day name.")

    return Config(
        email=email,
        password=password,
        club_name=club_name,
        sport=sport,
        preferred_days=preferred_days,
        session_keywords=session_keywords,
        session_exclusions=session_exclusions,
        booking_horizon_days=booking_horizon_days,
        headless=headless,
        log_file=log_file,
        calendar_timezone=calendar_timezone,
    )
