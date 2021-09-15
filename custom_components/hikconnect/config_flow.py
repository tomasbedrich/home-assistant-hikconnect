import logging

import voluptuous as vol
from hikconnect import HikConnect
from homeassistant import config_entries, exceptions, core

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

DATA_SCHEMA = vol.Schema({
    vol.Required("username"): str,
    vol.Required("password"): str,
})


async def validate_input(hass: core.HomeAssistant, data: dict, api):
    """
    Validate the user input by logging into Hik-Connect.

    Data has the keys from DATA_SCHEMA with values provided by the user.
    """
    try:
        await api.login(data["username"], data["password"])
    except ValueError as e:
        raise LoginFailed() from e

    # only test that API is working, don't return anything cause are only setting up the INTEGRATION
    await api.get_devices()


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step of config flow initiated by user manually."""
        errors = {}
        if user_input is not None:
            api = HikConnect()
            try:
                await validate_input(self.hass, user_input, api)
                unique_id = user_input["username"]
                _LOGGER.info("Adding Hik-Connect config entry with unique_id=%s", unique_id)
                await self.async_set_unique_id(unique_id)
                self._abort_if_unique_id_configured()
                data = {
                    **user_input,
                    "api": api
                }
                return self.async_create_entry(title=unique_id, data=data)
            except LoginFailed:
                _LOGGER.exception("Hik-Connect login failed")  # to show hikconnect library exception in logs
                errors["base"] = "login_failed"
            except Exception:  # NOQA
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"

        return self.async_show_form(
            step_id="user", data_schema=DATA_SCHEMA, errors=errors
        )


class LoginFailed(exceptions.HomeAssistantError):
    pass

