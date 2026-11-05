"""Agente da casa (PC): câmeras -> detecção de pessoas -> eventos na VPS; robô <-> fila de comandos.

Não precisa de túnel: só conexões de saída HTTPS para a VPS (seu-dominio.exemplo.com).
Config: secrets/agente.json {"vps": "https://...", "token": "...", "cameras": {...}}

Uso: agent\\.venv\\Scripts\\python.exe agent\\agente.py

Na VPS (serviço "vigia", 30/10) o mesmo código analisa as câmeras das casas sem PC (rancho: o vídeo chega pelo
celular-ponte). Lá a config vem de AGENTE_CFG (JSON) + AGENTE_TOKEN e só roda a parte das câmeras.
"""
import http.client
import json
import socket
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "agent"), str(ROOT / "tools")]
import miio  # noqa: E402
import visao  # noqa: E402

from logging.handlers import RotatingFileHandler  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s",
                    handlers=[logging.StreamHandler(),
                              RotatingFileHandler(ROOT / "agent" / "agente.log", maxBytes=2_000_000,
                                                  backupCount=3, encoding="utf-8")])
log = logging.getLogger("agente")
CFG = json.loads(os.environ["AGENTE_CFG"]) if os.environ.get("AGENTE_CFG") else     json.loads((ROOT / "secrets" / "agente.json").read_text(encoding="utf-8"))
VPS, TOKEN = CFG["vps"].rstrip("/"), CFG.get("token") or os.environ.get("AGENTE_TOKEN", "")
CAMERAS = CFG.get("cameras", {"garagem": "rtsp://127.0.0.1:8554/garagem",
                              "cw300": "rtsp://127.0.0.1:8554/cw300"})
INTERVALO = 0.5        # s entre análises por câmera
JANELA_MAX = 8.0       # s procurando rosto depois da 1ª detecção (sai antes se achar um rosto bom)
ROSTO_BOM = 40         # px de altura (já ponderado pela nitidez) para considerar o rosto "bom"
PAUSA = 120          # s sem novo evento na mesma câmera enquanto a pessoa continua lá
SUMIU = 20             # s sem pessoa para encerrar a "visita"
MIN_ALTURA = 0.06      # caixa da pessoa precisa ter >= 6% da altura da imagem
MIN_MOVIMENTO = 0.04   # fração mínima de pixels em movimento dentro da caixa
# Segunda olhada (30/10, 440 eventos da semana): recortar a caixa com margem e redetectar no recorte.
# Pombo/gato/cachorro/quadro corrompido: med 0,11; pessoa real: med 0,88. Abaixo de 0,30 em TODOS os quadros
# e sem rosto, o evento nem vai para a VPS (cortava 70% dos falsos; perdia 5% das pessoas com 1 quadro só).
SEGUNDA_MIN = float(CFG.get("segunda_olhada_min", 0.30))
MOV_QUADRO_MIN = 0.002  # fração do quadro em movimento abaixo da qual nem roda o detector (câmera parada = ~0 CPU)


# ---- HTTP ----------------------------------------------------------------------------------------
def _req(method, path, data=None, headers=None, timeout=30):
    # User-Agent próprio: o "Python-urllib" padrão é barrado pela Cloudflare (erro 1010) se o proxy voltar
    h = {"Authorization": f"Bearer {TOKEN}", "User-Agent": "casa-agente/1.0", **(headers or {})}
    req = urllib.request.Request(VPS + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"null")


def post_json(path, obj, timeout=30):
    return _req("POST", path, json.dumps(obj).encode(), {"Content-Type": "application/json"}, timeout)


def post_multipart(path, campos: dict, arquivos: list, timeout=90):
    """arquivos: [(nome_do_campo, nome_do_arquivo, tipo, bytes)]"""
    b = uuid.uuid4().hex
    partes = [f'--{b}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
              for k, v in campos.items()]
    for campo, nome, tipo, dados in arquivos:
        partes.append(f'--{b}\r\nContent-Disposition: form-data; name="{campo}"; filename="{nome}"\r\n'
                      f"Content-Type: {tipo}\r\n\r\n".encode() + dados + b"\r\n")
    partes.append(f"--{b}--\r\n".encode())
    return _req("POST", path, b"".join(partes), {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout)


def post_evento(camera, ts, caixas, jpgs):
    b = uuid.uuid4().hex
    partes = []
    for nome, val in (("camera", camera), ("ts", str(ts)), ("caixas", json.dumps(caixas))):
        partes.append(f'--{b}\r\nContent-Disposition: form-data; name="{nome}"\r\n\r\n{val}\r\n'.encode())
    for i, j in enumerate(jpgs):
        partes.append(f'--{b}\r\nContent-Disposition: form-data; name="fotos"; filename="f{i}.jpg"\r\n'
                      f"Content-Type: image/jpeg\r\n\r\n".encode() + j + b"\r\n")
    partes.append(f"--{b}--\r\n".encode())
    return _req("POST", "/api/agente/evento", b"".join(partes),
                {"Content-Type": f"multipart/form-data; boundary={b}"}, timeout=90)


# ---- vídeo para a VPS (ao vivo + gravação) --------------------------------------------------------
FALHA: dict[str, float] = {}  # câmera -> desde quando todo pedido ao go2rtc falha logo no início (vigia_go2rtc)


def repassar(cam: str):
    """go2rtc local (GET MPEG-TS) -> VPS (POST chunked em HTTPS, token do agente). Só vídeo H.265:
    o MPEG-TS do go2rtc carrega áudio apenas em AAC e as câmeras mandam Opus/PCMA."""
    u = urllib.parse.urlparse(VPS)
    espera = 5
    while True:
        enviado, inicio, src, dst = 0, time.time(), None, None
        try:
            # 60 s: com o go2rtc de 03/11 a câmera que não responde na rede local é tentada pela internet (CS2)
            src = http.client.HTTPConnection("127.0.0.1", 1984, timeout=60)
            src.request("GET", f"/api/stream.ts?src={cam}")
            r = src.getresponse()
            if r.status != 200:
                raise OSError(f"go2rtc local respondeu {r.status}")
            dst = http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=30)
            dst.putrequest("POST", f"/ingest/api/stream.ts?dst={cam}", skip_accept_encoding=True)
            for k, v in {"Authorization": f"Bearer {TOKEN}", "User-Agent": "casa-agente/1.0",
                         "Content-Type": "video/mp2t", "Transfer-Encoding": "chunked"}.items():
                dst.putheader(k, v)
            dst.endheaders()
            log.info("%s: enviando vídeo para a VPS", cam)
            FALHA.pop(cam, None)
            while True:
                b = r.read1(65536)
                if not b:
                    raise OSError("go2rtc local encerrou o fluxo")
                dst.send(f"{len(b):X}\r\n".encode() + b + b"\r\n")
                enviado += len(b)
        except (OSError, http.client.HTTPException) as e:
            dur = time.time() - inicio
            if dur < 60:
                FALHA.setdefault(cam, inicio)
            log.warning("%s: envio de vídeo parou após %.0f s / %.1f MB: %s", cam, dur, enviado / 1e6, e)
            espera = 5 if dur > 60 else min(espera * 2, 120)
            time.sleep(espera)
        finally:
            for conn in (src, dst):
                if conn is not None:
                    conn.close()


# ---- câmeras -------------------------------------------------------------------------------------
class Leitor(threading.Thread):
    """Lê o RTSP do go2rtc sem parar e guarda só o último quadro (decodificar é obrigatório no H.265)."""

    def __init__(self, nome, url):
        super().__init__(name=f"rtsp-{nome}", daemon=True)
        self.nome, self.url, self.quadro, self.t = nome, url, None, 0.0
        self.lock = threading.Lock()

    def run(self):
        while True:
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG)
            if not cap.isOpened():
                log.warning("%s: não abriu o RTSP, tentando de novo em 10 s", self.nome)
                time.sleep(10)
                continue
            log.info("%s: conectado", self.nome)
            while True:
                ok, fr = cap.read()
                if not ok:
                    log.warning("%s: fluxo caiu, reconectando", self.nome)
                    break
                with self.lock:
                    self.quadro, self.t = fr, time.time()
            cap.release()
            time.sleep(3)

    def ultimo(self):
        with self.lock:
            return (None, 0.0) if self.quadro is None else (self.quadro.copy(), self.t)


class Movimento:
    """Subtração de fundo (MOG2) numa cópia reduzida do quadro. Objeto parado vira fundo em ~1 min,
    então uma caixa de "pessoa" sem pixels em movimento é descartada (cadeira, roupa pendurada...)."""

    def __init__(self, largura=480):
        self.largura, self.mog = largura, cv2.createBackgroundSubtractorMOG2(history=120, varThreshold=32,
                                                                              detectShadows=False)
        self.mascara, self.escala = None, 1.0

    def atualizar(self, fr):
        self.escala = self.largura / fr.shape[1]
        peq = cv2.resize(fr, (self.largura, int(fr.shape[0] * self.escala)))
        self.mascara = cv2.morphologyEx(self.mog.apply(peq), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    def fracao(self, caixa) -> float:
        x, y, w, h = (int(v * self.escala) for v in caixa[:4])
        roi = self.mascara[max(0, y):y + max(1, h), max(0, x):x + max(1, w)]
        return float((roi > 0).mean()) if roi.size else 0.0


def segunda_olhada(det2: visao.DetectorPessoas, fr, ps) -> float:
    """Maior confiança de "pessoa" redetectando no recorte ampliado de cada caixa."""
    melhor = 0.0
    for x, y, w, h, *_ in ps:
        m = 0.35
        x0, y0 = max(0, int(x - w * m)), max(0, int(y - h * m))
        x1, y1 = min(fr.shape[1], int(x + w * (1 + m))), min(fr.shape[0], int(y + h * (1 + m)))
        crop = fr[y0:y1, x0:x1]
        if crop.size:
            melhor = max(melhor, max((p[4] for p in det2.detectar(crop)), default=0.0))
    return melhor


def vigiar(nome, leitor: Leitor, det: visao.DetectorPessoas, det_lock: threading.Lock, rostos: visao.Rostos,
           det2: visao.DetectorPessoas):
    ultimo_evento = visto = 0.0
    mov = Movimento()

    def pessoas_em(fr, forcar=False):
        mov.atualizar(fr)
        # quadro parado (< 0,2 % dos pixels mexendo): nem chama o YOLOX — na VPS 2 câmeras × 2 Hz saturavam as 2 CPUs
        if not forcar and float((mov.mascara > 0).mean()) < MOV_QUADRO_MIN:
            return []
        with det_lock:
            ps = [p for p in det.detectar(fr) if p[3] >= MIN_ALTURA * fr.shape[0]]
        return [p for p in ps if mov.fracao(p) >= MIN_MOVIMENTO]

    def avaliar(fr, ps):
        """(tamanho do melhor rosto em px, confiança da pessoa, quadro, caixas) — rosto borrado conta menos.
        O Rostos tem a própria trava: procurar rosto numa câmera não segura a detecção da outra."""
        faces = rostos.em_pessoas(fr, ps)
        melhor = 0.0
        for f in faces:
            nitidez = cv2.Laplacian(cv2.cvtColor(f["recorte"], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
            melhor = max(melhor, f["caixa"][3] * min(1.0, nitidez / 60.0))
        return (melhor, max(p[4] for p in ps), fr, ps)

    inicio = time.time()
    while True:
        time.sleep(INTERVALO)
        fr, t = leitor.ultimo()
        if fr is None or time.time() - t > 5:
            continue
        pessoas = pessoas_em(fr)
        agora = time.time()
        if agora - inicio < 40:  # o fundo do MOG2 ainda está sendo aprendido
            continue
        if not pessoas:
            continue
        nova_visita = agora - visto > SUMIU
        visto = agora
        if not nova_visita and agora - ultimo_evento < PAUSA:
            continue
        # coleta por até JANELA_MAX s procurando ROSTO (cabeça ampliada); prefere rosto grande e nítido.
        # Com um rosto bom (≥ ROSTO_BOM px) já envia; sem rosto, envia os quadros de pessoa mais confiantes.
        amostras = [avaliar(fr, pessoas)]
        fim = agora + JANELA_MAX
        while time.time() < fim:
            if sum(1 for a in amostras if a[0] >= ROSTO_BOM) >= 1 and len(amostras) >= 2:
                break
            time.sleep(0.5)
            f2, _ = leitor.ultimo()
            p2 = pessoas_em(f2, forcar=True)
            if p2:
                amostras.append(avaliar(f2, p2))
        amostras = sorted(amostras, key=lambda a: (-a[0], -a[1]))[:3]
        log.info("%s: %d quadro(s), melhor rosto %d px", nome, len(amostras), int(amostras[0][0]))
        if amostras[0][0] <= 0:  # sem rosto: confirma que é gente antes de gastar VPS/IA
            with det_lock:
                seg = max(segunda_olhada(det2, a[2], a[3]) for a in amostras)
            if seg < SEGUNDA_MIN:
                log.info("%s: descartado na segunda olhada (%.2f < %.2f): bicho/objeto/quadro ruim", nome, seg, SEGUNDA_MIN)
                ultimo_evento = agora
                continue
        # com rosto, JPEG mais fiel: a VPS recorta o rosto desta imagem e 85 já borra um rosto de 20 px
        qualidade = 92 if amostras[0][0] > 0 else 85
        amostras = [(a[1], a[2], a[3]) for a in amostras]
        jpgs = [cv2.imencode(".jpg", a[1], [cv2.IMWRITE_JPEG_QUALITY, qualidade])[1].tobytes() for a in amostras]
        caixas = [[list(p) for p in a[2]] for a in amostras]
        ultimo_evento = agora
        try:
            r = post_evento(nome, agora, caixas, jpgs)
            log.info("%s: evento %s -> %s %s", nome, r.get("id"), r.get("tipo"), r.get("conhecidos") or "")
        except (urllib.error.URLError, OSError) as e:
            log.warning("%s: falha ao enviar evento: %s", nome, e)


# ---- aparelhos (robô e ajustes das câmeras) pela LAN ------------------------------------------------
ACOES_LEGADAS = {  # botões antigos do painel (spec xiaomi.vacuum.c101)
    "limpar": (2, 1, []), "parar": (2, 2, []), "base": (3, 1, []),
    "lavar_mop": (7, 11, [1]), "secar_mop": (7, 11, [2]),
}


class Aparelho:
    """Um aparelho do catálogo: lê as propriedades legíveis e executa set/ação, refazendo a sessão miIO se cair."""

    def __init__(self, alvo: str, cat: dict, info: dict):
        self.alvo, self.cat, self.info = alvo, cat, info
        self.lock = threading.Lock()
        self.pedir_mapa = threading.Event()
        self.ultimo_status = None
        self.dev = self._novo()
        self.legiveis = [tuple(int(x) for x in p["id"].split("."))
                         for g in cat["grupos"] for p in g.get("props", []) if not p.get("so_grava")]

    def _novo(self):
        return miio.Device(self.info["localip"], self.info["token"], timeout=4)

    def chamar(self, fn):
        with self.lock:
            try:
                return fn(self.dev)
            except (OSError, RuntimeError, ValueError):
                self.dev = self._novo()  # refaz o hello (stamp/sessão)
                return fn(self.dev)

    def ler(self) -> dict:
        out = {}

        def guardar(resp):
            time.sleep(float(self.cat.get("pausa", 0)))  # Garagem engasga com pedidos seguidos
            for r in resp:
                if r.get("code") == 0:
                    out[f"{r['siid']}.{r['piid']}"] = r.get("value")

        # lotes de até 4 do MESMO serviço: o firmware da Garagem descarta itens (ou responde
        # "user ack invalid") quando um pedido mistura serviços diferentes
        por_servico: dict[int, list] = {}
        for par in self.legiveis:
            por_servico.setdefault(par[0], []).append(par)
        n = int(self.cat.get("lote", 4))  # Garagem: 1 (o firmware falha com lotes)
        for pares in por_servico.values():
            for i in range(0, len(pares), n):
                lote = pares[i:i + n]
                try:
                    guardar(self.chamar(lambda d: d.get_props(lote)))
                except (OSError, RuntimeError, ValueError):
                    pass
                for par in lote if n > 1 else ():  # o que faltou vai um a um
                    if f"{par[0]}.{par[1]}" not in out:
                        try:
                            guardar(self.chamar(lambda d: d.get_props([par])))
                        except (OSError, RuntimeError, ValueError):
                            pass
        return out

    def executar(self, acao: str, args) -> dict:
        if acao == "set":
            return self.chamar(lambda d: d.set_props([(args["siid"], args["piid"], args["valor"])]))
        if acao == "acao":
            return self.chamar(lambda d: d.action(args["siid"], args["aiid"], args.get("args", [])))
        if acao == "ptz":  # go2rtc com patch: POST /api/xiaomi/ptz (MISS_CMD_MOTOR_REQ pela sessão de vídeo)
            op, passo = int(args["op"]), float(args.get("passo", 0.6))

            def motor(o):
                req = urllib.request.Request(f"http://127.0.0.1:1984/api/xiaomi/ptz?src={self.alvo}&op={o}",
                                             method="POST")
                with urllib.request.urlopen(req, timeout=10) as r:
                    return json.loads(r.read())

            res = motor(op)
            if op in (1, 2, 3, 4) and passo > 0:  # um "passo": anda e para
                time.sleep(min(passo, 3.0))
                motor(-1001)
            return {"result": {"code": 0 if res.get("ok") else -1}, "go2rtc": res}
        if acao == "comodos":
            # c101 (testado em 22/10): set-room-clean (7.3) com ["ids do mapa em CSV", 0, 1] só GUARDA a seleção;
            # a limpeza começa com start-sweep (2.1), no modo atual (2.4) — o mesmo que o app Mi Home faz.
            # Os ids precisam ir como TEXTO: número vira "casa inteira". 2.7 quer ids do app Mi Home: não usar.
            # Secando o mop (estado 10), o robô às vezes não confirma a 7.3 a tempo ("user ack timeout"):
            # tenta até 3x e NUNCA dá a partida sem a seleção confirmada (senão limpa a casa toda).
            ids = ",".join(str(int(i)) for i in args)
            res = {}
            for tentativa in range(3):
                res = self.chamar(lambda d: d.action(7, 3, [ids, 0, 1]))
                if (res.get("result") or {}).get("code") == 0:
                    break
                log.warning("robô: 7.3 não confirmada (%s), tentativa %d", res.get("error") or res, tentativa + 1)
                time.sleep(3)
            else:
                return res
            time.sleep(1)
            return self.chamar(lambda d: d.action(2, 1, []))
        if acao == "atualizar_mapa":
            self.pedir_mapa.set()
            return {"result": {"code": 0}, "nota": "mapa pedido"}
        if acao in ACOES_LEGADAS:
            siid, aiid, a = ACOES_LEGADAS[acao]
            return self.chamar(lambda d: d.action(siid, aiid, a))
        raise ValueError(f"ação desconhecida {acao}")


def resumo_robo(v: dict) -> dict:
    return {"status": v.get("2.1"), "erro": v.get("2.2"), "bateria": v.get("3.1"), "modo": v.get("2.4"),
            "mop": v.get("7.4")}


def publicar_estado(ap: Aparelho):
    v = ap.ler()
    carga = {f"disp:{ap.alvo}": v}
    ap.ultimo_status = v.get("2.1", ap.ultimo_status)
    if ap.alvo == "robo":
        carga["robo"] = resumo_robo(v)  # usado pelos avisos de fim de limpeza
    post_json("/api/agente/estado", carga)


LIMPANDO = {3, 5, 6, 7}  # voltando à base, aspirando, aspirando+pano, pano


def mapa_loop(ap: "Aparelho"):
    """Mapa pela nuvem cn (passToken do PC): a cada 2 min limpando, 30 min parado ou quando o painel pede."""
    try:
        import mapa
    except ImportError as e:
        log.warning("mapa desligado (dependência ausente: %s)", e)
        return
    m, ultimo = mapa.Mapa(ap.info), 0.0
    while True:
        intervalo = 120 if ap.ultimo_status in LIMPANDO else 1800
        pedido = ap.pedir_mapa.wait(timeout=max(5.0, intervalo - (time.time() - ultimo)))
        ap.pedir_mapa.clear()
        try:
            out = ap.chamar(lambda d: m.baixar(d, pedir_envio=True))
            post_multipart("/api/agente/mapa", {"comodos": json.dumps(out["comodos"], ensure_ascii=False),
                                                "tamanho": json.dumps(out["tamanho"])},
                           [("imagem", "robo.png", "image/png", out["png"])])
            log.info("mapa enviado (%s cômodos)%s", len(out["comodos"]), " a pedido" if pedido else "")
        except Exception as e:  # noqa: BLE001
            log.warning("mapa: %s", e)
        ultimo = time.time()


def aparelhos_loop():
    while True:
        try:
            cat = _req("GET", "/api/agente/catalogo")
            break
        except (urllib.error.URLError, OSError) as e:
            log.warning("catálogo: %s", e)
            time.sleep(15)
    devs = {d.get("model"): d for d in miio.load_devices()}
    aps = {alvo: Aparelho(alvo, c, devs[c["modelo"]]) for alvo, c in cat.items() if c.get("modelo") in devs}
    log.info("aparelhos: %s", ", ".join(aps))

    def reportar(ap: Aparelho, intervalo: int):
        while True:
            try:
                publicar_estado(ap)
            except Exception as e:
                log.warning("estado de %s: %s", ap.alvo, e)
            time.sleep(intervalo)

    if "robo" in aps:
        threading.Thread(target=mapa_loop, args=(aps["robo"],), name="mapa", daemon=True).start()
    for alvo, ap in aps.items():
        threading.Thread(target=reportar, args=(ap, 30 if alvo == "robo" else 120),
                         name=f"estado-{alvo}", daemon=True).start()
    while True:
        try:
            cmds = _req("GET", "/api/agente/comandos?espera=25", timeout=60) or []
        except (urllib.error.URLError, OSError) as e:
            log.warning("fila de comandos: %s", e)
            time.sleep(10)
            continue
        for c in cmds:
            ap = aps.get(c.get("alvo") or "robo")
            try:
                if ap is None:
                    raise ValueError(f"aparelho desconhecido {c.get('alvo')}")
                res = ap.executar(c["acao"], json.loads(c.get("args") or "null"))
                r = res.get("result")
                ok = (r.get("code") == 0) if isinstance(r, dict) else                     all(x.get("code") == 0 for x in r) if isinstance(r, list) else False
                status = "feito" if ok else "erro"
                log.info("%s: %s %s -> %s", c.get("alvo"), c["acao"], c.get("args"), res)
            except Exception as e:
                res, status = {"erro": str(e)}, "erro"
            try:
                post_json(f"/api/agente/comandos/{c['id']}", {"status": status, "resultado": res})
                if ap is not None:
                    publicar_estado(ap)
            except Exception as e:
                log.warning("resultado do comando: %s", e)


# ---- vigia do go2rtc: sessão P2P "zumbi" (conectada, mas sem vídeo) --------------------------------------
def post_alerta(titulo: str, msg: str, prioridade: int = 3):
    try:
        post_json("/api/agente/alerta", {"titulo": titulo, "msg": msg, "prioridade": prioridade})
    except Exception as e:  # noqa: BLE001
        log.warning("alerta: %s", e)


RECRIAR_S = 300          # câmera sem conectar há tanto tempo: recria o stream dela no go2rtc (a cada 5 min)
REINICIAR_CAM_S = 1800   # ... e há tanto: reinicia a câmera, se o modelo tiver a ação (no máximo 1×/6 h)
# ação MIoT de reiniciar por modelo (spec). A EC3 Pro e a Garagem (025b02) não têm; o miIO.reboot não reinicia nelas.
ACAO_REINICIAR = {"mxiang.camera.moc001": (2, 2)}  # CW300: camera-control / restart-device (NÃO 3.1 = formatar)


def reiniciar_camera(url: str) -> bool:
    did = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("did", [""])[0]
    if not CFG.get("reiniciar_cameras", True):
        return False
    d = next((x for x in miio.load_devices() if str(x.get("did")) == did), None)
    acao = ACAO_REINICIAR.get((d or {}).get("model", ""))
    if not d or not acao:
        return False
    try:
        r = miio.Device(d["localip"], d["token"], timeout=5).action(*acao)
        log.warning("%s: reiniciada pela ação %d.%d -> %s", d.get("name"), *acao, r.get("result"))
        return True
    except (OSError, RuntimeError, ValueError) as e:
        log.warning("%s: não consegui reiniciar: %s", d.get("name"), e)
        return False


def vigia_go2rtc(parado_s: int = 60):
    """Visto em 22/10 na Garagem: o produtor xiaomi/miss continuava "conectado" com os bytes parados e o go2rtc
    não reconectava. Se os bytes de uma câmera não mudam por `parado_s`, recria só aquele stream pela API."""
    import re

    base = "http://127.0.0.1:1984/api/streams"
    # com o produtor ativo a API não mostra a URL de origem: lê do go2rtc.yaml (seção streams)
    yaml = (ROOT / "bin" / "go2rtc" / "go2rtc.yaml").read_text(encoding="utf-8")
    # aceita "  cam: url" e a forma em lista que o próprio go2rtc grava após um PUT ("  cam:\n    - url")
    origens = {m[0]: m[1] or m[2] for m in
               re.findall(r"(?m)^  ([A-Za-z0-9_-]+):[ ]*(xiaomi://\S+)?[ ]*$(?:\n    - (xiaomi://\S+))?", yaml)
               if m[1] or m[2]}
    ultimo: dict[str, tuple[int, float]] = {}
    tentativas: dict[str, int] = {}
    # câmera oscilando (cai e volta a cada poucos minutos) não pode virar uma enxurrada de avisos: "voltou" só
    # depois de ESTAVEL s com vídeo sem parar, e "sem vídeo" no máximo uma vez por REAVISO s por câmera
    ESTAVEL, REAVISO = 600, 3600
    avisado: dict[str, bool] = {}
    ok_desde: dict[str, float] = {}
    ultimo_aviso: dict[str, float] = {}
    recriado: dict[str, float] = {}
    reiniciado: dict[str, float] = {}
    q = urllib.parse.quote

    def recriar(cam, url):
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}?src={q(cam)}", method="DELETE"), timeout=10)
            urllib.request.urlopen(urllib.request.Request(f"{base}?name={q(cam)}&src={q(url, safe='')}",
                                                          method="PUT"), timeout=10)
        except (urllib.error.URLError, OSError) as e:
            log.warning("%s: não consegui reabrir: %s", cam, e)

    while True:
        time.sleep(20)
        try:
            with urllib.request.urlopen(base, timeout=10) as r:
                streams = json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            continue
        agora = time.time()
        for cam, desde in list(FALHA.items()):
            # todo pedido falha logo no início (ex.: CW300 em 02–03/11, "cs2: EOF" por 24 h): recria o stream a cada
            # RECRIAR_S; depois de REINICIAR_CAM_S reinicia a própria câmera (se o modelo tem a ação), 1×/6 h
            url = origens.get(cam)
            if not url or agora - desde < RECRIAR_S:
                continue
            if agora - desde > REINICIAR_CAM_S and agora - reiniciado.get(cam, 0) > 6 * 3600 and reiniciar_camera(url):
                reiniciado[cam] = agora
                FALHA[cam] = agora  # dá tempo de a câmera subir antes da próxima escalada
                post_alerta(f"Câmera {cam} reiniciada",
                            f"Sem vídeo há {int((agora - desde) / 60)} min mesmo recriando a sessão: reiniciei a câmera.", 3)
            elif agora - recriado.get(cam, 0) > RECRIAR_S:
                log.warning("%s: sem conectar há %d s; recriando o stream", cam, agora - desde)
                recriado[cam] = agora
                recriar(cam, url)
        for cam in CAMERAS:
            s = streams.get(cam) or {}
            prods = s.get("producers") or []
            total = sum(rc.get("bytes", 0) for p in prods for rc in (p.get("receivers") or []))
            url = origens.get(cam)
            b, desde = ultimo.get(cam, (-1, agora))
            if total != b:
                ok_desde.setdefault(cam, agora)
                if avisado.get(cam) and agora - ok_desde[cam] >= ESTAVEL:
                    post_alerta(f"Câmera {cam} voltou", "O vídeo voltou a chegar e está estável há 10 min.", 2)
                    avisado[cam] = False
                tentativas[cam] = 0
                ultimo[cam] = (total, agora)
                continue
            ok_desde.pop(cam, None)
            if agora - desde > parado_s and s.get("consumers") and url:
                log.warning("%s: vídeo parado há %d s no go2rtc; reabrindo a sessão da câmera", cam, agora - desde)
                recriar(cam, url)
                tentativas[cam] = tentativas.get(cam, 0) + 1
                if tentativas[cam] >= 3 and not avisado.get(cam) and agora - ultimo_aviso.get(cam, 0) > REAVISO:
                    avisado[cam], ultimo_aviso[cam] = True, agora  # sem gravação e sem detecção enquanto durar
                    post_alerta(f"Câmera {cam} sem vídeo",
                                "3 tentativas de reconexão falharam: sem gravação e sem detecção nela. "
                                "Confira energia/Wi-Fi da câmera.", 4)
                ultimo[cam] = (-1, agora)


# ---- presença: quais aparelhos estão na rede de casa (para saber se tem alguém em casa) -------------
def _varrer_rede(prefixo: str):
    """Ping rápido em toda a /24 para popular a tabela ARP (celular em repouso nem sempre responde sozinho)."""
    import concurrent.futures
    import subprocess

    def ping(i):
        subprocess.run(["ping", "-n", "1", "-w", "300", f"{prefixo}.{i}"], capture_output=True,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    with concurrent.futures.ThreadPoolExecutor(64) as ex:
        list(ex.map(ping, range(1, 255)))


def _ler_arp(prefixo: str) -> dict:
    import re
    import subprocess

    out = subprocess.run(["arp", "-a"], capture_output=True, text=True, encoding="latin-1",
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    vistos = {}
    for ip, mac in re.findall(r"(\d+\.\d+\.\d+\.\d+)\s+([0-9a-f]{2}(?:-[0-9a-f]{2}){5})", out, re.I):
        if ip.startswith(prefixo + ".") and not ip.endswith(".255") and mac.lower() != "ff-ff-ff-ff-ff-ff":
            vistos[mac.lower().replace("-", ":")] = ip
    return vistos


def presenca_loop():
    prefixo = CFG.get("rede", "192.168.0")
    # aparelhos Xiaomi já conhecidos aparecem com o nome do app (ajuda a achar os celulares na lista)
    nomes: dict[str, str] = {d["mac"].lower(): f"{d.get('name')} (Xiaomi)" for d in miio.load_devices() if d.get("mac")}
    ultima_varredura = 0.0
    while True:
        try:
            if time.time() - ultima_varredura > 300:
                _varrer_rede(prefixo)
                ultima_varredura = time.time()
            vistos = _ler_arp(prefixo)
            for mac, ip in vistos.items():
                if mac not in nomes:  # nome reverso pode levar segundos: busca em segundo plano
                    nomes[mac] = ""

                    def resolver(m=mac, i=ip):
                        try:
                            nomes[m] = socket.gethostbyaddr(i)[0]
                        except OSError:
                            pass

                    threading.Thread(target=resolver, daemon=True).start()
            post_json("/api/agente/presenca", {"dispositivos": [{"mac": m, "ip": ip, "nome": nomes.get(m, "")}
                                                                for m, ip in vistos.items()]})
        except Exception as e:  # noqa: BLE001
            log.warning("presença: %s", e)
        time.sleep(60)


def cameras_da_vps(det, det_lock, rostos, det2):
    """Vigia da VPS: analisa as câmeras que chegam por ponte, conforme o Admin (câmera nova entra em até 1 min)."""
    base = CFG.get("rtsp_base", "rtsp://go2rtc:8554")
    vigiando: set[str] = set()
    while True:
        try:
            for nome in _req("GET", "/api/agente/cameras", timeout=15).get("vigia", []):
                if nome not in vigiando:
                    vigiando.add(nome)
                    log.info("%s: começando a vigiar", nome)
                    leitor = Leitor(nome, f"{base}/{nome}")
                    leitor.start()
                    threading.Thread(target=vigiar, args=(nome, leitor, det, det_lock, rostos, det2),
                                     name=f"vigia-{nome}", daemon=True).start()
        except (urllib.error.URLError, OSError, ValueError) as e:
            log.warning("lista de câmeras: %s", e)
        time.sleep(60)


def publicar_aparelhos_xiaomi():
    """Lista dos aparelhos da conta (sem token) para o Admin escolher câmeras das pontes. A cada 6 h, renovando
    antes pela nuvem (passToken do PC): a VPS troca o IP do endereço da ponte quando a câmera muda de IP."""
    campos = ("name", "model", "did", "localip", "uid", "ssid", "isOnline", "mac", "rssi")
    while True:
        try:
            try:
                import mi_tokens  # noqa: PLC0415 - só no PC (tem secrets/session.json)
                lista = mi_tokens.atualizar_devices()
            except (Exception, SystemExit) as e:  # noqa: BLE001 - passToken vencido ou sem internet: usa a lista gravada
                log.warning("lista de aparelhos: não renovei pela nuvem (%s); mando a gravada", e)
                lista = miio.load_devices()
            post_json("/api/agente/estado", {"xiaomi_aparelhos": [{k: d.get(k) for k in campos} for d in lista]})
        except Exception as e:  # noqa: BLE001
            log.warning("lista de aparelhos: %s", e)
        time.sleep(6 * 3600)


def main():
    # no container da VPS o OpenCV vê os 4 vCPU do host mas o cgroup dá 2: threads demais = só espera
    if CFG.get("threads"):
        cv2.setNumThreads(int(CFG["threads"]))
    det, det_lock = visao.DetectorPessoas(), threading.Lock()
    # na cabeça ampliada aceita perfil um pouco pior; no PC só detectamos (a VPS reconhece com o ArcFace)
    rostos = visao.Rostos(conf=0.75, arcface=False)
    det2 = visao.DetectorPessoas(conf=0.05)  # segunda olhada: devolve tudo; o limiar é SEGUNDA_MIN
    if CFG.get("cameras_da_vps"):
        threading.Thread(target=cameras_da_vps, args=(det, det_lock, rostos, det2), name="cameras", daemon=True).start()
    for nome, url in ({} if CFG.get("cameras_da_vps") else CAMERAS).items():
        leitor = Leitor(nome, url)
        leitor.start()
        threading.Thread(target=vigiar, args=(nome, leitor, det, det_lock, rostos, det2), name=f"vigia-{nome}",
                         daemon=True).start()
        if CFG.get("enviar_video", True):
            threading.Thread(target=repassar, args=(nome,), name=f"envio-{nome}", daemon=True).start()
    if CFG.get("aparelhos", True):  # robô, ajustes das câmeras, presença e go2rtc local: só no PC de casa
        threading.Thread(target=aparelhos_loop, name="aparelhos", daemon=True).start()
        threading.Thread(target=presenca_loop, name="presenca", daemon=True).start()
        threading.Thread(target=vigia_go2rtc, name="vigia-go2rtc", daemon=True).start()
        threading.Thread(target=publicar_aparelhos_xiaomi, name="xiaomi", daemon=True).start()
    log.info("agente no ar: %s câmera(s), VPS %s", len(CAMERAS), VPS)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
