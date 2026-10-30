"""Binary sensors for Eufy Robomow, decoded from status DPs."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, CONF_DEVICE_ID
from .coordinator import EufyMowerCoordinator
from .decoders import (
    DP_BATTERY_STATUS,
    DP_CELLULAR_ENABLED,
    DP_EDGE_TRIM,
    DP_GPS_ANTI_THEFT,
    DP_ROBOT_STATUS,
    DP_WORK_ANGLE,
    DP_WORK_ANGLE_ENABLED,
    decode_battery_status,
    decode_robot_status,
)


def _bool_dp(dp: str) -> Callable[[dict], bool | None]:
    def read(data: dict) -> bool | None:
        value = data.get(dp)
        return value if isinstance(value, bool) else None
    return read


def _on_station(data: dict) -> bool | None:
    status = decode_battery_status(data.get(DP_BATTERY_STATUS))
    return status.charger_connected if status else None


def _problem(data: dict) -> bool | None:
    status = decode_robot_status(data.get(DP_ROBOT_STATUS))
    return status.error if status else None


def _work_angle_attrs(data: dict) -> dict[str, Any] | None:
    angle = data.get(DP_WORK_ANGLE)
    return {"angle": angle} if isinstance(angle, int) else None


@dataclass(frozen=True, kw_only=True)
class EufyBinarySensorDescription(BinarySensorEntityDescription):
    value_fn: Callable[[dict], bool | None]
    attrs_fn: Callable[[dict], dict[str, Any] | None] | None = None


BINARY_SENSORS: tuple[EufyBinarySensorDescription, ...] = (
    EufyBinarySensorDescription(
        # DP108 battery_status field 2: charger contact
        key="on_station",
        name="On Charging Station",
        device_class=BinarySensorDeviceClass.PLUG,
        icon="mdi:home-lightning-bolt",
        value_fn=_on_station,
    ),
    EufyBinarySensorDescription(
        # DP107 robot_status field 5: error flag
        key="problem",
        name="Problem",
        device_class=BinarySensorDeviceClass.PROBLEM,
        value_fn=_problem,
    ),
    EufyBinarySensorDescription(
        key="edge_trim",
        name="Edge Trimming",
        icon="mdi:border-outside",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bool_dp(DP_EDGE_TRIM),
    ),
    EufyBinarySensorDescription(
        key="work_angle_enabled",
        name="Fixed Mowing Angle",
        icon="mdi:angle-acute",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bool_dp(DP_WORK_ANGLE_ENABLED),
        attrs_fn=_work_angle_attrs,
    ),
    EufyBinarySensorDescription(
        key="cellular_enabled",
        name="Cellular",
        icon="mdi:signal-4g",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bool_dp(DP_CELLULAR_ENABLED),
    ),
    EufyBinarySensorDescription(
        key="gps_anti_theft",
        name="GPS Anti-Theft",
        icon="mdi:shield-lock-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_bool_dp(DP_GPS_ANTI_THEFT),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EufyMowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EufyBinarySensor(coordinator, entry, desc) for desc in BINARY_SENSORS
    )


class EufyBinarySensor(CoordinatorEntity[EufyMowerCoordinator], BinarySensorEntity):
    entity_description: EufyBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
        description: EufyBinarySensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
        )

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data or {})

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if self.entity_description.attrs_fn is None:
            return None
        return self.entity_description.attrs_fn(self.coordinator.data or {})
