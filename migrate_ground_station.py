"""
Migration: Add ground_station_enabled to User table
Run once on Railway with: python migrate_ground_station.py
"""

from database import engine
from sqlalchemy import text

def migrate():
    with engine.connect() as conn:
        # Add ground_station_enabled column if it doesn't exist
        try:
            conn.execute(text("""
                ALTER TABLE users 
                ADD COLUMN IF NOT EXISTS ground_station_enabled BOOLEAN DEFAULT FALSE;
            """))
            conn.commit()
            print("✅ Migration complete — ground_station_enabled column added")
        except Exception as e:
            print(f"Migration error (column may already exist): {e}")

if __name__ == "__main__":
    migrate()
