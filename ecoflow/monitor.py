"""
EcoFlow MQTT monitor.

Subscribes to the real-time device data stream, caches all device state,
and detects charging state transitions.  Also publishes set-commands back
to the device via the MQTT /set topic.

MQTT topics (EcoFlow Open API):
    Subscribe: /open/{certificateAccount}/{device_sn}/quota  – device telemetry
    Publish:   /open/{certificateAccount}/{device_sn}/set    – control commands

Charging state values (bms_emsStatus.chgState):
    0 = Idle / not charging
    1 = Constant-current charging
    2 = Constant-voltage charging
    3 = Constant-current discharging
    4 = Discharging
"""

import json
import logging
import random
import ssl
import threading
import uuid
from collections.abc import Callable
from typing import Any

import paho.mqtt.client as mqtt

from .auth import MqttCredentials

logger = logging.getLogger(__name__)

# chgState values that mean the battery is actively charging
_CHARGING_STATES = {1, 2}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """
    Recursively flatten a nested dict into dot-notation keys.
        {"bms_emsStatus": {"chgState": 1}} → {"bms_emsStatus.chgState": 1}
    """
    result: dict[str, Any] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            full_key = f"{prefix}.{k}" if prefix else k
            result.update(_flatten(v, full_key))
    else:
        result[prefix] = obj
    return result


def _coerce_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_charging(flat: dict[str, Any], watts_threshold: float) -> bool | None:
    """
    Determine charging state from a flattened MQTT payload.

    Returns True/False, or None when there's not enough data.
    """
    chg_state = flat.get("bms_emsStatus.chgState")
    if chg_state is not None:
        try:
            return int(chg_state) in _CHARGING_STATES
        except (ValueError, TypeError):
            pass

    # Fallback: total input watts
    watts_in = flat.get("pd.wattsInSum") or flat.get("inv.inputWatts")
    w = _coerce_float(watts_in)
    if w is not None:
        return w > watts_threshold

    return None


# ---------------------------------------------------------------------------
# DeviceState — typed snapshot of the latest cached values
# ---------------------------------------------------------------------------

class DeviceState:
    """
    A frozen snapshot of everything the monitor knows about the device.
    All fields are None when data hasn't arrived yet.
    """

    __slots__ = (
        "soc",
        "watts_in",
        "watts_out",
        "ac_in_watts",
        "ac_out_watts",
        "ac_in_voltage",
        "ac_in_freq",
        "ac_out_enabled",
        "usb_out_enabled",
        "dc_out_enabled",
        "chg_state",
        "chg_remain_min",
        "dsg_remain_min",
        "inv_temp_c",
        "is_charging",
    )

    def __init__(self, flat: dict[str, Any], watts_threshold: float) -> None:
        def f(key: str) -> float | None:
            return _coerce_float(flat.get(key))

        def b(key: str) -> bool | None:
            v = flat.get(key)
            if v is None:
                return None
            try:
                return bool(int(v))
            except (ValueError, TypeError):
                return None

        self.soc: float | None = f("pd.soc")
        self.watts_in: float | None = f("pd.wattsInSum")
        self.watts_out: float | None = f("pd.wattsOutSum")
        self.ac_in_watts: float | None = f("inv.inputWatts")
        self.ac_out_watts: float | None = f("inv.outputWatts")
        self.ac_in_voltage: float | None = f("inv.acInVol")
        self.ac_in_freq: float | None = f("inv.acInFreq")
        self.ac_out_enabled: bool | None = b("inv.cfgAcEnabled")
        # USB/DC 5V–12V outputs
        self.usb_out_enabled: bool | None = b("pd.dcOutState")
        # 12 V car port
        self.dc_out_enabled: bool | None = b("pd.carState")
        self.chg_state: int | None = (
            int(flat["bms_emsStatus.chgState"])
            if flat.get("bms_emsStatus.chgState") is not None else None
        )
        self.chg_remain_min: float | None = f("bms_emsStatus.chgRemainTime")
        self.dsg_remain_min: float | None = f("bms_emsStatus.dsgRemainTime")
        self.inv_temp_c: float | None = f("inv.outTemp")
        self.is_charging: bool | None = _is_charging(flat, watts_threshold)

    @property
    def has_data(self) -> bool:
        return self.soc is not None or self.watts_in is not None


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------

class EcoFlowMonitor:
    """
    Connects to the EcoFlow MQTT broker and:
      - Caches all incoming device telemetry in a DeviceState snapshot
      - Detects charging ↔ not-charging transitions and fires callbacks
      - Publishes control commands back to the device

    All callbacks are invoked from paho's network thread — callers must be
    thread-safe (e.g. asyncio.run_coroutine_threadsafe).
    """

    def __init__(
        self,
        credentials: MqttCredentials,
        device_sn: str,
        watts_threshold: float,
        on_charging_start: Callable[[DeviceState], None],
        on_charging_stop: Callable[[DeviceState], None],
        on_connect: Callable[[], None] | None = None,
        on_disconnect: Callable[[], None] | None = None,
    ) -> None:
        self._creds = credentials
        self._device_sn = device_sn
        self._watts_threshold = watts_threshold
        self._on_charging_start = on_charging_start
        self._on_charging_stop = on_charging_stop
        self._on_connect_cb = on_connect
        self._on_disconnect_cb = on_disconnect

        # Threading
        self._lock = threading.Lock()

        # Full device state cache (flat key→value)
        self._flat: dict[str, Any] = {}

        # Charging state machine
        self._charging: bool | None = None

        # MQTT connected flag
        self._connected: bool = False

        # Topic shortcuts
        self._quota_topic = f"/open/{credentials.username}/{device_sn}/quota"
        self._set_topic = f"/open/{credentials.username}/{device_sn}/set"

        self._client = self._build_client()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_client(self) -> mqtt.Client:
        client_id = f"OPEN_API_{uuid.uuid4().hex[:12].upper()}"
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION1,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
        except AttributeError:
            client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)

        client.username_pw_set(self._creds.username, self._creds.password)
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, rc):
        if rc == 0:
            logger.info("MQTT connected — subscribing to %s", self._quota_topic)
            client.subscribe(self._quota_topic, qos=1)
            with self._lock:
                self._connected = True
            if self._on_connect_cb:
                self._on_connect_cb()
        else:
            logger.error("MQTT connection failed (rc=%d)", rc)

    def _on_disconnect(self, client, userdata, rc):
        with self._lock:
            self._connected = False
        if rc != 0:
            logger.warning("Unexpected MQTT disconnect (rc=%d), reconnecting…", rc)
        if self._on_disconnect_cb:
            self._on_disconnect_cb()

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug("Bad MQTT payload: %s", exc)
            return

        params = payload.get("params", payload)
        incoming = _flatten(params)

        with self._lock:
            self._flat.update(incoming)
            flat_snapshot = dict(self._flat)
            previous = self._charging

        state = DeviceState(flat_snapshot, self._watts_threshold)

        if state.is_charging is None:
            return

        with self._lock:
            self._charging = state.is_charging

        # First message — establish baseline without notifying
        if previous is None:
            logger.info(
                "Initial state: %s | SOC=%s%% | In=%sW | Out=%sW",
                "charging" if state.is_charging else "idle",
                state.soc, state.watts_in, state.watts_out,
            )
            return

        if state.is_charging and not previous:
            logger.info("Charging STARTED | SOC=%s%% | In=%sW", state.soc, state.watts_in)
            self._on_charging_start(state)
        elif not state.is_charging and previous:
            logger.info("Charging STOPPED | SOC=%s%%", state.soc)
            self._on_charging_stop(state)

    # ------------------------------------------------------------------
    # Command publishing
    # ------------------------------------------------------------------

    def publish_command(
        self,
        operate_type: str,
        module_type: int,
        params: dict[str, Any],
    ) -> bool:
        """
        Publish a set-command to the device via MQTT.

        Returns True if the message was queued, False if not connected.
        """
        if not self._connected:
            logger.warning("Cannot send command — MQTT not connected")
            return False

        payload = {
            "id": str(random.randint(100_000, 999_999)),
            "version": "1.0",
            "moduleType": module_type,
            "operateType": operate_type,
            "params": params,
        }
        result = self._client.publish(self._set_topic, json.dumps(payload), qos=1)
        logger.info(
            "Command sent → %s (moduleType=%d, params=%s) rc=%d",
            operate_type, module_type, params, result.rc,
        )
        return result.rc == mqtt.MQTT_ERR_SUCCESS

    def set_ac_output(
        self,
        enabled: bool,
        voltage: int = 230,
        freq: int = 1,
        xboost: bool = True,
    ) -> bool:
        """
        Enable or disable AC output.

        All four params must be sent together — partial updates are ignored
        by the firmware. Set freq=1 for 50 Hz (most of the world), freq=2 for 60 Hz (North America).
        """
        return self.publish_command(
            operate_type="acOutCfg",
            module_type=5,
            params={
                "enabled": int(enabled),
                "xboost": int(xboost),
                "out_voltage": voltage,
                "out_freq": freq,
            },
        )

    def set_usb_output(self, enabled: bool) -> bool:
        """Enable or disable USB / DC 5 V–12 V output ports."""
        return self.publish_command(
            operate_type="dcOutCfg",
            module_type=1,
            params={"enabled": int(enabled)},
        )

    def set_dc_car_output(self, enabled: bool) -> bool:
        """Enable or disable the 12 V car / cigarette-lighter port."""
        return self.publish_command(
            operate_type="mpptCar",
            module_type=5,
            params={"enabled": int(enabled)},
        )

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_state(self) -> DeviceState:
        """Return a snapshot of the latest cached device state."""
        with self._lock:
            flat_snapshot = dict(self._flat)
        return DeviceState(flat_snapshot, self._watts_threshold)

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def current_charging(self) -> bool | None:
        """Current charging state (None if not yet determined)."""
        with self._lock:
            return self._charging

    def start(self) -> None:
        logger.info("Connecting to %s:%d", self._creds.host, self._creds.port)
        self._client.connect(self._creds.host, self._creds.port, keepalive=60)
        self._client.loop_start()

    def stop(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
