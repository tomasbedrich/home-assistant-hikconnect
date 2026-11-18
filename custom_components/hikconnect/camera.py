import asyncio
import contextlib
import hmac
import logging
import secrets
import time
from http import HTTPStatus

import aiohttp
from aiohttp import web
from hikconnect.api import HikConnect
from hikconnect.exceptions import HikConnectError
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.network import get_url

from .const import DOMAIN
from .tsmux import TsMuxer

# Every snapshot makes the device capture and upload a new image, the official app
# does it at most once per 10 minutes. Don't let the frontend (refreshing every 10 s)
# hammer the cloud.
SNAPSHOT_MIN_INTERVAL = 60  # seconds
# Frames buffered per stream client, a client lagging further behind is disconnected.
STREAM_QUEUE_SIZE = 100

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    data = hass.data[DOMAIN]
    api, coordinator = data["api"], data["coordinator"]
    verification_code = entry.options.get("verification_code") or None

    new_entities = [
        HikConnectCamera(api, entry.entry_id, device_info, camera_info, verification_code)
        for device_info in coordinator.data
        for camera_info in device_info["cameras"]
        if camera_info["is_shown"]
    ]
    data["cameras"] = {camera.stream_path: camera for camera in new_entities}
    # drop cameras of channels skipped by the coordinator, registered by previous versions
    registry = er.async_get(hass)
    unique_ids = {camera.unique_id for camera in new_entities}
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.domain == "camera" and entity.unique_id not in unique_ids:
            _LOGGER.debug("Removing camera %s of a skipped channel", entity.entity_id)
            registry.async_remove(entity.entity_id)
    if not hass.data.get(f"{DOMAIN}_stream_view"):
        hass.data[f"{DOMAIN}_stream_view"] = True
        hass.http.register_view(HikConnectStreamView(hass))
    if new_entities:
        async_add_entities(new_entities)


class HikConnectStreamView(HomeAssistantView):
    """Live video of a camera as MPEG-TS, the stream source of HA stream / go2rtc."""

    url = "/api/hikconnect/stream/{entry_id}/{serial}/{channel}"
    name = "api:hikconnect:stream"
    # HA stream (PyAV) and go2rtc open the URL without HA credentials, the per-camera
    # token in the `auth` query parameter (redacted in HA stream logs) is checked instead.
    requires_auth = False

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    async def get(
        self, request: web.Request, entry_id: str, serial: str, channel: str
    ) -> web.StreamResponse:
        cameras = self.hass.data.get(DOMAIN, {}).get("cameras", {})
        camera = cameras.get(f"{entry_id}/{serial}/{channel}")
        token = request.query.get("auth", "").encode()
        if camera is None or not hmac.compare_digest(token, camera.token.encode()):
            _LOGGER.debug("Refused stream request for %s/%s/%s", entry_id, serial, channel)
            return web.Response(status=HTTPStatus.UNAUTHORIZED)
        _LOGGER.debug(
            "Stream client %s connected to %s", request.remote, camera.entity_id
        )

        response = web.StreamResponse(headers={"Content-Type": "video/mp2t"})
        await response.prepare(request)
        muxer = TsMuxer()
        with contextlib.suppress(ConnectionResetError):
            async with contextlib.aclosing(camera.frames()) as frames:
                async for frame in frames:
                    await response.write(muxer.mux(frame))
        _LOGGER.debug(
            "Stream client %s disconnected from %s", request.remote, camera.entity_id
        )
        return response


class HikConnectCamera(Camera):
    """Hik-Connect channel: still image captured via the cloud, live video relayed from it."""

    def __init__(
        self,
        api: HikConnect,
        entry_id: str,
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
        self.token = secrets.token_urlsafe()
        self.stream_path = f"{entry_id}/{device_info['serial']}/{camera_info['channel_number']}"
        self._queues: set[asyncio.Queue] = set()
        self._producer: asyncio.Task | None = None
        self._attr_supported_features = CameraEntityFeature.STREAM
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
                _LOGGER.warning(
                    "Failed to get snapshot of %s, using live stream: %s", self.name, exc
                )
                try:
                    if stream := await self.async_create_stream():
                        self._image = (
                            await stream.async_get_image(width, height, wait_for_next_keyframe=True)
                            or self._image
                        )
                except HomeAssistantError as err:
                    _LOGGER.warning("Failed to get stream image of %s: %s", self.name, err)
            return self._image

    async def stream_source(self) -> str | None:
        base_url = get_url(self.hass, allow_external=False)
        return f"{base_url}/api/hikconnect/stream/{self.stream_path}?auth={self.token}"

    async def frames(self):
        """Yield live frames, starting at a keyframe. All clients share one cloud stream."""
        queue: asyncio.Queue = asyncio.Queue(STREAM_QUEUE_SIZE)
        self._queues.add(queue)
        if self._producer is None or self._producer.done():
            self._producer = self.hass.async_create_background_task(
                self._produce(), f"hikconnect live stream {self.entity_id}"
            )
        try:
            started = False
            while (frame := await queue.get()) is not None:
                started = started or frame.keyframe
                if started:
                    yield frame
        finally:
            self._queues.discard(queue)
            if not self._queues and self._producer:
                _LOGGER.debug("Stopping live stream of %s, no clients left", self.name)
                self._producer.cancel()
                self._producer = None

    async def _produce(self) -> None:
        _LOGGER.debug("Starting live stream of %s", self.name)
        count = 0
        try:
            async for frame in self._api.live_stream(
                self._device_info["serial"],
                self._camera_info["channel_number"],
                main_stream=True,
                verification_code=self._verification_code,
            ):
                count += 1
                if count == 1:
                    _LOGGER.debug(
                        "First frame of %s: %s, keyframe %s", self.name, frame.codec, frame.keyframe
                    )
                self._send(frame)
        except (HikConnectError, aiohttp.ClientError) as exc:
            _LOGGER.warning("Live stream of %s failed: %s", self.name, exc)
        finally:
            _LOGGER.debug("Live stream of %s ended after %d frames", self.name, count)
        self._send(None)

    def _send(self, frame) -> None:
        """Queue a frame for every client, None ends the clients' streams."""
        for queue in self._queues:
            if queue.full():
                _LOGGER.debug("Stream client of %s lags behind, disconnecting it", self.name)
                while not queue.empty():
                    queue.get_nowait()
                queue.put_nowait(None)
            else:
                queue.put_nowait(frame)

    async def async_will_remove_from_hass(self) -> None:
        if self._producer:
            self._producer.cancel()
            self._producer = None
        self._send(None)
        await super().async_will_remove_from_hass()
