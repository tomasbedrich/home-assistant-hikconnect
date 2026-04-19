import logging

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity, DataUpdateCoordinator

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]
    coordinator = data["coordinator"]

    new_entities = []
    for device_info in coordinator.data:
        for camera_info in device_info["cameras"]:
            if not camera_info["is_shown"]:
                continue
            new_entities.append(
                HikConnectCamera(
                    hass,
                    coordinator,
                    device_info["id"],
                    camera_info["id"],
                )
            )

    if new_entities:
        async_add_entities(new_entities)


class HikConnectCamera(CoordinatorEntity, Camera):
    """Experimental Hik-Connect camera entity.

    This entity exposes live-view bootstrap metadata already confirmed in reverse
    engineering logs. Actual SDK stream proxying still needs a dedicated bridge.
    """

    _attr_content_type = "image/jpeg"

    def __init__(
        self,
        hass: HomeAssistant,
        coordinator: DataUpdateCoordinator,
        device_id: str,
        camera_id: str,
    ):
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self.hass = hass
        self._device_id = device_id
        self._camera_id = camera_id
        self._last_image = None

    def _get_device_info(self):
        for device_info in self.coordinator.data:
            if device_info["id"] == self._device_id:
                return device_info
        return None

    def _get_camera_info(self):
        device_info = self._get_device_info()
        if not device_info:
            return None
        for camera_info in device_info["cameras"]:
            if camera_info["id"] == self._camera_id:
                return camera_info
        return None

    @property
    def name(self):
        camera_info = self._get_camera_info()
        return camera_info["name"] if camera_info else None

    @property
    def unique_id(self):
        return "-".join((DOMAIN, self._device_id, self._camera_id, "camera"))

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id + "-" + self._camera_id)},
            "via_device": (DOMAIN, self._device_id),
        }

    @property
    def entity_registry_enabled_default(self):
        camera_info = self._get_camera_info()
        return bool(camera_info and camera_info["is_shown"])

    @property
    def is_on(self):
        camera_info = self._get_camera_info()
        return bool(camera_info and camera_info["signal_status"] == 1)

    @property
    def available(self):
        return self._get_camera_info() is not None

    @property
    def extra_state_attributes(self):
        device_info = self._get_device_info()
        camera_info = self._get_camera_info()
        if not device_info or not camera_info:
            return None

        bootstrap = camera_info.get("stream_bootstrap", {})
        local_sdk = bootstrap.get("local_sdk", {})
        vtm = bootstrap.get("vtm", {})
        kms = bootstrap.get("kms", {})

        return {
            "device_serial": bootstrap.get("device_serial"),
            "camera_id": bootstrap.get("camera_id"),
            "channel_number": bootstrap.get("channel_number"),
            "signal_status": bootstrap.get("signal_status"),
            "stream_transport": bootstrap.get("stream_transport"),
            "stream_biz_url": bootstrap.get("stream_biz_url"),
            "local_ip": local_sdk.get("local_ip"),
            "local_cmd_port": local_sdk.get("local_cmd_port"),
            "local_stream_port": local_sdk.get("local_stream_port"),
            "local_rtsp_port": local_sdk.get("local_rtsp_port"),
            "vtm_domain": vtm.get("domain"),
            "vtm_port": vtm.get("port"),
            "kms_version": kms.get("version"),
            "has_local_stream": bool(local_sdk.get("local_stream_port")),
            "has_local_rtsp": bool(local_sdk.get("local_rtsp_port")),
            "experimental": bootstrap.get("experimental", True),
        }

    async def async_camera_image(self, width=None, height=None):
        camera_info = self._get_camera_info()
        cover_url = camera_info.get("cover_url") if camera_info else None
        if not cover_url:
            return self._last_image

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(cover_url) as response:
                if response.status != 200:
                    return self._last_image
                self._last_image = await response.read()
        except Exception as err:
            _LOGGER.debug("Failed to fetch cover image for %s: %s", self.name, err)
        return self._last_image