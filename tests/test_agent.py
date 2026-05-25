import os
import sys
import pytest
import pytest_asyncio
from datetime import datetime, timedelta, timezone
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy import select

# Add parent directory to path to import app
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Set environment variables for testing
os.environ["GEMINI_API_KEY"] = "mock-gemini-key"
os.environ["WHATSAPP_ACCESS_TOKEN"] = "mock-access-token"
os.environ["GOOGLE_CALENDAR_ID"] = "mock-calendar-id"

# Setup testing in-memory SQLite database
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"
test_engine = create_async_engine(TEST_DATABASE_URL, connect_args={"check_same_thread": False})
TestSessionLocal = async_sessionmaker(bind=test_engine, class_=AsyncSession, expire_on_commit=False)

# Override the app session factory and engine BEFORE importing other app components
import app.db.session
app.db.session.async_session_factory = TestSessionLocal
app.db.session.engine = test_engine

# Import app components after environment variables and session overrides are set
from app.db.models import Base, UserConversationState, Booking, Reminder, ProcessedWebhookMessage
from app.rag.kb_loader import KnowledgeBase
from app.rag.retriever import retrieve_context
from app.agent.intent import parse_intent_heuristics, validate_and_guard_response, generate_grounded_response
from app.agent.state import (
    get_or_create_user_state,
    transition_state,
    STATE_IDLE,
    STATE_AWAITING_SLOT,
    STATE_AWAITING_CONFIRMATION,
    STATE_QUALIFYING_BUDGET,
    STATE_QUALIFYING_TIMELINE,
    STATE_QUALIFYING_FUEL
)
from app.agent.flow import handle_whatsapp_webhook
from app.calendar.availability import get_available_slots, IST

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initializes in-memory test database tables."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

@pytest.mark.asyncio
async def test_kb_grounding():
    """Tests knowledge base structured context retrieval and fallback guardrails."""
    # Brezza VXi doesn't have sunroof
    context = retrieve_context("Brezza VXi me sunroof hai?")
    assert "Model: Maruti Brezza" in context
    assert "Variant: VXi" in context
    assert "Sunroof: NO" in context

    # Test the strict output guardrail blocks positive hallucinated claims
    valid = validate_and_guard_response("The Maruti Brezza VXi has sunroof.", context, "sunroof details for VXi?")
    assert "I don't have that information in the dealership knowledge base." in valid

    # Test the exact required response for Brezza VXi sunroof query
    custom_resp = await generate_grounded_response("Brezza VXi me sunroof hai?", context)
    assert custom_resp == "The Brezza VXi doesn't come with a sunroof – that's on the ZXi+ variant. Want me to share the VXi features, or are you interested in the ZXi+?"

    # Test that valid negative grounded answers are ALLOWED
    valid_neg = validate_and_guard_response("No, the Brezza VXi does not come with a sunroof.", context, "sunroof details for VXi?")
    assert "does not come with a sunroof" in valid_neg

    # Swift LXi doesn't have ADAS
    context_swift = retrieve_context("Swift LXi me ADAS features hai kya?")
    assert "Model: Maruti Swift" in context_swift
    assert "Variant: LXi" in context_swift
    # Check that ADAS keyword in query triggers fallback immediately
    valid_swift = validate_and_guard_response("Yes, the Swift LXi comes with ADAS features.", context_swift, "ADAS features in Swift LXi?")
    assert "I don't have that information in the dealership knowledge base." in valid_swift
    
    # Check that query with ADAS even with a negative response triggers fallback
    valid_swift_neg = validate_and_guard_response("No, the Swift LXi does not have ADAS.", context_swift, "Does it have ADAS?")
    assert "I don't have that information in the dealership knowledge base." in valid_swift_neg

@pytest.mark.asyncio
async def test_intent_detection():
    """Tests heuristic intent parsing."""
    # Quick slot select
    res = parse_intent_heuristics("slot_2", "STATE_AWAITING_SLOT")
    assert res["intent"] == "INTENT_SELECT_SLOT"
    assert res["entities"]["slot_index"] == 2

    # Confirmation
    res = parse_intent_heuristics("haan, confirm", "STATE_AWAITING_CONFIRMATION")
    assert res["intent"] == "INTENT_CONFIRM"

    # Booking request
    res = parse_intent_heuristics("I want to book a test drive for brezza vxi", "STATE_IDLE")
    assert res["intent"] == "INTENT_BOOK_REQUEST"
    assert res["entities"]["car_model"] == "Maruti Brezza"
    assert res["entities"]["car_variant"] == "VXi"

@pytest.mark.asyncio
async def test_flow_webhook_deduplication():
    """Tests that duplicate webhooks with the same message_id are ignored."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Rajveer"}, "wa_id": "919999999999"}],
                    "messages": [{
                        "from": "919999999999",
                        "id": "msg_unique_101",
                        "timestamp": "1716480000",
                        "text": {"body": "hi"},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    # First webhook call
    res1 = await handle_whatsapp_webhook(payload)
    assert res1["status"] == "success"

    # Second webhook call (duplicate message ID)
    res2 = await handle_whatsapp_webhook(payload)
    assert res2["status"] == "duplicate"

@pytest.mark.asyncio
async def test_booking_flow_and_idempotency():
    """Tests the state transition from booking init to calendar creation and idempotency protection."""
    async with TestSessionLocal() as session:
        # Create user state directly
        user_state = await get_or_create_user_state(session, "919999999999")
        user_state.state = STATE_AWAITING_CONFIRMATION
        user_state.selected_car_model = "Maruti Brezza"
        user_state.selected_car_variant = "VXi"
        
        # Set selected slot (tomorrow 4pm to 4:30pm)
        tomorrow = datetime.now(timezone.utc).astimezone(IST) + timedelta(days=1)
        slot_start = tomorrow.replace(hour=16, minute=0, second=0, microsecond=0).replace(tzinfo=None)
        slot_end = tomorrow.replace(hour=16, minute=30, second=0, microsecond=0).replace(tzinfo=None)
        
        user_state.selected_slot_start = slot_start
        user_state.selected_slot_end = slot_end
        await session.commit()

    # Now simulate the confirm webhook message
    payload_confirm = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Rajveer"}, "wa_id": "919999999999"}],
                    "messages": [{
                        "from": "919999999999",
                        "id": "msg_confirm_1",
                        "text": {"body": "confirm"},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    # First confirmation (books slot)
    res1 = await handle_whatsapp_webhook(payload_confirm)
    assert res1["status"] == "success"

    # Verify booking and reminders created in DB
    async with TestSessionLocal() as session:
        bookings_query = select(Booking).where(Booking.phone_number == "919999999999")
        b_res = await session.execute(bookings_query)
        bookings = b_res.scalars().all()
        assert len(bookings) == 1
        assert bookings[0].status == "COMPLETED"
        assert bookings[0].car_model == "Maruti Brezza"

        # Verify reminders
        reminders_query = select(Reminder).where(Reminder.booking_id == bookings[0].id)
        rem_res = await session.execute(reminders_query)
        reminders = rem_res.scalars().all()
        assert len(reminders) == 2
        assert any(r.reminder_type == "24H" for r in reminders)
        assert any(r.reminder_type == "2H" for r in reminders)

    # Double-tap confirm simulation (same request, but new message ID)
    payload_confirm_double = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Rajveer"}, "wa_id": "919999999999"}],
                    "messages": [{
                        "from": "919999999999",
                        "id": "msg_confirm_double_tap",
                        "text": {"body": "confirm"},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    res2 = await handle_whatsapp_webhook(payload_confirm_double)
    assert res2["status"] == "success"

    # Assert that no second booking was created
    async with TestSessionLocal() as session:
        bookings_query = select(Booking).where(Booking.phone_number == "919999999999")
        b_res = await session.execute(bookings_query)
        bookings = b_res.scalars().all()
        assert len(bookings) == 1  # Still exactly one booking!

import json

@pytest.mark.asyncio
async def test_session_timeout():
    """Tests that a conversation state is reset to STATE_IDLE after 30 minutes of inactivity."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "918888888888")
        user_state.state = STATE_AWAITING_SLOT
        user_state.selected_car_model = "Maruti Swift"
        user_state.selected_car_variant = "ZXi+"
        # Set updated_at to 31 minutes ago
        user_state.updated_at = datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None) - timedelta(minutes=31)
        await session.commit()

    async with TestSessionLocal() as session:
        # get_or_create_user_state should trigger timeout check and reset
        refreshed_state = await get_or_create_user_state(session, "918888888888")
        assert refreshed_state.state == STATE_IDLE
        assert refreshed_state.selected_car_model is None
        assert refreshed_state.selected_car_variant is None

@pytest.mark.asyncio
async def test_slot_expiration():
    """Tests that picking a slot fails if the slot was generated more than 10 minutes ago."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "917777777777")
        user_state.state = STATE_AWAITING_SLOT
        user_state.selected_car_model = "Maruti Ertiga"
        user_state.selected_car_variant = "VXi"
        # Mock slots JSON
        tomorrow = datetime.now(timezone.utc).astimezone(IST) + timedelta(days=1)
        slot_start = tomorrow.replace(hour=10, minute=0, second=0, microsecond=0)
        slot_end = tomorrow.replace(hour=10, minute=30, second=0, microsecond=0)
        user_state.slots_json = json.dumps([{"start": slot_start.isoformat(), "end": slot_end.isoformat()}])
        # Set slot_generated_at to 11 minutes ago
        user_state.slot_generated_at = datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None) - timedelta(minutes=11)
        await session.commit()

    # Trigger a slot selection webhook message
    payload_select = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Test User"}, "wa_id": "917777777777"}],
                    "messages": [{
                        "from": "917777777777",
                        "id": "msg_select_expired",
                        "interactive": {
                            "type": "button_reply",
                            "button_reply": {
                                "id": "slot_1",
                                "title": "Sat 10:00 AM"
                            }
                        },
                        "type": "interactive"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    # Should run successfully, but state should stay in STATE_AWAITING_SLOT with fresh slot_generated_at
    res = await handle_whatsapp_webhook(payload_select)
    assert res["status"] == "success"

    async with TestSessionLocal() as session:
        refreshed_state = await get_or_create_user_state(session, "917777777777")
        assert refreshed_state.state == STATE_AWAITING_SLOT
        # Check slot_generated_at is now recent (less than 1 minute ago)
        now_ist = datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)
        assert now_ist - refreshed_state.slot_generated_at < timedelta(minutes=1)

@pytest.mark.asyncio
async def test_cancel_active_booking():
    """Tests the cancellation flow for an active booking."""
    async with TestSessionLocal() as session:
        # Create a completed booking
        booking = Booking(
            phone_number="916666666666",
            customer_name="Aman",
            car_model="Maruti Swift",
            car_variant="ZXi+",
            slot_start=datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None) + timedelta(days=1),
            slot_end=datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None) + timedelta(days=1, minutes=30),
            status="COMPLETED"
        )
        session.add(booking)
        await session.flush()
        
        # Create reminders
        r1 = Reminder(booking_id=booking.id, reminder_type="24H", scheduled_time=datetime.now(), status="PENDING")
        r2 = Reminder(booking_id=booking.id, reminder_type="2H", scheduled_time=datetime.now(), status="PENDING")
        session.add_all([r1, r2])
        
        # Set user state to IDLE
        user_state = await get_or_create_user_state(session, "916666666666")
        user_state.state = STATE_IDLE
        await session.commit()

    # Trigger cancel message
    payload_cancel = {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "12345",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "contacts": [{"profile": {"name": "Aman"}, "wa_id": "916666666666"}],
                    "messages": [{
                        "from": "916666666666",
                        "id": "msg_cancel_booking",
                        "text": {"body": "cancel booking"},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

    res = await handle_whatsapp_webhook(payload_cancel)
    assert res["status"] == "success"

    # Assert booking and reminders are marked CANCELLED
    async with TestSessionLocal() as session:
        b_res = await session.execute(select(Booking).where(Booking.phone_number == "916666666666"))
        bookings = b_res.scalars().all()
        assert len(bookings) == 1
        assert bookings[0].status == "CANCELLED"

        r_res = await session.execute(select(Reminder).where(Reminder.booking_id == bookings[0].id))
        reminders = r_res.scalars().all()
        assert len(reminders) == 2
        assert all(r.status == "CANCELLED" for r in reminders)

@pytest.mark.asyncio
async def test_scheduler_restart_recovery():
    """Tests reloading pending reminders from SQLite database into APScheduler."""
    # Ensure scheduler starts
    from app.scheduler.reminders import ReminderScheduler, load_and_schedule_pending_reminders
    ReminderScheduler.start()
    
    async with TestSessionLocal() as session:
        # Create a booking
        tomorrow = datetime.now(timezone.utc).astimezone(IST) + timedelta(days=1)
        booking = Booking(
            phone_number="915555555555",
            customer_name="Rohan",
            car_model="Maruti Ertiga",
            car_variant="VXi",
            slot_start=tomorrow.replace(tzinfo=None),
            slot_end=(tomorrow + timedelta(minutes=30)).replace(tzinfo=None),
            status="COMPLETED"
        )
        session.add(booking)
        await session.flush()
        
        # Create pending reminders scheduled for future run times
        r_time_24h = tomorrow - timedelta(hours=24)
        r_time_2h = tomorrow - timedelta(hours=2)
        
        r1 = Reminder(booking_id=booking.id, reminder_type="24H", scheduled_time=r_time_24h.replace(tzinfo=None), status="PENDING")
        r2 = Reminder(booking_id=booking.id, reminder_type="2H", scheduled_time=r_time_2h.replace(tzinfo=None), status="PENDING")
        session.add_all([r1, r2])
        await session.commit()
        
        r1_id = r1.id
        r2_id = r2.id

    # Call load_and_schedule_pending_reminders
    await load_and_schedule_pending_reminders()

    # Assert that jobs are present in APScheduler
    job_1 = ReminderScheduler._scheduler.get_job(f"reminder_{r1_id}")
    job_2 = ReminderScheduler._scheduler.get_job(f"reminder_{r2_id}")
    assert job_1 is not None
    assert job_2 is not None

    # Clean up jobs and stop scheduler
    ReminderScheduler._scheduler.remove_job(f"reminder_{r1_id}")
    ReminderScheduler._scheduler.remove_job(f"reminder_{r2_id}")
    ReminderScheduler.stop()

@pytest.mark.asyncio
async def test_lead_qualification_flow():
    """Tests progression through lead qualification steps (budget -> timeline -> fuel) and final booking."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000001")
        assert user_state.state == STATE_IDLE
        assert not user_state.lead_completed

        # Step 1: Initiate booking for Ertiga VXi
        entities = {
            "car_model": "Maruti Ertiga",
            "car_variant": "VXi",
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_BOOK_REQUEST", entities, "book ertiga vxi", "Raj")
        assert user_state.state == STATE_QUALIFYING_BUDGET
        assert "budget" in reply.lower()
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000001")
        # Step 2: Answer budget
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": "10 lakhs",
            "timeline": None,
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_QA", entities, "10 lakhs", "Raj")
        assert user_state.state == STATE_QUALIFYING_TIMELINE
        assert "when" in reply.lower()
        assert user_state.lead_budget == "10 lakhs"
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000001")
        # Step 3: Answer timeline
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": "immediate",
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_QA", entities, "immediate", "Raj")
        assert user_state.state == STATE_QUALIFYING_FUEL
        assert "fuel" in reply.lower()
        assert user_state.lead_timeline == "immediate"
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000001")
        # Step 4: Answer fuel
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": "petrol"
        }
        reply = await transition_state(session, user_state, "INTENT_QA", entities, "petrol", "Raj")
        assert user_state.state == STATE_AWAITING_SLOT
        assert user_state.lead_fuel == "petrol"
        assert user_state.lead_completed
        await session.commit()

@pytest.mark.asyncio
async def test_qa_interception_during_qualification():
    """Tests that a Q&A question mid-qualification is answered and qualification is reprompted."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000002")
        user_state.state = STATE_QUALIFYING_BUDGET
        user_state.selected_car_model = "Maruti Brezza"
        user_state.selected_car_variant = "VXi"
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000002")
        # Ask about sunroof (unsupported for VXi)
        entities = {
            "car_model": "Maruti Brezza",
            "car_variant": "VXi",
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_QA", entities, "Brezza VXi me sunroof hai?", "Aman")
        assert "sunroof" in reply or "information" in reply
        assert "budget" in reply.lower()
        # Verify state is still in budget qualification
        assert user_state.state == STATE_QUALIFYING_BUDGET

@pytest.mark.asyncio
async def test_qualification_attempts_anti_loop():
    """Tests that qualification aborts to STATE_IDLE if attempts exceed the threshold."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000003")
        user_state.state = STATE_QUALIFYING_BUDGET
        user_state.selected_car_model = "Maruti Brezza"
        user_state.selected_car_variant = "VXi"
        user_state.qualification_attempts = 3 # Next one will be 4th
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000003")
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_QA", entities, "gibberish budget", "Aman")
        assert "representative" in reply.lower() or "call you back" in reply.lower()
        assert user_state.state == STATE_IDLE

@pytest.mark.asyncio
async def test_qualification_skipping():
    """Tests that known parameters are skipped in the qualification flow."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000004")
        
        # User requests booking and mentions petrol (fuel preference) and 6 lakhs (budget) in one message
        entities = {
            "car_model": "Maruti Swift",
            "car_variant": "LXi",
            "slot_index": None,
            "date_preference": None,
            "budget": "6 lakhs",
            "timeline": None,
            "fuel_preference": "petrol"
        }
        reply = await transition_state(session, user_state, "INTENT_BOOK_REQUEST", entities, "book swift lxi petrol for 6 lakhs", "Rohan")
        
        # Budget and fuel are skipped, timeline is asked
        assert user_state.state == STATE_QUALIFYING_TIMELINE
        assert "when" in reply.lower() or "timeline" in reply.lower()
        assert user_state.lead_fuel == "petrol"
        assert user_state.lead_budget == "6 lakhs"

@pytest.mark.asyncio
async def test_returning_user_greet():
    """Tests that a returning user with booking history is welcomed back by name."""
    async with TestSessionLocal() as session:
        # Create user state and booking history
        booking = Booking(
            phone_number="919000000005",
            customer_name="Vikram",
            car_model="Maruti Ertiga",
            car_variant="VXi",
            slot_start=datetime.now(),
            slot_end=datetime.now(),
            status="COMPLETED"
        )
        session.add(booking)
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000005")
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_GREETING", entities, "hi", "Vikram")
        assert "welcome back" in reply.lower()
        assert "vikram" in reply.lower()

@pytest.mark.asyncio
async def test_reschedule_flow():
    """Tests that rescheduling cancels the old booking/reminders/calendar event and triggers new slot selection."""
    from app.scheduler.reminders import ReminderScheduler
    ReminderScheduler.start()

    async with TestSessionLocal() as session:
        # 1. Create a booking with reminders
        booking = Booking(
            phone_number="919000000006",
            customer_name="Dev",
            car_model="Maruti Swift",
            car_variant="ZXi+",
            slot_start=datetime.now() + timedelta(days=1),
            slot_end=datetime.now() + timedelta(days=1, minutes=30),
            calendar_event_id="mock-reschedule-event-id",
            status="COMPLETED"
        )
        session.add(booking)
        await session.flush()

        r1 = Reminder(booking_id=booking.id, reminder_type="24H", scheduled_time=datetime.now() + timedelta(days=1), status="PENDING")
        session.add(r1)
        await session.commit()
        r1_id = r1.id

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000006")
        
        # 2. Trigger Reschedule
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        reply = await transition_state(session, user_state, "INTENT_RESCHEDULE", entities, "reschedule booking", "Dev")
        
        # 3. Assert old booking marked RESCHEDULED, reminders CANCELLED, state to AWAITING_SLOT
        b_res = await session.execute(select(Booking).where(Booking.phone_number == "919000000006"))
        old_booking = b_res.scalars().first()
        assert old_booking.status == "RESCHEDULED"

        r_res = await session.execute(select(Reminder).where(Reminder.id == r1_id))
        r1_refreshed = r_res.scalar_one()
        assert r1_refreshed.status == "CANCELLED"

        assert user_state.state == STATE_AWAITING_SLOT
        assert user_state.selected_car_model == "Maruti Swift"
        assert user_state.selected_car_variant == "ZXi+"

    ReminderScheduler.stop()

@pytest.mark.asyncio
async def test_variant_normalization():
    """Tests that variants and models are extracted robustly across cases, spacing, and aliases."""
    from app.utils.normalization import normalize_variant
    from app.rag.kb_loader import KnowledgeBase
    from app.agent.intent import extract_car_from_text

    # Test normalization helper
    assert normalize_variant("ZXi+") == "zxiplus"
    assert normalize_variant("zxi plus") == "zxiplus"
    assert normalize_variant("v-xi") == "vxi"
    assert normalize_variant("V Xi") == "vxi"
    assert normalize_variant("Z&Xi") == "zandxi"

    # Test KnowledgeBase matching
    v1 = KnowledgeBase.get_variant_details("Maruti Brezza", "vxi")
    assert v1 is not None
    assert v1["name"] == "VXi"

    v2 = KnowledgeBase.get_variant_details("brezza", "zxiplus")
    assert v2 is not None
    assert v2["name"] == "ZXi+"

    v3 = KnowledgeBase.get_variant_details("swift", "zxi plus")
    assert v3 is not None
    assert v3["name"] == "ZXi+"

    # Test extract_car_from_text
    m, v = extract_car_from_text("kal brezza vxi ka drive book karna hai")
    assert m == "Maruti Brezza"
    assert v == "VXi"

    m, v = extract_car_from_text("swift zxi plus chahiye")
    assert m == "Maruti Swift"
    assert v == "ZXi+"

    m, v = extract_car_from_text("booking request for ertiga zxi+")
    assert m == "Maruti Ertiga"
    assert v == "ZXi+"

@pytest.mark.asyncio
async def test_fsm_routing_and_entity_merging():
    """Verifies that sending 'VXi' during variant selection transitions to budget flow directly without RAG interception."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000009")
        user_state.state = "STATE_CAR_SELECTED"
        user_state.selected_car_model = "Maruti Brezza"
        user_state.selected_car_variant = None
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000009")
        
        # Simulating user typing "VXi". Heuristics/LLM will extract entity "VXi".
        entities = {
            "car_model": None,
            "car_variant": "VXi",
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        
        reply = await transition_state(
            session=session,
            user_state=user_state,
            intent="INTENT_QA", # stands for QA or generic inputs
            entities=entities,
            user_message="VXi",
            customer_name="Rajveer"
        )
        
        # Verify that it bypassed RAG Q&A (no fallback message prefix)
        assert "I don't have that information" not in reply
        # Verify that the variant was saved and budget flow was initiated
        assert user_state.selected_car_variant == "VXi"
        assert user_state.state == "STATE_QUALIFYING_BUDGET"
        assert "budget" in reply.lower()
        await session.commit()

@pytest.mark.asyncio
async def test_inline_slots_and_text_parsing():
    """Verifies inline slots formatting and text-based slot selection parsing (e.g. 'sat 4')."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000010")
        user_state.state = STATE_AWAITING_SLOT
        user_state.selected_car_model = "Maruti Brezza"
        user_state.selected_car_variant = "VXi"
        
        # Setup mock slots JSON (1: Sat 11:00 AM, 2: Sat 4:00 PM, 3: Sun 12:00 PM)
        # Saturday Jun 6, 2026 is weekend
        slots_data = [
            {"start": "2026-06-06T11:00:00", "end": "2026-06-06T11:30:00"},
            {"start": "2026-06-06T16:00:00", "end": "2026-06-06T16:30:00"},
            {"start": "2026-06-07T12:00:00", "end": "2026-06-07T12:30:00"}
        ]
        user_state.slots_json = json.dumps(slots_data)
        user_state.slot_generated_at = datetime.now()
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000010")
        
        # 1. Test inline formatting of slots message
        from app.agent.state import format_slots_message
        slots_msg = format_slots_message(slots_data)
        assert slots_msg == "1) Sat 11:00 AM  2) Sat 4:00 PM  3) Sun 12:00 PM"
        
        # 2. Test text-based parsing of "sat 4"
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        
        reply = await transition_state(
            session=session,
            user_state=user_state,
            intent="INTENT_QA", # User sent plain text
            entities=entities,
            user_message="sat 4",
            customer_name="Rajveer"
        )
        
        # Assert state transitioned to confirmation and selected slot 2
        assert user_state.state == STATE_AWAITING_CONFIRMATION
        assert user_state.selected_slot_start.hour == 16 # 4:00 PM
        assert reply == ""
        await session.commit()

@pytest.mark.asyncio
async def test_fsm_contextual_variant_extraction():
    """Verifies that sending 'VXi' (with no model name) in STATE_CAR_SELECTED resolves variant via FSM model context."""
    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000011")
        user_state.state = "STATE_CAR_SELECTED"
        user_state.selected_car_model = "Maruti Brezza"
        user_state.selected_car_variant = None
        await session.commit()

    async with TestSessionLocal() as session:
        user_state = await get_or_create_user_state(session, "919000000011")
        
        # Simulating user typing "VXi". Heuristics will extract entity "VXi" as None due to no model name.
        entities = {
            "car_model": None,
            "car_variant": None,
            "slot_index": None,
            "date_preference": None,
            "budget": None,
            "timeline": None,
            "fuel_preference": None
        }
        
        reply = await transition_state(
            session=session,
            user_state=user_state,
            intent="INTENT_QA",
            entities=entities,
            user_message="VXi",
            customer_name="Rajveer"
        )
        
        # Verify that FSM correctly extracted 'VXi' using the Brezza context
        assert user_state.selected_car_variant == "VXi"
        assert user_state.state == "STATE_QUALIFYING_BUDGET"
        assert "budget" in reply.lower()
        await session.commit()

@pytest.mark.asyncio
async def test_features_mock_grounded_response():
    """Verifies that asking for features/specs yields key spec lines instead of fallback."""
    context = (
        "Model: Maruti Brezza\n"
        "  Variant: VXi\n"
        "    Engine: 1.5L petrol, 103 bhp\n"
        "    Mileage: 19.8 kmpl (claimed)\n"
        "    Transmission: 5-speed manual\n"
        "    Features: 6 airbags, rear AC vents, touchscreen infotainment\n"
        "    Sunroof: NO\n"
        "    Price (ex-showroom): ₹9.7L\n"
        "    Colors: White, Grey, Blue"
    )
    
    # Ask for features of Brezza VXi
    reply = await generate_grounded_response("features of Brezza VXi", context)
    assert "Here are the key specs:" in reply
    assert "Engine: 1.5L petrol" in reply
    assert "Features: 6 airbags" in reply
    assert "Price (ex-showroom): ₹9.7L" in reply


