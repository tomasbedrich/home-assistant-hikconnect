import logging

from hikconnect.api import HikConnect
from homeassistant.components.lock import LockEntity, SUPPORT_OPEN
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]

    new_entities = []
    for device_info in coordinator.data:
        for camera_info in device_info["cameras"]:
            number_of_locks = device_info["locks"].get(camera_info["channel_number"], 0)
            # we can have camera-only devices (no locks to control)
            for lock_index in range(number_of_locks):
                new_entities.append(Latch(api, coordinator, device_info, camera_info, lock_index))

    if new_entities:
        async_add_entities(new_entities)


class Latch(CoordinatorEntity, LockEntity):
    """
    Represents a single channel for Hik-Connect device.

    Because we are not able to provide Camera signal, the only action available is to "open" a door.
    (Not unlock, they are presumably not "locked". Only the door latch is holding them closed.)
    That's the story behind naming this class "Latch".
    """

    def __init__(
        self, api: HikConnect, coordinator: DataUpdateCoordinator, device_info: dict, camera_info: dict, lock_index: int
    ):
        super().__init__(coordinator)
        self._api = api
        # TODO read all data from coordinator.data to ensure they are updated alright
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
        name = f"{self._camera_info['name']} lock"  # TODO translate?
        if self._lock_index:
            name += f" {self._lock_index + 1}"
        return name

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], self._camera_info["id"], str(self._lock_index)))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self._device_info["id"] + "-" + self._camera_info["id"])},
            "via_device": (DOMAIN, self._device_info["id"]),
        }

    @property
    def supported_features(self):
        return SUPPORT_OPEN

    @property
    def assumed_state(self):
        return True

    @property
    def is_locked(self):
        return False

    @property
    def icon(self):
        return "mdi:lock"
