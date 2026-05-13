"""
main.py — Smart Apiary API v4.0

Changes vs v3:
  ① All raw sqlite3 / DB_PATH queries replaced with SQLAlchemy ORM
    (users, sessions, notification_settings tables).
  ② New WebSocket endpoint  /ws/db       — streams live DB table snapshot
  ③ New WebSocket endpoint  /ws/visitors — streams connected-client count
  ④ Import of DB_PATH removed from config (engine lives in db.py now).
  ⑤ init_database() now creates ALL four tables via Base.metadata.create_all().
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import (
    Depends, FastAPI, Header, HTTPException, Query,
    WebSocket, WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import text as sa_text
from sqlalchemy.orm import Session as OrmSession

from test.BeeTrackDz.api.config import (
    CORS_ORIGINS, IOT_API_KEY, LOG_LEVEL,
    SECRET_SALT, SERVER_HOST, SERVER_PORT, TOKEN_TTL_HOURS,
    TELEGRAM_BOT_TOKEN, TWILIO_FROM, TWILIO_SID, TWILIO_TOKEN,
    SMTP_HOST, SMTP_PASS, SMTP_PORT, SMTP_USER,
    _warn_defaults,
)
from test.BeeTrackDz.api.db import (
    Session as DBSession,          # scoped-session factory
    _engine,                       # SQLAlchemy engine (for raw-SQL fallback)
    check_db_health, get_all_latest_data, get_history_data,
    get_latest_data, init_database, write_sensor_data,
)
from test.BeeTrackDz.api.db_models import (
    Base, NotificationSettingRow, SensorDataRow, SessionRow, UserRow,
)
from test.BeeTrackDz.api.models import SensorDataIn

import os as _os

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger(__name__)

_warn_defaults()

IS_SERVERLESS: bool = bool(
    _os.getenv("VERCEL") or _os.getenv("AWS_LAMBDA_FUNCTION_NAME")
)

BASE_DIR = Path(__file__).resolve().parent


# ═══════════════════════════════════════════════════════════════════════════
# PYDANTIC REQUEST / RESPONSE MODELS
# ═══════════════════════════════════════════════════════════════════════════

class LoginRequest(BaseModel):
    username: str
    password: str


class SignupRequest(BaseModel):
    username: str
    password: str
    confirm_password: str


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class CreateUserRequest(BaseModel):
    username: str
    password: str
    role: str = "viewer"


class NotifConfigRequest(BaseModel):
    channel: str          # "email" | "telegram" | "sms"
    enabled: bool
    address:  Optional[str] = None
    chat_id:  Optional[str] = None
    phone:    Optional[str] = None


# ═══════════════════════════════════════════════════════════════════════════
# AUTH HELPERS  (all ORM-based)
# ═══════════════════════════════════════════════════════════════════════════

def hash_password(password: str) -> str:
    salted = f"{SECRET_SALT}:{password}"
    return hashlib.sha256(salted.encode()).hexdigest()


# ── Sessions ─────────────────────────────────────────────────────────────────

def create_token(username: str) -> str:
    token = secrets.token_hex(32)
    expires = (datetime.utcnow() + timedelta(hours=TOKEN_TTL_HOURS)).isoformat()
    session = DBSession()
    try:
        # Prune expired tokens opportunistically
        session.query(SessionRow).filter(
            SessionRow.expires_at < datetime.utcnow().isoformat()
        ).delete(synchronize_session=False)
        session.add(SessionRow(token=token, username=username, expires_at=expires))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        DBSession.remove()
    return token


def validate_token(token: str) -> Optional[str]:
    if not token:
        return None
    session = DBSession()
    try:
        now = datetime.utcnow().isoformat()
        row = (
            session.query(SessionRow)
            .filter(SessionRow.token == token, SessionRow.expires_at > now)
            .first()
        )
        return row.username if row else None
    finally:
        DBSession.remove()


def revoke_token(token: str) -> None:
    session = DBSession()
    try:
        session.query(SessionRow).filter(SessionRow.token == token).delete()
        session.commit()
    except Exception:
        session.rollback()
    finally:
        DBSession.remove()


# ── Users ─────────────────────────────────────────────────────────────────────

def _init_default_admin() -> None:
    """Insert admin/admin if the users table is empty."""
    session = DBSession()
    try:
        exists = session.query(UserRow).filter(UserRow.username == "admin").first()
        if not exists:
            session.add(
                UserRow(
                    username="admin",
                    password=hash_password("admin"),
                    role="admin",
                    created_at=datetime.utcnow().isoformat(),
                )
            )
            session.commit()
            logger.info("Default admin created — change the password!")
    except Exception:
        session.rollback()
    finally:
        DBSession.remove()


def get_user(username: str) -> Optional[dict]:
    session = DBSession()
    try:
        row = session.query(UserRow).filter(UserRow.username == username).first()
        if not row:
            return None
        return {
            "id":         row.id,
            "username":   row.username,
            "password":   row.password,
            "role":       row.role,
            "created_at": row.created_at,
            "last_login": row.last_login,
        }
    finally:
        DBSession.remove()


def update_last_login(username: str) -> None:
    session = DBSession()
    try:
        session.query(UserRow).filter(UserRow.username == username).update(
            {"last_login": datetime.utcnow().isoformat()}
        )
        session.commit()
    except Exception:
        session.rollback()
    finally:
        DBSession.remove()


# ── Notification settings ─────────────────────────────────────────────────────

def get_notif_settings(username: str) -> dict:
    session = DBSession()
    try:
        rows = (
            session.query(NotificationSettingRow)
            .filter(NotificationSettingRow.username == username)
            .all()
        )
        result = {}
        for r in rows:
            cfg = json.loads(r.config or "{}")
            result[r.channel] = {"enabled": bool(r.enabled), **cfg}
        return result
    finally:
        DBSession.remove()


def save_notif_setting(
    username: str, channel: str, enabled: bool, config: dict
) -> None:
    session = DBSession()
    try:
        row = (
            session.query(NotificationSettingRow)
            .filter(
                NotificationSettingRow.username == username,
                NotificationSettingRow.channel == channel,
            )
            .first()
        )
        if row:
            row.enabled = 1 if enabled else 0
            row.config  = json.dumps(config)
        else:
            session.add(
                NotificationSettingRow(
                    username=username,
                    channel=channel,
                    enabled=1 if enabled else 0,
                    config=json.dumps(config),
                )
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        DBSession.remove()


def get_all_notif_with_channel() -> list[tuple[str, str, dict]]:
    """Return [(username, channel, config_dict)] for all enabled settings."""
    session = DBSession()
    try:
        rows = (
            session.query(NotificationSettingRow)
            .filter(NotificationSettingRow.enabled == 1)
            .all()
        )
        return [
            (r.username, r.channel, json.loads(r.config or "{}"))
            for r in rows
        ]
    finally:
        DBSession.remove()


# ═══════════════════════════════════════════════════════════════════════════
# SIMPLE IN-PROCESS RATE LIMITER
# ═══════════════════════════════════════════════════════════════════════════

_rate_store: dict[str, list[float]] = defaultdict(list)
_RATE_WINDOW = 60
_RATE_LIMIT  = 10


def _check_rate_limit(ip: str) -> None:
    now = time.monotonic()
    _rate_store[ip] = [t for t in _rate_store[ip] if now - t < _RATE_WINDOW]
    if len(_rate_store[ip]) >= _RATE_LIMIT:
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")
    _rate_store[ip].append(now)


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET MANAGER  (hive data)
# ═══════════════════════════════════════════════════════════════════════════

class ConnectionManager:
    def __init__(self) -> None:
        self.active: list[WebSocket] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket) -> None:
        async with self._lock:
            self.active.append(ws)
        logger.info("WS connected. Total: %d", len(self.active))

    async def disconnect(self, ws: WebSocket) -> None:
        async with self._lock:
            try:
                self.active.remove(ws)
            except ValueError:
                pass
        logger.info("WS disconnected. Total: %d", len(self.active))

    async def broadcast(self, data: dict) -> None:
        if not self.active:
            return
        message = json.dumps(data, default=str)
        dead: list[WebSocket] = []
        for ws in list(self.active):
            try:
                await ws.send_text(message)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self.active)


ws_manager = ConnectionManager()


# ═══════════════════════════════════════════════════════════════════════════
# VISITORS WEBSOCKET MANAGER  — tracks all authenticated WS connections
# ═══════════════════════════════════════════════════════════════════════════

class VisitorManager:
    """Tracks every authenticated WS connection (hive + db + visitors sockets)."""

    def __init__(self) -> None:
        self._sockets: list[tuple[WebSocket, str]] = []   # (ws, username)
        self._listeners: list[WebSocket] = []              # /ws/visitors subscribers
        self._lock = asyncio.Lock()

    async def register(self, ws: WebSocket, username: str) -> None:
        async with self._lock:
            self._sockets.append((ws, username))
        await self._notify_listeners()

    async def unregister(self, ws: WebSocket) -> None:
        async with self._lock:
            self._sockets = [(s, u) for s, u in self._sockets if s is not ws]
        await self._notify_listeners()

    async def add_listener(self, ws: WebSocket) -> None:
        async with self._lock:
            self._listeners.append(ws)

    async def remove_listener(self, ws: WebSocket) -> None:
        async with self._lock:
            try:
                self._listeners.remove(ws)
            except ValueError:
                pass

    async def _notify_listeners(self) -> None:
        payload = json.dumps(self._snapshot(), default=str)
        dead = []
        for ws in list(self._listeners):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.remove_listener(ws)

    def _snapshot(self) -> dict:
        return {
            "type":    "visitors_update",
            "count":   len(self._sockets),
            "clients": [
                {"username": u, "connected_at": datetime.utcnow().isoformat()}
                for _, u in self._sockets
            ],
            "timestamp": datetime.utcnow().isoformat(),
        }

    @property
    def count(self) -> int:
        return len(self._sockets)


visitor_manager = VisitorManager()


# ═══════════════════════════════════════════════════════════════════════════
# FASTAPI DEPENDENCIES
# ═══════════════════════════════════════════════════════════════════════════

security = HTTPBearer(auto_error=False)


def require_auth(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing authorization token")
    username = validate_token(credentials.credentials)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return username


def require_admin(username: str = Depends(require_auth)) -> str:
    user = get_user(username)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return username


def require_iot_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    if IOT_API_KEY and x_api_key != IOT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing IoT API key")


# ═══════════════════════════════════════════════════════════════════════════
# PERIODIC TASKS
# ═══════════════════════════════════════════════════════════════════════════

async def _broadcast_hive_update() -> None:
    try:
        hives = get_all_latest_data()
        await ws_manager.broadcast({
            "type":      "hive_update",
            "data":      hives,
            "timestamp": datetime.utcnow().isoformat(),
        })
    except Exception as exc:
        logger.error("WS broadcast error: %s", exc)


async def _periodic_broadcaster() -> None:
    while True:
        await asyncio.sleep(5)
        if ws_manager.count > 0:
            await _broadcast_hive_update()


# ═══════════════════════════════════════════════════════════════════════════
# LIFESPAN
# ═══════════════════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    broadcaster_task = None
    try:
        logger.info("━━━━━━━━━━ Smart Apiary v4.0 startup ━━━━━━━━━━")
        # init_database() now creates ALL four ORM tables
        init_database()
        _init_default_admin()

        if not IS_SERVERLESS:
            try:
                from test.BeeTrackDz.api.mqtt_service import start_mqtt_client
                start_mqtt_client()
                logger.info("MQTT client started.")
            except Exception as exc:
                logger.error("MQTT startup error (non-fatal): %s", exc)
            broadcaster_task = asyncio.create_task(_periodic_broadcaster())
            logger.info("WebSocket broadcaster started.")
        else:
            logger.info("Serverless mode — MQTT + broadcaster skipped.")

        logger.info("━━━━━━━━━━ Startup complete ━━━━━━━━━━")
        yield

    except Exception as e:
        logger.exception("STARTUP FAILED: %s", e)
        raise
    finally:
        if broadcaster_task:
            broadcaster_task.cancel()
        if not IS_SERVERLESS:
            try:
                from test.BeeTrackDz.api.mqtt_service import stop_mqtt_client
                stop_mqtt_client()
            except Exception:
                pass
        logger.info("Smart Apiary shut down cleanly.")


# ═══════════════════════════════════════════════════════════════════════════
# APP
# ═══════════════════════════════════════════════════════════════════════════

app = FastAPI(
    title="Smart Apiary API",
    version="4.0",
    description="IoT beehive monitoring backend",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ═══════════════════════════════════════════════════════════════════════════
# STATIC
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def serve_dashboard() -> HTMLResponse:
    html_path = BASE_DIR / "index.html"
    try:
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return HTMLResponse(content="<h1>index.html not found</h1>", status_code=404)


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET — hive data  (/ws)
# ═══════════════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()

    token    = ws.query_params.get("token")
    username = validate_token(token) if token else None

    if not username:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await ws_manager.connect(ws)
    await visitor_manager.register(ws, username)

    try:
        hives = get_all_latest_data()
        await ws.send_text(json.dumps({
            "type":      "hive_update",
            "data":      hives,
            "timestamp": datetime.utcnow().isoformat(),
        }, default=str))

        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=30.0)
                if msg == "ping":
                    await ws.send_text(json.dumps({
                        "type":      "pong",
                        "timestamp": datetime.utcnow().isoformat(),
                    }))
            except asyncio.TimeoutError:
                try:
                    await ws.send_text(json.dumps({
                        "type":      "heartbeat",
                        "timestamp": datetime.utcnow().isoformat(),
                    }))
                except Exception:
                    break
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        await ws_manager.disconnect(ws)
        await visitor_manager.unregister(ws)


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET — live DB viewer  (/ws/db)
# ═══════════════════════════════════════════════════════════════════════════

def _db_snapshot() -> dict:
    """
    Return a snapshot of every ORM-mapped table:
      { table_name: { columns: [...], rows: [...] } }
    Only the 200 most-recent sensor_data rows are included to keep
    the payload manageable.
    """
    session = DBSession()
    try:
        snapshot: dict = {}

        # ── sensor_data (latest 200) ──────────────────────────────────────
        sensor_rows = (
            session.query(SensorDataRow)
            .order_by(SensorDataRow.id.desc())
            .limit(200)
            .all()
        )
        snapshot["sensor_data"] = {
            "columns": [
                "id", "hive_id", "timestamp", "temperature", "humidity",
                "weight", "battery", "sound", "swarm", "latitude", "longitude",
            ],
            "rows": [
                [
                    r.id, r.hive_id, r.timestamp, r.temperature, r.humidity,
                    r.weight, r.battery, r.sound, r.swarm, r.latitude, r.longitude,
                ]
                for r in sensor_rows
            ],
        }

        # ── users ─────────────────────────────────────────────────────────
        user_rows = session.query(UserRow).order_by(UserRow.id).all()
        snapshot["users"] = {
            "columns": ["id", "username", "role", "created_at", "last_login"],
            "rows": [
                [r.id, r.username, r.role, r.created_at, r.last_login]
                for r in user_rows
            ],
        }

        # ── sessions ──────────────────────────────────────────────────────
        session_rows = session.query(SessionRow).all()
        snapshot["sessions"] = {
            "columns": ["token_preview", "username", "expires_at"],
            "rows": [
                [r.token[:12] + "…", r.username, r.expires_at]
                for r in session_rows
            ],
        }

        # ── notification_settings ─────────────────────────────────────────
        notif_rows = session.query(NotificationSettingRow).all()
        snapshot["notification_settings"] = {
            "columns": ["username", "channel", "enabled", "config"],
            "rows": [
                [r.username, r.channel, bool(r.enabled), r.config]
                for r in notif_rows
            ],
        }

        return snapshot
    finally:
        DBSession.remove()


@app.websocket("/ws/db")
async def ws_db_viewer(ws: WebSocket) -> None:
    """
    Authenticated WebSocket that streams the full DB snapshot on connect,
    then pushes an updated snapshot every 10 seconds.
    Clients can also send "refresh" to request an immediate snapshot.
    Admin-only.
    """
    await ws.accept()

    token    = ws.query_params.get("token")
    username = validate_token(token) if token else None

    if not username:
        await ws.close(code=4001, reason="Unauthorized")
        return

    user = get_user(username)
    if not user or user["role"] != "admin":
        await ws.close(code=4003, reason="Admin access required")
        return

    await visitor_manager.register(ws, username)

    async def _send_snapshot() -> None:
        payload = json.dumps(
            {"type": "db_snapshot", "data": _db_snapshot(),
             "timestamp": datetime.utcnow().isoformat()},
            default=str,
        )
        await ws.send_text(payload)

    try:
        await _send_snapshot()

        while True:
            try:
                msg = await asyncio.wait_for(ws.receive_text(), timeout=10.0)
                if msg == "refresh":
                    await _send_snapshot()
            except asyncio.TimeoutError:
                # Push periodic snapshot
                await _send_snapshot()
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        await visitor_manager.unregister(ws)


# ═══════════════════════════════════════════════════════════════════════════
# WEBSOCKET — live visitors  (/ws/visitors)
# ═══════════════════════════════════════════════════════════════════════════

@app.websocket("/ws/visitors")
async def ws_visitors(ws: WebSocket) -> None:
    """
    Authenticated WebSocket that streams the current connected-client list
    whenever it changes, plus a heartbeat every 15 s.
    """
    await ws.accept()

    token    = ws.query_params.get("token")
    username = validate_token(token) if token else None

    if not username:
        await ws.close(code=4001, reason="Unauthorized")
        return

    await visitor_manager.add_listener(ws)
    await visitor_manager.register(ws, username)

    # Send immediately
    try:
        await ws.send_text(json.dumps(
            visitor_manager._snapshot(), default=str
        ))

        while True:
            try:
                await asyncio.wait_for(ws.receive_text(), timeout=15.0)
            except asyncio.TimeoutError:
                # Heartbeat keeps the connection alive
                try:
                    await ws.send_text(json.dumps({
                        "type":      "heartbeat",
                        "timestamp": datetime.utcnow().isoformat(),
                    }))
                except Exception:
                    break
            except WebSocketDisconnect:
                break
            except Exception:
                break
    finally:
        await visitor_manager.remove_listener(ws)
        await visitor_manager.unregister(ws)


# ═══════════════════════════════════════════════════════════════════════════
# AUTH ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/auth/login", tags=["auth"])
def login(body: LoginRequest, x_forwarded_for: Optional[str] = Header(default=None)):
    client_ip = x_forwarded_for or "unknown"
    _check_rate_limit(f"login:{client_ip}")

    if not body.username or not body.password:
        raise HTTPException(status_code=400, detail="Username and password required")
    user = get_user(body.username.strip())
    if not user or user["password"] != hash_password(body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")

    token = create_token(user["username"])
    update_last_login(user["username"])
    logger.info("Login: '%s'", user["username"])
    return {
        "access_token": token,
        "token_type":   "bearer",
        "username":     user["username"],
        "role":         user["role"],
        "expires_in":   TOKEN_TTL_HOURS * 3600,
    }


@app.post("/auth/signup", tags=["auth"])
def signup(body: SignupRequest, x_forwarded_for: Optional[str] = Header(default=None)):
    client_ip = x_forwarded_for or "unknown"
    _check_rate_limit(f"signup:{client_ip}")

    username = body.username.strip()
    if not username or not body.password:
        raise HTTPException(status_code=400, detail="All fields required")
    if len(username) < 3:
        raise HTTPException(status_code=400, detail="Username must be ≥ 3 characters")
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be ≥ 8 characters")
    if body.password != body.confirm_password:
        raise HTTPException(status_code=400, detail="Passwords do not match")

    session = DBSession()
    try:
        existing = session.query(UserRow).filter(UserRow.username == username).first()
        if existing:
            raise HTTPException(status_code=409, detail="Username already taken")
        session.add(
            UserRow(
                username=username,
                password=hash_password(body.password),
                role="viewer",
                created_at=datetime.utcnow().isoformat(),
            )
        )
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise HTTPException(status_code=500, detail="Could not create account")
    finally:
        DBSession.remove()

    logger.info("Signup: '%s'", username)
    return {"status": "account created", "username": username, "role": "viewer"}


@app.post("/auth/logout", tags=["auth"])
def logout(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if credentials:
        revoke_token(credentials.credentials)
    return {"status": "logged out"}


@app.get("/auth/me", tags=["auth"])
def get_me(username: str = Depends(require_auth)):
    user = get_user(username)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return {
        "username":   user["username"],
        "role":       user["role"],
        "last_login": user["last_login"],
        "created_at": user["created_at"],
    }


@app.post("/auth/change-password", tags=["auth"])
def change_password(body: ChangePasswordRequest, username: str = Depends(require_auth)):
    user = get_user(username)
    if not user or user["password"] != hash_password(body.current_password):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    if len(body.new_password) < 8:
        raise HTTPException(status_code=400, detail="New password must be ≥ 8 characters")

    session = DBSession()
    try:
        session.query(UserRow).filter(UserRow.username == username).update(
            {"password": hash_password(body.new_password)}
        )
        # Invalidate all existing sessions for this user
        session.query(SessionRow).filter(SessionRow.username == username).delete()
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        DBSession.remove()

    return {"status": "password updated"}


@app.get("/auth/users", tags=["auth"])
def list_users(admin: str = Depends(require_admin)):
    session = DBSession()
    try:
        rows = session.query(UserRow).order_by(UserRow.id).all()
        return [
            {
                "id":         r.id,
                "username":   r.username,
                "role":       r.role,
                "created_at": r.created_at,
                "last_login": r.last_login,
            }
            for r in rows
        ]
    finally:
        DBSession.remove()


@app.post("/auth/users", tags=["auth"])
def create_user(body: CreateUserRequest, admin: str = Depends(require_admin)):
    if len(body.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be ≥ 8 characters")
    if body.role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="Role must be 'admin' or 'viewer'")

    session = DBSession()
    try:
        existing = session.query(UserRow).filter(
            UserRow.username == body.username.strip()
        ).first()
        if existing:
            raise HTTPException(status_code=409, detail="Username already exists")
        session.add(
            UserRow(
                username=body.username.strip(),
                password=hash_password(body.password),
                role=body.role,
                created_at=datetime.utcnow().isoformat(),
            )
        )
        session.commit()
    except HTTPException:
        session.rollback()
        raise
    except Exception:
        session.rollback()
        raise HTTPException(status_code=500, detail="Could not create user")
    finally:
        DBSession.remove()

    return {"status": "user created", "username": body.username}


@app.delete("/auth/users/{target_username}", tags=["auth"])
def delete_user(target_username: str, admin: str = Depends(require_admin)):
    if target_username == admin:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    session = DBSession()
    try:
        deleted = (
            session.query(UserRow)
            .filter(UserRow.username == target_username)
            .delete()
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        DBSession.remove()

    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return {"status": "user deleted"}


# ═══════════════════════════════════════════════════════════════════════════
# HEALTH
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/health", tags=["system"])
def health():
    db_ok = check_db_health()
    try:
        from test.BeeTrackDz.api.mqtt_service import get_mqtt_status
        mqtt_status = get_mqtt_status()
    except ImportError:
        mqtt_status = "not_configured"

    return {
        "status":            "ok" if db_ok else "degraded",
        "database":          "ok" if db_ok else "unavailable",
        "mqtt":              mqtt_status,
        "websocket_clients": ws_manager.count,
        "visitor_count":     visitor_manager.count,
        "timestamp":         datetime.utcnow().isoformat(),
    }


# ═══════════════════════════════════════════════════════════════════════════
# IOT INGESTION
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/data", tags=["iot"], dependencies=[Depends(require_iot_key)])
async def post_data(data: SensorDataIn) -> dict:
    if not write_sensor_data(data):
        raise HTTPException(status_code=500, detail="Failed to write to database")
    await _broadcast_hive_update()
    return {"status": "ok", "message": "Data saved"}


# ═══════════════════════════════════════════════════════════════════════════
# DATA ENDPOINTS
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/hives", tags=["data"])
def get_all_hives(_: str = Depends(require_auth)):
    return get_all_latest_data()


@app.get("/api/hives/{hive_id}", tags=["data"])
def get_hive(hive_id: str, _: str = Depends(require_auth)):
    data = get_latest_data(hive_id)
    if not data:
        raise HTTPException(status_code=404, detail="Hive not found")
    return data


@app.get("/data/latest/{hive_id}", tags=["data"])
def get_latest(hive_id: str, _: str = Depends(require_auth)):
    data = get_latest_data(hive_id)
    if not data:
        raise HTTPException(status_code=404, detail="No data found")
    return data


@app.get("/data/history/{hive_id}", tags=["data"])
def get_history(
    hive_id: str,
    hours: int = Query(24, ge=1, le=720),
    _: str = Depends(require_auth),
):
    return get_history_data(hive_id, hours)


@app.get("/data/all/latest", tags=["data"])
def get_all_latest(_: str = Depends(require_auth)):
    return get_all_latest_data()


# ═══════════════════════════════════════════════════════════════════════════
# API INFO
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/info", tags=["system"])
def api_info():
    return {
        "name":     "Smart Apiary API",
        "version":  "4.0",
        "features": [
            "WebSocket", "WS-DB-Viewer", "WS-Visitors",
            "Multi-user", "RBAC", "MQTT", "SQLAlchemy-ORM",
            "IoT-key-auth", "Rate-limiting", "Persistent-sessions",
        ],
    }


# ═══════════════════════════════════════════════════════════════════════════
# NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════════════════

@app.post("/api/notifications/config", tags=["notifications"])
async def set_notification_config(
    body: NotifConfigRequest,
    username: str = Depends(require_auth),
):
    if body.channel not in ("email", "telegram", "sms"):
        raise HTTPException(status_code=400, detail="Invalid channel")

    config: dict = {}
    if body.channel == "email"    and body.address:  config["address"] = body.address
    if body.channel == "telegram" and body.chat_id:  config["chat_id"] = body.chat_id
    if body.channel == "sms"      and body.phone:    config["phone"]   = body.phone

    save_notif_setting(username, body.channel, body.enabled, config)

    if body.enabled and config:
        test_msg = f"✅ Smart Apiary — {body.channel.title()} notifications enabled for {username}"
        asyncio.create_task(_send_notification(body.channel, config, test_msg))

    return {"status": "saved", "channel": body.channel, "enabled": body.enabled}


@app.get("/api/notifications/config", tags=["notifications"])
def get_notification_config(username: str = Depends(require_auth)):
    return get_notif_settings(username)


async def _send_notification(channel: str, config: dict, message: str) -> None:
    try:
        if channel == "telegram":
            await _send_telegram(config.get("chat_id", ""), message)
        elif channel == "email":
            await _send_email(config.get("address", ""), message)
        elif channel == "sms":
            await _send_sms(config.get("phone", ""), message)
    except Exception as exc:
        logger.error("Notification send error (%s): %s", channel, exc)


async def _send_telegram(chat_id: str, text: str) -> None:
    import urllib.request, urllib.parse
    bot_token = TELEGRAM_BOT_TOKEN
    if not bot_token or not chat_id:
        logger.warning("Telegram not configured (missing TELEGRAM_BOT_TOKEN or chat_id)")
        return
    url  = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode()
    req  = urllib.request.Request(url, data=data, method="POST")
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: urllib.request.urlopen(req, timeout=8))
    logger.info("Telegram sent to chat_id=%s", chat_id)


async def _send_email(address: str, text: str) -> None:
    import smtplib
    from email.mime.text import MIMEText
    if not SMTP_HOST or not SMTP_USER:
        logger.warning("Email not configured (missing SMTP_HOST / SMTP_USER)")
        return
    msg = MIMEText(text)
    msg["Subject"] = "🐝 Smart Apiary Alert"
    msg["From"]    = SMTP_USER
    msg["To"]      = address
    loop = asyncio.get_event_loop()

    def _send():
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(SMTP_USER, [address], msg.as_string())

    await loop.run_in_executor(None, _send)
    logger.info("Email sent to %s", address)


async def _send_sms(phone: str, text: str) -> None:
    if not TWILIO_SID or not TWILIO_FROM:
        logger.warning("SMS not configured (missing TWILIO_SID / TWILIO_FROM)")
        return
    try:
        from twilio.rest import Client
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: Client(TWILIO_SID, TWILIO_TOKEN).messages.create(
                body=text, from_=TWILIO_FROM, to=phone
            ),
        )
        logger.info("SMS sent to %s", phone)
    except ImportError:
        logger.warning("twilio package not installed — pip install twilio")


async def maybe_notify(hive_id: str, alert_type: str, message: str) -> None:
    try:
        for username, channel, cfg in get_all_notif_with_channel():
            full_msg = f"🐝 Apiary Alert [{hive_id}] — {alert_type}: {message}"
            asyncio.create_task(_send_notification(channel, cfg, full_msg))
    except Exception as exc:
        logger.error("maybe_notify error: %s", exc)


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    uvicorn.run(
        "api.main:app",
        host=SERVER_HOST,
        port=SERVER_PORT,
        reload=False,
        log_level=LOG_LEVEL.lower(),
    )