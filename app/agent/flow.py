import logging
import asyncio
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db.session import async_session_factory
from app.db.models import ProcessedWebhookMessage
from app.whatsapp.parser import parse_incoming_webhook
from app.whatsapp.sender import send_whatsapp_message
from app.agent.intent import parse_intent_with_llm
from app.agent.state import get_or_create_user_state, transition_state

logger = logging.getLogger(__name__)

async def handle_whatsapp_webhook(payload: dict) -> dict:
    """
    Main webhook controller.
    Coordinates webhook verification/ingestion, de-duplication, state transition, and messaging.
    """
    # 1. Parse payload
    parsed = parse_incoming_webhook(payload)
    if not parsed["is_valid"]:
        # Webhook payload was not a valid incoming message update
        return {"status": "ignored"}

    phone_number = parsed["phone_number"]
    message_id = parsed["message_id"]
    customer_name = parsed["customer_name"]
    message_text = parsed["text"]
    button_id = parsed["button_id"]

    # 2. Open Async DB Transaction
    async with async_session_factory() as session:
        try:
            # 3. Webhook Retry De-duplication Check
            dedup_query = select(ProcessedWebhookMessage).where(ProcessedWebhookMessage.message_id == message_id)
            d_res = await session.execute(dedup_query)
            existing_msg = d_res.scalar_one_or_none()

            if existing_msg:
                logger.info(f"Duplicate WhatsApp webhook message detected (ID: {message_id}). Dropping request.")
                return {"status": "duplicate"}

            # Log message_id to processed messages to guard against retries
            new_msg = ProcessedWebhookMessage(message_id=message_id)
            session.add(new_msg)
            await session.flush()

            # 4. Get User State (with row lock)
            user_state = await get_or_create_user_state(session, phone_number)
            current_state = user_state.state

            # 5. Extract intent and entities (uses quick route if button clicked)
            # If button reply is clicked, text is the button title, but button_id is populated.
            # We pass both to the NLU module
            nlu_input = button_id if button_id else message_text
            nlu_res = await parse_intent_with_llm(nlu_input, current_state)
            
            intent = nlu_res.get("intent")
            entities = nlu_res.get("entities", {})

            # 6. Execute Transition
            reply_text = await transition_state(
                session=session,
                user_state=user_state,
                intent=intent,
                entities=entities,
                user_message=message_text,
                customer_name=customer_name
            )

            # 7. Commit database changes
            await session.commit()

            # 8. Send WhatsApp Message (if reply_text is generated)
            if reply_text:
                # Simulate typing delay
                await asyncio.sleep(1)
                send_whatsapp_message(phone_number, reply_text)

            # Structured Log
            logger.info(f"Structured Log: {{'phone': '{phone_number}', 'intent': '{intent}', 'state_before': '{current_state}', 'state_after': '{user_state.state}', 'action': 'reply_sent'}}")

            return {"status": "success"}

        except Exception as e:
            # Rollback database on any exception to release row locks
            await session.rollback()
            logger.exception(f"Error handling WhatsApp webhook for phone {phone_number}: {e}")
            send_whatsapp_message(phone_number, "Oops, I encountered a temporary connection issue. Let's try again in a moment.")
            return {"status": "error", "detail": str(e)}
