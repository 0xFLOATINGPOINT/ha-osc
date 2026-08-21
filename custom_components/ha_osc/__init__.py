"""The OSC (Open Sound Control) integration."""
from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import DOMAIN
from .osc_server import OSCServer

_LOGGER = logging.getLogger(__name__)

# This integration provides no HA platforms (sensors, switches, etc.).
# It only runs an OSC server that calls services on existing entities.
PLATFORMS: list[str] = []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OSC from a config entry."""
    server = OSCServer(hass, entry)

    try:
        await server.start()
    except OSError as err:
        raise ConfigEntryNotReady(
            f"Cannot bind OSC server to port {server.port}: {err}"
        ) from err

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = server

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop the OSC server and clean up."""
    server: OSCServer = hass.data[DOMAIN].pop(entry.entry_id)
    await server.stop()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when the user saves new options."""
    await hass.config_entries.async_reload(entry.entry_id)
