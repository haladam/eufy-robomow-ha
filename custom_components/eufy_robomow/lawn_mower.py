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
    DEFAULT_MODEL,
    DP_TASK_ACTIVE,
    DP_PAUSED,
    DP_PROGRESS,
    CMD_START,
    CMD_PAUSE,
    CMD_RESUME,
    CMD_DOCK,
    RETURNING_THRESHOLD,
)
from .coordinator import EufyMowerCoordinator

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
        )

    # ── activity ──────────────────────────────────────────────────────────────

    @property
    def activity(self) -> LawnMowerActivity:
        dps = self.coordinator.data
        dp1   = dps.get(DP_TASK_ACTIVE, False)
        dp2   = dps.get(DP_PAUSED,      False)
        dp118 = dps.get(DP_PROGRESS,    0)

        # Paused: task active but movement stopped
        if dp1 and dp2:
            return LawnMowerActivity.PAUSED

        if dp1 and not dp2:
            # DP118 5–99 → mower returning to base
            # DP118=100 while DP1=True means briefly docked mid-session for
            # charging (will resume); treat as MOWING, not RETURNING.
            if RETURNING_THRESHOLD <= dp118 < 100:
                try:
                    return LawnMowerActivity.RETURNING
                except AttributeError:
                    return LawnMowerActivity.MOWING
            return LawnMowerActivity.MOWING

        # DP1 absent or False → no active session → docked / idle
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
