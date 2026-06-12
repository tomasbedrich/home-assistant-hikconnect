import json
import logging
from datetime import timedelta

import aiohttp
from hikconnect.api import HikConnect
from hikconnect.exceptions import HikConnectError, LoginError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, MANUFACTURER, PLATFORMS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    api = HikConnect()
    api.BASE_URL = entry.data["base_url"]

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

    async def async_fetch_pagelist_extras() -> dict:
        """Fetch CONNECTION/STATUS/WIFI fields the hikconnect lib drops.

        Returns serial -> dict with local_ip, wan_ip, is_online,
        wifi_signal, update_available.
        """
        result: dict = {}
        connections: dict = {}
        statuses: dict = {}
        wifis: dict = {}
        limit, offset = 50, 0
        while True:
            url = (
                f"{api.BASE_URL}/v3/userdevices/v1/devices/pagelist"
                f"?groupId=-1&limit={limit}&offset={offset}"
                "&filter=CONNECTION,STATUS,WIFI"
            )
            try:
                async with api.client.get(url) as res:
                    payload = await res.json()
            except (aiohttp.ClientError, ValueError) as e:
                _LOGGER.debug("Failed to fetch pagelist extras: %s", e)
                return result
            connections.update(payload.get("connectionInfos") or {})
            statuses.update(payload.get("statusInfos") or {})
            wifis.update(payload.get("wifiInfos") or {})
            page = payload.get("page") or {}
            if not page.get("hasNext"):
                break
            offset += limit
        _LOGGER.debug(
            "pagelist extras: connectionInfos serials=%s, statusInfos serials=%s, wifiInfos serials=%s",
            list(connections), list(statuses), list(wifis),
        )
        for serial in {*connections, *statuses, *wifis}:
            conn = connections.get(serial) or {}
            wifi = wifis.get(serial) or {}
            status = statuses.get(serial) or {}
            _LOGGER.debug(
                "pagelist extras for %s: CONNECTION=%s STATUS=%s WIFI=%s",
                serial, conn, status, wifi,
            )
            local_ip = conn.get("localIp")
            if not isinstance(local_ip, str) or not local_ip or local_ip == "0.0.0.0":
                local_ip = wifi.get("address")
            if not isinstance(local_ip, str) or local_ip == "0.0.0.0":
                local_ip = None
            wan_ip = conn.get("netIp")
            if not isinstance(wan_ip, str) or not wan_ip or wan_ip == "0.0.0.0":
                wan_ip = None
            # globalStatus: 1=online, 2=sleep, 0/missing=offline.
            status_code = status.get("globalStatus")
            if status_code is None:
                is_online = None
            else:
                is_online = status_code == 1
            wifi_signal = wifi.get("signal")
            if not isinstance(wifi_signal, int):
                wifi_signal = None
            upgrade_available = status.get("upgradeAvailable")
            if upgrade_available is None:
                update_available = None
            else:
                update_available = bool(upgrade_available)
            # Cloud keeps stale IP/signal after device drops; clear them.
            if not is_online:
                local_ip = None
                wan_ip = None
                wifi_signal = None
            result[serial] = {
                "local_ip": local_ip,
                "wan_ip": wan_ip,
                "is_online": is_online,
                "wifi_signal": wifi_signal,
                "update_available": update_available,
            }
        return result

    async def async_update():
        try:
            await relogin_if_needed()
            _LOGGER.info("Getting devices")
            # Skip devices with malformed payload - see #62.
            devices = []
            it = api.get_devices()
            while True:
                try:
                    device = await it.__anext__()
                except StopAsyncIteration:
                    break
                except json.JSONDecodeError as e:
                    _LOGGER.warning("Skipping device with malformed data: %s", e)
                    continue
                devices.append(device)
            extras = await async_fetch_pagelist_extras()
            empty_extras = {
                "local_ip": None,
                "wan_ip": None,
                "is_online": None,
                "wifi_signal": None,
                "update_available": None,
            }
            for device_info in devices:
                _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
                cameras = [c async for c in api.get_cameras(device_info["serial"])]
                device_info.update({"cameras": cameras})
                device_info.update(extras.get(device_info["serial"], empty_extras))
            return devices
        except (HikConnectError, aiohttp.ClientError) as e:
            raise UpdateFailed(e) from e

    # Refreshing device info can be relativelly infrequent, but...
    # BEWARE: Multiple people reported that they needed to restart the
    # integration every 24h / 48h. This is suspiciously regular.
    # There is probably a race condition between `update_interval`
    # and `api.is_refresh_login_needed()` => let's update it more often
    # than once per hour.
    # see: https://github.com/tomasbedrich/home-assistant-hikconnect/issues/27
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update,
        update_interval=timedelta(minutes=5),
    )
    await coordinator.async_config_entry_first_refresh()

    dr = device_registry.async_get(hass)
    expected_identifiers: set[tuple[str, str]] = set()
    for device in coordinator.data:
        ha_device_id = (DOMAIN, device["id"])
        expected_identifiers.add(ha_device_id)
        dr.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={ha_device_id},
            name=device["name"],
            manufacturer=MANUFACTURER,
            model=device["type"],
            sw_version=device["version"],
        )
        for camera in device["cameras"]:
            # see: https://github.com/tomasbedrich/home-assistant-hikconnect/issues/60
            if not camera["is_shown"]:
                continue
            if not device["locks"].get(camera["channel_number"], 0):
                continue
            ha_camera_id = (DOMAIN, device["id"] + "-" + camera["id"])
            expected_identifiers.add(ha_camera_id)
            dr.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={ha_camera_id},
                name=camera["name"],
                manufacturer=MANUFACTURER,
                via_device=ha_device_id,
            )

    # Drop orphan devices created by previous versions.
    for ha_device in device_registry.async_entries_for_config_entry(
        dr, entry.entry_id
    ):
        if not any(ident in expected_identifiers for ident in ha_device.identifiers):
            dr.async_update_device(
                ha_device.id, remove_config_entry_id=entry.entry_id
            )

    # TODO handle multiple instances of the same integration
    hass.data[DOMAIN] = {
        "api": api,
        "coordinator": coordinator,
    }
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry):
    _LOGGER.debug("Migrating from version %s", entry.version)

    if entry.version == 1:
        new = {**entry.data, "base_url": HikConnect.BASE_URL}
        entry.version = 2
        hass.config_entries.async_update_entry(entry, data=new)

    _LOGGER.info("Migration to version %s successful", entry.version)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry):
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data.pop(DOMAIN)
        await data["api"].close()
    return unload_ok
