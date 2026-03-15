# Lifetime Fitness Pickleball Auto-Booker

Automates booking pickleball courts on [my.lifetime.life](https://my.lifetime.life) using Playwright.
Runs locally on macOS; credentials stay in a `.env` file and are never committed.

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Lifetime login email and password
```

### 3. Edit `config.yaml`

| Key | Description |
|-----|-------------|
| `club_name` | Your club name as shown on the site (e.g. `"Bloomington"`) |
| `sport` | `pickleball` |
| `preferred_days` | List of days to target (lowercase, e.g. `monday`) |
| `preferred_times` | List of 24h times to try (e.g. `"07:00"`) |
| `booking_horizon_days` | How many days ahead to look (default `7`) |
| `headless` | `true` to run silently; `false` to watch the browser |

---

## Usage

```bash
# Book upcoming slots per config (live run)
python book.py

# Watch the browser while booking (good for first run)
python book.py --headed

# Find available slots without actually booking
python book.py --dry-run

# Debug: headed + dry-run
python book.py --headed --dry-run
```

macOS desktop notifications appear on success and failure.

---

## First-time verification

Run this to confirm login and slot detection work before making real bookings:

```bash
python book.py --headed --dry-run
```

---

## Cron setup

Lifetime opens booking windows at **N days + 22 hours** in advance:
- **Signature members**: 7 days ahead (window opens at 10am → run at ~10:01am, 7 days prior)
- **Other members**: 6 days ahead

### Cron timing calculator

If you want to book a **Monday 7:00am** slot as a Signature member:
- The window opens at **Monday 10:00am, 7 days prior** = the previous Monday at 10am
- Schedule cron to run at **10:01am every Monday**:

```cron
1 10 * * 1 cd /path/to/lifetimebookings && python book.py >> logs/cron.log 2>&1
```

General formula:
```
minute hour * * <day_of_week>
```

Where `day_of_week` is: 0=Sun, 1=Mon, 2=Tue, 3=Wed, 4=Thu, 5=Fri, 6=Sat

Edit your crontab:
```bash
crontab -e
```

---

## IMPORTANT: Selector customization required

The booking page selectors in `src/booking.py` are **placeholders**.
Lifetime's booking UI is JavaScript-rendered and the exact selectors depend on the live page.

To find the real selectors:

1. Run `python book.py --headed --dry-run` to open the browser.
2. After login, manually navigate to the court reservations page.
3. Open DevTools (Cmd+Option+I) → Inspector.
4. Find the time slot elements and the "Book" / "Reserve" buttons.
5. Update the selectors in:
   - `src/booking.py` → `_select_club()`, `_select_date()`, `_find_slot()`, `_confirm_booking()`
   - `src/auth.py` → `_do_login()` (if the login form selectors differ)
6. Also update `BOOKING_BASE_URL` in `src/booking.py` with the actual URL.

---

## Project structure

```
lifetimebookings/
├── .env                  # Credentials (gitignored)
├── .env.example          # Template
├── config.yaml           # Booking preferences
├── book.py               # CLI entry point
├── src/
│   ├── config.py         # Load + validate config
│   ├── auth.py           # Login + session management
│   ├── booking.py        # Slot discovery + booking
│   └── notifier.py       # macOS notifications
├── logs/
│   └── .gitkeep
└── requirements.txt
```

Sessions are cached in `storage_state.json` (gitignored) so you don't need to log in on every run.

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| Login fails | Check `.env` credentials; run `--headed` to see the browser |
| No slots found | Update selectors in `src/booking.py` to match the live page |
| CAPTCHA appears | Run `--headed` and complete it manually once to seed the session |
| Booking page URL changed | Update `BOOKING_BASE_URL` in `src/booking.py` |
| Session expired | Delete `storage_state.json` to force a fresh login |
