import logging

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DataUpdateCoordinator = hass.data[DOMAIN]["coordinator"]

    new_entities = [
        ConnectivitySensor(coordinator, index)
        for index, _ in enumerate(coordinator.data)
    ]
    if new_entities:
        async_add_entities(new_entities)


class ConnectivitySensor(CoordinatorEntity, BinarySensorEntity):
    """
    Reports whether the device is online and reachable to the Hik-Connect cloud.

    Backed by the ``statusInfos[serial].globalStatus`` field of the
    /devices/pagelist response. Hik-Connect reports ``1`` when the device
    is online; ``0`` (offline) and ``2`` (sleeping) are treated as not
    connected.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: DataUpdateCoordinator, device_index: int):
        super().__init__(coordinator)
        self._device_index = device_index

    @property
    def _device_info_data(self) -> dict:
        return self.coordinator.data[self._device_index]

    @property
    def name(self):
        return f"{self._device_info_data['name']} online"

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info_data["id"], "online"))

    @property
    def is_on(self) -> bool:
        return bool(self._device_info_data.get("is_online"))

    @property
    def available(self) -> bool:
        # Mark unavailable when the cloud did not surface a status code
        # (e.g. the extras request failed) so we don't falsely report
        # "disconnected" while the device is actually fine.
        return (
            super().available
            and self._device_info_data.get("is_online") is not None
        )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_info_data["id"])},
        }
