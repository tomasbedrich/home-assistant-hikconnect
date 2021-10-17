import logging
from datetime import timedelta

import aiohttp
from hikconnect.api import HikConnect
from hikconnect.exceptions import LoginError, HikConnectError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady, ConfigEntryAuthFailed
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PLATFORMS, MANUFACTURER
from .lock import Latch

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    api = HikConnect()

    try:
        await api.login(entry.data["username"], entry.data["password"])
    except LoginError as e:
        # TODO add config_flow reauthenticate handler
        raise ConfigEntryAuthFailed from e
    except aiohttp.ClientError as e:
        raise ConfigEntryNotReady from e

    async def relogin_if_needed():
        needed = api.is_refresh_login_needed()
        _LOGGER.debug("Relogin %s needed", ("IS" if needed else "IS NOT"))
        if needed:
            try:
                await api.refresh_login()
            except LoginError as e:
                # TODO add config_flow reauthenticate handler
                raise ConfigEntryAuthFailed from e

    async def async_update():
        try:
            await relogin_if_needed()
            _LOGGER.info("Getting devices")
            devices = [device async for device in api.get_devices()]
            for device_info in devices:
                _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
                cameras = [camera async for camera in api.get_cameras(device_info["serial"])]
                device_info.update({"cameras": cameras})
            return devices
        except (HikConnectError, aiohttp.ClientError) as e:
            raise UpdateFailed(e) from e

    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update,
        update_interval=timedelta(hours=1),  # refreshing device info can be relativelly infrequent
    )
    await coordinator.async_config_entry_first_refresh()

    dr = device_registry.async_get(hass)
    for device in coordinator.data:
        ha_device_id = (DOMAIN, device["id"])
        dr.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={ha_device_id},
            name=device["name"],
            manufacturer=MANUFACTURER,
            model=device["type"],
            sw_version=device["version"],
        )
        for camera in device["cameras"]:
            ha_camera_id = (DOMAIN, device["id"] + "-" + camera["id"])
            dr.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={ha_camera_id},
                name=camera["name"],
                manufacturer=MANUFACTURER,
                via_device=ha_device_id,
            )

    hass.data[DOMAIN] = {
        "api": api,
        "coordinator": coordinator,
    }
    hass.config_entries.async_setup_platforms(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data.pop(DOMAIN)
        await data["api"].close()
    return unload_ok
