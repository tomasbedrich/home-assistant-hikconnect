import json
import logging
from datetime import timedelta

import aiohttp
from hikconnect.api import HikConnect
from hikconnect.exceptions import HikConnectError, LoginError
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
import voluptuous as vol
import homeassistant.helpers.config_validation as cv

from .const import DEFAULT_SCAN_INTERVAL_MINUTES, DOMAIN, MANUFACTURER, PLATFORMS

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Integration setup
# ---------------------------------------------------------------------------


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
            for device_info in devices:
                _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
                cameras = [c async for c in api.get_cameras(device_info["serial"])]
                device_info.update({"cameras": cameras})

                _LOGGER.info("Getting areas for device: '%s'", device_info["serial"])
                try:
                    areas = [area async for area in api.get_areas(device_info["serial"])]
                    # Enrich each area with its member camera list
                    for area in areas:
                        group_id = area.get("group_id")
                        if group_id is not None:
                            members = await api.get_area(device_info["serial"], group_id)
                            area["resources"] = members
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug(
                        "Could not fetch areas for device '%s' (device may not support them): %s",
                        device_info["serial"], exc
                    )
                    areas = []
                device_info["areas"] = areas
                if areas:
                    _LOGGER.info(
                        "Found %d area(s) for device '%s'", len(areas), device_info["serial"]
                    )
                else:
                    _LOGGER.debug(
                        "No areas found for device '%s' (device may not support area management)",
                        device_info["serial"],
                    )

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
    scan_interval_minutes = entry.options.get(
        "scan_interval_minutes", DEFAULT_SCAN_INTERVAL_MINUTES
    )
    coordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_method=async_update,
        update_interval=timedelta(minutes=scan_interval_minutes),
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

    # -----------------------------------------------------------------------
    # Register area management services
    # -----------------------------------------------------------------------

    async def handle_create_area(call: ServiceCall) -> None:
        """Service: hikconnect.create_area — create a new NVR area/group."""
        device_serial = call.data["device_serial"]
        group_name = call.data["group_name"]
        resource_ids = call.data["resource_ids"]
        try:
            result = await api.create_area(device_serial, group_name, resource_ids)
            _LOGGER.info(
                "create_area succeeded: '%s' on device '%s' → %s",
                group_name, device_serial, result,
            )
            await coordinator.async_request_refresh()
        except Exception as exc:
            raise HomeAssistantError(f"create_area FAILED: {exc}") from exc

    async def handle_delete_area(call: ServiceCall) -> None:
        """Service: hikconnect.delete_area — delete an existing NVR area/group."""
        device_serial = call.data["device_serial"]
        group_id = call.data["group_id"]
        try:
            await api.delete_area(device_serial, group_id)
            _LOGGER.info("delete_area succeeded: groupId=%s", group_id)
            await coordinator.async_request_refresh()
        except Exception as exc:
            raise HomeAssistantError(f"delete_area FAILED: {exc}") from exc

    async def handle_update_area(call: ServiceCall) -> None:
        """
        Service: hikconnect.update_area — replace the cameras in an existing area.
        """
        device_serial = call.data["device_serial"]
        group_id = call.data["group_id"]
        group_name = call.data["group_name"]
        resource_ids = call.data["resource_ids"]
        try:
            _LOGGER.info(
                "update_area: updating groupId=%s on device '%s'",
                group_id, device_serial,
            )
            result = await api.update_area(device_serial, group_id, group_name, resource_ids)
            _LOGGER.info(
                "update_area: updated area '%s' → %s", group_name, result
            )
            await coordinator.async_request_refresh()
        except Exception as exc:
            raise HomeAssistantError(f"update_area FAILED: {exc}") from exc

    async def handle_list_areas(call: ServiceCall) -> dict:
        """Service: hikconnect.list_areas — return a list of areas."""
        device_serial = call.data["device_serial"]
        try:
            areas = [area async for area in api.get_areas(device_serial)]
            return {"areas": areas}
        except Exception as exc:
            raise HomeAssistantError(f"list_areas FAILED: {exc}") from exc

    async def handle_list_cameras(call: ServiceCall) -> dict:
        """Service: hikconnect.list_cameras — return a list of cameras."""
        device_serial = call.data["device_serial"]
        try:
            cameras = [camera async for camera in api.get_cameras(device_serial)]
            return {"cameras": cameras}
        except Exception as exc:
            raise HomeAssistantError(f"list_cameras FAILED: {exc}") from exc

    async def handle_get_area_details(call: ServiceCall) -> dict:
        """Service: hikconnect.get_area_details — return area members."""
        device_serial = call.data["device_serial"]
        group_id = call.data["group_id"]
        try:
            members = await api.get_area(device_serial, group_id)
            return {"members": members}
        except Exception as exc:
            raise HomeAssistantError(f"get_area_details FAILED: {exc}") from exc

    hass.services.async_register(
        DOMAIN,
        "create_area",
        handle_create_area,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
                vol.Required("group_name"): cv.string,
                vol.Required("resource_ids"): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "delete_area",
        handle_delete_area,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
                vol.Required("group_id"): vol.Coerce(int),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "update_area",
        handle_update_area,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
                vol.Required("group_id"): vol.Coerce(int),
                vol.Required("group_name"): cv.string,
                vol.Required("resource_ids"): vol.All(
                    cv.ensure_list, [cv.string]
                ),
            }
        ),
    )

    hass.services.async_register(
        DOMAIN,
        "list_areas",
        handle_list_areas,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        "list_cameras",
        handle_list_cameras,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    hass.services.async_register(
        DOMAIN,
        "get_area_details",
        handle_get_area_details,
        schema=vol.Schema(
            {
                vol.Required("device_serial"): cv.string,
                vol.Required("group_id"): vol.Coerce(int),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

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
        hass.services.async_remove(DOMAIN, "create_area")
        hass.services.async_remove(DOMAIN, "delete_area")
        hass.services.async_remove(DOMAIN, "update_area")
        hass.services.async_remove(DOMAIN, "list_areas")
        hass.services.async_remove(DOMAIN, "get_area_details")
        hass.services.async_remove(DOMAIN, "list_cameras")
        data = hass.data.pop(DOMAIN)
        await data["api"].close()
    return unload_ok
