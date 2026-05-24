import os
from datetime import datetime, timedelta, timezone
import logging
from app.calendar.google_client import GoogleCalendarClient

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

def check_slot_availability(slot_start: datetime, slot_end: datetime) -> bool:
    """
    Checks if a specific start/end window is free.
    Returns True if free, False if busy.
    """
    service, is_mock = GoogleCalendarClient.get_service()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "mock-calendar-id")
    
    # Query FreeBusy for this exact window
    body = {
        "timeMin": slot_start.isoformat(),
        "timeMax": slot_end.isoformat(),
        "items": [{"id": calendar_id}]
    }
    
    try:
        query = service.freebusy().query(body=body)
        result = query.execute()
        calendars = result.get("calendars", {})
        calendar_data = calendars.get(calendar_id, {})
        busy_data = calendar_data.get("busy", [])
        
        # If there are any busy intervals overlapping this slot, it's not available
        for busy in busy_data:
            b_start = datetime.fromisoformat(busy["start"].replace("Z", "+00:00")).astimezone(IST)
            b_end = datetime.fromisoformat(busy["end"].replace("Z", "+00:00")).astimezone(IST)
            if slot_start < b_end and slot_end > b_start:
                return False
        return True
    except Exception as e:
        logger.error(f"Error checking slot availability: {e}")
        # Default to True on errors (fallback)
        return True

def create_test_drive_event(
    customer_name: str,
    phone_number: str,
    car_model: str,
    car_variant: str,
    slot_start: datetime,
    slot_end: datetime
) -> str:
    """
    Creates an event in Google Calendar (or Mock) for the test drive.
    Revalidates slot availability first to prevent double-booking.
    Returns the event ID.
    """
    # 1. Revalidate slot availability
    if not check_slot_availability(slot_start, slot_end):
        raise ValueError("The selected time slot is no longer available. Please select another slot.")
        
    service, is_mock = GoogleCalendarClient.get_service()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "mock-calendar-id")
    
    event_summary = f"Test Drive - {car_model} {car_variant} - {customer_name}"
    event_description = (
        f"Test drive booked via WhatsApp Agent.\n\n"
        f"Customer Name: {customer_name}\n"
        f"Phone Number: {phone_number}\n"
        f"Car: {car_model} {car_variant}\n"
        f"Status: Confirmed"
    )
    
    event_body = {
        "summary": event_summary,
        "description": event_description,
        "start": {
            "dateTime": slot_start.isoformat(),
            "timeZone": "Asia/Kolkata"
        },
        "end": {
            "dateTime": slot_end.isoformat(),
            "timeZone": "Asia/Kolkata"
        }
    }
    
    try:
        event = service.events().insert(calendarId=calendar_id, body=event_body).execute()
        event_id = event.get("id")
        logger.info(f"Successfully booked event on Google Calendar. Event ID: {event_id}")
        return event_id
    except Exception as e:
        logger.error(f"Failed to create Google Calendar event: {e}")
        raise RuntimeError(f"Google Calendar event creation failed: {e}")

def delete_test_drive_event(event_id: str) -> None:
    """Deletes an event from Google Calendar."""
    if not event_id:
        return
    service, is_mock = GoogleCalendarClient.get_service()
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "mock-calendar-id")
    try:
        service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
        logger.info(f"Successfully deleted event {event_id} from Google Calendar.")
    except Exception as e:
        logger.error(f"Failed to delete Google Calendar event {event_id}: {e}")
