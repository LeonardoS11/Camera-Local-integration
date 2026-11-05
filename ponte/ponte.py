"""Ponte de uma casa sem PC (ex.: rancho) num celular Android antigo com Termux.

O celular só faz o que exige estar na rede local: o go2rtc conversa com as câmeras Xiaomi (P2P na LAN) e
este script empurra o vídeo (MPEG-TS) para a VPS, que faz todo o resto (detecção, rostos, IA, gravação,
avisos). Só usa a biblioteca padrão do Python: no Termux basta `pkg install python`.

  python ponte.py parear     # uma vez: pede o código de 6 dígitos gerado no painel (Ajustes > Pontes)
  python ponte.py            # roda: sobe o go2rtc, envia o vídeo, vigia travamentos, manda sinal de vida

Quando o ao vivo cai (Wi-Fi fraco, celular reiniciado), a ponte guarda o intervalo em cartao.json e depois pede à
câmera a gravação do cartão SD daquele horário (go2rtc com patch, MISS 0x10D), que a VPS grava em Gravações.

A conta Xiaomi entra pelo próprio go2rtc do celular: abra http://127.0.0.1:1984 no navegador do celular,
Add > Xiaomi, e faça o login (com o 2FA). A senha não passa por este script nem pela VPS.
"""
import http.client
import json
import logging
import os
import re
import hashlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

PASTA = Path(__file__).resolve().parent
CONFIG = PASTA / "config.json"
YAML = PASTA / "go2rtc.yaml"
VERSAO = "1.9"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
                    handlers=[logging.StreamHandler(),
                              RotatingFileHandler(PASTA / "ponte.log", maxBytes=1_000_000, backupCount=2,
                                                  encoding="utf-8")])
log = logging.getLogger("ponte")
PORTA_API = 1984  # API do go2rtc local (o teste no PC usa outra)
UA = "casa-ponte/" + VERSAO  # o "Python-urllib" padrão é barrado pela Cloudflare se o proxy voltar


def _req(vps, method, path, obj=None, token="", timeout=30):
    h = {"User-Agent": UA, "Content-Type": "application/json"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    data = json.dumps(obj).encode() if obj is not None else None
    with urllib.request.urlopen(urllib.request.Request(vps + path, data=data, headers=h, method=method),
                                timeout=timeout) as r:
        return json.loads(r.read() or b"null")


# ---- pareamento --------------------------------------------------------------------------------------
def parear():
    """Sem perguntas quando o comando do site já traz o código (PONTE_CODIGO; PONTE_VPS e PONTE_NOME opcionais)."""
    cfg = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    padrao = os.environ.get("PONTE_VPS") or cfg.get("vps", "https://seu-dominio.exemplo.com")
    codigo = os.environ.get("PONTE_CODIGO", "").strip()
    if codigo:
        vps, nome = padrao, os.environ.get("PONTE_NOME") or "celular da ponte"
    else:
        vps = input(f"Endereço da VPS [{padrao}]: ").strip() or padrao
        codigo = input("Código de 6 dígitos (painel > Admin > Pontes > Configurar celular): ").strip().replace(" ", "")
        nome = input("Nome deste celular [celular da ponte]: ").strip() or "celular da ponte"
    try:
        r = _req(vps.rstrip("/"), "POST", "/api/ponte/parear", {"codigo": codigo, "aparelho": nome})
    except urllib.error.HTTPError as e:
        sys.exit(f"Não pareou ({e.code}): {e.read().decode(errors='replace')[:200]}")
    cfg = {"vps": vps.rstrip("/"), "token": r["token"], "casa": r["casa"], "cameras": r["cameras"]}
    CONFIG.write_text(json.dumps(cfg, indent=1))
    os.chmod(CONFIG, 0o600)
    escrever_yaml(cfg["cameras"])
    print(f"Pareado com a casa '{r['casa']}' ({', '.join(r['cameras'])}).")


# ---- go2rtc ------------------------------------------------------------------------------------------
def escrever_yaml(cameras: dict):
    """Reescreve o go2rtc.yaml mantendo a seção xiaomi: (credencial que o próprio go2rtc grava no login)."""
    antigo = YAML.read_text(encoding="utf-8") if YAML.exists() else ""
    m = re.search(r"(?m)^xiaomi:\n(?:[ \t]+.*\n?)+", antigo)
    linhas = ["# gerado pela ponte.py; a seção xiaomi é gravada pelo go2rtc ao entrar na conta",
              "api:", '  listen: "127.0.0.1:1984"', "rtsp:", '  listen: "127.0.0.1:8554"',
              "webrtc:", '  listen: ""', "log:", "  level: info"]
    if m:
        linhas.append(m.group(0).rstrip("\n"))
    linhas.append("streams:")
    linhas += [f"  {cam}: {url}" for cam, url in cameras.items()]
    novo = "\n".join(linhas) + "\n"
    if novo != antigo:
        YAML.write_text(novo, encoding="utf-8")
        return True
    return False


def binario() -> Path:
    maq = os.uname().machine
    nome = "go2rtc_arm64" if maq in ("aarch64", "arm64") else "go2rtc_armv7"
    return PASTA / nome


def env_go2rtc() -> dict:
    """O Go procura os certificados raiz em /etc/ssl (não existe no Android) e o login Xiaomi falhava com
    "x509: certificate signed by unknown authority" (30/10): aponta para os do Termux (pacote ca-certificates)."""
    env = dict(os.environ)
    prefixo = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    for cert in (f"{prefixo}/etc/tls/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.isfile(cert):
            env.setdefault("SSL_CERT_FILE", cert)
            break
    return env


class Go2rtc(threading.Thread):
    """Mantém o go2rtc rodando (reinicia se cair ou se a lista de câmeras mudar)."""

    def __init__(self):
        super().__init__(name="go2rtc", daemon=True)
        self.proc = None
        self.reiniciar = threading.Event()

    def run(self):
        while True:
            log.info("iniciando %s", binario().name)
            glog = PASTA / "go2rtc.log"
            if glog.exists() and glog.stat().st_size > 5_000_000:
                os.replace(glog, PASTA / "go2rtc.log.1")
            with open(PASTA / "go2rtc.log", "ab") as saida:
                self.proc = subprocess.Popen([str(binario()), "-config", str(YAML)], cwd=PASTA, env=env_go2rtc(),
                                             stdout=saida, stderr=subprocess.STDOUT)
                while self.proc.poll() is None:
                    if self.reiniciar.wait(5):
                        self.reiniciar.clear()
                        self.proc.terminate()
                        try:
                            self.proc.wait(10)
                        except subprocess.TimeoutExpired:
                            self.proc.kill()
            log.warning("go2rtc saiu (%s); reiniciando em 5 s", self.proc.returncode)
            time.sleep(5)


def streams() -> dict:
    with urllib.request.urlopen("http://127.0.0.1:1984/api/streams", timeout=10) as r:
        return json.loads(r.read())


def bytes_recebidos(s: dict) -> int:
    return sum(rc.get("bytes", 0) for p in (s.get("producers") or []) for rc in (p.get("receivers") or []))


# ---- buracos no ao vivo (recuperados depois do cartão SD) ---------------------------------------------
CARTAO = PASTA / "cartao.json"
BURACO_MIN = 20          # s sem mandar vídeo = buraco
BURACO_MAX_IDADE = 86400  # não busca no cartão nada mais velho que isto
TRECHO = 300             # s por pedido ao cartão (um arquivo de gravação na VPS)
_cartao_lock = threading.Lock()
_cartao: dict = {}


def cartao_carregar():
    global _cartao
    try:
        _cartao = json.loads(CARTAO.read_text())
    except (OSError, ValueError):
        _cartao = {}
    _cartao.setdefault("ultimo", {})    # câmera -> último instante com vídeo indo para a VPS
    _cartao.setdefault("buracos", [])   # {cam, ini, fim, falhas, proxima}
    _cartao.setdefault("cameras", {})   # câmera -> {recuperado_s, perdido_s, ultimo_erro}
    # primeira vez com o cartão (ponte atualizada): a VPS pede o tempo em que ficou sem notícia desta ponte
    _cartao.setdefault("novo", not CARTAO.exists())


def cartao_salvar():
    tmp = CARTAO.with_suffix(".tmp")
    tmp.write_text(json.dumps(_cartao))
    os.replace(tmp, CARTAO)


def marcar_envio(cam: str, agora: float):
    """Chamado a cada pedaço enviado ao vivo: se ficou mais de BURACO_MIN sem vídeo (queda, celular reiniciado),
    guarda o intervalo para buscar no cartão. Os 5 s de folga cobrem o que ficou no meio do caminho."""
    with _cartao_lock:
        ult = _cartao["ultimo"].get(cam)
        if ult and agora - ult > BURACO_MIN:
            ini = max(ult - 5, agora - BURACO_MAX_IDADE)
            if agora - ini > BURACO_MIN:
                _cartao["buracos"].append({"cam": cam, "ini": ini, "fim": agora + 5, "falhas": 0, "proxima": 0})
                log.info("%s: buraco de %.0f s no ao vivo; vou buscar no cartão", cam, agora - ini)
        salvar = not ult or int(agora) // 30 != int(ult) // 30
        _cartao["ultimo"][cam] = agora
        if salvar:  # grava no máximo a cada ~30 s: é isto que mostra o buraco depois de um reinício
            cartao_salvar()


# ---- vídeo para a VPS --------------------------------------------------------------------------------
ESTADO: dict[str, dict] = {}  # por câmera: enviando, MB enviados, último erro


def repassar(cfg: dict, cam: str):
    """go2rtc local (GET MPEG-TS) -> VPS (POST chunked em HTTPS com o token da ponte). Mesmo esquema do PC."""
    u = urllib.parse.urlparse(cfg["vps"])
    espera = 5
    st = ESTADO.setdefault(cam, {"enviando": False, "mb": 0.0, "erro": ""})
    while True:
        enviado, inicio, src, dst = 0, time.time(), None, None
        try:
            # 60 s: pela internet (go2rtc 1.8: servidores CS2 quando a câmera não está na rede local) o 1º vídeo
            # demora mais que na rede local
            src = http.client.HTTPConnection("127.0.0.1", 1984, timeout=60)
            src.request("GET", f"/api/stream.ts?src={cam}")
            r = src.getresponse()
            if r.status != 200:  # o corpo traz o motivo (ex.: erro da nuvem Xiaomi)
                raise OSError(f"go2rtc local respondeu {r.status}: {limpar(r.read(400).decode(errors='replace').strip())}")
            dst = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=30)
            dst.putrequest("POST", f"/ingest/api/stream.ts?dst={cam}", skip_accept_encoding=True)
            for k, v in {"Authorization": f"Bearer {cfg['token']}", "User-Agent": UA,
                         "Content-Type": "video/mp2t", "Transfer-Encoding": "chunked"}.items():
                dst.putheader(k, v)
            dst.endheaders()
            log.info("%s: enviando vídeo para a VPS", cam)
            st.update(enviando=True, erro="", falhando_desde=0)
            while True:
                b = r.read1(65536)
                if not b:
                    raise OSError("go2rtc local encerrou o fluxo")
                dst.send(f"{len(b):X}\r\n".encode() + b + b"\r\n")
                enviado += len(b)
                st["mb"] += len(b) / 1e6
                marcar_envio(cam, time.time())
        except (OSError, http.client.HTTPException) as e:
            dur = time.time() - inicio
            st.update(enviando=False, erro=str(e)[:200])
            if dur < 60 and not st.get("falhando_desde"):
                st["falhando_desde"] = inicio
            log.warning("%s: envio parou após %.0f s / %.1f MB: %s", cam, dur, enviado / 1e6, e)
            espera = 5 if dur > 60 else min(espera * 2, 120)
            time.sleep(espera)
        finally:
            for conn in (src, dst):
                if conn is not None:
                    conn.close()


RECRIAR_S = 300       # câmera sem conectar há tanto tempo: recria o stream dela no go2rtc
REINICIAR_S = 1200    # ... e há tanto: reinicia o go2rtc (no máximo uma vez por hora)


def recriar_stream(cam: str, url: str) -> str:
    """DELETE + PUT do stream na API do go2rtc: derruba a sessão P2P e abre outra. Devolve o erro, se houver."""
    base, q = f"http://127.0.0.1:{PORTA_API}/api/streams", urllib.parse.quote
    try:
        urllib.request.urlopen(urllib.request.Request(f"{base}?src={q(cam)}", method="DELETE"), timeout=10)
        urllib.request.urlopen(urllib.request.Request(f"{base}?name={q(cam)}&src={q(url, safe='')}",
                                                      method="PUT"), timeout=10)
        return ""
    except (urllib.error.URLError, OSError) as e:
        log.warning("%s: não consegui reabrir: %s", cam, e)
        return str(e)


def ip_da_camera(url: str) -> str:
    return urllib.parse.urlsplit(url).hostname or ""


def alcance(ip: str, espera: float = 2.0) -> bool:
    """A câmera responde na rede local? Manda a mesma busca UDP (porta 32108) com que o go2rtc começa a sessão
    P2P; a resposta prova que ela está na rede do celular (sem abrir sessão). Em 02/11 a Rancho 2 mudou de rede
    (192.168.68 → 192.168.1) e o erro era só "read udp: i/o timeout"."""
    if not ip:
        return False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.settimeout(espera)
            s.sendto(b"\xf1\x30\x00\x00", (ip, 32108))
            while True:
                dados, de = s.recvfrom(64)
                if de[0] == ip and len(dados) >= 2 and dados[0] == 0xF1:
                    return True
    except OSError:
        return False


def rede() -> dict:
    """IP do celular na rede local (sem mandar pacote: só a rota que o sistema escolheria até a VPS)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 53))
            return {"ip": s.getsockname()[0]}
    except OSError:
        return {}


def vigia(cfg: dict, g: Go2rtc, parado_s: int = 60):
    """Mesmo problema visto na Garagem (22/10): sessão P2P "conectada" com bytes parados. Recria o stream.
    Também quando todo pedido falha logo na conexão ("read udp ... i/o timeout"): o Rancho 1 ficou assim das 21:06
    de 31/10 até a ponte ser reiniciada (voltou na hora), e de novo das 12:07 de 01/11. Primeiro recria o stream;
    se não adiantar, reinicia o go2rtc inteiro (as outras câmeras param ~10 s)."""
    ultimo: dict[str, tuple[int, float]] = {}
    recriado: dict[str, float] = {}
    reiniciado = 0.0
    recriar = recriar_stream

    while True:
        time.sleep(20)
        try:
            ss = streams()
        except (urllib.error.URLError, OSError, ValueError):
            continue
        agora = time.time()
        for cam, url in cfg["cameras"].items():
            desde = (ESTADO.get(cam) or {}).get("falhando_desde") or 0
            if desde and agora - desde > RECRIAR_S:
                if agora - desde > REINICIAR_S and agora - reiniciado > 3600:
                    log.warning("%s: sem conectar há %d s; reiniciando o go2rtc", cam, agora - desde)
                    reiniciado = agora
                    g.reiniciar.set()
                    break
                if agora - recriado.get(cam, 0) > RECRIAR_S:
                    log.warning("%s: sem conectar há %d s; recriando o stream", cam, agora - desde)
                    recriado[cam] = agora
                    recriar(cam, url)
                continue
            s = ss.get(cam) or {}
            total = bytes_recebidos(s)
            b, desde = ultimo.get(cam, (-1, agora))
            if total != b:
                ultimo[cam] = (total, agora)
                continue
            if agora - desde > parado_s and s.get("consumers"):
                log.warning("%s: vídeo parado há %d s; reabrindo a sessão da câmera", cam, agora - desde)
                recriar(cam, url)
                ultimo[cam] = (-1, agora)


# ---- recuperação pelo cartão SD ----------------------------------------------------------------------
def duracao_pts(buf: bytearray, est: dict) -> float:
    """Segundos de vídeo já vistos no MPEG-TS (pelo PTS dos pacotes de vídeo). Consome os pacotes inteiros de buf.
    Soma passo a passo: na troca de arquivo de 1 min do cartão o relógio da câmera salta (visto 13 h na garagem);
    saltos acima de 10 s contam como um quadro, como o ffmpeg da VPS faz (dts_delta_threshold)."""
    i, n = 0, len(buf)
    while n - i >= 188:
        if buf[i] != 0x47:  # fora de sincronia: procura o próximo pacote
            j = buf.find(b"\x47", i + 1)
            i = j if j > 0 else n
            continue
        p = buf[i:i + 188]
        i += 188
        off = 4 + (1 + p[4] if p[3] & 0x20 else 0)
        if not p[1] & 0x40 or not p[3] & 0x10 or off + 14 > 188:
            continue
        if p[off:off + 3] != b"\x00\x00\x01" or not 0xE0 <= p[off + 3] <= 0xEF or not p[off + 7] & 0x80:
            continue
        q = p[off + 9:off + 14]
        pts = ((q[0] >> 1) & 7) << 30 | q[1] << 22 | (q[2] >> 1) << 15 | q[3] << 7 | q[4] >> 1
        if "ultimo" in est:
            passo = (pts - est["ultimo"]) % (1 << 33)
            est["soma"] = est.get("soma", 0) + (passo if passo <= 900000 else 4500)  # >10 s: salto; 4500 = 50 ms
        est["ultimo"] = pts
    del buf[:i]
    return est.get("soma", 0) / 90000


def respostas_cartao(desde: int) -> list[tuple[str, int | None]]:
    """Respostas da câmera ao pedido de reprodução (o go2rtc com patch escreve cada uma no go2rtc.log):
    status filefound (com o início do arquivo de 1 min), endoffile ou outro (sem gravação/sem cartão)."""
    try:
        with open(PASTA / "go2rtc.log", "rb") as f:
            f.seek(0, 2)
            f.seek(desde if f.tell() >= desde else 0)
            linhas = f.read(1 << 20).decode(errors="replace").splitlines()
    except OSError:
        return []
    saida = []
    for x in linhas:
        if "playback command answer" in x and (s := re.search(r"status\W+(\w+)", x)):
            t = re.search(r"starttime\W+(\d+)", x)
            saida.append((s.group(1), int(t.group(1)) if t else None))
    return saida


def baixar_trecho(cfg: dict, cam: str, a: float, b: float, speed: int, fim: float = 0) -> tuple[str, float, float, str]:
    """Pede à câmera a gravação do cartão de a até b e manda para a VPS, que grava como um trecho comum de
    Gravações. Devolve (ok | sem_gravacao | falha, início real, segundos de vídeo, erro).
    A câmera toca a partir do primeiro arquivo (1 min) que COMEÇA depois do horário pedido (31/10: pedido 11:03:37,
    veio o de 11:03:49); por isso pede 1 min antes e usa o início que ela responde.
    A janela do pedido vai até `fim` (o fim do buraco): a câmera responde o primeiro arquivo que existe nela, então
    um buraco sem gravação (cartão só com movimento, sem cartão) custa um pedido só, não um a cada 5 min (31/10: a
    EC3 Pro respondeu filenotfound; dezenas de sessões seguidas já travaram a garagem). Em sem_gravacao o 2º valor
    é até onde se sabe que não há nada."""
    fim = max(fim, b)
    nome, q = f"cartao_{cam}", urllib.parse.quote
    base = f"http://127.0.0.1:{PORTA_API}/api/streams"
    url = cfg["cameras"][cam] + f"&playback={int(a) - 60}&endtime={int(fim) + 60}&live=0&speed={speed}&audio=0"
    try:
        desde = (PASTA / "go2rtc.log").stat().st_size
    except OSError:
        desde = 0
    urllib.request.urlopen(urllib.request.Request(f"{base}?name={nome}&src={q(url, safe='')}", method="PUT"),
                           timeout=10)
    src = dst = None
    try:
        src = http.client.HTTPConnection("127.0.0.1", PORTA_API, timeout=90)  # go2rtc espera até 45 s o 1º quadro
        src.request("GET", f"/api/stream.ts?src={nome}")
        r = src.getresponse()
        if r.status != 200:  # diz também se a câmera chegou a responder ao pedido (sem cartão x lenta)
            resp = respostas_cartao(desde)
            quem = f"câmera respondeu {', '.join(x for x, _ in resp[:3])}" if resp else "câmera não respondeu ao pedido"
            if resp and not any(x == "filefound" for x, _ in resp):
                return "sem_gravacao", fim, 0, quem
            return "falha", a, 0, f"{quem}; go2rtc {r.status}: {limpar(r.read(200).decode(errors='replace')).strip()}"
        try:
            pedaco = r.read1(65536)
        except OSError:
            pedaco = b""
        resp = respostas_cartao(desde)
        achado = next((t for s, t in resp if s == "filefound" and t), None)
        if not pedaco:
            if resp and not achado:  # a câmera respondeu, mas sem arquivo nesse horário
                return "sem_gravacao", fim, 0, f"câmera: {resp[0][0]}"
            return "falha", a, 0, "a câmera não mandou vídeo do cartão" + ("" if resp else " (nem respondeu)")
        inicio = achado or a  # achado > a: sem gravação até ali (o cartão pulou adiante)
        if inicio >= fim:
            return "sem_gravacao", fim, 0, ""
        if inicio >= b:  # a próxima gravação está mais adiante no buraco: aproveita a mesma sessão
            b = min(inicio + TRECHO, fim)
        alvo = b - inicio
        u = urllib.parse.urlparse(cfg["vps"])
        dst = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=60)
        dst.putrequest("POST", f"/api/ponte/cartao?camera={q(cam)}&inicio={inicio:.0f}&duracao={alvo:.0f}",
                       skip_accept_encoding=True)
        for k, v in {"Authorization": f"Bearer {cfg['token']}", "User-Agent": UA,
                     "Content-Type": "video/mp2t", "Transfer-Encoding": "chunked"}.items():
            dst.putheader(k, v)
        dst.endheaders()
        buf, est, seg = bytearray(), {}, 0.0
        limite = time.time() + alvo * 3 + 60  # sinal fraco: manda o que der nesse tempo
        while pedaco:
            buf += pedaco
            seg = duracao_pts(buf, est)
            novos = [t for st, t in respostas_cartao(desde) if st == "filefound" and t]
            # a sessão P2P caiu, o go2rtc reconectou sozinho e a câmera recomeçou do horário pedido (31/10, garagem:
            # trecho com 22 s repetidos). Aparece como um filefound que não avança; o relógio do vídeo não serve
            # para isto: ele também volta ao começo em cada troca de arquivo de 1 min.
            if any(y <= x for x, y in zip(novos, novos[1:])):
                log.info("%s: a câmera recomeçou o cartão do início (reconexão); fecho o trecho em %.0f s", cam, seg)
                break  # este pedaço já é repetido: não vai
            dst.send(f"{len(pedaco):X}\r\n".encode() + pedaco + b"\r\n")
            if seg >= alvo or time.time() > limite:
                break
            try:
                pedaco = r.read1(65536)
            except OSError:  # 45 s sem nada: a câmera parou de mandar
                break
        dst.send(b"0\r\n\r\n")
        rv = dst.getresponse()
        corpo = rv.read(2000)
        if rv.status != 200:
            return "falha", a, seg, f"VPS respondeu {rv.status}: {corpo.decode(errors='replace')[:150]}"
        return "ok", inicio, float(json.loads(corpo).get("segundos") or 0), ""
    finally:
        for conn in (src, dst):
            if conn is not None:
                conn.close()
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}?src={nome}", method="DELETE"), timeout=10)
        except (urllib.error.URLError, OSError):
            pass


def recuperar(cfg: dict):
    """Um trecho de até TRECHO s por vez, do buraco mais antigo, em velocidade 1: a 4 (≈5× mais rápido) a câmera
    manda só ~1 s de cada 3 (buracos de 2 s, visto em 31/10), ruim para segurança. Se falhar, espera cada vez mais
    (até 30 min); 8 falhas seguidas = perdido."""
    while True:
        time.sleep(60)  # no máximo uma sessão P2P de cartão por minuto
        agora = time.time()
        with _cartao_lock:
            # câmera com o ao vivo caído não recebe pedido de cartão (mais uma sessão P2P numa câmera que já não
            # responde), nem a que disse que o cartão não tem nada gravado (Rancho 1 e 2 em 31/10): essa só a cada 12 h
            pend = [x for x in _cartao["buracos"]
                    if x["cam"] in cfg["cameras"] and x["fim"] < agora - 90 and x["proxima"] <= agora
                    and (ESTADO.get(x["cam"]) or {}).get("enviando")
                    and _cartao["cameras"].get(x["cam"], {}).get("sem_cartao_ate", 0) <= agora]
        if not pend:
            continue
        x = min(pend, key=lambda y: y["ini"])
        cam = x["cam"]
        a = max(x["ini"], agora - BURACO_MAX_IDADE)
        b = min(x["fim"], a + TRECHO)
        speed = 1
        try:
            status, inicio, seg, erro = baixar_trecho(cfg, cam, a, b, speed, x["fim"])
        except (OSError, http.client.HTTPException, ValueError) as e:
            status, inicio, seg, erro = "falha", a, 0, limpar(str(e))[:200]
        with _cartao_lock:
            info = _cartao["cameras"].setdefault(cam, {"recuperado_s": 0, "perdido_s": 0})
            info["perdido_s"] += a - x["ini"]  # passou do limite de idade
            x["ini"] = a
            if status == "ok" and seg >= 3:
                log.info("%s: %.0f s recuperados do cartão (%s)", cam, seg, time.strftime("%d/%m %H:%M", time.localtime(inicio)))
                info["recuperado_s"] += max(0, inicio + seg - max(inicio, a))  # sem o pedaço antes do buraco
                info["perdido_s"] += max(0, inicio - a)
                info["ultimo_ok"], info["ultimo_erro"] = time.time(), ""
                x.update(ini=inicio + seg, falhas=0, proxima=0)
            elif status == "sem_gravacao":
                ate = min(max(inicio, a), x["fim"])
                log.info("%s: cartão sem gravação de %s a %s %s", cam, time.strftime("%H:%M", time.localtime(a)),
                         time.strftime("%H:%M", time.localtime(ate)), erro)
                info["perdido_s"] += ate - a
                info["ultimo_erro"] = "sem gravação no cartão nesse horário" + (f" ({erro})" if erro else "")
                x.update(ini=ate, falhas=0, proxima=0)
                if "filenotfound" in erro:
                    info["sem_cartao_ate"] = time.time() + 12 * 3600
            else:
                x["falhas"] += 1
                x["proxima"] = time.time() + min(60 * 2 ** x["falhas"], 1800)
                info["ultimo_erro"] = erro or "falhou"
                log.warning("%s: cartão falhou (%d× seguidas, velocidade %d): %s", cam, x["falhas"], speed, erro)
                if x["falhas"] >= 8:
                    info["perdido_s"] += x["fim"] - x["ini"]
                    x["ini"] = x["fim"]
            if x["ini"] >= x["fim"] - 2:
                _cartao["buracos"].remove(x)
            cartao_salvar()


def resumo_cartao(cfg: dict) -> dict:
    with _cartao_lock:
        saida = {}
        for cam in cfg["cameras"]:
            info = dict(_cartao["cameras"].get(cam, {}))
            bs = [x for x in _cartao["buracos"] if x["cam"] == cam]
            info["pendente_s"] = round(sum(x["fim"] - x["ini"] for x in bs))
            info["buracos"] = len(bs)
            saida[cam] = {k: round(v) if isinstance(v, float) else v for k, v in info.items()}
        return saida


def pedidos_da_vps(cfg: dict, pedidos: list):
    """A VPS também pode pedir um intervalo (ex.: o tempo em que esta ponte ficou desligada)."""
    with _cartao_lock:
        for p in pedidos:
            cam, ini, fim = p.get("camera"), float(p.get("ini", 0)), float(p.get("fim", 0))
            if cam in cfg["cameras"] and 0 < fim - ini and fim > time.time() - BURACO_MAX_IDADE:
                _cartao["buracos"].append({"cam": cam, "ini": ini, "fim": fim, "falhas": 0, "proxima": 0})
                log.info("%s: a VPS pediu o cartão de %s a %s", cam, time.strftime("%d/%m %H:%M", time.localtime(ini)),
                         time.strftime("%H:%M", time.localtime(fim)))
        cartao_salvar()


# ---- atualização automática --------------------------------------------------------------------------
# A VPS diz no sinal de vida qual é a versão publicada. Se esta for outra, a ponte baixa ponte.py e o go2rtc
# (conferindo o SHA256SUMS), guarda os atuais em anterior/, troca e sai com o código 3; o laco.sh a sobe de novo
# na hora. A versão nova se confirma no primeiro sinal de vida com o go2rtc no ar. Se ela cair antes disso (ou não
# se confirmar em 3 min), volta a anterior e essa versão fica recusada até sair outra.
ATUALIZANDO = PASTA / "atualizando"
RECUSADA = PASTA / "versao_recusada"
RESULTADO = PASTA / "atualizacao.json"
CODIGO_REINICIO = 3

LACO = """#!/data/data/com.termux/files/usr/bin/sh
# supervisor da ponte (gerado pela ponte.py): sobe de novo se ela sair; código 3 = atualizou, sobe na hora.
# Se uma atualização cair antes de se confirmar (arquivo "atualizando"), volta os arquivos de anterior/.
cd "$(dirname "$0")"
while true; do
  python ponte.py >> ponte.out 2>&1
  cod=$?
  pkill -f go2rtc_arm 2>/dev/null
  if [ "$cod" -ne 3 ] && [ -f atualizando ]; then
    for f in anterior/*; do
      [ -f "$f" ] && cp -f "$f" "$(basename "$f").tmp" && mv -f "$(basename "$f").tmp" "$(basename "$f")"
    done
    mv -f atualizando atualizacao_falhou
    echo "$(date) atualização caiu (saída $cod): voltei para a versão anterior" >> ponte.out
  fi
  [ "$cod" -eq 3 ] || sleep 5
done
"""

INICIAR = """#!/data/data/com.termux/files/usr/bin/sh
# sobe a ponte se ela não estiver rodando (Termux:Boot, ao abrir o Termux, instalador)
termux-wake-lock 2>/dev/null
pgrep -f "ponte/laco.sh" >/dev/null && exit 0
pkill -f "python ponte.py" 2>/dev/null
pkill -f go2rtc_arm 2>/dev/null
nohup sh "$(dirname "$0")/laco.sh" >/dev/null 2>&1 &
"""


def garantir_scripts():
    """laco.sh e iniciar.sh vêm daqui, para a atualização automática também poder mudá-los. Troca por rename:
    o sh que está rodando o laco.sh continua lendo o arquivo antigo."""
    for nome, txt in (("laco.sh", LACO), ("iniciar.sh", INICIAR)):
        alvo = PASTA / nome
        if not alvo.exists() or alvo.read_text(encoding="utf-8") != txt:
            tmp = PASTA / (nome + ".tmp")
            tmp.write_text(txt, encoding="utf-8")
            os.chmod(tmp, 0o755)
            os.replace(tmp, alvo)


def _sha256(arq: Path) -> str:
    h = hashlib.sha256()
    with open(arq, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _baixar(url: str, destino: Path):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=120) as r, \
            open(destino, "wb") as f:
        shutil.copyfileobj(r, f)


def atualizar(cfg: dict, nova: str, g: "Go2rtc"):
    if RECUSADA.exists() and RECUSADA.read_text().strip() == nova:
        return
    base = cfg["vps"] + "/ponte/"
    novo = PASTA / "novo"
    shutil.rmtree(novo, ignore_errors=True)
    novo.mkdir()
    _baixar(base + "SHA256SUMS", novo / "SHA256SUMS")
    somas = dict(reversed(x.split()) for x in (novo / "SHA256SUMS").read_text().splitlines() if x.strip())
    trocar = []
    for nome in ("ponte.py", binario().name):
        if nome not in somas:
            raise ValueError(f"{nome} fora do SHA256SUMS")
        if (PASTA / nome).exists() and _sha256(PASTA / nome) == somas[nome]:
            continue  # o go2rtc quase nunca muda: não baixa 19 MB à toa
        _baixar(base + nome, novo / nome)
        if _sha256(novo / nome) != somas[nome]:
            raise ValueError(f"sha256 de {nome} não confere")
        trocar.append(nome)
    if not trocar:
        shutil.rmtree(novo, ignore_errors=True)
        return
    anterior = PASTA / "anterior"
    shutil.rmtree(anterior, ignore_errors=True)
    anterior.mkdir()
    for nome in trocar:
        if (PASTA / nome).exists():
            shutil.copy2(PASTA / nome, anterior / nome)
    ATUALIZANDO.write_text(json.dumps({"de": VERSAO, "para": nova, "ts": time.time()}))
    for nome in trocar:  # mv troca mesmo com o go2rtc em uso (sobrescrever dá "text file busy")
        os.chmod(novo / nome, 0o755)
        os.replace(novo / nome, PASTA / nome)
    shutil.rmtree(novo, ignore_errors=True)
    log.info("atualizada de %s para %s (%s); reiniciando", VERSAO, nova, ", ".join(trocar))
    if g.proc and g.proc.poll() is None:
        g.proc.terminate()
        try:
            g.proc.wait(10)
        except subprocess.TimeoutExpired:
            g.proc.kill()
    logging.shutdown()
    os._exit(CODIGO_REINICIO)  # sys.exit numa thread só encerraria a thread


def resultado_atualizacao() -> dict:
    try:
        return json.loads(RESULTADO.read_text())
    except (OSError, ValueError):
        return {}


def conferir_atualizacao(confirmar: bool = False):
    """No início: registra uma volta feita pelo laco.sh. Com confirmar: a versão nova está no ar."""
    falhou = PASTA / "atualizacao_falhou"
    if falhou.exists():
        try:
            a = json.loads(falhou.read_text())
        except ValueError:
            a = {}
        RECUSADA.write_text(str(a.get("para", "")))
        RESULTADO.write_text(json.dumps({**a, "ok": False, "fim": time.time(), "erro": "a versão nova caiu ao iniciar"}))
        falhou.unlink()
        log.warning("a atualização para %s caiu; voltei para %s", a.get("para"), VERSAO)
    if confirmar and ATUALIZANDO.exists():
        try:
            a = json.loads(ATUALIZANDO.read_text())
        except ValueError:
            a = {}
        RESULTADO.write_text(json.dumps({**a, "ok": True, "fim": time.time()}))
        ATUALIZANDO.unlink()
        log.info("versão %s confirmada", VERSAO)


def vigiar_atualizacao(g: "Go2rtc", prazo: int = 180):
    """Versão nova que roda mas não chega a confirmar (go2rtc não sobe, VPS fora): volta a anterior."""
    time.sleep(prazo)
    if not ATUALIZANDO.exists():
        return
    log.warning("versão %s não se confirmou em %d s; voltando para a anterior", VERSAO, prazo)
    if g.proc and g.proc.poll() is None:
        g.proc.terminate()
    os.replace(ATUALIZANDO, PASTA / "atualizacao_falhou")
    for f in (PASTA / "anterior").glob("*"):
        shutil.copy2(f, PASTA / (f.name + ".tmp"))
        os.replace(PASTA / (f.name + ".tmp"), PASTA / f.name)
    logging.shutdown()
    os._exit(CODIGO_REINICIO)


# ---- sinal de vida -----------------------------------------------------------------------------------
def limpar(t: str) -> str:
    """Tira dos textos de erro o que pode ser segredo (query das URLs com chaves, tokens) antes de ir à VPS."""
    t = re.sub(r"\?[^\s\"']*", "?…", t)
    return re.sub(r"(?i)(token|password|sign|ssecurity)([\"':= ]+)[^\s\"',}]+", r"\1\2…", t)


def _cauda(arq: Path, tamanho: int = 65536) -> list[str]:
    try:
        with open(arq, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - tamanho))
            return f.read().decode(errors="replace").splitlines()
    except OSError:
        return []


def log_go2rtc(n: int = 12) -> list[str]:
    """Últimos avisos/erros do go2rtc (WRN/ERR), já limpos."""
    return [limpar(x)[:300] for x in _cauda(PASTA / "go2rtc.log") if " WRN " in x or " ERR " in x or " FTL " in x][-n:]


def log_ponte(n: int = 10) -> list[str]:
    """Últimos avisos da própria ponte (recriou stream, reiniciou go2rtc, cartão...): o painel mostra sem SSH."""
    return [limpar(x)[:300] for x in _cauda(PASTA / "ponte.log") if " WARNING " in x or " ERROR " in x][-n:]


# ---- comandos do painel (Admin > Pontes): chegam na resposta do sinal de vida, o resultado vai no próximo ---------
RESULTADOS = PASTA / "resultados.json"  # guardados em disco: "reiniciar_ponte" responde depois de voltar


def _resultados() -> list:
    try:
        return json.loads(RESULTADOS.read_text())
    except (OSError, ValueError):
        return []


def _guardar_resultado(cid, ok: bool, texto: str):
    lista = _resultados() + [{"id": cid, "ok": ok, "texto": texto[:4000], "fim": time.time()}]
    RESULTADOS.write_text(json.dumps(lista[-20:]))


def executar_comandos(cfg: dict, g: Go2rtc, comandos: list):
    for c in comandos:
        cid, acao, cam = c.get("id"), str(c.get("acao", "")), str(c.get("camera", ""))
        url = cfg["cameras"].get(cam, "")
        log.warning("comando %s do painel: %s %s", cid, acao, cam)
        try:
            if acao == "recriar" and url:
                erro = recriar_stream(cam, url)
                _guardar_resultado(cid, not erro, erro or f"stream {cam} recriado")
            elif acao == "reiniciar_go2rtc":
                g.reiniciar.set()
                _guardar_resultado(cid, True, "go2rtc reiniciando")
            elif acao == "reiniciar_ponte":
                _guardar_resultado(cid, True, "ponte reiniciada (o laco.sh sobe de novo)")
                logging.shutdown()
                os._exit(0)
            elif acao == "rede":
                ip = rede().get("ip", "?")
                linhas = [f"celular: {ip}"]
                for nome, u in cfg["cameras"].items():
                    cip = ip_da_camera(u)
                    linhas.append(f"{nome} ({cip}): {'responde na rede local' if alcance(cip) else 'NÃO responde na rede local'}")
                _guardar_resultado(cid, True, "\n".join(linhas))
            elif acao == "log":
                txt = "\n".join(limpar(x) for x in _cauda(PASTA / "ponte.log", 16384)[-40:])
                txt += "\n--- go2rtc ---\n" + "\n".join(limpar(x) for x in _cauda(PASTA / "go2rtc.log", 16384)[-25:])
                _guardar_resultado(cid, True, txt)
            else:
                _guardar_resultado(cid, False, f"comando desconhecido: {acao} {cam}")
        except Exception as e:  # noqa: BLE001 - um comando ruim não pode derrubar o sinal de vida
            _guardar_resultado(cid, False, limpar(str(e))[:300])


def contas_xiaomi() -> list[str]:
    """IDs das contas que o go2rtc gravou no login (só o número, nunca o token)."""
    m = re.search(r"(?m)^xiaomi:\n((?:[ \t]+.*\n?)+)", YAML.read_text(encoding="utf-8") if YAML.exists() else "")
    return re.findall(r"(?m)^[ \t]+[\"']?(\d+)[\"']?:", m.group(1)) if m else []


def bateria() -> dict:
    """Precisa do app Termux:API + `pkg install termux-api`; sem ele, só não informa a bateria."""
    try:
        r = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=15)
        b = json.loads(r.stdout)
        return {"bateria": b.get("percentage"), "carregando": b.get("plugged") not in (None, "UNPLUGGED"),
                "temperatura": b.get("temperature")}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {}


def sinal_de_vida(cfg: dict, g: Go2rtc):
    while True:
        info = {"versao": VERSAO, "maquina": os.uname().machine, "cameras": {}, **bateria()}
        try:
            info["login_xiaomi"] = "\nxiaomi:" in "\n" + YAML.read_text(encoding="utf-8")
            info["contas_xiaomi"] = contas_xiaomi()
            info["log"] = log_go2rtc()
            info["cartao"] = resumo_cartao(cfg)
            info["cartao_novo"] = _cartao.get("novo", False)
            info["atualizacao"] = resultado_atualizacao()
            info["log_ponte"] = log_ponte()
            info["rede"] = rede()
            info["resultados"] = _resultados()
            ss = streams()
            for cam, url in cfg["cameras"].items():
                s = ss.get(cam) or {}
                info["cameras"][cam] = {"bytes": bytes_recebidos(s), **ESTADO.get(cam, {})}
                if not ESTADO.get(cam, {}).get("enviando"):  # só na falha: diz se a câmera está na rede do celular
                    info["cameras"][cam]["lan"] = alcance(ip_da_camera(url))
            info["go2rtc"] = True
        except (urllib.error.URLError, OSError, ValueError):
            info["go2rtc"] = False
        try:
            r = _req(cfg["vps"], "POST", "/api/ponte/estado", info, cfg["token"])
            if info.get("resultados"):
                RESULTADOS.unlink(missing_ok=True)
            if info.get("go2rtc"):
                conferir_atualizacao(confirmar=True)
            if r.get("comandos"):
                executar_comandos(cfg, g, r["comandos"])
            nova = str(r.get("versao") or "")
            if nova and nova != VERSAO and not ATUALIZANDO.exists():
                try:
                    atualizar(cfg, nova, g)
                except (OSError, ValueError, urllib.error.URLError) as e:
                    log.warning("atualização para %s falhou: %s", nova, e)
                    RESULTADO.write_text(json.dumps({"de": VERSAO, "para": nova, "ok": False, "fim": time.time(),
                                                     "erro": limpar(str(e))[:200]}))
            if r.get("cartao") or _cartao.get("novo"):
                _cartao["novo"] = False
                pedidos_da_vps(cfg, r.get("cartao") or [])
            novas = r.get("cameras") or {}
            if novas and novas != cfg["cameras"]:  # câmeras mudaram no painel/casas.json
                log.info("lista de câmeras mudou: %s", ", ".join(novas))
                cfg["cameras"] = novas
                CONFIG.write_text(json.dumps(cfg, indent=1))
                if escrever_yaml(novas):
                    g.reiniciar.set()
                for cam in novas:
                    if cam not in ESTADO:
                        threading.Thread(target=repassar, args=(cfg, cam), name=f"envio-{cam}", daemon=True).start()
        except urllib.error.HTTPError as e:
            log.warning("sinal de vida: VPS respondeu %s%s", e.code,
                        " (ponte revogada? rode `python ponte.py parear`)" if e.code == 401 else "")
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("sinal de vida: %s", e)
        time.sleep(60)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "parear":
        return parear()
    if len(sys.argv) > 1 and sys.argv[1] == "scripts":  # instalador: gera laco.sh e iniciar.sh
        return garantir_scripts()
    if not CONFIG.exists():
        sys.exit("Ainda não pareado: rode `python ponte.py parear`.")
    cfg = json.loads(CONFIG.read_text())
    escrever_yaml(cfg["cameras"])
    cartao_carregar()
    garantir_scripts()
    conferir_atualizacao()
    g = Go2rtc()
    g.start()
    if ATUALIZANDO.exists():
        threading.Thread(target=vigiar_atualizacao, args=(g,), name="atualizacao", daemon=True).start()
    time.sleep(3)
    for cam in cfg["cameras"]:
        threading.Thread(target=repassar, args=(cfg, cam), name=f"envio-{cam}", daemon=True).start()
    threading.Thread(target=vigia, args=(cfg, g), name="vigia", daemon=True).start()
    threading.Thread(target=sinal_de_vida, args=(cfg, g), name="sinal", daemon=True).start()
    threading.Thread(target=recuperar, args=(cfg,), name="cartao", daemon=True).start()
    log.info("ponte no ar: casa %s, %d câmera(s), VPS %s", cfg["casa"], len(cfg["cameras"]), cfg["vps"])
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
