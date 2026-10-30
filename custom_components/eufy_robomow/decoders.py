"""Decoders for the mower's protobuf-encoded (raw) data points.

Raw DPs arrive as base64 text whose bytes are a protobuf message. DP names
come from the E15's Tuya product schema (robot_status, battery_status,
gps_info, …); the E18 uses the same schema. Field meanings for DP107 follow
github.com/Thomvanoorschot/ha-eufy-e15, DP154 follows the mode-selection
observations in github.com/keesmod/eufy-robomow-ha (docs/zone-control-research).

Every decoder returns None (or an empty result) for missing or malformed
input and never raises.
"""
from __future__ import annotations

import base64
import struct
from dataclasses import dataclass
from typing import Any

DP_ROBOT_STATUS = "107"    # raw: mission status (pushed only, also in cloud)
DP_BATTERY_STATUS = "108"  # raw: charge state + charger contact
DP_GPS_INFO = "142"        # raw: GPS fix (anti-theft)
DP_WORK_MODE = "154"       # raw: mowing mode selected in the app
DP_EDGE_TRIM = "128"       # bool
DP_WORK_ANGLE_ENABLED = "130"  # bool
DP_WORK_ANGLE = "131"      # int, degrees
DP_CELLULAR_ENABLED = "137"  # bool
DP_GPS_ANTI_THEFT = "140"  # bool
DP_STATION_USED = "125"    # int, minutes
DP_BLADE_USED = "126"      # int, minutes since blades were replaced
DP_MAP_SAVE_PROGRESS = "118"  # int, 0–100 %
DP_FIRMWARE = "129"        # raw: firmware versions of the mower's components


# ── generic protobuf parsing ──────────────────────────────────────────────────

def _varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("truncated varint")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            return result, pos
        if shift > 70:
            raise ValueError("varint too long")


def parse_message(data: bytes) -> dict[int, list[Any]]:
    """Parse one protobuf message level into {field: [values…]}.

    Varints become int, fixed64 becomes float (double), fixed32 float,
    length-delimited fields stay bytes (parse them again for sub-messages).
    """
    out: dict[int, list[Any]] = {}
    pos = 0
    while pos < len(data):
        key, pos = _varint(data, pos)
        field, wire = key >> 3, key & 7
        if field == 0:
            raise ValueError("field 0")
        if wire == 0:
            value, pos = _varint(data, pos)
        elif wire == 1:
            value = struct.unpack_from("<d", data, pos)[0]
            pos += 8
        elif wire == 5:
            value = struct.unpack_from("<f", data, pos)[0]
            pos += 4
        elif wire == 2:
            length, pos = _varint(data, pos)
            value = data[pos:pos + length]
            pos += length
        else:
            raise ValueError(f"wire type {wire}")
        out.setdefault(field, []).append(value)
    return out


def _decode(value: Any) -> dict[int, list[Any]] | None:
    """base64 text → parsed top-level message; None if not decodable."""
    if not isinstance(value, str) or not value:
        return None
    try:
        data = base64.b64decode(value, validate=True)
        if data in (b"", b"\x00"):  # the mower's "empty" placeholder
            return {}
        return parse_message(data)
    except Exception:  # noqa: BLE001
        return None


def _int(msg: dict[int, list[Any]], field: int, default: int = 0) -> int:
    values = msg.get(field)
    return values[-1] if values and isinstance(values[-1], int) else default


def _sub(msg: dict[int, list[Any]], field: int) -> dict[int, list[Any]]:
    values = msg.get(field)
    if not values or not isinstance(values[-1], bytes):
        return {}
    try:
        return parse_message(values[-1])
    except Exception:  # noqa: BLE001
        return {}


# ── DP107 robot_status ────────────────────────────────────────────────────────

MISSION_NONE = 0
MISSION_RECHARGE = 1
MISSIONS: dict[int, str] = {
    0: "none",
    1: "returning",
    2: "whole_lawn",
    3: "mapping",
    4: "mapping_while_mowing",
    5: "temporary",
    7: "remote_control",
    8: "scheduled_whole_lawn",
    9: "scheduled_mapping",
    10: "zone",
    16: "scheduled_zone",
    17: "box",
    18: "edge",
    22: "scheduled_edge",
}
MOWING_MISSIONS = {2, 4, 5, 7, 8, 10, 16, 17, 18, 22}

PHASES: dict[int, str] = {
    0: "none",
    1: "relocating",
    3: "leaving_station",
    5: "saving_map",
    6: "setting_blade_height",
    9: "defogging",
}

STATE_IDLE, STATE_RUNNING, STATE_PAUSED, STATE_ABORTED, STATE_COMPLETE = 0, 1, 2, 3, 4
STATES: dict[int, str] = {
    0: "idle",
    1: "running",
    2: "paused",
    3: "aborted",
    4: "complete",
}

OTHER = "other"


@dataclass(frozen=True)
class MissionStatus:
    mission: int = 0
    phase: int = 0
    state: int = 0
    power_mode: int = 0
    error: bool = False
    saving_data: bool = False

    @property
    def mission_name(self) -> str:
        return MISSIONS.get(self.mission, OTHER)

    @property
    def phase_name(self) -> str:
        return PHASES.get(self.phase, OTHER)

    @property
    def state_name(self) -> str:
        return STATES.get(self.state, OTHER)

    @property
    def is_mowing(self) -> bool:
        return self.mission in MOWING_MISSIONS

    @property
    def is_returning(self) -> bool:
        return self.mission == MISSION_RECHARGE and self.state != STATE_PAUSED


def decode_robot_status(value: Any) -> MissionStatus | None:
    msg = _decode(value)
    if msg is None:
        return None
    return MissionStatus(
        mission=_int(msg, 1),
        phase=_int(msg, 2),
        state=_int(msg, 3),
        power_mode=_int(msg, 4),
        error=bool(_int(msg, 5)),
        saving_data=bool(_int(msg, 6)),
    )


# ── DP108 battery_status ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class BatteryStatus:
    charge_state: int = 0
    charger_connected: bool = False


def decode_battery_status(value: Any) -> BatteryStatus | None:
    msg = _decode(value)
    if msg is None:
        return None
    return BatteryStatus(
        charge_state=_int(msg, 1),
        charger_connected=_int(msg, 2) == 1,
    )


# ── DP142 gps_info ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GpsFix:
    latitude: float
    longitude: float
    accuracy: float | None
    timestamp: int | None
    points: dict[int, tuple[float, float]]  # other reported points by type


def _coords(msg: dict[int, list[Any]]) -> tuple[float, float, float | None] | None:
    lat, lon = msg.get(1, [None])[-1], msg.get(2, [None])[-1]
    if not isinstance(lat, float) or not isinstance(lon, float):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180) or (lat == 0 and lon == 0):
        return None
    acc = msg.get(3, [None])[-1]
    return lat, lon, acc if isinstance(acc, float) else None


def decode_gps_info(value: Any) -> GpsFix | None:
    """Field 3.3 holds the current fix; repeated field 4 holds typed points."""
    msg = _decode(value)
    if not msg:
        return None
    loc = _sub(msg, 3)
    current = _coords(_sub(loc, 3)) if loc else None

    points: dict[int, tuple[float, float]] = {}
    for raw in msg.get(4, []):
        if not isinstance(raw, bytes):
            continue
        try:
            entry = parse_message(raw)
        except Exception:  # noqa: BLE001
            continue
        coords = _coords(_sub(entry, 2))
        if coords:
            points[_int(entry, 1)] = (coords[0], coords[1])

    if current is None:
        if not points:
            return None
        lat, lon = next(iter(points.values()))
        current = (lat, lon, None)

    ts = _int(_sub(msg, 2), 1) or None
    return GpsFix(current[0], current[1], current[2], ts, points)


# ── DP154 work mode (selected in the app) ─────────────────────────────────────

WORK_MODES: dict[int, str] = {0: "entire", 1: "zone", 2: "box"}


def decode_work_mode(value: Any) -> str | None:
    msg = _decode(value)
    if msg is None:
        return None
    return WORK_MODES.get(_int(msg, 3), OTHER)


# ── DP129 firmware versions ───────────────────────────────────────────────────
# Nine string fields. Field 2 matches the main firmware in DP102 and the app;
# field 9 is the LTE modem's AT+CGMR reply. The other boards are unnamed in
# any known source, so they are reported by field number.

def decode_firmware(value: Any) -> dict[str, str] | None:
    msg = _decode(value)
    if not msg:
        return None
    out: dict[str, str] = {}
    for field in sorted(msg):
        raw = msg[field][-1]
        if not isinstance(raw, bytes):
            continue
        text = raw.decode(errors="ignore").strip()
        if not text:
            continue
        if text.startswith("+CGMR:"):
            out["modem"] = text.removeprefix("+CGMR:").strip()
        elif field == 2:
            out["main"] = text
        else:
            out[f"component_{field}"] = text
    return out or None
