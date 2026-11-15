"""Address-suffix parsing and light service-call helpers.

OSC addresses may carry a trailing suffix that selects which light property
to control:

    /light/kitchen                   legacy single-arg brightness / on / off
    /light/kitchen/brightness        0.0–1.0  or 0–255
    /light/kitchen/rgb               0.0–1.0 ×3  or 0–255 ×3
    /light/kitchen/rgbw              0.0–1.0 ×4  or 0–255 ×4
    /light/kitchen/hs                hue, saturation (0–1 floats or raw degrees/%)
    /light/kitchen/color_temp        0.0–1.0 normalized mireds, or raw mireds
    /light/kitchen/temp_kelvin       Kelvin

The caller may pass the target entity's ``supported_color_modes`` list so we
emit only the attribute the light actually understands (e.g. ``hs_color``
instead of ``rgb_color`` for a bulb that only advertises ``hs``).
"""
from __future__ import annotations

from typing import Any, Iterable

from homeassistant.util import color as color_util

# Suffixes recognized on light addresses.
LIGHT_SUFFIXES: tuple[str, ...] = (
    "brightness",
    "rgb",
    "rgbw",
    "hs",
    "color_temp",
    "temp_kelvin",
)

# Mired range used when a color_temp value is normalized to 0.0–1.0.
_MIRED_MIN = 153
_MIRED_MAX = 500


def split_address(address: str) -> tuple[str, str | None]:
    """Split an OSC address into ``(base, suffix)``."""
    stripped = address.rstrip("/")
    parts = stripped.split("/")
    if len(parts) >= 4 and parts[-1] in LIGHT_SUFFIXES:
        return "/".join(parts[:-1]), parts[-1]
    return stripped, None


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

def _to_byte(value: Any) -> int:
    """Normalize one RGB/W component to 0–255."""
    if isinstance(value, bool):
        return 255 if value else 0
    f = float(value)
    if 0.0 <= f <= 1.0 and not isinstance(value, int):
        return max(0, min(255, round(f * 255)))
    return max(0, min(255, round(f)))


def _to_percent(value: Any) -> int:
    if isinstance(value, bool):
        return 100 if value else 0
    f = float(value)
    pct = round(f * 100) if (0.0 <= f <= 1.0 and not isinstance(value, int)) else round(f)
    return max(1, min(100, pct))


def _to_brightness(value: Any) -> int:
    if isinstance(value, bool):
        return 255 if value else 0
    f = float(value)
    if 0.0 <= f <= 1.0 and not isinstance(value, int):
        return max(1, min(255, round(f * 255)))
    return max(1, min(255, round(f)))


# ---------------------------------------------------------------------------
# Color attribute selection
# ---------------------------------------------------------------------------

def _pick_color_attr(supported: Iterable[str]) -> str:
    """Pick the best color attribute given a light's supported_color_modes."""
    supported = set(supported or ())
    if "rgbw" in supported:
        return "rgbw_color"
    if "rgbww" in supported:
        return "rgbww_color"
    if "rgb" in supported:
        return "rgb_color"
    if "hs" in supported:
        return "hs_color"
    if "xy" in supported:
        return "xy_color"
    # Unknown: emit rgb_color. HA translates this only if the platform allows it,
    # so this is a last resort.
    return "rgb_color"


def _emit_color(rgb: list[int], supported: Iterable[str]) -> dict[str, Any]:
    """Return the correct color kwarg for the light's supported modes."""
    attr = _pick_color_attr(supported)
    if attr == "hs_color":
        return {attr: list(color_util.color_RGB_to_hs(*rgb))}
    if attr == "xy_color":
        return {attr: list(color_util.color_RGB_to_xy(*rgb))}
    if attr == "rgb_color":
        return {attr: rgb}
    # rgbw / rgbww: use the RGB values plus white from the 4th arg. Caller must
    # have handled that; this branch handles rgbw/rgbww built from RGB only.
    return {attr: rgb + [0] * (4 - len(rgb))}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_light_call(
    suffix: str | None,
    args: list[Any],
    supported: Iterable[str] | None = None,
) -> tuple[str, dict[str, Any]] | None:
    """Return ``(service_name, data)`` for a light call, or None on failure.

    ``data`` does not include ``entity_id``; the caller adds it.

    *supported* is the target light's ``supported_color_modes`` list, if
    available. When it's provided we pick the right color attribute; when it's
    ``None`` we default to ``rgb_color``.
    """
    supported = list(supported or ())

    # ---------- No suffix: legacy brightness / on / off -----------------
    if suffix is None:
        if not args:
            return "toggle", {}
        if len(args) == 1:
            v = args[0]
            if isinstance(v, bool):
                return ("turn_on" if v else "turn_off"), {}
            if isinstance(v, (int, float)):
                f = float(v)
                if f <= 0:
                    return "turn_off", {}
                if isinstance(v, int) and v == 1:
                    return "turn_on", {}
                if 0.0 < f <= 1.0:
                    return "turn_on", {"brightness_pct": _to_percent(v)}
                return "turn_on", {"brightness": _to_brightness(v)}
            if isinstance(v, str):
                kw = v.lower().strip()
                if kw in ("on", "1", "true", "yes"):
                    return "turn_on", {}
                if kw in ("off", "0", "false", "no"):
                    return "turn_off", {}
                if kw in ("toggle", "t"):
                    return "toggle", {}
        if len(args) == 3:
            rgb = [_to_byte(a) for a in args]
            return "turn_on", _emit_color(rgb, supported)
        return None

    # ---------- Suffix-aware branches -----------------------------------
    if suffix == "brightness":
        if len(args) < 1:
            return None
        v = args[0]
        if 0.0 <= float(v) <= 1.0 and not isinstance(v, int):
            return "turn_on", {"brightness_pct": _to_percent(v)}
        return "turn_on", {"brightness": _to_brightness(v)}

    if suffix == "rgb":
        if len(args) < 3:
            return None
        rgb = [_to_byte(a) for a in args[:3]]
        return "turn_on", _emit_color(rgb, supported)

    if suffix == "rgbw":
        if len(args) < 3:
            return None
        rgb = [_to_byte(a) for a in args[:3]]
        # Only emit rgbw_color when the light supports it AND we have a 4th arg.
        if len(args) >= 4 and any(m in supported for m in ("rgbw", "rgbww")):
            attr = "rgbww_color" if "rgbww" in supported else "rgbw_color"
            return "turn_on", {attr: rgb + [_to_byte(args[3])]}
        # Fall back to whatever the light does support.
        return "turn_on", _emit_color(rgb, supported)

    if suffix == "hs":
        if len(args) < 2:
            return None
        h, s = args[0], args[1]
        hue = float(h) * 360.0 if (0.0 <= float(h) <= 1.0 and not isinstance(h, int)) else float(h)
        sat = float(s) * 100.0 if (0.0 <= float(s) <= 1.0 and not isinstance(s, int)) else float(s)
        if "hs" in supported:
            return "turn_on", {"hs_color": [hue, sat]}
        # Convert HS → RGB and re-emit through the best attr for this light.
        r, g, b = color_util.color_hs_to_RGB(hue, sat)
        return "turn_on", _emit_color([r, g, b], supported)

    if suffix == "color_temp":
        if len(args) < 1:
            return None
        v = args[0]
        f = float(v)
        if 0.0 <= f <= 1.0 and not isinstance(v, int):
            mired = round(_MIRED_MAX - f * (_MIRED_MAX - _MIRED_MIN))
        else:
            mired = round(f)
        # Prefer Kelvin on modern HA; fall back to mireds.
        kelvin = color_util.color_temperature_mired_to_kelvin(mired)
        if any(m == "color_temp" for m in supported):
            # Emit BOTH: color_temp_kelvin for HA ≥ 2024.11, color_temp for older.
            return "turn_on", {
                "color_temp_kelvin": kelvin,
                "color_temp": mired,
            }
        return "turn_on", {"color_temp_kelvin": kelvin}

    if suffix == "temp_kelvin":
        if len(args) < 1:
            return None
        kelvin = int(args[0])
        out: dict[str, Any] = {"color_temp_kelvin": kelvin}
        # Also send mireds for HA < 2024.11 / integrations that still read it.
        out["color_temp"] = color_util.color_temperature_kelvin_to_mired(kelvin)
        return "turn_on", out

    return None