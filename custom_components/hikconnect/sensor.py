import asyncio
import logging
from datetime import timedelta

import aiohttp
from hikconnect.api import HikConnect
from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=3)
SCAN_INTERVAL_TIMEOUT = timedelta(seconds=2.8)
ERROR_THRESHOLD = 10


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
        self._error_counter = 0

    async def async_update(self) -> None:
        get_call_status_coro = self._api.get_call_status(self._device_info["serial"])
        try:
            res = await asyncio.wait_for(get_call_status_coro, SCAN_INTERVAL_TIMEOUT.seconds)
            self._attr_native_value = res["status"]
            self._attr_extra_state_attributes = res["info"]
            self._error_counter = 0
        except (asyncio.TimeoutError, aiohttp.ClientError, KeyError):
            self._error_counter += 1
            if self._error_counter % ERROR_THRESHOLD == 0:
                # don't log:
                # - occurrences < ERROR_THRESHOLD ... rare issues
                # - N*ERROR_THRESHOLD < occurrence < (N+1)*ERROR_THRESHOLD ... error condition remains
                #   for longer period of time (e.g. API is down)
                _LOGGER.exception("Update of call status failed %d times in a row", ERROR_THRESHOLD)
                raise

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
