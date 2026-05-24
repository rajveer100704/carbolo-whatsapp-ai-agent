# Implementation Plan - CarBOLO WhatsApp AI Agent Bonus Features & Enhancements

This plan outlines the design and implementation steps for completing the three stretch bonus requirements and enhancing the Google Calendar integration in the CarBOLO WhatsApp agent, while ensuring all core zero-hallucination and RAG guardrails remain robust.

## Proposed Changes

We will implement the following changes across the database, calendar API client, state machine, and test suites.

---

### 1. Calendar Integration

We will extend the Google Calendar integration to handle event cancellation (deletion) on both the real API and the mock service during reschedule and cancel flows.

#### [MODIFY] [google_client.py](file:///c:/Users/BIT/CarBOLO/app/calendar/google_client.py)
* Add a `delete` method in the inner `EventsRequest` class inside `MockGoogleCalendarService` to simulate the Google Calendar Events delete API in memory:
  ```python
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
  ```

#### [MODIFY] [booking.py](file:///c:/Users/BIT/CarBOLO/app/calendar/booking.py)
* Expose a `delete_test_drive_event(event_id: str)` helper function:
  ```python
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
  ```

---

### 2. Scheduler Component

We will add a helper to cancel scheduled APScheduler jobs to release resources cleanly when a booking is cancelled or rescheduled.

#### [MODIFY] [reminders.py](file:///c:/Users/BIT/CarBOLO/app/scheduler/reminders.py)
* Add a `cancel_reminder_job(cls, reminder_id: int)` method to `ReminderScheduler` to deregister APScheduler jobs:
  ```python
  @classmethod
  def cancel_reminder_job(cls, reminder_id: int):
      """Cancels a scheduled reminder job."""
      if cls._scheduler is not None:
          job_id = f"reminder_{reminder_id}"
          if cls._scheduler.get_job(job_id):
              cls._scheduler.remove_job(job_id)
              logger.info(f"Successfully cancelled job {job_id} in APScheduler.")
  ```

---

### 3. Agent NLU & State Machine

We will update the Gemini intent prompt to recognize reschedule intents, implement the FSM transitions for the three qualification states, and incorporate calendar event deletion into the cancel/reschedule logic.

#### [MODIFY] [intent.py](file:///c:/Users/BIT/CarBOLO/app/agent/intent.py)
* Update the Gemini prompt in `parse_intent_with_llm` to define and allow `INTENT_RESCHEDULE`.
* Ensure that the reschedule keyword heuristic logic and LLM NLU both route properly to `INTENT_RESCHEDULE`.

#### [MODIFY] [state.py](file:///c:/Users/BIT/CarBOLO/app/agent/state.py)
* Implement FSM handlers inside `transition_state` for:
  - `STATE_QUALIFYING_BUDGET`
  - `STATE_QUALIFYING_TIMELINE`
  - `STATE_QUALIFYING_FUEL`
* Add Q&A interception in each of these states. If the user asks a question about car specs mid-flow, we retrieve context, answer the question, and immediately ask the qualification question again to keep the process on track.
* Update `INTENT_CANCEL` and `INTENT_RESCHEDULE` handlers:
  - Call `delete_test_drive_event(active_booking.calendar_event_id)` to delete/cancel the Google Calendar event.
  - Call `ReminderScheduler.cancel_reminder_job(r.id)` on each canceled reminder.
* Update `suggest_and_transition_slots` to use interactive WhatsApp buttons or bullet formatting depending on what inputs the user prefers.

---

### 4. Tests & Verification

#### [MODIFY] [test_agent.py](file:///c:/Users/BIT/CarBOLO/tests/test_agent.py)
* Add unit tests for:
  - Lead qualification flow (verifying progression through budget, timeline, and fuel type).
  - Rescheduling flow (ensuring old booking is marked CANCELLED, Google Calendar event is deleted, and new slots are suggested).
  - Returning user welcoming logic (verifying greeting customization by name).

---

## Verification Plan

### Automated Tests
* Run `python -m pytest` to execute the updated and existing test suite.

### Manual Verification
* Delete the SQLite file `data/carbolo.db` so the updated schema recreates cleanly.
* Restart the FastAPI server locally: `uvicorn app.main:app --reload`.
* Perform end-to-end integration tests using a real WhatsApp client:
  1. Greet the bot (`"Hi"`) -> should output default welcome message.
  2. Initiate booking (`"Book Brezza VXi"`) -> should enter qualification flow and ask for budget.
  3. Respond with budget -> should ask for timeline.
  4. Respond with timeline -> should ask for fuel preference.
  5. Respond with fuel type -> should present available slots.
  6. Confirm slot booking -> should create Google Calendar event, schedule reminders, and mark `lead_completed` as True.
  7. Reschedule (`"reschedule"`) -> should cancel the booking (and calendar event) and immediately suggest new slots.
  8. Cancel (`"cancel"`) -> should cancel booking and reminders.
  9. Greet again (`"Hi"`) -> should welcome back by customer name.
