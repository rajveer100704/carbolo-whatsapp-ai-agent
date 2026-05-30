import logging
import asyncio

from app.whatsapp.parser import parse_incoming_webhook
from app.whatsapp.sender import send_whatsapp_message
from app.agent.central_service import CentralAgentService, DuplicateMessageError

logger = logging.getLogger(__name__)

# Initialize central agent singleton
central_agent = CentralAgentService()

async def handle_whatsapp_webhook(payload: dict) -> dict:
    """
    Main webhook controller.
    Coordinates webhook verification/ingestion, de-duplication, state transition, and messaging.
    Delegates to CentralAgentService for core processing.
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

    try:
        # Process message via the CentralAgentService
        reply_text = await central_agent.process_message(
            channel="whatsapp",
            user_id=phone_number,
            message=message_text,
            customer_name=customer_name,
            message_id=message_id,
            button_id=button_id
        )

        # Send WhatsApp Message (if reply_text is generated)
        if reply_text:
            # Simulate typing delay
            await asyncio.sleep(1)
            send_whatsapp_message(phone_number, reply_text)

        return {"status": "success"}

    except DuplicateMessageError:
        return {"status": "duplicate"}
    except Exception as e:
        logger.exception(f"Error handling WhatsApp webhook for phone {phone_number}: {e}")
        # Send error fallback message
        send_whatsapp_message(phone_number, "Oops, I encountered a temporary connection issue. Let's try again in a moment.")
        return {"status": "error", "detail": str(e)}
