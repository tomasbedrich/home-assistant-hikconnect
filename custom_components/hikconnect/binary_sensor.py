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

    new_entities: list[BinarySensorEntity] = []
    for device_info in coordinator.data:
        new_entities.append(ConnectivitySensor(coordinator, device_info["id"]))
        new_entities.append(UpdateAvailableSensor(coordinator, device_info["id"]))
    if new_entities:
        async_add_entities(new_entities)


class _CoordinatorBinarySensor(CoordinatorEntity, BinarySensorEntity):
    """Common boilerplate for Hik-Connect diagnostic binary sensors."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    _field: str = ""
    _suffix: str = ""
    _name_suffix: str = ""

    def __init__(self, coordinator: DataUpdateCoordinator, device_id: str):
        super().__init__(coordinator)
        self._device_id = device_id
        self._attr_unique_id = "-".join((DOMAIN, device_id, self._suffix))

    @property
    def _device_info_data(self) -> dict:
        for device in self.coordinator.data or []:
            if device.get("id") == self._device_id:
                return device
        return {}

    @property
    def name(self):
        name = self._device_info_data.get("name") or self._device_id
        return f"{name} {self._name_suffix}"

    @property
    def is_on(self) -> bool:
        return bool(self._device_info_data.get(self._field))

    @property
    def available(self) -> bool:
        # Mark unavailable when the cloud did not surface this field so we
        # do not flip the sensor to its "off" state while data is missing.
        return (
            super().available
            and self._device_info_data.get(self._field) is not None
        )

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
        }


class ConnectivitySensor(_CoordinatorBinarySensor):
    """
    Reports whether the device is online and reachable to the Hik-Connect cloud.

    Backed by the ``statusInfos[serial].globalStatus`` field of the
    /devices/pagelist response. Hik-Connect reports ``1`` when the device
    is online; ``0`` (offline) and ``2`` (sleeping) are treated as not
    connected.
    """

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _field = "is_online"
    _suffix = "online"
    _name_suffix = "online"


class UpdateAvailableSensor(_CoordinatorBinarySensor):
    """Reports whether a firmware update is available for the device."""

    _attr_device_class = BinarySensorDeviceClass.UPDATE
    _field = "update_available"
    _suffix = "update-available"
    _name_suffix = "update available"
