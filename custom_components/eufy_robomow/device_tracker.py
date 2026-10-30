"""GPS device tracker for Eufy Robomow (DP142 gps_info, anti-theft location)."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.device_tracker import SourceType
from homeassistant.components.device_tracker.config_entry import TrackerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_DEVICE_ID
from .coordinator import EufyMowerCoordinator
from .decoders import DP_GPS_INFO, GpsFix, decode_gps_info


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EufyMowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EufyGpsTracker(coordinator, entry)])


class EufyGpsTracker(CoordinatorEntity[EufyMowerCoordinator], TrackerEntity):
    """Mower position as reported by its GPS (cloud-polled, ~5 min).

    The mower reports a coarse GPS fix for theft tracking; it is not precise
    enough to follow it around the lawn.
    """

    _attr_has_entity_name = True
    _attr_name = "Location"
    _attr_icon = "mdi:robot-mower"

    def __init__(self, coordinator: EufyMowerCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_gps"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )

    @property
    def _fix(self) -> GpsFix | None:
        return decode_gps_info((self.coordinator.data or {}).get(DP_GPS_INFO))

    @property
    def source_type(self) -> SourceType:
        return SourceType.GPS

    @property
    def latitude(self) -> float | None:
        fix = self._fix
        return fix.latitude if fix else None

    @property
    def longitude(self) -> float | None:
        fix = self._fix
        return fix.longitude if fix else None

    @property
    def location_accuracy(self) -> float:
        fix = self._fix
        return float(fix.accuracy) if fix and fix.accuracy else 0.0

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fix = self._fix
        if not fix or not fix.timestamp:
            return None
        return {
            "last_fix": datetime.fromtimestamp(fix.timestamp, tz=timezone.utc).isoformat(),
        }
