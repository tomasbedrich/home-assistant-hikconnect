import asyncio
import contextlib
import logging
import secrets
import time

import aiohttp
from hikconnect.api import HikConnect
from hikconnect.exceptions import HikConnectError
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import g711
from .const import DOMAIN
from .rtsp import RtspServer, parameter_sets

try:
    from homeassistant.components.camera import CameraEntityCapabilities
except ImportError:  # HA < 2026.11, no two-way audio
    CameraEntityCapabilities = None

# Every snapshot makes the device capture and upload a new image, the official app
# does it at most once per 10 minutes. Don't let the frontend (refreshing every 10 s)
# hammer the cloud.
SNAPSHOT_MIN_INTERVAL = 60  # seconds
# HA waits 10 s for a still image. Some devices answer the capture slowly or never
# (DS-KD8003, issue #6), leave the rest of the time to the live stream fallback.
SNAPSHOT_TIMEOUT = 5  # seconds
# Frames buffered per stream client, a client lagging further behind is disconnected.
STREAM_QUEUE_SIZE = 100
# go2rtc reconnects to add the microphone and retries a slow DESCRIBE right away, keep the cloud
# stream running for it after the last client left
STREAM_LINGER = 3  # seconds
# the browser stops sending when the talk button is released
TALK_IDLE_TIMEOUT = 3  # seconds
# browser audio packets (20 ms each in issue #83), more queued means the talk lags behind
TALK_QUEUE_SIZE = 10
# don't hammer the cloud with a new session for every packet when talking fails
TALK_RETRY_DELAY = 5  # seconds
TALK_SEND_TIMEOUT = 5  # seconds
# the app sends 20 ms packets in real time, 160 bytes of G.711 at 8 kHz (talk.py of hikconnect)
TALK_CHUNK, TALK_CHUNK_TIME = 160, 0.02
# device codecs of hikconnect.talk.CODECS the browser G.711 can be converted to
TALK_CODECS = {1: "PCMU", 2: "PCMA"}

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
    if new_entities:
        async_add_entities(new_entities)


async def _rtsp_server(hass: HomeAssistant) -> RtspServer:
    """The RTSP server of all cameras, started on the first use."""
    if (server := hass.data.get(f"{DOMAIN}_rtsp")) is None:
        server = hass.data[f"{DOMAIN}_rtsp"] = RtspServer(_Cameras(hass))
        await server.start()

        async def stop(_event):
            await server.stop()

        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop)
    return server


class _Cameras:
    """The cameras of the currently loaded config entry, looked up by the RTSP server."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass

    def get(self, path):
        return self.hass.data.get(DOMAIN, {}).get("cameras", {}).get(path)


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
        self.parameter_sets: dict[int, bytes] = {}
        self._talk_queue: asyncio.Queue | None = None
        self._talk_retry_at = 0.0
        self._talk_lagged = 0
        # RTSP clients of the device audio, called with PCMU during a talk
        self.audio_listeners: set = set()
        self._attr_supported_features = CameraEntityFeature.STREAM
        # support_talk 1 = full duplex; 3 = push to talk needs other commands, not implemented
        self.supports_talk = device_info.get("talk") == 1
        _LOGGER.debug(
            "%s of %s (%s): support_talk %s",
            camera_info["name"],
            device_info["serial"],
            device_info["type"],
            device_info.get("talk"),
        )
        if CameraEntityCapabilities is not None:
            self._attr_camera_entity_capabilities = CameraEntityCapabilities(
                two_way_audio=self.supports_talk
            )
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
                async with asyncio.timeout(SNAPSHOT_TIMEOUT):
                    self._image = await self._api.get_snapshot(
                        self._device_info["serial"],
                        self._camera_info["channel_number"],
                        is_encrypted=bool(self._device_info.get("is_encrypted")),
                        verification_code=self._verification_code,
                    )
            except (HikConnectError, aiohttp.ClientError, TimeoutError) as exc:
                _LOGGER.warning(
                    "Failed to get snapshot of %s, using live stream: %s",
                    self.name,
                    str(exc) or f"no response in {SNAPSHOT_TIMEOUT} s",
                )
                try:
                    if stream := await self.async_create_stream():
                        # full size, HA scales the cached image for each request
                        self._image = (
                            await stream.async_get_image(wait_for_next_keyframe=True) or self._image
                        )
                except HomeAssistantError as err:
                    _LOGGER.warning("Failed to get stream image of %s: %s", self.name, err)
            return self._image

    async def stream_source(self) -> str | None:
        # HA's go2rtc and stream run on the HA host
        port = (await _rtsp_server(self.hass)).port
        return f"rtsp://127.0.0.1:{port}/{self.stream_path}?auth={self.token}"

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
                self.hass.async_create_background_task(
                    self._stop_producer(), f"hikconnect stop live stream {self.entity_id}"
                )

    async def _stop_producer(self) -> None:
        await asyncio.sleep(STREAM_LINGER)
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
                if frame.keyframe and (params := parameter_sets(frame.codec, frame.data)):
                    self.parameter_sets = params
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

    def talk(self, codec: str, audio: bytes) -> None:
        """Audio from the browser microphone (G.711 `codec`), start a talk session on the first."""
        if (queue := self._talk_queue) is None:
            if time.monotonic() < self._talk_retry_at:
                return
            queue = self._talk_queue = asyncio.Queue(TALK_QUEUE_SIZE)
            self._talk_lagged = 0
            self.hass.async_create_background_task(
                self._talk(queue, codec), f"hikconnect talk {self.entity_id}"
            )
        if queue.full():
            _LOGGER.debug("Talk to %s lags behind, dropping audio", self.name)
            self._talk_lagged += 1
        else:
            queue.put_nowait(audio)

    async def _talk(self, queue: asyncio.Queue, codec: str) -> None:
        _LOGGER.debug("Starting talk to %s, %s audio from the browser", self.name, codec)
        received = chunks = dropped = 0
        failed = True
        device_audio = None
        try:
            async with self._api.talk(
                self._device_info["serial"], self._camera_info["channel_number"]
            ) as session:
                device_codec = TALK_CODECS.get(session.codec)
                _LOGGER.debug(
                    "Talk to %s started, device codec %s", self.name, session.codec_name
                )
                if device_codec is None:
                    _LOGGER.warning(
                        "Talk to %s is not supported: the device wants %s audio",
                        self.name,
                        session.codec_name,
                    )
                    return
                convert = g711.converter(codec, device_codec)
                device_audio = self.hass.async_create_background_task(
                    self._device_audio(session, g711.converter(device_codec, "PCMU")),
                    f"hikconnect device audio {self.entity_id}",
                )
                # recorded while the session was starting, sending it would delay all the rest
                while not queue.empty():
                    queue.get_nowait()
                    dropped += 1
                buffer = b""
                while True:
                    try:
                        async with asyncio.timeout(TALK_IDLE_TIMEOUT):
                            audio = await queue.get()
                    except TimeoutError:
                        _LOGGER.debug("No audio for %s s, ending talk to %s", TALK_IDLE_TIMEOUT, self.name)
                        failed = False
                        break
                    received += 1
                    buffer += convert(audio)
                    while len(buffer) >= TALK_CHUNK:
                        async with asyncio.timeout(TALK_SEND_TIMEOUT):
                            await session.send(buffer[:TALK_CHUNK])
                        buffer = buffer[TALK_CHUNK:]
                        chunks += 1
                        # spread a long packet over its real time, but send a backlog left by
                        # network jitter at once or it would never shrink
                        if len(buffer) >= TALK_CHUNK and queue.empty():
                            await asyncio.sleep(TALK_CHUNK_TIME)
        except (HikConnectError, aiohttp.ClientError, OSError, TimeoutError) as exc:
            _LOGGER.warning("Talk to %s failed: %s", self.name, str(exc) or repr(exc))
        finally:
            if device_audio:
                device_audio.cancel()
            if self._talk_queue is queue:
                self._talk_queue = None
            if failed:
                self._talk_retry_at = time.monotonic() + TALK_RETRY_DELAY
            _LOGGER.debug(
                "Talk to %s ended: %d packets from the browser (%d dropped at start, "
                "%d lagging behind), %d sent to the device%s",
                self.name,
                received + dropped + self._talk_lagged,
                dropped,
                self._talk_lagged,
                chunks,
                f", next try in {TALK_RETRY_DELAY} s" if failed else "",
            )

    async def _device_audio(self, session, convert) -> None:
        """Pass the device audio of a talk session on to the RTSP clients."""
        first = True
        with contextlib.suppress(HikConnectError):  # the talk itself reports it
            async for audio in session.received():
                if first:
                    _LOGGER.debug(
                        "Device audio of %s comes in packets of %d bytes", self.name, len(audio)
                    )
                    first = False
                audio = convert(audio)
                for send in self.audio_listeners:
                    send(audio)

    async def async_will_remove_from_hass(self) -> None:
        if self._producer:
            self._producer.cancel()
            self._producer = None
        self._send(None)
        await super().async_will_remove_from_hass()
