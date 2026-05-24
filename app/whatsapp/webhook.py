import os
from fastapi import APIRouter, Request, Response, BackgroundTasks, Query
import logging
from app.whatsapp.parser import parse_incoming_webhook
from app.agent.flow import handle_whatsapp_webhook

logger = logging.getLogger(__name__)

router = APIRouter()

@router.get("/webhook")
def verify_webhook(
    mode: str = Query(None, alias="hub.mode"),
    verify_token: str = Query(None, alias="hub.verify_token"),
    challenge: str = Query(None, alias="hub.challenge")
):
    """
    Webhook verification endpoint for Meta.
    Verifies token and returns challenge string.
    """
    expected_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "carbolo_verification_token_123")
    
    if mode == "subscribe" and verify_token == expected_token:
        logger.info("WhatsApp webhook verified successfully!")
        return Response(content=challenge, media_type="text/plain", status_code=200)
    else:
        logger.warning(f"Webhook verification failed. Received token: {verify_token}")
        return Response(content="Verification failed", status_code=403)

@router.post("/webhook")
async def receive_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    Ingests messages from Meta and schedules processing in a background task
    to respond within Meta's strict timeout window.
    """
    try:
        payload = await request.json()
        logger.debug(f"Received webhook payload: {payload}")
        
        parsed = parse_incoming_webhook(payload)
        if parsed["is_valid"]:
            logger.info(f"Scheduling background processing for message ID: {parsed['message_id']}")
            background_tasks.add_task(handle_whatsapp_webhook, payload)
            
        # Meta expects a prompt 200 OK
        return Response(content="EVENT_RECEIVED", status_code=200)
    except Exception as e:
        logger.error(f"Error receiving webhook payload: {e}")
        return Response(content="Internal Server Error", status_code=500)
