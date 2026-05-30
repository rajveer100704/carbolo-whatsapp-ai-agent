import asyncio
import logging
from app.email.gmail_service import GmailService, should_ignore_email, is_allowed_sender
from app.agent.central_service import CentralAgentService

logger = logging.getLogger(__name__)

class EmailWorker:
    def __init__(self):
        self.gmail_service = GmailService()
        self.central_agent = CentralAgentService()
        self.running = False
        self._task = None

    async def start(self):
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("EmailWorker background task started.")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("EmailWorker background task stopped.")

    async def _loop(self):
        while self.running:
            try:
                unread_emails = await self.gmail_service.fetch_unread_emails()
                for email_data in unread_emails:
                    sender = email_data["sender"]
                    sender_name = email_data["sender_name"]
                    subject = email_data["subject"]
                    body = email_data["body"]
                    message_id = email_data["message_id"]

                    # Hard allowlist guard — only trusted senders processed
                    if not is_allowed_sender(sender):
                        logger.info(f"EmailWorker: Blocked unauthorized sender: {sender}")
                        continue

                    if should_ignore_email(sender, subject):
                        logger.info(f"EmailWorker: Ignoring automated/spam/bounce email from {sender} (Subject: {subject})")
                        continue

                    logger.info(f"EmailWorker: Processing incoming email from {sender} (Subject: {subject})")
                    
                    # Call CentralAgentService
                    reply_text = await self.central_agent.process_message(
                        channel="gmail",
                        user_id=sender,
                        message=body,
                        customer_name=sender_name,
                        message_id=message_id
                    )

                    # Send reply if text is generated
                    if reply_text:
                        reply_subject = f"Re: {subject}" if not subject.lower().startswith("re:") else subject
                        await self.gmail_service.send_email_reply(
                            to_email=sender,
                            subject=reply_subject,
                            body=reply_text,
                            in_reply_to=message_id
                        )
            except Exception as e:
                logger.error(f"Error in EmailWorker polling loop: {e}", exc_info=True)
            
            # Poll every 5 seconds
            await asyncio.sleep(5)

# Singleton worker instance
email_worker = EmailWorker()
