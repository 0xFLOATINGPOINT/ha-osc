"""OSC UDP server and entity routing logic."""
from __future__ import annotations

import asyncio
import fnmatch
import logging
import re
from typing import Any

from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import AsyncIOOSCUDPServer

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_FILTER_RULES, CONF_PORT, DEFAULT_FILTER_RULES, DEFAULT_PORT

_LOGGER = logging.getLogger(__name__)

# Valid HA entity ID: domain.object_id
_ENTITY_ID_RE = re.compile(r"^[a-z_][a-z0-9_]*\.[a-z0-9_]+$")


# ---------------------------------------------------------------------------
# Filter helpers
# ---------------------------------------------------------------------------

def parse_filter_rules(rules_text: str) -> list[tuple[str, str]]:
    """Parse filter rules from a multiline string.

    Each non-blank, non-comment line must start with '+' (allow) or '-' (deny),
    followed by a glob pattern matching an OSC address, e.g. ``-/light/*``.
    Lines starting with ``#`` are treated as comments.

    Returns a list of ``(sign, pattern)`` tuples in source order.
    """
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
    """Return an error key if *rules_text* contains invalid lines, else None."""
    for line in rules_text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if not line.startswith(("+", "-")):
            return "invalid_filter_rule"
    return None


def is_address_allowed(osc_address: str, rules: list[tuple[str, str]]) -> bool:
    """Decide whether *osc_address* passes the current filter rules.

    Priority model
    --------------
    1. ``+`` rules are checked first and **always win** — a ``+`` match allows
       the address regardless of any ``-`` rules.
    2. ``-`` rules are checked next — a ``-`` match blocks the address.
    3. Default behaviour depends on whether *any* ``+`` rule exists:
       - If **no** ``+`` rules → **allow** (blacklist mode).
       - If **any** ``+`` rule → **deny** (whitelist mode).

    This means:
    - Only ``-`` rules → everything allowed except blocked patterns.
    - Any ``+`` rule present → nothing allowed except explicitly whitelisted.
    - Mix of both → ``+`` exceptions punch holes through ``-`` blocks.
    """
    has_allow_rules = any(sign == "+" for sign, _ in rules)

    # + rules have highest priority
    for sign, pattern in rules:
        if sign == "+" and fnmatch.fnmatch(osc_address, pattern):
            return True

    # - rules block
    for sign, pattern in rules:
        if sign == "-" and fnmatch.fnmatch(osc_address, pattern):
            return False

    # Default: deny when whitelist mode is active, allow otherwise
    return not has_allow_rules


# ---------------------------------------------------------------------------
# Service-call resolver
# ---------------------------------------------------------------------------

def resolve_service_call(
    domain: str, entity_id: str, args: list[Any]
) -> tuple[str, str, dict[str, Any]] | None:
    """Map an OSC *domain*, *entity_id*, and argument list to an HA service call.

    Returns ``(service_domain, service_name, service_data)`` or ``None`` if the
    arguments cannot be interpreted.

    Value conventions
    -----------------
    - No arguments → toggle
    - ``bool`` / ``int 0``  → turn_off
    - ``int 1``             → turn_on
    - ``float 0.0–1.0``    → percentage / level (domain-aware)
    - ``float/int > 1``    → absolute value (brightness 0-255, temp, etc.)
    - ``str``              → "on"/"off"/"toggle" keywords
    """
    if not args:
        return "homeassistant", "toggle", {"entity_id": entity_id}

    value = args[0]

    # --- Boolean -----------------------------------------------------------
    if isinstance(value, bool):
        svc = "turn_on" if value else "turn_off"
        return "homeassistant", svc, {"entity_id": entity_id}

    # --- Numeric (int / float) ---------------------------------------------
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        fval = float(value)

        if domain == "light":
            if fval <= 0:
                return "light", "turn_off", {"entity_id": entity_id}
            if fval <= 1.0:
                # 0.0–1.0 treated as brightness percentage
                return "light", "turn_on", {
                    "entity_id": entity_id,
                    "brightness_pct": min(100, max(1, round(fval * 100))),
                }
            # >1 treated as raw brightness (0–255)
            return "light", "turn_on", {
                "entity_id": entity_id,
                "brightness": min(255, max(1, round(fval))),
            }

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

        # Generic on/off for everything else
        if fval <= 0:
            return "homeassistant", "turn_off", {"entity_id": entity_id}
        return "homeassistant", "turn_on", {"entity_id": entity_id}

    # --- String keywords ---------------------------------------------------
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

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def port(self) -> int:
        """Return the effective port (options override data)."""
        raw = self._entry.options.get(
            CONF_PORT, self._entry.data.get(CONF_PORT, DEFAULT_PORT)
        )
        return int(raw)

    @property
    def _filter_rules(self) -> list[tuple[str, str]]:
        """Return parsed filter rules, re-read from entry on every access."""
        rules_text: str = self._entry.options.get(
            CONF_FILTER_RULES,
            self._entry.data.get(CONF_FILTER_RULES, DEFAULT_FILTER_RULES),
        )
        return parse_filter_rules(rules_text)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Bind the UDP socket and begin listening."""
        dispatcher = Dispatcher()
        dispatcher.set_default_handler(self._handle_message, needs_reply_address=False)

        loop = asyncio.get_running_loop()
        server = AsyncIOOSCUDPServer(("0.0.0.0", self.port), dispatcher, loop)
        self._transport, _ = await server.create_serve_endpoint()
        _LOGGER.info("OSC server listening on UDP port %d", self.port)

    async def stop(self) -> None:
        """Close the UDP socket."""
        if self._transport is not None:
            self._transport.close()
            self._transport = None
            _LOGGER.info("OSC server stopped")

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def _handle_message(self, address: str, *args: Any) -> None:
        """Called by python-osc for every incoming UDP datagram."""
        try:
            self._dispatch(address, list(args))
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error handling OSC message %s %s", address, args)

    def _dispatch(self, address: str, args: list[Any]) -> None:
        """Parse address, apply filters, and schedule the HA service call."""
        _LOGGER.debug("OSC received: %s %s", address, args)

        # Address must be /<domain>/<entity_name>
        parts = [p for p in address.split("/") if p]
        if len(parts) < 2:
            _LOGGER.debug("Ignoring malformed OSC address (need at least 2 segments): %s", address)
            return

        domain = parts[0].lower()
        entity_name = parts[1].lower()
        entity_id = f"{domain}.{entity_name}"

        if not _ENTITY_ID_RE.match(entity_id):
            _LOGGER.debug("Ignoring address that maps to invalid entity ID %r", entity_id)
            return

        # Normalise address to /<domain>/<entity_name> for filter matching
        canonical = f"/{domain}/{entity_name}"
        rules = self._filter_rules

        if not is_address_allowed(canonical, rules):
            _LOGGER.debug("OSC address blocked by filter rules: %s", canonical)
            return

        service_info = resolve_service_call(domain, entity_id, args)
        if service_info is None:
            return

        svc_domain, svc_name, svc_data = service_info
        self._hass.async_create_task(
            self._call_service(svc_domain, svc_name, svc_data),
            name=f"ha_osc.{svc_domain}.{svc_name}",
        )

    async def _call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> None:
        """Execute the resolved HA service call."""
        # Fall back to homeassistant domain if the specific domain lacks the service
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
