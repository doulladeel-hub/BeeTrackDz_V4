# 🐝 Smart Apiary API — v4.0
 
A production-grade IoT backend for monitoring beehives in real-time. Built with **FastAPI**, **SQLAlchemy ORM**, **MQTT**, and **WebSockets**, it ingests sensor telemetry from field devices, persists it in SQLite, and serves a live dashboard with multi-user authentication and multi-channel alerting.
 
---
 
## Architecture Overview
 
```
┌──────────────────────────────────────────────────────────────────┐
│                          CLIENT LAYER                            │
│   Browser / Mobile (index.html)  ||   IoT Sensors   ·   Admin    │
└────────────┬──────────────────────────┬──────────────────────────┘
             │  HTTP / WebSocket        │  MQTT (TLS)
             ▼                          ▼
┌──────────────────────┐    ┌───────────────────────┐
│   FastAPI (main.py)  │    │  MQTT Subscriber       │
│   - REST endpoints   │    │  (mqtt_service.py)     │
│   - WebSocket hub    │    │  - TLS connect         │
│   - Auth middleware  │◄───│  - Reconnect logic     │
│   - Rate limiter     │    │  - JSON → SensorDataIn │
│   - Notif dispatch   │    └───────────────────────┘
└──────────┬───────────┘
           │  SQLAlchemy ORM (scoped sessions)
           ▼
┌──────────────────────┐
│   SQLite (WAL mode)  │
│   - sensor_data      │
│   - users            │
│   - sessions         │
│   - notification_    │
│     settings         │
└──────────────────────┘
           │
           │  Outbound alerts
           ▼
   ┌───────┬────────┬──────┐
   │Telegram│ Email │  SMS │
   └───────┴────────┴──────┘
```
 
Data flows in two paths:
- **HTTP POST `/data`** — direct sensor push (API-key guarded)
- **MQTT** — broker-mediated pub/sub from field devices
Both paths converge on `write_sensor_data()` in `db.py`, which persists to SQLite and triggers a WebSocket broadcast to all connected dashboards.
 
---
 
## Project Structure
 
```
BeeTrackDz/
├── .env                    ← secrets (never commit)
├── smart_apiary.db         ← SQLite database (auto-created)
└── api/
    ├── config.py           ← centralised env-var config
    ├── db_models.py        ← SQLAlchemy ORM table definitions
    ├── db.py               ← engine, session factory, CRUD helpers
    ├── models.py           ← Pydantic request/response models
    ├── mqtt_service.py     ← MQTT subscriber & reconnect logic
    ├── main.py             ← FastAPI app, endpoints, WebSockets
    └── index.html          ← served as the root dashboard UI
```
 
---
 
## Module Reference
 
### `config.py`
 
Centralised configuration. All values are loaded from a `.env` file placed at the **project root** (one level above `/api`). The module auto-detects serverless environments (Vercel, AWS Lambda) and adjusts the database path accordingly.
 
**Key behaviours:**
- Calls `load_dotenv()` at import time, resolving the `.env` path relative to the file's location.
- Prints a warning if `.env` is missing.
- Exits with a critical log if `SECRET_SALT` is unset (prevents insecure operation).
- Warns (but does not exit) if `IOT_API_KEY` or `MQTT_BROKER` are missing.
**Exported constants:**
 
| Variable | Type | Default | Purpose |
|---|---|---|---|
| `IS_SERVERLESS` | `bool` | `False` | Switches DB path to `/tmp/` on Lambda/Vercel |
| `DB_PATH` | `str` | `smart_apiary.db` | SQLite file path |
| `SECRET_SALT` | `str` | — | Pepper for password hashing (required) |
| `TOKEN_TTL_HOURS` | `int` | `8` | Session token lifetime |
| `IOT_API_KEY` | `str` | — | Pre-shared key for IoT ingestion |
| `MQTT_BROKER` | `str` | — | MQTT broker hostname |
| `MQTT_PORT` | `int` | `8883` | MQTT port (default TLS) |
| `MQTT_USERNAME` | `str` | — | MQTT credential |
| `MQTT_PASSWORD` | `str` | — | MQTT credential |
| `MQTT_TOPIC` | `str` | `rucher/data` | Topic to subscribe to |
| `MQTT_TLS_ENABLED` | `bool` | `true` | Enable TLS for MQTT |
| `SERVER_HOST` | `str` | `0.0.0.0` | Uvicorn bind host |
| `SERVER_PORT` | `int` | `8000` | Uvicorn bind port |
| `TELEGRAM_BOT_TOKEN` | `str` | — | Telegram alert bot |
| `SMTP_HOST/PORT/USER/PASS` | various | — | Email alert SMTP |
| `TWILIO_SID/TOKEN/FROM` | `str` | — | SMS via Twilio |
| `RATE_LIMIT_GLOBAL` | `int` | `50` | Requests per window |
| `RATE_LIMIT_AUTH` | `int` | `10` | Auth requests per window |
| `RATE_WINDOW_SECS` | `int` | `60` | Rate limit rolling window |
| `CORS_ORIGINS` | `list[str]` | `["*"]` | Allowed CORS origins |
| `LOG_LEVEL` | `str` | `INFO` | Python logging level |
 
**Functions:**
 
`_warn_defaults() → None`
Validates critical configuration at startup. Exits the process if `SECRET_SALT` is empty. Logs warnings for missing `IOT_API_KEY` or `MQTT_BROKER`.
 
---
 
### `db_models.py`
 
Defines all four SQLAlchemy ORM mapped classes. Inherits from a shared `DeclarativeBase`.
 
#### `SensorDataRow` — table: `sensor_data`
 
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | Auto-increment |
| `hive_id` | String | Identifies the beehive |
| `timestamp` | String | ISO-8601 UTC text |
| `temperature` | Float | °C from environment sensor |
| `humidity` | Float | % RH |
| `weight` | Float | Kg — hive weight |
| `battery` | Integer | % battery level |
| `sound` | Integer | Audio level reading |
| `swarm` | Integer | `0`/`1` swarm detection flag |
| `latitude` | Float | GPS coordinate |
| `longitude` | Float | GPS coordinate |
 
Index: `idx_hive_timestamp` on `(hive_id, timestamp)` for fast per-hive queries.
 
#### `UserRow` — table: `users`
 
| Column | Type | Notes |
|---|---|---|
| `id` | Integer PK | |
| `username` | String UNIQUE | |
| `password` | String | SHA-256 hash with salt |
| `role` | String | `"admin"` or `"viewer"` |
| `created_at` | String | ISO-8601 |
| `last_login` | String | ISO-8601, nullable |
 
#### `SessionRow` — table: `sessions`
 
| Column | Type | Notes |
|---|---|---|
| `token` | String PK | 64-char hex secret |
| `username` | String | Owning user |
| `expires_at` | String | ISO-8601 expiry |
 
#### `NotificationSettingRow` — table: `notification_settings`
 
| Column | Type | Notes |
|---|---|---|
| `username` | String PK | Composite PK |
| `channel` | String PK | `"email"`, `"telegram"`, or `"sms"` |
| `enabled` | Integer | `0`/`1` |
| `config` | Text | JSON blob (address/chat_id/phone) |
 
---
 
### `db.py`
 
Persistence layer. Exposes a clean, public API consumed by `main.py` and `mqtt_service.py`. All raw `sqlite3` calls have been replaced by SQLAlchemy ORM operations.
 
**Engine setup:**
- `create_engine()` with `check_same_thread=False` for multi-threaded FastAPI use.
- WAL journal mode and foreign key enforcement set via an `@event.listens_for("connect")` hook on every new connection.
- `scoped_session` factory (`Session`) provides thread-local sessions automatically.
**Public functions:**
 
`init_database() → None`
Creates all four ORM tables via `Base.metadata.create_all()`. Safe to call multiple times (no-op if tables exist). Logs the engine URL on success.
 
`write_sensor_data(data: SensorDataIn, hive_id: str | None = None) → bool`
Persists one sensor reading. Auto-generates `hive_id` from GPS coordinates if none is provided. Rolls back and returns `False` on any exception.
 
`get_latest_data(hive_id: str) → Optional[dict]`
Returns the single most-recent reading for the given hive, or `None` if unknown.
 
`get_all_latest_data() → List[dict]`
Returns the latest reading for every known hive using a correlated subquery (max timestamp per hive_id group).
 
`get_history_data(hive_id: str, hours: int = 24) → List[dict]`
Returns up to 500 readings from the last `hours` hours for one hive, newest first. Supports 1–720 hour windows.
 
`check_db_health() → bool`
Executes `SELECT 1` through the engine. Returns `True` if successful, `False` otherwise.
 
**Internal helpers:**
 
`_row_to_dict(row: SensorDataRow) → dict`
Converts an ORM row into the standard nested JSON structure used by all API responses.
 
---
 
### `models.py`
 
Pydantic data models shared across the application for request validation and serialisation.
 
#### `EnvironmentData`
```python
temperature: float = 0.0   # °C
humidity:    float = 0.0   # % RH
```
 
#### `GPSData`
```python
latitude:  float = 0.0
longitude: float = 0.0
```
 
#### `HiveData`
```python
weight: float = 0.0   # kg
```
 
#### `SystemData`
```python
battery: int  = 0      # %
sound:   int  = 0      # audio level
swarm:   bool = False  # swarm detection
```
 
#### `SensorDataIn`
Top-level inbound payload. All sub-models have safe defaults so partial payloads are accepted.
```python
environment: EnvironmentData
gps:         GPSData
hive:        HiveData
system:      SystemData
hive_id:     Optional[str] = None
```
 
#### `SensorDataOut`
Outbound response model. Adds `hive_id` (required) and `timestamp` as a proper `datetime`.
 
---
 
### `mqtt_service.py`
 
Runs a persistent MQTT subscriber in the background. Receives JSON payloads from field devices and writes them to the database.
 
#### `MQTTSubscriber` class
 
Constructor initialises the paho-mqtt client with optional TLS (using the system CA bundle via `certifi`) and credentials from `config.py`. Registers three callbacks.
 
**Callbacks:**
 
`_on_connect(client, userdata, flags, rc) → None`
On `rc == 0`: marks the client as connected, resets the reconnect counter, and subscribes to `MQTT_TOPIC` at QoS 1. On failure: logs a human-readable error code.
 
`_on_disconnect(client, userdata, rc) → None`
Sets `connected = False`. If the disconnect was unexpected (`rc != 0`) and the stop event is not set, schedules a reconnect.
 
`_on_message(client, userdata, msg) → None`
Decodes the UTF-8 JSON payload, constructs a `SensorDataIn`, and calls `write_sensor_data()`. Logs success or failure. Handles `JSONDecodeError` and generic exceptions separately.
 
**Connection management:**
 
`connect() → None`
Calls `client.connect()` and starts the paho network loop in a background thread (`loop_start()`). Falls back to `_schedule_reconnect()` on failure.
 
`disconnect() → None`
Sets the stop event, stops the loop, and disconnects cleanly.
 
`_schedule_reconnect() → None`
Exponential back-off reconnect: delay = `min(60, 2 ^ attempt)` seconds. Caps at 10 attempts total. Uses a daemon `threading.Timer`.
 
**Module-level singleton & API:**
 
`start_mqtt_client() → Optional[MQTTSubscriber]`
Creates and connects the singleton subscriber.
 
`stop_mqtt_client() → None`
Disconnects and clears the singleton.
 
`get_mqtt_status() → str`
Returns `"not_configured"`, `"connected"`, or `"disconnected"`.
 
---
 
### `main.py`
 
The FastAPI application. Handles startup/shutdown, all HTTP endpoints, WebSocket hubs, auth helpers, rate limiting, and notification dispatch.
 
#### Pydantic Request Models
 
| Class | Fields | Used by |
|---|---|---|
| `LoginRequest` | `username`, `password` | `POST /auth/login` |
| `SignupRequest` | `username`, `password`, `confirm_password` | `POST /auth/signup` |
| `ChangePasswordRequest` | `current_password`, `new_password` | `POST /auth/change-password` |
| `CreateUserRequest` | `username`, `password`, `role` | `POST /auth/users` |
| `NotifConfigRequest` | `channel`, `enabled`, `address?`, `chat_id?`, `phone?` | `POST /api/notifications/config` |
 
#### Auth Helper Functions
 
`hash_password(password: str) → str`
SHA-256 hash of `"{SECRET_SALT}:{password}"`.
 
`create_token(username: str) → str`
Generates a 64-char hex token, writes it to `sessions` with an expiry timestamp, and opportunistically deletes expired sessions from the same transaction.
 
`validate_token(token: str) → Optional[str]`
Queries `sessions` for a matching, non-expired token. Returns the `username` or `None`.
 
`revoke_token(token: str) → None`
Deletes the session row (used by logout and password change).
 
`_init_default_admin() → None`
Inserts an `admin`/`admin` user on first run if the users table is empty. Logs a warning to change the password.
 
`get_user(username: str) → Optional[dict]`
Fetches a user row by username, returning a plain dict (avoids exposing ORM objects to callers).
 
`update_last_login(username: str) → None`
Updates the `last_login` timestamp for the given user.
 
`get_notif_settings(username: str) → dict`
Returns all notification channels and their config for a user, keyed by channel name.
 
`save_notif_setting(username, channel, enabled, config) → None`
Upserts a `NotificationSettingRow` (update if exists, insert otherwise).
 
`get_all_notif_with_channel() → list[tuple[str, str, dict]]`
Returns `(username, channel, config_dict)` for every enabled notification setting — used by `maybe_notify`.
 
#### FastAPI Dependencies
 
`require_auth(credentials) → str`
Bearer token guard. Extracts the token from the `Authorization` header, validates it, and returns the username. Raises `401` if missing or invalid.
 
`require_admin(username) → str`
Wraps `require_auth` and additionally checks `role == "admin"`. Raises `403` otherwise.
 
`require_iot_key(x_api_key) → None`
Checks the `X-Api-Key` header against `IOT_API_KEY`. Raises `401` on mismatch. No-op if `IOT_API_KEY` is unset (open ingestion).
 
#### Rate Limiter
 
`_check_rate_limit(ip: str) → None`
In-process sliding-window limiter. Maintains a `defaultdict` of per-IP timestamp lists. Purges entries older than 60 seconds and raises `429` if the count reaches `_RATE_LIMIT` (10). Applied to login and signup endpoints.
 
#### WebSocket Managers
 
**`ConnectionManager`** (singleton: `ws_manager`)
Manages the `/ws` hive-data channel.
 
| Method | Description |
|---|---|
| `connect(ws)` | Appends the socket to the active list (async lock) |
| `disconnect(ws)` | Removes the socket (async lock) |
| `broadcast(data: dict)` | Sends JSON to all active sockets; auto-removes dead connections |
| `count` property | Number of active connections |
 
**`VisitorManager`** (singleton: `visitor_manager`)
Tracks every authenticated WebSocket connection across all three channels and pushes presence updates to `/ws/visitors` subscribers.
 
| Method | Description |
|---|---|
| `register(ws, username)` | Adds `(ws, username)` to the tracked list; notifies listeners |
| `unregister(ws)` | Removes the socket by identity; notifies listeners |
| `add_listener(ws)` | Subscribes a socket to visitor-change events |
| `remove_listener(ws)` | Unsubscribes a socket |
| `_notify_listeners()` | Pushes the current snapshot to all listener sockets |
| `_snapshot()` | Builds `{type, count, clients, timestamp}` payload |
| `count` property | Total tracked connections |
 
#### Periodic Tasks
 
`_broadcast_hive_update() → None` (async)
Fetches all latest hive data and broadcasts a `hive_update` message via `ws_manager`. Called after every `POST /data` and every 5 seconds by the background task.
 
`_periodic_broadcaster() → None` (async)
`asyncio` task that sleeps 5 seconds and calls `_broadcast_hive_update()` if any clients are connected. Runs for the lifetime of the process.
 
#### Notification Dispatch
 
`_send_notification(channel, config, message) → None` (async)
Dispatcher. Routes to `_send_telegram`, `_send_email`, or `_send_sms` based on channel string.
 
`_send_telegram(chat_id, text) → None` (async)
Posts to the Telegram Bot API (`sendMessage`) in a thread-pool executor. Requires `TELEGRAM_BOT_TOKEN`.
 
`_send_email(address, text) → None` (async)
Sends via SMTP with STARTTLS in a thread-pool executor. Requires `SMTP_HOST` and `SMTP_USER`.
 
`_send_sms(phone, text) → None` (async)
Sends via Twilio REST API in a thread-pool executor. Requires `TWILIO_SID` and `TWILIO_FROM`. Gracefully handles missing `twilio` package.
 
`maybe_notify(hive_id, alert_type, message) → None` (async)
Iterates all enabled notification settings and dispatches an alert for each. Intended to be called from alert-detection logic.
 
#### Application Lifespan
 
`lifespan(app)` — async context manager registered with FastAPI.
 
**On startup:**
1. Calls `init_database()` to create all tables.
2. Calls `_init_default_admin()` to seed the admin account.
3. If not serverless: starts the MQTT client and the periodic WebSocket broadcaster task.
4. In serverless mode: skips MQTT and broadcaster.
**On shutdown:**
1. Cancels the broadcaster task.
2. Stops the MQTT client.
---
 
## API Endpoints
 
### System Endpoints
 
#### `GET /`
Returns the `index.html` dashboard as an HTML response. Served directly from the filesystem at `api/index.html`.
 
#### `GET /health`
No authentication required.
 
**Response:**
```json
{
  "status": "ok",
  "database": "ok",
  "mqtt": "connected",
  "websocket_clients": 3,
  "visitor_count": 3,
  "timestamp": "2025-01-15T10:30:00"
}
```
`status` is `"degraded"` if the database health check fails. `mqtt` is one of `"connected"`, `"disconnected"`, or `"not_configured"`.
 
#### `GET /api/info`
No authentication required.
 
Returns the API name, version, and feature list.
 
---
 
### Auth Endpoints
 
All auth endpoints are tagged `auth`. Login and signup are rate-limited by client IP.
 
#### `POST /auth/login`
**Body:** `LoginRequest`
 
Validates credentials (salted SHA-256 hash comparison), creates a session token, updates `last_login`.
 
**Response:**
```json
{
  "access_token": "<64-char hex>",
  "token_type": "bearer",
  "username": "alice",
  "role": "admin",
  "expires_in": 28800
}
```
**Errors:** `400` (missing fields), `401` (wrong credentials), `429` (rate limit).
 
---
 
#### `POST /auth/signup`
**Body:** `SignupRequest`
 
Self-registration. New accounts are always assigned `role: "viewer"`.
 
Validation rules:
- Username ≥ 3 characters
- Password ≥ 8 characters
- Passwords must match
- Username must be unique
**Errors:** `400` (validation), `409` (duplicate username), `429` (rate limit).
 
---
 
#### `POST /auth/logout`
**Auth:** Bearer token (optional — graceful if missing)
 
Revokes the current session token. Always returns `{"status": "logged out"}`.
 
---
 
#### `GET /auth/me`
**Auth:** Required
 
Returns the current user's profile: `username`, `role`, `last_login`, `created_at`.
 
---
 
#### `POST /auth/change-password`
**Auth:** Required
**Body:** `ChangePasswordRequest`
 
Verifies the current password, updates the hash, and **invalidates all existing sessions** for the user (forcing re-login everywhere).
 
**Errors:** `400` (new password < 8 chars), `401` (wrong current password).
 
---
 
#### `GET /auth/users`
**Auth:** Admin only
 
Returns all users (id, username, role, created_at, last_login). Passwords are never returned.
 
---
 
#### `POST /auth/users`
**Auth:** Admin only
**Body:** `CreateUserRequest`
 
Admin creates a user with an explicit role (`"admin"` or `"viewer"`).
 
---
 
#### `DELETE /auth/users/{target_username}`
**Auth:** Admin only
 
Deletes the target user. Admins cannot delete their own account.
 
**Errors:** `400` (self-delete), `404` (user not found).
 
---
 
### IoT Ingestion
 
#### `POST /data`
**Auth:** IoT API key (`X-Api-Key` header)
 
Accepts a `SensorDataIn` JSON body, writes it to the database, and broadcasts a `hive_update` WebSocket event to all connected clients.
 
**Response:** `{"status": "ok", "message": "Data saved"}`
 
**Errors:** `401` (bad key), `500` (DB write failure).
 
---
 
### Data / Hive Endpoints
 
All data endpoints require a valid Bearer token.
 
#### `GET /api/hives`
Returns latest readings for all hives. Equivalent to `/data/all/latest`.
 
#### `GET /api/hives/{hive_id}`
Returns the latest reading for a single hive. `404` if unknown.
 
#### `GET /data/latest/{hive_id}`
Alias for `GET /api/hives/{hive_id}`.
 
#### `GET /data/history/{hive_id}?hours=24`
Returns historical readings for a hive.
 
| Query param | Type | Range | Default |
|---|---|---|---|
| `hours` | `int` | 1–720 | 24 |
 
Returns up to 500 rows, newest first. Each row: `timestamp`, `temperature`, `humidity`, `weight`, `battery`, `sound`, `swarm`.
 
#### `GET /data/all/latest`
Returns the latest reading for every hive. Same as `GET /api/hives`.
 
---
 
### Notification Endpoints
 
#### `POST /api/notifications/config`
**Auth:** Required
**Body:** `NotifConfigRequest`
 
Saves a notification channel configuration for the authenticated user. If `enabled=true` and a delivery address is provided, sends an immediate test message.
 
Supported channels:
 
| `channel` | Required field | Value stored |
|---|---|---|
| `"email"` | `address` | SMTP destination |
| `"telegram"` | `chat_id` | Telegram chat ID |
| `"sms"` | `phone` | E.164 phone number |
 
#### `GET /api/notifications/config`
**Auth:** Required
 
Returns all notification settings for the current user:
```json
{
  "email":    {"enabled": true,  "address": "user@example.com"},
  "telegram": {"enabled": false, "chat_id": ""},
  "sms":      {"enabled": false, "phone":   ""}
}
```
 
---
 
### WebSocket Endpoints
 
All WebSocket endpoints authenticate via a `?token=<bearer_token>` query parameter. Invalid or missing tokens cause the connection to close immediately with code `4001`.
 
---
 
## Database Schema
 
Four tables, all managed by SQLAlchemy and created via `Base.metadata.create_all()`:
 
```
sensor_data
  id          INTEGER  PRIMARY KEY AUTOINCREMENT
  hive_id     TEXT     NOT NULL
  timestamp   TEXT     NOT NULL   -- ISO-8601
  temperature REAL
  humidity    REAL
  weight      REAL
  battery     INTEGER
  sound       INTEGER
  swarm       INTEGER  DEFAULT 0  -- 0 or 1
  latitude    REAL
  longitude   REAL
  INDEX idx_hive_timestamp (hive_id, timestamp)
 
users
  id          INTEGER  PRIMARY KEY AUTOINCREMENT
  username    TEXT     NOT NULL UNIQUE
  password    TEXT     NOT NULL   -- SHA-256(salt:password)
  role        TEXT     NOT NULL   DEFAULT 'viewer'
  created_at  TEXT     NOT NULL
  last_login  TEXT
 
sessions
  token       TEXT     PRIMARY KEY  -- 64-char hex
  username    TEXT     NOT NULL
  expires_at  TEXT     NOT NULL     -- ISO-8601
 
notification_settings
  username    TEXT     NOT NULL  PRIMARY KEY (composite)
  channel     TEXT     NOT NULL  PRIMARY KEY (composite)
  enabled     INTEGER  NOT NULL  DEFAULT 0
  config      TEXT               -- JSON blob
```
 
SQLite is run in **WAL (Write-Ahead Logging)** mode for better read concurrency and crash safety.
 
---
 
## Authentication & RBAC
 
The system uses a simple token-based session mechanism with two roles.
 
**Password hashing:** SHA-256 applied to `"{SECRET_SALT}:{plaintext_password}"`. The salt is a server-side secret; no per-user salt is stored.
 
**Session tokens:** 64-character cryptographically random hex strings generated by `secrets.token_hex(32)`. Tokens are stored in the `sessions` table with an expiry timestamp. Expired tokens are pruned opportunistically on each login.
 
**Roles:**
 
| Role | Capabilities |
|---|---|
| `viewer` | Login, view hive data, manage own notifications, change own password |
| `admin` | All viewer capabilities + list/create/delete users, access `/ws/db` |
 
Self-registration always creates `viewer` accounts. Only admins can create other admins.
 
---
 
## WebSocket Protocol
 
### `/ws` — Live Hive Data
 
Authentication: `?token=<bearer_token>`
 
**On connect:** immediately sends a full `hive_update` snapshot.
 
**Periodic:** server sends `hive_update` every 5 seconds if any clients are connected.
 
**Client → Server messages:**
- `"ping"` → server replies with `{"type": "pong", "timestamp": "..."}`
**Server → Client message types:**
 
```json
// Hive data update
{"type": "hive_update", "data": [...], "timestamp": "..."}
 
// Keepalive (every 30s if no client message)
{"type": "heartbeat", "timestamp": "..."}
 
// Ping response
{"type": "pong", "timestamp": "..."}
```
 
---
 
### `/ws/db` — Live Database Viewer
 
Authentication: `?token=<bearer_token>` + admin role required. Closes with code `4003` if the user is not an admin.
 
**On connect:** immediately sends a full DB snapshot.
 
**Periodic:** pushes an updated snapshot every 10 seconds.
 
**Client → Server messages:**
- `"refresh"` → triggers an immediate snapshot push.
**Server → Client:**
```json
{
  "type": "db_snapshot",
  "data": {
    "sensor_data":             {"columns": [...], "rows": [[...], ...]},
    "users":                   {"columns": [...], "rows": [[...], ...]},
    "sessions":                {"columns": [...], "rows": [[...], ...]},
    "notification_settings":   {"columns": [...], "rows": [[...], ...]}
  },
  "timestamp": "..."
}
```
 
`sensor_data` is limited to the 200 most recent rows. Session tokens are truncated to 12 chars + `…` for security.
 
---
 
### `/ws/visitors` — Connected Clients
 
Authentication: `?token=<bearer_token>`
 
Streams presence updates whenever any authenticated WebSocket connects or disconnects. Also sends a heartbeat every 15 seconds.
 
**Server → Client:**
```json
// Presence update (also sent immediately on connect)
{
  "type": "visitors_update",
  "count": 2,
  "clients": [
    {"username": "alice", "connected_at": "..."},
    {"username": "bob",   "connected_at": "..."}
  ],
  "timestamp": "..."
}
 
// Heartbeat
{"type": "heartbeat", "timestamp": "..."}
```
 
---
 
## Notification Channels
 
Notifications are dispatched asynchronously using `asyncio.create_task`, so they never block the HTTP response.
 
| Channel | Transport | Required config |
|---|---|---|
| Email | SMTP + STARTTLS | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS` |
| Telegram | Bot API (HTTP POST) | `TELEGRAM_BOT_TOKEN`, per-user `chat_id` |
| SMS | Twilio REST API | `TWILIO_SID`, `TWILIO_TOKEN`, `TWILIO_FROM` |
 
All three channels run their blocking I/O in a thread-pool executor (`loop.run_in_executor`) to avoid blocking the event loop.
 
`maybe_notify(hive_id, alert_type, message)` is the application-level entry point. It queries all enabled settings across all users and dispatches to each. Call this from alert-detection logic when a sensor threshold is crossed.
 
---
 
## Rate Limiting
 
An in-process sliding-window limiter protects the login and signup endpoints.
 
- Window: 60 seconds
- Limit: 10 requests per unique IP
- Key: `"login:{client_ip}"` or `"signup:{client_ip}"`
- Responses over the limit: `HTTP 429 Too Many Requests`
- IP extracted from the `X-Forwarded-For` header (for reverse-proxy deployments)
The limiter is in-process (no Redis), so limits reset on server restart and are not shared across multiple processes/workers.
 
---
 
## Configuration Reference
 
Minimum required `.env` for development:
 
```dotenv
# Required
SECRET_SALT=<64-char hex — generate with: python -c "import secrets; print(secrets.token_hex(32))">
 
# Recommended
IOT_API_KEY=<random key for IoT devices>
 
# Optional MQTT
MQTT_BROKER=your-broker.example.com
MQTT_PORT=8883
MQTT_USERNAME=apiaryuser
MQTT_PASSWORD=secret
MQTT_TOPIC=rucher/data
MQTT_TLS_ENABLED=true
 
# Optional notifications
TELEGRAM_BOT_TOKEN=123456:ABC...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@gmail.com
SMTP_PASS=app-password
TWILIO_SID=AC...
TWILIO_TOKEN=...
TWILIO_FROM=+15005550006
 
# Server
SERVER_HOST=0.0.0.0
SERVER_PORT=8000
LOG_LEVEL=INFO
 
# CORS (comma-separated)
CORS_ORIGINS=https://yourdomain.com,https://app.yourdomain.com
```
 
---
 
## Running the Server
 
**Install dependencies:**
```bash
pip install fastapi uvicorn sqlalchemy pydantic python-dotenv \
            paho-mqtt certifi
# Optional:
pip install twilio
```
 
**Run:**
```bash
python -m api.main
# or
uvicorn api.main:app --host 0.0.0.0 --port 8000
```
 
**First run:** A default `admin` user with password `admin` is created automatically. **Change this password immediately** via `POST /auth/change-password`.
 
**Interactive API docs:** `http://localhost:8000/docs` (Swagger UI)
 
**Serverless note:** When `VERCEL=1` or `AWS_LAMBDA_FUNCTION_NAME` is set, the database path switches to `/tmp/smart_apiary.db`, and the MQTT client and WebSocket broadcaster are not started (incompatible with stateless serverless execution).
 
---
 
## Design Decisions
 
**SQLAlchemy ORM over raw sqlite3**
Thread-safe scoped sessions replace the previous thread-local `sqlite3.Connection` approach. WAL mode is preserved via a connection event hook, maintaining the same concurrency characteristics.
 
**Scoped sessions (`scoped_session`)**
Each request handler acquires a session at the start and calls `Session.remove()` in a `finally` block. This ensures sessions are never leaked across requests even on exceptions.
 
**In-process rate limiter**
Chosen for simplicity (no Redis dependency). Adequate for single-instance deployments. For multi-worker setups, replace `_rate_store` with a Redis-backed implementation.
 
**Async notification dispatch**
All three notification transports use blocking I/O (SMTP, Twilio, `urllib`). Wrapping them in `loop.run_in_executor` keeps the FastAPI event loop unblocked.
 
**WebSocket authentication via query parameter**
The WebSocket upgrade request cannot carry custom headers in browsers, so Bearer tokens are passed as `?token=`. This is a standard trade-off; tokens should be short-lived and the connection should be over TLS.
 
**DB snapshot token truncation**
The `/ws/db` endpoint truncates session tokens to 12 characters in the snapshot to prevent token leakage to admin dashboard users who may not be the token owner.
 
**Serverless-aware lifespan**
MQTT and the periodic broadcaster are conditionally skipped in serverless environments, which have no persistent process between invocations.
 
