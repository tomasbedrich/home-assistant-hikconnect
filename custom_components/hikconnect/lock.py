import datetime
import logging

from hikconnect.api import HikConnect
from homeassistant.components.lock import LockEntity, SUPPORT_OPEN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import event

from .const import DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    data = hass.data[DOMAIN].setdefault(entry.entry_id, {})
    api = data.setdefault("api", HikConnect())
    await api.login(entry.data["username"], entry.data["password"])

    # TODO add device to group cameras

    _LOGGER.info("Getting devices")
    new_entities = []
    async for device_info in api.get_devices():
        _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
        async for camera_info in api.get_cameras(device_info["serial"]):
            number_of_locks = device_info["locks"].get(camera_info["channel_number"], 0)
            # we can have camera-only devices (no locks to control)
            for lock_index in range(number_of_locks):
                new_entities.append(Latch(api, device_info, camera_info, lock_index))

    if new_entities:
        async_add_entities(new_entities)

    async def relogin_if_needed(_now):
        needed = api.is_refresh_login_needed()
        _LOGGER.debug("Relogin if needed called, relogin %s needed", ("IS" if needed else "IS NOT"))
        if needed:
            await api.refresh_login()

    if "cancel_relogin_task" not in data:
        # TODO change to async_track_point_in_time
        data["cancel_relogin_task"] = event.async_track_time_interval(
            hass, relogin_if_needed, datetime.timedelta(minutes=10)
        )


async def async_unload_entry(hass, entry):
    data = hass.data[DOMAIN].pop(entry.entry_id)
    cancel_relogin_task = data.get("cancel_relogin_task")
    if cancel_relogin_task:
        cancel_relogin_task()
    api = data.get("api")
    if api:
        await api.close()


class Latch(LockEntity):
    """
    Represents a single channel for Hik-Connect device.

    Because we are not able to provide Camera signal, the only action available is to "open" a door.
    (Not unlock, they are presumably not "locked". Only the door latch is holding them closed.)
    That's the story behind naming this class "Latch".
    """

    def __init__(
        self, api: HikConnect, device_info: dict, camera_info: dict, lock_index: int
    ):
        self._api = api
        self._device_info = device_info
        self._camera_info = camera_info
        self._lock_index = lock_index

    def lock(self, **kwargs):
        _LOGGER.warning("Locking not implemented")

    def unlock(self, **kwargs):
        _LOGGER.warning("Unlocking not implemented")

    def open(self, **kwargs):
        raise NotImplementedError()

    async def async_open(self, **kwargs):
        await self._api.unlock(self._device_info["serial"], self._camera_info["channel_number"], self._lock_index)

    @property
    def name(self):
        name = f"{self._device_info['serial']}: {self._camera_info['name']}"
        if self._lock_index:
            name += f" {self._lock_index + 1}"
        return name

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], self._camera_info["id"], self._lock_index))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "suggested_area": "Outside",
            "manufacturer": MANUFACTURER,
            "name": self.name,
            # TODO following 2 are better to be moved to Device
            # "sw_version": self._device_info["version"],
            # "model": self._device_info["type"],
            "via_device": (DOMAIN, self._device_info["id"]),
        }

    @property
    def supported_features(self):
        """Flag supported features."""
        return SUPPORT_OPEN

    @property
    def should_poll(self):
        return False

    @property
    def assumed_state(self):
        return True

    @property
    def is_locked(self):
        return False

    @property
    def icon(self):
        return "mdi:lock"
