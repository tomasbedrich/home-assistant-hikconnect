#!/usr/bin/env python3
"""
Hik-Connect cloud snapshot / live video probe.

Collects anonymized debug information needed to add snapshot and live video support to
https://github.com/tomasbedrich/home-assistant-hikconnect (see issue #6).

Usage:  python3 hikconnect_probe.py
Needs Python 3.9+, no extra packages. Optional: `pip install cryptography` enables one more test.
Set HC_SKIP_PLAIN=1 to skip the unencrypted live stream attempt (needs `cryptography`).

The script logs into Hik-Connect with your account (the same way the Home Assistant integration
does), asks your devices for a snapshot and tries to open a live video stream for a few seconds.
It prints an anonymized report (no account name, password, serial numbers, tokens or IP addresses)
which you can paste into the GitHub issue. The snapshot and video are saved next to the script
for you to check locally - please do NOT upload them.
"""
import base64
import getpass
import hashlib
import json
import os
import re
import socket
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.hik-connect.com"
HEADERS = {"clientType": "55", "lang": "en-US", "featureCode": "deadbeef"}
CLIENT_VERSION = "v3.10.1.20260710"
STREAM_SECONDS = 10
MAX_CHANNELS = 3
JPEG_MAGIC = b"\xff\xd8"

report = []
secrets = []  # values scrubbed from the report as a last line of defence


def out(line=""):
    for s in secrets:
        if s and len(s) > 3:
            line = line.replace(s, "<redacted>")
    report.append(line)
    print(line)


# --------------------------------------------------------------------------- HTTP


def http(method, url, data=None, params=None, headers=None, raw=False):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as res:
        content = res.read()
        return content if raw else json.loads(content)


def login(username, password):
    global API
    data = {"account": username, "password": hashlib.md5(password.encode()).hexdigest()}
    for _ in range(3):
        res = http("POST", f"{API}/v3/users/login/v2", data, headers=HEADERS)
        code = res.get("meta", {}).get("code")
        if code == 1100:
            API = "https://" + res["loginArea"]["apiDomain"]
            continue
        if code in (1013, 1014):
            sys.exit("Login failed: wrong username or password.")
        if code == 1015:
            sys.exit("Login failed: CAPTCHA required. Log in with the Hik-Connect app, then retry.")
        session = res["loginSession"]["sessionId"]
        domain = (res.get("loginArea") or {}).get("apiDomain")
        if domain:
            API = "https://" + domain
        return session
    sys.exit(f"Login failed: {res.get('meta')}")


# --------------------------------------------------------------------------- AES (pure Python)
# Standard AES; Hikvision video uses it with the number of rounds reduced to 3.


def _xt(a):
    return ((a << 1) ^ (0x1B if a & 0x80 else 0)) & 0xFF


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a, b = _xt(a), b >> 1
    return r


def _make_sbox():
    s = [0] * 256
    for x in range(256):
        inv = next((y for y in range(1, 256) if _mul(x, y) == 1), 0)
        v = inv
        for i in range(1, 5):
            v ^= ((inv << i) | (inv >> (8 - i))) & 0xFF
        s[x] = v ^ 0x63
    return s


SBOX = _make_sbox()
INV_SBOX = [SBOX.index(i) for i in range(256)]
RCON = [0x00, 0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36]
MUL = {m: [_mul(x, m) for x in range(256)] for m in (9, 11, 13, 14)}


def expand_key(key, rounds):
    nk = len(key) // 4
    w = [list(key[4 * i : 4 * i + 4]) for i in range(nk)]
    for i in range(nk, 4 * (rounds + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [SBOX[t[1]] ^ RCON[i // nk], SBOX[t[2]], SBOX[t[3]], SBOX[t[0]]]
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([a ^ b for a, b in zip(w[i - nk], t)])
    return [sum(w[4 * r : 4 * r + 4], []) for r in range(rounds + 1)]


def decrypt_block(block, rks):
    s = [a ^ b for a, b in zip(block, rks[-1])]
    for r in range(len(rks) - 2, -1, -1):
        s = [INV_SBOX[s[4 * ((c - r_) % 4) + r_]] for c in range(4) for r_ in range(4)]
        s = [a ^ b for a, b in zip(s, rks[r])]
        if r:
            m = []
            for c in range(4):
                a0, a1, a2, a3 = s[4 * c : 4 * c + 4]
                m += [
                    MUL[14][a0] ^ MUL[11][a1] ^ MUL[13][a2] ^ MUL[9][a3],
                    MUL[9][a0] ^ MUL[14][a1] ^ MUL[11][a2] ^ MUL[13][a3],
                    MUL[13][a0] ^ MUL[9][a1] ^ MUL[14][a2] ^ MUL[11][a3],
                    MUL[11][a0] ^ MUL[13][a1] ^ MUL[9][a2] ^ MUL[14][a3],
                ]
            s = m
    return bytes(s)


def aes_cbc_decrypt(key, iv, data):
    rks, prev, res = expand_key(key, 10), iv, bytearray()
    for i in range(0, len(data) - len(data) % 16, 16):
        blk = data[i : i + 16]
        res += bytes(a ^ b for a, b in zip(decrypt_block(blk, rks), prev))
        prev = blk
    return bytes(res[: -res[-1]]) if res and 1 <= res[-1] <= 16 else bytes(res)


def _aes_selftest():
    # FIPS-197 C.1: AES-128 decrypt
    rks = expand_key(bytes(range(16)), 10)
    ct = bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")
    assert decrypt_block(ct, rks).hex() == "00112233445566778899aabbccddeeff"


def code_key(code):
    return code.encode()[:16].ljust(16, b"\0")


# --------------------------------------------------------------------------- stream protocol


def varint(n):
    b = bytearray()
    while True:
        b.append((n & 0x7F) | (0x80 if n > 0x7F else 0))
        n >>= 7
        if not n:
            return bytes(b)


def pb_str(f, s):
    s = s.encode() if isinstance(s, str) else s
    return varint(f << 3 | 2) + varint(len(s)) + s


def pb_int(f, n):
    return varint(f << 3) + varint(n)


def _read_varint(d, i):
    v = shift = 0
    while True:
        b = d[i]
        i += 1
        v |= (b & 0x7F) << shift
        shift += 7
        if not b & 0x80:
            return v, i


def pb_decode(d):
    i, res = 0, {}
    while i < len(d):
        key, i = _read_varint(d, i)
        f, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(d, i)
        elif wt == 2:
            n, i = _read_varint(d, i)
            v, i = d[i : i + n], i + n
        else:
            raise ValueError(f"protobuf wire type {wt}")
        res.setdefault(f, []).append(v)
    return res


def stream_info_req(url, vtm_key=None):
    body = pb_str(1, url)
    if vtm_key:
        body += pb_str(2, vtm_key)
    return body + pb_str(3, CLIENT_VERSION) + pb_int(4, 0) + pb_str(6, CLIENT_VERSION)


def frame(body, channel=0x00, code=0x13B):
    return struct.pack(">BBHHH", 0x24, channel, len(body), 0, code) + body


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("connection closed by server")
        buf += chunk
    return buf


def recv_frame(sock):
    hdr = recv_exact(sock, 8)
    magic, channel, length, _seq, code = struct.unpack(">BBHHH", hdr)
    if magic != 0x24:
        raise ValueError(f"unexpected frame header {hdr.hex()}")
    return channel, code, recv_exact(sock, length)


def ysproto_host(url):
    host = url.split("://", 1)[1].split("/", 1)[0]
    h, p = host.rsplit(":", 1)
    return h.strip("[]"), int(p)


def param_names(url):
    return [p.split("=")[0] for p in url.split("?", 1)[-1].split("&")]


def describe_rsp(rsp, code_md5=None):
    """Field meaning from the open-source LE-EZVIZ-VS project; values are not printed."""
    names = {1: "result", 2: "datakey", 3: "streamhead", 4: "streamssn", 5: "vtmstreamkey",
             6: "serverinfo", 7: "streamurl", 8: "srvinfo", 9: "aesmd5", 10: "udptransinfo",
             11: "peerpbkey", 12: "pdslist"}
    parts = []
    for f in sorted(rsp):
        v = rsp[f][0]
        name = names.get(f, f"#{f}")
        if f == 1:
            parts.append(f"{name}={v}")
        elif f == 2:
            parts.append(f"{name}={'set' if v else 0}")
        elif f == 3:
            parts.append(f"{name}({len(v)}B, starts {v[:12].hex(' ')})")
        elif f == 9 and code_md5:
            parts.append(f"{name}(matches md5(md5(code))={v.decode(errors='replace') in code_md5})")
        else:
            parts.append(f"{name}({len(v) if isinstance(v, bytes) else v}B)")
    return ", ".join(parts)


# --------------------------------------------------------------------------- ECDH (optional)


def ecdh_session(peer_pub_der, key_ver):
    """Client side of the EZVIZ ECDH envelope (reverse-engineered, see issue #6)."""
    import zlib

    from cryptography.hazmat.primitives import hashes, hmac, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    priv = ec.generate_private_key(ec.SECP256R1())
    self_pub = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    master = priv.exchange(ec.ECDH(), serialization.load_der_public_key(peer_pub_der))
    st = {"session": os.urandom(32), "seq": 1, "handshaked": False}

    def chacha(key, seq, data):
        nonce = b"\0\0\0\0" + struct.pack("<I", seq) + b"\0" * 8
        return Cipher(algorithms.ChaCha20(key, nonce), None).encryptor().update(data)

    def mac(key, head, ct):
        msg = f"{zlib.crc32(head)}{zlib.crc32(ct)}".encode()[:8].ljust(8, b"\0")
        h = hmac.HMAC(key, hashes.SHA256())
        h.update(msg)
        return h.finalize()

    def enc(data):
        if not st["handshaked"]:
            ecb = Cipher(algorithms.AES(master), modes.ECB()).encryptor()
            head = (b"\x24\x01\x00" + struct.pack(">H", len(data)) + bytes([1, key_ver])
                    + struct.pack(">I", 1) + ecb.update(st["session"]) + self_pub)
            ct = chacha(st["session"], 1, data)
            return head + ct + mac(master, head, ct)
        st["seq"] += 1
        head = b"\x24\x02\x00" + struct.pack(">H", len(data)) + b"\0\0" + struct.pack(">I", st["seq"])
        ct = chacha(st["session"], st["seq"], data)
        return head + ct + mac(st["session"], head, ct)

    def dec(pkg, st=st):
        """st: the message channel state by default; the stream channel has its own one."""
        typ, ext = pkg[1], pkg[2]
        (length,) = struct.unpack(">H", pkg[ext + 3 : ext + 5])
        (seq,) = struct.unpack(">I", pkg[ext + 7 : ext + 11])
        if typ == 1:
            hl = ext + 134
            peer = serialization.load_der_public_key(pkg[ext + 43 : ext + 134])
            m = priv.exchange(ec.ECDH(), peer)
            ecb = Cipher(algorithms.AES(m), modes.ECB()).decryptor()
            st["session"] = ecb.update(pkg[ext + 11 : ext + 43])
            mac_ok = mac(m, pkg[:hl], pkg[hl : hl + length]) == pkg[hl + length : hl + length + 32]
        else:
            hl = ext + 11
            mac_ok = mac(st["session"], pkg[:hl], pkg[hl : hl + length]) == pkg[hl + length : hl + length + 32]
        st["handshaked"] = True
        return typ, mac_ok, chacha(st["session"], seq, pkg[hl : hl + length])

    return enc, dec


# --------------------------------------------------------------------------- video analysis


def demux_ps(data):
    """Return (packs, stream types, encrypt descriptors, video elementary stream)."""
    packs, types, descs, es = 0, set(), set(), bytearray()
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 6 > len(data):
            break
        sid = data[i + 3]
        if sid == 0xBA:
            packs += 1
            i += 14
            continue
        length = struct.unpack(">H", data[i + 4 : i + 6])[0]
        body = data[i + 6 : i + 6 + length]
        if sid == 0xBC and len(body) > 4:
            info_len = struct.unpack(">H", body[2:4])[0]
            for m in re.finditer(rb"\x80\x06\x00\x01", body):
                descs.add(body[m.start() : m.start() + 8].hex(" "))
            p = 4 + info_len + 2
            while p + 4 <= len(body) - 4:
                types.add(body[p])
                p += 4 + struct.unpack(">H", body[p + 2 : p + 4])[0]
        elif 0xE0 <= sid <= 0xEF and len(body) > 3:
            es += body[3 + body[2] :]
        if sid >= 0xBC:
            i += 6 + length
        else:
            i += 3
    return packs, types, descs, bytes(es)


def nal_units(es):
    starts = [m.start() for m in re.finditer(b"\x00\x00\x01", es)]
    for n, s in enumerate(starts):
        sc = 4 if s and es[s - 1] == 0 else 3
        end = starts[n + 1] if n + 1 < len(starts) else len(es)
        if n + 1 < len(starts) and es[end - 1] == 0:
            end -= 1
        yield s - sc + 3, sc, es[s + 3 : end]


def nal_stats(es, hevc):
    total = valid = 0
    types = {}
    for _pos, _sc, nal in nal_units(es):
        if not nal:
            continue
        total += 1
        h = nal[0]
        t = (h >> 1) & 0x3F if hevc else h & 0x1F
        ok = not h & 0x80 and (t <= 40 if hevc else 1 <= t <= 23)
        valid += ok
        types[t] = types.get(t, 0) + 1
    top = ", ".join(f"{t}:{c}" for t, c in sorted(types.items(), key=lambda x: -x[1])[:8])
    return total, valid, top


def decrypt_es(es, hevc, key, enc_type):
    """Hikvision PlayCtrl NAL decryption (types 1-3), reverse-engineered."""
    rounds = {1: 3, 2: 10, 3: 14}[enc_type]
    rks = expand_key(key.ljust(32, b"\0")[: 32 if enc_type == 3 else 16], rounds)
    res = bytearray()
    for pos, sc, nal in nal_units(es):
        nal = bytearray(nal)
        off = 2 if hevc else 0
        n = 16 if enc_type == 1 else min(len(nal) - off, 4096) // 16 * 16
        if off + 16 <= len(nal):
            for p in range(off, off + n, 16):
                nal[p : p + 16] = decrypt_block(bytes(nal[p : p + 16]), rks)
        res += b"\x00" * (sc - 3) + b"\x00\x00\x01" + nal
    return bytes(res)


def rtp_payloads(packets):
    """Return (payload types, payloads) of RTP packets."""
    pts, res = set(), []
    for pkt in packets:
        if len(pkt) < 12 or pkt[0] >> 6 != 2:
            continue
        hl = 12 + 4 * (pkt[0] & 0x0F)
        if pkt[0] & 0x10 and len(pkt) >= hl + 4:
            hl += 4 + 4 * struct.unpack(">H", pkt[hl + 2 : hl + 4])[0]
        end = len(pkt) - (pkt[-1] if pkt[0] & 0x20 else 0)
        if end > hl:
            pts.add(pkt[1] & 0x7F)
            res.append(pkt[hl:end])
    return pts, res


def depay(payloads, hevc):
    """RFC 6184 (H.264) / RFC 7798 (H.265) depayloading to an Annex-B stream."""
    es, sc = bytearray(), b"\x00\x00\x00\x01"
    hl = 2 if hevc else 1
    for pl in payloads:
        if len(pl) < hl + 1:
            continue
        t = (pl[0] >> 1) & 0x3F if hevc else pl[0] & 0x1F
        if t == (48 if hevc else 24):  # aggregation packet
            off = hl
            while off + 2 <= len(pl):
                (n,) = struct.unpack(">H", pl[off : off + 2])
                es += sc + pl[off + 2 : off + 2 + n]
                off += 2 + n
        elif t == (49 if hevc else 28):  # fragmentation unit
            fu = pl[hl]
            if fu & 0x80:
                hdr = bytes([(pl[0] & 0x81) | (fu & 0x3F) << 1, pl[1]]) if hevc else bytes([(pl[0] & 0xE0) | (fu & 0x1F)])
                es += sc + hdr
            es += pl[hl + 1 :]
        else:
            es += sc + pl
    return bytes(es)


def analyze_stream(packets, tag, code):
    if not packets:
        return
    data = b"".join(packets)
    pts, payloads = rtp_payloads(packets)
    hevc = None
    if payloads and len(payloads) > 0.9 * len(packets):
        if payloads[0][:4] == b"\x00\x00\x01\xba":
            container, data = "RTP + MPEG-PS", b"".join(payloads)
        else:
            sample = payloads[:200]
            hevc = sum(p[1:2] == b"\x01" and not p[0] & 0x81 for p in sample) > 0.9 * len(sample)
            container = f"RTP ({'H.265' if hevc else 'H.264'} payload)"
    elif data[:4] == b"\x00\x00\x01\xba":
        container = "MPEG-PS"
    else:
        container = "unknown"
    out(f"  stream container: {container}, RTP payload types: {sorted(pts)}, first bytes: {packets[0][:8].hex(' ')}")
    if hevc is not None:
        es = depay(payloads, hevc)
    elif "MPEG-PS" in container:
        packs, types, descs, es = demux_ps(data)
        hevc = 0x24 in types
        codec = {0x1B: "H.264", 0x24: "H.265"}
        out(f"  PS packs: {packs}, stream types: {[hex(t) + '=' + codec.get(t, '?') for t in sorted(types)]}")
        out(f"  encryption descriptors (tag 0x80): {sorted(descs) or 'none'}")
    else:
        return
    total, valid, top = nal_stats(es, hevc)
    out(f"  video NAL units: {total}, valid headers: {valid} ({100 * valid // max(total, 1)}%), types: {top}")
    ext = "h265" if hevc else "h264"
    with open(f"hikconnect_probe_{tag}.{ext}", "wb") as f:
        f.write(es)
    if not code or valid > 0.9 * total:
        return
    for enc_type in (1, 2, 3):
        dec = decrypt_es(es[:2_000_000], hevc, code_key(code), enc_type)
        t2, v2, top2 = nal_stats(dec, hevc)
        out(f"  decrypt with code, type {enc_type}: valid headers {v2}/{t2} ({100 * v2 // max(t2, 1)}%), types: {top2}")
        if v2 > valid and v2 > 0.9 * t2:
            with open(f"hikconnect_probe_{tag}_decrypted.{ext}", "wb") as f:
                f.write(dec)
            out(f"  -> saved hikconnect_probe_{tag}_decrypted.{ext}")
            break


# --------------------------------------------------------------------------- live test


def vtm_vtdu(vtm, url, tag, code, ecdh=None):
    host, port = vtm
    code_md5 = None
    if code:
        md5 = hashlib.md5(code.encode()).hexdigest()
        code_md5 = {md5, hashlib.md5(md5.encode()).hexdigest()}
    crypto = {}
    if ecdh:
        url += "&ecdh=1"

    def connect(addr):
        # every connection has its own ECDH session
        if ecdh:
            crypto["enc"], crypto["dec"] = ecdh_session(*ecdh)
        return socket.create_connection(addr, timeout=10)

    def send(sock, body, code_=0x13B):
        if crypto:
            sock.sendall(frame(crypto["enc"](body), 0x0A, code_))
        else:
            sock.sendall(frame(body, 0x00, code_))

    def receive(sock):
        channel, code_, body = recv_frame(sock)
        if channel == 0x0A and crypto:
            typ, mac_ok, body = crypto["dec"](body)
            out(f"  [{tag}] encrypted reply: package type {typ}, mac ok: {mac_ok}")
        return channel, code_, body

    with connect((host, port)) as s:
        send(s, stream_info_req(url))
        channel, code_, body = receive(s)
    try:
        rsp = pb_decode(body)
    except Exception as exc:  # pylint: disable=broad-except
        out(f"  [{tag}] VTM reply channel={channel:#x} code={code_:#x}, cannot decode ({exc}), {len(body)} B")
        return False
    out(f"  [{tag}] VTM reply channel={channel:#x} code={code_:#x}: {describe_rsp(rsp, code_md5)}")
    if rsp.get(1, [0])[0] not in (0, 5302) or 7 not in rsp:
        return False

    vtdu_url = rsp[7][0].decode()
    out(f"  [{tag}] VTDU port {ysproto_host(vtdu_url)[1]}, url params: {param_names(vtdu_url)}")
    with connect(ysproto_host(vtdu_url)) as s:
        send(s, stream_info_req(url, rsp.get(5, [b""])[0]))
        channel, code_, body = receive(s)
        rsp2 = pb_decode(body)
        out(f"  [{tag}] VTDU reply channel={channel:#x} code={code_:#x}: {describe_rsp(rsp2, code_md5)}")
        if rsp2.get(1, [0])[0] != 0:
            return False
        keepalive = pb_str(1, rsp2.get(4, [b""])[0])
        send(s, keepalive, 0x132)
        stats, packets, start, last_ka = {}, [], time.time(), time.time()
        stream_state = {"session": b"\0" * 32}
        s.settimeout(5)
        try:
            while time.time() - start < STREAM_SECONDS:
                channel, code_, body = recv_frame(s)
                key = f"ch{channel:#x}/code{code_:#x}"
                stats[key] = stats.get(key, 0) + 1
                if channel in (0x0B, 0x0C) and crypto:
                    head, pkg = (b"", body) if channel == 0x0B else (body[:12], body[12:])
                    try:
                        typ, mac_ok, body = crypto["dec"](pkg, stream_state)
                        key = f"ecdh type{typ} mac_ok={mac_ok}"
                    except Exception as exc:  # pylint: disable=broad-except
                        body, key = b"", f"ecdh error {type(exc).__name__}"
                    stats[key] = stats.get(key, 0) + 1
                    if head and body:
                        body = bytes([head[0] & 0xDF]) + head[1:] + body
                if channel in (0x01, 0x0B, 0x0C) and body:
                    packets.append(body)
                if time.time() - last_ka > 3:
                    send(s, keepalive, 0x132)
                    last_ka = time.time()
        except (socket.timeout, ConnectionError, ValueError) as exc:
            out(f"  [{tag}] stream ended early: {exc!r}")
    out(f"  [{tag}] received {sum(map(len, packets))} B in {time.time() - start:.1f} s, frames: {stats}")
    analyze_stream(packets, tag, code)
    return bool(packets)


def main():
    _aes_selftest()
    print(__doc__)
    username = os.environ.get("HC_USERNAME") or input("Hik-Connect username: ").strip()
    password = os.environ.get("HC_PASSWORD") or getpass.getpass("Hik-Connect password (hidden): ")
    code = os.environ.get("HC_CODE", "").strip() or getpass.getpass(
        "Device verification code, only if 'Image and Video Encryption' is enabled (hidden, Enter to skip): "
    ).strip()
    secrets.extend([username, password, code])

    session = login(username, password)
    secrets.append(session)
    headers = {**HEADERS, "sessionId": session}
    out("=== hikconnect_probe report ===")
    out(f"python {sys.version.split()[0]}, api domain {urllib.parse.urlparse(API).hostname}, code given: {bool(code)}")

    pl = http(
        "GET", f"{API}/v3/userdevices/v1/devices/pagelist",
        params={"groupId": -1, "limit": 50, "offset": 0, "filter": "VTM,TTS,STATUS,CONNECTION"},
        headers=headers,
    )
    devices = pl.get("deviceInfos") or []
    vtms = pl.get("VTM") or {}
    ttss = pl.get("TTS") or pl.get("ttsInfos") or {}
    statuses = pl.get("statusInfos") or pl.get("STATUS") or {}
    out(f"pagelist: {len(devices)} devices, meta: {pl.get('meta', {}).get('code')}, "
        f"keys: {sorted(k for k in pl if k not in ('meta', 'page'))}")
    out(f"VTM map keys look like: {sorted({'serial' if k in [d['deviceSerial'] for d in devices] else 'other' for k in vtms})}")

    anon = {}
    for d in devices:
        sn = d["deviceSerial"]
        anon[sn] = f"dev{len(anon) + 1}"
        secrets.extend([sn, d.get("fullSerial", ""), d.get("name", "")])
        st = statuses.get(sn, {})
        tts = ttss.get(sn) or {}
        pk = tts.get("publicKey") or {}
        vtm = vtms.get(sn) or {}
        out(f"{anon[sn]}: model={d.get('deviceType')} fw={d.get('version')} category={d.get('deviceCategory')} "
            f"isEncrypt={st.get('isEncrypt')} vtm={'yes' if vtm else 'no'} vtm_fields={sorted(vtm)} "
            f"tts_publicKey={'v%s/%dB' % (pk.get('version'), len(pk.get('key', ''))) if pk else 'no'}")

    cams = [c for c in pl.get("cameraInfos") or [] if c.get("deviceSerial") in anon]
    for sn in anon:
        if any(c["deviceSerial"] == sn for c in cams):
            continue
        try:
            ci = http("GET", f"{API}/v3/userdevices/v1/cameras/info", headers=headers,
                      params={"deviceSerial": sn, "channelNo": 1}).get("cameraInfos") or []
            out(f"cameras/info fallback for {anon[sn]}: {len(ci)} channels")
            cams += ci
        except Exception as exc:  # pylint: disable=broad-except
            out(f"cameras/info fallback for {anon[sn]} failed: {exc!r}")
    for c in cams:
        secrets.append(c.get("cameraName", ""))
    out(f"cameraInfos: {len(cams)}, fields: {sorted(cams[0]) if cams else []}")
    for c in cams:
        biz = c.get("streamBizUrl")
        if c.get("isShow") or len(cams) <= 8:
            out(f"  {anon[c['deviceSerial']]} ch{c.get('channelNo')}: shown={c.get('isShow')} "
                f"signal={(c.get('deviceChannelInfo') or {}).get('signalStatus')} "
                f"streamBizUrl params={param_names(biz) if biz else None}")

    try:
        tk = http("POST", f"{API}/api/user/token/get", {"featureCode": "deadbeef", "count": 6}, headers=headers)
        tokens = tk.get("tokenArray") or []
        secrets.extend(tokens)
        out(f"stream tokens: {len(tokens)}, resultCode={tk.get('resultCode')}")
    except Exception as exc:  # pylint: disable=broad-except
        tokens = []
        out(f"stream tokens: failed {exc!r}")

    shown = [c for c in cams if c.get("isShow")] or cams
    active = [c for c in shown if (c.get("deviceChannelInfo") or {}).get("signalStatus") == 1]
    targets = (active or shown)[:MAX_CHANNELS]
    for c in targets:
        sn, ch = c["deviceSerial"], c["channelNo"]
        tag = f"{anon[sn]}_ch{ch}"
        enc = 1 if (statuses.get(sn) or {}).get("isEncrypt") else 0
        out(f"--- {tag} snapshot ---")
        try:
            cap = http("PUT", f"{API}/v3/devices/security/capture", headers=headers,
                       params={"deviceSerial": sn, "channelNo": ch, "isEncrypted": enc})
            pic = (cap.get("captureInfo") or {}).get("picUrl")
            out(f"  capture meta={cap.get('meta')}, picUrl host={urllib.parse.urlparse(pic).hostname if pic else None}")
            if pic:
                img = http("GET", pic, raw=True)
                out(f"  image {len(img)} B, starts with: {img[:4].hex(' ')}, hikencode: {img.startswith(b'hikencode')}")
                if img.startswith(b"hikencode") and code:
                    img = aes_cbc_decrypt(code_key(code), b"01234567".ljust(16, b"\0"), img[48:])
                    out(f"  decrypted: is JPEG = {img.startswith(JPEG_MAGIC)}")
                with open(f"hikconnect_probe_{tag}.jpg", "wb") as f:
                    f.write(img)
        except Exception as exc:  # pylint: disable=broad-except
            out(f"  snapshot failed: {exc!r}")

        vtm = vtms.get(sn) or next((vtms[v] for v in c.values() if isinstance(v, str) and v in vtms), None)
        if not vtm:
            try:
                r = http("GET", f"{API}/v3/streaming/vtm/{sn}/{ch}", headers=headers)
                vtm = r.get("streamServerConfig")
                out(f"  VTM via /v3/streaming/vtm: meta={r.get('meta', {}).get('code')}, fields={sorted(vtm or {})}")
            except Exception as exc:  # pylint: disable=broad-except
                out(f"  VTM via /v3/streaming/vtm failed: {exc!r}")
        if not vtm or not tokens:
            out("  no VTM info or no tokens, skipping live test")
            continue
        out(f"--- {tag} live: VTM {vtm.get('domain')}:{vtm.get('port')} ---")
        biz = c.get("streamBizUrl") or ""
        attempts = [] if os.environ.get("HC_SKIP_PLAIN") else [("plain", None)]
        pk = (ttss.get(sn) or {}).get("publicKey") or {}
        if pk.get("key"):
            try:
                import cryptography  # noqa: F401  pylint: disable=unused-import,import-outside-toplevel

                attempts.append(("ecdh", (base64.b64decode(pk["key"]), int(pk.get("version") or 1))))
            except ImportError:
                out("  (ECDH test skipped, run `pip install cryptography` to enable it)")
        for name, ecdh in attempts:
            if not tokens:
                break
            url = (f"ysproto://{vtm['domain']}:{vtm['port']}/live?dev={sn}&chn={ch}&stream=1&cln=55"
                   f"&isp=0&auth=1&ssn={tokens.pop(0)}{'&' + biz if biz else ''}&vip=0"
                   f"&timestamp={int(time.time() * 1000)}")
            try:
                if vtm_vtdu((vtm["domain"], int(vtm["port"])), url, f"{tag}_{name}", code, ecdh):
                    break
            except Exception as exc:  # pylint: disable=broad-except
                out(f"  [{tag}_{name}] failed: {exc!r}")

    out("=== end of report ===")
    with open("hikconnect_probe_report.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    print("\nReport saved to hikconnect_probe_report.txt - please paste it into the GitHub issue.")
    print("Images/videos (hikconnect_probe_*.jpg/.h264/.h265) are for you only, please do not upload them.")


if __name__ == "__main__":
    try:
        main()
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP error {e.code} on {urllib.parse.urlparse(e.url).path}")
