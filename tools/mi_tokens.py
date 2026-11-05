"""Fase 1: entra na conta Xiaomi e lista os aparelhos (did, modelo, IP, token) de cada região.

Uso:  python tools/mi_tokens.py            (renova pelo passToken salvo; sem ele pede usuário e senha)
      python tools/mi_tokens.py --login    (força login com senha e 2FA)
      python tools/mi_tokens.py --selftest (testa só o RC4, sem rede)

Saída: secrets/devices.json (tokens dos aparelhos) e secrets/session.json (sessão da nuvem,
reaproveitada pelas próximas ferramentas). Os dois arquivos são sigilosos: não versionar nem enviar.

O protocolo segue o observado no Xiaomi Home 11.8.706 (RC4 + ssecurity, docs/RE-achados.md §2)
e o esquema público usado pelos extratores de token da comunidade.
"""
import base64
import getpass
import hashlib
import http.cookiejar
import json
import os
import random
import string
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
SERVERS = ["cn", "de", "us", "ru", "tw", "sg", "in", "i2"]
ACCOUNT = "https://account.xiaomi.com"


def rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out = bytearray()
    # a Xiaomi descarta os primeiros 1024 bytes do fluxo RC4
    for n in range(1024 + len(data)):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        if n >= 1024:
            out.append(data[n - 1024] ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def json_resp(text: str) -> dict:
    return json.loads(text.replace("&&&START&&&", "", 1))


def mask(v: str) -> str:
    return v[:4] + "…" + v[-4:] if v and len(v) > 8 else "?"


# ---- HTTP só com a biblioteca padrão (o Python do usuário pode não ter `requests`) -------------
class Resp:
    def __init__(self, status: int, body: bytes):
        self.status_code, self.content = status, body

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text[:200]}")


class Jar(http.cookiejar.CookieJar):
    def set(self, name: str, value: str, domain: str) -> None:
        self.set_cookie(http.cookiejar.Cookie(
            0, name, value, None, False, "." + domain.lstrip("."), True, True, "/", True,
            False, None, False, None, None, {}))

    def get(self, name: str):
        return next((c.value for c in self if c.name == name), None)


class Session:
    def __init__(self):
        self.headers: dict = {}
        self.cookies = Jar()
        self._opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))

    def _do(self, method, url, params=None, data=None, headers=None, cookies=None, timeout=20) -> Resp:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        h = {**self.headers, **(headers or {})}
        if body is not None:
            h.setdefault("Content-Type", "application/x-www-form-urlencoded")
        if cookies:  # um Cookie explícito substitui o do jar (CookieJar não sobrescreve)
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        req = urllib.request.Request(url, data=body, headers=h, method=method)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                return Resp(r.status, r.read())
        except urllib.error.HTTPError as e:
            return Resp(e.code, e.read())

    def get(self, url, **kw) -> Resp:
        return self._do("GET", url, **kw)

    def post(self, url, **kw) -> Resp:
        return self._do("POST", url, **kw)


class MiCloud:
    def __init__(self):
        self.s = Session()
        # identidade de "aparelho" fixa entre execuções: depois do 2FA com trust=true a Xiaomi
        # reconhece este cliente e não pede o código de novo
        client = SECRETS / "client.json"
        if client.exists():
            c = json.loads(client.read_text(encoding="utf-8"))
            self.agent, self.device_id = c["agent"], c["device_id"]
        else:
            rnd = "".join(random.choices(string.ascii_letters + string.digits, k=16))
            self.agent = f"Android-7.1.1-1.0.0-ONEPLUS A3010-136-{rnd.upper()} APP/xiaomi.smarthome APPV/62830"
            self.device_id = "".join(random.choices(string.ascii_lowercase, k=16))
            SECRETS.mkdir(exist_ok=True)
            client.write_text(json.dumps({"agent": self.agent, "device_id": self.device_id}), encoding="utf-8")
        self.s.headers["User-Agent"] = self.agent
        self.s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="mi.com")
        self.s.cookies.set("sdkVersion", "accountsdk-18.8.15", domain="xiaomi.com")
        self.s.cookies.set("deviceId", self.device_id, domain="mi.com")
        self.s.cookies.set("deviceId", self.device_id, domain="xiaomi.com")
        self.user_id = self.ssecurity = self.service_token = self.pass_token = None

    # ---- login -------------------------------------------------------------------------------
    def _service_login(self) -> dict:
        r = self.s.get(f"{ACCOUNT}/pass/serviceLogin", params={"sid": "xiaomiio", "_json": "true"}, timeout=20)
        return json_resp(r.text)

    def _auth2(self, form: dict) -> dict:
        while True:
            r = self.s.post(f"{ACCOUNT}/pass/serviceLoginAuth2", data=form, timeout=20)
            d = json_resp(r.text)
            if d.get("captchaUrl"):
                form["captCode"] = self._captcha(d["captchaUrl"])
                continue
            return d

    def login(self, user: str, password: str) -> None:
        form = {
            "sid": "xiaomiio",
            "hash": hashlib.md5(password.encode()).hexdigest().upper(),
            "callback": "https://sts.api.io.mi.com/sts",
            "qs": "%3Fsid%3Dxiaomiio%26_json%3Dtrue",
            "user": user,
            "_sign": self._service_login().get("_sign", ""),
            "_json": "true",
        }
        d = self._auth2(form)
        if d.get("notificationUrl"):
            self._two_factor(d["notificationUrl"])
            # com o passToken do 2FA, o serviceLogin já devolve ssecurity; senão, repete o auth2
            d = self._service_login()
            if not d.get("ssecurity"):
                form["_sign"] = d.get("_sign", form["_sign"])
                d = self._auth2(form)
            if d.get("notificationUrl"):
                raise SystemExit("\nA Xiaomi ainda pede verificação depois do código. Me mande a saída acima.")
        if not d.get("ssecurity"):
            raise SystemExit(f"\nLogin recusado: {d.get('desc') or d.get('description') or d.get('code')}")
        self._finish(d)

    def login_with_token(self, user_id: str, pass_token: str) -> None:
        """Renova a sessão só com o passToken (sem senha nem 2FA), como o go2rtc faz."""
        r = self.s.get(f"{ACCOUNT}/pass/serviceLogin", params={"sid": "xiaomiio", "_json": "true"},
                       cookies={"userId": user_id, "passToken": pass_token}, timeout=20)
        d = json_resp(r.text)
        if not d.get("ssecurity"):
            raise SystemExit(f"\npassToken recusado ({d.get('code')}: {d.get('desc')}). Rode com --login.")
        d.setdefault("userId", user_id)
        d.setdefault("passToken", pass_token)
        self._finish(d)

    def _finish(self, d: dict) -> None:
        self.user_id, self.ssecurity = str(d["userId"]), d["ssecurity"]
        self.pass_token = d.get("passToken") or self.s.cookies.get("passToken")
        self.s.get(d["location"], timeout=20)
        self.service_token = self.s.cookies.get("serviceToken")
        if not self.service_token:
            raise SystemExit("\nLogin aceito, mas o serviceToken não veio (resposta inesperada do STS).")

    def _two_factor(self, url: str) -> None:
        """Fluxo da página identity/authStart: escolhe telefone/e-mail, envia o código, verifica."""
        log = []

        def step(name, r):
            try:
                d = json_resp(r.text)
            except ValueError:
                d = {"_raw": r.text[:300]}
            log.append({"step": name, "http": r.status_code, "code": d.get("code"),
                        "desc": d.get("desc") or d.get("description"), "keys": sorted(d)})
            (SECRETS / "debug_2fa.json").write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
            return d

        context = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["context"][0]
        base = {"sid": "xiaomiio", "context": context, "_locale": "pt_BR"}
        self.s.get(url, timeout=20)  # cookies da sessão de identidade
        d = step("list", self.s.get(f"{ACCOUNT}/identity/list", params=base, timeout=20))
        kinds = {4: "Phone", 8: "Email"}
        options = [o for o in (d.get("options") or [d.get("flag")]) if o in kinds]
        if not options:
            raise SystemExit(f"\n2FA: opções inesperadas ({d.get('options')}, flag={d.get('flag')}). "
                             f"Detalhes em {SECRETS / 'debug_2fa.json'}")
        flag = options[0]
        if len(options) > 1:
            print("\nA Xiaomi pede verificação. Enviar o código por:  1) SMS   2) e-mail")
            flag = options[1] if input("Escolha [1]: ").strip() == "2" else options[0]
        kind = kinds[flag]
        d = step("send", self.s.post(f"{ACCOUNT}/identity/auth/send{kind}Ticket",
                                     params={"_dc": int(time.time() * 1000), **base},
                                     data={"retry": "0", "icode": "", "_json": "true"}, timeout=20))
        if d.get("code") != 0:
            raise SystemExit(f"\n2FA: a Xiaomi não enviou o código ({d.get('code')}: {d.get('desc')}). "
                             f"Espere uns minutos e tente de novo.")
        ticket = input(f"Código recebido por {'SMS' if flag == 4 else 'e-mail'}: ").strip()
        d = step("verify", self.s.post(f"{ACCOUNT}/identity/auth/verify{kind}",
                                       params={"_flag": flag, "_json": "true", **base},
                                       data={"_flag": str(flag), "ticket": ticket, "trust": "true", "_json": "true"},
                                       timeout=20))
        if d.get("code") != 0 or not d.get("location"):
            raise SystemExit(f"\n2FA: código recusado ({d.get('code')}: {d.get('desc')}).")
        self.s.get(d["location"], timeout=20)  # segue os redirecionamentos e recebe o passToken
        log.append({"step": "after", "passToken": bool(self.s.cookies.get("passToken"))})
        (SECRETS / "debug_2fa.json").write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

    def _captcha(self, url: str) -> str:
        if url.startswith("/"):
            url = ACCOUNT + url
        img = SECRETS / "captcha.jpg"
        img.write_bytes(self.s.get(url, timeout=20).content)
        print(f"\nA Xiaomi pediu captcha. Abra {img} e digite o texto da imagem.")
        os.startfile(img) if os.name == "nt" else None
        return input("Captcha: ").strip()

    # ---- API cifrada (RC4) -------------------------------------------------------------------
    @staticmethod
    def api_url(server: str) -> str:
        return "https://api.io.mi.com/app" if server == "cn" else f"https://{server}.api.io.mi.com/app"

    def _signed_nonce(self, nonce: str) -> str:
        return b64(hashlib.sha256(base64.b64decode(self.ssecurity) + base64.b64decode(nonce)).digest())

    @staticmethod
    def _enc_signature(path: str, signed_nonce: str, params: dict) -> str:
        parts = ["POST", path] + [f"{k}={v}" for k, v in params.items()] + [signed_nonce]
        return b64(hashlib.sha1("&".join(parts).encode()).digest())

    def call(self, server: str, path: str, data: dict) -> dict:
        millis = int(time.time() * 1000)
        nonce = b64(os.urandom(8) + (millis // 60000).to_bytes(4, "big"))
        snonce = self._signed_nonce(nonce)
        key = base64.b64decode(snonce)
        params = {"data": json.dumps(data, separators=(",", ":"))}
        params["rc4_hash__"] = self._enc_signature(path, snonce, params)
        params = {k: b64(rc4(key, v.encode())) for k, v in params.items()}
        params["signature"] = self._enc_signature(path, snonce, params)
        params["ssecurity"] = self.ssecurity
        params["_nonce"] = nonce
        headers = {
            "Accept-Encoding": "identity",
            "Content-Type": "application/x-www-form-urlencoded",
            "x-xiaomi-protocal-flag-cli": "PROTOCAL-HTTP2",
            "MIOT-ENCRYPT-ALGORITHM": "ENCRYPT-RC4",
        }
        cookies = {
            "userId": self.user_id,
            "yetAnotherServiceToken": self.service_token,
            "serviceToken": self.service_token,
            "locale": "pt_BR",
            "timezone": "GMT-03:00",
            "is_daylight": "0",
            "dst_offset": "0",
            "channel": "MI_APP_STORE",
        }
        r = self.s.post(self.api_url(server) + path, data=params, headers=headers, cookies=cookies, timeout=20)
        r.raise_for_status()
        if r.text.lstrip().startswith("{"):  # erros às vezes voltam sem cifra
            return json.loads(r.text)
        return json.loads(rc4(key, base64.b64decode(r.text)))

    # ---- aparelhos ---------------------------------------------------------------------------
    def devices(self, server: str) -> list:
        homes = self.call(server, "/v2/homeroom/gethome",
                          {"fg": True, "fetch_share": True, "fetch_share_dev": True, "limit": 300, "app_ver": 7})
        found, seen = [], set()
        for h in (homes.get("result") or {}).get("homelist") or []:
            r = self.call(server, "/v2/home/home_device_list",
                          {"home_owner": self.user_id, "home_id": int(h["id"]), "limit": 200,
                           "get_split_device": True, "support_card_dev_type": True})
            for d in (r.get("result") or {}).get("device_info") or []:
                if d.get("did") not in seen:
                    seen.add(d.get("did"))
                    found.append(d | {"_home": h.get("name")})
        if not found:  # contas antigas: lista plana
            r = self.call(server, "/home/device_list",
                          {"getVirtualModel": True, "getHuamiDevices": 1, "get_split_device": False,
                           "support_smart_home": True})
            found = (r.get("result") or {}).get("list") or []
        return found


def selftest() -> None:
    # vetor clássico de RC4 (Key="Key", texto="Plaintext"), ajustado ao descarte de 1024 bytes
    def plain_rc4(key, data):
        s, j = list(range(256)), 0
        for i in range(256):
            j = (j + s[i] + key[i % len(key)]) & 0xFF
            s[i], s[j] = s[j], s[i]
        i = j = 0
        out = bytearray()
        for c in data:
            i = (i + 1) & 0xFF
            j = (j + s[i]) & 0xFF
            s[i], s[j] = s[j], s[i]
            out.append(c ^ s[(s[i] + s[j]) & 0xFF])
        return bytes(out)

    assert plain_rc4(b"Key", b"Plaintext").hex() == "bbf316e8d940af0ad3"
    msg = b"x" * 37
    assert rc4(b"Key", msg) == plain_rc4(b"Key", bytes(1024) + msg)[1024:]
    assert rc4(b"Key", rc4(b"Key", msg)) == msg
    print("selftest ok")


def main() -> None:
    if "--selftest" in sys.argv:
        return selftest()
    SECRETS.mkdir(exist_ok=True)
    mc = MiCloud()
    old = SECRETS / "session.json"
    saved = json.loads(old.read_text(encoding="utf-8")) if old.exists() else {}
    if saved.get("pass_token") and "--login" not in sys.argv:
        mc.login_with_token(saved["user_id"], saved["pass_token"])
        print(f"Sessão renovada pelo passToken (userId {mc.user_id}).\n")
    else:
        user = input("Conta Xiaomi (e-mail, telefone ou ID): ").strip()
        password = getpass.getpass("Senha (não aparece ao digitar): ")
        mc.login(user, password)
        del password
        print(f"Login ok (userId {mc.user_id}, passToken {'salvo' if mc.pass_token else 'NÃO veio'}).\n")

    result = {}
    for srv in SERVERS:
        try:
            devs = mc.devices(srv)
        except Exception as e:  # região sem acesso ou fora do ar
            print(f"[{srv}] erro: {e}")
            continue
        if not devs:
            continue
        result[srv] = devs
        print(f"[{srv}] {len(devs)} aparelho(s)")
        for d in devs:
            print(f"   {d.get('name', '?'):<28} {d.get('model', '?'):<26} did={d.get('did')} "
                  f"ip={d.get('localip', '?'):<15} token={mask(d.get('token', ''))}")

    _salvar(mc, result)
    print(f"\nSalvo em {SECRETS / 'devices.json'} e {SECRETS / 'session.json'} (sigilosos).")


def _salvar(mc: MiCloud, result: dict) -> None:
    (SECRETS / "devices.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    (SECRETS / "session.json").write_text(json.dumps({
        "user_id": mc.user_id, "ssecurity": mc.ssecurity, "service_token": mc.service_token,
        "pass_token": mc.pass_token,
        "user_agent": mc.agent, "device_id": mc.device_id, "saved_at": int(time.time()),
    }, indent=2), encoding="utf-8")


def atualizar_devices() -> list:
    """Sem perguntas: renova a sessão pelo passToken e regrava devices.json com os IPs atuais. O agente do PC
    chama a cada 6 h: o IP de uma câmera muda quando o roteador quer (Rancho 2 em 02/11) e a VPS precisa do novo
    para o endereço da ponte. Só consulta as regiões que já tinham aparelhos. Levanta erro se o passToken venceu."""
    saved = json.loads((SECRETS / "session.json").read_text(encoding="utf-8"))
    arq = SECRETS / "devices.json"
    antigo = json.loads(arq.read_text(encoding="utf-8")) if arq.exists() else {}
    mc = MiCloud()
    mc.login_with_token(saved["user_id"], saved["pass_token"])
    result = {}
    for srv in list(antigo) or SERVERS:
        try:
            devs = mc.devices(srv)
        except Exception:  # noqa: BLE001 - região fora do ar: fica a lista antiga dela
            devs = antigo.get(srv)
        if devs:
            result[srv] = devs
    if not result:
        raise RuntimeError("a nuvem não devolveu aparelho nenhum; devices.json mantido")
    _salvar(mc, result)
    return [d for devs in result.values() for d in devs]


if __name__ == "__main__":
    main()
