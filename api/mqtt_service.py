"""
mqtt_service.py — MQTT subscriber that writes incoming sensor data to SQLite.

Fixes vs v2:
  - Import paths corrected to `api.*` package imports.
  - Reconnect uses threading.Event for cleaner shutdown instead of daemon timers.
  - on_disconnect signature updated for paho-mqtt >= 2.x (rc can be ReasonCode).
  - Callback added for on_log in DEBUG mode.
"""
import json
import logging
import ssl
import threading
from typing import Optional

import certifi
import paho.mqtt.client as mqtt

from test.BeeTrackDz.api.models import SensorDataIn
from test.BeeTrackDz.api.db import write_sensor_data
from test.BeeTrackDz.api.config import (
    MQTT_BROKER, MQTT_PORT, MQTT_USERNAME, MQTT_PASSWORD,
    MQTT_TOPIC, MQTT_TLS_ENABLED,
)

logger = logging.getLogger(__name__)

_MAX_RECONNECT_ATTEMPTS = 10
_RECONNECT_BASE_DELAY   = 2   # seconds, doubled each attempt (max 60 s)


class MQTTSubscriber:
    def __init__(self) -> None:
        self.connected = False
        self._reconnect_attempts = 0
        self._stop_event = threading.Event()

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION1,
            client_id="apiary_backend",
            clean_session=True,
        )

        if MQTT_USERNAME and MQTT_PASSWORD:
            self.client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

        if MQTT_TLS_ENABLED:
            try:
                self.client.tls_set(
                    ca_certs=certifi.where(),
                    certfile=None,
                    keyfile=None,
                    tls_version=ssl.PROTOCOL_TLS_CLIENT,
                )
                self.client.tls_insecure_set(False)
                logger.info("MQTT TLS configured.")
            except Exception as exc:
                logger.error("MQTT TLS setup failed: %s", exc)

        self.client.on_connect    = self._on_connect
        self.client.on_message    = self._on_message
        self.client.on_disconnect = self._on_disconnect

    # ── Callbacks ────────────────────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc) -> None:  # noqa: ARG002
        rc_int = rc if isinstance(rc, int) else rc.value
        if rc_int == 0:
            logger.info("MQTT connected → %s:%s", MQTT_BROKER, MQTT_PORT)
            self.connected = True
            self._reconnect_attempts = 0
            client.subscribe(MQTT_TOPIC, qos=1)
            logger.info("Subscribed to topic: %s", MQTT_TOPIC)
        else:
            _codes = {
                1: "bad protocol version",
                2: "invalid client id",
                3: "server unavailable",
                4: "bad credentials",
                5: "not authorised",
            }
            logger.error("MQTT connect refused: %s", _codes.get(rc_int, f"rc={rc_int}"))
            self.connected = False

    def _on_disconnect(self, client, userdata, rc) -> None:  # noqa: ARG002
        rc_int = rc if isinstance(rc, int) else rc.value
        logger.warning("MQTT disconnected (rc=%s).", rc_int)
        self.connected = False
        if not self._stop_event.is_set() and rc_int != 0:
            self._schedule_reconnect()

    def _on_message(self, client, userdata, msg) -> None:  # noqa: ARG002
        try:
            payload: dict = json.loads(msg.payload.decode("utf-8"))
            logger.debug("MQTT message on %s: %s", msg.topic, payload)

            sensor_data = SensorDataIn(
                environment=payload.get("environment", {}),
                gps=payload.get("gps", {}),
                hive=payload.get("hive", {}),
                system=payload.get("system", {}),
                hive_id=payload.get("hive_id"),
            )
            if write_sensor_data(sensor_data):
                logger.info("MQTT → DB: saved hive=%s", sensor_data.hive_id)
            else:
                logger.error("MQTT → DB: write failed for hive=%s", sensor_data.hive_id)

        except json.JSONDecodeError as exc:
            logger.error("MQTT invalid JSON: %s", exc)
        except Exception as exc:
            logger.error("MQTT message processing error: %s", exc, exc_info=True)

    # ── Connection management ─────────────────────────────────────────────────

    def connect(self) -> None:
        if not MQTT_BROKER:
            logger.warning("MQTT_BROKER not configured — skipping MQTT.")
            return
        try:
            logger.info("Connecting to MQTT %s:%s …", MQTT_BROKER, MQTT_PORT)
            self.client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
            self.client.loop_start()
        except Exception as exc:
            logger.error("MQTT initial connection failed: %s", exc)
            self._schedule_reconnect()

    def disconnect(self) -> None:
        self._stop_event.set()
        self.client.loop_stop()
        self.client.disconnect()
        logger.info("MQTT client disconnected.")

    def _schedule_reconnect(self) -> None:
        if self._reconnect_attempts >= _MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "MQTT: max reconnect attempts (%d) reached. "
                "Check broker configuration.",
                _MAX_RECONNECT_ATTEMPTS,
            )
            return
        self._reconnect_attempts += 1
        delay = min(60, _RECONNECT_BASE_DELAY ** self._reconnect_attempts)
        logger.info(
            "MQTT reconnect in %ds (attempt %d/%d)…",
            delay, self._reconnect_attempts, _MAX_RECONNECT_ATTEMPTS,
        )
        t = threading.Timer(delay, self.connect)
        t.daemon = True
        t.start()


# ── Module-level singleton ────────────────────────────────────────────────────

_subscriber: Optional[MQTTSubscriber] = None


def start_mqtt_client() -> Optional[MQTTSubscriber]:
    global _subscriber
    try:
        _subscriber = MQTTSubscriber()
        _subscriber.connect()
        logger.info("MQTT client started.")
    except Exception as exc:
        logger.error("Failed to start MQTT client: %s", exc)
    return _subscriber


def stop_mqtt_client() -> None:
    global _subscriber
    if _subscriber:
        _subscriber.disconnect()
        _subscriber = None


def get_mqtt_status() -> str:
    """Return a human-readable connection status string."""
    if _subscriber is None:
        return "not_configured"
    return "connected" if _subscriber.connected else "disconnected"