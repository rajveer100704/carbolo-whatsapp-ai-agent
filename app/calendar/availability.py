import os
from datetime import datetime, timedelta, timezone, date
import logging
from app.calendar.google_client import GoogleCalendarClient

logger = logging.getLogger(__name__)

# IST offset is UTC + 5:30
IST = timezone(timedelta(hours=5, minutes=30))

def get_ist_now() -> datetime:
    """Returns the current datetime in IST."""
    return datetime.now(timezone.utc).astimezone(IST)

def generate_candidate_slots(target_date: date) -> list[tuple[datetime, datetime]]:
    """
    Generates candidate 30-minute slots between 9:00 AM and 6:00 PM on a given date.
    Returns a list of (start_datetime, end_datetime) in IST.
    """
    slots = []
    # Start at 9:00 AM IST, end at 6:00 PM IST
    start_hour = 9
    end_hour = 18
    
    current_time = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=IST)
    current_time = current_time.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    end_limit = current_time.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    
    while current_time + timedelta(minutes=30) <= end_limit:
        slot_start = current_time
        slot_end = current_time + timedelta(minutes=30)
        slots.append((slot_start, slot_end))
        current_time += timedelta(minutes=30)
        
    return slots

def get_available_slots(target_date: date, limit: int = 3) -> list[tuple[datetime, datetime]]:
    """
    Checks busy times against Google Calendar (or mock) for the target date,
    filters out past times, and returns the first `limit` available 30-min slots.
    """
    service, is_mock = GoogleCalendarClient.get_service()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "mock-calendar-id")
    
    # Calculate timeMin and timeMax for FreeBusy request (entire day)
    day_start = datetime.combine(target_date, datetime.min.time()).replace(tzinfo=IST)
    day_end = day_start + timedelta(days=1)
    
    # Query FreeBusy
    body = {
        "timeMin": day_start.isoformat(),
        "timeMax": day_end.isoformat(),
        "items": [{"id": calendar_id}]
    }
    
    busy_intervals = []
    try:
        query = service.freebusy().query(body=body)
        result = query.execute()
        calendars = result.get("calendars", {})
        calendar_data = calendars.get(calendar_id, {})
        busy_data = calendar_data.get("busy", [])
        
        for busy in busy_data:
            # Parse ISO 8601 string to datetime
            b_start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00")).astimezone(IST)
            b_end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00")).astimezone(IST)
            busy_intervals.append((b_start, b_end))
    except Exception as e:
        logger.error(f"Error querying FreeBusy calendar: {e}")
        # In case of error, treat calendar as empty (or mock list)
        pass
        
    candidates = generate_candidate_slots(target_date)
    now_ist = get_ist_now()
    
    free_slots = []
    for start, end in candidates:
        # Skip slots in the past
        if start <= now_ist + timedelta(hours=1):  # must be at least 1 hour in the future
            continue
            
        # Check overlap with busy intervals
        overlap = False
        for b_start, b_end in busy_intervals:
            # Overlap if start is before busy end and end is after busy start
            if start < b_end and end > b_start:
                overlap = True
                break
                
        if not overlap:
            free_slots.append((start, end))
            if len(free_slots) >= limit:
                break
                
    return free_slots
