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
    CONF_PRODUCT_NAME,
    DEFAULT_MODEL,
    detect_model,
)
from .coordinator import EufyMowerCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.LAWN_MOWER,
    Platform.SENSOR,
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

    # Entries created by older versions have no stored model (it used to be
    # hardcoded to "E15"). Look it up once from the cloud and persist it.
    if cloud_client is not None and not entry.data.get(CONF_MODEL):
        await _async_backfill_model(hass, entry, cloud_client)

    coordinator = EufyMowerCoordinator(
        hass,
        host=entry.data[CONF_HOST],
        device_id=entry.data[CONF_DEVICE_ID],
        local_key=entry.data[CONF_LOCAL_KEY],
        cloud_client=cloud_client,
    )

    # Initial data fetch — raises ConfigEntryNotReady if the mower is unreachable
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def _async_backfill_model(hass: HomeAssistant, entry: ConfigEntry, cloud_client) -> None:
    """Detect and store the model for an existing entry. Never raises."""
    device_id = entry.data[CONF_DEVICE_ID]
    try:
        devices = await hass.async_add_executor_job(cloud_client.list_all_devices)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Model detection skipped (cloud unavailable): %s", exc)
        return

    device = next((d for d in devices if d.get("devId") == device_id), None)
    if device is None:
        _LOGGER.debug("Model detection: device %s not found in cloud list", device_id)
        return

    model = detect_model(device)
    _LOGGER.info(
        "Detected Eufy mower model %s (productName=%r)",
        model or "unknown", device.get("productName"),
    )
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_MODEL: model or DEFAULT_MODEL,
            CONF_PRODUCT_NAME: device.get("productName") or "",
        },
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unload_ok
