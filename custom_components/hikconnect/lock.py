import logging

from hikconnect import HikConnect
from homeassistant.components.lock import LockEntity, SUPPORT_OPEN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant

from .const import DOMAIN, MANUFACTURER

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities
):
    api = hass.data[DOMAIN][entry.entry_id].setdefault("api", HikConnect())
    await api.login(entry.data["username"], entry.data["password"])

    # TODO add device to group cameras

    _LOGGER.info("Getting devices")
    new_entities = []
    async for device_info in api.get_devices():
        _LOGGER.info("Getting cameras for device: '%s'", device_info["serial"])
        async for camera_info in api.get_cameras(device_info["serial"]):
            new_entities.append(Latch(api, device_info, camera_info))

    if new_entities:
        async_add_entities(new_entities)


async def async_unload_entry(hass, entry):
    api = hass.data[DOMAIN].pop(entry.entry_id)["api"]
    # await api.close()


class Device:
    def __init__(self, api: HikConnect, device_info: dict):
        self._api = api
        self._device_info = device_info

    @property
    def name(self):
        return self._device_info["name"]

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"]))

    @property
    def model(self):
        return self._device_info["type"]

    @property
    def sw_version(self):
        return self._device_info["version"]

    @property
    def device_info(self):
        """Information about this entity/device."""
        # TODO don't know if this is used or the properties?
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "manufacturer": MANUFACTURER,
            "name": self.name,
            "sw_version": self.sw_version,
            "model": self.model,
        }


class Latch(LockEntity):
    """
    Represents a single channel for Hik-Connect device.

    Because we are not able to provide Camera signal, the only action available is to "open" a door.
    (Not unlock, they are presumably not "locked". Only the door latch is holding them closed.)
    That's the story behind naming this class "Latch".
    """

    def __init__(
        self, api: HikConnect, device_info: dict, camera_info: dict,
    ):
        self._api = api
        self._device_info = device_info
        self._camera_info = camera_info

    def lock(self, **kwargs):
        _LOGGER.info("Locking not implemented")

    def unlock(self, **kwargs):
        _LOGGER.info("Unlocking not implemented")

    def open(self, **kwargs):
        raise NotImplementedError()

    async def async_open(self, **kwargs):
        await self._api.unlock(
            self._device_info["serial"], self._camera_info["channel_number"]
        )

    @property
    def name(self):
        return f"{self._device_info['name']}: {self._camera_info['name']}"

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], self._camera_info["id"]))

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
    def state(self):
        return STATE_UNAVAILABLE

    @property
    def state_attributes(self):
        return {
            "signal_status": self._camera_info["signal_status"],
            "is_shown": self._camera_info["is_shown"],
        }

    @property
    def icon(self):
        return "mdi:lock"
