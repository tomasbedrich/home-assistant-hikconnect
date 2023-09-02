import logging

from hikconnect.api import HikConnect
from homeassistant.components.lock import LockEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

DOOR_LATCH_UNLOCKED_FOR = 5  # seconds

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]

    new_entities = []
    for device_info in coordinator.data:
        for camera_info in device_info["cameras"]:
            number_of_locks = device_info["locks"].get(camera_info["channel_number"], 0)
            # we can have camera-only devices (no locks to control)
            for lock_index in range(number_of_locks):
                new_entities.append(
                    Lock(api, coordinator, device_info, camera_info, lock_index)
                )

    if new_entities:
        async_add_entities(new_entities)


class Lock(CoordinatorEntity, LockEntity):
    """
    Represents a single channel for Hik-Connect device.

    Because we are not able to provide Camera signal, the only action available is to unlock a door.

    Actually, "open" would be better because the door are presumably not "locked" - only the door latch is
    holding them closed. But Home Assistant offers nicer UI for un/locking and emperically proven: people
    are fighting a lot to create a script for calling `lock.open` service.

    That's why we fall back to un/lock API. The most accurate comparison would be:
    - locked = door latch closed,
    - unlocked = door latch free.
    """

    def __init__(
        self,
        api: HikConnect,
        coordinator: DataUpdateCoordinator,
        device_info: dict,
        camera_info: dict,
        lock_index: int,
    ):
        super().__init__(coordinator)
        self._api = api
        # TODO read all data from coordinator.data to ensure they are updated alright
        self._device_info = device_info
        self._camera_info = camera_info
        self._lock_index = lock_index
        self._is_locked = True

    def lock(self, **kwargs):
        _LOGGER.warning("Locking not implemented")

    def unlock(self, **kwargs):
        _LOGGER.warning("Unlocking not implemented")

    def open(self, **kwargs):
        raise NotImplementedError()

    async def async_lock(self, **kwargs):
        self._is_locked = True
        self.async_write_ha_state()

    async def async_unlock(self, **kwargs):
        await self._api.unlock(
            self._device_info["serial"],
            self._camera_info["channel_number"],
            self._lock_index,
        )

        async def _lock_later(_now):
            await self.async_lock()

        # this mimicks the real behaviour of door latch connected to Hikvision outdoor
        # station = the latch locks itself after a few seconds
        self._is_locked = False
        async_call_later(self.hass, delay=DOOR_LATCH_UNLOCKED_FOR, action=_lock_later)
        self.async_write_ha_state()

    async def async_open(self, **kwargs):
        _LOGGER.warning(
            "Calling lock.open service for hikconnect is DEPRECATED and will be removed in next version."
        )
        return await self.async_unlock()

    @property
    def name(self):
        name = f"{self._camera_info['name']} lock"  # TODO translate?
        if self._lock_index:
            name += f" {self._lock_index + 1}"
        return name

    @property
    def unique_id(self):
        return "-".join(
            (
                DOMAIN,
                self._device_info["id"],
                self._camera_info["id"],
                str(self._lock_index),
            )
        )

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {
                (DOMAIN, self._device_info["id"] + "-" + self._camera_info["id"])
            },
            "via_device": (DOMAIN, self._device_info["id"]),
        }

    @property
    def entity_registry_enabled_default(self):
        return bool(self._camera_info["is_shown"])

    @property
    def assumed_state(self):
        return True

    @property
    def is_locked(self):
        return self._is_locked

    @property
    def icon(self):
        return "mdi:lock"
