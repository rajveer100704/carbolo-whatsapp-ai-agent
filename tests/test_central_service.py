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

import app.db.session
from app.db.models import Base, Booking, UserConversationState
from app.agent.central_service import CentralAgentService
from app.agent.state import STATE_QUALIFYING_BUDGET, STATE_AWAITING_SLOT, STATE_AWAITING_CONFIRMATION, STATE_IDLE
from app.calendar.availability import IST

@pytest_asyncio.fixture(autouse=True)
async def setup_db():
    """Initializes in-memory test database tables and overrides global session factory dynamically."""
    # Save original session and engine
    old_factory = app.db.session.async_session_factory
    old_engine = app.db.session.engine
    
    # Override with this file's test settings
    app.db.session.async_session_factory = TestSessionLocal
    app.db.session.engine = test_engine

    # Initialize tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    yield
    
    # Drop tables
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        
    # Restore original session and engine
    app.db.session.async_session_factory = old_factory
    app.db.session.engine = old_engine

@pytest.mark.asyncio
async def test_central_service_whatsapp_vs_gmail_routing():
    """
    Verifies that the same message sent via whatsapp vs gmail channel uses different database identity keys
    and that Gmail returns plain text instead of trying to send WhatsApp Cloud API interactive buttons.
    """
    central_service = CentralAgentService()
    
    # 1. Test Gmail Booking Init
    # For Gmail, the booking init should return budget qualification question text directly.
    gmail_reply = await central_service.process_message(
        channel="gmail",
        user_id="sanskar@example.com",
        message="I want to book a Brezza VXi test drive",
        customer_name="Sanskar Soni"
    )
    
    assert "budget" in gmail_reply.lower()
    
    # Check that Gmail user state was created with the prefixed key
    async with TestSessionLocal() as session:
        states = (await session.execute(select(UserConversationState))).scalars().all()
        assert len(states) == 1
        assert states[0].phone_number == "gmail:sanskar@example.com"
        assert states[0].state == STATE_QUALIFYING_BUDGET
        assert states[0].selected_car_model == "Maruti Brezza"
        assert states[0].selected_car_variant == "VXi"

    # 2. Test WhatsApp Booking Init (separate state)
    # For WhatsApp, the booking init should also ask for budget, but use the raw phone number.
    whatsapp_reply = await central_service.process_message(
        channel="whatsapp",
        user_id="919999999999",
        message="I want to book a Brezza VXi test drive",
        customer_name="Rajveer"
    )
    
    assert "budget" in whatsapp_reply.lower()

    # Check both states exist in DB
    async with TestSessionLocal() as session:
        states = (await session.execute(select(UserConversationState))).scalars().all()
        assert len(states) == 2
        keys = [s.phone_number for s in states]
        assert "gmail:sanskar@example.com" in keys
        assert "919999999999" in keys

@pytest.mark.asyncio
async def test_gmail_plain_text_slot_and_confirm():
    """
    Verifies that Gmail slot suggestions and confirmation requests are returned as plain text response bodies
    instead of sending WhatsApp buttons.
    """
    central_service = CentralAgentService()
    
    # Pre-qualify user directly in DB to bypass qualification
    async with TestSessionLocal() as session:
        user_state = UserConversationState(
            phone_number="gmail:test@example.com",
            state=STATE_QUALIFYING_BUDGET,
            selected_car_model="Maruti Swift",
            selected_car_variant="ZXi+",
            lead_budget="7 lakhs",
            lead_timeline="immediate",
            lead_fuel="petrol",
            lead_completed=True
        )
        session.add(user_state)
        await session.commit()

    # Trigger next transition -> suggest slots.
    # The return string should be the formatted slots text, since WhatsApp buttons are bypassed.
    reply_slots = await central_service.process_message(
        channel="gmail",
        user_id="test@example.com",
        message="yes my budget is 7 lakhs, immediate petrol",
        customer_name="Test User"
    )
    
    # Assert FSM suggests slots inline
    assert "open slots" in reply_slots.lower()
    assert "1." in reply_slots or "1)" in reply_slots
    assert "2." in reply_slots or "2)" in reply_slots
    assert "3." in reply_slots or "3)" in reply_slots
    
    # Confirm the state is now awaiting slot selection
    async with TestSessionLocal() as session:
        state = (await session.execute(select(UserConversationState).where(UserConversationState.phone_number == "gmail:test@example.com"))).scalar_one()
        assert state.state == STATE_AWAITING_SLOT
        
        # Manually verify slot_generated_at and slots_json are populated
        assert state.slots_json is not None

    # Simulate choosing slot 1
    reply_confirm = await central_service.process_message(
        channel="gmail",
        user_id="test@example.com",
        message="1",
        customer_name="Test User"
    )
    
    # For Gmail, this should return the confirmation text ("You selected ... Reply with 'Confirm'")
    assert "you selected" in reply_confirm.lower()
    assert "confirm" in reply_confirm.lower()
    
    # Simulate sending "Confirm" to complete booking
    reply_final = await central_service.process_message(
        channel="gmail",
        user_id="test@example.com",
        message="confirm",
        customer_name="Test User"
    )
    
    assert "done" in reply_final.lower()
    assert "booked" in reply_final.lower()
    
    # Check DB booking and channel type
    async with TestSessionLocal() as session:
        booking = (await session.execute(select(Booking).where(Booking.phone_number == "gmail:test@example.com"))).scalar_one()
        assert booking.status == "COMPLETED"
        assert booking.channel == "gmail"
        assert booking.customer_name == "Test User"
        assert booking.car_model == "Maruti Swift"
        assert booking.car_variant == "ZXi+"
