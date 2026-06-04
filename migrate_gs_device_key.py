"""
Migration: Add gs_device_key to User table
Run once on Railway with: python migrate_gs_device_key.py
"""

from database import engine
from sqlalchemy import text

def migrate():
    with engine.connect() as conn:
        try:
            conn.execute(text("""
                ALTER TABLE users
                ADD COLUMN IF NOT EXISTS gs_device_key VARCHAR(64) UNIQUE;
            """))
            conn.commit()
            print("Migration complete — gs_device_key column added")
        except Exception as e:
            print(f"Migration error: {e}")

if __name__ == "__main__":
    migrate()
