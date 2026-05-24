import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from app.db.models import Base

# Create data directory if it doesn't exist
DATABASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "data"))
os.makedirs(DATABASE_DIR, exist_ok=True)

DATABASE_URL = f"sqlite+aiosqlite:///{os.path.join(DATABASE_DIR, 'carbolo.db')}"

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},  # Needed for SQLite multithreading
)

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
