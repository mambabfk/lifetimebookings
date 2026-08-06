#!/usr/bin/env python3
"""Lifetime Fitness pickleball booking sniper, driven by Hermes cron.

Books sessions at my.lifetime.life the instant their registration window opens
(window = session start - 7 days 22 hours, e.g. Mon 8:30 PM class opens the
previous Sunday at 10:30 PM ET).

Runs WITHOUT Hermes: a systemd user timer (or plain crontab entry) ticks this
script every 10 minutes; when a queued target's window opens within the next
few minutes the run sleeps to the exact open second and books. Hermes is only
the ignitor/remote control — it edits the state file (add/remove/enable/
disable) when Tim asks in chat. Results are delivered straight to Telegram via
bot API when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are configured.

    lifetime_booking.py                 # scheduler tick: fire due targets, else silent
    lifetime_booking.py install-timer   # install systemd user timer (--crontab for cron)
    lifetime_booking.py status          # human-readable state + next fire times
    lifetime_booking.py plan            # per-target open time + which tick fires
    lifetime_booking.py enable|disable  # master on/off switch
    lifetime_booking.py add --date 2026-03-24 --time 17:30 --keyword "3.0-3.75"
    lifetime_booking.py remove --id ab12   (or --all)
    lifetime_booking.py check           # environment self-test (run on the box)
    lifetime_booking.py selftest        # offline logic tests, safe anywhere
    lifetime_booking.py login-test      # verify credentials + save session
    lifetime_booking.py notify-test     # send a Telegram test message
    lifetime_booking.py listen          # Telegram command loop (tick auto-starts it)
    lifetime_booking.py dry-run         # find pending targets on the schedule, no booking
    lifetime_booking.py book-now --id ab12  # book immediately, ignore open time
    lifetime_booking.py cancel --id ab12    # cancel the real reservation on the site

Telegram control: point TELEGRAM_BOT_TOKEN at a DEDICATED bot (BotFather), not
the Hermes bot — two programs cannot poll one token. The listener understands
plain messages ("8/14 6:30am drill", "status", "plan", "remove ab12",
"cancel ab12", "on", "off", "help") and replies from the same bot that sends
booking results and 24h reminders.

Secrets (never synced to git; both names are gitignored):
    ~/.hermes/state/lifetime_booking/.env        LIFETIME_EMAIL / LIFETIME_PASSWORD
                                                 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID (optional)
    ~/.hermes/state/lifetime_booking/auth.json   Playwright storage state (cookies)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
WINDOW_BEFORE_START = timedelta(days=7, hours=22)
# Cron runs every 10 min; anything opening within the next 11 min is "due" so
# no open time can fall between two ticks.
LOOKAHEAD = timedelta(minutes=11)
# Lifetime charges for no-shows — ask 24h ahead whether to keep the session.
REMIND_BEFORE = timedelta(hours=24)

BASE_DIR = Path(os.environ.get(
    "LIFETIME_BOOKING_DIR", "~/.hermes/state/lifetime_booking")).expanduser()
STATE_PATH = Path(os.environ.get(
    "LIFETIME_BOOKING_STATE", "~/.hermes/state/lifetime_booking.json")).expanduser()
ENV_PATH = BASE_DIR / ".env"
AUTH_PATH = BASE_DIR / "auth.json"

DEFAULT_STATE = {
    "enabled": True,
    "club_path": "clubs/ny/penn-1",
    "participant": "tim",
    "deselect": ["mark"],
    "targets": [],
    "history": [],
}

LOGIN_URL = "https://my.lifetime.life/login.html"
RESERVATIONS_URL = "https://my.lifetime.life/account/my-reservations.html"


def log(msg: str) -> None:
    """Step logging to stderr (kept out of the Telegram-delivered stdout)."""
    ts = datetime.now(ET).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text())
    else:
        state = {}
    for k, v in DEFAULT_STATE.items():
        state.setdefault(k, v)
    return state


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(STATE_PATH)


def read_env() -> dict:
    """Merge process env over the .env file for the keys this script uses."""
    keys = ("LIFETIME_EMAIL", "LIFETIME_PASSWORD",
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
    env: dict = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            env[key.strip()] = val.strip().strip('"').strip("'")
    for k in keys:
        if os.environ.get(k, "").strip():
            env[k] = os.environ[k].strip()
    return env


def load_credentials() -> tuple[str, str]:
    env = read_env()
    email = env.get("LIFETIME_EMAIL", "")
    password = env.get("LIFETIME_PASSWORD", "")
    if not email or not password:
        raise RuntimeError(
            f"Missing credentials. Put LIFETIME_EMAIL / LIFETIME_PASSWORD in {ENV_PATH}")
    return email, password


def notify(text: str) -> bool:
    """Send text to Telegram directly (no Hermes in the loop). Returns True if sent."""
    env = read_env()
    token, chat_id = env.get("TELEGRAM_BOT_TOKEN", ""), env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return False
    import urllib.parse
    import urllib.request
    try:
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status == 200
    except Exception as e:
        log(f"Telegram notify failed: {e}")
        return False


# ---------------------------------------------------------------------------
# Time math (pure — covered by selftest)
# ---------------------------------------------------------------------------

def parse_target_time(raw: str) -> tuple[int, int]:
    """'17:30', '5:30 PM', '5:30pm', '4pm' -> (hour, minute)."""
    m = re.fullmatch(r"\s*(\d{1,2})(?::(\d{2}))?\s*([AaPp][Mm])?\s*", raw)
    # A bare hour with neither minutes nor am/pm is ambiguous — reject it.
    if not m or (m.group(2) is None and m.group(3) is None):
        raise ValueError(f"Cannot parse time {raw!r} (use 4pm, 4:15pm, or 16:00)")
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    ampm = (m.group(3) or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time out of range: {raw!r}")
    return hour, minute


def parse_spec(text: str, now: datetime) -> tuple[str, str, str]:
    """Shorthand like '8/11 4pm 4.0+' -> ('2026-08-11', '4pm', '4.0+').

    Tokens may appear in any order: one date (M/D, M/D/YY[YY], or YYYY-MM-DD),
    one time (4pm / 4:15pm / 16:00), keyword = everything else. A date without
    a year resolves to the next future occurrence. The keyword is optional —
    without one, any pickleball session at that time matches.
    """
    # Glue '4 pm' -> '4pm' so time is always one token
    text = re.sub(r"(?i)\b(\d{1,2}(?::\d{2})?)\s+([ap]m)\b", r"\1\2", text.strip())
    date_str = time_str = None
    keyword_parts: list[str] = []
    for tok in text.split():
        if date_str is None:
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", tok)
            iso = re.fullmatch(r"\d{4}-\d{2}-\d{2}", tok)
            if iso:
                date_str = tok
                continue
            if m:
                mo, d = int(m.group(1)), int(m.group(2))
                if m.group(3):
                    y = int(m.group(3))
                    y += 2000 if y < 100 else 0
                else:
                    y = now.year
                    if (mo, d) < (now.month, now.day):
                        y += 1
                date_str = f"{y:04d}-{mo:02d}-{d:02d}"
                continue
        if time_str is None and re.fullmatch(
                r"(?i)\d{1,2}:\d{2}([ap]m)?|\d{1,2}[ap]m", tok):
            time_str = tok
            continue
        keyword_parts.append(tok)
    if not date_str or not time_str:
        raise ValueError(
            f"Cannot parse {text!r} — need at least a date and a time, "
            "e.g.: 8/11 4pm 4.0+ (keyword optional)")
    return date_str, time_str, " ".join(keyword_parts)


WEEKDAYS = {"mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1,
            "wed": 2, "weds": 2, "wednesday": 2,
            "thu": 3, "thur": 3, "thurs": 3, "thursday": 3,
            "fri": 4, "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6}
BULK_FILLER = {"all", "of", "the", "a", "session", "sessions", "at", "and",
               "on", "every", "book", "me", "for", "plus", "times", "time"}
KEYWORD_ALIASES = {"drilling": "drill", "drills": "drill"}


def parse_bulk(text: str, now: datetime) -> list[tuple[str, str, str]] | None:
    """Expand 'all the drilling sessions next week at 630 and 8' into
    (date, time, keyword) tuples — every matching day x every listed time.

    Returns None when the text isn't a bulk phrase (no 'week' token or no
    times). Bare-hour times inherit am/pm from the previous time in the list;
    a leading bare hour with nothing to inherit from is an error.
    """
    toks = re.sub(r"[,;]+", " ", text.strip().lower()).split()
    if "week" not in toks:
        return None
    scope = "next" if "next" in toks else "this"
    wanted_days = {WEEKDAYS[t] for t in toks if t in WEEKDAYS}

    times: list[str] = []
    kw_parts: list[str] = []
    last_was_pm: bool | None = None
    for tok in toks:
        if tok in ("week", "next", "this") or tok in WEEKDAYS or tok in BULK_FILLER:
            continue
        m = re.fullmatch(r"(\d{1,4})(?::(\d{2}))?(am|pm)?", tok)
        if not m:
            kw_parts.append(KEYWORD_ALIASES.get(tok, tok))
            continue
        digits, minutes, ampm = m.group(1), m.group(2), m.group(3)
        if minutes is not None:
            hour, minute = int(digits), int(minutes)
        elif len(digits) >= 3:              # compact 630 / 1730
            hour, minute = int(digits[:-2]), int(digits[-2:])
        else:
            hour, minute = int(digits), 0
            if not ampm:                     # bare hour — inherit meridian
                if last_was_pm is None:
                    raise ValueError(
                        f"Ambiguous time {tok!r} — give the first time with "
                        "minutes or am/pm (e.g. 6:30 and 8)")
                if last_was_pm and hour < 12:
                    hour += 12
        if ampm == "pm" and hour < 12:
            hour += 12
        elif ampm == "am" and hour == 12:
            hour = 0
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"Time out of range: {tok!r}")
        last_was_pm = hour >= 12
        times.append(f"{hour}:{minute:02d}")
    if not times:
        return None

    today = now.date()
    monday = today - timedelta(days=today.weekday())
    if scope == "next":
        first, last = monday + timedelta(days=7), monday + timedelta(days=13)
    else:
        first, last = today, monday + timedelta(days=6)
    keyword = " ".join(kw_parts)
    out = []
    day = first
    while day <= last:
        if not wanted_days or day.weekday() in wanted_days:
            for tm in times:
                out.append((day.isoformat(), tm, keyword))
        day += timedelta(days=1)
    return out


def session_start(target: dict) -> datetime:
    y, mo, d = (int(p) for p in target["date"].split("-"))
    hour, minute = parse_target_time(target["time"])
    return datetime(y, mo, d, hour, minute, tzinfo=ET)


def opens_at(target: dict) -> datetime:
    return session_start(target) - WINDOW_BEFORE_START


def clock_label(dt: datetime) -> str:
    """'5:30' — how the time appears in the schedule cell text (no leading zero)."""
    return dt.strftime("%I:%M").lstrip("0")


def classify(target: dict, now: datetime) -> str:
    """pending target -> 'wait' | 'due' (sleep to T-0) | 'late' (fire now) | 'expired'."""
    start, open_t = session_start(target), opens_at(target)
    if start <= now:
        return "expired"
    if now < open_t:
        return "due" if open_t - now <= LOOKAHEAD else "wait"
    return "late"  # window already open; keep trying until the session starts


def needs_reminder(target: dict, now: datetime) -> bool:
    """True when a successfully booked session starts within the next 24h
    and no keep-or-cancel reminder has been sent yet."""
    if target.get("status") != "done" or target.get("result") not in ("booked", "waitlisted"):
        return False
    if target.get("reminded_at"):
        return False
    start = session_start(target)
    return now < start and (start - now) <= REMIND_BEFORE


# ---------------------------------------------------------------------------
# Browser flow (ported from lifetimebookings one-off scripts, headless Linux)
# ---------------------------------------------------------------------------

def schedule_url(club_path: str, date_str: str) -> str:
    return (f"https://my.lifetime.life/{club_path}/classes.html"
            f"?teamMemberView=true&mode=week&selectedDate={date_str}&interest=Pickleball")


def dismiss_cookie_popup(page) -> None:
    try:
        btn = page.locator('button:has-text("Accept All")').first
        if btn.is_visible(timeout=2000):
            btn.click()
    except Exception:
        pass


def do_login(page, email: str, password: str) -> bool:
    log("Logging in...")
    page.goto(LOGIN_URL, wait_until="networkidle", timeout=30000)
    dismiss_cookie_popup(page)
    page.wait_for_selector("#account-username", timeout=15000)
    page.fill("#account-username", email)
    page.fill("#account-password", password)
    page.click('button[type="submit"]')
    try:
        page.wait_for_url(lambda url: "login" not in url, timeout=20000)
        log("Login OK")
        return True
    except Exception:
        log("Login failed (did not leave login page)")
        return False


def is_logged_in(page) -> bool:
    try:
        page.goto(RESERVATIONS_URL, wait_until="networkidle", timeout=20000)
        dismiss_cookie_popup(page)
        if "login" in page.url.lower():
            return False
        btn = page.locator('a:has-text("Log In"), button:has-text("Log In")').first
        return not btn.is_visible(timeout=2000)
    except Exception:
        return False


def open_browser(playwright):
    browser = playwright.chromium.launch(headless=True)
    if AUTH_PATH.exists():
        context = browser.new_context(storage_state=str(AUTH_PATH))
    else:
        context = browser.new_context()
    return browser, context


def ensure_logged_in(page, context) -> bool:
    if is_logged_in(page):
        return True
    email, password = load_credentials()
    if not do_login(page, email, password):
        return False
    AUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(AUTH_PATH))
    return True


def select_participants(page, participant: str, deselect: list[str]) -> None:
    for cb in page.locator('[data-testid="participantCheckBox"]').all():
        try:
            label = cb.evaluate(
                "el => (el.closest('label')||el.parentElement)?.innerText?.trim()||''"
            ).lower()
            checked = cb.is_checked()
            if participant in label and not checked:
                cb.evaluate("el=>{el.checked=true;"
                            "el.dispatchEvent(new Event('change',{bubbles:true}));"
                            "el.dispatchEvent(new Event('input',{bubbles:true}))}")
                log(f"  selected participant: {participant}")
            elif any(d in label for d in deselect) and checked:
                cb.evaluate("el=>{el.checked=false;"
                            "el.dispatchEvent(new Event('change',{bubbles:true}));"
                            "el.dispatchEvent(new Event('input',{bubbles:true}))}")
                log("  deselected extra participant")
        except Exception:
            continue


def find_class_link(page, target: dict):
    """Locate the session cell on the loaded schedule page.
    Returns (link_locator, href, session_name) or (None, None, None)."""
    date_str = target["date"]
    day_idx = page.evaluate(
        """(d) => {
            const radios = [...document.querySelectorAll('.planner-date-radio-input')];
            return radios.findIndex(r => r.value === d);
        }""", date_str)
    if day_idx == -1:
        log(f"  day column not found for {date_str}")
        return None, None, None
    time_label = clock_label(session_start(target))
    keyword = (target.get("keyword") or "").lower()
    day_col = page.locator(".calendar .day").nth(day_idx)
    for entry in day_col.locator('[data-testid="classCell"]').all():
        try:
            text = entry.inner_text().strip()
            if time_label not in text:
                continue
            if keyword and keyword not in text.lower():
                continue
            link = entry.locator(
                '[data-testid="reserveLink"], [data-testid="classLink"]').first
            if link.count() == 0:
                continue
            href = link.get_attribute("href") or ""
            if href and not href.startswith("http"):
                href = "https://my.lifetime.life" + href
            name = next((l.strip() for l in text.split("\n") if l.strip()), text[:60])
            log(f"  found: {name}")
            return link, href, name
        except Exception:
            continue
    log(f"  no cell matching {target['keyword']!r} at {time_label} on {date_str}")
    return None, None, None


def execute_booking(page, participant: str, deselect: list[str]) -> tuple[bool, str]:
    """Detail page is loading: Reserve -> Finish/Join Waitlist -> confirm."""
    page.wait_for_load_state("networkidle", timeout=15000)
    select_participants(page, participant, deselect)
    try:
        page.wait_for_selector('[data-testid="sectionSpinner"]',
                               state="hidden", timeout=15000)
    except Exception:
        pass
    reserve_btn = page.locator('[data-testid="reserveButton"]').first
    btn_text = reserve_btn.inner_text().strip()
    log(f"  clicking: {btn_text!r}")
    reserve_btn.evaluate("el => el.click()")

    page.wait_for_url(lambda url: "/account/reservations" in url, timeout=15000)
    confirm_btn = page.wait_for_selector(
        'button:has-text("Finish"), a:has-text("Finish"), '
        'button:has-text("Join Waitlist"), a:has-text("Join Waitlist"), '
        'button:has-text("Done"), a:has-text("Done")',
        state="attached", timeout=8000)
    confirm_text = confirm_btn.inner_text().strip()
    log(f"  clicking: {confirm_text!r}")
    confirm_btn.evaluate("el => el.click()")
    page.wait_for_load_state("networkidle", timeout=10000)

    booked = "waitlist" not in confirm_text.lower()
    return True, ("booked" if booked else "waitlisted")


def run_target(page, context, state: dict, target: dict, snipe: bool) -> tuple[bool, str]:
    """Full flow for one target. snipe=True sleeps to T-0; False books immediately."""
    open_t = opens_at(target)
    url = schedule_url(state["club_path"], target["date"])
    participant, deselect = state["participant"], state["deselect"]

    if not ensure_logged_in(page, context):
        return False, "login failed"

    log(f"Loading schedule: {url}")
    page.goto(url, wait_until="networkidle", timeout=30000)
    dismiss_cookie_popup(page)
    link, href, name = find_class_link(page, target)
    if not href:
        return False, "session not found on schedule page"
    target["session_name"] = name

    if snipe:
        # T-2: idle wait, then re-verify login and pre-warm the detail page.
        t2 = open_t - timedelta(minutes=2)
        wait = (t2 - datetime.now(ET)).total_seconds()
        if wait > 0:
            log(f"Sleeping {wait:.0f}s until T-2 ({t2.strftime('%I:%M:%S %p')})")
            time.sleep(wait)
        if not ensure_logged_in(page, context):
            return False, "login failed at T-2"
        log("Pre-warming detail page...")
        page.goto(href, wait_until="domcontentloaded", timeout=15000)
        dismiss_cookie_popup(page)
        select_participants(page, participant, deselect)

        log("Reloading schedule page...")
        page.goto(url, wait_until="networkidle", timeout=30000)
        dismiss_cookie_popup(page)
        link, href, name = find_class_link(page, target)
        if link is None and not href:
            return False, "session link disappeared before T-0"

        wait = (open_t - datetime.now(ET)).total_seconds()
        if wait > 0:
            log(f"Sleeping {wait:.1f}s until T-0 ({open_t.strftime('%I:%M:%S %p')})")
            time.sleep(wait)
        log("T-0: clicking class link")
        if link is not None:
            link.evaluate("el => el.click()")
        else:
            page.goto(href, wait_until="domcontentloaded", timeout=15000)
    else:
        log("Window already open — going straight to detail page")
        page.goto(href, wait_until="domcontentloaded", timeout=15000)
        dismiss_cookie_popup(page)

    return execute_booking(page, participant, deselect)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def fmt_target(t: dict, now: datetime) -> str:
    start, open_t = session_start(t), opens_at(t)
    status = t.get("status", "pending")
    line = (f"[{t['id']}] {start.strftime('%a %b %-d %-I:%M %p')} — "
            f"{t.get('keyword') or 'any'}"
            f" | opens {open_t.strftime('%a %b %-d %-I:%M %p')} | {status}")
    if t.get("result"):
        line += f" ({t['result']})"
    return line


def cmd_cron(state: dict) -> int:
    """The scheduled tick. Silent unless something fires, fails, or expires."""
    # A snipe run can outlive one tick interval; never let two ticks overlap.
    import fcntl
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(BASE_DIR / "tick.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another tick is already running — exiting.")
        return 0

    ensure_listener()
    now = datetime.now(ET)
    report: list[str] = []
    dirty = False

    for t in state["targets"]:
        if t.get("status") != "pending":
            continue
        kind = classify(t, now)
        if kind == "expired":
            t["status"] = "expired"
            report.append(f"⚠️ Missed (never fired): {fmt_target(t, now)}")
            dirty = True
        elif kind in ("due", "late"):
            t["status"] = "attempting"
            save_state(state)
            ok, result = False, "error"
            try:
                from playwright.sync_api import sync_playwright
                with sync_playwright() as pw:
                    browser, context = open_browser(pw)
                    page = context.new_page()
                    try:
                        ok, result = run_target(page, context, state, t,
                                                snipe=(kind == "due"))
                    finally:
                        browser.close()
            except Exception as e:  # noqa: BLE001 — must always record the outcome
                result = f"error: {e}"
            not_found = not ok and "session not found" in result
            t["status"] = "done" if ok else ("skipped" if not_found else "failed")
            t["result"] = result
            t["attempted_at"] = now.isoformat()
            state["history"].append({k: t.get(k) for k in
                                     ("id", "date", "time", "keyword", "status",
                                      "result", "session_name", "attempted_at")})
            dirty = True
            start = session_start(t)
            label = start.strftime("%a %b %-d %-I:%M %p")
            name = t.get("session_name") or t["keyword"] or "pickleball"
            if ok and result == "booked":
                report.append(f"✅ Booked: {label} — {name}")
            elif ok:
                report.append(f"🕐 Waitlisted: {label} — {name}")
            elif not_found:
                report.append(f"⏭ {label}: no session matching "
                              f"'{t['keyword'] or 'any'}' on the schedule — skipped")
            else:
                report.append(f"❌ Booking failed: {label} — {name} ({result})")

    # 24h keep-or-cancel reminders for booked sessions ($40 no-show fee)
    for t in state["targets"]:
        if needs_reminder(t, now):
            start = session_start(t)
            name = t.get("session_name") or t["keyword"] or "pickleball"
            msg = (f"⏰ Pickleball tomorrow: {start.strftime('%a %b %-d %-I:%M %p')} — {name}\n"
                   f"Keeping it: do nothing.\n"
                   f"To cancel (avoid the $40 no-show fee): reply \"cancel {t['id']}\" "
                   f"or do it at https://my.lifetime.life/account/my-reservations.html")
            print(msg)
            if not notify(msg):
                log("Reminder could not be delivered to Telegram!")
            t["reminded_at"] = now.isoformat()
            dirty = True

    if dirty:
        save_state(state)
    if report:
        text = "🏓 Lifetime booking\n" + "\n".join(report)
        print(text)
        if not notify(text):
            log("Telegram not configured/reachable — report went to stdout only.")
    return 0


# ---------------------------------------------------------------------------
# Telegram control listener — dedicated bot, deterministic, no agent involved
# ---------------------------------------------------------------------------

HELP_TEXT = """🏓 Pickleball booking commands:
• 8/14 6:30am drill — queue a session (date, time, optional keyword)
• several at once: one per line, or comma-separated
• bulk: "all the drill sessions next week at 630 and 8" (also: this week, mon wed fri)
• booked — list the reservations actually on your Lifetime account
• status — queue + recent results
• plan — when each target fires
• remove ab12 — drop a queued target
• cancel ab12 — cancel the REAL reservation on the site
• off / on — master switch
• help — this message"""


def tg_api(token: str, method: str, params: dict, timeout: int = 70) -> dict:
    import urllib.parse
    import urllib.request
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=urllib.parse.urlencode(params).encode())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def handle_message(text: str) -> str:
    """Route one incoming Telegram message; return the reply text."""
    import contextlib
    import io
    from argparse import Namespace

    def capture(fn, *fn_args) -> str:
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                fn(*fn_args)
        except Exception as e:  # noqa: BLE001 — reply must never crash the loop
            print(f"❌ {e}", file=buf)
        return buf.getvalue().strip() or "✅ done"

    low = text.strip().lower()
    state = load_state()
    if low in ("help", "/help", "/start"):
        return HELP_TEXT
    if low in ("status", "queue", "queued", "/status"):
        return capture(cmd_status, state)
    if low in ("plan", "/plan"):
        return capture(cmd_plan, state)
    if low in ("on", "enable"):
        return capture(cmd_toggle, state, True)
    if low in ("off", "disable"):
        return capture(cmd_toggle, state, False)
    m = re.fullmatch(r"(?:remove|rm|drop)\s+\[?([0-9a-f]{4})\]?", low)
    if m:
        return capture(cmd_remove, state, Namespace(id=m.group(1), all=False))
    m = re.fullmatch(r"cancel\s+\[?([0-9a-f]{4})\]?", low)
    if m:
        return capture(cmd_cancel, state, Namespace(id=m.group(1), headed=False))
    if low in ("booked", "reservations", "sessions", "my sessions",
               "what's booked", "what have i booked", "/booked"):
        return capture(cmd_booked, state)

    # Bulk pattern: "all the drilling sessions next week at 630 and 8"
    try:
        bulk = parse_bulk(text, datetime.now(ET))
    except ValueError as e:
        return f"❌ {e}"
    if bulk:
        if len(bulk) > 21:
            return (f"❌ That expands to {len(bulk)} targets — narrow it "
                    "(specific days, fewer times).")
        replies = [capture(cmd_add, state,
                           Namespace(spec=[d, tm] + (kw.split() if kw else []),
                                     date=None, time=None, keyword=None))
                   for d, tm, kw in bulk]
        return (f"📋 Expanded to {len(bulk)} target(s):\n\n"
                + "\n\n".join(replies)
                + "\n\nDays with no matching session just get skipped at "
                  "booking time.")

    # One or many booking specs — newline / semicolon / comma separated
    parts = [p.strip() for p in re.split(r"[\n;,]+", text) if p.strip()]
    replies = []
    for part in parts:
        try:
            parse_spec(part, datetime.now(ET))
        except ValueError:
            if len(parts) == 1:
                return "🤷 Didn't understand that.\n\n" + HELP_TEXT
            replies.append(f"❌ {part!r} — needs at least a date and a time")
            continue
        replies.append(capture(
            cmd_add, state,
            Namespace(spec=part.split(), date=None, time=None, keyword=None)))
    return "\n\n".join(replies) if replies else HELP_TEXT


def ensure_listener() -> None:
    """Start the Telegram listener if it isn't running (called by every tick)."""
    import fcntl
    import subprocess
    env = read_env()
    if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
        return
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    probe = open(BASE_DIR / "listen.lock", "a")
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    except OSError:
        probe.close()
        return  # already running
    probe.close()
    logf = open(BASE_DIR / "listen.log", "a")
    subprocess.Popen([sys.executable or "python3", str(Path(__file__).resolve()), "listen"],
                     stdout=logf, stderr=logf, start_new_session=True)
    log("Spawned Telegram listener.")


def cmd_listen() -> int:
    import fcntl
    env = read_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = env.get("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        print(f"❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in {ENV_PATH}")
        return 1
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    lock_fh = open(BASE_DIR / "listen.lock", "w")
    try:
        fcntl.flock(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Listener already running — exiting.")
        return 0

    offset_path = BASE_DIR / "telegram_offset"
    offset = int(offset_path.read_text()) if offset_path.exists() else 0
    log("Telegram listener started.")
    while True:
        try:
            resp = tg_api(token, "getUpdates", {"timeout": 50, "offset": offset})
        except Exception as e:  # noqa: BLE001
            if "409" in str(e):
                log("409 Conflict: something else is polling this bot token "
                    "(the Hermes bot?). Use a DEDICATED BotFather bot for booking.")
                return 1
            log(f"getUpdates failed: {e} — retrying in 10s")
            time.sleep(10)
            continue
        for upd in resp.get("result", []):
            offset = upd["update_id"] + 1
            offset_path.write_text(str(offset))
            msg = upd.get("message") or {}
            if str((msg.get("chat") or {}).get("id")) != str(chat_id):
                continue  # ignore strangers
            text = (msg.get("text") or "").strip()
            if not text:
                continue
            log(f"<< {text}")
            slow = re.match(r"(?i)^(cancel\s+\[?[0-9a-f]{4}|booked|reservations|"
                            r"sessions|my sessions|what's booked|what have i booked)",
                            text)
            if slow:
                try:
                    tg_api(token, "sendMessage",
                           {"chat_id": chat_id,
                            "text": "🔄 On it — checking the site, give me ~30s..."},
                           timeout=15)
                except Exception:
                    pass
            reply = handle_message(text)
            try:
                tg_api(token, "sendMessage", {"chat_id": chat_id, "text": reply},
                       timeout=15)
            except Exception as e:  # noqa: BLE001
                log(f"Reply failed: {e}")


# ---------------------------------------------------------------------------
# List the reservations actually on the Lifetime account
# ---------------------------------------------------------------------------

def cmd_booked(state: dict) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, context = open_browser(pw)
        page = context.new_page()
        try:
            if not ensure_logged_in(page, context):
                print("❌ Login failed — see reservations at "
                      "https://my.lifetime.life/account/my-reservations.html")
                return 1
            page.goto(RESERVATIONS_URL, wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)

            def clean(raw: str) -> str:
                lines = [l.strip() for l in raw.split("\n") if l.strip()]
                joined = " | ".join(lines)
                return joined[:140] + ("…" if len(joined) > 140 else "")

            items: list[str] = []
            seen: set[str] = set()
            for el in page.locator(
                    '[data-testid*="reservation"], [class*="reservation-item"], '
                    '[class*="my-reservation"], [class*="upcoming-reservation"]').all():
                try:
                    text = el.inner_text().strip()
                    if text and len(text) < 400 and text.lower() not in seen:
                        seen.add(text.lower())
                        items.append(clean(text))
                except Exception:
                    continue
            if not items:  # fallback: generic cards that look like reservations
                for el in page.locator('[class*="card"], [class*="item"]').all():
                    try:
                        text = el.inner_text().strip()
                        low = text.lower()
                        if (text and len(text) < 400 and low not in seen
                                and any(k in low for k in
                                        ("pickleball", "court", "class", "reservation"))):
                            seen.add(low)
                            items.append(clean(text))
                    except Exception:
                        continue
            if not items:
                print("📅 No upcoming reservations found on your account.")
            else:
                print(f"📅 Upcoming reservations on your account ({len(items)}):")
                for i, item in enumerate(items[:15], 1):
                    print(f"{i}. {item}")
                if len(items) > 15:
                    print(f"…and {len(items) - 15} more: {RESERVATIONS_URL}")
            return 0
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Cancel an existing reservation on the site
# ---------------------------------------------------------------------------

def cmd_cancel(state: dict, args) -> int:
    target = next((t for t in state["targets"] if t["id"] == args.id), None)
    if target is None:
        print(f"No target with id {args.id}. Run status to list ids.")
        return 1
    start = session_start(target)
    day_pat = f"{start.strftime('%b')} {start.day}"       # "Aug 13"
    time_pat = clock_label(start)                          # "5:30"
    label = start.strftime("%a %b %-d %-I:%M %p")

    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        context = (browser.new_context(storage_state=str(AUTH_PATH))
                   if AUTH_PATH.exists() else browser.new_context())
        page = context.new_page()
        try:
            if not ensure_logged_in(page, context):
                print("❌ Login failed — cancel manually: "
                      "https://my.lifetime.life/account/my-reservations.html")
                return 1

            def find_card():
                for el in page.locator(
                        '[data-testid*="reservation"], [class*="reservation"], '
                        '[class*="card"]').all():
                    try:
                        text = el.inner_text()
                        if day_pat.lower() in text.lower() and time_pat in text:
                            return el
                    except Exception:
                        continue
                return None

            page.goto(RESERVATIONS_URL, wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)
            card = find_card()
            if card is None:
                print(f"❌ Could not find a reservation matching {label} on the "
                      "reservations page — cancel manually if it exists.")
                return 1
            log(f"Found reservation card for {label}")

            # Cancel link either on the card or on its detail page
            cancel_btn = card.locator(
                'a:has-text("Cancel"), button:has-text("Cancel")').first
            if cancel_btn.count() == 0 or not cancel_btn.is_visible():
                link = card.locator("a").first
                if link.count() == 0:
                    print("❌ No cancel control or detail link on the card — cancel manually.")
                    return 1
                link.evaluate("el => el.click()")
                page.wait_for_load_state("networkidle", timeout=15000)
                cancel_btn = page.locator(
                    'a:has-text("Cancel"), button:has-text("Cancel")').first
                if cancel_btn.count() == 0:
                    print("❌ No cancel control on the detail page — cancel manually.")
                    return 1
            log(f"Clicking: {cancel_btn.inner_text().strip()!r}")
            cancel_btn.evaluate("el => el.click()")

            # Confirmation dialog — click the affirmative, never a bare "No"
            try:
                confirm = page.wait_for_selector(
                    'button:has-text("Yes"), a:has-text("Yes"), '
                    'button:has-text("Cancel Reservation"), '
                    'a:has-text("Cancel Reservation"), '
                    'button:has-text("Confirm"), a:has-text("Confirm")',
                    state="visible", timeout=8000)
                log(f"Confirming: {confirm.inner_text().strip()!r}")
                confirm.evaluate("el => el.click()")
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                log("No confirmation dialog appeared — assuming single-step cancel.")

            # Verify: reload the reservations list, the card should be gone
            page.goto(RESERVATIONS_URL, wait_until="networkidle", timeout=20000)
            dismiss_cookie_popup(page)
            if find_card() is None:
                target["status"] = "cancelled"
                target["cancelled_at"] = datetime.now(ET).isoformat()
                save_state(state)
                name = target.get("session_name") or target["keyword"] or "pickleball"
                msg = f"🗑 Cancelled: {label} — {name}"
                print(msg)
                notify(msg)
                return 0
            print(f"⚠️ Clicked through the cancel flow but {label} still shows on the "
                  "reservations page — verify manually: "
                  "https://my.lifetime.life/account/my-reservations.html")
            return 1
        finally:
            browser.close()


def cmd_status(state: dict) -> int:
    now = datetime.now(ET)
    print(f"Master switch: {'ON' if state['enabled'] else 'OFF'}")
    sched = scheduler_status()
    print(f"Scheduler: {sched}" + (" ⚠️ NOT ARMED — run install-timer" if sched == "none" else ""))
    print(f"Club: {state['club_path']} | participant: {state['participant']}")
    pending = [t for t in state["targets"] if t.get("status") == "pending"]
    others = [t for t in state["targets"] if t.get("status") != "pending"]
    if not state["targets"]:
        print("No targets. Add one with: lifetime_booking.py add --date ... --time ... --keyword ...")
    for t in pending:
        print("  " + fmt_target(t, now))
    for t in others[-5:]:
        print("  " + fmt_target(t, now))
    if not ENV_PATH.exists():
        print(f"⚠️ Credentials file missing: {ENV_PATH}")
    return 0


def cmd_plan(state: dict) -> int:
    now = datetime.now(ET)
    print(f"Now (ET): {now.strftime('%a %b %-d %-I:%M %p')}")
    for t in state["targets"]:
        if t.get("status") != "pending":
            continue
        open_t = opens_at(t)
        print(fmt_target(t, now))
        if open_t <= now:
            print("    window ALREADY OPEN — next cron tick books immediately")
            continue
        # First 10-min cron tick inside the (open - 11min, open] window
        tick = open_t - timedelta(minutes=open_t.minute % 10,
                                  seconds=open_t.second,
                                  microseconds=open_t.microsecond)
        if tick == open_t:
            tick -= timedelta(minutes=10)
        print(f"    cron tick that fires it: {tick.strftime('%a %b %-d %-I:%M %p')} ET"
              f" (sleeps {int((open_t - tick).total_seconds())}s to T-0)")
    return 0


def cmd_add(state: dict, args) -> int:
    now = datetime.now(ET)
    if args.spec:
        try:
            date_str, time_str, keyword = parse_spec(" ".join(args.spec), now)
        except ValueError as e:
            print(f"❌ {e}")
            return 1
    elif args.date and args.time:
        date_str, time_str, keyword = args.date, args.time, args.keyword or ""
    else:
        print("❌ Give a shorthand spec (add 8/11 4pm 4.0+) or --date/--time [--keyword].")
        return 1
    target = {
        "id": uuid.uuid4().hex[:4],
        "date": date_str,
        "time": time_str,
        "keyword": keyword,
        "status": "pending",
        "added_at": now.isoformat(),
    }
    try:
        start, open_t = session_start(target), opens_at(target)  # validates date/time
    except ValueError as e:
        print(f"❌ {e}")
        return 1
    if start <= now:
        print(f"❌ {date_str} {time_str} is in the past — not added.")
        return 1
    state["targets"].append(target)
    save_state(state)
    when = "ALREADY OPEN — next cron tick will book immediately" if open_t <= now \
        else f"window opens {open_t.strftime('%a %b %-d %-I:%M %p')} ET"
    print(f"✅ Target [{target['id']}]: {start.strftime('%a %b %-d %-I:%M %p')} "
          f"— {keyword or 'any pickleball session at that time'}\n   {when}")
    if not state["enabled"]:
        print("⚠️ Master switch is OFF — run `enable` or it will not fire.")
    return 0


def cmd_remove(state: dict, args) -> int:
    if args.all:
        n = len([t for t in state["targets"] if t.get("status") == "pending"])
        state["targets"] = [t for t in state["targets"] if t.get("status") != "pending"]
        save_state(state)
        print(f"Removed {n} pending target(s).")
        return 0
    before = len(state["targets"])
    state["targets"] = [t for t in state["targets"] if t["id"] != args.id]
    save_state(state)
    print("Removed." if len(state["targets"]) < before else f"No target with id {args.id}.")
    return 0


def cmd_toggle(state: dict, enabled: bool) -> int:
    state["enabled"] = enabled
    save_state(state)
    print(f"Lifetime booking is now {'ON' if enabled else 'OFF'}.")
    return 0


SYSTEMD_UNIT = "lifetime-booking"


def _systemd_user_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", "~/.config")).expanduser() / "systemd" / "user"


def _run(cmd: list[str]) -> tuple[int, str]:
    import subprocess
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def scheduler_status() -> str:
    """'systemd' | 'crontab' | 'none' — which independent scheduler is armed."""
    rc, out = _run(["systemctl", "--user", "is-active", f"{SYSTEMD_UNIT}.timer"])
    if rc == 0 and out.strip() == "active":
        return "systemd"
    rc, out = _run(["crontab", "-l"])
    if rc == 0 and "lifetime_booking.py" in out:
        return "crontab"
    return "none"


def cmd_install_timer(args) -> int:
    script = Path(__file__).resolve()
    python = sys.executable or "python3"

    if args.crontab:
        marker = "# lifetime_booking tick"
        line = f"*/10 * * * * {python} {script} >> {BASE_DIR}/tick.log 2>&1 {marker}"
        rc, current = _run(["crontab", "-l"])
        lines = [l for l in (current.splitlines() if rc == 0 else [])
                 if "lifetime_booking.py" not in l]
        lines.append(line)
        import subprocess
        try:
            p = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True)
            rc = p.returncode
        except FileNotFoundError:
            rc = 127
        if rc != 0:
            print("❌ Could not install crontab entry"
                  + (" (no crontab binary on this box)." if rc == 127 else "."))
            return 1
        print(f"✅ Crontab entry installed:\n   {line}")
        return 0

    unit_dir = _systemd_user_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / f"{SYSTEMD_UNIT}.service").write_text(f"""[Unit]
Description=Lifetime pickleball booking tick

[Service]
Type=oneshot
ExecStart={python} {script}
""")
    (unit_dir / f"{SYSTEMD_UNIT}.timer").write_text(f"""[Unit]
Description=Lifetime pickleball booking tick (every 10 minutes)

[Timer]
OnCalendar=*:00/10
AccuracySec=30s
Persistent=true

[Install]
WantedBy=timers.target
""")
    for cmd in (["systemctl", "--user", "daemon-reload"],
                ["systemctl", "--user", "enable", "--now", f"{SYSTEMD_UNIT}.timer"]):
        rc, out = _run(cmd)
        if rc != 0:
            print(f"❌ {' '.join(cmd)} failed: {out}\n"
                  f"   No systemd user session? Use: lifetime_booking.py install-timer --crontab")
            return 1
    print(f"✅ systemd user timer installed and started ({SYSTEMD_UNIT}.timer, every 10 min).")
    rc, out = _run(["loginctl", "show-user", os.environ.get("USER", ""), "-p", "Linger"])
    if rc == 0 and "Linger=no" in out:
        print("⚠️ Lingering is OFF — user timers stop when you log out. "
              f"Fix: loginctl enable-linger {os.environ.get('USER', '')}")
    return 0


def cmd_check(state: dict) -> int:
    ok = True
    print(f"State file: {STATE_PATH} ({'exists' if STATE_PATH.exists() else 'will be created'})")
    print(f"Timezone check: now ET = {datetime.now(ET).strftime('%a %b %-d %-I:%M %p')}")
    try:
        load_credentials()
        print(f"Credentials: OK ({ENV_PATH})")
    except RuntimeError as e:
        print(f"❌ {e}")
        ok = False
    env = read_env()
    if env.get("TELEGRAM_BOT_TOKEN") and env.get("TELEGRAM_CHAT_ID"):
        print("Telegram delivery: configured")
        import fcntl
        try:
            probe = open(BASE_DIR / "listen.lock", "a")
            fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(probe, fcntl.LOCK_UN)
            probe.close()
            print("⚠️ Telegram listener: not running (the next tick starts it, "
                  "or run `listen` in the background yourself)")
        except OSError:
            print("Telegram listener: running ✅ (text the bot 'status' to try it)")
    else:
        print("⚠️ Telegram delivery not configured — booking results will only reach "
              f"the tick log. Add TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID to {ENV_PATH}")
    print(f"Saved session: {'present' if AUTH_PATH.exists() else 'none yet (login-test will create it)'}")
    sched = scheduler_status()
    if sched == "none":
        print("❌ No independent scheduler armed — bookings will NOT fire on their own. "
              "Run: lifetime_booking.py install-timer")
        ok = False
    else:
        print(f"Scheduler: {sched} ✅ (fires every 10 min without Hermes)")
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            b.close()
        print("Playwright + headless Chromium: OK")
    except Exception as e:
        print(f"❌ Playwright/Chromium: {e}\n   Fix: pip install playwright && playwright install chromium --with-deps")
        ok = False
    print("Environment " + ("OK ✅" if ok else "NOT ready ❌"))
    return 0 if ok else 1


def cmd_login_test(state: dict) -> int:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, context = open_browser(pw)
        page = context.new_page()
        try:
            if ensure_logged_in(page, context):
                print("✅ Login OK — session saved for future runs.")
                return 0
            print("❌ Login failed. Check credentials; if a CAPTCHA is involved, "
                  "seed auth.json from a machine where you can run headed once.")
            return 1
        finally:
            browser.close()


def cmd_dry_run(state: dict) -> int:
    pending = [t for t in state["targets"] if t.get("status") == "pending"]
    if not pending:
        print("No pending targets to check.")
        return 0
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, context = open_browser(pw)
        page = context.new_page()
        try:
            if not ensure_logged_in(page, context):
                print("❌ Login failed.")
                return 1
            for t in pending:
                url = schedule_url(state["club_path"], t["date"])
                page.goto(url, wait_until="networkidle", timeout=30000)
                dismiss_cookie_popup(page)
                _, href, name = find_class_link(page, t)
                mark = f"✅ visible: {name}" if href else "❌ not found on schedule (yet?)"
                print(f"[{t['id']}] {t['date']} {t['time']} {t['keyword']} — {mark}")
        finally:
            browser.close()
    return 0


def cmd_book_now(state: dict, args) -> int:
    target = next((t for t in state["targets"] if t["id"] == args.id), None)
    if target is None:
        print(f"No target with id {args.id}.")
        return 1
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser, context = open_browser(pw)
        page = context.new_page()
        try:
            ok, result = run_target(page, context, state, target, snipe=False)
        finally:
            browser.close()
    target["status"] = "done" if ok else "failed"
    target["result"] = result
    target["attempted_at"] = datetime.now(ET).isoformat()
    save_state(state)
    print(("✅ " if ok else "❌ ") + result)
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# Selftest — pure logic, no network, safe to run anywhere
# ---------------------------------------------------------------------------

def cmd_selftest() -> int:
    import tempfile
    failures = []

    def check(name, cond):
        print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            failures.append(name)

    # Time parsing
    check("parse 17:30", parse_target_time("17:30") == (17, 30))
    check("parse 5:30 PM", parse_target_time("5:30 PM") == (17, 30))
    check("parse 12:00am", parse_target_time("12:00am") == (0, 0))
    check("parse 8:30pm", parse_target_time("8:30pm") == (20, 30))
    check("parse 4pm", parse_target_time("4pm") == (16, 0))
    check("parse 12pm", parse_target_time("12pm") == (12, 0))
    try:
        parse_target_time("25:00")
        check("reject 25:00", False)
    except ValueError:
        check("reject 25:00", True)
    try:
        parse_target_time("4")
        check("reject bare hour", False)
    except ValueError:
        check("reject bare hour", True)

    # Shorthand spec parsing (now = Wed Aug 5 2026)
    fake_now = datetime(2026, 8, 5, 12, 0, tzinfo=ET)
    check("spec 8/11 4pm 4.0+",
          parse_spec("8/11 4pm 4.0+", fake_now) == ("2026-08-11", "4pm", "4.0+"))
    check("spec order-insensitive",
          parse_spec("4.0+ 4pm 8/11", fake_now) == ("2026-08-11", "4pm", "4.0+"))
    check("spec year rollover",
          parse_spec("1/2 4pm drill", fake_now) == ("2027-01-02", "4pm", "drill"))
    check("spec explicit year",
          parse_spec("3/24/27 5:30pm 3.0-3.75", fake_now)
          == ("2027-03-24", "5:30pm", "3.0-3.75"))
    check("spec '4 pm' with space",
          parse_spec("8/11 4 pm open play", fake_now)
          == ("2026-08-11", "4pm", "open play"))
    check("spec 24h time",
          parse_spec("8/11 16:00 4.0+", fake_now) == ("2026-08-11", "16:00", "4.0+"))
    check("spec keyword optional",
          parse_spec("8/14 8:00AM", fake_now) == ("2026-08-14", "8:00AM", ""))
    check("keyword with slash not eaten as date",
          parse_spec("8/11 4pm 3.5/4.0", fake_now) == ("2026-08-11", "4pm", "3.5/4.0"))
    try:
        parse_spec("4pm 4.0+", fake_now)
        check("reject spec without date", False)
    except ValueError:
        check("reject spec without date", True)

    # Bulk expansion (fake_now = Wed Aug 5 2026; next week = Mon 8/10 – Sun 8/16)
    bulk = parse_bulk("all of the drilling sessions next week at 630 and 8", fake_now)
    check("bulk 7 days x 2 times", len(bulk) == 14)
    check("bulk dates span next week",
          bulk[0][0] == "2026-08-10" and bulk[-1][0] == "2026-08-16")
    check("bulk times inherit am", {t for _, t, _ in bulk} == {"6:30", "8:00"})
    check("bulk keyword drilling->drill", all(kw == "drill" for _, _, kw in bulk))
    bulk = parse_bulk("drill next week mon wed at 6:30am", fake_now)
    check("bulk weekday filter",
          [d for d, _, _ in bulk] == ["2026-08-10", "2026-08-12"])
    bulk = parse_bulk("next week at 5:30pm and 8", fake_now)
    check("bulk pm inheritance", {t for _, t, _ in bulk} == {"17:30", "20:00"})
    bulk = parse_bulk("this week at 6:30", fake_now)
    check("bulk this week = today..sunday",
          [d for d, _, _ in bulk] == [f"2026-08-{n:02d}" for n in range(5, 10)])
    check("non-bulk returns None",
          parse_bulk("8/14 6:30am drill", fake_now) is None)
    try:
        parse_bulk("next week at 8", fake_now)
        check("bulk bare hour without anchor rejected", False)
    except ValueError:
        check("bulk bare hour without anchor rejected", True)

    # Open-time math — ground truth from the March one-offs:
    # Mon Mar 23 2026 8:30 PM class opened Sun Mar 15 10:30 PM ET.
    t = {"date": "2026-03-23", "time": "20:30", "keyword": "x"}
    check("open time = start - 7d22h",
          opens_at(t) == datetime(2026, 3, 15, 22, 30, tzinfo=ET))
    # Tue Mar 24 5:30 PM -> Mon Mar 16 7:30 PM
    t2 = {"date": "2026-03-24", "time": "17:30", "keyword": "x"}
    check("open time (one-off #2)",
          opens_at(t2) == datetime(2026, 3, 16, 19, 30, tzinfo=ET))
    check("clock label", clock_label(session_start(t2)) == "5:30")

    # Fire-window classification
    open_t = opens_at(t2)
    check("wait long before", classify(t2, open_t - timedelta(hours=3)) == "wait")
    check("due 10min before", classify(t2, open_t - timedelta(minutes=10)) == "due")
    check("due 1min before", classify(t2, open_t - timedelta(minutes=1)) == "due")
    check("late just after", classify(t2, open_t + timedelta(minutes=5)) == "late")
    check("expired after start",
          classify(t2, session_start(t2) + timedelta(minutes=1)) == "expired")
    check("wait 12min before", classify(t2, open_t - timedelta(minutes=12)) == "wait")

    # Every open time is caught by exactly one 10-min tick with an 11-min lookahead
    for minute in (0, 5, 10, 30, 55):
        o = datetime(2026, 3, 16, 19, minute, tzinfo=ET)
        tt = {"date": "2026-03-24",
              "time": f"{(o + WINDOW_BEFORE_START).hour}:{(o + WINDOW_BEFORE_START).minute:02d}",
              "keyword": "x"}
        ticks = [datetime(2026, 3, 16, h, m, tzinfo=ET)
                 for h in range(24) for m in range(0, 60, 10)]
        firing = [k for k in ticks if classify(tt, k) == "due"]
        check(f"open at :{minute:02d} caught by exactly one tick", len(firing) == 1)

    # Reminder gating
    booked = {"date": "2026-03-24", "time": "17:30", "keyword": "x",
              "status": "done", "result": "booked"}
    start = session_start(booked)
    check("no reminder 25h before",
          not needs_reminder(booked, start - timedelta(hours=25)))
    check("reminder 23h before",
          needs_reminder(booked, start - timedelta(hours=23)))
    check("no reminder after start",
          not needs_reminder(booked, start + timedelta(minutes=1)))
    check("no reminder when already sent",
          not needs_reminder({**booked, "reminded_at": "x"},
                             start - timedelta(hours=23)))
    check("no reminder for failed booking",
          not needs_reminder({**booked, "result": "error: x", "status": "failed"},
                             start - timedelta(hours=23)))
    check("reminder for waitlisted",
          needs_reminder({**booked, "result": "waitlisted"},
                         start - timedelta(hours=23)))
    check("no reminder for pending",
          not needs_reminder({**booked, "status": "pending", "result": None},
                             start - timedelta(hours=23)))

    # State round-trip in a sandbox dir
    global STATE_PATH, BASE_DIR, ENV_PATH, AUTH_PATH
    old = (STATE_PATH, BASE_DIR, ENV_PATH, AUTH_PATH)
    with tempfile.TemporaryDirectory() as td:
        STATE_PATH = Path(td) / "state.json"
        BASE_DIR = Path(td)
        ENV_PATH = BASE_DIR / ".env"
        AUTH_PATH = BASE_DIR / "auth.json"
        ENV_PATH.write_text("# comment\nLIFETIME_EMAIL=a@b.com\nLIFETIME_PASSWORD='p w'\n")
        env = read_env()
        check("env parse email", env["LIFETIME_EMAIL"] == "a@b.com")
        check("env parse quoted", env["LIFETIME_PASSWORD"] == "p w")
        check("notify no-op when unconfigured", notify("test") is False)
        s = load_state()
        check("default enabled", s["enabled"] is True)
        s["targets"].append({"id": "test", "date": "2099-01-04", "time": "19:00",
                             "keyword": "4.0+", "status": "pending"})
        save_state(s)
        s2 = load_state()
        check("state round-trip", s2["targets"][0]["id"] == "test")
        check("cron silent when nothing due", cmd_cron(s2) == 0)
        s3 = load_state()
        check("pending untouched by idle tick",
              s3["targets"][0]["status"] == "pending")

        # Token appended only after the cron test — a tick with a token would
        # spawn a real listener subprocess, which selftest must never do.
        ENV_PATH.write_text(ENV_PATH.read_text()
                            + "TELEGRAM_BOT_TOKEN=tok\nTELEGRAM_CHAT_ID=123\n")
        check("env parse telegram", read_env()["TELEGRAM_CHAT_ID"] == "123")

        # Telegram message routing (offline — handle_message never sends)
        check("tg help", handle_message("help") == HELP_TEXT)
        check("tg off", "OFF" in handle_message("off"))
        check("tg on", "ON" in handle_message("on"))
        reply = handle_message("12/25 9:00am drill")
        check("tg add spec", "Target" in reply and "window opens" in reply)
        listing = handle_message("status")
        check("tg status lists it", "drill" in listing)
        new_id = load_state()["targets"][-1]["id"]
        check("tg remove", "Removed" in handle_message(f"remove {new_id}"))
        check("tg garbage -> help",
              "Didn't understand" in handle_message("hello there"))

        # Multi-session add: one message, several specs
        n_before = len(load_state()["targets"])
        reply = handle_message("12/26 9:00am drill\n12/27 4pm 4.0+, 12/28 10:30am")
        check("tg multi-add queues all three",
              len(load_state()["targets"]) == n_before + 3)
        check("tg multi-add echoes each", reply.count("✅ Target") == 3)
        reply = handle_message("12/29 8am drill, gibberish here")
        check("tg multi-add flags bad segment",
              "✅ Target" in reply and "needs at least a date and a time" in reply)
        check("tg multi-add still queues good segment",
              len(load_state()["targets"]) == n_before + 4)
    STATE_PATH, BASE_DIR, ENV_PATH, AUTH_PATH = old

    print(f"\n{'ALL TESTS PASSED' if not failures else f'{len(failures)} FAILURE(S)'}")
    return 0 if not failures else 1


# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("status")
    sub.add_parser("plan")
    sub.add_parser("enable")
    sub.add_parser("disable")
    sub.add_parser("check")
    sub.add_parser("selftest")
    sub.add_parser("login-test")
    sub.add_parser("notify-test")
    sub.add_parser("listen")
    sub.add_parser("booked")
    sub.add_parser("dry-run")
    p_timer = sub.add_parser("install-timer")
    p_timer.add_argument("--crontab", action="store_true",
                         help="install a crontab entry instead of a systemd user timer")
    p_add = sub.add_parser("add")
    p_add.add_argument("spec", nargs="*",
                       help="shorthand: date time keyword in any order, e.g. 8/11 4pm 4.0+")
    p_add.add_argument("--date", help="YYYY-MM-DD of the session")
    p_add.add_argument("--time", help="session start, e.g. 4pm, 17:30, 5:30pm")
    p_add.add_argument("--keyword",
                       help="text that identifies the session, e.g. '3.0-3.75'")
    p_rm = sub.add_parser("remove")
    p_rm.add_argument("--id")
    p_rm.add_argument("--all", action="store_true")
    p_now = sub.add_parser("book-now")
    p_now.add_argument("--id", required=True)
    p_cancel = sub.add_parser("cancel")
    p_cancel.add_argument("--id", required=True)
    p_cancel.add_argument("--headed", action="store_true",
                          help="show the browser window (recommended for the first cancel)")
    args = parser.parse_args()

    if args.cmd == "selftest":
        return cmd_selftest()

    state = load_state()
    if args.cmd is None:
        if not state["enabled"]:
            return 0  # switched off: fully silent
        return cmd_cron(state)
    if args.cmd == "status":
        return cmd_status(state)
    if args.cmd == "plan":
        return cmd_plan(state)
    if args.cmd == "enable":
        return cmd_toggle(state, True)
    if args.cmd == "disable":
        return cmd_toggle(state, False)
    if args.cmd == "add":
        return cmd_add(state, args)
    if args.cmd == "remove":
        if not args.all and not args.id:
            print("Need --id or --all")
            return 1
        return cmd_remove(state, args)
    if args.cmd == "install-timer":
        return cmd_install_timer(args)
    if args.cmd == "check":
        return cmd_check(state)
    if args.cmd == "listen":
        return cmd_listen()
    if args.cmd == "login-test":
        return cmd_login_test(state)
    if args.cmd == "notify-test":
        env = read_env()
        if not env.get("TELEGRAM_BOT_TOKEN") or not env.get("TELEGRAM_CHAT_ID"):
            print(f"❌ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set in {ENV_PATH}")
            return 1
        if notify("🏓 Lifetime booking — notify test. If you can read this, "
                  "booking results will reach you."):
            print("✅ Sent — check your Telegram.")
            return 0
        print("❌ Telegram API call failed — token or chat_id is wrong "
              "(watch stderr above for the error).")
        return 1
    if args.cmd == "booked":
        return cmd_booked(state)
    if args.cmd == "dry-run":
        return cmd_dry_run(state)
    if args.cmd == "book-now":
        return cmd_book_now(state, args)
    if args.cmd == "cancel":
        return cmd_cancel(state, args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
