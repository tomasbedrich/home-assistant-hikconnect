import asyncio
import logging
import time

import aiohttp
from hikconnect.api import HikConnect
from hikconnect.exceptions import HikConnectError
from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN

# Every snapshot makes the device capture and upload a new image, the official app
# does it at most once per 10 minutes. Don't let the frontend (refreshing every 10 s)
# hammer the cloud.
SNAPSHOT_MIN_INTERVAL = 60  # seconds

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]
    verification_code = entry.options.get("verification_code") or None

    new_entities = [
        HikConnectCamera(api, device_info, camera_info, verification_code)
        for device_info in coordinator.data
        for camera_info in device_info["cameras"]
        if camera_info["is_shown"]
    ]
    if new_entities:
        async_add_entities(new_entities)


class HikConnectCamera(Camera):
    """Still image of a Hik-Connect channel, captured on demand via the cloud."""

    def __init__(
        self,
        api: HikConnect,
        device_info: dict,
        camera_info: dict,
        verification_code: str | None,
    ):
        super().__init__()
        self._api = api
        self._device_info = device_info
        self._camera_info = camera_info
        self._verification_code = verification_code
        self._image: bytes | None = None
        self._image_time = 0.0
        self._lock = asyncio.Lock()
        self._attr_name = camera_info["name"]
        self._attr_unique_id = "-".join(
            (DOMAIN, device_info["id"], camera_info["id"], "camera")
        )
        # cameras with locks have their own HA device, see __init__.py
        if device_info["locks"].get(camera_info["channel_number"], 0):
            identifier = device_info["id"] + "-" + camera_info["id"]
        else:
            identifier = device_info["id"]
        self._attr_device_info = {"identifiers": {(DOMAIN, identifier)}}

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        async with self._lock:
            if time.monotonic() - self._image_time < SNAPSHOT_MIN_INTERVAL:
                return self._image
            self._image_time = time.monotonic()
            try:
                self._image = await self._api.get_snapshot(
                    self._device_info["serial"],
                    self._camera_info["channel_number"],
                    is_encrypted=bool(self._device_info.get("is_encrypted")),
                    verification_code=self._verification_code,
                )
            except (HikConnectError, aiohttp.ClientError) as exc:
                _LOGGER.warning("Failed to get snapshot of %s: %s", self.name, exc)
            return self._image
