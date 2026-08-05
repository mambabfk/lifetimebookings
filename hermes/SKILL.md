---
name: lifetime-pickleball
description: Book Lifetime Fitness pickleball sessions the instant their registration window opens. Use when Tim asks to book a pickleball session, turn pickleball booking on/off, check booking status, or schedule a booking for a specific date/time. Triggers on "book pickleball", "pickleball tuesday 5:30", "turn off pickleball booking", "did the booking work", "what bookings are queued".
platforms: [linux, macos]
---

# Lifetime pickleball auto-booking

Everything is driven by one script: `~/.hermes/scripts/lifetime_booking.py`.
Registration for a session opens **7 days 22 hours before it starts** (a Mon
8:30 PM class opens the previous Sun 10:30 PM ET). All times are
**America/New_York** regardless of this machine's timezone.

**Architecture — Hermes is only the ignitor.** The booking engine is a
systemd user timer (or crontab entry) that ticks the script every 10 minutes,
completely independent of the Hermes gateway; when a queued target's window
opens within the next few minutes, the run sleeps to the exact open second
and books it, then reports straight to Telegram via bot API. Hermes's job is
just to edit the queue (add/remove/enable/disable/status) when Tim asks in
chat — bookings still fire even if Hermes is down or restarting.

## Commands

Run with `python3 ~/.hermes/scripts/lifetime_booking.py <command>`.

| Tim says | Run |
|----------|-----|
| "8/11 4pm 4.0+" (shorthand — pass it straight through) | `add 8/11 4pm 4.0+` |
| "book pickleball Tue 3/24 5:30pm, the 3.0-3.75 one" | `add 3/24 5:30pm 3.0-3.75` |
| "turn pickleball booking off" / "on" | `disable` / `enable` |
| "what's queued?" / "did it work?" | `status` |
| "when will it fire?" | `plan` |
| "cancel the tuesday one" | `status` to find the id, then `remove --id <id>` |
| "book it right now, window's already open" | `book-now --id <id>` |

Notes:
- `add` takes shorthand directly: a date (`8/11`, `3/24/27`, or ISO), a time
  (`4pm`, `5:30pm`, `16:00`), and a keyword — in any order. A date without a
  year means the next future occurrence. The keyword is text that appears in
  the session name on the schedule (skill level like `"3.0-3.75"` or `"4.0+"`
  works best). So when Tim sends something like "8/11 4pm 4.0+", pass it
  through verbatim: `add 8/11 4pm 4.0+`. `add` echoes back the resolved
  session and computed open time — always relay that to Tim so he can
  sanity-check it.
- The state file is `~/.hermes/state/lifetime_booking.json`; club defaults to
  PENN 1 (`club_path`), participant Tim. Edit that file for a different club.
- Booking results (booked / waitlisted / failed) are sent straight to
  Telegram by the tick itself. `status` shows the last few outcomes and
  whether the independent scheduler is armed.

## One-time setup (if not done yet)

1. Credentials + delivery: create `~/.hermes/state/lifetime_booking/.env` with
   `LIFETIME_EMAIL=...`, `LIFETIME_PASSWORD=...`, and (for direct Telegram
   delivery) `TELEGRAM_BOT_TOKEN=...` / `TELEGRAM_CHAT_ID=...` — reuse the
   bot token from Hermes's own config and the chat_id from
   `~/.hermes/cron/jobs.json` origin. This path is gitignored — never put
   credentials anywhere else, the repo auto-syncs to GitHub daily.
2. `pip install playwright && playwright install chromium --with-deps`
3. Arm the engine: `install-timer` (systemd user timer, every 10 min;
   `install-timer --crontab` on boxes without a systemd user session).
   If it warns about lingering, run `loginctl enable-linger <user>`.
4. Verify: `check` (environment + scheduler armed), `selftest` (logic),
   `login-test` (credentials — also seeds the saved session), `dry-run`
   (finds queued sessions on the live schedule without booking).

## Troubleshooting

- **"session not found on schedule page"** — the class isn't visible yet,
  the keyword doesn't match the listing text, or the club path is wrong.
  Run `dry-run` a day early to confirm the target is visible.
- **Login failures** — Lifetime may CAPTCHA headless logins. Seed
  `~/.hermes/state/lifetime_booking/auth.json` by copying a
  `storage_state.json` produced by a headed login elsewhere; the script
  refreshes it on every successful login afterwards.
- **Missed booking (⚠️ in output)** — the box was down across the whole
  window→start span. Nothing to fix retroactively; re-add the next occurrence.
