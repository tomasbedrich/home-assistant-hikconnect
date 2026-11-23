"""G.711 µ-law <-> A-law conversion (audioop is gone from Python 3.13)."""


def _ulaw(code: int) -> int:
    code = ~code & 0xFF
    value = (((code & 0x0F) << 3) + 0x84) << ((code & 0x70) >> 4)
    return 0x84 - value if code & 0x80 else value - 0x84


def _alaw(code: int) -> int:
    code ^= 0x55
    value, segment = (code & 0x0F) << 4, (code & 0x70) >> 4
    value = value + 8 if segment == 0 else (value + 0x108) << max(segment - 1, 0)
    return value if code & 0x80 else -value


ULAW = [_ulaw(i) for i in range(256)]
ALAW = [_alaw(i) for i in range(256)]
_TABLES = {
    ("PCMU", "PCMA"): bytes(min(range(256), key=lambda a: abs(ALAW[a] - v)) for v in ULAW),
    ("PCMA", "PCMU"): bytes(min(range(256), key=lambda u: abs(ULAW[u] - v)) for v in ALAW),
}


def converter(source: str, target: str):
    """Function converting `source` codec bytes ("PCMU" / "PCMA") to `target`."""
    if source == target:
        return bytes
    return lambda data: data.translate(_TABLES[source, target])
