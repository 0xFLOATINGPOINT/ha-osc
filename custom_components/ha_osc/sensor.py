"""OSC Monitor sensor — tracks the most recent messages received by the server."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_PORT, DEFAULT_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)

# How many messages to keep in the rolling log attribute
_MAX_RECENT = 20


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the OSC Monitor sensor for this config entry."""
    from .osc_server import OSCServer  # avoid circular import at module level

    server: OSCServer = hass.data[DOMAIN][entry.entry_id]
    sensor = OscMonitorSensor(entry)
    server.set_monitor_callback(sensor.handle_message)
    async_add_entities([sensor])


class OscMonitorSensor(SensorEntity):
    """Sensor that shows the last received OSC message and a rolling log.

    State      : OSC address of the most recent message (e.g. "/light/kitchen")
    Attributes :
      args             – argument list of the last message
      entity_id        – resolved HA entity ID, or null for invalid/malformed addresses
      blocked          – true if the filter rules prevented the service call
      service          – service that was (or would have been) called, e.g. "light.turn_on"
      recent_messages  – list of the last 20 messages (newest first)
    """

    _attr_should_poll = False
    _attr_icon = "mdi:radio-tower"

    def __init__(self, entry: ConfigEntry) -> None:
        port = int(entry.options.get(CONF_PORT, entry.data.get(CONF_PORT, DEFAULT_PORT)))
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_monitor"
        self._attr_name = f"OSC Monitor (port {port})"
        self._attr_native_value: str = "—"
        self._last_args: list = []
        self._last_entity: str | None = None
        self._last_blocked: bool = False
        self._last_service: str | None = None
        self._recent: list[dict[str, Any]] = []

    @property
    def device_info(self) -> DeviceInfo:
        port = int(
            self._entry.options.get(
                CONF_PORT, self._entry.data.get(CONF_PORT, DEFAULT_PORT)
            )
        )
        return DeviceInfo(
            identifiers={(DOMAIN, self._entry.entry_id)},
            name=f"OSC Server (port {port})",
            manufacturer="Open Sound Control",
            entry_type=DeviceEntryType.SERVICE,
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "args": self._last_args,
            "entity_id": self._last_entity,
            "blocked": self._last_blocked,
            "service": self._last_service,
            "recent_messages": self._recent,
        }

    @callback
    def handle_message(
        self,
        address: str,
        args: list,
        entity_id: str | None,
        blocked: bool,
        service: str | None,
    ) -> None:
        """Update sensor state whenever an OSC message arrives."""
        self._attr_native_value = address
        self._last_args = args
        self._last_entity = entity_id
        self._last_blocked = blocked
        self._last_service = service
        self._recent = [
            {
                "address": address,
                "args": args,
                "entity_id": entity_id,
                "blocked": blocked,
                "service": service,
                "time": datetime.now().isoformat(timespec="seconds"),
            },
            *self._recent,
        ][:_MAX_RECENT]
        self.async_write_ha_state()

    async def async_will_remove_from_hass(self) -> None:
        """Deregister the callback when the entity is removed."""
        from .osc_server import OSCServer

        server: OSCServer | None = self.hass.data.get(DOMAIN, {}).get(
            self._entry.entry_id
        )
        if server is not None:
            server.clear_monitor_callback()
