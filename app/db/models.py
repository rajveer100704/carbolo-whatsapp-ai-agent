from datetime import datetime, timezone, timedelta
from sqlalchemy import Column, String, Integer, DateTime, ForeignKey, Boolean
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def get_naive_ist():
    """Generates localized naive datetime in Indian Standard Time (UTC+5:30) for SQLite storage."""
    IST = timezone(timedelta(hours=5, minutes=30))
    return datetime.now(timezone.utc).astimezone(IST).replace(tzinfo=None)

class UserConversationState(Base):
    __tablename__ = "user_conversation_states"

    phone_number = Column(String, primary_key=True, index=True)
    state = Column(String, default="STATE_IDLE", nullable=False)
    selected_car_model = Column(String, nullable=True)
    selected_car_variant = Column(String, nullable=True)
    slots_json = Column(String, nullable=True)  # JSON serialized list of available slots
    slot_generated_at = Column(DateTime, nullable=True)
    selected_slot_start = Column(DateTime, nullable=True)
    selected_slot_end = Column(DateTime, nullable=True)
    lead_budget = Column(String, nullable=True)
    lead_timeline = Column(String, nullable=True)
    lead_fuel = Column(String, nullable=True)
    lead_completed = Column(Boolean, default=False, nullable=False)
    qualification_attempts = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=get_naive_ist, onupdate=get_naive_ist)

class Booking(Base):
    __tablename__ = "bookings"

    id = Column(Integer, primary_key=True, autoincrement=True)
    phone_number = Column(String, index=True, nullable=False)
    customer_name = Column(String, nullable=False)
    car_model = Column(String, nullable=False)
    car_variant = Column(String, nullable=False)
    slot_start = Column(DateTime, nullable=False)
    slot_end = Column(DateTime, nullable=False)
    calendar_event_id = Column(String, nullable=True)
    channel = Column(String, default="whatsapp", nullable=False)
    status = Column(String, default="PENDING", nullable=False)  # PENDING, CONFIRMED, COMPLETED
    created_at = Column(DateTime, default=get_naive_ist)
    
    reminders = relationship("Reminder", back_populates="booking", cascade="all, delete-orphan")

class Reminder(Base):
    __tablename__ = "reminders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    booking_id = Column(Integer, ForeignKey("bookings.id"), nullable=False)
    reminder_type = Column(String, nullable=False)  # "24H" or "2H"
    scheduled_time = Column(DateTime, nullable=False)
    status = Column(String, default="PENDING", nullable=False)  # PENDING, SENT, FAILED
    retry_count = Column(Integer, default=0, nullable=False)
    job_id = Column(String, nullable=True)  # Associated APScheduler job ID
    
    booking = relationship("Booking", back_populates="reminders")

class ProcessedWebhookMessage(Base):
    __tablename__ = "processed_webhook_messages"

    message_id = Column(String, primary_key=True, index=True)
    processed_at = Column(DateTime, default=get_naive_ist)
