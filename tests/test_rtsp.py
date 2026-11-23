"""The RTSP server: auth, SDP, the ONVIF backchannel and the talk session it feeds."""

import asyncio
import contextlib
import logging
import struct
from unittest.mock import patch
from urllib.parse import urlsplit

import av
import numpy as np
import pytest
from homeassistant.core import HomeAssistant

from custom_components.hikconnect import g711
from custom_components.hikconnect.const import DOMAIN
from test_camera import _api, _setup

REQUIRE = "Require: www.onvif.org/ver20/backchannel"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class _Client:
    """Bare RTSP client over TCP, like go2rtc's."""

    def __init__(self, reader, writer, url):
        self.reader, self.writer, self.url, self.cseq = reader, writer, url, 0

    async def request(self, method, url=None, *headers):
        self.cseq += 1
        lines = [f"{method} {url or self.url} RTSP/1.0", f"CSeq: {self.cseq}", *headers]
        self.writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
        while (first := await self.reader.readexactly(1)) == b"$":
            _, length = struct.unpack(">BH", await self.reader.readexactly(3))
            await self.reader.readexactly(length)
        status = (first + await self.reader.readuntil(b"\r\n")).decode().split(" ", 2)[1]
        headers = {}
        while (line := (await self.reader.readuntil(b"\r\n")).decode().strip()):
            key, _, value = line.partition(":")
            headers[key.lower()] = value.strip()
        body = await self.reader.readexactly(int(headers.get("content-length", 0)))
        return int(status), headers, body.decode()

    def send_audio(self, channel, payload_type, payload, seq):
        rtp = struct.pack(">BBHII", 0x80, payload_type, seq, 0, 1234) + payload
        self.writer.write(struct.pack(">cBH", b"$", channel, len(rtp)) + rtp)


@contextlib.asynccontextmanager
async def _client(url):
    split = urlsplit(url)
    reader, writer = await asyncio.open_connection(split.hostname, split.port)
    try:
        yield _Client(reader, writer, url)
    finally:
        writer.close()


async def _source(hass):
    camera = next(iter(hass.data[DOMAIN]["cameras"].values()))
    camera.entity_id = hass.states.async_entity_ids("camera")[0]
    return camera, await camera.stream_source()


class _FakeTalk:
    def __init__(self, codec):
        self.codec, self.codec_name, self.sent = codec, {1: "G711_MU", 2: "G711_A"}.get(codec, "AAC"), []

    async def send(self, audio):
        self.sent.append(audio)


def _fake_talk(session):
    @contextlib.asynccontextmanager
    async def talk(serial, channel):
        talk.calls.append((serial, channel))
        yield session
        talk.closed += 1

    talk.calls, talk.closed = [], 0
    return talk


async def test_describe(hass: HomeAssistant, socket_enabled):
    await _setup(hass, _api())
    camera, url = await _source(hass)
    assert url.startswith("rtsp://")

    async with _client(url) as client:
        status, _, _ = await client.request("DESCRIBE", url.replace(camera.token, "wrong"))
        assert status == 401
    async with _client(url) as client:
        status, _, _ = await client.request("SETUP", url + "/trackID=0", "Transport: RTP/AVP/TCP;interleaved=0-1")
        assert status == 455

    async with _client(url) as client:
        status, headers, sdp = await client.request("DESCRIBE")
        assert status == 200
        assert headers["content-type"] == "application/sdp"
        assert "m=video 0 RTP/AVP 96" in sdp and "H264/90000" in sdp
        assert "sprop-parameter-sets=" in sdp
        assert "m=audio" not in sdp, "backchannel only for clients asking for it"
        base = headers["content-base"]
        status, _, _ = await client.request("SETUP", base + "trackID=0", "Transport: RTP/AVP;unicast;client_port=5000-5001")
        assert status == 461, "UDP is not served"
        status, headers, _ = await client.request("SETUP", base + "trackID=0", "Transport: RTP/AVP/TCP;unicast;interleaved=0-1")
        assert status == 200 and "interleaved=0-1" in headers["transport"]
        status, _, _ = await client.request("PLAY", base, f"Session: {headers['session'].split(';')[0]}")
        assert status == 200
        assert (await client.reader.readexactly(1)) == b"$"
    for _ in range(100):
        if camera._producer is None:
            break
        await asyncio.sleep(0.05)
    assert camera._producer is None, "upstream must stop when the last client disconnects"


@pytest.mark.parametrize(
    ("device_codec", "browser_pt", "convert"),
    [(1, 0, None), (2, 0, ("PCMU", "PCMA")), (1, 8, ("PCMA", "PCMU"))],
)
async def test_talk(hass: HomeAssistant, socket_enabled, device_codec, browser_pt, convert):
    api = _api()
    session = _FakeTalk(device_codec)
    api.talk = _fake_talk(session)
    await _setup(hass, api)
    camera, url = await _source(hass)

    audio = bytes(range(256)) * 4  # go2rtc repacks G.711 into 1024 byte packets
    with patch("custom_components.hikconnect.camera.TALK_IDLE_TIMEOUT", 0.3):
        async with _client(url) as client:
            status, headers, sdp = await client.request("DESCRIBE", None, REQUIRE)
            assert status == 200
            assert "a=sendonly" in sdp and "PCMU/8000" in sdp and "PCMA/8000" in sdp
            base = headers["content-base"]
            await client.request("SETUP", base + "trackID=0", "Transport: RTP/AVP/TCP;interleaved=0-1")
            await client.request("SETUP", base + "trackID=1", "Transport: RTP/AVP/TCP;interleaved=2-3")
            await client.request("PLAY", base)
            for seq in range(2):
                client.send_audio(2, browser_pt, audio, seq)
            await client.writer.drain()
            for _ in range(100):
                if api.talk.closed:
                    break
                await asyncio.sleep(0.05)

    assert api.talk.calls == [("D1", 1)]
    assert api.talk.closed == 1, "the session ends when the browser stops sending"
    assert [len(chunk) for chunk in session.sent] == [160] * (2 * 1024 // 160)
    expected = audio * 2 if convert is None else g711.converter(*convert)(audio * 2)
    assert b"".join(session.sent) == expected[: 160 * len(session.sent)]


async def test_talk_unsupported_codec(hass: HomeAssistant, socket_enabled, caplog):
    api = _api()
    session = _FakeTalk(7)  # AAC
    api.talk = _fake_talk(session)
    await _setup(hass, api)
    camera, url = await _source(hass)
    async with _client(url) as client:
        _, headers, _ = await client.request("DESCRIBE", None, REQUIRE)
        await client.request("SETUP", headers["content-base"] + "trackID=1", "Transport: RTP/AVP/TCP;interleaved=2-3")
        client.send_audio(2, 0, b"\xff" * 1024, 0)
        await client.writer.drain()
        for _ in range(100):
            if api.talk.closed:
                break
            await asyncio.sleep(0.05)
    assert api.talk.closed == 1
    assert session.sent == []
    assert "device wants AAC audio" in caplog.text


@pytest.mark.parametrize(("codec", "table"), [("pcm_mulaw", g711.ULAW), ("pcm_alaw", g711.ALAW)])
def test_g711_matches_ffmpeg(codec, table):
    ctx = av.CodecContext.create(codec, "r")
    ctx.sample_rate, ctx.layout = 8000, "mono"
    decoded = np.concatenate([f.to_ndarray().ravel() for f in ctx.decode(av.Packet(bytes(range(256))))])
    assert decoded.tolist() == table


def test_g711_conversion():
    to_alaw = g711.converter("PCMU", "PCMA")
    for code, value in enumerate(g711.ULAW):
        converted = g711.ALAW[to_alaw(bytes([code]))[0]]
        # A-law steps are at most 1024 apart, the nearest one is within half of that
        assert abs(converted - value) <= 512
    assert g711.converter("PCMU", "PCMU")(b"\x01\x02") == b"\x01\x02"


async def test_describe_retry_keeps_stream(hass: HomeAssistant, socket_enabled):
    """A client giving up on a slow cloud stream finds it running when it retries."""
    api = _api()
    live_stream = api.live_stream.side_effect

    async def slow_live_stream(*args, **kwargs):
        await asyncio.sleep(0.6)
        async for frame in live_stream(*args, **kwargs):
            yield frame

    api.live_stream.side_effect = slow_live_stream
    await _setup(hass, api)
    camera, url = await _source(hass)
    with patch("custom_components.hikconnect.rtsp.DESCRIBE_TIMEOUT", 0.3):
        async with _client(url) as client:
            assert (await client.request("DESCRIBE"))[0] == 503
        async with _client(url) as client:
            assert (await client.request("DESCRIBE"))[0] == 200
    api.live_stream.assert_called_once()


async def test_two_way_audio_capability(hass: HomeAssistant):
    await _setup(hass, _api())
    camera = next(iter(hass.data[DOMAIN]["cameras"].values()))
    if not hasattr(camera, "camera_entity_capabilities"):
        pytest.skip("HA < 2026.11 has no two-way audio")
    assert camera.camera_entity_capabilities.two_way_audio
