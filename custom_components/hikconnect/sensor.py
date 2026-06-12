import asyncio
import json
import logging
from datetime import timedelta

import aiohttp
from hikconnect.api import HikConnect
from homeassistant.components.sensor import (
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=3)  # TODO make it configurable via UI?
SCAN_INTERVAL_TIMEOUT = timedelta(seconds=2.8)
RAISE_ON_ERRORS = False  # TODO make it configurable via UI?


def _patch_hikconnect_logger():
    """
    Discard a single log message from HikConnect.get_call_status() if log level is INFO.

    This is to prevent too verbose logging, because get_call_status() is called in 3s loop.
    It should remain working when explicitly desired by setting log level to DEBUG.
    """
    def log_filter(record: logging.LogRecord):
        return not (record.levelno == logging.INFO and "call status" in record.msg)

    hikconnect_logger = logging.getLogger("hikconnect.api")
    if hikconnect_logger.getEffectiveLevel() == logging.INFO:
        hikconnect_logger.addFilter(log_filter)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]

    _patch_hikconnect_logger()

    new_entities = []
    for device_info in coordinator.data:
        new_entities.append(CallStatusSensor(api, device_info))
        new_entities.append(LocalIpSensor(coordinator, device_info["id"]))
        new_entities.append(WanIpSensor(coordinator, device_info["id"]))
        new_entities.append(WifiSignalSensor(coordinator, device_info["id"]))

    if new_entities:
        async_add_entities(new_entities, update_before_add=True)


class CallStatusSensor(SensorEntity):
    """
    Represents a call status of an indoor station.
    """

    def __init__(self, api: HikConnect, device_info: dict):
        super().__init__()
        self._api = api
        self._device_info = device_info
        self._attr_available = False

    async def async_update(self) -> None:
        get_call_status_coro = self._api.get_call_status(self._device_info["serial"])
        try:
            res = await asyncio.wait_for(get_call_status_coro, SCAN_INTERVAL_TIMEOUT.seconds)
            self._attr_native_value = res["status"]
            self._attr_extra_state_attributes = res["info"]
            self._attr_available = True
        except (asyncio.TimeoutError, aiohttp.ClientError, KeyError, json.decoder.JSONDecodeError):
            if RAISE_ON_ERRORS:
                _LOGGER.exception("Update of call status failed")
                raise
            else:
                # don't raise by default because hikconnect API errors are
                # so frequent, that they can spam logs A LOT
                self._attr_available = False

    @property
    def name(self):
        return f"{self._device_info['name']} call status"  # TODO translate?

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], "call-status"))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self._device_info["id"])},
        }

    @property
    def icon(self):
        # TODO fix duplication of constants?
        if self.native_value == "idle":
            return "mdi:phone-hangup"
        elif self.native_value == "ringing":
            return "mdi:phone-ring"
        elif self.native_value == "call in progress":
            return "mdi:phone-in-talk"
        else:
            return "mdi:phone-alert"


class _DeviceFieldSensor(CoordinatorEntity, SensorEntity):
    """Base sensor backed by a single coordinator device field."""

    _field: str = ""
    _suffix: str = ""
    _icon: str = ""
    _name_suffix: str = ""

    # Diagnostic, off by default.
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

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
    def native_value(self):
        return self._device_info_data.get(self._field)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
        }

    @property
    def icon(self):
        return self._icon


class LocalIpSensor(_DeviceFieldSensor):
    """LAN IP address reported by Hik-Connect cloud."""

    _field = "local_ip"
    _suffix = "local-ip"
    _icon = "mdi:ip-network"
    _name_suffix = "local IP"


class WanIpSensor(_DeviceFieldSensor):
    """Public/WAN IP address reported by Hik-Connect cloud."""

    _field = "wan_ip"
    _suffix = "wan-ip"
    _icon = "mdi:wan"
    _name_suffix = "WAN IP"


class WifiSignalSensor(_DeviceFieldSensor):
    """WiFi signal strength reported by the device (0-100)."""

    _field = "wifi_signal"
    _suffix = "wifi-signal"
    _icon = "mdi:wifi-strength-3"
    _name_suffix = "WiFi signal"

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
