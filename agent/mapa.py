"""Mapa do robô (xiaomi.vacuum.c101, fabricado pela ijai) pela nuvem cn.

Fluxo confirmado em 21/10/2026:
1. (opcional) pedir ao robô que envie o mapa: ação MIoT 10.2 upload-by-mapid pela NUVEM (/miotspec/action).
   Pela LAN as ações de mapa ficam sem resposta.
2. URL assinada: /v2/home/get_interim_file_url_pro com obj_name "<userId>/<did>/<slot>", onde slot 0
   é o mapa atual e 1 é o trajeto (bucket xiaomi-map-c101-record).
3. Decifrar com o esquema ijai: aes_encrypt("wifi_sn+userId+did", mac + "c101") -> MD5 hex, chave
   "hex"; o wifi_sn vem da propriedade 1.5 (ou de 7.45). Depois zlib -> protobuf RobotMap.
4. Desenhar com vacuum-map-parser-ijai (Apache-2.0) -> PNG + cômodos {id: nome}.

O passToken fica só no PC (secrets/session.json). Para a VPS vão só a imagem e os cômodos.
Teste: agent\\.venv\\Scripts\\python.exe -X utf8 agent\\mapa.py  (gera secrets/mapa_teste.png)
"""
import base64
import binascii
import io
import json
import sys
import time
import urllib.request
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "tools")]
import mi_tokens  # noqa: E402
import miio  # noqa: E402

from Crypto.Hash import MD5  # noqa: E402
from vacuum_map_parser_base.config.color import ColorsPalette  # noqa: E402
from vacuum_map_parser_base.config.drawable import Drawable  # noqa: E402
from vacuum_map_parser_base.config.image_config import ImageConfig  # noqa: E402
from vacuum_map_parser_base.config.size import Sizes  # noqa: E402
from vacuum_map_parser_ijai import aes_decryptor  # noqa: E402
from vacuum_map_parser_ijai.map_data_parser import IjaiMapDataParser  # noqa: E402

SESSAO = ROOT / "secrets" / "session.json"
_nuvem = {"mc": None, "t": 0.0}


def nuvem() -> mi_tokens.MiCloud:
    """Sessão cn renovada pelo passToken a cada 6 h (sem senha nem 2FA)."""
    if _nuvem["mc"] is None or time.time() - _nuvem["t"] > 6 * 3600:
        s = json.loads(SESSAO.read_text(encoding="utf-8"))
        mc = mi_tokens.MiCloud()
        mc.login_with_token(s["user_id"], s["pass_token"])
        if mc.pass_token and mc.pass_token != s["pass_token"]:  # a Xiaomi pode girar o token
            s["pass_token"] = mc.pass_token
        s.update({"ssecurity": mc.ssecurity, "service_token": mc.service_token, "saved_at": int(time.time())})
        SESSAO.write_text(json.dumps(s, indent=2), encoding="utf-8")
        _nuvem.update(mc=mc, t=time.time())
    return _nuvem["mc"]


def acao_nuvem(did: str, siid: int, aiid: int, args: list) -> dict:
    r = nuvem().call("cn", "/miotspec/action", {"params": {"did": did, "siid": siid, "aiid": aiid, "in": args}})
    return r.get("result") or r


def wifi_sn(dev: miio.Device, user_id: str) -> str:
    for r in dev.get_props([(1, 5), (1, 3)]):
        v = r.get("value")
        if isinstance(v, str) and len(v) in (18, 20) and v.isupper():
            return v
    mp = dev.get_props([(7, 45)])[0].get("value") or ""
    for prop in str(mp).split(","):
        c = prop.replace('"', "")
        if user_id in c:
            c = c.split(";")[0]
        if len(c) in (18, 20) and c.isalnum() and c.isupper():
            return c
    raise RuntimeError("não achei o wifi_sn do robô (1.5/1.3/7.45)")


def decifrar(raw: bytes, sn: str, user_id: str, did: str, mac: str, modelo: str) -> bytes:
    try:
        raw = base64.b64decode(raw, validate=True)
    except binascii.Error:
        pass
    sufixo = modelo.split(".")[-1][-4:].rjust(4, "0")
    aeskey = aes_decryptor.aes_encrypt("+".join([sn, user_id, did]), mac.lower().replace(":", "") + sufixo)
    dig = MD5.new(aeskey.encode("utf-8")).hexdigest()
    erros = []
    for hex_key, chave in ((True, dig), (False, dig[8:-8].upper())):  # c101: chave hex (confirmado)
        try:
            return zlib.decompress(aes_decryptor.aes_decrypt(raw, chave, hex_key))
        except Exception as e:  # noqa: BLE001
            erros.append(e)
    raise RuntimeError(f"não consegui decifrar o mapa: {erros[0]}")


_DESENHOS = [Drawable.CHARGER, Drawable.VACUUM_POSITION, Drawable.PATH, Drawable.VIRTUAL_WALLS,
             Drawable.NO_GO_AREAS, Drawable.NO_MOPPING_AREAS, Drawable.ZONES, Drawable.OBSTACLES]


class Mapa:
    def __init__(self, info: dict):
        self.info, self.did = info, str(info["did"])
        self.sn = None
        self.parser = IjaiMapDataParser(ColorsPalette(), Sizes(), _DESENHOS, ImageConfig(scale=3), [])

    def baixar(self, dev: miio.Device, pedir_envio: bool = True) -> dict:
        mc = nuvem()
        if self.sn is None:
            self.sn = wifi_sn(dev, mc.user_id)
        if pedir_envio:
            mapa_id = dev.get_props([(10, 2)])[0].get("value")
            if mapa_id:
                acao_nuvem(self.did, 10, 2, [mapa_id])  # robô reenvia o mapa atual
                time.sleep(3)
        r = mc.call("cn", "/v2/home/get_interim_file_url_pro", {"obj_name": f"{mc.user_id}/{self.did}/0"})
        url = (r.get("result") or {}).get("url")
        if not url:
            raise RuntimeError(f"a nuvem não devolveu a URL do mapa ({r.get('code')}: {r.get('message')})")
        with urllib.request.urlopen(url, timeout=30) as resp:
            bruto = resp.read()
        md = self.parser.parse(decifrar(bruto, self.sn, mc.user_id, self.did, self.info["mac"], self.info["model"]))
        buf = io.BytesIO()
        md.image.data.save(buf, format="PNG", optimize=True)
        dm = md.image.dimensions
        w, h = md.image.data.size
        comodos = []
        for k, v in sorted((md.rooms or {}).items()):
            # posição da etiqueta em % da imagem (o parser usa Y para cima; a imagem, para baixo)
            pt = dm.img_transformation(type(md.charger)(v.pos_x, v.pos_y)) if v.pos_x is not None else None
            comodos.append({"id": int(k), "nome": getattr(v, "name", None) or f"Cômodo {k}",
                            "x": round(100 * pt.x * dm.scale / w, 2) if pt else None,
                            "y": round(100 * (dm.height - pt.y) * dm.scale / h, 2) if pt else None})
        return {"png": buf.getvalue(), "comodos": comodos, "tamanho": [w, h]}


if __name__ == "__main__":
    d = next(x for x in miio.load_devices() if x.get("model") == "xiaomi.vacuum.c101")
    out = Mapa(d).baixar(miio.Device(d["localip"], d["token"]))
    (ROOT / "secrets" / "mapa_teste.png").write_bytes(out["png"])
    print("imagem", out["tamanho"], "cômodos:", out["comodos"])
