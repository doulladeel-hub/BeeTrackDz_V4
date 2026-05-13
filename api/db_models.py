"""
db_models.py — SQLAlchemy ORM table definitions.

All four tables that were previously created with raw SQL are declared
here as mapped classes.  The engine / session factory lives in db.py.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Index,
    Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


# ── sensor_data ──────────────────────────────────────────────────────────────

class SensorDataRow(Base):
    __tablename__ = "sensor_data"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    hive_id     = Column(String,  nullable=False)
    timestamp   = Column(String,  nullable=False)   # ISO-8601 text (kept as-is for compat)
    temperature = Column(Float)
    humidity    = Column(Float)
    weight      = Column(Float)
    battery     = Column(Integer)
    sound       = Column(Integer)
    swarm       = Column(Integer, default=0)         # 0 / 1
    latitude    = Column(Float)
    longitude   = Column(Float)

    __table_args__ = (
        Index("idx_hive_timestamp", "hive_id", "timestamp"),
    )


# ── users ─────────────────────────────────────────────────────────────────────

class UserRow(Base):
    __tablename__ = "users"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    username   = Column(String,  nullable=False, unique=True)
    password   = Column(String,  nullable=False)
    role       = Column(String,  nullable=False, default="viewer")
    created_at = Column(String,  nullable=False,
                        default=lambda: datetime.utcnow().isoformat())
    last_login = Column(String)

    __table_args__ = (UniqueConstraint("username"),)


# ── sessions ──────────────────────────────────────────────────────────────────

class SessionRow(Base):
    __tablename__ = "sessions"

    token      = Column(String, primary_key=True)
    username   = Column(String, nullable=False)
    expires_at = Column(String, nullable=False)   # ISO-8601 text


# ── notification_settings ─────────────────────────────────────────────────────

class NotificationSettingRow(Base):
    __tablename__ = "notification_settings"

    username = Column(String,  nullable=False, primary_key=True)
    channel  = Column(String,  nullable=False, primary_key=True)
    enabled  = Column(Integer, nullable=False, default=0)
    config   = Column(Text)                           # JSON blob