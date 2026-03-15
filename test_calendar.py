"""Quick test: create a dummy Google Calendar event to verify integration."""

from datetime import datetime, timedelta
from src.notifier import add_to_calendar

# Use tomorrow at 10:00 AM as a test event
start = datetime.now().replace(hour=10, minute=0, second=0, microsecond=0) + timedelta(days=1)
end = start + timedelta(hours=1, minutes=30)

print(f"Creating test calendar event: {start.strftime('%A %b %d, %Y %I:%M %p')} → {end.strftime('%I:%M %p')}")
add_to_calendar(
    session_name="TEST: Pickleball Open Play : 4.0+ DUPR Optional",
    start_dt=start,
    end_dt=end,
    timezone="America/New_York",
)
print("Done — check Google Calendar for the event.")
