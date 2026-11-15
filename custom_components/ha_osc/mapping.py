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

Non-light domains ignore suffixes.
"""
from __future__ import annotations

from typing import Any

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
# 153 mired ≈ 6500 K (coolest), 500 mired ≈ 2000 K (warmest).
_MIRED_MIN = 153
_MIRED_MAX = 500


def split_address(address: str) -> tuple[str, str | None]:
    """Split an OSC address into ``(base, suffix)``.

    ``suffix`` is one of :data:`LIGHT_SUFFIXES` if present as the final path
    segment, otherwise ``None``. ``base`` always has no trailing slash.

    Examples
    --------
    >>> split_address("/light/kitchen")
    ('/light/kitchen', None)
    >>> split_address("/light/kitchen/rgb")
    ('/light/kitchen', 'rgb')
    >>> split_address("/switch/garden_pump")
    ('/switch/garden_pump', None)
    """
    stripped = address.rstrip("/")
    parts = stripped.split("/")
    if len(parts) >= 4 and parts[-1] in LIGHT_SUFFIXES:
        return "/".join(parts[:-1]), parts[-1]
    return stripped, None


def _to_byte(value: Any) -> int:
    """Normalize one RGB/W component to 0–255.

    Floats in 0.0–1.0 are treated as normalized and scaled by 255.
    Ints and floats > 1 are clamped to 0–255 as-is.
    """
    if isinstance(value, bool):
        return 255 if value else 0
    f = float(value)
    if 0.0 <= f <= 1.0 and not isinstance(value, int):
        return max(0, min(255, round(f * 255)))
    return max(0, min(255, round(f)))


def _to_percent(value: Any) -> int:
    """Normalize a 0.0–1.0 float or 0–100 int to a percentage."""
    if isinstance(value, bool):
        return 100 if value else 0
    f = float(value)
    pct = round(f * 100) if (0.0 <= f <= 1.0 and not isinstance(value, int)) else round(f)
    return max(1, min(100, pct))


def _to_brightness(value: Any) -> int:
    """Normalize a 0.0–1.0 float or 0–255 int to a raw brightness value."""
    if isinstance(value, bool):
        return 255 if value else 0
    f = float(value)
    if 0.0 <= f <= 1.0 and not isinstance(value, int):
        raw = round(f * 255)
    else:
        raw = round(f)
    return max(1, min(255, raw))


def build_light_call(
    suffix: str | None, args: list[Any]
) -> tuple[str, dict[str, Any]] | None:
    """Return ``(service_name, data)`` for a light call, or None on failure.

    ``data`` does **not** include ``entity_id``; the caller adds it.

    Without a suffix, single-argument behavior is unchanged from v1.0.0:
    0/False → off, 1/True → on, 0.0–1.0 → brightness %, > 1 → raw brightness.
    Three arguments without a suffix are interpreted as RGB for convenience.
    """
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
            return "turn_on", {"rgb_color": [_to_byte(a) for a in args]}
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
        return "turn_on", {"rgb_color": [_to_byte(a) for a in args[:3]]}

    if suffix == "rgbw":
        if len(args) < 3:
            return None
        rgb = [_to_byte(a) for a in args[:3]]
        if len(args) >= 4:
            return "turn_on", {"rgbw_color": rgb + [_to_byte(args[3])]}
        return "turn_on", {"rgb_color": rgb}

    if suffix == "hs":
        if len(args) < 2:
            return None
        h, s = args[0], args[1]
        hue = float(h) * 360.0 if (0.0 <= float(h) <= 1.0 and not isinstance(h, int)) else float(h)
        sat = float(s) * 100.0 if (0.0 <= float(s) <= 1.0 and not isinstance(s, int)) else float(s)
        return "turn_on", {"hs_color": [hue, sat]}

    if suffix == "color_temp":
        if len(args) < 1:
            return None
        v = args[0]
        f = float(v)
        if 0.0 <= f <= 1.0 and not isinstance(v, int):
            # 0.0 = warmest (500 mired), 1.0 = coolest (153 mired)
            mired = round(_MIRED_MAX - f * (_MIRED_MAX - _MIRED_MIN))
        else:
            mired = round(f)
        return "turn_on", {"color_temp": mired}

    if suffix == "temp_kelvin":
        if len(args) < 1:
            return None
        return "turn_on", {"kelvin": int(args[0])}

    return None