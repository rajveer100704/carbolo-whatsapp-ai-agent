import os
import httpx
import logging

logger = logging.getLogger(__name__)

def send_whatsapp_message(to: str, text: str) -> bool:
    """
    Sends a simple text message via WhatsApp Cloud API.
    If credentials are set to mock, logs output instead of sending.
    """
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "mock-phone-id")
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "mock-access-token")

    if access_token == "mock-access-token" or phone_number_id == "mock-phone-id":
        logger.info(f"\n===== [Mock WhatsApp Sent to {to}] =====\n{text}\n========================================")
        return True

    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {
            "preview_url": False,
            "body": text
        }
    }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=10.0)
        if response.status_code in [200, 201]:
            logger.info(f"Successfully sent text message to {to}")
            return True
        else:
            logger.error(f"Failed to send WhatsApp message to {to}. Response: {response.text}")
            return False
    except Exception as e:
        logger.error(f"Error sending WhatsApp message: {e}")
        return False

def send_whatsapp_buttons(to: str, header: str, body: str, buttons: list[tuple[str, str]]) -> bool:
    """
    Sends an interactive reply buttons message.
    buttons: list of (button_id, button_title) tuples. Max 3 buttons.
    If credentials are set to mock, logs output.
    """
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "mock-phone-id")
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "mock-access-token")

    if len(buttons) > 3:
        buttons = buttons[:3]

    if access_token == "mock-access-token" or phone_number_id == "mock-phone-id":
        btn_str = "\n".join([f"[{i+1}] {title} (ID: {bid})" for i, (bid, title) in enumerate(buttons)])
        logger.info(
            f"\n===== [Mock WhatsApp Buttons Sent to {to}] =====\n"
            f"Header: {header}\n"
            f"Body: {body}\n"
            f"Buttons:\n{btn_str}\n"
            f"================================================="
        )
        return True

    url = f"https://graph.facebook.com/v20.0/{phone_number_id}/messages"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    
    formatted_buttons = []
    for button_id, button_title in buttons:
        # Title limit is 20 chars
        title = button_title[:20]
        formatted_buttons.append({
            "type": "reply",
            "reply": {
                "id": button_id,
                "title": title
            }
        })

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {
                "text": body
            },
            "action": {
                "buttons": formatted_buttons
            }
        }
    }
    
    if header:
        payload["interactive"]["header"] = {
            "type": "text",
            "text": header[:60] # Header limit is 60 chars
        }

    try:
        response = httpx.post(url, headers=headers, json=payload, timeout=10.0)
        if response.status_code in [200, 201]:
            logger.info(f"Successfully sent buttons to {to}")
            return True
        else:
            logger.error(f"Failed to send WhatsApp buttons to {to}. Response: {response.text}")
            # Fall back to text if buttons fail
            button_text = f"{header}\n\n{body}\n\n" + "\n".join([f"- {title}" for _, title in buttons])
            return send_whatsapp_message(to, button_text)
    except Exception as e:
        logger.error(f"Error sending WhatsApp buttons: {e}")
        return False
