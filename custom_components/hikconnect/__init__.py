import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup(hass, config):
    hass.data.setdefault(DOMAIN, {})
    # we don't support YAML configuration, therefore just return True
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    hass.data.setdefault(DOMAIN, {})
    hass.async_create_task(hass.config_entries.async_forward_entry_setup(entry, "lock"))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    return await hass.config_entries.async_forward_entry_unload(entry, "lock")
