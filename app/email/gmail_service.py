import os
import json
import imaplib
import smtplib
import email
from email.mime.text import MIMEText
from email.utils import parseaddr
from datetime import datetime, timezone
import logging
from sqlalchemy import select
from app.db.session import async_session_factory
from app.db.models import ProcessedWebhookMessage

logger = logging.getLogger(__name__)

ALLOWED_SENDERS = {
    "carboloagent@gmail.com",
}

def is_allowed_sender(sender_email: str) -> bool:
    return sender_email.lower().strip() in ALLOWED_SENDERS

def should_ignore_email(msg_or_sender, subject: str = None) -> bool:
    """
    Checks if an email should be ignored based on headers, sender, or subject.
    Can be called with:
      should_ignore_email(msg: Message)
    or
      should_ignore_email(sender: str, subject: str)
    """
    if isinstance(msg_or_sender, str):
        sender = msg_or_sender
        # subject is passed as second argument
    else:
        msg = msg_or_sender
        # 1. Check standard automated/newsletter headers
        if msg.get("List-Unsubscribe") or msg.get("List-ID") or msg.get("List-Id"):
            return True
            
        auto_submitted = msg.get("Auto-Submitted", "").lower()
        if auto_submitted and auto_submitted != "no":
            return True
            
        precedence = msg.get("Precedence", "").lower()
        if precedence in ("bulk", "list", "junk"):
            return True

        from_header = msg.get("From", "")
        sender_name, sender_email = parseaddr(from_header)
        sender = sender_email or from_header
        subject = msg.get("Subject", "")

    sender_lower = sender.lower() if sender else ""
    subject_lower = subject.lower() if subject else ""
    
    ignore_senders = [
        "mailer-daemon", "noreply", "no-reply", "donotreply", 
        "auto-", "newsletter", "linkedin", "naukri", "jobs",
        "hire", "career", "apply", "recruit", "substack", "beehiiv",
        "digest", "forbes", "reddit", "unstop", "ziprecruiter",
        "quora", "edx", "lensa", "internshala", "devpost", "hackerearth",
        "gainrepmail", "propeers", "googlemail", "glassdoor", "indeed",
        "growasengineers", "labmentix", "innovexis", "blackkiteai"
    ]
    ignore_subjects = [
        "delivery status", "undelivered", "failure notice", 
        "out of office", "auto-reply", "bounce", "spam",
        "unsubscribe", "digest", "newsletter", "weekly", "daily",
        "monthly", "job alert", "hiring", "internship", "applied skills"
    ]
    
    if any(pattern in sender_lower for pattern in ignore_senders):
        return True
    if any(pattern in subject_lower for pattern in ignore_subjects):
        return True
        
    return False


# Ensure data directory exists
DATA_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
os.makedirs(DATA_DIR, exist_ok=True)

MOCK_INBOX_PATH = os.path.join(DATA_DIR, "mock_inbox.json")
MOCK_OUTBOX_PATH = os.path.join(DATA_DIR, "mock_outbox.json")

class GmailService:
    def __init__(self):
        self.email_user = os.getenv("EMAIL_USER") or os.getenv("GMAIL_ADDRESS") or "mock@example.com"
        self.email_password = os.getenv("EMAIL_PASSWORD") or os.getenv("GMAIL_APP_PASSWORD") or "mock-password"
        self.is_mock = (
            self.email_user == "mock@example.com"
            or self.email_password == "mock-password"
            or not self.email_user
            or not self.email_password
        )
        if self.is_mock:
            logger.info("GmailService initialized in MOCK mode. Falls back to mock JSON files.")
            self._ensure_mock_files()

    def _ensure_mock_files(self):
        """Creates dummy files for mock mode if they do not exist."""
        if not os.path.exists(MOCK_INBOX_PATH):
            # Populate with a default test drive request email to help users demo easily
            initial_emails = [
                {
                    "sender": "sanskar@example.com",
                    "sender_name": "Sanskar Soni",
                    "subject": "Test Drive Request",
                    "body": "Hi, I want to book a Brezza test drive tomorrow. Budget is 12 lakhs.",
                    "message_id": "mock_email_001",
                    "processed": False
                }
            ]
            with open(MOCK_INBOX_PATH, "w", encoding="utf-8") as f:
                json.dump(initial_emails, f, indent=2)
            logger.info(f"Created default mock inbox at {MOCK_INBOX_PATH}")

        if not os.path.exists(MOCK_OUTBOX_PATH):
            with open(MOCK_OUTBOX_PATH, "w", encoding="utf-8") as f:
                json.dump([], f, indent=2)

    async def fetch_unread_emails(self) -> list[dict]:
        """
        Fetches unread emails from IMAP or mock JSON.
        """
        if self.is_mock:
            return self._fetch_mock_emails()
        
        return await self._fetch_real_emails()

    def _fetch_mock_emails(self) -> list[dict]:
        """Reads unprocessed emails from mock inbox and marks them processed."""
        if not os.path.exists(MOCK_INBOX_PATH):
            return []
            
        try:
            with open(MOCK_INBOX_PATH, "r+", encoding="utf-8") as f:
                emails = json.load(f)
                unread = []
                for e in emails:
                    if not e.get("processed", False):
                        unread.append({
                            "sender": e["sender"],
                            "sender_name": e.get("sender_name", "User"),
                            "subject": e.get("subject", "No Subject"),
                            "body": e.get("body", ""),
                            "message_id": e.get("message_id", "mock-msg-id")
                        })
                        e["processed"] = True
                
                # Write back changes
                f.seek(0)
                json.dump(emails, f, indent=2)
                f.truncate()
                return unread
        except Exception as ex:
            logger.error(f"Error reading mock inbox: {ex}")
            return []

    async def _fetch_real_emails(self) -> list[dict]:
        """Fetches unseen and recent messages from real Gmail IMAP and filters duplicates/spam/unauthorized senders."""
        unread_list = []
        try:
            # Connect using SSL
            mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
            mail.login(self.email_user, self.email_password)
            status, select_data = mail.select("inbox")
            if status != "OK":
                logger.error(f"IMAP select inbox failed: {select_data}")
                return []
                
            total_messages = int(select_data[0]) if select_data and select_data[0] else 0
            
            # Search for unseen (unread) messages
            status, response = mail.search(None, "UNSEEN")
            unseen_ids = []
            if status == "OK" and response[0]:
                unseen_ids = [int(x) for x in response[0].split()]
                
            # Get sequence numbers of the last 50 messages in the inbox
            recent_ids = list(range(max(1, total_messages - 49), total_messages + 1))
            
            # Combine, deduplicate, and sort (ascending)
            combined_ids = sorted(list(set(unseen_ids + recent_ids)))
            
            # Limit to the last 50 (newest) candidate messages to scan
            combined_ids = combined_ids[-50:]
            
            logger.info(
                f"Scanning {len(combined_ids)} recent/unseen emails in IMAP (Total inbox size: {total_messages})."
            )
            
            for msg_id_int in combined_ids:
                msg_id = str(msg_id_int)
                
                # 1. Fetch only the headers first (fast)
                status, header_data = mail.fetch(msg_id, "(RFC822.HEADER)")
                if status != "OK" or not header_data or len(header_data[0]) < 2:
                    continue
                    
                raw_headers = header_data[0][1]
                msg = email.message_from_bytes(raw_headers)
                
                message_id = msg.get("Message-ID", f"real-msg-{msg_id}")
                
                # Check duplication against the database
                async with async_session_factory() as session:
                    dedup_key = f"gmail:{message_id}"
                    dedup_query = select(ProcessedWebhookMessage).where(ProcessedWebhookMessage.message_id == dedup_key)
                    d_res = await session.execute(dedup_query)
                    existing_msg = d_res.scalar_one_or_none()
                    
                if existing_msg:
                    # Already processed. If it was unseen, mark it read to keep inbox clean
                    if msg_id_int in unseen_ids:
                        mail.store(msg_id, "+FLAGS", "\\Seen")
                    continue
                
                # Parse sender
                from_header = msg.get("From", "")
                sender_name, sender_email = parseaddr(from_header)
                if not sender_email:
                    sender_email = from_header
                
                # Check allowlist first!
                if not is_allowed_sender(sender_email):
                    logger.info(f"Ignoring unauthorized sender: {sender_email}")
                    # Mark read so we don't scan it forever
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    
                    # Store message ID in ProcessedWebhookMessage to prevent re-fetching header in the future
                    async with async_session_factory() as session:
                        dedup_key = f"gmail:{message_id}"
                        new_msg = ProcessedWebhookMessage(message_id=dedup_key)
                        session.add(new_msg)
                        await session.commit()
                    continue

                # Check if it should be ignored as spam/newsletter
                if should_ignore_email(msg):
                    logger.info(f"GmailService: Ignoring automated/spam email ID {msg_id} (Subject: {msg.get('Subject', '')})")
                    # Mark ignored emails as read
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                    
                    # Also log to ProcessedWebhookMessage so we don't scan it again
                    async with async_session_factory() as session:
                        dedup_key = f"gmail:{message_id}"
                        new_msg = ProcessedWebhookMessage(message_id=dedup_key)
                        session.add(new_msg)
                        await session.commit()
                    continue
                    
                # 2. Fetch the full email content
                status, full_data = mail.fetch(msg_id, "(RFC822)")
                if status != "OK" or not full_data or len(full_data[0]) < 2:
                    continue
                    
                raw_email = full_data[0][1]
                msg = email.message_from_bytes(raw_email)
                
                # Parse body
                body = ""
                if msg.is_multipart():
                    for part in msg.walk():
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition"))
                        if content_type == "text/plain" and "attachment" not in content_disposition:
                            charset = part.get_content_charset() or "utf-8"
                            body = part.get_payload(decode=True).decode(charset, errors="ignore")
                            break
                else:
                    charset = msg.get_content_charset() or "utf-8"
                    body = msg.get_payload(decode=True).decode(charset, errors="ignore")

                unread_list.append({
                    "sender": sender_email,
                    "sender_name": sender_name,
                    "subject": msg.get("Subject", "No Subject"),
                    "body": body,
                    "message_id": message_id
                })
                
                # Ensure it's marked as read
                mail.store(msg_id, "+FLAGS", "\\Seen")
                
            mail.close()
            mail.logout()
        except Exception as e:
            logger.error(f"Error fetching emails from Gmail IMAP: {e}", exc_info=True)
        return unread_list


    async def send_email_reply(self, to_email: str, subject: str, body: str, in_reply_to: str):
        """
        Sends an email reply.
        """
        if self.is_mock:
            self._send_mock_email(to_email, subject, body, in_reply_to)
            return
            
        await self._send_real_email(to_email, subject, body, in_reply_to)

    def _send_mock_email(self, to_email: str, subject: str, body: str, in_reply_to: str):
        """Appends the mock outgoing email reply to mock_outbox.json."""
        logger.info(
            f"\n===== [Mock Email Reply Sent to {to_email}] =====\n"
            f"Subject: {subject}\n"
            f"In-Reply-To: {in_reply_to}\n"
            f"Body:\n{body}\n"
            f"================================================="
        )
        
        try:
            with open(MOCK_OUTBOX_PATH, "r+", encoding="utf-8") as f:
                outbox = json.load(f)
                outbox.append({
                    "to": to_email,
                    "subject": subject,
                    "body": body,
                    "in_reply_to": in_reply_to,
                    "timestamp": datetime.now(timezone.utc).isoformat()
                })
                f.seek(0)
                json.dump(outbox, f, indent=2)
                f.truncate()
        except Exception as ex:
            logger.error(f"Error writing mock outbox: {ex}")

    async def _send_real_email(self, to_email: str, subject: str, body: str, in_reply_to: str):
        """Sends a threaded RFC822 email reply via Gmail SMTP."""
        try:
            msg = MIMEText(body, "plain", "utf-8")
            msg["To"] = to_email
            msg["From"] = self.email_user
            msg["Subject"] = subject
            
            # Gmail threading headers
            if in_reply_to:
                msg["In-Reply-To"] = in_reply_to
                msg["References"] = in_reply_to

            # Connect using TLS
            server = smtplib.SMTP("smtp.gmail.com", 587)
            server.starttls()
            server.login(self.email_user, self.email_password)
            server.send_message(msg)
            server.quit()
            logger.info(f"Successfully sent real email reply to {to_email}")
        except Exception as e:
            logger.error(f"Error sending email reply via Gmail SMTP: {e}")
