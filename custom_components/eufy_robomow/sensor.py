"""Sensors for Eufy Robomow."""

from __future__ import annotations

import base64
import binascii
import logging
import struct
from dataclasses import dataclass, field
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfLength, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    DP_BATTERY,
    DP_NETWORK,
    DP_SIGNAL,
    DP_LIVE_VIEW,
    DP_TASK_ACTIVE,
    GENERIC_SENSOR_PREFIX,
)
from .coordinator import EufyMowerCoordinator
from .decoders import (
    DP_FIRMWARE,
    decode_firmware,
    DP_BLADE_USED,
    DP_GPS_INFO,
    DP_MAP_SAVE_PROGRESS,
    DP_ROBOT_STATUS,
    DP_BATTERY_STATUS,
    DP_STATION_USED,
    DP_WORK_MODE,
    DP_EDGE_TRIM,
    DP_WORK_ANGLE_ENABLED,
    DP_WORK_ANGLE,
    DP_CELLULAR_ENABLED,
    DP_GPS_ANTI_THEFT,
    MISSIONS,
    OTHER,
    PHASES,
    WORK_MODES,
    decode_robot_status,
    decode_work_mode,
)

_LOGGER = logging.getLogger(__name__)

# DP125 unit: ~6.6 seconds per unit (confirmed: 36149 units ≈ 66h total)
DP125_SECONDS_PER_UNIT = 6.6

# DPS that should NOT be exposed as generic sensors (already have dedicated entities)
EXCLUDED_DPS = {
    "1",    # Task active      → used internally by lawn_mower entity
    "2",    # Paused           → used internally by lawn_mower entity
    "8",    # Battery          → dedicated Battery sensor
    "26",   # Volume           → dedicated Number entity
    "47",   # Child protection → dedicated Switch entity
    "101",  # Rain detection   → dedicated Switch entity
    "109",  # Signal strength  → dedicated Sensor entity
    "110",  # Cut height       → dedicated Number entity
    "114",  # Live view state  → dedicated Sensor entity
    "113",  # Session telemetry → dedicated EufyMowingProgressSensor + EufySessionDistanceSensor
    "118",  # Return Progress  → dedicated sensor
    "124",  # Coverage         → dedicated EufyCoverageSensor (blob decode)
    "125",  # Total time       → dedicated Total Mow Time sensor
    "126",  # Area             → dedicated Mowed Area sensor
    "132",  # Smart suggestion → dedicated Switch entity
    "133",  # Real lawn map    → dedicated Switch entity
    "134",  # Network          → dedicated Network sensor
    "141",  # Mow yellow grass → dedicated Switch entity
    # Raw/bool DPs with decoded dedicated entities
    DP_ROBOT_STATUS,        # 107 → Task / Task Phase sensors, Problem binary sensor
    DP_BATTERY_STATUS,      # 108 → On Charging Station binary sensor
    DP_GPS_INFO,            # 142 → device tracker
    DP_WORK_MODE,           # 154 → Mowing Mode sensor
    DP_EDGE_TRIM,           # 128 → binary sensor
    DP_WORK_ANGLE_ENABLED,  # 130 → binary sensor (angle in attributes)
    DP_WORK_ANGLE,          # 131
    DP_CELLULAR_ENABLED,    # 137 → binary sensor
    DP_GPS_ANTI_THEFT,      # 140 → binary sensor
    DP_FIRMWARE,            # 129 → Firmware sensor
    "102",  # device info → device page (serial / firmware); contains network identity
    "135",  # SIM card identifiers — never exposed
}

# Generic "DPxxx" entities created by earlier versions for DPs that now have
# decoded entities, and sensors whose meaning was wrong. Removed on setup.
OBSOLETE_UNIQUE_SUFFIXES = (
    [f"generic_{dp}" for dp in (
        DP_ROBOT_STATUS, DP_BATTERY_STATUS, DP_GPS_INFO, DP_WORK_MODE, DP_EDGE_TRIM,
        DP_WORK_ANGLE_ENABLED, DP_WORK_ANGLE, DP_CELLULAR_ENABLED, DP_GPS_ANTI_THEFT,
        DP_FIRMWARE, "102", "135",
    )]
    + ["mowed_area", "total_time"]
)

# Cloud setting keys that have dedicated entities — exclude from generic sensors
# to avoid duplicating values that are already well-represented.
CLOUD_ENTITY_KEYS = {
    "cloud_path_mm",       # Path distance select (8/10/12 cm)
    "cloud_travel_speed",  # Travel speed select
    "cloud_blade_speed",   # Blade speed select
    "cloud_edge_mm",       # Edge distance number
    "cloud_pad_direction", # Pad direction number
}

# Pre-seed set is now empty — all previously known DPs have dedicated entities.
# Keep the set so the mechanism is ready for future DPs we discover.
PRESEED_DPS: set[str] = set()


@dataclass(frozen=True)
class EufySensorDescription(SensorEntityDescription):
    dp: str = ""


SENSORS: tuple[EufySensorDescription, ...] = (
    EufySensorDescription(
        key="battery",
        dp=DP_BATTERY,
        name="Battery",
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:battery",
    ),
    EufySensorDescription(
        # DP126 mow_blade_used_time (minutes since the blades were replaced).
        # Previously mislabelled "Mowed Area". Resets when blades are replaced.
        key="blade_used_time",
        dp=DP_BLADE_USED,
        name="Blade Usage",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        icon="mdi:saw-blade",
    ),
    EufySensorDescription(
        # DP125 base_station_used_time (minutes). Previously mislabelled
        # "Total Mow Time" with a guessed 6.6 s/unit factor.
        key="station_used_time",
        dp=DP_STATION_USED,
        name="Base Station Usage",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        suggested_display_precision=1,
        icon="mdi:home-lightning-bolt-outline",
    ),
    EufySensorDescription(
        # DP118 save_map_process: progress of the map save that ends every task.
        key="progress",
        dp=DP_MAP_SAVE_PROGRESS,
        name="Map Save Progress",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:content-save-outline",
    ),
    EufySensorDescription(
        key="network",
        dp=DP_NETWORK,
        name="Network",
        icon="mdi:wifi",
    ),
    EufySensorDescription(
        key="signal",
        dp=DP_SIGNAL,
        # DP109 wifi_signal_strength is a percentage, not dBm
        name="Wi-Fi Signal",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:wifi",
    ),
    EufySensorDescription(
        key="live_view",
        dp=DP_LIVE_VIEW,
        name="Live View State",
        icon="mdi:camera",
    ),
)

# ── Shared protobuf helpers ────────────────────────────────────────────────────

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """Read a protobuf varint from *data* at *pos*. Returns (value, new_pos)."""
    result = shift = 0
    while pos < len(data):
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return result, pos


def _proto_flat(data: bytes) -> dict[int, Any]:
    """Decode a protobuf blob into a flat {field_num: value} dict.

    Wire-type 2 (length-delimited) values are returned as raw bytes so the
    caller can recurse into nested messages if needed.  Repeated fields keep
    only the last value (sufficient for our use-cases).
    """
    fields: dict[int, Any] = {}
    pos = 0
    while pos < len(data):
        try:
            tag, pos = _read_varint(data, pos)
            fn = tag >> 3; wt = tag & 7
            if wt == 0:
                val, pos = _read_varint(data, pos)
                fields[fn] = val
            elif wt == 2:
                length, pos = _read_varint(data, pos)
                fields[fn] = data[pos:pos + length]; pos += length
            elif wt == 1:
                pos += 8   # fixed64 — skip
            elif wt == 5:
                pos += 4   # fixed32 / float — skip
            else:
                break      # unknown wire type — bail out
        except Exception:
            break
    return fields


# ── DP113 live session telemetry blob ─────────────────────────────────────────
# DP113 is updated every ~60–90 s while mowing.  Confirmed field meanings:
#   field1 = mode/constant (4 while mowing)
#   field2 = zone total planned area (units TBD; 913 for zone 1, 577 for zone 2)
#   field3 = area covered this session so far (same units, incrementing)
#   field5 = distance traveled this session (metres)
#
# field3 / field2 × 100 = real-time mowing completion %.
# When field2 is absent (session not started yet) all values are None.
_DP_SESSION = "113"

_Dp113Result = tuple[int | None, int | None, int | None]


def _decode_dp113(raw_blob: str | None) -> _Dp113Result:
    """Return (zone_total_area, session_area_covered, session_distance_m).

    All three are None when the blob is absent or the session has not started.
    """
    if not raw_blob or not isinstance(raw_blob, str):
        return None, None, None
    try:
        data = base64.b64decode(raw_blob)
    except Exception:
        return None, None, None

    f = _proto_flat(data)
    total   = f.get(2)   # zone planned area
    covered = f.get(3)   # area covered this session
    dist    = f.get(5)   # distance in metres

    # field2 must be a positive int for data to be meaningful
    if not isinstance(total, int) or total <= 0:
        return None, None, None

    return (
        total,
        covered if isinstance(covered, int) else None,
        dist    if isinstance(dist,    int) else None,
    )


# ── DP124 cloud blob: area/coverage stats ─────────────────────────────────────
# DP124 is a protobuf blob fetched from the cloud every 5 minutes.
#   field1 = total lawn area (raw units — scale TBD, likely ×0.01 m²)
#   field2 = area covered in last session (same unit)
#   field3 = map coverage score (increments ≈ +1 per mowing session; lifetime score)
# We expose field3 as a "Map Coverage" sensor.
_DP_COVERAGE = "124"


def _decode_dp124_field3(raw_blob: str | None) -> int | None:
    """Decode DP124 protobuf blob and return field3 (map coverage score)."""
    if not raw_blob or not isinstance(raw_blob, str):
        return None
    try:
        data = base64.b64decode(raw_blob)
    except Exception:
        return None
    return _proto_flat(data).get(3)  # type: ignore[return-value]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EufyMowerCoordinator = hass.data[DOMAIN][entry.entry_id]

    # Add dedicated sensors (standard + session progress + coverage)
    entities: list = [EufySensor(coordinator, entry, desc) for desc in SENSORS]
    entities.append(EufyMowingProgressSensor(coordinator, entry))
    entities.append(EufySessionDistanceSensor(coordinator, entry))
    entities.append(EufyCoverageSensor(coordinator, entry))
    entities.append(EufyTaskSensor(coordinator, entry))
    entities.append(EufyTaskPhaseSensor(coordinator, entry))
    entities.append(EufyMowingModeSensor(coordinator, entry))
    entities.append(EufyFirmwareSensor(coordinator, entry))
    async_add_entities(entities)

    # Remove raw "DPxxx" sensors that are now decoded elsewhere, and the old
    # mislabelled Mowed Area / Total Mow Time sensors.
    registry = er.async_get(hass)
    device_id = entry.data[CONF_DEVICE_ID]
    for suffix in OBSOLETE_UNIQUE_SUFFIXES:
        entity_id = registry.async_get_entity_id("sensor", DOMAIN, f"{device_id}_{suffix}")
        if entity_id:
            _LOGGER.debug("Removing obsolete entity %s", entity_id)
            registry.async_remove(entity_id)

    # Track which DP IDs already have a generic sensor so we never duplicate.
    # Seed with the DPs that have dedicated entities.
    dp_with_sensor: set[str] = set(EXCLUDED_DPS) | CLOUD_ENTITY_KEYS

    def _add_generic_sensors_for_new_dps(current_dps: dict | None = None) -> None:
        """Create EufyGenericSensor for any DP not yet covered by a sensor.

        *current_dps* is the freshly-polled dict passed by coordinator callbacks.
        Falls back to coordinator.data when called at setup time (no arg).
        """
        data = current_dps if current_dps is not None else coordinator.data
        if not data:
            return
        new_entities = []
        for dp_id in data:
            if dp_id not in dp_with_sensor:
                dp_with_sensor.add(dp_id)
                new_entities.append(EufyGenericSensor(coordinator, entry, dp_id))
        if new_entities:
            _LOGGER.debug(
                "Adding generic sensors for new DPs: %s",
                [e.name for e in new_entities],
            )
            async_add_entities(new_entities)

    # Pre-seed sensors for known DPs that may not be present in every poll.
    # Currently empty — all known DPs have dedicated entities.
    preseed_entities = []
    for dp_id in PRESEED_DPS:
        if dp_id not in dp_with_sensor:
            dp_with_sensor.add(dp_id)
            preseed_entities.append(EufyGenericSensor(coordinator, entry, dp_id))
    if preseed_entities:
        async_add_entities(preseed_entities)

    # Create sensors for all DPS already known at setup time (first refresh
    # includes both local + cloud DPS, so this catches everything in one go).
    _add_generic_sensors_for_new_dps()

    # Register with coordinator: whenever a truly new DP key appears in any future
    # poll (local or cloud), a sensor is added on-the-fly.
    coordinator.async_add_new_dp_listener(_add_generic_sensors_for_new_dps)


def _decode_blob(value: Any) -> str:
    """Attempt to decode blob/string values for human-readable output."""
    if isinstance(value, str):
        try:
            # Try base64 decode first (common for protobuf blobs)
            decoded = base64.b64decode(value)
            # Try to detect if it's ASCII text
            try:
                text = decoded.decode("utf-8")
                if all(32 <= ord(c) < 127 or c in "\n\r\t" for c in text):
                    return f"<text> {text}"
            except:
                pass

            # Return hex representation for binary data
            return f"<blob ({len(decoded)} bytes)> {decoded.hex()}"
        except (binascii.Error, ValueError):
            # Not base64, return as-is
            pass

    return str(value)


class EufySensor(CoordinatorEntity[EufyMowerCoordinator], SensorEntity):
    """A sensor that reads one DPS value."""

    entity_description: EufySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
        description: EufySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )

    @property
    def native_value(self) -> Any:
        raw = self.coordinator.data.get(self.entity_description.dp)
        if raw is None:
            return None
        if self.entity_description.dp in (DP_BLADE_USED, DP_STATION_USED):
            return round(raw / 60, 2) if isinstance(raw, (int, float)) else None
        return raw


class EufyCoverageSensor(CoordinatorEntity[EufyMowerCoordinator], SensorEntity):
    """Reads the DP124 protobuf blob and exposes field3 as map coverage %.

    DP124 is a cloud-side blob fetched every 5 minutes.  Field3 appears to
    track how much of the lawn has been mapped / visited (0–100 scale).
    The exact semantics are still being reverse-engineered.
    """

    _attr_has_entity_name = True
    _attr_name = "Map Coverage"
    _attr_entity_registry_enabled_default = False
    _attr_icon = "mdi:map-check"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_map_coverage"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )

    @property
    def native_value(self) -> int | None:
        raw_blob = self.coordinator.data.get(_DP_COVERAGE)
        return _decode_dp124_field3(raw_blob)


class EufyMowingProgressSensor(CoordinatorEntity[EufyMowerCoordinator], SensorEntity):
    """Real mowing completion % derived from DP113 session telemetry blob.

    Computes field3 (area covered this session) / field2 (zone total planned area) × 100.
    Returns None when not actively mowing (blob absent or session not started).
    """

    _attr_has_entity_name = True
    _attr_name = "Mowing Progress"
    _attr_icon = "mdi:percent"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_mowing_progress"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )

    @property
    def native_value(self) -> float | None:
        raw_blob = self.coordinator.data.get(_DP_SESSION)
        total, covered, _ = _decode_dp113(raw_blob)
        if total is None or covered is None:
            return None
        return round(covered / total * 100, 1)


class EufySessionDistanceSensor(CoordinatorEntity[EufyMowerCoordinator], SensorEntity):
    """Distance traveled in the current mowing session (metres), from DP113 field5.

    Returns None when no active session blob is available.
    """

    _attr_has_entity_name = True
    _attr_name = "Session Distance"
    _attr_icon = "mdi:map-marker-distance"
    _attr_native_unit_of_measurement = UnitOfLength.METERS
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_session_distance"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )

    @property
    def native_value(self) -> int | None:
        raw_blob = self.coordinator.data.get(_DP_SESSION)
        _, _, dist = _decode_dp113(raw_blob)
        return dist


class EufyGenericSensor(CoordinatorEntity[EufyMowerCoordinator], SensorEntity):
    """A generic sensor that exposes any DPS value."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
        dp_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._dp_id = dp_id
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_generic_{dp_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )
        self._attr_name = f"DP{dp_id}"
        self._attr_native_value = None

    @property
    def native_value(self) -> Any:
        raw = self.coordinator.data.get(self._dp_id)
        if raw is None:
            return None

        # Decode blob/string values for human readability
        return _decode_blob(raw)

    async def async_added_to_hass(self) -> None:
        """Register for coordinator updates."""
        await super().async_added_to_hass()
        self.async_on_remove(self.coordinator.async_add_listener(self._handle_update))

    def _handle_update(self) -> None:
        """Handle coordinator update."""
        self.async_write_ha_state()


# ── Decoded raw DPs ───────────────────────────────────────────────────────────

class _EufyDecodedSensor(CoordinatorEntity[EufyMowerCoordinator], SensorEntity):
    """Base for sensors decoded from a raw protobuf DP."""

    _attr_has_entity_name = True
    _key: str = ""

    def __init__(self, coordinator: EufyMowerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_{self._key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )


class EufyTaskSensor(_EufyDecodedSensor):
    """Current task from DP107 robot_status (whole lawn, zone, edge, returning…)."""

    _key = "task"
    _attr_name = "Task"
    _attr_icon = "mdi:clipboard-play-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(set(MISSIONS.values()) | {OTHER})

    @property
    def native_value(self) -> str | None:
        status = decode_robot_status(self.coordinator.data.get(DP_ROBOT_STATUS))
        return status.mission_name if status else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        status = decode_robot_status(self.coordinator.data.get(DP_ROBOT_STATUS))
        if not status:
            return None
        return {
            "state": status.state_name,
            "error": status.error,
            "mission_code": status.mission,
        }


class EufyTaskPhaseSensor(_EufyDecodedSensor):
    """Sub-step of the current task from DP107 (leaving station, relocating, saving map…)."""

    _key = "task_phase"
    _attr_name = "Task Phase"
    _attr_icon = "mdi:progress-clock"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(set(PHASES.values()) | {OTHER})

    @property
    def native_value(self) -> str | None:
        status = decode_robot_status(self.coordinator.data.get(DP_ROBOT_STATUS))
        return status.phase_name if status else None


class EufyMowingModeSensor(_EufyDecodedSensor):
    """Mowing mode selected in the Eufy app, from DP154 (entire / zone / box).

    This is the app's selection, not the running task. Starting from Home
    Assistant (DP1) is expected to use this mode — not yet confirmed.
    """

    _key = "mowing_mode"
    _attr_name = "Mowing Mode"
    _attr_icon = "mdi:texture-box"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = sorted(set(WORK_MODES.values()) | {OTHER})

    @property
    def native_value(self) -> str | None:
        return decode_work_mode(self.coordinator.data.get(DP_WORK_MODE))


class EufyFirmwareSensor(_EufyDecodedSensor):
    """Main firmware version from DP129; all component versions as attributes."""

    _key = "firmware"
    _attr_name = "Firmware"
    _attr_icon = "mdi:chip"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        versions = decode_firmware(self.coordinator.data.get(DP_FIRMWARE))
        if not versions:
            return None
        return versions.get("main") or next(iter(versions.values()))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        return decode_firmware(self.coordinator.data.get(DP_FIRMWARE))
