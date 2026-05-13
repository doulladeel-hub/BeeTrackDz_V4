"""
db.py — SQLAlchemy ORM persistence layer.

Changes vs raw-sqlite version:
  - Thread-local sqlite3 connections replaced by a single SQLAlchemy engine
    with a scoped-session factory (thread-safe, reusable).
  - All SQL queries replaced by ORM operations.
  - WAL mode is still enabled via an engine event so we keep the same
    concurrency characteristics.
  - Public API is identical to the old db.py so nothing in main.py /
    mqtt_service.py that calls these helpers needs to change its call sites.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional, List

from sqlalchemy import create_engine, text, func
from sqlalchemy.orm import sessionmaker, scoped_session

from test.BeeTrackDz.api.config import DB_PATH
from test.BeeTrackDz.api.db_models import Base, SensorDataRow
from test.BeeTrackDz.api.models import SensorDataIn

logger = logging.getLogger(__name__)

# ── Engine & session factory ─────────────────────────────────────────────────

_engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=False,
)

# Enable WAL + foreign keys for every new connection
from sqlalchemy import event as _sa_event


@_sa_event.listens_for(_engine, "connect")
def _set_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


_SessionFactory = sessionmaker(bind=_engine, autocommit=False, autoflush=False)
Session = scoped_session(_SessionFactory)   # thread-local sessions


# ── Schema init ──────────────────────────────────────────────────────────────

def init_database() -> None:
    """Create all tables (if they don't exist) and log the result."""
    Base.metadata.create_all(_engine)
    logger.info("SQLAlchemy — all tables ready (WAL mode, engine: %s).", _engine.url)


# ── Writes ───────────────────────────────────────────────────────────────────

def write_sensor_data(data: SensorDataIn, hive_id: str | None = None) -> bool:
    """Persist one sensor reading. Returns True on success."""
    session = Session()
    try:
        final_hive_id = (
            hive_id
            or data.hive_id
            or f"hive_{data.gps.latitude:.4f}_{data.gps.longitude:.4f}"
        )
        row = SensorDataRow(
            hive_id=final_hive_id,
            timestamp=datetime.utcnow().isoformat(),
            temperature=data.environment.temperature,
            humidity=data.environment.humidity,
            weight=data.hive.weight,
            battery=data.system.battery,
            sound=data.system.sound,
            swarm=1 if data.system.swarm else 0,
            latitude=data.gps.latitude,
            longitude=data.gps.longitude,
        )
        session.add(row)
        session.commit()
        logger.debug("Data saved for hive: %s", final_hive_id)
        return True
    except Exception as exc:
        session.rollback()
        logger.error("Failed to write sensor data: %s", exc)
        return False
    finally:
        Session.remove()


# ── Reads ────────────────────────────────────────────────────────────────────

def _row_to_dict(row: SensorDataRow) -> dict:
    return {
        "hive_id":   row.hive_id,
        "timestamp": row.timestamp,
        "environment": {
            "temperature": row.temperature or 0.0,
            "humidity":    row.humidity    or 0.0,
        },
        "gps": {
            "latitude":  row.latitude  or 0.0,
            "longitude": row.longitude or 0.0,
        },
        "hive":   {"weight":  row.weight  or 0.0},
        "system": {
            "battery": row.battery or 0,
            "sound":   row.sound   or 0,
            "swarm":   bool(row.swarm),
        },
    }


def get_latest_data(hive_id: str) -> Optional[dict]:
    """Latest reading for one hive, or None."""
    session = Session()
    try:
        row = (
            session.query(SensorDataRow)
            .filter(SensorDataRow.hive_id == hive_id)
            .order_by(SensorDataRow.timestamp.desc())
            .first()
        )
        return _row_to_dict(row) if row else None
    except Exception as exc:
        logger.error("get_latest_data error: %s", exc)
        return None
    finally:
        Session.remove()


def get_all_latest_data() -> List[dict]:
    """Latest reading for every hive."""
    session = Session()
    try:
        # Subquery: max timestamp per hive
        sub = (
            session.query(
                SensorDataRow.hive_id,
                func.max(SensorDataRow.timestamp).label("max_ts"),
            )
            .group_by(SensorDataRow.hive_id)
            .subquery()
        )
        rows = (
            session.query(SensorDataRow)
            .join(
                sub,
                (SensorDataRow.hive_id == sub.c.hive_id)
                & (SensorDataRow.timestamp == sub.c.max_ts),
            )
            .order_by(SensorDataRow.hive_id)
            .all()
        )
        return [_row_to_dict(r) for r in rows]
    except Exception as exc:
        logger.error("get_all_latest_data error: %s", exc)
        return []
    finally:
        Session.remove()


def get_history_data(hive_id: str, hours: int = 24) -> List[dict]:
    """Up to 500 readings for one hive within the last `hours` hours, newest first."""
    session = Session()
    try:
        cutoff = text(f"datetime('now', '-{hours} hours')")
        rows = (
            session.query(SensorDataRow)
            .filter(
                SensorDataRow.hive_id == hive_id,
                SensorDataRow.timestamp >= cutoff,
            )
            .order_by(SensorDataRow.timestamp.desc())
            .limit(500)
            .all()
        )
        return [
            {
                "timestamp":   r.timestamp,
                "temperature": r.temperature,
                "humidity":    r.humidity,
                "weight":      r.weight,
                "battery":     r.battery,
                "sound":       r.sound,
                "swarm":       bool(r.swarm),
            }
            for r in rows
        ]
    except Exception as exc:
        logger.error("get_history_data error: %s", exc)
        return []
    finally:
        Session.remove()


def check_db_health() -> bool:
    """Quick connectivity probe."""
    try:
        with _engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        return False