# Hermes-driven booking sniper

`lifetime_booking.py` is the standalone successor to the cron approach in the
root README: queue targets in a state file, an OS scheduler (crontab on macOS,
systemd timer on Linux) ticks it every 10 minutes, and when a target's
registration window (session start − 7d22h) opens within the next few minutes
the run sleeps to the exact open second and books. Results go straight to
Telegram. A Hermes agent acts purely as the remote control (see `SKILL.md`),
but the script has no Hermes dependency.

Install on the machine that will do the booking:

```bash
mkdir -p ~/.hermes/scripts ~/.hermes/state/lifetime_booking
curl -fsSL https://raw.githubusercontent.com/mambabfk/lifetimebookings/hermes/hermes/lifetime_booking.py \
  -o ~/.hermes/scripts/lifetime_booking.py
python3 ~/.hermes/scripts/lifetime_booking.py selftest
```

Then follow the one-time setup in `SKILL.md` (credentials file, playwright,
`install-timer`, `check`, `login-test`).
