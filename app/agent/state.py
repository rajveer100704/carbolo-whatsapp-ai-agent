import json
import re
import logging
from datetime import datetime, timezone, timedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import UserConversationState, Booking, Reminder
from app.calendar.availability import get_available_slots, get_ist_now, IST
from app.calendar.booking import create_test_drive_event, delete_test_drive_event
from app.scheduler.reminders import ReminderScheduler
from app.whatsapp.sender import send_whatsapp_message, send_whatsapp_buttons
from app.rag.retriever import retrieve_context
from app.agent.intent import generate_grounded_response, extract_car_from_text, INTENT_RESCHEDULE, is_spec_query

logger = logging.getLogger(__name__)

# State Constants
STATE_IDLE = "STATE_IDLE"
STATE_CAR_SELECTED = "STATE_CAR_SELECTED"
STATE_AWAITING_SLOT = "STATE_AWAITING_SLOT"
STATE_AWAITING_CONFIRMATION = "STATE_AWAITING_CONFIRMATION"
STATE_BOOKED = "STATE_BOOKED"
STATE_QUALIFYING_BUDGET = "STATE_QUALIFYING_BUDGET"
STATE_QUALIFYING_TIMELINE = "STATE_QUALIFYING_TIMELINE"
STATE_QUALIFYING_FUEL = "STATE_QUALIFYING_FUEL"

# Session Settings
SESSION_TIMEOUT_MINUTES = 30
SLOT_EXPIRATION_MINUTES = 10

async def get_or_create_user_state(session: AsyncSession, phone_number: str) -> UserConversationState:
    """
    Retrieves the UserConversationState row for the phone number.
    Acquires a database row lock (with_for_update) to block concurrent updates.
    Creates a default state if not present.
    """
    query = select(UserConversationState).where(
        UserConversationState.phone_number == phone_number
    ).with_for_update()
    
    result = await session.execute(query)
    user_state = result.scalar_one_or_none()
    
    now = get_ist_now().replace(tzinfo=None)  # SQLite stores datetime naive
    
    if not user_state:
        user_state = UserConversationState(
            phone_number=phone_number,
            state=STATE_IDLE,
            updated_at=now
        )
        session.add(user_state)
        await session.flush()
        logger.info(f"Created new conversation state for user: {phone_number}")
    else:
        # Check Session Timeout
        last_updated = user_state.updated_at
        delta = now - last_updated
        if delta > timedelta(minutes=SESSION_TIMEOUT_MINUTES) and user_state.state != STATE_IDLE:
            logger.info(f"Session timeout for {phone_number} (last updated {delta.total_seconds() / 60:.1f} mins ago). Resetting to STATE_IDLE.")
            user_state.state = STATE_IDLE
            user_state.selected_car_model = None
            user_state.selected_car_variant = None
            user_state.slots_json = None
            user_state.slot_generated_at = None
            user_state.selected_slot_start = None
            user_state.selected_slot_end = None
            user_state.updated_at = now
            await session.flush()
            
    return user_state

def update_qualification_entities(user_state, entities: dict):
    if entities.get("budget"):
        user_state.lead_budget = entities["budget"]
    if entities.get("timeline"):
        user_state.lead_timeline = entities["timeline"]
    if entities.get("fuel_preference"):
        user_state.lead_fuel = entities["fuel_preference"]



def check_qualification_loop(user_state: UserConversationState) -> str | None:
    user_state.qualification_attempts += 1
    if user_state.qualification_attempts > 3:
        user_state.state = STATE_IDLE
        return (
            "I'm having trouble capturing that information. No worries, I will ask a "
            "dealership representative to call you back to help finish scheduling your test drive. "
            "In the meantime, what else can I tell you about our cars?"
        )
    return None

async def transition_state(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str
) -> str:
    """
    The state transition engine. Updates user_state in the database
    and returns the response message string to send back to the user.
    """
    state = user_state.state
    now = get_ist_now()
    now_naive = now.replace(tzinfo=None)
    
    logger.info(f"[{user_state.phone_number}] Current State: {state} | Intent: {intent} | Entities: {entities}")

    # Capture early qualification parameters if present
    update_qualification_entities(user_state, entities)

    # Global Cancellation Command
    if intent == "INTENT_CANCEL":
        # Check if user has a completed booking to cancel
        booking_query = select(Booking).where(
            Booking.phone_number == user_state.phone_number,
            Booking.status == "COMPLETED"
        ).order_by(Booking.created_at.desc())
        b_res = await session.execute(booking_query)
        active_booking = b_res.scalars().first()
        
        user_state.state = STATE_IDLE
        user_state.selected_car_model = None
        user_state.selected_car_variant = None
        user_state.slots_json = None
        user_state.slot_generated_at = None
        user_state.selected_slot_start = None
        user_state.selected_slot_end = None
        user_state.updated_at = now_naive
        
        if active_booking:
            active_booking.status = "CANCELLED"
            # Cancel Google Calendar event
            delete_test_drive_event(active_booking.calendar_event_id)
            
            # Cancel reminders
            rem_query = select(Reminder).where(Reminder.booking_id == active_booking.id)
            r_res = await session.execute(rem_query)
            reminders = r_res.scalars().all()
            for r in reminders:
                r.status = "CANCELLED"
                ReminderScheduler.cancel_reminder_job(r.id)
            return f"Your test drive for Maruti Suzuki {active_booking.car_model} has been successfully cancelled and reminders have been turned off."
            
        return "Ok, test drive booking process has been canceled. Feel free to ask any other questions about our cars!"

    # Global Reschedule Command
    if intent == "INTENT_RESCHEDULE":
        booking_query = select(Booking).where(
            Booking.phone_number == user_state.phone_number,
            Booking.status == "COMPLETED"
        ).order_by(Booking.created_at.desc())
        b_res = await session.execute(booking_query)
        active_booking = b_res.scalars().first()
        
        if active_booking:
            # Restore model and variant from old booking
            user_state.selected_car_model = active_booking.car_model
            user_state.selected_car_variant = active_booking.car_variant
            
            # Cancel old booking (booking versioning)
            active_booking.status = "RESCHEDULED"
            # Cancel Google Calendar event
            delete_test_drive_event(active_booking.calendar_event_id)
            
            # Cancel reminders
            rem_query = select(Reminder).where(Reminder.booking_id == active_booking.id)
            r_res = await session.execute(rem_query)
            reminders = r_res.scalars().all()
            for r in reminders:
                r.status = "CANCELLED"
                ReminderScheduler.cancel_reminder_job(r.id)
                
            # Transition state to proceed to slot suggestion
            user_state.state = STATE_CAR_SELECTED
            user_state.updated_at = now_naive
            
            # Suggest slots for the same vehicle (lead is already qualified)
            return await suggest_and_transition_slots(user_state, now, now_naive, entities.get("date_preference"))
        else:
            return "I couldn't find an active booking to reschedule. Would you like to book a new test drive?"

    # State Dispatcher
    handlers = {
        STATE_IDLE: handle_idle,
        STATE_CAR_SELECTED: handle_car_selected,
        STATE_QUALIFYING_BUDGET: handle_qualifying_budget,
        STATE_QUALIFYING_TIMELINE: handle_qualifying_timeline,
        STATE_QUALIFYING_FUEL: handle_qualifying_fuel,
        STATE_AWAITING_SLOT: handle_awaiting_slot,
        STATE_AWAITING_CONFIRMATION: handle_awaiting_confirmation,
    }

    handler = handlers.get(state)
    if handler:
        response = await handler(session, user_state, intent, entities, user_message, customer_name, now, now_naive)
        user_state.updated_at = now_naive
        return response

    return "Hi, how can I assist you with Maruti Suzuki today?"

async def handle_idle(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    if intent == "INTENT_GREETING":
        # Greet returning user by name from previous booking history
        booking_query = select(Booking).where(
            Booking.phone_number == user_state.phone_number
        ).order_by(Booking.created_at.desc())
        b_res = await session.execute(booking_query)
        last_booking = b_res.scalars().first()
        
        if last_booking and last_booking.customer_name:
            return (
                f"Welcome back, {last_booking.customer_name}! Great to chat with you again at Maruti Suzuki Dealership. "
                "How can I assist you today? Would you like to schedule another test drive or ask about our cars?"
            )
        return (
            "Hi! Welcome to Maruti Suzuki Dealership. I can answer questions about Brezza, Swift, or Ertiga, "
            "or schedule a test drive for you. What would you like to do today?"
        )
        
    elif intent == "INTENT_BOOK_REQUEST":
        return await handle_booking_init(session, user_state, entities, now_naive)
        
    else:
        # Q&A intent or anything else
        context = retrieve_context(user_message)
        return await generate_grounded_response(user_message, context)

async def handle_car_selected(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    if not user_state.selected_car_variant:
        variant = entities.get("car_variant")
        if not variant:
            from app.agent.intent import extract_variant_for_model
            extracted_variant = extract_variant_for_model(
                user_state.selected_car_model,
                user_message
            )
            logger.info(
                f"[FSM] Variant extraction | "
                f"model={user_state.selected_car_model} | "
                f"user_message={user_message} | "
                f"extracted_variant={extracted_variant}"
            )
            variant = extracted_variant
        
        if variant:
            # Validate variant is in knowledgebase for this model
            from app.rag.kb_loader import KnowledgeBase
            kb_variant = KnowledgeBase.get_variant_details(user_state.selected_car_model, variant)
            if kb_variant:
                user_state.selected_car_variant = kb_variant["name"]
                return await start_qualification_or_suggest_slots(user_state, now, now_naive, entities.get("date_preference"))
            else:
                variants_list = "VXi or ZXi+"
                if "swift" in user_state.selected_car_model.lower():
                    variants_list = "LXi or ZXi+"
                return f"Sorry, we do not offer the variant '{variant}' for the {user_state.selected_car_model}. We offer {variants_list}."
        
        if intent == "INTENT_QA" and is_spec_query(user_message):
            context = retrieve_context(user_message)
            answer = await generate_grounded_response(user_message, context)
            variants_list = "VXi or ZXi+"
            if "swift" in user_state.selected_car_model.lower():
                variants_list = "LXi or ZXi+"
            return f"{answer}\n\nTo continue, which variant of the {user_state.selected_car_model} would you like to drive? We offer {variants_list}."
            
        variants_list = "VXi or ZXi+"
        if "swift" in user_state.selected_car_model.lower():
            variants_list = "LXi or ZXi+"
        return f"To continue, which variant of the {user_state.selected_car_model} would you like to drive? We offer {variants_list}."
    else:
        return await start_qualification_or_suggest_slots(user_state, now, now_naive, entities.get("date_preference"))

async def handle_qualifying_budget(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    # Check loop protection
    loop_msg = check_qualification_loop(user_state)
    if loop_msg:
        return loop_msg

    # Q&A Interception
    if intent == "INTENT_QA" and is_spec_query(user_message):
        context = retrieve_context(user_message)
        answer = await generate_grounded_response(user_message, context)
        return f"{answer}\n\nTo continue with scheduling, could you tell us what your approximate budget is?"

    # Accept the answer
    user_state.lead_budget = user_message
    return await proceed_qualification(user_state, now, now_naive, entities.get("date_preference"))

async def handle_qualifying_timeline(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    # Check loop protection
    loop_msg = check_qualification_loop(user_state)
    if loop_msg:
        return loop_msg

    # Q&A Interception
    if intent == "INTENT_QA" and is_spec_query(user_message):
        context = retrieve_context(user_message)
        answer = await generate_grounded_response(user_message, context)
        return f"{answer}\n\nTo continue with scheduling, when are you looking to purchase the vehicle? (e.g., immediate, within a month, or just researching?)"

    # Accept the answer
    user_state.lead_timeline = user_message
    return await proceed_qualification(user_state, now, now_naive, entities.get("date_preference"))

async def handle_qualifying_fuel(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    # Check loop protection
    loop_msg = check_qualification_loop(user_state)
    if loop_msg:
        return loop_msg

    # Q&A Interception
    if intent == "INTENT_QA" and is_spec_query(user_message):
        context = retrieve_context(user_message)
        answer = await generate_grounded_response(user_message, context)
        return f"{answer}\n\nTo continue with scheduling, what is your preferred fuel type? (Petrol, Diesel, CNG, or Hybrid?)"

    # Accept the answer
    user_state.lead_fuel = user_message
    return await proceed_qualification(user_state, now, now_naive, entities.get("date_preference"))

async def handle_awaiting_slot(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    # Try parsing slot_idx from entities or user message
    slot_idx = entities.get("slot_index")
    
    if not slot_idx and user_state.slots_json:
        try:
            slots = json.loads(user_state.slots_json)
            matched_idx = None
            msg_clean = user_message.lower().replace(" ", "").replace(":", "")
            
            # 1. Check if it's a direct digit index
            num_match = re.search(r'\b([1-3])\b', user_message)
            if num_match:
                matched_idx = int(num_match.group(1))
            elif "slot_" in msg_clean:
                s_num_match = re.search(r'slot_(\d+)', msg_clean)
                if s_num_match:
                    matched_idx = int(s_num_match.group(1))
                    
            if not matched_idx:
                # 2. Try exact day/time matching
                for i, slot in enumerate(slots):
                    start_dt = datetime.fromisoformat(slot["start"]).astimezone(IST)
                    day_short = start_dt.strftime("%a").lower()      # e.g. "sat"
                    day_full = start_dt.strftime("%A").lower()       # e.g. "saturday"
                    time_hour = start_dt.strftime("%I").lstrip("0")  # e.g. "4"
                    time_hour_val = str(start_dt.hour)               # e.g. "16"
                    time_min = start_dt.strftime("%M")               # e.g. "00"
                    am_pm = start_dt.strftime("%p").lower()          # e.g. "pm"
                    
                    patterns = [
                        f"{day_short}{time_hour}",
                        f"{day_short}{time_hour}{am_pm}",
                        f"{day_full}{time_hour}",
                        f"{day_full}{time_hour}{am_pm}",
                        f"{day_short}{time_hour_val}",
                        f"{day_full}{time_hour_val}"
                    ]
                    
                    if any(pat in msg_clean for pat in patterns):
                        matched_idx = i + 1
                        break
                        
            if not matched_idx:
                # 3. Check if both the day and hour match as substrings (e.g. "sat 4")
                for i, slot in enumerate(slots):
                    start_dt = datetime.fromisoformat(slot["start"]).astimezone(IST)
                    day_short = start_dt.strftime("%a").lower()
                    day_full = start_dt.strftime("%A").lower()
                    time_hour = start_dt.strftime("%I").lstrip("0")
                    time_hour_val = str(start_dt.hour)
                    
                    has_day = (day_short in msg_clean) or (day_full in msg_clean)
                    has_time = (time_hour in msg_clean) or (time_hour_val in msg_clean)
                    
                    if has_day and has_time:
                        matched_idx = i + 1
                        break
                        
            if matched_idx and 1 <= matched_idx <= len(slots):
                slot_idx = matched_idx
        except Exception as e:
            logger.error(f"Error parsing slot from message: {e}")

    if slot_idx is not None:
        # Check Slot Expiration
        if user_state.slot_generated_at:
            generated_at = user_state.slot_generated_at.replace(tzinfo=IST) if not user_state.slot_generated_at.tzinfo else user_state.slot_generated_at
            if now - generated_at > timedelta(minutes=SLOT_EXPIRATION_MINUTES):
                logger.info(f"Slot hold expired for {user_state.phone_number}. Regenerating...")
                reply_header = "These slots have expired."
                return await regenerate_slots_and_reply(user_state, now, now_naive, reply_header)

        # Retrieve slots list
        if not user_state.slots_json:
            return "Something went wrong. Let's start over. Which car would you like to book a test drive for?"
            
        slots = json.loads(user_state.slots_json)
        if slot_idx < 1 or slot_idx > len(slots):
            return f"Invalid slot choice. Please choose a slot between 1 and {len(slots)}."

        selected = slots[slot_idx - 1]
        user_state.selected_slot_start = datetime.fromisoformat(selected["start"]).replace(tzinfo=None)
        user_state.selected_slot_end = datetime.fromisoformat(selected["end"]).replace(tzinfo=None)
        user_state.state = STATE_AWAITING_CONFIRMATION
        
        start_dt = datetime.fromisoformat(selected["start"]).astimezone(IST)
        day_str = start_dt.strftime("%A, %b %d")
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")
        
        body_text = f"You selected {user_state.selected_car_model} ({user_state.selected_car_variant}) on {day_str} at {time_str}.\n\nReply with 'Confirm' to book your test drive."
        
        sent = send_whatsapp_buttons(
            to=user_state.phone_number,
            header="Confirm Booking",
            body=body_text,
            buttons=[
                ("confirm_booking_yes", "Confirm"),
                ("confirm_booking_no", "Cancel")
            ]
        )
        if sent:
            return ""
        return body_text

    elif intent == "INTENT_SELECT_SLOT":
        return "Please choose a valid option (1, 2, or 3) from the slots list, or click one of the buttons."
        
    elif intent == "INTENT_BOOK_REQUEST":
        return await handle_booking_init(session, user_state, entities, now_naive)
        
    else:
        slots_list_msg = format_slots_message(json.loads(user_state.slots_json))
        if is_spec_query(user_message):
            context = retrieve_context(user_message)
            answer = await generate_grounded_response(user_message, context)
            return f"{answer}\n\nTo continue, please select a slot option:\n{slots_list_msg}"
        return f"To continue, please select a slot option:\n{slots_list_msg}"

async def handle_awaiting_confirmation(
    session: AsyncSession,
    user_state: UserConversationState,
    intent: str,
    entities: dict,
    user_message: str,
    customer_name: str,
    now: datetime,
    now_naive: datetime
) -> str:
    if intent == "INTENT_CONFIRM":
        booking_query = select(Booking).where(
            Booking.phone_number == user_state.phone_number,
            Booking.slot_start == user_state.selected_slot_start,
            Booking.slot_end == user_state.selected_slot_end,
            Booking.status == "COMPLETED"
        )
        b_res = await session.execute(booking_query)
        existing_booking = b_res.scalar_one_or_none()
        
        if existing_booking:
            user_state.state = STATE_IDLE
            return f"Your booking is already confirmed for {existing_booking.car_model} {existing_booking.car_variant}!"
            
        try:
            start_dt = user_state.selected_slot_start.replace(tzinfo=IST)
            end_dt = user_state.selected_slot_end.replace(tzinfo=IST)
            
            new_booking = Booking(
                phone_number=user_state.phone_number,
                customer_name=customer_name,
                car_model=user_state.selected_car_model,
                car_variant=user_state.selected_car_variant,
                slot_start=user_state.selected_slot_start,
                slot_end=user_state.selected_slot_end,
                status="COMPLETED",
                created_at=now_naive
            )
            session.add(new_booking)
            await session.flush()
            
            event_id = create_test_drive_event(
                customer_name=customer_name,
                phone_number=user_state.phone_number,
                car_model=user_state.selected_car_model,
                car_variant=user_state.selected_car_variant,
                slot_start=start_dt,
                slot_end=end_dt
            )
            new_booking.calendar_event_id = event_id
            
            t_24h = start_dt - timedelta(hours=24)
            t_2h = start_dt - timedelta(hours=2)
            
            reminder_24h = Reminder(
                booking_id=new_booking.id,
                reminder_type="24H",
                scheduled_time=t_24h.replace(tzinfo=None),
                status="PENDING"
            )
            reminder_2h = Reminder(
                booking_id=new_booking.id,
                reminder_type="2H",
                scheduled_time=t_2h.replace(tzinfo=None),
                status="PENDING"
            )
            session.add_all([reminder_24h, reminder_2h])
            await session.flush()
            
            ReminderScheduler.schedule_reminder_job(reminder_24h.id, t_24h)
            ReminderScheduler.schedule_reminder_job(reminder_2h.id, t_2h)
            
            user_state.state = STATE_IDLE
            user_state.selected_car_model = None
            user_state.selected_car_variant = None
            user_state.slots_json = None
            user_state.slot_generated_at = None
            user_state.selected_slot_start = None
            user_state.selected_slot_end = None
            
            day_str = start_dt.strftime("%A")
            time_str = start_dt.strftime("%I:%M %p").lstrip("0")
            
            return (
                f"Done ✅ Test drive booked – {new_booking.car_model} {new_booking.car_variant}, "
                f"{day_str} {time_str}.\n"
                f"I'll remind you a day before and 2 hours before. See you then!"
            )
            
        except ValueError as e:
            user_state.state = STATE_AWAITING_SLOT
            user_state.selected_slot_start = None
            user_state.selected_slot_end = None
            return f"Ah, it looks like that slot is no longer available. {str(e)} Let's choose another slot."
        except Exception as e:
            logger.exception("Failed to book test drive.")
            return "I'm sorry, I encountered an issue booking the test drive on the system. Please try again in a few moments."

    elif intent == "INTENT_CANCEL":
        user_state.state = STATE_IDLE
        user_state.selected_car_model = None
        user_state.selected_car_variant = None
        user_state.slots_json = None
        user_state.slot_generated_at = None
        user_state.selected_slot_start = None
        user_state.selected_slot_end = None
        return "Test drive booking process canceled. What else can I help you with?"

    else:
        start_dt = user_state.selected_slot_start.replace(tzinfo=IST)
        day_str = start_dt.strftime("%A, %b %d")
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")
        return (
            f"You have a pending booking request for {user_state.selected_car_model} ({user_state.selected_car_variant}) on {day_str} at {time_str}.\n\n"
            f"Please reply with 'Confirm' to finalize the booking, or 'Cancel'."
        )

async def handle_booking_init(
    session: AsyncSession,
    user_state: UserConversationState,
    entities: dict,
    now_naive: datetime
) -> str:
    """Initializes the booking state and extracts model/variant preference."""
    model = entities.get("car_model")
    variant = entities.get("car_variant")
    
    if not model:
        user_state.state = STATE_IDLE
        return "Which Maruti Suzuki car are you interested in booking a test drive for? (We have the Brezza, Swift, and Ertiga)."
        
    user_state.selected_car_model = model
    
    if not variant:
        variants_list = "VXi or ZXi+"
        if "swift" in model.lower():
            variants_list = "LXi or ZXi+"
            
        user_state.state = STATE_CAR_SELECTED
        return f"Which variant of the {model} would you like to drive? We offer {variants_list}."

    # Validate variant is in knowledgebase for this model
    from app.rag.kb_loader import KnowledgeBase
    kb_variant = KnowledgeBase.get_variant_details(model, variant)
    if not kb_variant:
        user_state.state = STATE_IDLE
        return f"Sorry, we do not offer the variant '{variant}' for the {model}. Please choose a valid variant."

    user_state.selected_car_variant = variant
    user_state.state = STATE_CAR_SELECTED
    
    now_ist = get_ist_now()
    return await start_qualification_or_suggest_slots(user_state, now_ist, now_naive, entities.get("date_preference"))

async def start_qualification_or_suggest_slots(
    user_state: UserConversationState,
    now_ist: datetime,
    now_naive: datetime,
    date_preference: str = None
) -> str:
    """Checks if lead qualification is completed. If not, initiates the qualifying flow. Otherwise, suggests slots."""
    if user_state.lead_completed:
        return await suggest_and_transition_slots(user_state, now_ist, now_naive, date_preference)
        
    user_state.qualification_attempts = 0

    if not user_state.lead_budget:
        user_state.state = STATE_QUALIFYING_BUDGET
        return (
            f"Awesome choice! Before we select a slot for your test drive of the "
            f"{user_state.selected_car_model} ({user_state.selected_car_variant}), "
            f"could you tell us what your approximate budget is?"
        )
    elif not user_state.lead_timeline:
        user_state.state = STATE_QUALIFYING_TIMELINE
        return (
            "Got it. And when are you looking to purchase the vehicle? "
            "(e.g., immediate, within a month, or just researching?)"
        )
    elif not user_state.lead_fuel:
        user_state.state = STATE_QUALIFYING_FUEL
        return (
            "Understood. Lastly, what is your preferred fuel type? "
            "(Petrol, Diesel, CNG, or Hybrid?)"
        )
    else:
        user_state.lead_completed = True
        return await suggest_and_transition_slots(user_state, now_ist, now_naive, date_preference)

async def proceed_qualification(
    user_state: UserConversationState,
    now_ist: datetime,
    now_naive: datetime,
    date_preference: str = None
) -> str:
    """Transitions to the next missing qualification state or completes qualification and suggests slots."""
    user_state.qualification_attempts = 0

    if not user_state.lead_timeline:
        user_state.state = STATE_QUALIFYING_TIMELINE
        return (
            "Got it. And when are you looking to purchase the vehicle? "
            "(e.g., immediate, within a month, or just researching?)"
        )
    elif not user_state.lead_fuel:
        user_state.state = STATE_QUALIFYING_FUEL
        return (
            "Understood. Lastly, what is your preferred fuel type? "
            "(Petrol, Diesel, CNG, or Hybrid?)"
        )
    else:
        user_state.lead_completed = True
        return await suggest_and_transition_slots(user_state, now_ist, now_naive, date_preference)

async def suggest_and_transition_slots(
    user_state: UserConversationState,
    now_ist: datetime,
    now_naive: datetime,
    date_preference: str = None
) -> str:
    """Generates slot suggestions, stores them in user state, and transitions state to STATE_AWAITING_SLOT."""
    target_date = now_ist.date() + timedelta(days=1)
    
    if date_preference:
        pref = date_preference.lower()
        if "weekend" in pref or "sat" in pref or "sun" in pref:
            days_until_sat = (5 - now_ist.weekday()) % 7
            if days_until_sat == 0:
                days_until_sat = 7
            target_date = now_ist.date() + timedelta(days=days_until_sat)
        elif "today" in pref or "aaj" in pref:
            target_date = now_ist.date()
        elif "tomorrow" in pref or "kal" in pref:
            target_date = now_ist.date() + timedelta(days=1)
        elif "parso" in pref or "day after" in pref:
            target_date = now_ist.date() + timedelta(days=2)
            
    slots = get_available_slots(target_date, limit=3)
    if len(slots) < 3 and date_preference and "weekend" in date_preference.lower():
        sat_slots = slots
        sun_date = target_date + timedelta(days=1)
        sun_slots = get_available_slots(sun_date, limit=3 - len(sat_slots))
        slots = sat_slots + sun_slots
        
    if not slots:
        slots = get_available_slots(now_ist.date() + timedelta(days=2), limit=3)

    if not slots:
        return "I'm sorry, there are no available test drive slots in the next few days. Please check back later!"

    slots_data = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in slots]
    user_state.slots_json = json.dumps(slots_data)
    user_state.slot_generated_at = now_naive
    user_state.state = STATE_AWAITING_SLOT

    slots_list_msg = format_slots_message(slots_data)
    
    buttons = []
    for i, slot in enumerate(slots_data[:3]):
        start_dt = datetime.fromisoformat(slot["start"]).astimezone(IST)
        day_str = start_dt.strftime("%a")
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")
        buttons.append((f"slot_{i+1}", f"{day_str} {time_str}"))

    # Determine day description
    is_weekend = False
    if date_preference:
        pref = date_preference.lower()
        if "weekend" in pref or "sat" in pref or "sun" in pref:
            is_weekend = True
    else:
        for slot in slots_data:
            start_dt = datetime.fromisoformat(slot["start"]).astimezone(IST)
            if start_dt.weekday() in [5, 6]:
                is_weekend = True
                break
    day_desc = "this weekend" if is_weekend else "tomorrow"

    body_text = f"Sure! Here are open slots {day_desc}:\n{slots_list_msg}\nWhich works?"
    sent = send_whatsapp_buttons(
        to=user_state.phone_number,
        header="Select Test Drive Slot",
        body=body_text,
        buttons=buttons
    )
    if sent:
        return ""
    return body_text

async def regenerate_slots_and_reply(
    user_state: UserConversationState,
    now_ist: datetime,
    now_naive: datetime,
    reply_header: str
) -> str:
    """Regenerates slot list after expiration, saves to state, and sends them."""
    target_date = now_ist.date() + timedelta(days=1)
    slots = get_available_slots(target_date, limit=3)
    
    if not slots:
        user_state.state = STATE_IDLE
        return f"{reply_header} Unfortunately, there are no other free slots at this time. Let's restart later."

    slots_data = [{"start": s.isoformat(), "end": e.isoformat()} for s, e in slots]
    user_state.slots_json = json.dumps(slots_data)
    user_state.slot_generated_at = now_naive
    user_state.state = STATE_AWAITING_SLOT

    slots_list_msg = format_slots_message(slots_data)

    buttons = []
    for i, slot in enumerate(slots_data):
        start_dt = datetime.fromisoformat(slot["start"]).astimezone(IST)
        day_str = start_dt.strftime("%a")
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")
        buttons.append((f"slot_{i+1}", f"{day_str} {time_str}"))

    body_text = f"{reply_header} Let's try these fresh available slots instead:\n{slots_list_msg}\nWhich works?"
    sent = send_whatsapp_buttons(
        to=user_state.phone_number,
        header="Select Slot",
        body=body_text,
        buttons=buttons
    )
    if sent:
        return ""
    return body_text

def format_slots_message(slots_data: list[dict]) -> str:
    """Formats slot selections as a bullet list."""
    lines = []
    for i, slot in enumerate(slots_data):
        start_dt = datetime.fromisoformat(slot["start"]).astimezone(IST)
        day_str = start_dt.strftime("%a")
        time_str = start_dt.strftime("%I:%M %p").lstrip("0")
        lines.append(f"{i+1}) {day_str} {time_str}")
    return "  ".join(lines)
