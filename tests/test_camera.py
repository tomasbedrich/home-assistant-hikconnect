import asyncio
from collections import namedtuple
from fractions import Fraction
from unittest.mock import AsyncMock, MagicMock, patch

import av
import numpy as np
import pytest
from hikconnect.exceptions import SnapshotError
from homeassistant.components.camera import async_get_image
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hikconnect.const import DOMAIN

JPEG = b"\xff\xd8jpeg"
# same shape as hikconnect.stream.Frame
Frame = namedtuple("Frame", "codec pts data keyframe")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _h264_frames(count=50, fps=25):
    """Encode a moving gradient as Annex-B H.264 access units."""
    ctx = av.CodecContext.create("libx264", "w")
    ctx.width, ctx.height, ctx.pix_fmt = 320, 240, "yuv420p"
    ctx.time_base = Fraction(1, fps)
    ctx.options = {"g": str(fps), "bf": "0", "tune": "zerolatency"}
    packets = []
    for i in range(count):
        img = np.full((240, 320, 3), (i * 5) % 256, dtype=np.uint8)
        frame = av.VideoFrame.from_ndarray(img, format="rgb24").reformat(format="yuv420p")
        frame.pts = i
        packets += ctx.encode(frame)
    packets += ctx.encode(None)
    return [(bytes(p), p.is_keyframe) for p in packets]


async def _agen(items):
    for i in items:
        yield i


async def _live_stream(serial, channel, main_stream=True, verification_code=None):
    units, pts = _h264_frames(), 0
    while True:
        for data, keyframe in units:
            yield Frame("h264", pts, data, keyframe)
            pts += 3600
            await asyncio.sleep(0.04)


def _api():
    api = MagicMock()
    api.login = AsyncMock()
    api.is_refresh_login_needed.return_value = False
    api.get_devices = lambda: _agen([{
        "id": "DS-KV-1", "name": "Doorbell", "serial": "D1", "type": "DS-KV6113",
        "version": "V1", "locks": {1: 1}, "local_ip": None, "wan_ip": None,
        "is_online": True, "wifi_signal": None, "update_available": False, "is_encrypted": True,
    }])
    api.get_cameras = lambda serial: _agen([
        {"id": "cam1", "name": "Entrance", "channel_number": 1, "signal_status": 1, "is_shown": 1},
        {"id": "cam2", "name": "Hidden", "channel_number": 2, "signal_status": 1, "is_shown": 0},
        # intercom channel without a lock, issue #54
        {"id": "cam3", "name": "Phantom", "channel_number": 3, "signal_status": 0, "is_shown": 1},
    ])
    api.get_areas = lambda serial: _agen([])
    api.get_call_status = AsyncMock(return_value={"status": "idle", "info": {}})
    api.get_snapshot = AsyncMock(return_value=JPEG)
    api.live_stream = MagicMock(side_effect=_live_stream)
    api.close = AsyncMock()
    return api


async def _setup(hass, api):
    entry = MockConfigEntry(
        domain=DOMAIN, version=2,
        data={"username": "u", "password": "p", "base_url": "https://api.hik-connect.com"},
        options={"verification_code": "ABCDEF"},
    )
    entry.add_to_hass(hass)
    with patch("custom_components.hikconnect.HikConnect", return_value=api):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


async def test_camera(hass: HomeAssistant):
    api = _api()
    entry = await _setup(hass, api)
    # a camera of a skipped channel registered by a previous version is removed on reload
    registry = er.async_get(hass)
    stale = registry.async_get_or_create(
        "camera", DOMAIN, "hikconnect-DS-KV-1-cam3-camera", config_entry=entry
    )
    with patch("custom_components.hikconnect.HikConnect", return_value=api):
        assert await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    assert registry.async_get(stale.entity_id) is None

    cameras = hass.states.async_entity_ids("camera")
    assert len(cameras) == 1

    image = await async_get_image(hass, cameras[0])
    assert image.content == JPEG
    api.get_snapshot.assert_awaited_once_with("D1", 1, is_encrypted=True, verification_code="ABCDEF")

    await async_get_image(hass, cameras[0])
    assert api.get_snapshot.await_count == 1, "second call within interval must be cached"

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert "verification_code" in [str(k) for k in result["data_schema"].schema]


async def test_stream_view(hass: HomeAssistant, hass_client_no_auth):
    api = _api()
    await _setup(hass, api)
    client = await hass_client_no_auth()
    camera = next(iter(hass.data[DOMAIN]["cameras"].values()))
    path = f"/api/hikconnect/stream/{camera.stream_path}"

    assert (await client.get(path, params={"auth": "wrong"})).status == 401
    assert (await client.get(path)).status == 401
    assert (await client.get(path + "9", params={"auth": camera.token})).status == 401

    res = await client.get(path, params={"auth": camera.token})
    assert res.status == 200
    assert res.headers["Content-Type"] == "video/mp2t"
    data = await res.content.readexactly(188 * 10)
    assert data[::188] == b"\x47" * 10
    api.live_stream.assert_called_once_with("D1", 1, main_stream=True, verification_code="ABCDEF")

    res.close()
    for _ in range(50):
        if camera._producer is None:
            break
        await asyncio.sleep(0.05)
    assert camera._producer is None, "upstream must stop when the last client disconnects"


@pytest.mark.parametrize("expected_lingering_timers", [True])  # HLS idle timer of HA stream
async def test_snapshot_falls_back_to_stream(hass: HomeAssistant, hass_client_no_auth):
    """End to end: HA stream (PyAV) reads the view and decodes a JPEG."""
    api = _api()
    api.get_snapshot.side_effect = SnapshotError("meta 2009")
    assert await async_setup_component(hass, "stream", {})
    await _setup(hass, api)
    client = await hass_client_no_auth()
    await async_process_ha_core_config(
        hass, {"internal_url": str(client.make_url("/")).rstrip("/")}
    )
    entity_id = hass.states.async_entity_ids("camera")[0]
    camera = next(iter(hass.data[DOMAIN]["cameras"].values()))

    image = await async_get_image(hass, entity_id, timeout=30)
    assert image.content[:2] == b"\xff\xd8"
    assert image.content != JPEG

    await camera.stream.stop()
