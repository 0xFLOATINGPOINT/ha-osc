# OSC for Home Assistant

[![hacs_badge](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![GitHub Release](https://img.shields.io/github/release/jeremy-lx-design/ha-osc.svg)](https://github.com/jeremy-lx-design/ha-osc/releases)
[![License](https://img.shields.io/github/license/jeremy-lx-design/ha-osc.svg)](LICENSE)

Receive **OSC (Open Sound Control)** messages over UDP and route them directly to Home Assistant entities — no YAML, no manual path mapping required.

Designed for DAWs, lighting desks, TouchOSC, Lemur, and any other OSC-capable controller.

---

## Features

- **Zero-config routing** — OSC address `/light/kitchen` automatically targets entity `light.kitchen`
- **Simple filter rules** — a single text box in the UI lets you whitelist or blacklist any address pattern using `+` / `-` prefix and `*` wildcards
- **Whitelist mode** — add any `+` rule and nothing fires except what you explicitly allow
- **Smart value mapping** — floats, integers, booleans, and strings all map to the right service call per domain
- **Multiple instances** — add the integration more than once to listen on different ports
- **No restarts needed** — filter rules update live; port changes trigger an automatic reload

---

## Installation

### Via HACS (recommended)

1. Open HACS → **Integrations** → ⋮ menu → **Custom repositories**
2. Add `https://github.com/jeremy-lx-design/ha-osc` with category **Integration**
3. Search for **OSC** and click **Download**
4. Restart Home Assistant
5. Go to **Settings → Devices & Services → Add Integration** and search for **OSC**

### Manual

1. Copy the `custom_components/ha_osc` folder into your HA `config/custom_components/` directory
2. Restart Home Assistant
3. Go to **Settings → Devices & Services → Add Integration** and search for **OSC**

---

## Configuration

### Setup

You only need to provide a **UDP port** (default `8000`). Filter rules are optional.

Open **Settings → Devices & Services**, find the OSC integration card, and click **Configure** at any time to change the port or edit filter rules.

### OSC Address Format

```
/<domain>/<entity_name>
```

| OSC Address            | Home Assistant Entity        |
|------------------------|------------------------------|
| `/light/kitchen`       | `light.kitchen`              |
| `/switch/garden_pump`  | `switch.garden_pump`         |
| `/media_player/tv`     | `media_player.tv`            |
| `/input_number/target_temp` | `input_number.target_temp` |
| `/cover/living_room`   | `cover.living_room`          |
| `/climate/bedroom`     | `climate.bedroom`            |

> **Tip:** Replace dots in entity IDs with slashes: `light.bedroom_ceiling` → `/light/bedroom_ceiling`

### Value Mapping

| OSC Value            | Action                                                     |
|----------------------|------------------------------------------------------------|
| *(no args)*          | `toggle`                                                   |
| `0` / `false`        | `turn_off`                                                 |
| `1` / `true`         | `turn_on`                                                  |
| `float 0.0–1.0`      | **light** → brightness %; **media_player** → volume level; **cover** → position %; **fan** → speed % |
| `int > 1`            | **light** → brightness 0–255; **media_player** → volume 0–100 |
| `"toggle"`           | `toggle`                                                   |
| `"on"` / `"off"`     | `turn_on` / `turn_off`                                     |
| numeric value        | **input_number** / **number** → `set_value`; **climate** → `set_temperature` |

---

## Filter Rules

Filter rules are entered one per line in the **Filter Rules** text box. They use glob-style wildcards (`*` matches any sequence of characters, `?` matches a single character).

### Syntax

```
-/pattern    block addresses matching this pattern
+/pattern    allow addresses matching this pattern
# comment    ignored
```

### Priority Model

| Situation                         | Default behaviour         |
|-----------------------------------|---------------------------|
| No rules                          | Everything is allowed     |
| Only `-` rules                    | Everything allowed **except** blocked patterns |
| Any `+` rule present              | **Whitelist mode** — nothing fires unless explicitly allowed |
| Both `+` and `-` rules            | `+` wins over `-`; anything unmatched is denied |

### Examples

```
# Block everything except specific lights
+/light/kitchen
+/light/living_room

# Block all media players
-/media_player/*

# Block all lights except the kitchen
-/light/*
+/light/kitchen

# Allow only a specific switch
+/switch/party_mode

# No rules = everything works (default)
```

---

## Troubleshooting

**Messages aren't getting through**
- Check that your firewall allows UDP on the configured port
- Enable debug logging to see incoming messages:
  ```yaml
  # configuration.yaml
  logger:
    logs:
      custom_components.ha_osc: debug
  ```

**Entity not found / service error**
- Verify the OSC address matches the entity ID exactly: `/domain/object_id`
- Entity IDs are lowercase with underscores — e.g. `/light/bedroom_ceiling`
- Check HA logs for warnings about unknown entities or services

**Port already in use**
- Another process (or another OSC integration instance) is using that port
- Pick a different port in the integration options

---

## Contributing

Pull requests and issues are welcome at [github.com/jeremy-lx-design/ha-osc](https://github.com/jeremy-lx-design/ha-osc).

---

## License

[MIT](LICENSE)
