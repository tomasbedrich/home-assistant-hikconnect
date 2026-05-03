import asyncio
import contextlib
import hashlib
import logging
import secrets
import struct
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

from aiohttp import web
from Crypto.Cipher import AES
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from .const import DOMAIN

try:
    from homeassistant.helpers.network import NoURLAvailableError, get_url
except ImportError:  # pragma: no cover - older HA fallback at runtime
    class NoURLAvailableError(RuntimeError):
        pass

    def get_url(*args, **kwargs):
        raise NoURLAvailableError

_LOGGER = logging.getLogger(__name__)

MAGIC = b"\x9e\xba\xac\xe9"
HDR_SIZE = 32
CTRL_PORT = 9010
STREAM_PORT = 9020
VIDEO_CHANNEL = 4
MEDIA_INIT_IV = b"01234567" + b"\x00" * 8
DEFAULT_MEDIA_AES_KEY = b"EDB54773F214422F"

# Captured and validated against the Hik-Connect Android client.
STEP1_CAPTURE = bytes.fromhex(
    "9ebaace901000000000000010000000000003003ffffffff0000007000000000"
    "b9f56732fa20dbef1be53f4f4892989ac68b59d96f9b9b1c2237a0369b22b16d"
    "5d77efdbf5c059e3e3ad950d84c2f9db644c46e7b729135f54125ea65e9b29bd"
    "1568bc4da10b5e2bd0d862364b9651361b4b1ea4a0caa884ea718f0fba717d7c"
    "dc1a30ca873193b7e53e3f6b713199a230393436613562623961393962666163"
    "32303066333432613038633763323034"
)
STEP3_CAPTURE = bytes.fromhex(
    "9ebaace901000000000000020000000000002011ffffffff000001d000000000"
    "b9f56732fa20dbef1be53f4f4892989ac68b59d96f9b9b1c2237a0369b22b16d"
    "5d77efdbf5c059e3e3ad950d84c2f9db644c46e7b729135f54125ea65e9b29bd"
    "1568bc4da10b5e2bd0d862364b9651367e6a8ae890aedb44d2ee37a3f39b7af6"
    "4c68496112aa61e18db8b22b5c859f9e54ba068a7abf2163cd72837592367bb8"
    "e4d71e3336279d72f874451012ebecab1da80d9233542dab73596b0b883daf25"
    "7d96a6d6ebc8468a0ec6832c1163c0d023a5c2ff0d71d15a39c7fb7d598950e5"
    "6bf09713c94b3c435691660bd9826485109d8c19fac7f2899f08de4ab2afd2fc"
    "124130e0a4359f53918ac7bfaeffe841edc362240fdafa1f32491e09ababae9b"
    "878e994e3cb725d43f209454a059a86a2c1f8c79d39107064c126bf5f44051ca"
    "16aa5553671a6d8eaf564dadc2ff23e8a500edd9dab8b6f0698cdfa39f86fea3"
    "aa016729d8aff912b25022c98e260fa449d0b6d66ffa640413d33ad175a43a46"
    "a21aba1a299df05874aced285d456f635dba6bfb1aa42d54b1cda1285462ea50"
    "9dcd04b72b2cd0afd6284b1b08661757f26b33d71bf1c8b087dc8579d7b8e2b3"
    "8f4fcd9ae9a1b5d54fe9595217e47dd250d2a5995be302667e5eec1e15e84ae0"
    "24b700e1034ef0c5d5114afbb9c7b92234336339343435323937373863623835"
    "62653964356162306531633631383437"
)
STEP4_CAPTURE = bytes.fromhex(
    "9ebaace901000000000000040000000000002013ffffffff0000008000000000"
    "b9f56732fa20dbef1be53f4f4892989ac68b59d96f9b9b1c2237a0369b22b16d"
    "5d77efdbf5c059e3e3ad950d84c2f9db644c46e7b729135f54125ea65e9b29bd"
    "1568bc4da10b5e2bd0d862364b9651360bca4de8076014f53916fa37f490eafa"
    "34cc3c0582e8f11593dfbab17b505aced0bf208efde2edd7d18eda8537b31f76"
    "3532653164383435356163626366356632383130613533376434613231313764"
)


def parse_hik_header(hdr: bytes) -> dict:
    if len(hdr) != HDR_SIZE:
        raise ValueError(f"Invalid Hik header size: {len(hdr)}")

    return {
        "magic": hdr[0:4],
        "flags": struct.unpack_from(">I", hdr, 4)[0],
        "seq": struct.unpack_from(">I", hdr, 8)[0],
        "opcode": struct.unpack_from(">I", hdr, 12)[0],
        "unk16": struct.unpack_from(">I", hdr, 16)[0],
        "unk20": struct.unpack_from(">I", hdr, 20)[0],
        "payload_len": struct.unpack_from(">I", hdr, 24)[0],
        "unk28": struct.unpack_from(">I", hdr, 28)[0],
    }


async def read_exact(reader: asyncio.StreamReader, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = await reader.read(size - len(data))
        if not chunk:
            raise EOFError(f"Connection closed after {len(data)}/{size} bytes")
        data += chunk
    return data


async def recv_hik_response(
    reader: asyncio.StreamReader,
) -> tuple[dict, bytes, bytes]:
    header = await read_exact(reader, HDR_SIZE)
    parsed = parse_hik_header(header)
    if parsed["magic"] != MAGIC:
        raise ValueError(f"Bad Hik magic: {parsed['magic'].hex()}")
    payload = await read_exact(reader, parsed["payload_len"]) if parsed["payload_len"] else b""
    token = await read_exact(reader, 32)
    return parsed, payload, token


def extract_xml_text(xml: str, tag: str) -> str:
    start_tag = f"<{tag}>"
    end_tag = f"</{tag}>"
    start = xml.find(start_tag)
    end = xml.find(end_tag)
    if start == -1 or end == -1:
        return ""
    return xml[start + len(start_tag) : end]


def build_media_init_packet(session_id: int, aes_key: bytes) -> bytes:
    xml = (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<Request>\n"
        f"\t<Session>{session_id}</Session>\n"
        "\t<Rate>1</Rate>\n"
        "\t<Mode>-1</Mode>\n"
        "</Request>\n"
    ).encode()
    if len(xml) > 128:
        raise ValueError(f"Media init XML too long: {len(xml)}")

    padded = xml + b"\t" * (128 - len(xml))
    encrypted = AES.new(aes_key, AES.MODE_CBC, MEDIA_INIT_IV).encrypt(padded)
    header = struct.pack(
        ">4sIIIIIII",
        MAGIC,
        0x01000000,
        3,
        0,
        0x00003105,
        0xFFFFFFFF,
        128,
        0,
    )
    return header + encrypted + hashlib.md5(encrypted).hexdigest().encode()


def parse_rtp(data: bytes) -> Optional[dict]:
    if len(data) < 12:
        return None
    b0, b1 = data[0], data[1]
    header_len = 12 + (b0 & 0x0F) * 4
    if (b0 & 0x10) != 0 and len(data) >= header_len + 4:
        ext_words = struct.unpack_from(">H", data, header_len + 2)[0]
        header_len += 4 + ext_words * 4
    return {
        "version": (b0 >> 6) & 0x03,
        "seq": struct.unpack_from(">H", data, 2)[0],
        "ssrc": struct.unpack_from(">I", data, 8)[0],
        "header_len": header_len,
    }


@dataclass
class FuaState:
    nal_header: int
    chunks: list[bytes] = field(default_factory=list)


def depacketize_h264(payload: bytes, seq: int, ssrc: int, state: dict[str, FuaState]):
    if not payload:
        return
    nal_type = payload[0] & 0x1F
    if 1 <= nal_type <= 23:
        yield payload
        return

    if nal_type == 24:
        offset = 1
        while offset + 2 <= len(payload):
            size = struct.unpack_from(">H", payload, offset)[0]
            offset += 2
            if size == 0 or offset + size > len(payload):
                break
            yield payload[offset : offset + size]
            offset += size
        return

    if nal_type != 28 or len(payload) < 2:
        return

    fu_indicator = payload[0]
    fu_header = payload[1]
    start = (fu_header & 0x80) != 0
    end = (fu_header & 0x40) != 0
    key = f"ssrc:{ssrc}"
    fragment = payload[2:]

    if start:
        state[key] = FuaState(
            nal_header=(fu_indicator & 0xE0) | (fu_header & 0x1F),
            chunks=[fragment],
        )
        return

    current = state.get(key)
    if current is None:
        return
    current.chunks.append(fragment)
    if end:
        nal = bytes([current.nal_header]) + b"".join(current.chunks)
        del state[key]
        yield nal


class HikLocalBridge:
    def __init__(self, bootstrap: dict, aes_key: bytes = DEFAULT_MEDIA_AES_KEY):
        self._bootstrap = bootstrap
        self._aes_key = aes_key[:16]

    @property
    def local_ip(self) -> str:
        return self._bootstrap.get("local_sdk", {}).get("local_ip")

    @property
    def cmd_port(self) -> int:
        return self._bootstrap.get("local_sdk", {}).get("local_cmd_port") or CTRL_PORT

    @property
    def stream_port(self) -> int:
        return self._bootstrap.get("local_sdk", {}).get("local_stream_port") or STREAM_PORT

    async def _send_step(self, packet: bytes, port: int) -> tuple[dict, bytes, bytes]:
        reader, writer = await asyncio.open_connection(self.local_ip, port)
        try:
            writer.write(packet)
            await writer.drain()
            return await recv_hik_response(reader)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def handshake(self) -> int:
        _, payload1, token1 = await self._send_step(STEP1_CAPTURE, self.cmd_port)
        _LOGGER.debug("Step1 token=%s payload=%s", token1.decode(errors="ignore"), payload1[:120])

        _, payload3, token3 = await self._send_step(STEP3_CAPTURE, self.cmd_port)
        xml3 = payload3.decode("utf-8", errors="replace")
        if extract_xml_text(xml3, "Result") not in ("", "0"):
            raise RuntimeError(f"9010 step3 rejected: {xml3}")
        session = extract_xml_text(xml3, "Session")
        if not session:
            raise RuntimeError(f"No session id in step3 response: {xml3}")
        _LOGGER.debug("Step3 token=%s session=%s", token3.decode(errors="ignore"), session)

        _, payload4, token4 = await self._send_step(STEP4_CAPTURE, self.cmd_port)
        xml4 = payload4.decode("utf-8", errors="replace")
        if extract_xml_text(xml4, "Result") not in ("", "0"):
            raise RuntimeError(f"9010 step4 rejected: {xml4}")
        _LOGGER.debug("Step4 token=%s", token4.decode(errors="ignore"))
        return int(session)

    async def stream_annex_b(self) -> AsyncIterator[bytes]:
        if not self.local_ip:
            raise RuntimeError("Camera has no local Hik LAN address")

        session_id = await self.handshake()
        init_packet = build_media_init_packet(session_id, self._aes_key)

        reader, writer = await asyncio.open_connection(self.local_ip, self.stream_port)
        try:
            writer.write(init_packet)
            writer.write(init_packet)
            await writer.drain()

            init_header = parse_hik_header(await read_exact(reader, HDR_SIZE))
            if init_header["payload_len"] == 0:
                try:
                    reject_token = await asyncio.wait_for(read_exact(reader, 32), timeout=1.0)
                    reject_info = reject_token.decode(errors="ignore")
                except Exception:
                    reject_info = ""
                raise RuntimeError(f"9020 init rejected for session {session_id}: {reject_info}")

            init_payload = await read_exact(reader, init_header["payload_len"])
            init_xml = init_payload.decode("utf-8", errors="replace")
            init_result = extract_xml_text(init_xml, "Result")
            if init_result and init_result != "0":
                raise RuntimeError(f"9020 init rejected: {init_xml}")
            await read_exact(reader, 32)
            await read_exact(reader, 256)

            fua_state: dict[str, FuaState] = {}
            last_sps: Optional[bytes] = None
            last_pps: Optional[bytes] = None
            idr_seen = False

            while True:
                frame_header = await read_exact(reader, 4)
                if frame_header[0] != 0x24:
                    continue

                channel = frame_header[1]
                frame_len = struct.unpack_from(">H", frame_header, 2)[0]
                frame_payload = await read_exact(reader, frame_len)
                if channel != VIDEO_CHANNEL:
                    continue

                outer_rtp = parse_rtp(frame_payload)
                if outer_rtp is None or outer_rtp["version"] != 2:
                    continue

                inner_offset = 0
                for offset in range(0, 4):
                    if frame_len - 12 - offset < 12:
                        continue
                    if (frame_payload[12 + offset] >> 6) == 2:
                        inner_offset = offset
                        break

                inner_payload = frame_payload[12 + inner_offset :]
                inner_rtp = parse_rtp(inner_payload)
                if inner_rtp is None or inner_rtp["version"] != 2:
                    continue

                nal_payload = inner_payload[inner_rtp["header_len"] :]
                for nal in depacketize_h264(
                    nal_payload,
                    inner_rtp["seq"],
                    inner_rtp["ssrc"],
                    fua_state,
                ):
                    nal_type = nal[0] & 0x1F
                    if nal_type == 7:
                        last_sps = nal
                    elif nal_type == 8:
                        last_pps = nal
                    elif nal_type == 5:
                        idr_seen = True
                        if last_sps:
                            yield b"\x00\x00\x00\x01" + last_sps
                        if last_pps:
                            yield b"\x00\x00\x00\x01" + last_pps
                    if idr_seen:
                        yield b"\x00\x00\x00\x01" + nal
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass


def has_local_bridge_support(bootstrap: dict) -> bool:
    local_sdk = bootstrap.get("local_sdk", {})
    return bool(
        local_sdk.get("local_ip")
        and local_sdk.get("local_cmd_port")
        and local_sdk.get("local_stream_port")
    )


def get_or_create_stream_token(hass: HomeAssistant, camera_id: str) -> str:
    tokens = hass.data[DOMAIN].setdefault("stream_bridge_tokens", {})
    token = tokens.get(camera_id)
    if token is None:
        token = secrets.token_hex(16)
        tokens[camera_id] = token
        hass.data[DOMAIN].setdefault("stream_bridge_lookup", {})[token] = camera_id
    return token


def build_internal_stream_url(hass: HomeAssistant, camera_id: str) -> str:
    token = get_or_create_stream_token(hass, camera_id)
    path = f"/api/{DOMAIN}/stream/{token}"
    try:
        base_url = get_url(hass, prefer_external=False)
    except NoURLAvailableError:
        internal_url = getattr(hass.config, "internal_url", None)
        if internal_url:
            base_url = internal_url
        else:
            base_url = "http://127.0.0.1:8123"
    return f"{base_url.rstrip('/')}{path}"


def resolve_camera_bootstrap(hass: HomeAssistant, camera_id: str) -> Optional[dict]:
    coordinator = hass.data[DOMAIN]["coordinator"]
    for device_info in coordinator.data:
        for camera_info in device_info["cameras"]:
            if camera_info["id"] != camera_id:
                continue
            return camera_info.get("stream_bootstrap")
    return None


class HikConnectLocalStreamView(HomeAssistantView):
    url = f"/api/{DOMAIN}/stream/{{token}}"
    name = f"api:{DOMAIN}:stream"
    requires_auth = False

    async def get(self, request: web.Request, token: str) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        camera_id = hass.data[DOMAIN].get("stream_bridge_lookup", {}).get(token)
        if camera_id is None:
            raise web.HTTPNotFound

        bootstrap = resolve_camera_bootstrap(hass, camera_id)
        if not bootstrap or not has_local_bridge_support(bootstrap):
            raise web.HTTPNotFound

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "video/h264",
                "Cache-Control": "no-store",
                "Pragma": "no-cache",
            },
        )
        await response.prepare(request)

        bridge = HikLocalBridge(bootstrap)
        try:
            async for chunk in bridge.stream_annex_b():
                await response.write(chunk)
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        except Exception as err:
            _LOGGER.warning("Local Hik stream failed for camera %s: %s", camera_id, err)
        finally:
            with contextlib.suppress(RuntimeError, ConnectionResetError):
                await response.write_eof()

        return response