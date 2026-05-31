"""
CarBOLO Full E2E Test Script
Tests both WhatsApp (via webhook simulation) and Email flows end-to-end.
Covers: QA queries, test drive booking for multiple cars, slot confirmation.
"""

import asyncio
import json
import time
import smtplib
import hashlib
import hmac
import os
import sys
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import httpx
from dotenv import load_dotenv

load_dotenv()

BASE_URL = "http://localhost:8000"
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "1054958191044148")
VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN", "")
GMAIL_USER = os.getenv("GMAIL_ADDRESS", "")
GMAIL_PASS = os.getenv("GMAIL_APP_PASSWORD", "")

# Test phone number (fake — simulates user sending messages)
TEST_PHONE = "919900000001"
TEST_EMAIL = "carboloagent@gmail.com"

PASS = "[PASS]"
FAIL = "[FAIL]"
INFO = "[INFO]"

results = []

def log(icon, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {icon}  {msg}")

def record(name, passed, detail=""):
    results.append({"name": name, "passed": passed, "detail": detail})
    log(PASS if passed else FAIL, f"{name}: {detail}")


# ─────────────────────────────────────────────
# SECTION 1: Server Health
# ─────────────────────────────────────────────
async def test_health():
    log(INFO, "=== SECTION 1: Server Health ===")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BASE_URL}/")
            ok = r.status_code == 200 and "healthy" in r.text
            record("Server /health", ok, r.text.strip()[:80])
    except Exception as e:
        record("Server /health", False, str(e))


# ─────────────────────────────────────────────
# SECTION 2: WhatsApp Webhook Verification
# ─────────────────────────────────────────────
async def test_webhook_verify():
    log(INFO, "=== SECTION 2: Webhook GET Verification ===")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BASE_URL}/webhook", params={
                "hub.mode": "subscribe",
                "hub.challenge": "TEST_CHALLENGE_123",
                "hub.verify_token": VERIFY_TOKEN
            })
            ok = r.status_code == 200 and "TEST_CHALLENGE_123" in r.text
            record("WhatsApp webhook GET verify", ok, f"status={r.status_code} body={r.text[:60]}")
    except Exception as e:
        record("WhatsApp webhook GET verify", False, str(e))


# ─────────────────────────────────────────────
# SECTION 3: WhatsApp Simulated Conversation
# Sends real webhook POST payloads to /webhook
# ─────────────────────────────────────────────
def make_wa_payload(phone: str, text: str, msg_id: str) -> dict:
    """Build a real Meta-style WhatsApp webhook POST body."""
    return {
        "object": "whatsapp_business_account",
        "entry": [{
            "id": "ENTRY_ID",
            "changes": [{
                "value": {
                    "messaging_product": "whatsapp",
                    "metadata": {
                        "display_phone_number": "15551395579",
                        "phone_number_id": PHONE_NUMBER_ID
                    },
                    "contacts": [{"profile": {"name": "E2E Tester"}, "wa_id": phone}],
                    "messages": [{
                        "from": phone,
                        "id": msg_id,
                        "timestamp": str(int(time.time())),
                        "text": {"body": text},
                        "type": "text"
                    }]
                },
                "field": "messages"
            }]
        }]
    }

async def send_wa_message(client: httpx.AsyncClient, phone: str, text: str, msg_id: str, delay: float = 0):
    """Send a simulated WhatsApp message to the webhook."""
    if delay:
        await asyncio.sleep(delay)
    payload = make_wa_payload(phone, text, msg_id)
    try:
        r = await client.post(f"{BASE_URL}/webhook", json=payload, timeout=15)
        log(INFO, f"  -> WA sent '{text}' | webhook={r.status_code}")
        return r.status_code == 200
    except Exception as e:
        log(FAIL, f"  -> WA send error: {e}")
        return False

async def test_whatsapp_flow():
    log(INFO, "=== SECTION 3: WhatsApp Full Conversation Flow ===")
    log(INFO, "  Testing: Brezza QA -> Swift booking -> slot confirm")

    # Use unique phone per test run to avoid FSM state carryover
    phone = f"9190{int(time.time()) % 10000000:07d}"
    msg_counter = [0]

    def next_id():
        msg_counter[0] += 1
        return f"wamid.E2ETEST{phone}{msg_counter[0]:04d}"

    async with httpx.AsyncClient(timeout=20) as client:

        # Step 1: Greeting
        ok = await send_wa_message(client, phone, "hi", next_id())
        await asyncio.sleep(3)
        record("WA: greeting received", ok)

        # Step 2: QA - Brezza features
        ok = await send_wa_message(client, phone, "what are the features of brezza", next_id())
        await asyncio.sleep(3)
        record("WA: Brezza features query", ok)

        # Step 3: QA - sunroof question
        ok = await send_wa_message(client, phone, "does brezza ZXi+ have a sunroof", next_id())
        await asyncio.sleep(3)
        record("WA: sunroof question", ok)

        # Step 4: Book Swift test drive
        ok = await send_wa_message(client, phone, "I want to book a Swift test drive", next_id())
        await asyncio.sleep(3)
        record("WA: Swift book request", ok)

        # Step 5: Pick variant
        ok = await send_wa_message(client, phone, "ZXi+", next_id())
        await asyncio.sleep(3)
        record("WA: variant selection ZXi+", ok)

        # Step 6: Budget
        ok = await send_wa_message(client, phone, "around 9 lakhs", next_id())
        await asyncio.sleep(3)
        record("WA: budget provided", ok)

        # Step 7: Timeline
        ok = await send_wa_message(client, phone, "tomorrow", next_id())
        await asyncio.sleep(3)
        record("WA: timeline tomorrow", ok)

        # Step 8: Fuel preference
        ok = await send_wa_message(client, phone, "petrol", next_id())
        await asyncio.sleep(4)
        record("WA: fuel preference petrol", ok)

        # Step 9: Select slot
        ok = await send_wa_message(client, phone, "2", next_id())
        await asyncio.sleep(4)
        record("WA: slot selection 2", ok)

        # Step 10: Confirm
        ok = await send_wa_message(client, phone, "yes confirm", next_id())
        await asyncio.sleep(5)
        record("WA: booking confirmation", ok)

    log(INFO, "  WhatsApp flow complete. Checking server logs for calendar booking...")
    await asyncio.sleep(2)


# ─────────────────────────────────────────────
# SECTION 4: WhatsApp — Second car (Ertiga)
# ─────────────────────────────────────────────
async def test_whatsapp_ertiga():
    log(INFO, "=== SECTION 4: WhatsApp Ertiga Booking ===")

    phone = f"9191{int(time.time()) % 10000000:07d}"
    msg_counter = [0]
    def next_id():
        msg_counter[0] += 1
        return f"wamid.E2EERTIGA{phone}{msg_counter[0]:04d}"

    async with httpx.AsyncClient(timeout=20) as client:
        steps = [
            ("hi", "greeting"),
            ("book ertiga test drive", "Ertiga book request"),
            ("VXi", "VXi variant select"),
            ("12 lakhs", "budget 12L"),
            ("this weekend", "timeline weekend"),
            ("diesel", "fuel diesel"),
            ("1", "slot 1"),
            ("yes", "confirm Ertiga booking"),
        ]
        for text, name in steps:
            ok = await send_wa_message(client, phone, text, next_id())
            await asyncio.sleep(3)
            record(f"WA Ertiga: {name}", ok)

    await asyncio.sleep(2)


# ─────────────────────────────────────────────
# SECTION 5: Email Flow — send real email
# ─────────────────────────────────────────────
def send_test_email(subject: str, body: str) -> bool:
    """Send a real email to carboloagent@gmail.com to trigger the worker."""
    try:
        msg = MIMEMultipart()
        msg["From"] = TEST_EMAIL
        msg["To"] = TEST_EMAIL
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_PASS)
        server.send_message(msg)
        server.quit()
        log(INFO, f"  -> Email sent: '{subject}'")
        return True
    except Exception as e:
        log(FAIL, f"  -> Email send failed: {e}")
        return False

async def test_email_flow():
    log(INFO, "=== SECTION 5: Email Flow — Brezza Booking ===")

    # Email 1: Initial booking request
    ok1 = send_test_email(
        "Test Drive Request",
        "Hi, I want to book a Brezza ZXi+ test drive tomorrow. My budget is 13 lakhs. Fuel: petrol. Thanks, E2E Tester"
    )
    record("Email: initial booking sent", ok1)
    log(INFO, "  Waiting 30s for worker to pick up and reply...")
    await asyncio.sleep(30)

    # Email 2: Slot selection reply (simulated as a new email since worker reads UNSEEN)
    ok2 = send_test_email(
        "Re: Test Drive Request",
        "1"  # Selecting slot 1
    )
    record("Email: slot selection sent", ok2)
    log(INFO, "  Waiting 30s for confirmation reply...")
    await asyncio.sleep(30)

    # Email 3: Confirm
    ok3 = send_test_email(
        "Re: Test Drive Request",
        "yes confirm"
    )
    record("Email: confirm booking sent", ok3)
    log(INFO, "  Waiting 30s for final confirmation...")
    await asyncio.sleep(30)


# ─────────────────────────────────────────────
# SECTION 6: Check logs for successful replies
# ─────────────────────────────────────────────
async def verify_logs(start_log_offset=0):
    log(INFO, "=== SECTION 6: Log Verification ===")
    try:
        if os.path.exists("app.log"):
            with open("app.log", "r", encoding="utf-8", errors="ignore") as f:
                f.seek(start_log_offset)
                content = f.read()
        else:
            content = ""

        checks = [
            ("Calendar booking created", "app.calendar.booking: Successfully booked"),
            ("WhatsApp replies sent", "Successfully sent text message"),
            ("Email replies sent", "Successfully sent real email reply"),
            ("No 401 token errors", "401 Unauthorized"),  # inverted
            ("FSM state transitions", "state_after"),
            ("No DB lock errors", "database is locked"),  # inverted
        ]

        for label, pattern in checks:
            found = pattern in content
            if "No 401" in label or "No DB lock" in label:
                # These should NOT be present in current run
                record(label, not found, "clean" if not found else f"FOUND: {pattern}")
            else:
                record(label, found, "found in logs" if found else "NOT FOUND")
    except Exception as e:
        record("Log verification", False, str(e))


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
async def main():
    print("\n" + "="*60)
    print("  CarBOLO E2E Test Suite")
    print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60 + "\n")

    # Record log file offset before test starts
    start_log_offset = 0
    if os.path.exists("app.log"):
        try:
            start_log_offset = os.path.getsize("app.log")
        except Exception:
            pass

    await test_health()
    await asyncio.sleep(1)

    await test_webhook_verify()
    await asyncio.sleep(1)

    await test_whatsapp_flow()
    await asyncio.sleep(2)

    await test_whatsapp_ertiga()
    await asyncio.sleep(2)

    await test_email_flow()
    await asyncio.sleep(2)

    await verify_logs(start_log_offset)

    # ── Summary ──
    print("\n" + "="*60)
    print("  TEST SUMMARY")
    print("="*60)
    passed = [r for r in results if r["passed"]]
    failed = [r for r in results if not r["passed"]]
    print(f"  {PASS} Passed: {len(passed)}/{len(results)}")
    print(f"  {FAIL} Failed: {len(failed)}/{len(results)}")
    if failed:
        print("\n  Failed Tests:")
        for r in failed:
            print(f"    {FAIL} {r['name']}: {r['detail']}")
    print("="*60 + "\n")

    return len(failed) == 0


if __name__ == "__main__":
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
