"""
Database Configuration
SQLAlchemy setup for PostgreSQL
"""

from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import os

# Database URL from environment variable
# Format: postgresql://username:password@host:port/database
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://postgres:postgres@localhost:5432/aircraft_tracker"
)

# Create engine
engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=10,      # keep more connections warm so the app's parallel
    max_overflow=5,    # per-tab requests reuse them instead of paying the
    pool_recycle=600,  # ~550ms cost of establishing fresh overflow connections
    pool_timeout=30,
    connect_args={
        "connect_timeout": 10,
        "keepalives": 1,
        "keepalives_idle": 30,
        "keepalives_interval": 10,
        "keepalives_count": 5,
    }
)

# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for models
Base = declarative_base()


def get_db():
    """
    Dependency for FastAPI routes
    Provides database session
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
