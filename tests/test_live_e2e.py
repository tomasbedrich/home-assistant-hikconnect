"""The live video path judged by real consumers: HA stream (PyAV) and go2rtc.

Each encoded frame is a flat picture whose luma encodes the frame index. Decoded frames must
map back to consecutive indices (the fake upstream loops, so modulo FRAMES) without gaps or
corruption. Sockets are needed: the test HTTP server is read over TCP, go2rtc is a subprocess.
"""

import asyncio
import os
import socket
import subprocess
from fractions import Fraction
from pathlib import Path
from unittest.mock import patch

import aiohttp
import av
import numpy as np
import pytest
from homeassistant.core import HomeAssistant
from homeassistant.core_config import async_process_ha_core_config

from custom_components.hikconnect.const import DOMAIN
from test_camera import Frame, _api, _setup

GO2RTC_BIN = Path(
    os.environ.get("GO2RTC_BIN", "~/.cache/hikconnect-test/go2rtc-1.9.15/go2rtc")
).expanduser()
FFMPEG_DIR = "/opt/homebrew/bin"
FPS, FRAMES, GOP = 25, 50, 25
WIDTH, HEIGHT = 320, 240
DECODE_FRAMES = 60  # more than FRAMES, so the upstream loop is crossed


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _level(index):
    return 16 + 4 * index


def _encode(codec):
    """Annex-B access units of FRAMES flat pictures, picture i has luma _level(i)."""
    name, options = {
        "h264": ("libx264", {"g": str(GOP), "bf": "0", "tune": "zerolatency", "crf": "10"}),
        "h265": (
            "libx265",
            {"x265-params": f"keyint={GOP}:min-keyint={GOP}:scenecut=0:bframes=0:repeat-headers=1:log-level=none", "crf": "10"},
        ),
    }[codec]
    if name not in av.codecs_available:
        pytest.skip(f"{name} not available in PyAV")
    ctx = av.CodecContext.create(name, "w")
    ctx.width, ctx.height, ctx.pix_fmt = WIDTH, HEIGHT, "yuv420p"
    ctx.time_base = Fraction(1, FPS)
    ctx.options = options
    packets = []
    for i in range(FRAMES):
        yuv = np.full((HEIGHT * 3 // 2, WIDTH), 128, dtype=np.uint8)
        yuv[:HEIGHT] = _level(i)
        frame = av.VideoFrame.from_ndarray(yuv, format="yuv420p")
        frame.pts = i
        packets += ctx.encode(frame)
    packets += ctx.encode(None)
    assert len(packets) == FRAMES
    return [(bytes(p), p.is_keyframe) for p in packets]


def _fake_live_stream(codec):
    units = _encode(codec)

    async def live_stream(serial, channel, main_stream=True, verification_code=None):
        pts = 0
        while True:
            for data, keyframe in units:
                yield Frame(codec, pts, data, keyframe)
                pts += 90000 // FPS
                await asyncio.sleep(1 / FPS)

    return live_stream


def _decode(url, options=None, count=DECODE_FRAMES):
    """Blocking: open the URL like HA stream does and return [(seconds, index, spread)]."""
    out = []
    with av.open(url, options=options or {}, timeout=15) as container:
        stream = container.streams.video[0]
        for frame in container.decode(stream):
            luma = frame.to_ndarray()[: frame.height]
            out.append((frame.time, (luma.mean() - 16) / 4, np.ptp(luma)))
            if len(out) == count:
                break
    return out


def _check(decoded, skipped=0):
    """skipped: pictures a consumer may drop from the start (go2rtc output of HEVC over RTSP)."""
    assert len(decoded) == DECODE_FRAMES, "stream ended early"
    times = [t for t, _, _ in decoded]
    if skipped:  # ffmpeg may emit the first picture without pts
        times = [t for t in times if t is not None]
    assert None not in times, f"frames without pts: {times}"
    assert all(b > a for a, b in zip(times, times[1:])), f"pts not increasing: {times}"
    assert 0.6 * DECODE_FRAMES / FPS < times[-1] - times[0] < 1.5 * DECODE_FRAMES / FPS
    assert max(spread for *_, spread in decoded) <= 8, "corrupted picture"
    indices = [round(i) for _, i, _ in decoded]
    assert all(abs(i - round(i)) < 0.5 for _, i, _ in decoded)
    assert indices[0] % GOP <= skipped, f"started at a non keyframe: {indices}"
    assert indices[1:] == [(i + 1) % FRAMES for i in indices[:-1]], f"gap or corruption: {indices}"


async def _wait_producer_stopped(camera):
    for _ in range(100):
        if camera._producer is None:
            return
        await asyncio.sleep(0.1)
    pytest.fail("upstream must stop when the last client disconnects")


async def _camera(hass, hass_client_no_auth, codec):
    api = _api()
    api.live_stream.side_effect = _fake_live_stream(codec)
    await _setup(hass, api)
    client = await hass_client_no_auth()
    await async_process_ha_core_config(
        hass, {"internal_url": str(client.make_url("/")).rstrip("/")}
    )
    entity_id = hass.states.async_entity_ids("camera")[0]
    camera = next(iter(hass.data[DOMAIN]["cameras"].values()))
    camera.entity_id = entity_id
    return camera, await camera.stream_source()


def _free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def go2rtc(tmp_path):
    if not GO2RTC_BIN.exists():
        pytest.skip(f"go2rtc binary not found at {GO2RTC_BIN}, set GO2RTC_BIN")
    api_port, rtsp_port = _free_port(), _free_port()
    config = tmp_path / "go2rtc.yaml"
    config.write_text(f"""app:
  modules: [api, exec, ffmpeg, http, mjpeg, onvif, rtmp, rtsp, srtp, webrtc, ws]
api:
  listen: "127.0.0.1:{api_port}"
exec:
  allow_paths:
    - ffmpeg
rtsp:
  listen: "127.0.0.1:{rtsp_port}"
webrtc:
  listen: "127.0.0.1:{_free_port()}/tcp"
  ice_servers: []
log:
  level: debug
""")
    log = (tmp_path / "go2rtc.log").open("wb")
    process = subprocess.Popen(
        [str(GO2RTC_BIN), "-config", str(config)],
        stdout=log,
        stderr=subprocess.STDOUT,
        env={**os.environ, "PATH": f"{FFMPEG_DIR}:{os.environ['PATH']}"},  # allow_paths: [ffmpeg]
    )
    base = f"http://127.0.0.1:{api_port}"
    session = aiohttp.ClientSession()
    try:
        for _ in range(100):
            try:
                async with session.get(f"{base}/api") as res:
                    if res.status == 200:
                        break
            except aiohttp.ClientError:
                await asyncio.sleep(0.1)
        else:
            pytest.fail("go2rtc did not start: " + (tmp_path / "go2rtc.log").read_text())

        async def add_stream(name, source):
            # the way HA's go2rtc provider adds a camera
            params = [("name", name), ("src", source), ("src", f"ffmpeg:{name}#audio=opus#query=log_level=debug")]
            async with session.put(f"{base}/api/streams", params=params) as res:
                assert res.status == 200, await res.text()
            return f"rtsp://127.0.0.1:{rtsp_port}/{name}"

        yield add_stream
    finally:
        await session.close()
        process.terminate()
        process.wait(10)
        log.close()


@pytest.mark.parametrize("codec", ["h264", "h265"])
@pytest.mark.enable_socket
async def test_ha_stream_decodes(hass: HomeAssistant, hass_client_no_auth, codec):
    camera, source = await _camera(hass, hass_client_no_auth, codec)
    # the options HA stream adds to rtsp:// sources (stream/__init__.py _convert_stream_options)
    options = {"rtsp_flags": "prefer_tcp", "stimeout": "5000000"} if source.startswith("rtsp://") else {}
    decoded = await hass.async_add_executor_job(_decode, source, options)
    # ffmpeg's RTSP demuxer gives the first picture no pts, the same with go2rtc's RTSP output
    _check(decoded, skipped=1 if options else 0)
    await _wait_producer_stopped(camera)


@pytest.mark.parametrize("codec", ["h264", "h265"])
@pytest.mark.enable_socket
async def test_go2rtc_decodes(hass: HomeAssistant, hass_client_no_auth, go2rtc, codec):
    camera, source = await _camera(hass, hass_client_no_auth, codec)
    url = await go2rtc("cam", source)
    decoded = await hass.async_add_executor_job(_decode, url, {"rtsp_transport": "tcp"})
    _check(decoded, skipped=3)
    await _wait_producer_stopped(camera)


@pytest.mark.enable_socket
async def test_go2rtc_rotated_decodes(hass: HomeAssistant, hass_client_no_auth, go2rtc):
    """HA orientation setting: go2rtc transcodes the source through ffmpeg."""
    camera, source = await _camera(hass, hass_client_no_auth, "h264")
    url = await go2rtc("cam", f"ffmpeg:{source}#video=h264#audio=copy#rotate=90")
    decoded = await hass.async_add_executor_job(_decode, url, {"rtsp_transport": "tcp"})
    _check(decoded, skipped=GOP)
    await _wait_producer_stopped(camera)


@pytest.mark.parametrize("codec", ["h264", "h265"])
@pytest.mark.enable_socket
async def test_fragmented_decodes(hass: HomeAssistant, hass_client_no_auth, go2rtc, codec):
    """NAL units longer than an RTP packet go out as fragmentation units (FU-A / H.265 FU)."""
    with patch("custom_components.hikconnect.rtsp.MAX_PAYLOAD", 50):
        camera, source = await _camera(hass, hass_client_no_auth, codec)
        options = {"rtsp_flags": "prefer_tcp", "stimeout": "5000000"}
        _check(await hass.async_add_executor_job(_decode, source, options), skipped=1)
        url = await go2rtc("cam", source)
        decoded = await hass.async_add_executor_job(_decode, url, {"rtsp_transport": "tcp"})
        _check(decoded, skipped=3)
    await _wait_producer_stopped(camera)
