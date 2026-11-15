"""OSC UDP server and entity routing logic."""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from typing import Any

import warnings as _warnings

_warnings.filterwarnings(
    "ignore",
    category=SyntaxWarning,
    module=r"pythonosc\.dispatcher",
)
del _warnings

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_DEBOUNCE_MS, CONF_FILTER_RULES, CONF_PORT, DEFAULT_DEBOUNCE_MS, DEFAULT_FILTER_RULES, DEFAULT_PORT
from .mapping import build_light_call, split_address

_LOGGER = logging.getLogger(__name__)

_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def parse_filter_rules(rules_text: str) -> list[tuple[str, str]]:
    rules: list[tuple[str, str]] = []
    for line in rules_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("+"):
            rules.append(("+", line[1:].strip()))
        elif line.startswith("-"):
            rules.append(("-", line[1:].strip()))
    return rules


def validate_filter_rules(rules_text: str) -> str | None:
    for line in rules_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("+", "-")):
            return "invalid_filter_rule"
    return None


def is_address_allowed(osc_address: str, rules: list[tuple[str, str]]) -> bool:
    has_allow_rules = any(sign == "+" for sign, _ in rules)

    for sign, pattern in rules:
        if sign == "+" and fnmatch.fnmatch(osc_address, pattern):
            return True

    for sign, pattern in rules:
        if sign == "-" and fnmatch.fnmatch(osc_address, pattern):
            return False

    return not has_allow_rules


# ---------------------------------------------------------------------------
# Service-call resolver
# ---------------------------------------------------------------------------

def resolve_service_call(
    domain: str,
    entity_id: str,
    args: list[Any],
    suffix: str | None = None,
    supported: list[str] | None = None,
) -> tuple[str, str, dict[str, Any]] | None:
    """Map an OSC *domain*, *entity_id*, and argument list to an HA service call.

    For ``light`` entities, *suffix* selects the property, and *supported* is
    the entity's ``supported_color_modes`` (so we can translate e.g. ``rgb`` →
    ``hs_color`` for a bulb that doesn't advertise ``rgb``).
    """
    if domain == "light":
        result = build_light_call(suffix, args, supported)
        if result is None:
            _LOGGER.debug(
                "Cannot resolve light call for %s suffix=%r args=%s",
                entity_id, suffix, args,
            )
            return None
        service_name, data = result
        data = {"entity_id": entity_id, **data}
        return "light", service_name, data

    if not args:
        return "homeassistant", "toggle", {"entity_id": entity_id}

    value = args[0]

    if isinstance(value, bool):
        svc = "turn_on" if value else "turn_off"
        return "homeassistant", svc, {"entity_id": entity_id}

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fval = float(value)

        if domain == "media_player":
            if fval <= 0:
                return "media_player", "media_pause", {"entity_id": entity_id}
            vol_level = fval if fval <= 1.0 else fval / 100.0
            return "media_player", "volume_set", {
                "entity_id": entity_id,
                "volume_level": min(1.0, max(0.0, vol_level)),
            }

        if domain == "cover":
            if fval <= 0:
                return "cover", "close_cover", {"entity_id": entity_id}
            position = round(fval * 100) if fval <= 1.0 else round(fval)
            return "cover", "set_cover_position", {
                "entity_id": entity_id,
                "position": min(100, max(0, position)),
            }

        if domain == "fan":
            if fval <= 0:
                return "fan", "turn_off", {"entity_id": entity_id}
            pct = round(fval * 100) if fval <= 1.0 else round(fval)
            return "fan", "set_percentage", {
                "entity_id": entity_id,
                "percentage": min(100, max(1, pct)),
            }

        if domain == "input_number":
            return "input_number", "set_value", {"entity_id": entity_id, "value": fval}

        if domain == "number":
            return "number", "set_value", {"entity_id": entity_id, "value": fval}

        if domain == "climate":
            return "climate", "set_temperature", {
                "entity_id": entity_id,
                "temperature": fval,
            }

        if fval <= 0:
            return "homeassistant", "turn_off", {"entity_id": entity_id}
        return "homeassistant", "turn_on", {"entity_id": entity_id}

    if isinstance(value, str):
        kw = value.lower().strip()
        if kw in ("toggle", "t"):
            return "homeassistant", "toggle", {"entity_id": entity_id}
        if kw in ("on", "1", "true", "yes"):
            return "homeassistant", "turn_on", {"entity_id": entity_id}
        if kw in ("off", "0", "false", "no"):
            return "homeassistant", "turn_off", {"entity_id": entity_id}

    _LOGGER.debug(
        "Cannot resolve service call for entity %s with args %s", entity_id, args
    )
    return None


# ---------------------------------------------------------------------------
# OSC server class
# ---------------------------------------------------------------------------

class OSCServer:
    """Manages the async UDP OSC server and dispatches messages to HA."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._hass = hass
        self._entry = entry
        self._transport: asyncio.BaseTransport | None = None
        self._monitor_callback: Any | None = None
        self._pending_handles: dict[str, asyncio.TimerHandle] = {}
        self._pending_data: dict[str, tuple[str, str, dict]] = {}

    @property
    def port(self) -> int:
        raw = self._entry.options.get(
            CONF_PORT, self._entry.data.get(CONF_PORT, DEFAULT_PORT)
        )
        return int(raw)

    @property
    def _filter_rules(self) -> list[tuple[str, str]]:
        rules_text: str = self._entry.options.get(
            CONF_FILTER_RULES,
            self._entry.data.get(CONF_FILTER_RULES, DEFAULT_FILTER_RULES),
        )
        return parse_filter_rules(rules_text)

    @property
    def _debounce_ms(self) -> int:
        raw = self._entry.options.get(
            CONF_DEBOUNCE_MS,
            self._entry.data.get(CONF_DEBOUNCE_MS, DEFAULT_DEBOUNCE_MS),
        )
        return int(raw)

    async def start(self) -> None:
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self._handle_message, needs_reply_address=False)

        loop = asyncio.get_running_loop()
        server = AsyncIOOSCUDPServer(("0.0.0.0", self.port), dispatcher, loop)
        self._transport, _ = await server.create_serve_endpoint()
        _LOGGER.info("OSC server listening on UDP port %d", self.port)

    async def stop(self) -> None:
        for handle in self._pending_handles.values():
            handle.cancel()
        self._pending_handles.clear()
        self._pending_data.clear()
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            _LOGGER.info("OSC server stopped")

    def set_monitor_callback(self, callback: Any) -> None:
        self._monitor_callback = callback

    def clear_monitor_callback(self) -> None:
        self._monitor_callback = None

    def _schedule_debounced(
        self, canonical: str, svc_domain: str, svc_name: str, svc_data: dict
    ) -> None:
        """Queue (or merge into) the pending call for *canonical*."""
        existing = self._pending_handles.pop(canonical, None)
        if existing is not None:
            existing.cancel()

        # Merge into any pending payload so successive faders combine:
        #   /light/x/brightness 0.5  +  /light/x/rgb [1,0,0.5]  →  one call
        prev = self._pending_data.get(canonical)
        if prev is not None and prev[0] == svc_domain and prev[1] == svc_name:
            merged = {**prev[2], **svc_data}
            self._pending_data[canonical] = (svc_domain, svc_name, merged)
        else:
            self._pending_data[canonical] = (svc_domain, svc_name, svc_data)

        delay = self._debounce_ms
        if delay <= 0:
            self._fire_debounced(canonical)
            return

        handle = asyncio.get_running_loop().call_later(
            delay / 1000.0,
            self._fire_debounced,
            canonical,
        )
        self._pending_handles[canonical] = handle

    def _fire_debounced(self, canonical: str) -> None:
        self._pending_handles.pop(canonical, None)
        pending = self._pending_data.pop(canonical, None)
        if pending is None:
            return
        svc_domain, svc_name, svc_data = pending
        self._hass.async_create_task(
            self._call_service(svc_domain, svc_name, svc_data),
            name=f"ha_osc.debounced.{svc_domain}.{svc_name}",
        )

    def _handle_message(self, address: str, *args: Any) -> None:
        try:
            self._dispatch(address, list(args))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error handling OSC message %s %s", address, args)

    def _dispatch(self, address: str, args: list[Any]) -> None:
        _LOGGER.debug("OSC received: %s %s", address, args)

        base_address, suffix = split_address(address)
        parts = [p for p in base_address.split("/") if p]
        if len(parts) < 2:
            _LOGGER.debug("Malformed OSC address (need /<domain>/<entity_name>): %s", address)
            self._notify(address, args, None, False, None)
            return

        domain = parts[0].lower()
        entity_name = parts[1].lower()
        entity_id = f"{domain}.{entity_name}"
        canonical = f"/{domain}/{entity_name}"

        if not _ENTITY_ID_RE.match(entity_id):
            _LOGGER.debug("Address maps to invalid entity ID %r; ignored", entity_id)
            self._notify(address, args, None, False, None)
            return

        rules = self._filter_rules
        blocked = not is_address_allowed(canonical, rules)

        service_info: tuple | None = None
        service_str: str | None = None
        if not blocked:
            # Look up supported_color_modes so light calls can pick the right
            # attribute (rgb_color / hs_color / xy_color / rgbw_color).
            supported: list[str] = []
            if domain == "light":
                state = self._hass.states.get(entity_id)
                if state is not None:
                    supported = list(
                        state.attributes.get("supported_color_modes") or []
                    )
            service_info = resolve_service_call(
                domain, entity_id, args, suffix, supported
            )
            if service_info:
                service_str = f"{service_info[0]}.{service_info[1]}"

        self._notify(address, args, entity_id, blocked, service_str)

        if blocked:
            _LOGGER.debug("OSC address blocked by filter: %s", canonical)
            return

        if service_info is None:
            return

        svc_domain, svc_name, svc_data = service_info
        self._schedule_debounced(canonical, svc_domain, svc_name, svc_data)

    def _notify(
        self,
        address: str,
        args: list[Any],
        entity_id: str | None,
        blocked: bool,
        service: str | None,
    ) -> None:
        self._hass.bus.async_fire(
            "ha_osc_message",
            {
                "address": address,
                "args": args,
                "entity_id": entity_id,
                "blocked": blocked,
                "service": service,
                "port": self.port,
            },
        )
        if self._monitor_callback is not None:
            self._monitor_callback(address, args, entity_id, blocked, service)

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        if not self._hass.services.has_service(domain, service):
            if service in ("turn_on", "turn_off", "toggle"):
                domain = "homeassistant"
            else:
                _LOGGER.warning("Service %s.%s not available; skipping", domain, service)
                return

        try:
            await self._hass.services.async_call(domain, service, data)
            _LOGGER.debug("Called %s.%s(%s)", domain, service, data)
        except Exception as err:  # noqa: BLE001
            _LOGGER.error("Error calling %s.%s: %s", domain, service, err)