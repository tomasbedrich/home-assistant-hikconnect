"""
Minimal RTSP server, the stream source of the cameras for go2rtc and HA stream.

Video goes out as RTP (H.264 RFC 6184, H.265 RFC 7798) interleaved in the RTSP TCP connection.
Clients asking for the ONVIF backchannel (go2rtc does) also get a receive-only G.711 audio
track: go2rtc sends the browser microphone into it, the camera passes it on to the talk session.
"""

import asyncio
import base64
import contextlib
import hmac
import logging
import os
import struct
import time
from urllib.parse import parse_qs, urlsplit

_LOGGER = logging.getLogger(__name__)

# go2rtc reads the DESCRIBE response with a 5 s deadline (pkg/rtsp/conn.go ReadResponse)
DESCRIBE_TIMEOUT = 4.5
# unauthenticated clients get little time and memory
AUTH_TIMEOUT = 10
MAX_HEADERS, MAX_BODY = 50, 4096
# go2rtc drops a source without data for 5 s, the cloud stream reconnects take longer
KEEPALIVE_INTERVAL = 2
MAX_PAYLOAD = 1400
PT_VIDEO = 96
PT_PCMU, PT_PCMA = 0, 8
AUDIO_CODECS = {PT_PCMU: "PCMU", PT_PCMA: "PCMA"}
BACKCHANNEL = "www.onvif.org/ver20/backchannel"


def nal_units(data: bytes) -> list[bytes]:
    """Split an Annex-B access unit into NAL units without start codes."""
    units, start = [], None
    i = data.find(b"\x00\x00\x01")
    while i != -1:
        if start is not None:
            units.append(data[start:i].rstrip(b"\x00"))
        start = i + 3
        i = data.find(b"\x00\x00\x01", start)
    if start is not None:
        units.append(data[start:])
    return [u for u in units if u]


def _nal_type(codec: str, nal: bytes) -> int:
    return nal[0] & 0x1F if codec == "h264" else (nal[0] >> 1) & 0x3F


def parameter_sets(codec: str, data: bytes) -> dict[int, bytes]:
    """VPS/SPS/PPS of a keyframe by NAL type."""
    types = (7, 8) if codec == "h264" else (32, 33, 34)
    return {t: nal for nal in nal_units(data) if (t := _nal_type(codec, nal)) in types}


def payloads(codec: str, nal: bytes) -> list[bytes]:
    """RTP payloads of one NAL unit: as is, or fragmented (FU-A / H.265 FU)."""
    if len(nal) <= MAX_PAYLOAD:
        return [nal]
    if codec == "h264":
        header, body = bytes([nal[0] & 0xE0 | 28]), nal[1:]
        fu = nal[0] & 0x1F
    else:
        header, body = bytes([nal[0] & 0x81 | 49 << 1, nal[1]]), nal[2:]
        fu = (nal[0] >> 1) & 0x3F
    chunks = [body[i : i + MAX_PAYLOAD] for i in range(0, len(body), MAX_PAYLOAD)]
    return [
        header + bytes([fu | (0x80 if i == 0 else 0) | (0x40 if i == len(chunks) - 1 else 0)]) + c
        for i, c in enumerate(chunks)
    ]


def sdp(codec: str, params: dict[int, bytes], backchannel: bool) -> str:
    def b64(*types):
        return ",".join(base64.b64encode(params[t]).decode() for t in types if t in params)

    if codec == "h264":
        rtpmap, fmtp = "H264/90000", "packetization-mode=1"
        if sets := b64(7, 8):
            fmtp += f";sprop-parameter-sets={sets}"
    else:
        rtpmap = "H265/90000"
        fmtp = ";".join(
            f"sprop-{name}={b64(t)}" for name, t in (("vps", 32), ("sps", 33), ("pps", 34)) if t in params
        )
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 0.0.0.0",
        "s=Hik-Connect",
        "t=0 0",
        f"m=video 0 RTP/AVP {PT_VIDEO}",
        f"a=rtpmap:{PT_VIDEO} {rtpmap}",
        *([f"a=fmtp:{PT_VIDEO} {fmtp}"] if fmtp else []),
        "a=control:trackID=0",
    ]
    if backchannel:
        # ONVIF backchannel: the client sends, G.711 is what the devices mostly talk and what
        # browsers offer, go2rtc does not transcode the microphone
        lines += [
            f"m=audio 0 RTP/AVP {PT_PCMU} {PT_PCMA}",
            f"a=rtpmap:{PT_PCMU} PCMU/8000",
            f"a=rtpmap:{PT_PCMA} PCMA/8000",
            "a=control:trackID=1",
            "a=sendonly",
        ]
    return "\r\n".join(lines) + "\r\n"


class RtspServer:
    """Serves `cameras[path]` (HikConnectCamera) at rtsp://127.0.0.1:port/<path>?auth=<token>."""

    def __init__(self, cameras: dict):
        self.cameras = cameras
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        _LOGGER.debug("RTSP server listening on port %d", self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            self._server.close_clients()
            await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        session = _Session(self, reader, writer)
        try:
            await session.run()
        except (ConnectionError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            await session.close()


class _Session:
    def __init__(self, server: RtspServer, reader, writer):
        self.server, self.reader, self.writer = server, reader, writer
        self.peer = writer.get_extra_info("peername")
        self.id = os.urandom(8).hex()
        self.camera = None
        self.frames = None
        self.first = None  # the frame read in DESCRIBE, sent first on PLAY
        self.channels: dict[int, int] = {}  # interleaved channel -> track
        self.video_channel = 0
        self.player: asyncio.Task | None = None
        self.ssrc = int.from_bytes(os.urandom(4))
        self.seq = self.rtp_ts = 0
        self.last_write = 0.0
        self.counts = {"video": 0, "audio": 0}

    async def run(self) -> None:
        while True:
            async with asyncio.timeout(None if self.camera else AUTH_TIMEOUT):
                request = await self._read()
            if request is None:
                continue
            method, url, headers = request
            status, extra, body = await self._request(method, url, headers)
            _LOGGER.debug(
                "RTSP %s %s %s: %s", self.peer, method, urlsplit(url).path, status
            )
            response = [f"RTSP/1.0 {status}", f"CSeq: {headers.get('cseq', '0')}", *extra]
            if self.channels or method == "SETUP":
                response.append(f"Session: {self.id}" + (";timeout=60" if method == "SETUP" else ""))
            if body:
                response.append(f"Content-Length: {len(body)}")
            self.writer.write(("\r\n".join(response) + "\r\n\r\n" + body).encode())
            await self.writer.drain()
            if method == "TEARDOWN" or (method == "DESCRIBE" and self.camera is None):
                return

    async def _line(self) -> str:
        data = await self.reader.readuntil(b"\r\n")
        return data[:-2].decode(errors="replace")

    async def _read(self):
        """The next request as (method, url, headers), None for an interleaved packet."""
        first = await self.reader.readexactly(1)
        if first == b"$":
            channel, length = struct.unpack(">BH", await self.reader.readexactly(3))
            self._rtp_in(channel, await self.reader.readexactly(length))
            return None
        method, url, _ = (first.decode(errors="replace") + await self._line()).split(" ", 2)
        headers = {}
        while header := await self._line():
            if len(headers) == MAX_HEADERS:
                raise ValueError("Too many headers")
            key, _, value = header.partition(":")
            headers[key.strip().lower()] = value.strip()
        if not 0 <= (length := int(headers.get("content-length", 0))) <= MAX_BODY:
            raise ValueError("Body too long")
        await self.reader.readexactly(length)
        return method, url, headers

    async def _request(self, method, url, headers) -> tuple[str, list[str], str]:
        if method == "OPTIONS":
            return "200 OK", ["Public: OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"], ""
        if method in ("GET_PARAMETER", "SET_PARAMETER"):
            return "200 OK", [], ""
        if method == "TEARDOWN":
            return "200 OK", [], ""
        if method == "DESCRIBE":
            return await self._describe(url, headers)
        if self.camera is None:
            return "455 Method Not Valid in This State", [], ""
        if method == "SETUP":
            return self._setup(url, headers)
        if method == "PLAY":
            if self.player is None:
                self.player = asyncio.create_task(self._play())
            return "200 OK", [], ""
        return "501 Not Implemented", [], ""

    async def _describe(self, url, headers):
        split = urlsplit(url)
        path = split.path.strip("/")
        camera = self.server.cameras.get(path)
        token = (parse_qs(split.query).get("auth") or [""])[0].encode()
        if camera is None or not hmac.compare_digest(token, camera.token.encode()):
            _LOGGER.debug("Refused RTSP client %s for %s", self.peer, path)
            return "401 Unauthorized", [], ""
        if self.frames is None:
            frames = camera.frames()
            try:
                async with asyncio.timeout(DESCRIBE_TIMEOUT):
                    self.first = await anext(frames)
            except (TimeoutError, StopAsyncIteration):
                # the cloud stream keeps running for STREAM_LINGER, for the retry
                _LOGGER.debug(
                    "No video of %s within %s s, the client has to retry",
                    camera.entity_id,
                    DESCRIBE_TIMEOUT,
                )
                return "503 Service Unavailable", [], ""
            self.frames = frames
        self.camera = camera
        backchannel = BACKCHANNEL in headers.get("require", "") and camera.supports_talk
        if backchannel:
            _LOGGER.debug("Offering the talk backchannel of %s to %s", camera.entity_id, self.peer)
        body = sdp(self.first.codec, camera.parameter_sets, backchannel)
        base = url.split("?")[0].rstrip("/") + "/"
        extra = ["Content-Type: application/sdp", f"Content-Base: {base}"]
        if backchannel:
            extra.append(f"Require: {BACKCHANNEL}")
        return "200 OK", extra, body

    def _setup(self, url, headers):
        track = urlsplit(url).path.rstrip("/").rpartition("trackID=")[2]
        transport = headers.get("transport", "")
        if track not in ("0", "1") or "interleaved=" not in transport:
            # UDP is not served, ffmpeg falls back to TCP
            return "461 Unsupported Transport", [], ""
        channel = int(transport.split("interleaved=")[1].split("-")[0].split(";")[0])
        if not 0 <= channel < 254:
            return "400 Bad Request", [], ""
        self.channels[channel] = int(track)
        if track == "0":
            self.video_channel = channel
        return "200 OK", [f"Transport: RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}"], ""

    def _rtp_in(self, channel: int, packet: bytes) -> None:
        if self.channels.get(channel) != 1 or len(packet) < 12 or not self.camera.supports_talk:
            return  # RTCP reports and the like
        header_len = 12 + 4 * (packet[0] & 0x0F)
        if packet[0] & 0x10 and len(packet) >= header_len + 4:  # header extension
            header_len += 4 + 4 * int.from_bytes(packet[header_len + 2 : header_len + 4])
        payload_type = packet[1] & 0x7F
        end = len(packet) - (packet[-1] if packet[0] & 0x20 else 0)  # padding
        if payload_type not in AUDIO_CODECS or end <= header_len:
            return
        self.counts["audio"] += 1
        self.camera.talk(AUDIO_CODECS[payload_type], packet[header_len:end])

    async def _play(self) -> None:
        keepalive = asyncio.create_task(self._keepalive())
        try:
            if self.first is not None:
                await self._send_frame(self.first)
            async for frame in self.frames:
                await self._send_frame(frame)
            _LOGGER.debug("Live stream of %s ended, closing RTSP client %s", self.camera.entity_id, self.peer)
        except ConnectionError:
            pass
        finally:
            keepalive.cancel()
            self.writer.close()

    async def _send_frame(self, frame) -> None:
        nals = nal_units(frame.data)
        self.rtp_ts = frame.pts & 0xFFFFFFFF
        out = []
        for n, nal in enumerate(nals):
            parts = payloads(frame.codec, nal)
            for i, part in enumerate(parts):
                marker = n == len(nals) - 1 and i == len(parts) - 1
                self.seq = (self.seq + 1) & 0xFFFF
                rtp = struct.pack(
                    ">BBHII", 0x80, (0x80 if marker else 0) | PT_VIDEO, self.seq, self.rtp_ts, self.ssrc
                ) + part
                out.append(struct.pack(">cBH", b"$", self.video_channel, len(rtp)) + rtp)
                self.counts["video"] += 1
        self.writer.write(b"".join(out))
        self.last_write = time.monotonic()
        await self.writer.drain()

    async def _keepalive(self) -> None:
        """RTCP sender reports while the cloud stream reconnects, so go2rtc keeps the source."""
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            if time.monotonic() - self.last_write < KEEPALIVE_INTERVAL:
                continue
            now = time.time() + 2208988800  # NTP epoch
            report = struct.pack(
                ">BBHIIIIII", 0x80, 200, 6, self.ssrc, int(now), int(now % 1 * (1 << 32)),
                self.rtp_ts, self.counts["video"], 0,
            )
            self.writer.write(struct.pack(">cBH", b"$", self.video_channel + 1, len(report)) + report)
            self.last_write = time.monotonic()

    async def close(self) -> None:
        if self.player:
            self.player.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.player
        if self.frames is not None:
            await self.frames.aclose()
        self.writer.close()
        if self.camera is not None:
            _LOGGER.debug(
                "RTSP client %s of %s disconnected: %d video packets sent, %d talk packets received",
                self.peer,
                self.camera.entity_id,
                self.counts["video"],
                self.counts["audio"],
            )
