"""UI configuration for local Assist logging."""

from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant import config_entries
from homeassistant.components.assist_pipeline import async_get_pipelines
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector

from .const import (
    CONF_FAILURE_EVENTS,
    CONF_IGNORE_DUPLICATE_WAKE_UPS,
    CONF_MAX_FILE_MB,
    CONF_MAX_FILES,
    CONF_PIPELINE_IDS,
    CONF_RETENTION_DAYS,
    DEFAULTS,
    DOMAIN,
)


def _schema(hass: HomeAssistant, current: Mapping[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Optional(
                CONF_PIPELINE_IDS, default=current[CONF_PIPELINE_IDS]
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=pipeline.id, label=pipeline.name
                        )
                        for pipeline in async_get_pipelines(hass)
                    ],
                    multiple=True,
                    custom_value=True,
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Required(
                CONF_RETENTION_DAYS, default=current[CONF_RETENTION_DAYS]
            ): vol.All(vol.Coerce(int), vol.Range(min=1, max=365)),
            vol.Required(CONF_MAX_FILE_MB, default=current[CONF_MAX_FILE_MB]): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
            vol.Required(CONF_MAX_FILES, default=current[CONF_MAX_FILES]): vol.All(
                vol.Coerce(int), vol.Range(min=1, max=100)
            ),
            vol.Required(
                CONF_FAILURE_EVENTS, default=current[CONF_FAILURE_EVENTS]
            ): bool,
            vol.Required(
                CONF_IGNORE_DUPLICATE_WAKE_UPS,
                default=current[CONF_IGNORE_DUPLICATE_WAKE_UPS],
            ): bool,
        }
    )


class IntentLoggerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Set up a single local logger."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Ask for logging and failure-event preferences."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Intent Logger", data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=_schema(self.hass, DEFAULTS)
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> IntentLoggerOptionsFlow:
        """Offer the same settings after setup."""
        return IntentLoggerOptionsFlow()


class IntentLoggerOptionsFlow(config_entries.OptionsFlow):
    """Change options without editing YAML."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show persisted settings and reload after changes."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**DEFAULTS, **self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init", data_schema=_schema(self.hass, current)
        )
