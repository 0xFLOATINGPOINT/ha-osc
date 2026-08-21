"""Config flow and options flow for the OSC integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, OptionsFlow
from homeassistant.core import callback

from .const import CONF_FILTER_RULES, CONF_PORT, DEFAULT_FILTER_RULES, DEFAULT_PORT, DOMAIN
from .osc_server import validate_filter_rules

try:
    from homeassistant.helpers.selector import (
        NumberSelector,
        NumberSelectorConfig,
        NumberSelectorMode,
        TextSelector,
        TextSelectorConfig,
        TextSelectorType,
    )
    _HAS_SELECTORS = True
except ImportError:
    _HAS_SELECTORS = False


def _port_schema(default_port: int, default_rules: str) -> vol.Schema:
    """Build the voluptuous schema for the setup / options form."""
    if _HAS_SELECTORS:
        port_field = NumberSelector(
            NumberSelectorConfig(min=1, max=65535, step=1, mode=NumberSelectorMode.BOX)
        )
        rules_field = TextSelector(
            TextSelectorConfig(multiline=True, type=TextSelectorType.TEXT)
        )
    else:
        port_field = vol.All(vol.Coerce(int), vol.Range(min=1, max=65535))
        rules_field = str

    return vol.Schema(
        {
            vol.Required(CONF_PORT, default=default_port): port_field,
            vol.Optional(CONF_FILTER_RULES, default=default_rules): rules_field,
        }
    )


class OscConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial setup flow for OSC."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None):
        """Present the configuration form to the user."""
        errors: dict[str, str] = {}

        if user_input is not None:
            port = int(user_input[CONF_PORT])
            rules_text: str = user_input.get(CONF_FILTER_RULES, DEFAULT_FILTER_RULES)

            # Validate filter rules syntax
            rule_error = validate_filter_rules(rules_text)
            if rule_error:
                errors[CONF_FILTER_RULES] = rule_error
            else:
                # Ensure no other entry already uses this port
                await self.async_set_unique_id(f"osc_port_{port}")
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=f"OSC port {port}",
                    data={CONF_PORT: port, CONF_FILTER_RULES: rules_text},
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_port_schema(DEFAULT_PORT, DEFAULT_FILTER_RULES),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OscOptionsFlow:
        """Create the options flow handler."""
        return OscOptionsFlow()


class OscOptionsFlow(OptionsFlow):
    """Allow the user to edit port and filter rules after initial setup."""

    async def async_step_init(self, user_input: dict | None = None):
        """Show the options form."""
        errors: dict[str, str] = {}

        current_port = int(
            self.config_entry.options.get(
                CONF_PORT, self.config_entry.data.get(CONF_PORT, DEFAULT_PORT)
            )
        )
        current_rules: str = self.config_entry.options.get(
            CONF_FILTER_RULES,
            self.config_entry.data.get(CONF_FILTER_RULES, DEFAULT_FILTER_RULES),
        )

        if user_input is not None:
            port = int(user_input[CONF_PORT])
            rules_text: str = user_input.get(CONF_FILTER_RULES, DEFAULT_FILTER_RULES)

            rule_error = validate_filter_rules(rules_text)
            if rule_error:
                errors[CONF_FILTER_RULES] = rule_error
            else:
                return self.async_create_entry(
                    title="",
                    data={CONF_PORT: port, CONF_FILTER_RULES: rules_text},
                )

        return self.async_show_form(
            step_id="init",
            data_schema=_port_schema(current_port, current_rules),
            errors=errors,
        )
