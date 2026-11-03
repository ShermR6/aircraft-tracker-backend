"""
Migration: Add gs_lat / gs_lon to User table.

The ground station's own position, which is not the airport position. Run
once on Railway with: python migrate_gs_location.py
"""

from database import engine
from sqlalchemy import text


def migrate():
    with engine.connect() as conn:
        try:
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS gs_lat DOUBLE PRECISION,
                ADD COLUMN IF NOT EXISTS gs_lon DOUBLE PRECISION;
            """))
            conn.commit()
            print("Migration complete - gs_lat / gs_lon columns added")
        except Exception as e:
            print(f"Migration error: {e}")


if __name__ == "__main__":
    migrate()
