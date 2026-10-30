"""Eufy Robomow — Home Assistant integration for Eufy E-series robot mowers (E15, E18).

Protocol: Tuya local v3.5 over TCP port 6668.
Requires: device_id + local_key (obtainable via eufy-clean-local-key-grabber).

Optional: Eufy account email + password unlock cloud-managed settings
(edge distance, path distance, travel speed, blade speed) via DP155.
"""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    CONF_DEVICE_ID,
    CONF_LOCAL_KEY,
    CONF_EUFY_EMAIL,
    CONF_EUFY_PASSWORD,
    CONF_MODEL,
    CONF_SERIAL,
    CONF_FIRMWARE,
    DP_DEVICE_INFO,
    model_from_serial,
    parse_device_info,
)
from .coordinator import EufyMowerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.DEVICE_TRACKER,
    Platform.NUMBER,
    Platform.SELECT,   # no-op when no cloud client (handled in select.py)
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Eufy Robomow from a config entry."""
    email    = entry.data.get(CONF_EUFY_EMAIL, "").strip()
    password = entry.data.get(CONF_EUFY_PASSWORD, "").strip()

    cloud_client = None
    if email and password:
        from .cloud import EufyCloudClient  # lazy import — avoids loading cryptography at startup
        cloud_client = EufyCloudClient(
            email=email,
            password=password,
            device_id=entry.data[CONF_DEVICE_ID],
        )
        _LOGGER.debug("Cloud client created for device %s", entry.data[CONF_DEVICE_ID])

    coordinator = EufyMowerCoordinator(
        hass,
        host=entry.data[CONF_HOST],
        device_id=entry.data[CONF_DEVICE_ID],
        local_key=entry.data[CONF_LOCAL_KEY],
        cloud_client=cloud_client,
    )

    # Initial data fetch — raises ConfigEntryNotReady if the mower is unreachable
    await coordinator.async_config_entry_first_refresh()

    # Identify the model from the serial number in DP102 (device info, cloud
    # only). The serial embeds the SKU (AMP2801… = E18), which is more reliable
    # than the cloud product name. Runs until a model is found.
    _update_device_identity(hass, entry, coordinator.data or {})

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


def _update_device_identity(hass: HomeAssistant, entry: ConfigEntry, data: dict) -> None:
    """Store model, serial and firmware parsed from DP102. Never raises."""
    try:
        info = parse_device_info(data.get(DP_DEVICE_INFO))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not parse DP102 device info: %s", exc)
        return
    if not info:
        return

    new = dict(entry.data)
    model = model_from_serial(info.get("serial"))
    if model and entry.data.get(CONF_MODEL) != model:
        _LOGGER.info("Detected Eufy mower model %s from serial number", model)
        new[CONF_MODEL] = model
    for key, conf in (("serial", CONF_SERIAL), ("firmware", CONF_FIRMWARE)):
        if info.get(key):
            new[conf] = info[key]
    if new != entry.data:
        hass.config_entries.async_update_entry(entry, data=new)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
