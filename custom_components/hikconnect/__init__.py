import logging
from datetime import timedelta

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import HikConnect
from .const import (
    DOMAIN,
    MANUFACTURER,
    PLATFORMS,
    SERVICE_GET_STREAM_BOOTSTRAP,
    SERVICE_SELECT_STREAM_CANDIDATE,
    SERVICE_GET_STREAM_SOURCE,
)
from .exceptions import HikConnectError, LoginError
from .local_stream import (
    HikConnectLocalStreamAuthView,
    HikConnectLocalStreamView,
    build_authenticated_stream_url,
    build_internal_stream_url,
)

_LOGGER = logging.getLogger(__name__)

STREAM_BOOTSTRAP_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Optional("device_serial"): cv.string,
        vol.Optional("camera_id"): cv.string,
    }
)

SELECT_STREAM_CANDIDATE_SERVICE_SCHEMA = vol.Schema(
    {
        vol.Required("camera_id"): cv.string,
        vol.Optional("device_serial"): cv.string,
        vol.Optional("candidate_index"): vol.All(vol.Coerce(int), vol.Range(min=0)),
        vol.Optional("candidate_kind"): cv.string,
    }
)


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
            devices = [device async for device in api.get_devices()]
            for device_info in devices:
                _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
                cameras = [c async for c in api.get_cameras(device_info["serial"])]
                for camera in cameras:
                    camera["stream_bootstrap"] = api.build_stream_bootstrap(
                        device_info, camera
                    )
                device_info.update({"cameras": cameras})
            return devices
        except (HikConnectError, aiohttp.ClientError) as e:
            raise UpdateFailed(e) from e

    async def async_handle_get_stream_bootstrap(call: ServiceCall):
        device_serial = call.data.get("device_serial")
        camera_id = call.data.get("camera_id")
        cameras = []

        for device_info in coordinator.data:
            if device_serial and device_info["serial"] != device_serial:
                continue
            for camera_info in device_info["cameras"]:
                if camera_id and camera_info["id"] != camera_id:
                    continue
                cameras.append(camera_info.get("stream_bootstrap") or api.build_stream_bootstrap(device_info, camera_info))

        return {"cameras": cameras}

    async def async_handle_get_stream_source(call: ServiceCall):
        device_serial = call.data.get("device_serial")
        camera_id = call.data.get("camera_id")
        selected_candidates = hass.data[DOMAIN].setdefault("selected_stream_candidates", {})

        for device_info in coordinator.data:
            if device_serial and device_info["serial"] != device_serial:
                continue
            for camera_info in device_info["cameras"]:
                if camera_id and camera_info["id"] != camera_id:
                    continue
                bootstrap = camera_info.get("stream_bootstrap") or api.build_stream_bootstrap(device_info, camera_info)
                candidates = bootstrap.get("stream_candidates", [])
                selected_index = selected_candidates.get(camera_info["id"], 0)
                selected_source = None
                selected_kind = None
                if 0 <= selected_index < len(candidates):
                    selected_kind = candidates[selected_index]["kind"]
                    if selected_kind == "local_hik_bridge":
                        selected_source = build_internal_stream_url(hass, camera_info["id"])
                    else:
                        selected_source = candidates[selected_index]["url"]
                return {
                    "camera_id": camera_info["id"],
                    "device_serial": device_info["serial"],
                    "stream_source": selected_source,
                    "authenticated_stream_source": build_authenticated_stream_url(
                        hass, camera_info["id"]
                    ),
                    "selected_candidate_index": selected_index,
                    "selected_candidate_kind": selected_kind,
                    "stream_candidates": candidates,
                }

        return {
            "camera_id": camera_id,
            "device_serial": device_serial,
            "stream_source": None,
            "selected_candidate_index": None,
            "selected_candidate_kind": None,
            "stream_candidates": [],
        }

    async def async_handle_select_stream_candidate(call: ServiceCall):
        device_serial = call.data.get("device_serial")
        camera_id = call.data["camera_id"]
        candidate_index = call.data.get("candidate_index")
        candidate_kind = call.data.get("candidate_kind")

        selected_candidates = hass.data[DOMAIN].setdefault("selected_stream_candidates", {})

        for device_info in coordinator.data:
            if device_serial and device_info["serial"] != device_serial:
                continue
            for camera_info in device_info["cameras"]:
                if camera_info["id"] != camera_id:
                    continue

                bootstrap = camera_info.get("stream_bootstrap") or api.build_stream_bootstrap(device_info, camera_info)
                candidates = bootstrap.get("stream_candidates", [])
                resolved_index = None

                if candidate_kind is not None:
                    for index, candidate in enumerate(candidates):
                        if candidate["kind"] == candidate_kind:
                            resolved_index = index
                            break
                elif candidate_index is not None:
                    if 0 <= candidate_index < len(candidates):
                        resolved_index = candidate_index
                else:
                    resolved_index = 0

                if resolved_index is None:
                    return {
                        "camera_id": camera_id,
                        "device_serial": device_info["serial"],
                        "updated": False,
                        "reason": "candidate_not_found",
                        "stream_candidates": candidates,
                    }

                selected_candidates[camera_id] = resolved_index
                selected_kind = candidates[resolved_index]["kind"]
                if selected_kind == "local_hik_bridge":
                    stream_source = build_internal_stream_url(hass, camera_id)
                else:
                    stream_source = candidates[resolved_index]["url"]
                return {
                    "camera_id": camera_id,
                    "device_serial": device_info["serial"],
                    "updated": True,
                    "selected_candidate_index": resolved_index,
                    "selected_candidate_kind": selected_kind,
                    "stream_source": stream_source,
                    "stream_candidates": candidates,
                }

        return {
            "camera_id": camera_id,
            "device_serial": device_serial,
            "updated": False,
            "reason": "camera_not_found",
            "stream_candidates": [],
        }

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
        update_interval=timedelta(minutes=30),
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
            if not camera['is_shown']:
                continue
            ha_camera_id = (DOMAIN, device["id"] + "-" + camera["id"])
            dr.async_get_or_create(
                config_entry_id=entry.entry_id,
                identifiers={ha_camera_id},
                name=camera["name"],
                manufacturer=MANUFACTURER,
                via_device=ha_device_id,
            )

    # TODO handle multiple instances of the same integration
    hass.data[DOMAIN] = {
        "api": api,
        "coordinator": coordinator,
        "selected_stream_candidates": {},
        "stream_bridge_tokens": {},
        "stream_bridge_lookup": {},
    }
    hass.http.register_view(HikConnectLocalStreamView())
    hass.http.register_view(HikConnectLocalStreamAuthView())
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_STREAM_BOOTSTRAP,
        async_handle_get_stream_bootstrap,
        schema=STREAM_BOOTSTRAP_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_GET_STREAM_SOURCE,
        async_handle_get_stream_source,
        schema=STREAM_BOOTSTRAP_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_SELECT_STREAM_CANDIDATE,
        async_handle_select_stream_candidate,
        schema=SELECT_STREAM_CANDIDATE_SERVICE_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
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
        hass.services.async_remove(DOMAIN, SERVICE_GET_STREAM_BOOTSTRAP)
        hass.services.async_remove(DOMAIN, SERVICE_GET_STREAM_SOURCE)
        hass.services.async_remove(DOMAIN, SERVICE_SELECT_STREAM_CANDIDATE)
        data = hass.data.pop(DOMAIN)
        await data["api"].close()
    return unload_ok
