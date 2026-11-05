"""Gravação contínua na VPS: um ffmpeg por câmera lê o RTSP interno do go2rtc e grava trechos de
5 min em MP4 (cópia do H.265, sem recodificar). A limpeza mantém RETENCAO_GRAVACAO_DIAS e um
mínimo de MIN_LIVRE_GB de disco livre (apaga os trechos mais antigos primeiro).
"""
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(threadName)s: %(message)s")
log = logging.getLogger("gravador")

REC = Path(os.environ.get("REC", "/rec"))
CAMERAS = os.environ.get("CAMERAS", "garagem,cw300").split(",")  # só até o app responder
APP = os.environ.get("APP_URL", "http://app:8000")
TOKEN = os.environ.get("AGENTE_TOKEN", "")
RTSP = os.environ.get("RTSP_BASE", "rtsp://go2rtc:8554")
SEGMENTO = int(os.environ.get("SEGMENTO_S", "300"))
DIAS = float(os.environ.get("RETENCAO_GRAVACAO_DIAS", "7"))
MIN_LIVRE = float(os.environ.get("MIN_LIVRE_GB", "15")) * 1e9


def gravar(cam: str):
    pasta = REC / cam
    pasta.mkdir(parents=True, exist_ok=True)
    while True:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp",
               "-i", f"{RTSP}/{cam}", "-map", "0:v", "-c", "copy",
               "-f", "segment", "-segment_time", str(SEGMENTO), "-segment_atclocktime", "1",
               "-reset_timestamps", "1", "-strftime", "1", "-segment_format", "mp4",
               "-segment_format_options", "movflags=+faststart",
               str(pasta / "%Y-%m-%d_%H-%M-%S.mp4")]
        inicio = time.time()
        # stderr num arquivo temporário, não em PIPE: o ffmpeg roda por dias e os avisos iam se acumulando na RAM
        with tempfile.TemporaryFile() as err:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=err)
            err.seek(0, os.SEEK_END)
            err.seek(max(0, err.tell() - 300))
            cauda = err.read().decode(errors="replace").strip()
        dur = time.time() - inicio
        if dur > 30:
            log.warning("%s: ffmpeg saiu (%s) após %.0f s: %s", cam, r.returncode, dur, cauda)
        time.sleep(5 if dur > 30 else 20)  # sem fluxo (PC desligado): tenta com calma


def limpar_passo():
    arquivos = sorted(REC.glob("*/*.mp4"), key=lambda p: p.name)
    limite = time.time() - DIAS * 86400
    for p in arquivos:
        if p.stat().st_mtime < limite:
            p.unlink(missing_ok=True)
    arquivos = sorted(REC.glob("*/*.mp4"), key=lambda p: p.stat().st_mtime)
    while arquivos and shutil.disk_usage(REC).free < MIN_LIVRE:
        p = arquivos.pop(0)
        log.info("disco cheio: apagando %s", p.name)
        p.unlink(missing_ok=True)
    # trechos vazios (fluxo caiu logo no começo) não servem para nada
    for p in REC.glob("*/*.mp4"):
        if p.stat().st_size < 4096 and time.time() - p.stat().st_mtime > SEGMENTO * 2:
            p.unlink(missing_ok=True)


def limpar():
    while True:
        try:
            limpar_passo()
        except OSError as e:  # arquivo sumiu entre o glob e o stat (ffmpeg trocando de trecho): não mata a thread
            log.warning("limpeza falhou nesta rodada: %s", e)
        time.sleep(300)


def cameras_do_app() -> list[str] | None:
    """A lista vem do Admin (casas e câmeras): câmera nova começa a gravar sozinha em até 1 min."""
    try:
        req = urllib.request.Request(APP + "/api/agente/cameras", headers={"Authorization": f"Bearer {TOKEN}"})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())["todas"]
    except (OSError, ValueError, KeyError):
        return None


if __name__ == "__main__":
    threading.Thread(target=limpar, name="limpeza", daemon=True).start()
    log.info("gravando em %s (retenção %s dias, mínimo livre %.0f GB)", REC, DIAS, MIN_LIVRE / 1e9)
    gravando: set[str] = set()
    while True:  # câmera removida no Admin: o ffmpeg dela só fica tentando (sem fluxo), até o próximo reinício
        for c in cameras_do_app() or ([] if gravando else CAMERAS):
            if c not in gravando:
                gravando.add(c)
                log.info("gravando %s", c)
                threading.Thread(target=gravar, args=(c,), name=f"rec-{c}", daemon=True).start()
        time.sleep(60)
