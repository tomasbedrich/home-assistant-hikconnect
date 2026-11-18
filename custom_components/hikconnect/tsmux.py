"""Minimal MPEG-TS muxer for one H.264/H.265 video stream.

HA's stream integration (PyAV) needs timestamps, which raw Annex-B over HTTP lacks.
Wrapping each access unit in a PES packet with its 90 kHz PTS makes it work.
"""

import struct

PMT_PID, VIDEO_PID = 0x1000, 0x100


def _crc32_mpeg(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b << 24
        for _ in range(8):
            crc = (crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1
    return crc & 0xFFFFFFFF


def _psi(table_id: int, ext_id: int, body: bytes) -> bytes:
    sec = (
        bytes([table_id])
        + struct.pack(">HH", 0xB000 | (len(body) + 9), ext_id)
        + b"\xc1\x00\x00"
        + body
    )
    return b"\x00" + sec + struct.pack(">I", _crc32_mpeg(sec))


class TsMuxer:
    def __init__(self):
        self.cc = {}

    def _packets(self, pid: int, payload: bytes, pcr: int | None = None) -> bytes:
        out, first = bytearray(), True
        while first or payload:
            af = None
            if first and pcr is not None:
                base = pcr & 0x1FFFFFFFF
                af = b"\x10" + struct.pack(">IH", base >> 1, ((base & 1) << 15) | 0x7E00)
            if len(payload) < 184 - (len(af) + 1 if af is not None else 0):  # stuff the last packet
                if af is None:
                    n = 183 - len(payload)
                    af = b"\x00" + b"\xff" * (n - 1) if n else b""
                else:
                    af += b"\xff" * (183 - len(af) - len(payload))
            size = 184 - (len(af) + 1 if af is not None else 0)
            chunk, payload = payload[:size], payload[size:]
            cc = self.cc.get(pid, 0)
            self.cc[pid] = (cc + 1) & 0xF
            out += struct.pack(
                ">BHB", 0x47, (0x4000 if first else 0) | pid, (0x30 if af is not None else 0x10) | cc
            )
            out += (bytes([len(af)]) + af if af is not None else b"") + chunk
            first = False
        return bytes(out)

    def mux(self, frame) -> bytes:
        """Return TS packets for a `hikconnect.stream.Frame`, with PAT/PMT before each keyframe."""
        res = b""
        if frame.keyframe:
            stream_type = 0x24 if frame.codec == "h265" else 0x1B
            res += self._packets(0, _psi(0x00, 1, struct.pack(">HH", 1, 0xE000 | PMT_PID)))
            res += self._packets(
                PMT_PID,
                _psi(
                    0x02,
                    1,
                    struct.pack(">HH", 0xE000 | VIDEO_PID, 0xF000)
                    + bytes([stream_type])
                    + struct.pack(">HH", 0xE000 | VIDEO_PID, 0xF000),
                ),
            )
        p = frame.pts & 0x1FFFFFFFF
        pts = bytes([
            0x21 | ((p >> 29) & 0x0E),
            (p >> 22) & 0xFF,
            0x01 | ((p >> 14) & 0xFE),
            (p >> 7) & 0xFF,
            0x01 | ((p << 1) & 0xFE),
        ])
        pes = b"\x00\x00\x01\xe0\x00\x00\x80\x80\x05" + pts + frame.data
        return res + self._packets(VIDEO_PID, pes, pcr=frame.pts if frame.keyframe else None)
