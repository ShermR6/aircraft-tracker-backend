"""
Migration: columns for the web app.

  users.routing                              per-event channel routing
  users.gs_stats                             last stats reported by the ground agent
  airport_configs.approach_runway_headings   several accepted runway headings

Runs before uvicorn on every Railway start (see railway.toml). Idempotent, and
it never raises: a failure here must not stop the API from starting. The
columns are mapped as deferred, so the app runs fine without them and only the
features that use them are unavailable.
"""

from database import engine
from sqlalchemy import text

STATEMENTS = [
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS routing JSON",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS gs_stats JSON",
    "ALTER TABLE airport_configs ADD COLUMN IF NOT EXISTS approach_runway_headings JSON",
]


def migrate():
    for stmt in STATEMENTS:
        try:
            with engine.connect() as conn:
                conn.execute(text(stmt))
                conn.commit()
            print(f"Migration ok: {stmt}")
        except Exception as e:
            print(f"Migration error ({stmt}): {e}")


if __name__ == "__main__":
    migrate()
