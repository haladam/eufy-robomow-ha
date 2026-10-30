"""Lawn mower entity for Eufy Robomow."""
from __future__ import annotations

import logging

from homeassistant.components.lawn_mower import (
    LawnMowerActivity,
    LawnMowerEntity,
    LawnMowerEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_MODEL,
    CONF_PRODUCT_NAME,
    CONF_SERIAL,
    CONF_FIRMWARE,
    DEFAULT_MODEL,
    DP_TASK_ACTIVE,
    DP_PAUSED,
    CMD_START,
    CMD_PAUSE,
    CMD_RESUME,
    CMD_DOCK,
)
from .coordinator import EufyMowerCoordinator
from .decoders import (
    DP_BATTERY_STATUS,
    DP_MAP_SAVE_PROGRESS,
    DP_ROBOT_STATUS,
    decode_battery_status,
    decode_robot_status,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: EufyMowerCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EufyRobomowEntity(coordinator, entry)])


class EufyRobomowEntity(CoordinatorEntity[EufyMowerCoordinator], LawnMowerEntity):
    """Represents a Eufy E-series (E15/E18) robot mower."""

    _attr_has_entity_name = True
    _attr_translation_key = "lawn_mower"
    _attr_icon = "mdi:robot-mower"
    _attr_supported_features = (
        LawnMowerEntityFeature.START_MOWING
        | LawnMowerEntityFeature.PAUSE
        | LawnMowerEntityFeature.DOCK
    )

    def __init__(
        self,
        coordinator: EufyMowerCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.data[CONF_DEVICE_ID]}_mower"
        model = entry.data.get(CONF_MODEL) or DEFAULT_MODEL
        product_name = entry.data.get(CONF_PRODUCT_NAME) or None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_DEVICE_ID])},
            name=f"Eufy Robomow {model}",
            manufacturer="Eufy (Anker)",
            model=model,
            model_id=product_name if product_name and product_name != model else None,
            serial_number=entry.data.get(CONF_SERIAL) or None,
            sw_version=entry.data.get(CONF_FIRMWARE) or None,
        )

    # ── activity ──────────────────────────────────────────────────────────────

    @property
    def activity(self) -> LawnMowerActivity:
        dps = self.coordinator.data
        dp1   = dps.get(DP_TASK_ACTIVE, False)   # fresh (local)
        dp2   = dps.get(DP_PAUSED,      False)   # fresh (local)
        mission = decode_robot_status(dps.get(DP_ROBOT_STATUS))
        battery = decode_battery_status(dps.get(DP_BATTERY_STATUS))
        on_station = battery.charger_connected if battery else None

        if mission is not None and mission.error:
            return LawnMowerActivity.ERROR

        # Paused: task active but movement stopped
        if dp1 and dp2:
            return LawnMowerActivity.PAUSED

        # Driving home: DP107 says "recharge" and the charger has no contact yet.
        # Sending the mower home sets DP1=False, so check this before DP1.
        if mission is not None and mission.is_returning and on_station is False:
            return LawnMowerActivity.RETURNING

        if dp1:
            # DP1 stays True while the mower docks mid-session to recharge.
            if on_station and (mission is None or not mission.is_mowing):
                return LawnMowerActivity.DOCKED
            # Legacy fallback when DP107 is unavailable: DP118 (map save) runs
            # between the end of mowing and the drive home.
            if mission is None and 5 <= dps.get(DP_MAP_SAVE_PROGRESS, 0) < 100:
                return LawnMowerActivity.RETURNING
            return LawnMowerActivity.MOWING

        return LawnMowerActivity.DOCKED

    # ── commands ──────────────────────────────────────────────────────────────

    async def async_start_mowing(self) -> None:
        """Start or resume mowing."""
        current = self.activity
        if current == LawnMowerActivity.PAUSED:
            dp, val = CMD_RESUME
        else:
            dp, val = CMD_START
        await self.coordinator.async_send_command(dp, val)

    async def async_pause(self) -> None:
        """Pause the mowing session."""
        dp, val = CMD_PAUSE
        await self.coordinator.async_send_command(dp, val)

    async def async_dock(self) -> None:
        """Stop mowing and return to base."""
        dp, val = CMD_DOCK
        await self.coordinator.async_send_command(dp, val)
