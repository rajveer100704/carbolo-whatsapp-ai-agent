import logging
from sqlalchemy import select
import app.db.session
from app.db.models import ProcessedWebhookMessage
from app.agent.intent import parse_intent_with_llm
from app.agent.state import get_or_create_user_state, transition_state

logger = logging.getLogger(__name__)

class DuplicateMessageError(Exception):
    """Raised when a message with a duplicate ID is processed."""
    pass

class CentralAgentService:
    async def process_message(
        self,
        channel: str,
        user_id: str,
        message: str,
        customer_name: str = "User",
        message_id: str = None,
        button_id: str = None
    ) -> str | None:
        """
        Unified processing entry point for all channels (WhatsApp, Gmail, etc.)
        Uses unified identity prefixing (channel:user_id) for collision prevention.
        """
        if channel == "whatsapp":
            user_key = user_id
        else:
            user_key = f"{channel}:{user_id}"
        
        async with app.db.session.async_session_factory() as session:
            try:
                # 1. Message De-duplication Check
                if message_id:
                    dedup_key = f"{channel}:{message_id}"
                    dedup_query = select(ProcessedWebhookMessage).where(ProcessedWebhookMessage.message_id == dedup_key)
                    d_res = await session.execute(dedup_query)
                    existing_msg = d_res.scalar_one_or_none()

                    if existing_msg:
                        logger.info(f"Duplicate message detected for channel {channel} (ID: {message_id}). Dropping request.")
                        raise DuplicateMessageError(f"Duplicate message ID: {message_id}")

                    # Log message_id to processed messages
                    new_msg = ProcessedWebhookMessage(message_id=dedup_key)
                    session.add(new_msg)
                    await session.flush()

                # 2. Get User State (with row lock)
                user_state = await get_or_create_user_state(session, user_key)
                current_state = user_state.state

                # 3. Parse intent and entities
                nlu_input = button_id if button_id else message
                nlu_res = await parse_intent_with_llm(nlu_input, current_state)
                
                intent = nlu_res.get("intent")
                entities = nlu_res.get("entities", {})

                # 4. Execute Transition
                reply_text = await transition_state(
                    session=session,
                    user_state=user_state,
                    intent=intent,
                    entities=entities,
                    user_message=message,
                    customer_name=customer_name,
                    channel=channel
                )

                # 5. Commit database changes
                await session.commit()

                # Structured Log
                logger.info(f"Structured Log: {{'user_key': '{user_key}', 'intent': '{intent}', 'state_before': '{current_state}', 'state_after': '{user_state.state}', 'action': 'reply_generated'}}")

                return reply_text

            except DuplicateMessageError:
                # Re-raise duplicate error so the controller handles it
                raise
            except Exception as e:
                # Rollback database on any exception to release row locks
                await session.rollback()
                logger.exception(f"Error processing message for channel {channel}, user {user_id}: {e}")
                raise e
