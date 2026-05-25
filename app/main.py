import os
import logging
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, Response
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configure logging
log_file = "app.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(log_file, mode="a", encoding="utf-8")
    ]
)
logger = logging.getLogger(__name__)

from app.db.session import init_db
from app.scheduler.reminders import ReminderScheduler, load_and_schedule_pending_reminders
from app.whatsapp.webhook import router as webhook_router

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup tasks
    logger.info("Initializing database tables...")
    await init_db()
    
    logger.info("Starting reminder scheduler...")
    ReminderScheduler.start()
    
    # Reload and schedule pending reminders to survive server restart
    await load_and_schedule_pending_reminders()
    
    yield
    
    # Shutdown tasks
    logger.info("Shutting down reminder scheduler...")
    ReminderScheduler.stop()

app = FastAPI(
    title="CarBOLO WhatsApp Test Drive Agent",
    version="1.0.0",
    lifespan=lifespan
)

# Register routes
app.include_router(webhook_router)

@app.get("/")
def health_check():
    """Simple status check endpoint."""
    return {
        "status": "healthy",
        "app": "CarBOLO WhatsApp Agent",
        "version": "1.0.3-debug-routing"
    }

@app.get("/logs")
def get_logs():
    if os.path.exists("app.log"):
        with open("app.log", "r", encoding="utf-8") as f:
            return Response(content=f.read(), media_type="text/plain")
    return Response(content="No logs found", media_type="text/plain")

if __name__ == "__main__":
    # Get port and host from env
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")
    uvicorn.run("app.main:app", host=host, port=port, reload=False)
