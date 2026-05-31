# CarBOLO: Pre-Interview Manual Validation & Demo Guide

This guide walks you through verifying both the WhatsApp and Gmail services locally, preparing for deployment, and executing a flawless live demo for your interviewer to showcase the multi-channel RAG and FSM architecture.

---

## 🛠️ Step 0: Pre-Flight Check (Credentials & Tokens)

Before you boot up any services, ensure your `.env` contains valid credentials:

1. **Meta Temporary Access Token**: 
   - Go to your [Meta Developer Dashboard](https://developers.facebook.com/).
   - Copy a fresh **Temporary Access Token** (valid for 24 hours).
   - Update `WHATSAPP_ACCESS_TOKEN` in your `.env` file.
2. **Gmail App Password**:
   - Ensure `GMAIL_ADDRESS` is `rajveer19255@gmail.com`.
   - Ensure `GMAIL_APP_PASSWORD` is `eyjrrhmgyizjkmmp` (App password with Mail access enabled).
3. **Google Calendar service account**:
   - Verify `service_account.json` exists in the root directory.

---

## 🚀 Phase 1: Local Server Setup (Terminal 1)

1. Open your terminal at `c:\Users\BIT\CarBOLO`.
2. Activate your virtual environment if you use one.
3. Start the FastAPI server (without `--reload` to prevent uvicorn reload storms from log writes):
   ```bash
   python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
   ```
4. Verify the startup logs:
   - Check that it prints `Initializing database tables...`
   - Check that it prints `Starting reminder scheduler...`
   - Check that it prints `Starting Gmail worker...` and `EmailWorker background task started.`

---

## 🌐 Phase 2: Local Tunnel Setup (Terminal 2)

1. Open a new terminal tab/window.
2. Run `localtunnel` to expose port 8000 to the internet:
   ```bash
   lt --port 8000
   ```
3. Copy the generated URL (e.g., `https://xxxxxx.loca.lt`).
4. **Test Health check**: Open `https://xxxxxx.loca.lt/` in your browser. It should return:
   ```json
   {"status":"healthy","app":"CarBOLO WhatsApp Agent","version":"1.0.3-debug-routing"}
   ```

---

## 🔗 Phase 3: Connect WhatsApp Webhook

1. Go to your **Meta Developer Dashboard** -> **WhatsApp** -> **Configuration**.
2. Click **Edit** next to Webhooks.
3. **Callback URL**: Paste `https://xxxxxx.loca.lt/webhook` (ensure `/webhook` is appended).
4. **Verify Token**: Copy the token string from `WHATSAPP_VERIFY_TOKEN` in your `.env` and paste it here.
5. Click **Verify and Save**.
6. Ensure **Webhooks fields** has `messages` subscribed.

---

## 💬 Phase 4: WhatsApp Manual Validation

Grab your test phone and send messages to the WhatsApp business test number:

### Test 1: Grounded Specifications (RAG)
1. **Send**: `Hi, Brezza VXi me sunroof hai kya?`
   - *Expected Reply*: The agent tells you Brezza VXi variant does not have a sunroof, but the ZXi+ variant does.
2. **Send**: `Does Brezza VXi have ADAS?`
   - *Expected Reply*: *“I don't have that information in the dealership knowledge base.”* (Verifies the grounded guardrail intercepting ungrounded spec claims).

### Test 2: Full Booking FSM Flow
1. **Send**: `I want to book a Ertiga test drive tomorrow`
   - *Expected Reply*: Prompts you to choose a variant (`VXi` or `ZXi+`).
2. **Send**: `VXi`
   - *Expected Reply*: Prompts for approximate budget.
3. **Send**: `12 lakhs`
   - *Expected Reply*: Prompts for purchase timeline.
4. **Send**: `immediate`
   - *Expected Reply*: Prompts for fuel preference.
5. **Send**: `petrol`
   - *Expected Reply*: Renders slot selection buttons.
6. **Select a slot** (e.g., option 1).
   - *Expected Reply*: Prompts with selection details and asks to reply with `Confirm`.
7. **Send**: `Confirm`
   - *Expected Reply*: *“Done ✅ Test drive booked...”*

### Test 3: Verification of Database & Calendar
- **SQLite Check**: Open `data/carbolo.db` in a SQLite viewer (or run python queries). Verify a row exists in `bookings` with `channel = 'whatsapp'` and `status = 'COMPLETED'`.
- **Google Calendar**: Check your Google Calendar to confirm the test drive event was added.
- **APScheduler**: Check terminal logs for: `Scheduled job reminder_<id> to run at...` (confirming two reminders are pending).

### Test 4: Rescheduling & Cancellation
1. **Send**: `reschedule booking`
   - *Expected Reply*: Confirms cancellation of the old slot and presents a new set of slots.
   - *Verification*: Old calendar event is deleted, old reminders are cancelled, and the database status updates to `RESCHEDULED`.
2. **Send**: `cancel booking`
   - *Expected Reply*: Confirms cancellation and stops reminders.
   - *Verification*: Event is removed from Google Calendar, reminders are removed, database status is `CANCELLED`.

---

## ✉️ Phase 5: Gmail Manual Validation

Now verify the Gmail transport channel:

1. Open your personal email client logged into `carboloagent@gmail.com` (which is the authorized sender list address).
2. Compose an email to the bot's address `rajveer19255@gmail.com`.
3. **Email 1 (Booking Init)**:
   - **Subject**: `Test Drive Request`
   - **Body**: `Hi, I want to book a Swift ZXi+ test drive tomorrow. Budget is 12 lakhs. Fuel petrol. Thanks!`
4. **Verify Terminal logs**:
   - Within 5 seconds, you should see: `EmailWorker: Processing incoming email from carboloagent@gmail.com`.
5. **Check Inbox**:
   - You should receive an email reply from `rajveer19255@gmail.com` displaying the slots formatted in a **clean line-by-line numbered list**:
     ```
     1. Mon 10:00 AM
     2. Mon 10:30 AM
     3. Mon 11:00 AM
     
     Please reply with the slot number (e.g. 1, 2, or 3) to choose your time.
     ```
6. **Email 2 (Slot Selection)**:
   - Reply to the thread with just the number: `1`
   - *Expected Reply*: You receive a confirmation email: `You selected Maruti Swift (ZXi+) on... Reply with 'Confirm' to book...`
7. **Email 3 (Confirm)**:
   - Reply to the thread with: `Confirm`
   - *Expected Reply*: You receive: `Done ✅ Test drive booked...`
   - *Verification*: Confirm in SQLite that a booking exists with `channel = 'gmail'`.

---

## 🛡️ Phase 6: Concurrency & Spam Protection

1. **Spam Check**:
   - Send an email to `rajveer19255@gmail.com` from a personal account *other* than `carboloagent@gmail.com`.
   - *Expected Result*: The bot ignores it. Terminal logs print: `Ignoring unauthorized sender: <email>`.
2. **Spam Header Check**:
   - Send an email from `carboloagent@gmail.com` with the subject `LinkedIn Job Alert` or `Delivery Status Notification`.
   - *Expected Result*: The bot ignores it. Terminal logs print: `Ignoring automated/spam email from...`.
3. **Race Condition Check (Lock Validation)**:
   - On WhatsApp, send `Hi`, `features of brezza`, and `book test drive` in rapid succession (within 2 seconds).
   - *Expected Result*: The server serializes these calls sequentially using our new `asyncio.Lock` fix. Look at your server logs; there should be **no** `StaleDataError`, `database is locked`, or transaction rollback errors!

---

## ☁️ Phase 7: Deploying to Render

Once all local checks pass, deploy to production:

1. **Deploy to Render**:
   - Link your GitHub repo to a new **Web Service** on Render.
   - Set Build Command: `pip install -r requirements.txt`
   - Set Start Command: `python -m uvicorn app.main:app --host 0.0.0.0 --port $PORT`
2. **Environment Variables**:
   - Add all env variables from your `.env` (excluding HOST/PORT).
   - Double-check that your `WHATSAPP_ACCESS_TOKEN` is a fresh token (not expired).
3. **Redirect Meta Webhook**:
   - Once deployed, copy your Render app URL (e.g. `https://carbolo-agent.onrender.com`).
   - Go to Meta Configuration and update the callback URL to: `https://carbolo-agent.onrender.com/webhook`.
   - Perform a quick smoke test on WhatsApp by sending `Hi` to ensure the live server processes it.

---

## 🏆 The Interview Presentation Script

When demonstrating in front of the interviewer, keep it structured to highlight the engineering design rather than just clicking buttons:

### Demo 1: Grounded Specs RAG (WhatsApp)
- **Action**: Ask a spec query (e.g., `Does Brezza VXi have sunroof?`).
- **Talking Point**: *"The model retrieved the spec catalog JSON database, found that only ZXi+ has a sunroof, and correctly informed us. If we ask about ADAS (which isn't in the spec JSON), our custom post-processing guardrail intercepts it to prevent hallucination, replying with a clean refusal."*

### Demo 2: Conversational FSM (WhatsApp)
- **Action**: Book a test drive. Complete variant, budget, timeline, and fuel. Choose a slot and type `Confirm`.
- **Talking Point**: *"This uses a Finite State Machine (FSM) implemented in SQLAlchemy models. If the user interrupts mid-qualification with a specification question, the agent answers the question using RAG and automatically resumes the booking FSM at the correct step."*

### Demo 3: Calendar & SQLite Verification (API Integration)
- **Action**: Open Google Calendar and show the newly created event live. Show the bookings table database row.
- **Talking Point**: *"When a booking is confirmed, it runs transactional write serialization to prevent double bookings, reserves the calendar slot, and writes reminder tasks to our database scheduler. On system restarts, APScheduler reloads pending reminder triggers from SQLite to guarantee restart-safe delivery."*

### Demo 4: Multi-Channel Gmail Integration (The WoW Factor)
- **Action**: Send a booking request email from `carboloagent@gmail.com` to `rajveer19255@gmail.com`. Show the formatted line-by-line slots reply, reply with `1`, and then `Confirm`.
- **Talking Point**: *"We use a unified processing engine (`CentralAgentService`). Both the WhatsApp webhook and the background Gmail IMAP polling worker route their text payloads through the same engine. The state machine dynamically detects the channel—sending interactive buttons to WhatsApp, and formatting slots into numbered lines for email. We've also resolved SQLite write lock contentions by using NullPool and SQLite WAL mode."*
