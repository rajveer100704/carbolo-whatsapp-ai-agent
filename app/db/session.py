import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool
from sqlalchemy import event
from app.db.models import Base

# Create data directory if it doesn't exist
DATABASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
os.makedirs(DATABASE_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(DATABASE_DIR, 'carbolo.db')}"

# NullPool: opens a fresh connection per session — ensures transaction isolation.
# Combined with WAL mode and busy_timeout, this is the robust setup for SQLite.
engine = create_async_engine(
    DATABASE_URL,
    connect_args={
        "check_same_thread": False,
    },
    poolclass=NullPool,
)

# Enable WAL mode on every new connection so readers never block writers
@event.listens_for(engine.sync_engine, "connect")
def set_wal_mode(dbapi_conn, connection_record):
    cursor = dbapi_conn.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute("PRAGMA busy_timeout=30000")  # 30s wait before SQLITE_BUSY error
    cursor.close()

async_session_factory = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def init_db():
    """Initializes the database tables."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

async def get_db_session() -> AsyncSession:
    """Async generator to yield database sessions."""
    async with async_session_factory() as session:
        try:
            yield session
        finally:
            await session.close()
