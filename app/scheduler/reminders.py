import os
from datetime import datetime, timedelta, timezone
import logging
from sqlalchemy import select
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from app.db.models import Reminder, Booking
from app.db.session import async_session_factory
from app.whatsapp.sender import send_whatsapp_message

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

class ReminderScheduler:
    _scheduler = None

    @classmethod
    def start(cls):
        """Initializes and starts the BackgroundScheduler."""
        if cls._scheduler is None:
            cls._scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
            cls._scheduler.start()
            logger.info("Background reminder scheduler started.")

    @classmethod
    def stop(cls):
        if cls._scheduler is not None:
            cls._scheduler.shutdown()
            cls._scheduler = None
            logger.info("Background reminder scheduler stopped.")

    @classmethod
    def schedule_reminder_job(cls, reminder_id: int, run_time: datetime):
        """Schedules a one-off reminder execution job."""
        if cls._scheduler is None:
            cls.start()
            
        job_id = f"reminder_{reminder_id}"
        
        # If job already exists, remove it first
        if cls._scheduler.get_job(job_id):
            cls._scheduler.remove_job(job_id)
            
        # Convert run_time to localized IST
        run_time_ist = run_time.astimezone(IST) if run_time.tzinfo else run_time.replace(tzinfo=IST)
        
        # If run_time is in the past, run it in 5 seconds
        now_ist = datetime.now(timezone.utc).astimezone(IST)
        if run_time_ist <= now_ist:
            logger.info(f"Scheduled time {run_time_ist} is in the past. Scheduling job {job_id} to run immediately.")
            run_time_ist = now_ist + timedelta(seconds=5)

        cls._scheduler.add_job(
            func=execute_reminder_job,
            trigger=DateTrigger(run_date=run_time_ist),
            args=[reminder_id],
            id=job_id,
            replace_existing=True
        )
        logger.info(f"Scheduled job {job_id} to run at {run_time_ist}")

    @classmethod
    def cancel_reminder_job(cls, reminder_id: int):
        """Cancels a scheduled reminder job."""
        if cls._scheduler is not None:
            job_id = f"reminder_{reminder_id}"
            if cls._scheduler.get_job(job_id):
                cls._scheduler.remove_job(job_id)
                logger.info(f"Successfully cancelled job {job_id} in APScheduler.")

async def load_and_schedule_pending_reminders():
    """Reads all PENDING reminders from SQLite and schedules them in APScheduler."""
    logger.info("Reloading pending reminders from SQLite...")
    async with async_session_factory() as session:
        query = select(Reminder).where(Reminder.status == "PENDING")
        result = await session.execute(query)
        reminders = result.scalars().all()
        
        now = datetime.now(timezone.utc).astimezone(IST)
        count = 0
        for r in reminders:
            # Only schedule if slot hasn't already passed completely
            # Fetch associated booking
            booking_query = select(Booking).where(Booking.id == r.booking_id)
            b_res = await session.execute(booking_query)
            booking = b_res.scalar_one_or_none()
            
            if booking:
                slot_end = booking.slot_end.replace(tzinfo=IST) if not booking.slot_end.tzinfo else booking.slot_end
                if slot_end > now:
                    ReminderScheduler.schedule_reminder_job(r.id, r.scheduled_time)
                    count += 1
                else:
                    r.status = "EXPIRED"
                    
        await session.commit()
    logger.info(f"Rescheduled {count} pending reminders.")

def execute_reminder_job(reminder_id: int):
    """
    Called by APScheduler date trigger. Runs inside a background thread.
    Uses an event loop to execute the async database updates and send messages.
    """
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(_run_reminder_logic(reminder_id))
    finally:
        loop.close()

async def _run_reminder_logic(reminder_id: int):
    """Core reminder sending logic with retry handlers."""
    async with async_session_factory() as session:
        # Fetch reminder
        query = select(Reminder).where(Reminder.id == reminder_id)
        result = await session.execute(query)
        reminder = result.scalar_one_or_none()
        
        if not reminder or reminder.status != "PENDING":
            return
            
        # Fetch associated booking
        booking_query = select(Booking).where(Booking.id == reminder.booking_id)
        b_res = await session.execute(booking_query)
        booking = b_res.scalar_one_or_none()
        
        if not booking:
            reminder.status = "FAILED"
            await session.commit()
            return

        # Prepare message
        # Format time beautifully
        slot_start_ist = booking.slot_start.astimezone(IST) if booking.slot_start.tzinfo else booking.slot_start.replace(tzinfo=IST)
        time_str = slot_start_ist.strftime("%I:%M %p").lstrip("0")
        day_str = slot_start_ist.strftime("%A")
        
        if reminder.reminder_type == "24H":
            msg = (
                f"Reminder: Your Maruti Suzuki {booking.car_model} ({booking.car_variant}) test drive is scheduled "
                f"for tomorrow, {day_str} at {time_str}. See you then!"
            )
        else: # 2H
            msg = (
                f"Reminder: Your test drive for Maruti Suzuki {booking.car_model} ({booking.car_variant}) starts in 2 hours "
                f"({time_str}). Get ready!"
            )

        # Send WhatsApp message
        success = send_whatsapp_message(booking.phone_number, msg)
        
        if success:
            reminder.status = "SENT"
            logger.info(f"Successfully sent {reminder.reminder_type} reminder for Booking {booking.id}")
        else:
            reminder.retry_count += 1
            if reminder.retry_count >= 3:
                reminder.status = "FAILED"
                logger.error(f"Failed to send {reminder.reminder_type} reminder after 3 attempts.")
            else:
                # Retry in 5 minutes
                retry_time = datetime.now(timezone.utc).astimezone(IST) + timedelta(minutes=5)
                reminder.scheduled_time = retry_time
                ReminderScheduler.schedule_reminder_job(reminder.id, retry_time)
                logger.warning(
                    f"WhatsApp send failed. Retrying job {reminder.id} in 5 minutes "
                    f"(Attempt {reminder.retry_count}/3)"
                )
                
        await session.commit()
