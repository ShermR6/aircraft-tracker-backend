#!/usr/bin/env python3
"""Add sdr_range_nm and sdr_range_updated_at columns to airport_configs."""
from database import engine
from sqlalchemy import text

with engine.connect() as conn:
    conn.execute(text("""
        ALTER TABLE airport_configs
        ADD COLUMN IF NOT EXISTS sdr_range_nm JSONB,
        ADD COLUMN IF NOT EXISTS sdr_range_updated_at TIMESTAMP;
    """))
    conn.commit()
    print("Migration complete.")
