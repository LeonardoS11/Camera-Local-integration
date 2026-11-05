"""Cliente miIO/MIoT local (UDP 54321) só com a biblioteca padrão.

Uso:
  python tools/miio.py probe                 hello + leitura de estado de cada aparelho em secrets/devices.json
  python tools/miio.py get <nome|did> SIID PIID [SIID PIID ...]
  python tools/miio.py action <nome|did> SIID AIID [valores...]   (ex.: action Robson 7 11 1 = lavar mop)
  python tools/miio.py --selftest            testa o AES contra vetores FIPS-197 (sem rede)

Protocolo (docs/RE-achados.md §3): cabeçalho de 32 bytes (0x2131, tamanho, 0, did, stamp, md5),
corpo AES-128-CBC com key=MD5(token), iv=MD5(key+token), PKCS#7. `probe`/`get` só leem; `action` executa.
"""
import hashlib
import json
import re
import socket
import struct
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEVICES = ROOT / "secrets" / "devices.json"

# ---- AES-128 (FIPS-197), suficiente para pacotes pequenos --------------------------------------
_SBOX = bytearray(256)
_INV = bytearray(256)


def _init_sbox():
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ (((q << 1) | (q >> 7)) & 0xFF) ^ (((q << 2) | (q >> 6)) & 0xFF) \
            ^ (((q << 3) | (q >> 5)) & 0xFF) ^ (((q << 4) | (q >> 4)) & 0xFF)
        _SBOX[p] = x ^ 0x63
        if p == 1:
            break
    _SBOX[0] = 0x63
    for i, v in enumerate(_SBOX):
        _INV[v] = i


_init_sbox()


def _xt(a):
    return ((a << 1) ^ 0x1B) & 0xFF if a & 0x80 else a << 1


def _mul(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xt(a)
        b >>= 1
    return r


def _expand(key: bytes):
    w = [list(key[i:i + 4]) for i in range(0, 16, 4)]
    rcon = 1
    for i in range(4, 44):
        t = list(w[i - 1])
        if i % 4 == 0:
            t = [_SBOX[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xt(rcon)
        w.append([a ^ b for a, b in zip(w[i - 4], t)])
    return [sum(w[r * 4:r * 4 + 4], []) for r in range(11)]


def _enc_block(rk, b):
    s = [x ^ k for x, k in zip(b, rk[0])]
    for r in range(1, 11):
        s = [_SBOX[x] for x in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]  # ShiftRows (coluna-major)
        if r != 10:
            m = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                m += [_mul(a[0], 2) ^ _mul(a[1], 3) ^ a[2] ^ a[3],
                      a[0] ^ _mul(a[1], 2) ^ _mul(a[2], 3) ^ a[3],
                      a[0] ^ a[1] ^ _mul(a[2], 2) ^ _mul(a[3], 3),
                      _mul(a[0], 3) ^ a[1] ^ a[2] ^ _mul(a[3], 2)]
            s = m
        s = [x ^ k for x, k in zip(s, rk[r])]
    return bytes(s)


def _dec_block(rk, b):
    s = [x ^ k for x, k in zip(b, rk[10])]
    for r in range(9, -1, -1):
        s = [s[(i - 4 * (i % 4)) % 16] for i in range(16)]  # InvShiftRows
        s = [_INV[x] for x in s]
        s = [x ^ k for x, k in zip(s, rk[r])]
        if r != 0:
            m = []
            for c in range(4):
                a = s[4 * c:4 * c + 4]
                m += [_mul(a[0], 14) ^ _mul(a[1], 11) ^ _mul(a[2], 13) ^ _mul(a[3], 9),
                      _mul(a[0], 9) ^ _mul(a[1], 14) ^ _mul(a[2], 11) ^ _mul(a[3], 13),
                      _mul(a[0], 13) ^ _mul(a[1], 9) ^ _mul(a[2], 14) ^ _mul(a[3], 11),
                      _mul(a[0], 11) ^ _mul(a[1], 13) ^ _mul(a[2], 9) ^ _mul(a[3], 14)]
            s = m
    return bytes(s)


def cbc_encrypt(key, iv, data):
    rk = _expand(key)
    pad = 16 - len(data) % 16
    data += bytes([pad]) * pad
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        prev = _enc_block(rk, bytes(a ^ b for a, b in zip(data[i:i + 16], prev)))
        out += prev
    return out


def cbc_decrypt(key, iv, data):
    rk = _expand(key)
    out, prev = b"", iv
    for i in range(0, len(data), 16):
        blk = data[i:i + 16]
        out += bytes(a ^ b for a, b in zip(_dec_block(rk, blk), prev))
        prev = blk
    return out[:-out[-1]] if out and 1 <= out[-1] <= 16 else out


def _json_tolerante(raw: bytes) -> dict:
    """Alguns firmwares mandam JSON quebrado: vírgula antes de ] ou }, lixo depois do último }."""
    raw = raw.rstrip(b"\x00")
    try:
        return json.loads(raw)
    except ValueError:
        raw = raw[: raw.rfind(b"}") + 1]
        return json.loads(re.sub(rb",\s*([\]}])", rb"\1", raw))


# ---- miIO --------------------------------------------------------------------------------------
HELLO = bytes.fromhex("21310020") + b"\xff" * 28


class Device:
    def __init__(self, ip: str, token: str, timeout: float = 3.0):
        self.ip, self.token = ip, bytes.fromhex(token)
        self.key = hashlib.md5(self.token).digest()
        self.iv = hashlib.md5(self.key + self.token).digest()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(timeout)
        self.did = self.stamp = None
        self.t0 = 0.0
        self.msg_id = int(time.time()) % 10000

    def hello(self) -> dict:
        self.sock.sendto(HELLO, (self.ip, 54321))
        pkt, _ = self.sock.recvfrom(1024)
        _, _, _, self.did, self.stamp = struct.unpack(">HHIII", pkt[:16])
        self.t0 = time.time()
        return {"did": self.did, "stamp": self.stamp}

    def send(self, method: str, params) -> dict:
        if self.did is None:
            self.hello()
        self.msg_id += 1
        body = json.dumps({"id": self.msg_id, "method": method, "params": params}, separators=(",", ":"))
        enc = cbc_encrypt(self.key, self.iv, body.encode())
        stamp = self.stamp + int(time.time() - self.t0)
        head = struct.pack(">HHIII", 0x2131, 32 + len(enc), 0, self.did, stamp)
        pkt = head + hashlib.md5(head + self.token + enc).digest() + enc
        self.sock.sendto(pkt, (self.ip, 54321))
        while True:
            resp, _ = self.sock.recvfrom(4096)
            if len(resp) <= 32:
                continue
            if hashlib.md5(resp[:16] + self.token + resp[32:]).digest() != resp[16:32]:
                raise RuntimeError("checksum da resposta não confere (token errado?)")
            d = _json_tolerante(cbc_decrypt(self.key, self.iv, resp[32:]))
            if d.get("id") == self.msg_id:
                return d

    def action(self, siid: int, aiid: int, args=()) -> dict:
        if self.did is None:
            self.hello()
        return self.send("action", {"did": str(self.did), "siid": siid, "aiid": aiid, "in": list(args)})

    def set_props(self, itens) -> dict:
        """itens: [(siid, piid, valor), ...]"""
        if self.did is None:
            self.hello()
        return self.send("set_properties", [{"did": str(self.did), "siid": s, "piid": p, "value": v}
                                            for s, p, v in itens])

    def get_props(self, pairs) -> list:
        if self.did is None:
            self.hello()
        params = [{"did": str(self.did), "siid": s, "piid": p} for s, p in pairs]
        return self.send("get_properties", params).get("result", [])


# leituras seguras por modelo (spec em home.miot-spec.com)
PROBES = {
    "xiaomi.vacuum.c101": [(2, 1), (2, 2), (3, 1), (2, 4), (7, 4), (7, 5), (7, 6)],  # + mop, sucção, água
    "chuangmi.camera.025b02": [(2, 1), (4, 1), (5, 1)],       # ligada, cartão SD, detecção
    "mxiang.camera.moc001": [(2, 1)],
    "chuangmi.camera.042a02": [(2, 1)],
}


# nomes dos valores (spec urn:miot-spec-v2:device:vacuum:0000A006:xiaomi-c101:2)
LABELS = {
    ("xiaomi.vacuum.c101", 2, 1): ["dormindo", "em espera", "pausado", "voltando à base", "carregando",
                                   "aspirando", "aspirando e passando pano", "passando pano", "atualizando",
                                   "lavando o mop", "secando o mop", "coletando o pó", "secagem pausada"],
    ("xiaomi.vacuum.c101", 2, 4): ["aspirar", "aspirar e passar pano", "passar pano", "aspirar, depois pano"],
    ("xiaomi.vacuum.c101", 7, 4): ["sem mop", "com mop"],
    ("xiaomi.vacuum.c101", 7, 5): ["silenciosa", "padrão", "média", "turbo"],
    ("xiaomi.vacuum.c101", 7, 6): ["pouca", "média", "muita"],
}


def label(model, siid, piid, value):
    names = LABELS.get((model, siid, piid))
    return f" = {names[value]}" if names and isinstance(value, int) and 0 <= value < len(names) else ""


def load_devices():
    data = json.loads(DEVICES.read_text(encoding="utf-8"))
    return [d for devs in data.values() for d in devs]


def probe():
    for d in load_devices():
        name, ip, model = d.get("name"), d.get("localip"), d.get("model")
        print(f"\n{name} ({model}) {ip}")
        dev = Device(ip, d["token"])
        try:
            h = dev.hello()
            print(f"  hello ok: did={h['did']} (nuvem: {d.get('did')})")
        except OSError as e:
            print(f"  sem resposta na UDP 54321: {e}")
            continue
        try:
            for r in dev.get_props(PROBES.get(model, [(2, 1)])):
                v = r.get("value")
                print(f"  siid {r.get('siid')} piid {r.get('piid')}: {v!r}"
                      f"{label(model, r.get('siid'), r.get('piid'), v)} (code {r.get('code')})")
        except (OSError, RuntimeError, ValueError) as e:
            print(f"  hello respondeu, mas get_properties falhou: {e}")


def get(target, nums):
    d = next(x for x in load_devices() if target in (x.get("name"), str(x.get("did"))))
    pairs = [(int(nums[i]), int(nums[i + 1])) for i in range(0, len(nums), 2)]
    print(json.dumps(Device(d["localip"], d["token"]).get_props(pairs), indent=2, ensure_ascii=False))


def action(target, siid, aiid, args):
    d = next(x for x in load_devices() if target in (x.get("name"), str(x.get("did"))))
    vals = [int(a) if a.lstrip("-").isdigit() else a for a in args]
    print(json.dumps(Device(d["localip"], d["token"]).action(int(siid), int(aiid), vals), ensure_ascii=False))


def selftest():
    key = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    rk = _expand(key)
    assert _enc_block(rk, pt).hex() == "69c4e0d86a7b0430d8cdb78070b4c55a"
    assert _dec_block(rk, bytes.fromhex("69c4e0d86a7b0430d8cdb78070b4c55a")) == pt
    iv = bytes(range(16))
    for n in (0, 5, 16, 33):
        assert cbc_decrypt(key, iv, cbc_encrypt(key, iv, b"a" * n)) == b"a" * n
    print("selftest ok")


if __name__ == "__main__":
    a = sys.argv[1:]
    if a[:1] == ["--selftest"]:
        selftest()
    elif a[:1] == ["probe"]:
        probe()
    elif a[:1] == ["get"] and len(a) >= 4:
        get(a[1], a[2:])
    elif a[:1] == ["action"] and len(a) >= 4:
        action(a[1], a[2], a[3], a[4:])
    else:
        print(__doc__)
