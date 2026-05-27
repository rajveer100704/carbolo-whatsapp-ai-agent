import os
import json
import logging
from google.oauth2 import service_account
from googleapiclient.discovery import build

logger = logging.getLogger(__name__)

class GoogleCalendarClient:
    _service = None
    _is_mock = False

    @classmethod
    def get_service(cls):
        if cls._service is not None:
            return cls._service, cls._is_mock

        # Read configurations
        service_account_json_str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "mock-google-json")
        calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "mock-calendar-id")

        if service_account_json_str == "mock-google-json" or calendar_id == "mock-calendar-id":
            logger.info("Google Calendar Service Account not configured or set to mock. Running in MOCK mode.")
            cls._is_mock = True
            cls._service = MockGoogleCalendarService()
            return cls._service, cls._is_mock

        try:
            # Parse service account JSON (it can be a filepath, base64 string, or raw JSON string)
            if os.path.exists(service_account_json_str):
                with open(service_account_json_str, "r") as f:
                    info = json.load(f)
            else:
                # Try base64 decoding if the string doesn't start with standard JSON braces
                raw_str = service_account_json_str.strip()
                if not raw_str.startswith("{"):
                    try:
                        import base64
                        decoded = base64.b64decode(raw_str).decode("utf-8")
                        if decoded.strip().startswith("{"):
                            raw_str = decoded.strip()
                    except Exception:
                        pass
                info = json.loads(raw_str)

            scopes = ["https://www.googleapis.com/auth/calendar"]
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
            cls._service = build("calendar", "v3", credentials=creds)
            cls._is_mock = False
            logger.info("Successfully authenticated and built Google Calendar client.")
        except Exception as e:
            logger.exception("Failed to build real Google Calendar client. Falling back to MOCK mode.")
            cls._is_mock = True
            cls._service = MockGoogleCalendarService()

        return cls._service, cls._is_mock

class MockGoogleCalendarService:
    """Mock Google Calendar Service that mirrors FreeBusy and Events behavior using in-memory dicts."""
    def __init__(self):
        self.events_list = []

    def freebusy(self):
        class FreeBusyRequest:
            def __init__(self, service):
                self.service = service
            def query(self, body):
                class QueryRequest:
                    def __init__(self, service, query_body):
                        self.service = service
                        self.query_body = query_body
                    def execute(self):
                        # Extract requested timeMin and timeMax
                        time_min = self.query_body.get("timeMin")
                        time_max = self.query_body.get("timeMax")
                        # Return all matching in-memory events
                        busy_slots = []
                        for event in self.service.events_list:
                            start = event["start"]["dateTime"]
                            end = event["end"]["dateTime"]
                            if start < time_max and end > time_min:
                                busy_slots.append({"start": start, "end": end})
                        
                        return {
                            "calendars": {
                                self.query_body.get("items", [{}])[0].get("id", "primary"): {
                                    "busy": busy_slots
                                }
                            }
                        }
                return QueryRequest(self.service, body)
        return FreeBusyRequest(self)

    def events(self):
        class EventsRequest:
            def __init__(self, service):
                self.service = service
            def insert(self, calendarId, body):
                class InsertRequest:
                    def __init__(self, service, cal_id, event_body):
                        self.service = service
                        self.cal_id = cal_id
                        self.event_body = event_body
                    def execute(self):
                        # Generate a mock event ID
                        import uuid
                        event_id = f"mock-event-{uuid.uuid4()}"
                        event = {
                            "id": event_id,
                            "summary": self.event_body.get("summary"),
                            "description": self.event_body.get("description"),
                            "start": self.event_body.get("start"),
                            "end": self.event_body.get("end"),
                            "status": "confirmed"
                        }
                        self.service.events_list.append(event)
                        logger.info(f"[Mock Calendar] Created Event: {event_id} - {event['summary']}")
                        return event
                return InsertRequest(self.service, calendarId, body)
            def delete(self, calendarId, eventId):
                class DeleteRequest:
                    def __init__(self, service, cal_id, event_id):
                        self.service = service
                        self.cal_id = cal_id
                        self.event_id = event_id
                    def execute(self):
                        self.service.events_list = [e for e in self.service.events_list if e["id"] != self.event_id]
                        logger.info(f"[Mock Calendar] Deleted Event: {self.event_id}")
                        return {}
                return DeleteRequest(self.service, calendarId, eventId)
        return EventsRequest(self)
