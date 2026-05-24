import logging

logger = logging.getLogger(__name__)

def parse_incoming_webhook(payload: dict) -> dict:
    """
    Parses incoming Meta WhatsApp Cloud API webhooks.
    Returns a dict with phone_number, customer_name, message_id, text, button_id, and is_valid.
    """
    result = {
        "phone_number": "",
        "customer_name": "Customer",
        "message_id": "",
        "text": "",
        "button_id": None,
        "is_valid": False
    }

    try:
        # Check entries
        if not payload.get("object") == "whatsapp_business_account":
            return result

        entry = payload.get("entry", [])
        if not entry:
            return result

        changes = entry[0].get("changes", [])
        if not changes:
            return result

        value = changes[0].get("value", {})
        messages = value.get("messages", [])
        if not messages:
            # Could be a status update (sent, delivered, read) - ignore silently
            return result

        msg = messages[0]
        result["phone_number"] = msg.get("from", "")
        result["message_id"] = msg.get("id", "")
        result["is_valid"] = True

        # Parse contact name
        contacts = value.get("contacts", [])
        if contacts:
            profile = contacts[0].get("profile", {})
            result["customer_name"] = profile.get("name", "Customer")

        # Parse message content based on type
        msg_type = msg.get("type")
        
        if msg_type == "text":
            result["text"] = msg.get("text", {}).get("body", "").strip()
        elif msg_type == "interactive":
            interactive = msg.get("interactive", {})
            int_type = interactive.get("type")
            if int_type == "button_reply":
                button_reply = interactive.get("button_reply", {})
                result["button_id"] = button_reply.get("id", "")
                result["text"] = button_reply.get("title", "").strip()
        elif msg_type == "button":
            # Quick reply templates
            button = msg.get("button", {})
            result["button_id"] = button.get("payload", "")
            result["text"] = button.get("text", "").strip()
        else:
            logger.info(f"Received unhandled WhatsApp message type: {msg_type}")
            result["text"] = ""

    except Exception as e:
        logger.error(f"Error parsing incoming WhatsApp webhook: {e}")
        result["is_valid"] = False

    return result
