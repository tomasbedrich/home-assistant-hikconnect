import logging

from hikconnect.api import HikConnect
from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]

    new_entities = []
    for device_info in coordinator.data:
        new_entities.append(AnswerCallButton(api, device_info))
        new_entities.append(CancelCallButton(api, device_info))
        new_entities.append(HangupCallButton(api, device_info))

    if new_entities:
        async_add_entities(new_entities, update_before_add=True)


class AnswerCallButton(ButtonEntity):
    """
    Represents a answer call operation of an indoor station.
    """

    def __init__(self, api: HikConnect, device_info: dict):
        super().__init__()
        self._api = api
        self._device_info = device_info

    async def async_press(self) -> None:
        await self._api.answer_call(self._device_info["serial"])

    @property
    def name(self):
        return f"{self._device_info['name']} answer call"  # TODO translate?

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], "answer-call"))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self._device_info["id"])},
        }

    @property
    def icon(self):
        return "mdi:phone"
        

class CancelCallButton(ButtonEntity):
    """
    Represents a cancel call operation of an indoor station.
    """

    def __init__(self, api: HikConnect, device_info: dict):
        super().__init__()
        self._api = api
        self._device_info = device_info

    async def async_press(self) -> None:
        await self._api.cancel_call(self._device_info["serial"])

    @property
    def name(self):
        return f"{self._device_info['name']} cancel call"  # TODO translate?

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], "cancel-call"))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self._device_info["id"])},
        }

    @property
    def icon(self):
        return "mdi:phone-hangup"


class HangupCallButton(ButtonEntity):
    """
    Represents a hangup call operation of an indoor station.
    """

    def __init__(self, api: HikConnect, device_info: dict):
        super().__init__()
        self._api = api
        self._device_info = device_info

    async def async_press(self) -> None:
        await self._api.hangup_call(self._device_info["serial"])

    @property
    def name(self):
        return f"{self._device_info['name']} hangup call"  # TODO translate?

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_info["id"], "hangup-call"))

    @property
    def device_info(self):
        # https://developers.home-assistant.io/docs/device_registry_index/#device-properties
        return {
            "identifiers": {(DOMAIN, self._device_info["id"])},
        }

    @property
    def icon(self):
        return "mdi:phone-hangup"
