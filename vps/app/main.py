"""Servidor da casa (VPS): recebe eventos do agente do PC, reconhece rostos, chama a Claude,
manda avisos pelo ntfy e serve o painel. O robô recebe comandos por uma fila que o agente busca.

Rotas /api/agente/* : Bearer AGENTE_TOKEN · /api/acao/*: ACAO_TOKEN · /api/ponte/*: token da ponte do celular
Demais rotas         : sessão do login (acesso.py). Quem não é admin só vê Ao vivo, Eventos e Gravações das casas
                       liberadas para ele (casas.json), ex.: os pais só o rancho.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sqlite3
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import numpy as np
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

import acesso
import ia
import push
import visao

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("casa")

DADOS = Path(os.environ.get("DADOS", "/data"))
MIDIA = DADOS / "midia"
MIDIA.mkdir(parents=True, exist_ok=True)
AGENTE_TOKEN = os.environ.get("AGENTE_TOKEN", "")
NTFY_URL = os.environ.get("NTFY_URL", "http://ntfy:80")
NTFY_TOPICO = os.environ.get("NTFY_TOPICO", "casa")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
PAINEL_URL = os.environ.get("PAINEL_URL", "https://seu-dominio.exemplo.com")
RETENCAO_DIAS = int(os.environ.get("RETENCAO_DIAS", "30"))
STATIC = Path(__file__).parent / "static"

# ---- casas: cada câmera pertence a uma casa (tópico de aviso, alarme e quem pode ver) ----------------
CASAS: dict = json.loads((Path(__file__).parent / "casas.json").read_text(encoding="utf-8"))
CAM_CASA = {cam: c for c, d in CASAS.items() for cam in d["cameras"]}


def casa_de(camera: str) -> str:
    return CAM_CASA.get(camera, "casa")


def nome_cam(camera: str) -> str:
    return CASAS.get(casa_de(camera), {}).get("cameras", {}).get(camera, camera)


def cams_da_casa(casa: str) -> list[str]:
    return list(CASAS.get(casa, {}).get("cameras", {}))


def _em(cams: list) -> str:
    """Trecho SQL "camera IN (?,?)" (use com os nomes das câmeras nos argumentos)."""
    return f"camera IN ({','.join('?' * len(cams))})" if cams else "0"


def casas_do(u: dict) -> list[str]:
    return list(CASAS) if "*" in u["casas"] else [c for c in u["casas"] if c in CASAS]


def camera_permitida(u: dict, camera: str) -> bool:
    return bool(u["admin"]) or any(camera in CASAS[c]["cameras"] for c in casas_do(u))


def cams_permitidas(u: dict, casa: str = "") -> list[str] | None:
    """Câmeras que o usuário pode ver (None = todas, admin sem filtro de casa)."""
    if casa:
        if casa not in casas_do(u):
            raise HTTPException(403, "casa não liberada para você")
        return cams_da_casa(casa)
    if u["admin"]:
        return None
    return [cam for c in casas_do(u) for cam in CASAS[c]["cameras"]]

ROSTOS = visao.Rostos(Path(__file__).parent / "models", conf=0.75)  # igual ao agente: o que ele mandou já é cabeça de gente
APARENCIA = visao.Aparencia(Path(__file__).parent / "models")

# ---- banco ---------------------------------------------------------------------------------------
db = sqlite3.connect(DADOS / "casa.db", check_same_thread=False, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS eventos(
  id INTEGER PRIMARY KEY, ts REAL, camera TEXT, tipo TEXT, conhecidos TEXT, desconhecidos INTEGER,
  descricao TEXT, suspeito INTEGER, notificado INTEGER, fotos TEXT, caixas TEXT);
CREATE TABLE IF NOT EXISTS pessoas(id INTEGER PRIMARY KEY, nome TEXT UNIQUE, criado REAL);
CREATE TABLE IF NOT EXISTS rostos(
  id INTEGER PRIMARY KEY, evento_id INTEGER, pessoa_id INTEGER, quem TEXT, sim REAL, emb BLOB, img TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS comandos(
  id INTEGER PRIMARY KEY, ts REAL, alvo TEXT, acao TEXT, args TEXT, status TEXT, resultado TEXT);
CREATE TABLE IF NOT EXISTS estado(chave TEXT PRIMARY KEY, valor TEXT, ts REAL);
CREATE INDEX IF NOT EXISTS ix_ev_ts ON eventos(ts);
CREATE INDEX IF NOT EXISTS ix_ro_pessoa ON rostos(pessoa_id);
CREATE TABLE IF NOT EXISTS corpos(id INTEGER PRIMARY KEY, evento_id INTEGER, camera TEXT, ts REAL, emb BLOB, nome TEXT);
CREATE INDEX IF NOT EXISTS ix_corpos_ts ON corpos(ts);
CREATE INDEX IF NOT EXISTS ix_corpos_ev ON corpos(evento_id);
CREATE INDEX IF NOT EXISTS ix_ro_evento ON rostos(evento_id);
CREATE INDEX IF NOT EXISTS ix_ro_img ON rostos(img);
CREATE INDEX IF NOT EXISTS ix_cmd_status ON comandos(status);
CREATE TABLE IF NOT EXISTS ia_uso(id INTEGER PRIMARY KEY, ts REAL, tarefa TEXT, modelo TEXT, entrada INTEGER,
  saida INTEGER, custo REAL);
CREATE TABLE IF NOT EXISTS rotinas(
  id INTEGER PRIMARY KEY, nome TEXT, ativo INTEGER, dias TEXT, hora TEXT, comodos TEXT, config TEXT,
  criado REAL, ultima REAL);
""")
if "ignorado" not in {c["name"] for c in db.execute("PRAGMA table_info(rostos)")}:
    db.execute("ALTER TABLE rostos ADD COLUMN ignorado INTEGER DEFAULT 0")
def _migrar_rostos() -> None:
    """Se o modelo de rosto mudou (SFace -> ArcFace), recalcula todos os embeddings a partir dos recortes
    112x112 guardados; recorte sumido = rosto ignorado."""
    r = db.execute("SELECT valor FROM estado WHERE chave='modelo_rosto'").fetchone()
    if r and json.loads(r["valor"]) == ROSTOS.modelo:
        return
    n = 0
    for row in db.execute("SELECT id, img FROM rostos").fetchall():
        im = cv2.imread(str(MIDIA / row["img"])) if row["img"] else None
        if im is None:  # sem recorte não dá para recalcular: sai do cadastro (o vetor velho tem outro tamanho)
            db.execute("UPDATE rostos SET ignorado=1, pessoa_id=NULL WHERE id=?", (row["id"],))
            continue
        db.execute("UPDATE rostos SET emb=? WHERE id=?", (ROSTOS.embedding(im).tobytes(), row["id"]))
        n += 1
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('modelo_rosto',?,?)",
               (json.dumps(ROSTOS.modelo), time.time()))
    log.info("rostos recalculados com %s: %d", ROSTOS.modelo, n)


def _migrar_colunas_rostos() -> None:
    """02/11: `manual` (cadastro por foto ou confirmado pelo morador: não entra na poda dos automáticos), `lado`
    (px reais do rosto na imagem) e `qual` (nitidez × pose × inteiro). Rostos antigos ficam com lado/qual nulos."""
    colunas = {r["name"] for r in db.execute("PRAGMA table_info(rostos)")}
    if "manual" not in colunas:
        db.execute("ALTER TABLE rostos ADD COLUMN manual INTEGER DEFAULT 0")
        db.execute("UPDATE rostos SET manual=1 WHERE evento_id IS NULL OR (pessoa_id IS NOT NULL AND quem IS NULL)")
    for col, tipo in (("lado", "REAL"), ("qual", "REAL")):
        if col not in colunas:
            db.execute(f"ALTER TABLE rostos ADD COLUMN {col} {tipo}")


_migrar_colunas_rostos()
_migrar_rostos()
ia._registrar = lambda tarefa, modelo, ent, sai, custo: db.execute(
    "INSERT INTO ia_uso(ts,tarefa,modelo,entrada,saida,custo) VALUES(?,?,?,?,?,?)",
    (time.time(), tarefa, modelo, ent, sai, custo))
# Morador reconhecido (rosto, passagem ou aparência) não vai para a IA: não avisa e era ~46% das chamadas.
# Para a IA procurar "atividade suspeita" também neles: IA_ANALISAR_CONHECIDOS=1 no .env.
IA_ANALISAR_CONHECIDOS = os.environ.get("IA_ANALISAR_CONHECIDOS", "0") == "1"


def recortes_para_ia(dados: list[bytes], caixas_img: list, maximo: int = 2, lado: int = 768) -> list[bytes]:
    """Em vez de 2–3 quadros inteiros de 2560x1440 (~1.800 tokens cada), manda até 2 recortes da área das pessoas
    (com folga para a cena ao redor), com no máximo `lado` px (~500–800 tokens cada)."""
    out = []
    for i, b in enumerate(dados[:maximo]):
        img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        cx = caixas_img[i] if i < len(caixas_img) else []
        if cx:
            x0 = min(c[0] for c in cx); y0 = min(c[1] for c in cx)
            x1 = max(c[0] + c[2] for c in cx); y1 = max(c[1] + c[3] for c in cx)
            mw, mh = (x1 - x0) * 0.6 + 40, (y1 - y0) * 0.4 + 40
            img = img[max(0, int(y0 - mh)):int(y1 + mh), max(0, int(x0 - mw)):int(x1 + mw)]
        esc = lado / max(img.shape[:2])
        if esc < 1:
            img = cv2.resize(img, None, fx=esc, fy=esc, interpolation=cv2.INTER_AREA)
        out.append(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 82])[1].tobytes())
    return out or dados[:1]





_lock = asyncio.Lock()
_novo_comando = asyncio.Event()
GRUPO_LIMIAR = ROSTOS.limiar + 0.06  # um pouco acima de "mesma pessoa" (com ArcFace 0,34; a 0,42 os grupos fragmentavam)
_conhecidos_cache: dict = {"chave": None, "lista": []}


def conhecidos() -> list[tuple[str, np.ndarray]]:
    """Rostos do cadastro (sem os ignorados: o vetor de um recorte sumido pode ter outro tamanho). Em cache:
    recarrega só quando a tabela muda (a chave vê contagem, último id e os pares id×pessoa)."""
    chave = tuple(db.execute("SELECT COUNT(*), MAX(id), SUM(id * pessoa_id) FROM rostos "
                             "WHERE pessoa_id IS NOT NULL AND COALESCE(ignorado,0)=0").fetchone())
    if chave != _conhecidos_cache["chave"]:
        rows = db.execute("SELECT p.nome, r.emb FROM rostos r JOIN pessoas p ON p.id=r.pessoa_id "
                          "WHERE COALESCE(r.ignorado,0)=0").fetchall()
        _conhecidos_cache.update(chave=chave, lista=[(r["nome"], np.frombuffer(r["emb"], np.float32)) for r in rows])
    return _conhecidos_cache["lista"]


def ajuste(casa: str, chave: str, padrao):
    """Parâmetros do reconhecimento por casa (Admin > Casas grava em "parametros"): no rancho a mesma pessoa fica
    o dia todo com a mesma roupa, então a aparência pode valer mais horas do que em casa."""
    v = CASAS.get(casa, {}).get("parametros", {}).get(chave)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else padrao


# ---- ntfy ----------------------------------------------------------------------------------------
# botões no aviso do celular (ntfy "Actions", montados em acoes_evento): o POST volta para /api/acao/*
# com um token que só serve para isso (silenciar, modo, "é fulano")
ACAO_TOKEN = os.environ.get("ACAO_TOKEN", "")
db.execute("CREATE TABLE IF NOT EXISTS pontes(id INTEGER PRIMARY KEY, casa TEXT, nome TEXT, hash TEXT UNIQUE, "
           "criado REAL, visto REAL, info TEXT, avisado INTEGER DEFAULT 0)")
acesso.iniciar(db, os.environ.get("PAINEL_USUARIO", ""), os.environ.get("PAINEL_HASH", ""))
db.execute("CREATE TABLE IF NOT EXISTS push_subs(id INTEGER PRIMARY KEY, uid INTEGER, endpoint TEXT UNIQUE, chaves TEXT, "
           "aparelho TEXT, criado REAL, ultimo REAL, status INTEGER)")
# histórico dos avisos (7 dias): o que saiu, para qual aparelho, e se o aparelho confirmou que recebeu (sw.js)
db.execute("CREATE TABLE IF NOT EXISTS avisos_log(id TEXT PRIMARY KEY, ts REAL, titulo TEXT, casa TEXT, categoria TEXT, "
           "prio INTEGER, ntfy INTEGER)")
db.execute("CREATE TABLE IF NOT EXISTS push_envios(id TEXT PRIMARY KEY, aviso TEXT, sub_id INTEGER, uid INTEGER, ts REAL, "
           "status INTEGER, recebido REAL)")
db.execute("CREATE INDEX IF NOT EXISTS push_envios_aviso ON push_envios(aviso)")
# gravação do cartão SD que a ponte mandou para cobrir buracos do ao vivo; pedidos_cartao = intervalos que a VPS
# pede à ponte (entregues no próximo sinal de vida)
db.execute("CREATE TABLE IF NOT EXISTS trechos_cartao(arquivo TEXT PRIMARY KEY, camera TEXT, inicio REAL, fim REAL, criado REAL)")
db.execute("CREATE TABLE IF NOT EXISTS pedidos_cartao(id INTEGER PRIMARY KEY, ponte INTEGER, camera TEXT, ini REAL, fim REAL)")
# comandos do Admin > Pontes para o celular (ponte 1.7+): entregues no sinal de vida, o resultado volta no seguinte
db.execute("CREATE TABLE IF NOT EXISTS comandos_ponte(id INTEGER PRIMARY KEY, ponte INTEGER, acao TEXT, camera TEXT, "
           "criado REAL, enviado REAL, ok INTEGER, resultado TEXT, fim REAL)")
push.iniciar(DADOS, PAINEL_URL)


def _ler_estado(chave: str, padrao=None):
    r = db.execute("SELECT valor FROM estado WHERE chave=?", (chave,)).fetchone()
    return json.loads(r["valor"]) if r else padrao


def aplicar_casas(novas: dict) -> None:
    """Troca as casas em memória (os dicts são os mesmos objetos usados no resto do código)."""
    CASAS.clear()
    CASAS.update(novas)
    CAM_CASA.clear()
    CAM_CASA.update({cam: c for c, d in CASAS.items() for cam in d["cameras"]})


# casas.json é só a semente: depois de salvar no Admin, vale o que está no banco
if _ler_estado("casas"):
    aplicar_casas(_ler_estado("casas"))

SEGREDO_MIDIA = bytes.fromhex(_ler_estado("segredo_midia") or "")
if not SEGREDO_MIDIA:
    SEGREDO_MIDIA = secrets.token_bytes(32)
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('segredo_midia',?,?)",
               (json.dumps(SEGREDO_MIDIA.hex()), time.time()))


def url_assinada(caminho: str, horas: float = 48) -> str:
    """Foto do evento na notificação: o celular busca a imagem sem o cookie do login."""
    ate = int(time.time() + horas * 3600)
    sig = hmac.new(SEGREDO_MIDIA, f"{caminho}|{ate}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"/midia/{caminho}?ate={ate}&s={sig}"


def assinatura_ok(caminho: str, ate: str, sig: str) -> bool:
    if not sig or not ate.isdigit() or int(ate) < time.time():
        return False
    esperado = hmac.new(SEGREDO_MIDIA, f"{caminho}|{ate}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(esperado, sig)


SISTEMA_PADRAO = {"ia_analisar_conhecidos": os.environ.get("IA_ANALISAR_CONHECIDOS", "0") == "1",
                  "ia_modelo_evento": ia.MODELO_EVENTO, "resumo_diario": True, "resumo_hora": "22:00"}
MODELOS_EVENTO = {"claude-haiku-5-5": "Haiku 5.5 (mais barato)", "claude-sonnet-5-5": "Sonnet 5.5",
                  "claude-opus-5-5": "Opus 5.5 (mais caro)"}


def sistema() -> dict:
    return {**SISTEMA_PADRAO, **(_ler_estado("sistema") or {})}


ia.MODELO_EVENTO = sistema()["ia_modelo_evento"]


def _ntfy(titulo: str, msg: str, prioridade: int, tags: str, click: str, foto: bytes | None, acoes: str = "",
          topico: str = NTFY_TOPICO):
    # cabeçalhos HTTP só aceitam latin-1: o texto vai codificado (RFC 2047) como o ntfy aceita
    def h(v: str) -> str:
        return v if v.isascii() else "=?UTF-8?B?" + base64.b64encode(v.encode()).decode() + "?="

    headers = {"Title": h(titulo), "Priority": str(prioridade), "Tags": tags, "Click": click,
               "Authorization": f"Bearer {NTFY_TOKEN}"}
    if acoes:
        headers["Actions"] = h(acoes)
    if foto:
        headers.update({"Filename": "evento.jpg", "Message": h(msg)})
        body = foto
    else:
        body = msg.encode()
    req = urllib.request.Request(f"{NTFY_URL}/{topico}", data=body, headers=headers, method="PUT")
    with urllib.request.urlopen(req, timeout=15) as r:
        r.read()


CATEGORIAS = {"pessoa": "Pessoa desconhecida ou não identificada", "suspeito": "Atividade suspeita",
              "sistema": "Câmera ou ponte fora do ar", "robo": "Robô", "resumo": "Resumo do dia"}
PERM_CATEGORIA = {"pessoa": "eventos", "suspeito": "eventos", "sistema": "aovivo", "robo": "robo", "resumo": "eventos"}
_envios: set = set()


def _categoria(titulo: str, tags: str) -> str:
    if titulo.startswith("Atividade suspeita"):
        return "suspeito"
    if tags in ("rotating_light", "bust_in_silhouette"):
        return "pessoa"
    if tags == "broom" or titulo.startswith(("Robson", "Rotina")):
        return "robo"
    return "resumo" if tags == "memo" else "sistema"


def prefs_push(bruto) -> dict:
    p = json.loads(bruto) if isinstance(bruto, str) and bruto else (bruto or {})
    return {"casas": p.get("casas"), "categorias": {c: bool(p.get("categorias", {}).get(c, True)) for c in CATEGORIAS}}


async def avisar_push(titulo, msg, prio, click, casa, categoria, ev_id, aviso_id):
    try:
        img = None
        if ev_id:
            r = db.execute("SELECT fotos FROM eventos WHERE id=?", (ev_id,)).fetchone()
            fotos = json.loads(r["fotos"] or "[]") if r else []
            img = url_assinada(fotos[0]) if fotos else None
        url = click[len(PAINEL_URL):] if click and click.startswith(PAINEL_URL) else "/"
        for r in db.execute("SELECT s.id, s.endpoint, s.chaves, u.casas, u.push_prefs, p.permissoes FROM push_subs s "
                            "JOIN usuarios u ON u.id=s.uid LEFT JOIN perfis p ON p.id=u.perfil_id "
                            "WHERE COALESCE(u.ativo,1)=1").fetchall():
            perms = set(json.loads(r["permissoes"] or "[]"))
            u = {"casas": json.loads(r["casas"] or "[]"), "admin": "admin" in perms}
            pref = prefs_push(r["push_prefs"])
            if casa not in casas_do(u) or (pref["casas"] is not None and casa not in pref["casas"]):
                continue
            if not pref["categorias"].get(categoria, True) or not (u["admin"] or PERM_CATEGORIA[categoria] in perms):
                continue
            acoes = []
            if ev_id and (u["admin"] or "alarme" in perms):
                acoes.append({"action": "silenciar", "title": "Silenciar 1 h"})
            acoes.append({"action": "abrir", "title": "Abrir"})
            # tag única: um aviso não substitui o anterior na gaveta (antes "sistema" apagava o "sistema" anterior)
            envio = secrets.token_hex(8)
            dados = {"t": titulo, "b": msg[:400], "u": url, "i": img, "tag": f"ev{ev_id}" if ev_id else f"{categoria}-{envio}",
                     "p": prio, "casa": casa, "a": acoes, "id": envio}
            # Urgency high sempre: com "normal" o Android segura o push no modo soneca (Doze) e o aviso chega
            # atrasado ou some no TTL, enquanto o ntfy (conexão própria) chega na hora
            st = await asyncio.to_thread(push.enviar, {"endpoint": r["endpoint"], "keys": json.loads(r["chaves"])},
                                         dados, True)
            db.execute("INSERT INTO push_envios(id,aviso,sub_id,uid,ts,status) VALUES(?,?,?,?,?,?)",
                       (envio, aviso_id, r["id"], r["uid"], time.time(), st))
            if st in (404, 410):
                db.execute("DELETE FROM push_subs WHERE id=?", (r["id"],))
            else:
                db.execute("UPDATE push_subs SET ultimo=?, status=? WHERE id=?", (time.time(), st, r["id"]))
    except Exception as e:  # noqa: BLE001
        log.warning("push: %s", e)


async def avisar(titulo, msg, prioridade=3, tags="house", click=PAINEL_URL, foto=None, acoes="", casa="casa",
                 categoria=""):
    """casa escolhe o tópico do ntfy e quem recebe o push; categoria (ou o tipo deduzido do título/tags) filtra
    pelas preferências de cada um (aba Avisos)."""
    m = re.search(r"#evento-(\d+)", click or "")
    categoria = categoria or _categoria(titulo, tags)
    aviso_id = secrets.token_hex(8)
    db.execute("DELETE FROM avisos_log WHERE ts<?", (time.time() - 7 * 86400,))
    db.execute("DELETE FROM push_envios WHERE ts<?", (time.time() - 7 * 86400,))
    db.execute("INSERT INTO avisos_log(id,ts,titulo,casa,categoria,prio,ntfy) VALUES(?,?,?,?,?,?,NULL)",
               (aviso_id, time.time(), titulo, casa, categoria, prioridade))
    log.info("aviso [%s/%s p%d]: %s", casa, categoria, prioridade, titulo)
    t = asyncio.create_task(avisar_push(titulo, msg, prioridade, click, casa, categoria,
                                        int(m.group(1)) if m else None, aviso_id))
    _envios.add(t)
    t.add_done_callback(_envios.discard)
    if not NTFY_TOKEN:
        log.info("aviso (ntfy sem token): %s | %s", titulo, msg)
        return
    topico = CASAS.get(casa, {}).get("topico") or NTFY_TOPICO
    try:
        await asyncio.to_thread(_ntfy, titulo, msg, prioridade, tags, click, foto, acoes, topico)
        db.execute("UPDATE avisos_log SET ntfy=1 WHERE id=?", (aviso_id,))
    except Exception as e:  # aviso não pode derrubar o evento
        db.execute("UPDATE avisos_log SET ntfy=0 WHERE id=?", (aviso_id,))
        log.warning("ntfy falhou: %s", e)


# ---- rotinas -------------------------------------------------------------------------------------
async def _laco(nome: str, passo, intervalo: float):
    """Roda `passo` a cada `intervalo` s. Uma exceção é registrada e o laço segue: antes ela matava a tarefa
    para sempre (limpeza parada = disco cheio, agendador parado = rotina não dispara)."""
    while True:
        try:
            await passo()
        except Exception:  # noqa: BLE001
            log.exception("laço %s falhou nesta rodada", nome)
        await asyncio.sleep(intervalo)


async def limpeza_passo():
    limite = time.time() - RETENCAO_DIAS * 86400
    for r in db.execute("SELECT id, fotos FROM eventos WHERE ts < ?", (limite,)).fetchall():
        for f in json.loads(r["fotos"] or "[]"):
            (MIDIA / f).unlink(missing_ok=True)
    for r in db.execute("SELECT img FROM rostos WHERE pessoa_id IS NULL AND ts < ? AND img IS NOT NULL", (limite,)).fetchall():
        (MIDIA / r["img"]).unlink(missing_ok=True)  # recorte do rosto que vai sair (os do cadastro ficam)
    db.execute("DELETE FROM rostos WHERE pessoa_id IS NULL AND ts < ?", (limite,))
    db.execute("DELETE FROM eventos WHERE ts < ?", (limite,))
    db.execute("DELETE FROM corpos WHERE ts < ?", (limite,))  # assinaturas de corpo seguem os eventos
    db.execute("DELETE FROM comandos WHERE ts < ?", (time.time() - 7 * 86400,))  # a fila só mostra os últimos 20
    db.execute("DELETE FROM pedidos_cartao WHERE ponte NOT IN (SELECT id FROM pontes)")  # ponte revogada


_resumo = {"feito": ""}  # dia (AAAA-MM-DD) em que o resumo já saiu


async def resumo_passo():
    # hora e liga/desliga vêm do Admin > Sistema
    cfg, agora = sistema(), datetime.now()
    if not cfg["resumo_diario"] or agora.strftime("%H:%M") != cfg["resumo_hora"] or _resumo["feito"] == agora.strftime("%F"):
        return
    _resumo["feito"] = agora.strftime("%F")
    for casa, d in list(CASAS.items()):
        txt = eventos_texto(time.time() - 86400, cams_da_casa(casa))
        if txt:
            r = await ia.sobre_historico("Faça um resumo curto do dia (até 5 linhas), destacando estranhos "
                                         "e qualquer coisa fora do normal.", txt, esforco="low")
            await avisar(f"Resumo do dia — {d['nome']}", r, prioridade=2, tags="memo", casa=casa)


@asynccontextmanager
async def vida(app):
    tarefas = [asyncio.create_task(_laco("limpeza", limpeza_passo, 6 * 3600)),
               asyncio.create_task(_laco("resumo", resumo_passo, 20)),
               asyncio.create_task(_laco("agendador", agendador_passo, 20)),
               asyncio.create_task(vigia_pontes()),
               asyncio.create_task(sincronizar_go2rtc_inicio())]
    yield
    for t in tarefas:
        t.cancel()


app = FastAPI(title="Casa", lifespan=vida, docs_url=None, redoc_url=None)


@app.get("/api/ia/uso")
async def uso_ia(dias: int = 7):
    por_dia = [dict(r) for r in db.execute(
        "SELECT date(ts,'unixepoch','localtime') AS dia, tarefa, modelo, COUNT(*) AS chamadas, SUM(entrada) AS entrada, "
        "SUM(saida) AS saida, ROUND(SUM(custo),4) AS custo FROM ia_uso WHERE ts>? GROUP BY dia, tarefa, modelo "
        "ORDER BY dia DESC", (time.time() - min(dias, 90) * 86400,)).fetchall()]
    return {"por_dia": por_dia, "modelo_evento": ia.MODELO_EVENTO, "modelo": ia.MODELO,
            "analisa_conhecidos": sistema()["ia_analisar_conhecidos"]}


def _token_ok(authorization: str, token: str) -> bool:
    # compara bytes: compare_digest com str só aceita ASCII (cabeçalho com acento dava TypeError -> 500)
    return bool(token) and hmac.compare_digest(authorization.encode(), f"Bearer {token}".encode())


def agente(authorization: str = Header("")):
    if not _token_ok(authorization, AGENTE_TOKEN):
        raise HTTPException(401, "token do agente inválido")


# ---- agente --------------------------------------------------------------------------------------
APARENCIA_JANELA = 3 * 3600  # assinatura do corpo de quem foi reconhecido vale por 3 h (mesma roupa)
# 23/10 (48 h reais, pares dentro de 3 h): mesma câmera — mesma pessoa med 0,45, outra pessoa p99 0,55 / máx 0,75
# (0,65 confunde 0,3%); outra câmera — mesma pessoa med 0,40, outra pessoa máx 0,54. Só rebaixa, nunca apaga.
APAR_MESMA_CAMERA, APAR_OUTRA_CAMERA = 0.65, 0.58
VISITA_JANELA = 90       # s: a mesma passagem aparece nas duas câmeras em segundos (52 de 53 nomes concordaram)
VISITA_APAR = 0.40       # aparência mínima para levar o nome de uma câmera à outra (outra pessoa p90 0,36)
AVISO_ESPERA = 45        # s: "pessoa sem rosto" espera a outra câmera reconhecer antes de avisar
HIST_JANELA = 600        # s: quem é reconhecido dá nome, no histórico, a eventos sem rosto de ± 10 min com aparência forte
REPETE_JANELA = 600      # s: a mesma pessoa não identificada não avisa de novo em 10 min (sequências de 4–6 avisos)
REPETE_APAR = 0.45       # aparência para considerar "a mesma pessoa de antes" (23/10: mesma pessoa em 3 h med 0,40–0,45)


def _uma_pessoa(caixas_json) -> bool:
    try:
        cx = json.loads(caixas_json) if isinstance(caixas_json, str) else caixas_json
        return 0 < max((len(c) for c in cx), default=0) <= 1  # sem caixa nenhuma não é "uma pessoa"
    except (ValueError, TypeError):
        return False


def nome_da_passagem(ts: float, embs: list, ignorar_id: int = 0, cams: list | None = None) -> tuple[str | None, float]:
    """Morador reconhecido pelo ROSTO em ± VISITA_JANELA (qualquer câmera), sozinho no quadro, cujo corpo bate
    com este (≥ VISITA_APAR). Com duas pessoas no quadro não leva o nome (alguém pode ter entrado junto)."""
    if not embs:
        return None, 0.0
    melhor, sim_m = None, 0.0
    filtro, extra = (f" AND {_em(cams)}", cams) if cams else ("", [])
    for ev in db.execute("SELECT id, caixas FROM eventos WHERE tipo='conhecido' AND id<>? AND ts BETWEEN ? AND ?" + filtro,
                         (ignorar_id, ts - VISITA_JANELA, ts + VISITA_JANELA, *extra)).fetchall():
        if not _uma_pessoa(ev["caixas"]):
            continue
        for c in db.execute("SELECT nome, emb FROM corpos WHERE evento_id=? AND nome IS NOT NULL", (ev["id"],)):
            g = np.frombuffer(c["emb"], np.float32)
            for e in embs:
                sm = float(e @ g)
                if sm >= VISITA_APAR and sm > sim_m:
                    melhor, sim_m = c["nome"], sm
    return melhor, sim_m


def _sims(ev_id: int, embs: list) -> list[float]:
    return [float(np.frombuffer(c["emb"], np.float32) @ e)
            for c in db.execute("SELECT emb FROM corpos WHERE evento_id=?", (ev_id,)) for e in embs]


def ligar_passagem(ev_id: int, ts: float, nome: str, embs: list, camera: str) -> int:
    """Evento reconhecido chegou: eventos sem rosto ao redor, com 1 pessoa, viram "visita" com o nome.
    Até 90 s basta aparência ≥ 0,40 (mesma passagem); até 10 min exige aparência forte (limiares do "provável").
    Inclui os que ainda esperam para avisar (o aviso atrasado confere o tipo)."""
    n, cams = 0, cams_da_casa(casa_de(camera))
    for ev in db.execute("SELECT id, ts, camera, caixas FROM eventos WHERE id<>? AND ts BETWEEN ? AND ? "
                         f"AND tipo IN ('pessoa','pessoa_familiar','provavel') AND {_em(cams)}",
                         (ev_id, ts - HIST_JANELA, ts + HIST_JANELA, *cams)).fetchall():
        if not _uma_pessoa(ev["caixas"]):
            continue
        sims = _sims(ev["id"], embs)
        if not sims:
            continue
        perto = abs(ev["ts"] - ts) <= VISITA_JANELA
        lim = VISITA_APAR if perto else (APAR_MESMA_CAMERA if ev["camera"] == camera else APAR_OUTRA_CAMERA)
        if max(sims) >= lim:
            db.execute("UPDATE eventos SET tipo='visita', conhecidos=? WHERE id=?", (json.dumps([nome]), ev["id"]))
            db.execute("UPDATE corpos SET nome=? WHERE evento_id=? AND nome IS NULL", (nome, ev["id"]))
            n += 1
    return n


def repete_aviso(ev_id: int, ts: float, camera: str) -> int | None:
    """Outro aviso de pessoa não identificada/estranho nos últimos 10 min com a mesma aparência → id dele."""
    embs = [np.frombuffer(c["emb"], np.float32) for c in db.execute("SELECT emb FROM corpos WHERE evento_id=?", (ev_id,))]
    if not embs:
        return None
    cams = cams_da_casa(casa_de(camera))
    for ev in db.execute("SELECT id FROM eventos WHERE id<>? AND notificado=1 AND tipo IN ('pessoa','estranho') "
                         f"AND ts BETWEEN ? AND ? AND {_em(cams)}",
                         (ev_id, ts - ajuste(casa_de(camera), "repete_janela_s", REPETE_JANELA), ts, *cams)).fetchall():
        sims = _sims(ev["id"], embs)
        if sims and max(sims) >= REPETE_APAR:
            return ev["id"]
    return None


def provavel_por_aparencia(camera: str, embs: list, ts: float, lim_min: float | None = None) -> tuple[str | None, float]:
    """Melhor pessoa identificada (por rosto ou botão) cuja aparência recente bate com algum corpo do evento.
    `lim_min`: limiar mais baixo quando um rosto no limite já aponta a mesma pessoa (ver evento)."""
    if not embs:
        return None, 0.0
    melhor, sim_m = None, 0.0
    casa = casa_de(camera)
    cams = cams_da_casa(casa)
    for r in db.execute(f"SELECT nome, camera, emb FROM corpos WHERE nome IS NOT NULL AND ts > ? AND {_em(cams)}",
                        (ts - ajuste(casa, "aparencia_janela_s", APARENCIA_JANELA), *cams)).fetchall():
        g = np.frombuffer(r["emb"], np.float32)
        lim = APAR_MESMA_CAMERA if r["camera"] == camera else APAR_OUTRA_CAMERA
        if lim_min is not None:
            lim = min(lim, lim_min)
        for e in embs:
            s = float(e @ g)
            if s >= lim and s > sim_m:
                melhor, sim_m = r["nome"], s
    return melhor, sim_m


def acoes_evento(ev_id: int, casa: str = "casa") -> str:
    """Botões do aviso: "É <morador>" (até 2) + "Silenciar 1 h" (o ntfy aceita no máximo 3)."""
    if not ACAO_TOKEN:
        return ""
    nomes = [r["nome"] for r in db.execute("SELECT nome FROM pessoas ORDER BY id LIMIT 2").fetchall()]
    botoes = [f"http, É {n}, {PAINEL_URL}/api/acao/identificar?ev={ev_id}&nome={urllib.parse.quote(n)}, "
              f"method=POST, headers.Authorization=Bearer {ACAO_TOKEN}, clear=true" for n in nomes]
    botoes.append(f"http, Silenciar 1 h, {PAINEL_URL}/api/acao/silenciar?horas=1&casa={casa}, method=POST, "
                  f"headers.Authorization=Bearer {ACAO_TOKEN}, clear=true")
    return "; ".join(botoes)


@app.post("/api/agente/evento", dependencies=[Depends(agente)])
async def evento(camera: str = Form(...), ts: float = Form(...), caixas: str = Form("[]"),
                 fotos: list[UploadFile] = File(...)):
    dados = [await f.read() for f in fotos[:3]]
    casa, cam_nome, cams_casa = casa_de(camera), nome_cam(camera), cams_da_casa(casa_de(camera))
    dia = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    (MIDIA / dia).mkdir(exist_ok=True)
    nomes_fotos, faces, corpos = [], [], []  # corpos: (assinatura, nome do rosto dentro dele ou None)
    known = conhecidos()
    try:
        caixas_img = json.loads(caixas)
    except ValueError:
        caixas_img = []
    for i, b in enumerate(dados):
        nome = f"{dia}/{int(ts)}_{camera}_{i}_{uuid.uuid4().hex[:6]}.jpg"
        (MIDIA / nome).write_bytes(b)
        nomes_fotos.append(nome)
        img = cv2.imdecode(np.frombuffer(b, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        pessoas_img = caixas_img[i] if i < len(caixas_img) else []
        # rosto só dentro da cabeça de uma pessoa (ampliada): descarta pelúcia/estampa e pega rosto pequeno
        dono_corpo: dict[int, str | None] = {}
        for f in await asyncio.to_thread(ROSTOS.em_pessoas, img, pessoas_img):
            if f["lado_real"] < ROSTO_MIN_RECONHECER:
                continue  # com ~5–15 px reais o embedding é ruído: conta como "sem rosto", nunca como estranho
            r = ROSTOS.comparar_detalhe(f["emb"], known)
            recorte = f"{dia}/rosto_{uuid.uuid4().hex[:10]}.jpg"
            cv2.imwrite(str(MIDIA / recorte), f["recorte"])
            faces.append({**r, "emb": f["emb"], "img": recorte, "lado": f["lado_real"], "qual": f["qualidade"]})
            if r["quem"]:
                dono_corpo[f["pessoa"]] = r["quem"]
        for k, cx in enumerate(pessoas_img):
            e = await asyncio.to_thread(APARENCIA.assinatura, img, cx)
            if e is not None:
                corpos.append((e, dono_corpo.get(k)))

    # Uma pessoa só nos quadros: vale o MELHOR rosto para todos os quadros. Antes, um quadro borrado (0,21) da
    # mesma pessoa reconhecida em outro quadro (0,39) virava "desconhecido" — 7 dos 10 "estranhos" de 21–23/10
    # eram isso; e um quadro borrado reconhecido como OUTRO morador a 0,29 dava evento com dois nomes.
    uma_pessoa = _uma_pessoa(caixas_img)
    if uma_pessoa and any(f["quem"] for f in faces):
        dono = max((f for f in faces if f["quem"]), key=lambda f: f["sim"])["quem"]
        for f in faces:
            f["quem"] = dono
        corpos = [(e, dono) for e, _ in corpos]
    nomes = sorted({f["quem"] for f in faces if f["quem"]})
    estranhos = sum(1 for f in faces if f["quem"] is None)
    provavel, sim_apar = None, 0.0
    sem_rosto = [e for e, n in corpos if n is None]
    if estranhos and uma_pessoa and faces:
        # rosto no limite (0,20–0,28; pessoas diferentes deram no máximo 0,23 na calibração) com folga para o 2º
        # candidato E o corpo batendo com a aparência recente da MESMA pessoa: morador, não estranho
        q = max(faces, key=lambda f: f["sim"])
        cand, s_ap = provavel_por_aparencia(camera, [e for e, _ in corpos], ts, lim_min=VISITA_APAR)
        if q["sim"] >= QUASE_ROSTO and q["margem"] >= 0.05 and cand and cand == q["melhor"]:
            estranhos, provavel, sim_apar = 0, cand, s_ap
            log.info("rosto no limite (%.2f) + aparência (%.2f) de %s: provável, sem aviso", q["sim"], s_ap, cand)
    if estranhos:
        tipo = "estranho"
    elif nomes:
        tipo = "conhecido"
    elif provavel:
        tipo = "provavel"
    else:
        visita, sim_vis = nome_da_passagem(ts, sem_rosto, cams=cams_casa) if uma_pessoa else (None, 0.0)
        if visita:
            tipo, provavel, sim_apar = "visita", visita, sim_vis
        else:
            provavel, sim_apar = provavel_por_aparencia(camera, sem_rosto, ts)
            if provavel:
                tipo = "provavel"
            else:
                recente = db.execute("SELECT 1 FROM eventos WHERE tipo IN ('conhecido','provavel','visita') AND ts>? "
                                     f"AND {_em(cams_casa)}", (ts - 2 * 60, *cams_casa)).fetchone()
                tipo = "pessoa_familiar" if recente else "pessoa"

    # Avisa só o que não foi confirmado (pessoa não identificada, rosto desconhecido). Morador reconhecido,
    # "provavelmente Fulano" e "junto de morador" ficam só no histórico, mas a IA olha todos: atividade
    # suspeita avisa sempre, mesmo de alguém conhecido (decisão do usuário, 21/10).
    notificar = tipo in ("estranho", "pessoa")
    descr = {"descricao": "", "suspeito": False, "motivo": ""}
    analisar = tipo in ("estranho", "pessoa") or \
        (sistema()["ia_analisar_conhecidos"] and tipo in ("pessoa_familiar", "provavel", "visita", "conhecido"))
    if not analisar and tipo in ("pessoa_familiar", "provavel", "visita", "conhecido"):
        quem = ", ".join(nomes) or provavel or "morador"
        descr = {"descricao": f"{quem} (reconhecido; sem análise da IA para economizar)", "pessoas": 1,
                 "suspeito": False, "motivo": ""}
    if analisar:
        ctx = (f"O reconhecimento facial identificou: {', '.join(nomes)}." if nomes else "") + \
              (f" Há {estranhos} rosto(s) que não estão no cadastro." if estranhos else "") + \
              ("" if faces else " Nenhum rosto ficou visível.")
        descr = await ia.descrever_evento(camera, await asyncio.to_thread(recortes_para_ia, dados, caixas_img), ctx.strip())
        # a Claude confere: se ela não vê ninguém, é falso alarme do detector (objeto, sombra, animal)
        if descr.get("pessoas") == 0 and not descr["suspeito"]:
            tipo, notificar = "sem_pessoa", False

    alarme = situacao_alarme(casa)
    if alarme["silencio"] and not descr["suspeito"]:
        notificar = False

    async with _lock:
        cur = db.execute(
            "INSERT INTO eventos(ts,camera,tipo,conhecidos,desconhecidos,descricao,suspeito,notificado,fotos,caixas)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, camera, tipo, json.dumps(nomes or ([provavel] if provavel else [])), estranhos, descr["descricao"],
             int(descr["suspeito"]), int(notificar or descr["suspeito"]), json.dumps(nomes_fotos), caixas))
        ev_id = cur.lastrowid
        for f in faces:
            # aprendizado automático só com rosto reconhecido com folga (≥ AUTO_APRENDER), de frente/nítido/inteiro,
            # com ≥ ROSTO_MIN_APRENDER px, longe do 2º candidato e que não seja quase o mesmo quadro de um já guardado:
            # o cadastro ganha fotos da própria câmera (noite) sem andar na direção do ruído ou da outra pessoa
            pid = None
            if _pode_aprender(f, known):
                row = db.execute("SELECT id FROM pessoas WHERE nome=?", (f["quem"],)).fetchone()
                pid = row["id"] if row else None
            db.execute("INSERT INTO rostos(evento_id,pessoa_id,quem,sim,emb,img,ts,lado,qual) VALUES(?,?,?,?,?,?,?,?,?)",
                       (ev_id, pid, f["quem"], f["sim"], f["emb"].tobytes(), f["img"], ts, f["lado"], f["qual"]))
            if pid:
                _podar_auto(pid)
        for e, n in corpos:  # nome só quando veio do ROSTO (provável por aparência não realimenta a galeria)
            db.execute("INSERT INTO corpos(evento_id,camera,ts,emb,nome) VALUES(?,?,?,?,?)",
                       (ev_id, camera, ts, e.tobytes(), n))
        if tipo == "conhecido" and uma_pessoa and len(nomes) == 1:
            ligados = ligar_passagem(ev_id, ts, nomes[0], [e for e, _ in corpos], camera)
            if ligados:
                log.info("evento %s (%s) deu nome a %d evento(s) da mesma passagem", ev_id, nomes[0], ligados)

    if tipo == "pessoa" and notificar and not descr["suspeito"]:
        # espera a outra câmera: se ela reconhecer a pessoa nesse meio tempo, ligar_passagem muda o tipo
        db.execute("UPDATE eventos SET notificado=0 WHERE id=?", (ev_id,))
        t = asyncio.create_task(aviso_atrasado(ev_id, camera, ts, descr, dados[0], alarme))
        _envios.add(t)  # guarda a referência: sem ela o GC pode cancelar a tarefa antes dos 45 s
        t.add_done_callback(_envios.discard)
        notificar = False
    if notificar or descr["suspeito"]:
        hora = datetime.fromtimestamp(ts).strftime("%H:%M")
        noite = not 6 <= datetime.fromtimestamp(ts).hour < 22
        modo_casa = alarme["modo"] == "casa"
        if descr["suspeito"]:
            titulo, prio, tags = f"Atividade suspeita — {cam_nome} ({hora})", 5, "warning"
        elif tipo == "estranho":
            titulo, prio, tags = f"Pessoa desconhecida — {cam_nome} ({hora})", 3 if modo_casa else (5 if noite else 4), "rotating_light"
        elif tipo == "provavel":
            titulo, prio, tags = f"Provavelmente {provavel} (aparência) — {cam_nome} ({hora})", 2, "bust_in_silhouette"
        elif tipo == "pessoa_familiar":
            titulo, prio, tags = f"Pessoa sem rosto, junto de morador — {cam_nome} ({hora})", 2, "bust_in_silhouette"
        else:
            titulo, prio, tags = f"Pessoa (rosto não visível) — {cam_nome} ({hora})", 2 if modo_casa else (4 if noite else 3), "bust_in_silhouette"
        msg = descr["descricao"] + (f"\nMotivo: {descr['motivo']}" if descr["suspeito"] and descr["motivo"] else "")
        await avisar(titulo, msg, prio, tags, f"{PAINEL_URL}/#evento-{ev_id}", dados[0],
                     acoes=acoes_evento(ev_id, casa) if tipo != "conhecido" else "", casa=casa)
    return {"id": ev_id, "tipo": tipo, "conhecidos": nomes, "provavel": provavel, "sim_aparencia": round(sim_apar, 3),
            "estranhos": estranhos, "notificado": notificar}


AUTO_APRENDER = 0.40 if ROSTOS.modelo == "arcface_r50" else 0.45  # mínimo para um rosto de evento entrar sozinho no cadastro
AUTO_MAX = 60          # rostos automáticos por pessoa (ficam os de melhor nota; os manuais/confirmados não contam)
AUTO_DUPLICADO = 0.90  # cosseno com um rosto já cadastrado da pessoa: é o mesmo quadro, não ensina nada
AUTO_MARGEM = 0.08     # folga mínima para o 2º candidato: no limite entre dois moradores não aprende
ROSTO_MIN_RECONHECER = 18  # px reais: abaixo o ArcFace não distingue (os "estranhos" do rancho a 0,20–0,28)
ROSTO_MIN_APRENDER = 32    # px reais (e qualidade ≥ 0,5) para um rosto entrar sozinho no cadastro
QUASE_ROSTO = 0.20     # rosto entre isto e o limiar só vale com o corpo apontando a mesma pessoa


def _pode_aprender(f: dict, known: list) -> bool:
    if not f["quem"] or f["sim"] < AUTO_APRENDER or f["margem"] < AUTO_MARGEM:
        return False
    if f["lado"] < ROSTO_MIN_APRENDER or f["qual"] < 0.5:
        return False
    mesmos = [e for n, e in known if n == f["quem"]]
    return max((float(f["emb"] @ e) for e in mesmos), default=0.0) < AUTO_DUPLICADO


def _podar_auto(pid: int) -> None:
    """Ficam os AUTO_MAX automáticos de melhor nota (sim × qualidade), não os mais recentes."""
    ids = [r["id"] for r in db.execute(
        "SELECT id FROM rostos WHERE pessoa_id=? AND evento_id IS NOT NULL AND COALESCE(manual,0)=0 "
        "ORDER BY sim * COALESCE(qual, 1) DESC", (pid,)).fetchall()]
    for rid in ids[AUTO_MAX:]:
        db.execute("UPDATE rostos SET pessoa_id=NULL, ignorado=1 WHERE id=?", (rid,))


async def aviso_atrasado(ev_id, camera, ts, descr, foto, alarme):
    await asyncio.sleep(AVISO_ESPERA)
    r = db.execute("SELECT tipo FROM eventos WHERE id=?", (ev_id,)).fetchone()
    if not r or r["tipo"] != "pessoa" or situacao_alarme(casa_de(camera))["silencio"]:
        return  # reconhecido pela outra câmera (visita), identificado no painel, ou silenciado
    anterior = repete_aviso(ev_id, ts, camera)
    if anterior:
        log.info("evento %s: mesma pessoa do aviso %s (há < 10 min); sem novo aviso", ev_id, anterior)
        return
    db.execute("UPDATE eventos SET notificado=1 WHERE id=?", (ev_id,))
    hora = datetime.fromtimestamp(ts).strftime("%H:%M")
    noite = not 6 <= datetime.fromtimestamp(ts).hour < 22
    prio = 2 if alarme["modo"] == "casa" else (4 if noite else 3)
    await avisar(f"Pessoa não identificada — {nome_cam(camera)} ({hora})", descr["descricao"], prio, "bust_in_silhouette",
                 f"{PAINEL_URL}/#evento-{ev_id}", foto, acoes=acoes_evento(ev_id, casa_de(camera)), casa=casa_de(camera))


def identificar_evento(ev_id: int, nome: str) -> dict:
    """Morador disse "é fulano": marca o evento, liga os rostos do evento à pessoa e ensina a aparência do dia.
    Rostos e corpos só são ligados quando há UMA pessoa nos quadros; com mais gente, os rostos ficam em
    "Rostos a identificar" para o morador escolher."""
    pessoa = db.execute("SELECT id FROM pessoas WHERE nome=?", (nome,)).fetchone()
    if not pessoa:
        raise HTTPException(404, f"{nome} não está cadastrado")
    ev = db.execute("SELECT caixas FROM eventos WHERE id=?", (ev_id,)).fetchone()
    if not ev:
        raise HTTPException(404, "evento não existe")
    uma_pessoa = _uma_pessoa(ev["caixas"] or "[]")
    db.execute("UPDATE eventos SET tipo='conhecido', conhecidos=? WHERE id=?", (json.dumps([nome]), ev_id))
    rostos_n = corpos_n = 0
    if uma_pessoa:
        # só rosto com tamanho/qualidade de cadastro: "É Fulano" num aviso de rosto de 6 px ligava ruído à pessoa
        # (rostos antigos sem medida entram como antes)
        rostos_n = db.execute(
            "UPDATE rostos SET pessoa_id=?, quem=?, manual=1 WHERE evento_id=? AND pessoa_id IS NULL AND COALESCE(ignorado,0)=0 "
            "AND (lado IS NULL OR lado >= ?) AND (qual IS NULL OR qual >= 0.5)",
            (pessoa["id"], nome, ev_id, ROSTO_MIN_APRENDER)).rowcount
        corpos_n = db.execute("UPDATE corpos SET nome=? WHERE evento_id=?", (nome, ev_id)).rowcount
        _podar_auto(pessoa["id"])
    return {"ok": True, "evento": ev_id, "nome": nome, "rostos": rostos_n, "aparencias": corpos_n,
            "uma_pessoa": uma_pessoa}


@app.post("/api/agente/alerta", dependencies=[Depends(agente)])
async def alerta_agente(payload: dict):
    """Avisos de saúde vindos do PC (ex.: câmera sem vídeo depois de várias reconexões)."""
    casa = str(payload.get("casa", "casa"))
    await avisar(str(payload.get("titulo", "Aviso do PC"))[:100], str(payload.get("msg", ""))[:500],
                 max(1, min(int(payload.get("prioridade", 3)), 5)), "warning", casa=casa if casa in CASAS else "casa")
    return {"ok": True}


@app.post("/api/agente/estado", dependencies=[Depends(agente)])
async def estado_agente(payload: dict):
    for k, v in payload.items():
        antigo = db.execute("SELECT valor FROM estado WHERE chave=?", (k,)).fetchone()
        db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES(?,?,?)", (k, json.dumps(v), time.time()))
        if k == "robo" and isinstance(v, dict):
            await avisos_robo(json.loads(antigo["valor"]) if antigo else {}, v)
    return {"ok": True}


# Falhas do c101 identificadas pelo que o app mostrava no mesmo horário (22/10). Acrescente aqui as novas.
FALHAS_ROBO = {
    584: "Falta de água no tanque de água limpa (encha o tanque da base)",
    586: "Muita água no compartimento de limpeza da base (esvazie/limpe a bandeja e o tanque de água suja)",
}


async def avisos_robo(antes: dict, agora: dict):
    # status 0..12 da spec xiaomi.vacuum.c101; avisa transições relevantes
    a, b = antes.get("status"), agora.get("status")
    if a in (5, 6, 7) and b in (3, 4):
        await avisar("Robson terminou a limpeza", f"Bateria {agora.get('bateria')}%.", 2, "broom")
    # O "fault" (2.2) do c101 muda o tempo todo com o trabalho do robô e da base (2013, 2104 lavando o mop,
    # 2105, 2108, 2110...): não são falhas, então não viram aviso. Ficam registrados para descobrir o significado.
    codigo = agora.get("erro")
    # Falhas reais têm 3 dígitos (confirmado em 22/10 pelos horários relatados pelo morador); 2xxx = estados.
    # str(...).isdigit(): o agente pode mandar texto ("ok", "-1"), e int() quebraria o estado inteiro
    if codigo and str(codigo).isdigit() and int(codigo) < 1000:
        avisados, _ = _estado("robo_falhas_avisadas", {})
        if time.time() - avisados.get(str(codigo), 0) > 3600:  # a mesma falha no máximo 1x por hora
            avisados[str(codigo)] = time.time()
            _gravar("robo_falhas_avisadas", avisados)
            nome = FALHAS_ROBO.get(int(codigo), f"falha {codigo} (veja o app Mi Home e me diga o texto)")
            await avisar(f"Robson: {nome.split(' (')[0]}", f"Código {codigo}: {nome}.", 4, "warning")
    if codigo:
        cods, _ = _estado("robo_codigos", {})
        c = cods.setdefault(str(codigo), {"vezes": 0, "estados": []})
        c["vezes"] += 1
        c["ultimo"] = time.time()
        if b is not None and b not in c["estados"]:
            c["estados"] = sorted(c["estados"] + [b])
        _gravar("robo_codigos", cods)
    # limpeza parou no meio: pausado (2) por mais de 10 min depois de estar limpando
    pausa, _ = _estado("robo_pausa", {})
    if b == 2 and a in (5, 6, 7):
        _gravar("robo_pausa", {"desde": time.time(), "avisado": False})
    elif b == 2 and pausa.get("desde") and not pausa.get("avisado") and time.time() - pausa["desde"] > 600:
        _gravar("robo_pausa", {**pausa, "avisado": True})
        await avisar("Robson parou no meio da limpeza", "Está pausado há mais de 10 min (preso, tanque ou mop?).", 3, "warning")
    elif b != 2 and pausa:
        _gravar("robo_pausa", {})


# ---- presença e modo de alarme ---------------------------------------------------------------------
PRESENTE_JANELA = 20 * 60  # celular visto na rede de casa nos últimos 20 min = pessoa em casa (Wi-Fi do celular cochila)


def _gravar(chave: str, valor) -> None:
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES(?,?,?)", (chave, json.dumps(valor), time.time()))


def _chave(nome: str, casa: str) -> str:
    return nome if casa == "casa" else f"{nome}:{casa}"


def situacao_alarme(casa: str = "casa") -> dict:
    modo, _ = _estado(_chave("alarme_modo", casa), "normal")
    silencio, _ = _estado(_chave("silencio_ate", casa), 0)
    vistos, _ = _estado("rede_vistos", {})
    donos, _ = _estado("presenca_donos", {})
    agora = time.time()
    em_casa = sorted({donos[m] for m, v in vistos.items() if m in donos and agora - v["ts"] < PRESENTE_JANELA}) \
        if CASAS.get(casa, {}).get("presenca") else []  # a lista da rede vem do PC de casa
    # presença pelo Wi-Fi é só informativa: silenciar por celular na rede deixaria passar quem entra junto
    ocupada = modo == "casa"
    return {"casa": casa, "modo": modo, "em_casa": em_casa, "ocupada": ocupada,
            "silencio": agora < float(silencio or 0), "silencio_ate": silencio or 0}


@app.post("/api/agente/presenca", dependencies=[Depends(agente)])
async def receber_presenca(payload: dict):
    vistos, _ = _estado("rede_vistos", {})
    agora = time.time()
    for d in payload.get("dispositivos", [])[:300]:
        mac = str(d.get("mac", "")).lower()
        if mac:
            vistos[mac] = {"ts": agora, "ip": d.get("ip", ""), "nome": d.get("nome") or vistos.get(mac, {}).get("nome", "")}
    vistos = {m: v for m, v in vistos.items() if agora - v["ts"] < 30 * 86400}
    _gravar("rede_vistos", vistos)
    return {"ok": True}


@app.get("/api/presenca")
async def ver_presenca():
    vistos, _ = _estado("rede_vistos", {})
    donos, _ = _estado("presenca_donos", {})
    agora = time.time()
    lista = [{"mac": m, **v, "ha_s": round(agora - v["ts"]), "dono": donos.get(m, "")} for m, v in vistos.items()]
    lista.sort(key=lambda x: (x["dono"] == "", x["ha_s"]))
    return {"alarme": situacao_alarme(), "dispositivos": lista}


@app.post("/api/presenca/dono")
async def definir_dono(payload: dict):
    donos, _ = _estado("presenca_donos", {})
    mac, dono = str(payload.get("mac", "")).lower(), str(payload.get("dono", "")).strip()[:40]
    if dono:
        donos[mac] = dono
    else:
        donos.pop(mac, None)
    _gravar("presenca_donos", donos)
    return {"ok": True}


def _modo(modo: str, casa: str = "casa"):
    if modo not in ("normal", "casa"):
        raise HTTPException(400, "modo: normal ou casa")
    _gravar(_chave("alarme_modo", casa), modo)


def _silenciar(horas: float, casa: str = "casa"):
    _gravar(_chave("silencio_ate", casa), time.time() + max(0.0, min(horas, 24.0)) * 3600 if horas > 0 else 0)


@app.get("/api/alarme")
async def ver_alarme(request: Request, casa: str = "casa"):
    cams_permitidas(request.state.usuario, casa)  # confere se a casa é dele
    return situacao_alarme(casa)


@app.post("/api/alarme")
async def alarme(request: Request, payload: dict):
    casa = str(payload.get("casa", "casa"))
    cams_permitidas(request.state.usuario, casa)
    if "modo" in payload:
        _modo(payload["modo"], casa)
    if "silenciar_horas" in payload:
        _silenciar(float(payload["silenciar_horas"]), casa)
    return situacao_alarme(casa)


def _acao_token(authorization: str = Header("")):
    if not _token_ok(authorization, ACAO_TOKEN):
        raise HTTPException(401)


@app.post("/api/acao/silenciar", dependencies=[Depends(_acao_token)])
async def acao_silenciar(horas: float = 1, casa: str = "casa"):
    casa = casa if casa in CASAS else "casa"
    _silenciar(horas, casa)
    return situacao_alarme(casa)


@app.post("/api/acao/identificar", dependencies=[Depends(_acao_token)])
async def acao_identificar(ev: int, nome: str):
    return identificar_evento(ev, nome)


@app.post("/api/eventos/{ev_id}/identificar")
async def painel_identificar(request: Request, ev_id: int, payload: dict):
    ev = db.execute("SELECT camera FROM eventos WHERE id=?", (ev_id,)).fetchone()
    if not ev or not camera_permitida(request.state.usuario, ev["camera"]):
        raise HTTPException(404, "evento não existe")
    return identificar_evento(ev_id, str(payload.get("nome", "")))


@app.post("/api/acao/modo", dependencies=[Depends(_acao_token)])
async def acao_modo(modo: str, casa: str = "casa"):
    casa = casa if casa in CASAS else "casa"
    _modo(modo, casa)
    return situacao_alarme(casa)


@app.get("/api/agente/comandos", dependencies=[Depends(agente)])
async def comandos_pendentes(espera: int = 25):
    fim = time.time() + min(espera, 50)
    while True:
        rows = db.execute("SELECT * FROM comandos WHERE status='pendente' ORDER BY id").fetchall()
        if rows or time.time() >= fim:
            for r in rows:
                db.execute("UPDATE comandos SET status='enviado' WHERE id=?", (r["id"],))
            return [dict(r) for r in rows]
        _novo_comando.clear()
        try:
            await asyncio.wait_for(_novo_comando.wait(), timeout=max(0.1, fim - time.time()))
        except asyncio.TimeoutError:
            pass


@app.post("/api/agente/comandos/{cid}", dependencies=[Depends(agente)])
async def comando_resultado(cid: int, payload: dict):
    db.execute("UPDATE comandos SET status=?, resultado=? WHERE id=?",
               (payload.get("status", "feito"), json.dumps(payload.get("resultado")), cid))
    return {"ok": True}


# ---- painel --------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def painel():
    return (STATIC / "index.html").read_text(encoding="utf-8")


@app.get("/midia/{caminho:path}")
async def midia(request: Request, caminho: str):
    p = (MIDIA / caminho).resolve()
    if not p.is_relative_to(MIDIA.resolve()) or not p.is_file():
        raise HTTPException(404)
    u = request.state.usuario
    if not u["admin"]:  # foto de evento ou recorte de rosto: só de câmera liberada para ele
        r = db.execute("SELECT camera FROM eventos WHERE fotos LIKE ?", (f'%"{caminho}"%',)).fetchone() or \
            db.execute("SELECT e.camera FROM rostos r JOIN eventos e ON e.id=r.evento_id WHERE r.img=?", (caminho,)).fetchone()
        if not r or not camera_permitida(u, r["camera"]):
            raise HTTPException(404)
    # a foto nunca muda: o navegador guarda e não pede de novo a cada abertura da lista
    return FileResponse(p, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/eventos")
async def listar_eventos(request: Request, limite: int = 60, tipo: str = "", casa: str = ""):
    cams = cams_permitidas(request.state.usuario, casa)
    conds, args = [], []
    if tipo:
        conds.append("tipo=?")
        args.append(tipo)
    if cams is not None:
        conds.append(_em(cams))
        args += cams
    q = "SELECT * FROM eventos" + (" WHERE " + " AND ".join(conds) if conds else "")
    rows = db.execute(q + " ORDER BY ts DESC LIMIT ?", (*args, min(limite, 500))).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["fotos"] = json.loads(d["fotos"] or "[]")
        d["conhecidos"] = json.loads(d["conhecidos"] or "[]")
        d["rostos"] = [dict(x) for x in db.execute(
            "SELECT r.id, r.img, r.sim, COALESCE(p.nome, r.quem) AS nome, r.pessoa_id FROM rostos r LEFT JOIN pessoas p ON p.id=r.pessoa_id "
            "WHERE r.evento_id=?", (r["id"],)).fetchall()]
        out.append(d)
    return out


@app.get("/api/pessoas")
async def listar_pessoas():
    return [dict(r) for r in db.execute(
        "SELECT p.id, p.nome, COUNT(r.id) AS rostos, MAX(r.img) AS foto FROM pessoas p "
        "LEFT JOIN rostos r ON r.pessoa_id=p.id GROUP BY p.id ORDER BY p.nome").fetchall()]


def _pessoa_id(nome: str) -> int:
    nome = nome.strip()
    if not nome:
        raise HTTPException(400, "nome vazio")
    db.execute("INSERT OR IGNORE INTO pessoas(nome,criado) VALUES(?,?)", (nome, time.time()))
    return db.execute("SELECT id FROM pessoas WHERE nome=?", (nome,)).fetchone()["id"]


@app.post("/api/rostos/{rid}/nomear")
async def nomear_rosto(request: Request, rid: int, payload: dict):
    # como painel_identificar: só rosto de evento de câmera liberada para ele (rosto de cadastro: só admin)
    r = db.execute("SELECT e.camera FROM rostos r LEFT JOIN eventos e ON e.id=r.evento_id WHERE r.id=?", (rid,)).fetchone()
    if not r:
        raise HTTPException(404, "rosto não existe")
    if not camera_permitida(request.state.usuario, r["camera"]):
        raise HTTPException(403, "rosto de câmera de outra casa")
    pid = _pessoa_id(payload.get("nome", ""))
    db.execute("UPDATE rostos SET pessoa_id=?, manual=1 WHERE id=?", (pid, rid))  # confirmado pelo morador: não é podado
    return {"ok": True, "pessoa_id": pid}


@app.get("/api/rostos/grupos")
async def grupos_rostos(dias: int = 30, limite: int = 800):
    """Rostos de eventos ainda sem nome, agrupados por semelhança (provável mesma pessoa)."""
    rows = db.execute(
        "SELECT r.id, r.img, r.sim, r.quem, r.emb, r.ts, e.camera FROM rostos r "
        "LEFT JOIN eventos e ON e.id=r.evento_id WHERE r.pessoa_id IS NULL AND COALESCE(r.ignorado,0)=0 "
        "AND r.evento_id IS NOT NULL AND r.ts>=? ORDER BY r.ts DESC LIMIT ?",
        (time.time() - min(dias, 90) * 86400, min(limite, 2000))).fetchall()
    def agrupar() -> list[dict]:  # agrupamento guloso por centroide; fora do event loop (até 2000 rostos)
        grupos: list[dict] = []
        for r in rows:
            emb = np.frombuffer(r["emb"], np.float32)
            melhor, sim = None, -1.0
            for g in grupos:
                s = float(emb @ g["_c"])
                if s > sim:
                    melhor, sim = g, s
            if melhor is not None and sim >= GRUPO_LIMIAR:
                melhor["_soma"] += emb
                melhor["_c"] = melhor["_soma"] / (np.linalg.norm(melhor["_soma"]) + 1e-9)
                melhor["rostos"].append(r)
            else:
                grupos.append({"_soma": emb.copy(), "_c": emb, "rostos": [r]})
        return grupos

    grupos = await asyncio.to_thread(agrupar)
    known = conhecidos()
    out = []
    for g in grupos:
        # sugestão: a pessoa cadastrada mais parecida com o centro do grupo, mesmo abaixo do limiar
        notas = ROSTOS.notas(g["_c"], known)
        sim, nome = notas[0] if notas else (-1.0, None)
        quem = {r["quem"] for r in g["rostos"] if r["quem"]}
        out.append({
            "id": g["rostos"][0]["id"],
            "rostos": [{"id": r["id"], "img": r["img"], "ts": r["ts"], "camera": r["camera"], "quem": r["quem"]}
                       for r in g["rostos"][:40]],
            "total": len(g["rostos"]),
            "sugestao": {"nome": nome, "sim": round(sim, 3),
                         "certeza": "alta" if sim >= ROSTOS.limiar else "duvida" if sim >= ROSTOS.limiar * 0.65 else "baixa"}
            if nome else None,
            "reconhecido_como": sorted(quem),
        })
    out.sort(key=lambda g: (-g["total"], -max(r["ts"] for r in g["rostos"])))
    return out


@app.post("/api/rostos/nomear-grupo")  # não usar /api/rostos/grupo/...: colide com /api/rostos/{rid}/nomear
async def nomear_grupo(payload: dict):
    ids = [int(i) for i in payload.get("ids", [])][:500]
    if not ids:
        raise HTTPException(400, "nenhum rosto selecionado")
    pid = _pessoa_id(payload.get("nome", ""))
    db.executemany("UPDATE rostos SET pessoa_id=?, ignorado=0, manual=1 WHERE id=?", [(pid, i) for i in ids])
    return {"ok": True, "pessoa_id": pid, "rostos": len(ids)}


@app.post("/api/rostos/descartar")
async def descartar_rostos(payload: dict):
    """Tira rostos da fila de identificação (não é rosto, foto ruim, desconhecido que não interessa)."""
    ids = [int(i) for i in payload.get("ids", [])][:500]
    db.executemany("UPDATE rostos SET ignorado=1 WHERE id=? AND pessoa_id IS NULL", [(i,) for i in ids])
    return {"ok": True, "rostos": len(ids)}


@app.post("/api/pessoas")
async def cadastrar_pessoa(nome: str = Form(...), fotos: list[UploadFile] = File(...)):
    pid, total, sem = _pessoa_id(nome), 0, []
    for f in fotos:
        img = cv2.imdecode(np.frombuffer(await f.read(), np.uint8), cv2.IMREAD_COLOR)
        rostos = await asyncio.to_thread(ROSTOS.encontrar, img, 40) if img is not None else []
        if len(rostos) != 1:
            sem.append(f"{f.filename}: {len(rostos)} rosto(s)")
            continue
        recorte = f"cadastro/{uuid.uuid4().hex[:10]}.jpg"
        (MIDIA / "cadastro").mkdir(exist_ok=True)
        cv2.imwrite(str(MIDIA / recorte), rostos[0]["recorte"])
        db.execute("INSERT INTO rostos(evento_id,pessoa_id,sim,emb,img,ts,manual,lado,qual) VALUES(NULL,?,1,?,?,?,1,?,?)",
                   (pid, rostos[0]["emb"].tobytes(), recorte, time.time(), rostos[0]["lado_real"],
                    ROSTOS.qualidade(rostos[0])))
        total += 1
    return {"pessoa_id": pid, "rostos_cadastrados": total, "ignoradas": sem}


@app.get("/api/pessoas/{pid}/rostos")
async def rostos_da_pessoa(pid: int):
    return [dict(r) for r in db.execute(
        "SELECT r.id, r.img, r.ts, r.evento_id, ROUND(r.sim, 3) AS sim, e.camera FROM rostos r "
        "LEFT JOIN eventos e ON e.id=r.evento_id WHERE r.pessoa_id=? ORDER BY r.ts DESC", (pid,)).fetchall()]


@app.post("/api/rostos/{rid}/remover")
async def remover_rosto(rid: int):
    """Tira um rosto do cadastro (foto ruim ou da pessoa errada): deixa de valer no reconhecimento."""
    db.execute("UPDATE rostos SET pessoa_id=NULL, ignorado=1 WHERE id=?", (rid,))
    return {"ok": True}


@app.delete("/api/pessoas/{pid}")
async def apagar_pessoa(pid: int):
    p = db.execute("SELECT nome FROM pessoas WHERE id=?", (pid,)).fetchone()
    if not p:
        raise HTTPException(404, "pessoa não existe")
    db.execute("UPDATE rostos SET pessoa_id=NULL WHERE pessoa_id=?", (pid,))
    # o nome não pode continuar valendo na aparência/passagem (dava "provável Fulano" de alguém apagado)
    db.execute("UPDATE rostos SET quem=NULL WHERE quem=?", (p["nome"],))
    db.execute("UPDATE corpos SET nome=NULL WHERE nome=?", (p["nome"],))
    db.execute("DELETE FROM pessoas WHERE id=?", (pid,))
    return {"ok": True}


ESTADO_PAINEL = ("robo", "robo_codigos", "mapa:robo", "mapa_nomes")  # chaves que o painel usa (index.html)


@app.get("/api/estado")
async def ver_estado():
    # só o que o painel lê: a tabela também guarda segredo_midia, casas, rede_vistos... que não podem sair daqui
    return {r["chave"]: {"valor": json.loads(r["valor"]), "ts": r["ts"]}
            for r in db.execute("SELECT * FROM estado").fetchall()
            if r["chave"].startswith("disp:") or r["chave"] in ESTADO_PAINEL}


CATALOGO = json.loads((Path(__file__).parent / "catalogo.json").read_text(encoding="utf-8"))
CATALOGO.pop("_doc", None)


@app.get("/api/catalogo")
async def catalogo():
    return CATALOGO


@app.get("/api/agente/catalogo", dependencies=[Depends(agente)])
async def catalogo_agente():
    return CATALOGO


def _enfileirar(alvo: str, acao: str, args) -> int:
    cid = db.execute("INSERT INTO comandos(ts,alvo,acao,args,status) VALUES(?,?,?,?, 'pendente')",
                     (time.time(), alvo, acao, json.dumps(args))).lastrowid
    _novo_comando.set()
    return cid


def _item(alvo: str, chave: str, ident: str) -> dict:
    disp = CATALOGO.get(alvo) or {}
    for g in disp.get("grupos", []):
        for it in g.get(chave, []):
            if it["id"] == ident:
                return it
    raise HTTPException(404, f"{ident} não está no catálogo de {alvo}")


@app.post("/api/disp/{alvo}/set")
async def disp_set(alvo: str, payload: dict):
    it = _item(alvo, "props", str(payload.get("id")))
    if it.get("so_leitura"):
        raise HTTPException(400, "propriedade só de leitura")
    siid, piid = (int(x) for x in it["id"].split("."))
    return {"id": _enfileirar(alvo, "set", {"siid": siid, "piid": piid, "valor": payload.get("valor")})}


@app.post("/api/disp/{alvo}/acao")
async def disp_acao(alvo: str, payload: dict):
    ident, args = str(payload.get("id")), payload.get("args")
    disp = CATALOGO.get(alvo) or {}
    it = next((a for g in disp.get("grupos", []) for a in g.get("acoes", [])
               if a["id"] == ident and (args is None or a.get("args", []) == args)), None)
    if it is None:
        raise HTTPException(404, f"ação {ident} não está no catálogo de {alvo}")
    siid, n = (int(x) for x in it["id"].split("."))
    if it.get("set"):  # "ação" que na spec é uma propriedade gravável (ex.: tocar som do robô)
        return {"id": _enfileirar(alvo, "set", {"siid": siid, "piid": n, "valor": it.get("valor")})}
    return {"id": _enfileirar(alvo, "acao", {"siid": siid, "aiid": n, "args": it.get("args", [])})}


PTZ_OPS = {"esquerda": 1, "direita": 2, "cima": 3, "baixo": 4, "calibrar": 5, "parar": -1001}


@app.post("/api/disp/{alvo}/ptz")
async def disp_ptz(alvo: str, payload: dict):
    if not (CATALOGO.get(alvo) or {}).get("ptz"):
        raise HTTPException(400, f"{alvo} não tem giro")
    direcao = payload.get("direcao")
    if direcao not in PTZ_OPS:
        raise HTTPException(400, f"direção inválida; use {sorted(PTZ_OPS)}")
    passo = max(0.2, min(float(payload.get("passo", 0.6)), 3.0))
    return {"id": _enfileirar(alvo, "ptz", {"op": PTZ_OPS[direcao], "passo": passo})}


@app.post("/api/disp/{alvo}/remoto")
async def disp_remoto(alvo: str, payload: dict):
    cr = (CATALOGO.get(alvo) or {}).get("controle_remoto")
    if not cr or payload.get("cmd") not in cr["comandos"]:
        raise HTTPException(400, "comando de controle remoto inválido")
    siid, piid = (int(x) for x in cr["id"].split("."))
    return {"id": _enfileirar(alvo, "set", {"siid": siid, "piid": piid, "valor": cr["comandos"][payload["cmd"]]})}


# ---- mapa do robô (o agente baixa da nuvem Xiaomi e manda só a imagem + cômodos) -------------------
MAPA_DIR = DADOS / "mapa"
MAPA_DIR.mkdir(exist_ok=True)


def _estado(chave: str, padrao=None):
    r = db.execute("SELECT valor, ts FROM estado WHERE chave=?", (chave,)).fetchone()
    return (json.loads(r["valor"]), r["ts"]) if r else (padrao, None)


@app.post("/api/agente/mapa", dependencies=[Depends(agente)])
async def receber_mapa(comodos: str = Form("[]"), tamanho: str = Form("[]"), imagem: UploadFile = File(...)):
    (MAPA_DIR / "robo.png.tmp").write_bytes(await imagem.read())
    (MAPA_DIR / "robo.png.tmp").replace(MAPA_DIR / "robo.png")
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('mapa:robo',?,?)",
               (json.dumps({"comodos": json.loads(comodos), "tamanho": json.loads(tamanho)}), time.time()))
    return {"ok": True}


@app.get("/mapa/robo.png")
async def imagem_mapa():
    p = MAPA_DIR / "robo.png"
    if not p.is_file():
        raise HTTPException(404, "o mapa ainda não chegou")
    return FileResponse(p, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/mapa")
async def ver_mapa():
    m, ts = _estado("mapa:robo", {"comodos": [], "tamanho": []})
    nomes, _ = _estado("mapa_nomes", {})
    for c in m["comodos"]:
        c["nome_robo"] = c["nome"]
        c["nome"] = nomes.get(str(c["id"])) or c["nome"]
    return {**m, "ts": ts}


@app.post("/api/mapa/nome")
async def nomear_comodo(payload: dict):
    nomes, _ = _estado("mapa_nomes", {})
    nomes[str(int(payload["id"]))] = str(payload.get("nome", "")).strip()[:40]
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('mapa_nomes',?,?)", (json.dumps(nomes), time.time()))
    return {"ok": True}


@app.post("/api/robo/comodos")
async def limpar_comodos(payload: dict):
    ids = [int(i) for i in payload.get("ids", [])]
    if not ids:
        raise HTTPException(400, "escolha ao menos um cômodo")
    return {"id": _enfileirar("robo", "comodos", ids)}


@app.post("/api/robo/atualizar_mapa")
async def atualizar_mapa():
    return {"id": _enfileirar("robo", "atualizar_mapa", [])}


# ---- rotinas de limpeza (agenda na VPS; o agente executa) -----------------------------------------
# config -> propriedade MIoT do c101 (mesmos valores do catálogo)
CONFIG_ROBO = {"modo": (2, 4, range(4)), "succao": (7, 5, range(4)), "agua": (7, 6, range(3)),
               "repetir": (7, 1, range(2)), "trajeto": (7, 7, range(2))}
DIAS_PT = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def _rotina_ok(r: dict) -> dict:
    """Valida e normaliza uma rotina vinda do painel ou da IA."""
    nome = str(r.get("nome") or "Rotina").strip()[:60]
    try:
        dias = sorted({int(d) for d in r.get("dias", []) if 0 <= int(d) <= 6})
    except (ValueError, TypeError):
        raise HTTPException(400, "dias: use números de 0 (seg) a 6 (dom)")
    hora = str(r.get("hora", "")).strip()
    try:
        hh, mm = (int(x) for x in hora.split(":"))
        assert 0 <= hh <= 23 and 0 <= mm <= 59
    except (ValueError, AssertionError):
        raise HTTPException(400, f"hora inválida: {hora!r} (use HH:MM)")
    if not dias:
        raise HTTPException(400, "escolha ao menos um dia")
    mapa, _ = _estado("mapa:robo", {"comodos": []})
    validos = {c["id"] for c in mapa["comodos"]}
    try:
        comodos = [int(c) for c in r.get("comodos", []) if not validos or int(c) in validos]
        config = {}
        for k, (_, _, faixa) in CONFIG_ROBO.items():
            v = (r.get("config") or {}).get(k, r.get(k))
            if v is not None and v != "" and int(v) in faixa:
                config[k] = int(v)
    except (ValueError, TypeError):  # valor que não é número (do painel ou da IA): erro do pedido, não do servidor
        raise HTTPException(400, "cômodos e ajustes do robô precisam ser números")
    return {"nome": nome, "ativo": bool(r.get("ativo", True)), "dias": dias, "hora": f"{hh:02d}:{mm:02d}",
            "comodos": comodos, "config": config}


def _rotinas() -> list[dict]:
    out = []
    for r in db.execute("SELECT * FROM rotinas ORDER BY hora, id").fetchall():
        d = dict(r)
        d.update(ativo=bool(d["ativo"]), dias=json.loads(d["dias"]), comodos=json.loads(d["comodos"]),
                 config=json.loads(d["config"]))
        out.append(d)
    return out


def _salvar_rotina(r: dict, rid: int | None = None) -> int:
    v = (r["nome"], int(r["ativo"]), json.dumps(r["dias"]), r["hora"], json.dumps(r["comodos"]), json.dumps(r["config"]))
    if rid:
        if not db.execute("UPDATE rotinas SET nome=?,ativo=?,dias=?,hora=?,comodos=?,config=? WHERE id=?", (*v, rid)).rowcount:
            raise HTTPException(404, f"rotina {rid} não existe")
        return rid
    return db.execute("INSERT INTO rotinas(nome,ativo,dias,hora,comodos,config,criado) VALUES(?,?,?,?,?,?,?)",
                      (*v, time.time())).lastrowid


def _agente_online() -> bool:
    _, ts = _estado("robo")
    return bool(ts) and time.time() - ts < 180


def executar_rotina(r: dict) -> list[int]:
    """Enfileira: ajustes (modo, sucção, água...) e depois a limpeza (cômodos ou casa toda)."""
    ids = []
    for k, v in r["config"].items():
        siid, piid, _ = CONFIG_ROBO[k]
        ids.append(_enfileirar("robo", "set", {"siid": siid, "piid": piid, "valor": v}))
    ids.append(_enfileirar("robo", "comodos", r["comodos"]) if r["comodos"]
               else _enfileirar("robo", "acao", {"siid": 2, "aiid": 1, "args": []}))
    if r.get("id"):
        db.execute("UPDATE rotinas SET ultima=? WHERE id=?", (time.time(), r["id"]))
    return ids


async def agendador_passo():
    agora = datetime.now()
    hhmm, dia = agora.strftime("%H:%M"), agora.weekday()
    for r in _rotinas():
        if not (r["ativo"] and dia in r["dias"] and r["hora"] == hhmm):
            continue
        if r["ultima"] and time.time() - r["ultima"] < 120:
            continue  # já disparou neste minuto
        if not _agente_online():
            db.execute("UPDATE rotinas SET ultima=? WHERE id=?", (time.time(), r["id"]))
            await avisar(f"Rotina não executada: {r['nome']}", "O PC de casa (agente) está offline; o robô não recebeu o comando.", 4, "warning")
            continue
        executar_rotina(r)
        await avisar(f"Robson: {r['nome']}", "Limpeza agendada iniciada.", 2, "broom")


@app.get("/api/rotinas")
async def listar_rotinas():
    return _rotinas()


@app.post("/api/rotinas")
async def criar_rotina(payload: dict):
    return {"id": _salvar_rotina(_rotina_ok(payload))}


@app.put("/api/rotinas/{rid}")
async def alterar_rotina(rid: int, payload: dict):
    return {"id": _salvar_rotina(_rotina_ok(payload), rid)}


@app.delete("/api/rotinas/{rid}")
async def apagar_rotina(rid: int):
    db.execute("DELETE FROM rotinas WHERE id=?", (rid,))
    return {"ok": True}


@app.post("/api/rotinas/{rid}/executar")
async def executar_agora(rid: int):
    r = next((x for x in _rotinas() if x["id"] == rid), None)
    if r is None:
        raise HTTPException(404)
    if not _agente_online():
        raise HTTPException(409, "o PC de casa (agente) está offline")
    return {"comandos": executar_rotina(r)}


@app.post("/api/robo/ia")
async def robo_ia(payload: dict):
    """A IA traduz o pedido em operações sobre as rotinas. Nada é aplicado aqui: o painel mostra e confirma."""
    mapa, _ = _estado("mapa:robo", {"comodos": []})
    nomes, _ = _estado("mapa_nomes", {})
    comodos = [{"id": c["id"], "nome": nomes.get(str(c["id"])) or c["nome"]} for c in mapa["comodos"]]
    contexto = {"agora": datetime.now().strftime("%A %d/%m/%Y %H:%M"), "comodos": comodos, "rotinas": _rotinas()}
    return await ia.planejar_robo(str(payload.get("texto", ""))[:2000], contexto)


@app.post("/api/robo/ia/aplicar")
async def robo_ia_aplicar(payload: dict):
    feitos = []
    for op in payload.get("operacoes", []):
        tipo, rid = op.get("tipo"), int(op.get("id") or 0)
        if tipo == "criar":
            feitos.append(f"criada #{_salvar_rotina(_rotina_ok(op['rotina']))}")
        elif tipo == "alterar" and rid:
            feitos.append(f"alterada #{_salvar_rotina(_rotina_ok(op['rotina']), rid)}")
        elif tipo == "apagar" and rid:
            db.execute("DELETE FROM rotinas WHERE id=?", (rid,))
            feitos.append(f"apagada #{rid}")
        elif tipo == "executar_agora":
            if not _agente_online():
                raise HTTPException(409, "o PC de casa (agente) está offline")
            r = _rotina_ok({**op["rotina"], "dias": [0], "hora": "00:00"})
            executar_rotina(r)
            feitos.append("limpeza iniciada agora")
    return {"feito": feitos}


ACOES_ROBO = {"limpar", "parar", "base", "lavar_mop", "secar_mop"}


@app.post("/api/robo")
async def robo(payload: dict):
    acao = payload.get("acao")
    if acao not in ACOES_ROBO:
        raise HTTPException(400, f"ação inválida; use {sorted(ACOES_ROBO)}")
    cid = db.execute("INSERT INTO comandos(ts,alvo,acao,args,status) VALUES(?,?,?,?, 'pendente')",
                     (time.time(), "robo", acao, json.dumps(payload.get("args", [])))).lastrowid
    _novo_comando.set()
    return {"id": cid}


@app.get("/api/comandos")
async def ver_comandos():
    return [dict(r) for r in db.execute("SELECT * FROM comandos ORDER BY id DESC LIMIT 20").fetchall()]


def eventos_texto(desde: float, cams: list | None = None) -> str:
    linhas = []
    filtro, extra = (f" AND {_em(cams)}", cams) if cams is not None else ("", [])
    for r in db.execute("SELECT * FROM eventos WHERE ts>=?" + filtro + " ORDER BY ts", (desde, *extra)).fetchall():
        quem = ", ".join(json.loads(r["conhecidos"] or "[]")) or "-"
        linhas.append(f"{datetime.fromtimestamp(r['ts']):%d/%m %H:%M} | {nome_cam(r['camera'])} | {r['tipo']} | "
                      f"conhecidos: {quem} | estranhos: {r['desconhecidos']} | {r['descricao'] or ''}")
    return "\n".join(linhas)


@app.post("/api/perguntar")
async def perguntar(request: Request, payload: dict):
    dias = min(int(payload.get("dias", 7)), 30)
    casa = str(payload.get("casa", ""))
    # casa vazia: admin vê tudo (None), os outros só as casas deles; casa que não é dele -> 403
    txt = eventos_texto(time.time() - dias * 86400, cams_permitidas(request.state.usuario, casa))
    if not txt:
        return {"resposta": f"Não há eventos nos últimos {dias} dias."}
    return {"resposta": await ia.sobre_historico(payload.get("pergunta", ""), txt)}


# ---- gravações (escritas pelo serviço "gravador": /rec/<camera>/AAAA-MM-DD_HH-MM-SS.mp4) ------------
REC = Path(os.environ.get("REC", "/rec"))


def _inicio(p: Path) -> float:
    return datetime.strptime(p.stem, "%Y-%m-%d_%H-%M-%S").timestamp()


def _pode_gravacao(request: Request, camera: str) -> None:
    if not camera_permitida(request.state.usuario, camera):
        raise HTTPException(404)


@app.get("/api/gravacoes")
async def listar_gravacoes(request: Request, camera: str, dia: str = ""):
    _pode_gravacao(request, camera)
    dia = dia or datetime.now().strftime("%Y-%m-%d")
    pasta = REC / Path(camera).name
    trechos = []
    do_cartao = {r[0] for r in db.execute("SELECT arquivo FROM trechos_cartao WHERE camera=?", (pasta.name,))}
    for p in sorted(pasta.glob(f"{dia}_*.mp4")):
        st = p.stat()
        if st.st_size < 4096:
            continue
        arq = f"{pasta.name}/{p.name}"
        trechos.append({"arquivo": arq, "inicio": _inicio(p), "fim": st.st_mtime,
                        "mb": round(st.st_size / 1e6, 1), "cartao": arq in do_cartao})
    dias = sorted({p.stem[:10] for p in pasta.glob("*.mp4")}, reverse=True)
    return {"camera": camera, "dia": dia, "trechos": trechos, "dias": dias}


@app.get("/api/gravacoes/em")
async def gravacao_em(request: Request, camera: str, ts: float):
    """Trecho que contém o instante ts (para abrir a gravação a partir de um evento)."""
    _pode_gravacao(request, camera)
    pasta = REC / Path(camera).name
    # só os trechos do dia de ts e o último do dia anterior (pode cruzar a meia-noite): antes fazia stat em todos
    dia = datetime.fromtimestamp(ts)
    cands = sorted(pasta.glob(f"{dia:%Y-%m-%d}_*.mp4"), reverse=True)
    anterior = sorted(pasta.glob(f"{dia - timedelta(days=1):%Y-%m-%d}_*.mp4"))
    if anterior:
        cands.append(anterior[-1])
    for p in cands:
        ini = _inicio(p)
        if ini <= ts <= p.stat().st_mtime + 1:
            return {"arquivo": f"{pasta.name}/{p.name}", "inicio": ini, "offset": max(0.0, ts - ini - 10)}
    raise HTTPException(404, "sem gravação nesse horário")


@app.get("/gravacoes/{caminho:path}")
async def gravacao_arquivo(request: Request, caminho: str):
    # a permissão é conferida na pasta REAL do arquivo (depois do resolve): "garagem/../rancho1/x.mp4" não passa
    raiz = REC.resolve()
    p = (REC / caminho).resolve()
    if not p.is_relative_to(raiz) or p.parent.parent != raiz or p.suffix != ".mp4" or not p.is_file():
        raise HTTPException(404)
    _pode_gravacao(request, p.parent.name)
    return FileResponse(p, media_type="video/mp4")


@app.get("/saude")
async def saude():
    # rota pública: só conta (conhecidos() carregaria todos os embeddings a cada chamada)
    return {"ok": True, "ia": bool(ia.client()), "ntfy": bool(NTFY_TOKEN),
            "pessoas": db.execute("SELECT COUNT(*) FROM pessoas").fetchone()[0]}


# ---- login e permissões (perfis) ---------------------------------------------------------------------
# sem sessão: só estas (cada uma tem o próprio token, ou é a própria tela de login / arquivos do app instalável)
LIVRES = ("/api/agente/", "/api/acao/", "/api/ponte/parear", "/api/ponte/estado", "/api/ponte/cartao", "/api/auth/ingest",
          "/login", "/api/login", "/saude", "/favicon.ico", "/sw.js", "/manifest.webmanifest", "/icone-",
          "/api/push/recebido")
# rota -> permissões que liberam (qualquer uma). Vazio = basta estar logado. Rota fora da lista exige "admin".
REGRAS_PERM = [
    (None, r"/|/api/eu|/api/eu/senha|/api/logout|/api/push/.*|/static/[\w.-]+\.js", set()),
    ("GET", r"/api/auth/aovivo|/api/aovivo/estado", {"aovivo"}),
    ("GET", r"/api/eventos|/midia/.*", {"eventos"}),
    ("POST", r"/api/eventos/\d+/identificar|/api/rostos/\d+/nomear", {"identificar"}),
    ("GET", r"/api/pessoas", {"identificar", "pessoas"}),
    (None, r"/api/pessoas.*|/api/rostos/.*|/api/presenca.*", {"pessoas"}),
    ("GET", r"/api/gravacoes(/em)?|/gravacoes/.*", {"gravacoes"}),
    ("GET", r"/api/alarme", {"eventos", "alarme"}),
    ("POST", r"/api/alarme", {"alarme"}),
    ("POST", r"/api/disp/[^/]+/ptz", {"ptz"}),
    (None, r"/api/disp/robo/.*|/api/robo.*|/api/rotinas.*|/api/mapa.*|/mapa/.*", {"robo"}),
    (None, r"/api/disp/.*", {"ajustes"}),
    ("GET", r"/api/catalogo|/api/estado|/api/comandos", {"robo", "ajustes"}),
    (None, r"/api/perguntar|/api/ia/uso", {"perguntar"}),
]
_REGRAS = [(m, re.compile(rx), perms) for m, rx, perms in REGRAS_PERM]


def permissoes_da_rota(metodo: str, caminho: str) -> set:
    for m, rx, perms in _REGRAS:
        if (m is None or m == metodo) and rx.fullmatch(caminho):
            return perms
    return {"admin"}


@app.middleware("http")
async def exigir_login(request: Request, chamar):
    p = request.url.path
    if p.startswith(LIVRES):
        return await chamar(request)
    q = request.query_params
    if p.startswith("/midia/") and assinatura_ok(p[len("/midia/"):], q.get("ate", ""), q.get("s", "")):
        request.state.usuario = {"admin": True, "perms": set(), "casas": ["*"]}  # foto da notificação (link assinado)
        return await chamar(request)
    u = acesso.usuario_da_sessao(db, request.cookies.get(acesso.COOKIE))
    if u is None:
        if request.method == "GET" and not p.startswith(("/api/", "/midia/", "/gravacoes/", "/mapa/")):
            return RedirectResponse("/login" + (f"?volta={urllib.parse.quote(p)}" if p != "/" else ""), 303)
        return JSONResponse({"detail": "sessão expirada: entre de novo"}, 401)
    if not u["admin"]:
        precisa = permissoes_da_rota(request.method, p)
        if precisa and not precisa & u["perms"]:
            return JSONResponse({"detail": "seu perfil não permite isto"}, 403)
        alvo = re.match(r"/api/disp/([^/]+)/", p)
        if alvo and alvo.group(1) in CAM_CASA and not camera_permitida(u, alvo.group(1)):
            return JSONResponse({"detail": "câmera de outra casa"}, 403)
        if precisa == {"robo"} and not any(CASAS[c].get("robo") for c in casas_do(u)):
            return JSONResponse({"detail": "nenhuma casa sua tem robô"}, 403)
    request.state.usuario = u
    return await chamar(request)


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@app.get("/login", response_class=HTMLResponse)
async def tela_login(request: Request):
    if acesso.usuario_da_sessao(db, request.cookies.get(acesso.COOKIE)):
        return RedirectResponse("/", 303)
    return (STATIC / "login.html").read_text(encoding="utf-8")


@app.post("/api/login")
async def entrar(request: Request, payload: dict):
    login, senha, ip = str(payload.get("usuario", "")).strip().lower()[:60], str(payload.get("senha", "")), _ip(request)
    ua = request.headers.get("user-agent", "")
    espera = acesso.bloqueado(login, ip)
    if espera:
        acesso.registrar_acesso(db, login, ip, False, ua)
        raise HTTPException(429, f"Muitas tentativas erradas. Tente de novo em {max(1, round(espera / 60))} min.")
    u = await asyncio.to_thread(acesso.conferir, db, login, senha, ip)
    acesso.registrar_acesso(db, login, ip, bool(u), ua)
    if not u:
        log.warning("login recusado: %r de %s", login, ip)
        raise HTTPException(401, "Usuário ou senha incorretos (ou usuário desativado).")
    lembrar = bool(payload.get("lembrar"))
    token, dur = acesso.abrir_sessao(db, u["id"], lembrar, ip, ua)
    log.info("login: %s de %s", login, ip)
    r = JSONResponse({"ok": True})
    r.set_cookie(acesso.COOKIE, token, max_age=dur if lembrar else None, httponly=True, secure=True,
                 samesite="lax", path="/")
    return r


@app.post("/api/logout")
async def sair(request: Request):
    acesso.fechar_sessao(db, request.cookies.get(acesso.COOKIE))
    r = JSONResponse({"ok": True})
    r.delete_cookie(acesso.COOKIE, path="/")
    return r


def _perms(u: dict) -> set:
    return set(acesso.PERMISSOES) if u["admin"] else u["perms"]


@app.get("/api/eu")
async def eu(request: Request):
    u = request.state.usuario
    perms = _perms(u)
    casas = [{"id": c, "nome": CASAS[c]["nome"], "cameras": CASAS[c]["cameras"], "robo": bool(CASAS[c].get("robo")),
              "ptz": CASAS[c].get("ptz", []) if "ptz" in perms else [], "ajustes": CASAS[c].get("ajustes", []),
              "ponte": "ponte" in CASAS[c], "presenca": bool(CASAS[c].get("presenca"))} for c in casas_do(u)]
    return {"login": u["login"], "nome": u["nome"], "perfil": u.get("perfil"), "admin": u["admin"],
            "perms": sorted(perms), "casas": casas}


@app.post("/api/eu/senha")
async def trocar_senha(request: Request, payload: dict):
    u = request.state.usuario
    nova = str(payload.get("nova", ""))
    if len(nova) < 8:
        raise HTTPException(400, "A senha nova precisa ter pelo menos 8 caracteres.")
    if not await asyncio.to_thread(acesso.conferir, db, u["login"], str(payload.get("atual", "")), _ip(request)):
        raise HTTPException(400, "A senha atual não confere.")
    db.execute("UPDATE usuarios SET hash=? WHERE id=?", (await asyncio.to_thread(acesso.gerar_hash, nova), u["id"]))
    acesso.fechar_todas(db, u["id"], exceto=request.cookies.get(acesso.COOKIE))
    return {"ok": True}


# ---- Admin: perfis -----------------------------------------------------------------------------------
def _perfil(pid) -> sqlite3.Row:
    r = db.execute("SELECT * FROM perfis WHERE id=?", (pid,)).fetchone()
    if not r:
        raise HTTPException(400, "perfil não existe")
    return r


def _admins_ativos(sem_uid: int = 0) -> int:
    return sum(1 for r in db.execute("SELECT u.id, p.permissoes FROM usuarios u JOIN perfis p ON p.id=u.perfil_id "
                                     "WHERE COALESCE(u.ativo,1)=1 AND u.id<>?", (sem_uid,)).fetchall()
               if "admin" in json.loads(r["permissoes"] or "[]"))


@app.get("/api/perfis")
async def listar_perfis():
    return {"permissoes": acesso.PERMISSOES, "perfis": [
        {"id": r["id"], "nome": r["nome"], "permissoes": json.loads(r["permissoes"] or "[]"), "fixo": bool(r["fixo"]),
         "usuarios": db.execute("SELECT COUNT(*) FROM usuarios WHERE perfil_id=?", (r["id"],)).fetchone()[0]}
        for r in db.execute("SELECT * FROM perfis ORDER BY fixo DESC, nome").fetchall()]}


def _dados_perfil(payload: dict) -> tuple[str, str]:
    nome = str(payload.get("nome", "")).strip()[:40]
    if not nome:
        raise HTTPException(400, "Dê um nome ao perfil.")
    perms = [x for x in payload.get("permissoes", []) if x in acesso.PERMISSOES]
    return nome, json.dumps(perms)


@app.post("/api/perfis")
async def criar_perfil(payload: dict):
    nome, perms = _dados_perfil(payload)
    if db.execute("SELECT 1 FROM perfis WHERE nome=?", (nome,)).fetchone():
        raise HTTPException(400, "Já existe um perfil com esse nome.")
    return {"id": db.execute("INSERT INTO perfis(nome,permissoes) VALUES(?,?)", (nome, perms)).lastrowid}


@app.put("/api/perfis/{pid}")
async def alterar_perfil(pid: int, payload: dict):
    if _perfil(pid)["fixo"]:
        raise HTTPException(400, "O perfil Administrador tem todas as permissões e não muda.")
    nome, perms = _dados_perfil(payload)
    db.execute("UPDATE perfis SET nome=?, permissoes=? WHERE id=?", (nome, perms, pid))
    return {"ok": True}


@app.delete("/api/perfis/{pid}")
async def apagar_perfil(pid: int):
    if _perfil(pid)["fixo"]:
        raise HTTPException(400, "O perfil Administrador não pode ser apagado.")
    if db.execute("SELECT 1 FROM usuarios WHERE perfil_id=?", (pid,)).fetchone():
        raise HTTPException(400, "Há usuários com este perfil: troque o perfil deles antes.")
    db.execute("DELETE FROM perfis WHERE id=?", (pid,))
    return {"ok": True}


# ---- Admin: usuários -----------------------------------------------------------------------------------
def _dados_usuario(payload: dict, novo: bool) -> dict:
    out = {}
    if novo:
        login = str(payload.get("login", "")).strip().lower()
        if not re.fullmatch(r"[a-z0-9._-]{2,30}", login):
            raise HTTPException(400, "Usuário: 2 a 30 letras minúsculas, números, ponto, hífen ou sublinhado.")
        out["login"] = login
    if "nome" in payload or novo:
        out["nome"] = str(payload.get("nome", "")).strip()[:60] or out.get("login", "")
    if "casas" in payload or novo:
        casas = [c for c in payload.get("casas", []) if c in CASAS or c == "*"]
        if not casas:
            raise HTTPException(400, "Escolha ao menos uma casa.")
        out["casas"] = json.dumps(casas)
    if "perfil_id" in payload or novo:
        pf = _perfil(payload.get("perfil_id"))
        out["perfil_id"] = pf["id"]
        out["admin"] = int("admin" in json.loads(pf["permissoes"] or "[]"))
    if "ativo" in payload:
        out["ativo"] = int(bool(payload["ativo"]))
    senha = payload.get("senha")
    if senha or novo:
        if len(str(senha or "")) < 8:
            raise HTTPException(400, "A senha precisa ter pelo menos 8 caracteres.")
        out["hash"] = acesso.gerar_hash(str(senha))
    return out


@app.get("/api/usuarios")
async def listar_usuarios():
    agora = time.time()
    return [{**dict(r), "casas": json.loads(r["casas"] or "[]"), "ativo": r["ativo"] != 0,
             "sessoes": db.execute("SELECT COUNT(*) FROM sessoes WHERE uid=? AND expira>?", (r["id"], agora)).fetchone()[0],
             "avisos": db.execute("SELECT COUNT(*) FROM push_subs WHERE uid=?", (r["id"],)).fetchone()[0]}
            for r in db.execute("SELECT u.id, u.login, u.nome, u.casas, u.admin, u.criado, u.ultimo, u.ativo, u.perfil_id, "
                                "p.nome AS perfil FROM usuarios u LEFT JOIN perfis p ON p.id=u.perfil_id "
                                "ORDER BY u.admin DESC, u.nome").fetchall()]


@app.post("/api/usuarios")
async def criar_usuario(payload: dict):
    d = await asyncio.to_thread(_dados_usuario, payload, True)
    if db.execute("SELECT 1 FROM usuarios WHERE login=?", (d["login"],)).fetchone():
        raise HTTPException(400, "Já existe um usuário com esse login.")
    cur = db.execute("INSERT INTO usuarios(login,nome,hash,casas,admin,perfil_id,ativo,criado) VALUES(?,?,?,?,?,?,1,?)",
                     (d["login"], d["nome"], d["hash"], d["casas"], d["admin"], d["perfil_id"], time.time()))
    return {"id": cur.lastrowid}


@app.put("/api/usuarios/{uid}")
async def alterar_usuario(request: Request, uid: int, payload: dict):
    d = await asyncio.to_thread(_dados_usuario, payload, False)
    perde_admin = d.get("admin") == 0 or d.get("ativo") == 0
    if perde_admin and uid == request.state.usuario["id"]:
        raise HTTPException(400, "Você não pode tirar o seu próprio acesso de administrador nem se desativar.")
    if perde_admin and not _admins_ativos(sem_uid=uid):
        raise HTTPException(400, "Precisa sobrar pelo menos um administrador ativo.")
    if d:
        db.execute(f"UPDATE usuarios SET {', '.join(k + '=?' for k in d)} WHERE id=?", (*d.values(), uid))
    if {"hash", "casas", "perfil_id", "ativo"} & set(d):  # muda o que pode: encerra as sessões abertas dele
        acesso.fechar_todas(db, uid, exceto=request.cookies.get(acesso.COOKIE) if uid == request.state.usuario["id"] else None)
    if d.get("ativo") == 0:
        db.execute("DELETE FROM push_subs WHERE uid=?", (uid,))
    return {"ok": True}


@app.delete("/api/usuarios/{uid}")
async def apagar_usuario(request: Request, uid: int):
    if uid == request.state.usuario["id"]:
        raise HTTPException(400, "Você não pode apagar o seu próprio usuário.")
    if not _admins_ativos(sem_uid=uid):
        raise HTTPException(400, "Precisa sobrar pelo menos um administrador ativo.")
    db.execute("DELETE FROM usuarios WHERE id=?", (uid,))
    db.execute("DELETE FROM push_subs WHERE uid=?", (uid,))
    acesso.fechar_todas(db, uid)
    return {"ok": True}


# ---- Admin: sessões abertas e histórico de acessos ---------------------------------------------------------
@app.get("/api/admin/acessos")
async def ver_acessos(request: Request):
    atual = acesso.token_hash(request.cookies.get(acesso.COOKIE) or "")
    sess = [{"id": r["hash"][:16], "login": r["login"], "nome": r["nome"], "criado": r["criado"], "expira": r["expira"],
             "ip": r["ip"], "ua": r["ua"], "esta": r["hash"] == atual}
            for r in db.execute("SELECT s.*, u.login, u.nome FROM sessoes s JOIN usuarios u ON u.id=s.uid WHERE s.expira>? "
                                "ORDER BY s.criado DESC", (time.time(),)).fetchall()]
    hist = [dict(r) for r in db.execute("SELECT ts, login, ip, ok, ua FROM acessos ORDER BY id DESC LIMIT 80").fetchall()]
    return {"sessoes": sess, "acessos": hist}


@app.delete("/api/admin/sessoes/{sid}")
async def encerrar_sessao(sid: str):
    if not re.fullmatch(r"[0-9a-f]{16}", sid):
        raise HTTPException(400)
    db.execute("DELETE FROM sessoes WHERE substr(hash,1,16)=?", (sid,))
    return {"ok": True}


# ---- Caddy (forward_auth): ao vivo pelo login; vídeo de entrada pelo token do PC ou da ponte ----------
@app.get("/api/auth/aovivo")
async def auth_aovivo(request: Request):
    u = request.state.usuario
    if u["admin"]:
        return Response(status_code=204)
    uri = urllib.parse.urlparse(request.headers.get("x-forwarded-uri", ""))
    src = urllib.parse.parse_qs(uri.query).get("src", [""])[0]
    # sem src: só a página do player e os scripts dela (a lista de streams e o resto da API, não)
    ok = camera_permitida(u, src) if src else uri.path.endswith((".html", ".js"))
    return Response(status_code=204 if ok else 403)


def _ponte(authorization: str):
    if not authorization.startswith("Bearer "):
        return None
    return db.execute("SELECT * FROM pontes WHERE hash=?", (acesso.token_hash(authorization[7:].strip()),)).fetchone()


@app.get("/api/auth/ingest")
async def auth_ingest(request: Request):
    auth = request.headers.get("authorization", "")
    if _token_ok(auth, AGENTE_TOKEN):
        return Response(status_code=204)
    uri = urllib.parse.urlparse(request.headers.get("x-forwarded-uri", ""))
    dst = urllib.parse.parse_qs(uri.query).get("dst", [""])[0]
    p = _ponte(auth)
    if p and dst in CASAS.get(p["casa"], {}).get("cameras", {}):
        return Response(status_code=204)
    return Response(status_code=401)


# ---- pontes (celular numa casa sem PC): pareamento por código, sinal de vida, aviso se cair -----------
CODIGOS_PONTE: dict[str, dict] = {}   # código -> {casa, expira}; some ao reiniciar o app (gere outro)
_erros_ponte: list[float] = []
PONTE_OFFLINE = 300                   # s sem sinal de vida -> aviso
CAMERA_PARADA = 300                   # s com os bytes da câmera parados no celular -> aviso
CAMERA_ESTAVEL = 600                  # s com vídeo chegando sem parar antes de avisar "voltou" (câmera oscilando = 1 aviso só)
CAMERA_REAVISO = 3600                 # s entre dois avisos de "sem vídeo" da mesma câmera (02/11: Rancho 1 caía e voltava)
_parada: dict[tuple[int, str], dict] = {}  # (ponte, cam) -> bytes, desde (última mudança), ok_desde, avisado, ultimo_aviso


@app.post("/api/pontes/codigo")
async def codigo_ponte(payload: dict):
    casa = str(payload.get("casa", ""))
    if "ponte" not in CASAS.get(casa, {}):
        raise HTTPException(400, "essa casa não tem ponte em casas.json")
    codigo = f"{secrets.randbelow(10 ** 6):06d}"
    CODIGOS_PONTE.clear()  # um código por vez
    CODIGOS_PONTE[codigo] = {"casa": casa, "expira": time.time() + 900}
    return {"codigo": codigo, "expira": CODIGOS_PONTE[codigo]["expira"],
            "comando": f"apt update && yes | apt full-upgrade && curl -fsSL {PAINEL_URL}/ponte/instalar.sh | CODIGO={codigo} sh"}


@app.post("/api/ponte/parear")
async def parear_ponte(payload: dict):
    agora = time.time()
    _erros_ponte[:] = [t for t in _erros_ponte if agora - t < 900]
    if len(_erros_ponte) >= 10:
        CODIGOS_PONTE.clear()
        raise HTTPException(429, "muitas tentativas: gere um código novo no painel daqui a 15 min")
    c = CODIGOS_PONTE.get(str(payload.get("codigo", "")).strip())
    if not c or c["expira"] < agora:
        _erros_ponte.append(agora)
        raise HTTPException(401, "código inválido ou vencido (vale 15 min; gere outro no painel)")
    CODIGOS_PONTE.clear()
    token = secrets.token_urlsafe(32)
    nome = str(payload.get("aparelho", "celular"))[:60]
    # reinstalar o mesmo celular troca o registro (senão o antigo ficaria "sem sinal" e avisando)
    db.execute("DELETE FROM pontes WHERE casa=? AND nome=?", (c["casa"], nome))
    db.execute("INSERT INTO pontes(casa,nome,hash,criado,visto,info) VALUES(?,?,?,?,?,?)",
               (c["casa"], nome, acesso.token_hash(token), agora, agora, "{}"))
    log.info("ponte pareada: %s (%s)", nome, c["casa"])
    return {"token": token, "casa": c["casa"], "cameras": CASAS[c["casa"]]["ponte"]}


_versao_ponte: dict = {}


def versao_ponte() -> str:
    """Versão do ponte.py publicado em /ponte/ (a ponte se atualiza sozinha quando a dela é outra)."""
    arq = Path("/srv/ponte/ponte.py")
    try:
        mt = arq.stat().st_mtime
    except OSError:
        return ""
    if _versao_ponte.get("mt") != mt:
        m = re.search(r'(?m)^VERSAO = "([^"]+)"', arq.read_text(encoding="utf-8"))
        _versao_ponte.update(mt=mt, v=m.group(1) if m else "")
    return _versao_ponte["v"]


def urls_ponte(casa: str) -> dict:
    """Endereços xiaomi:// das câmeras da casa com o IP que a nuvem Xiaomi informa agora (lista que o PC publica).
    O go2rtc só procura a câmera nesse IP, na rede local: quando o roteador dá outro IP, o endereço fixo do Admin
    deixa de funcionar ("read udp: i/o timeout", Rancho 2 em 02/11). Grava a mudança em casas e avisa."""
    urls = dict(CASAS.get(casa, {}).get("ponte", {}))
    ips = {str(a.get("did")): a.get("localip") for a in _ler_estado("xiaomi_aparelhos", []) if a.get("localip")}
    mudou = {}
    for cam, url in urls.items():
        u = urllib.parse.urlsplit(url)
        did = urllib.parse.parse_qs(u.query).get("did", [""])[0]
        novo = ips.get(did)
        if novo and u.hostname and novo != u.hostname:
            mudou[cam] = (u.hostname, novo)
            # netloc montado de novo (não str.replace: o IP antigo poderia aparecer também na senha)
            cred = f"{u.username or ''}:{u.password or ''}@" if u.username or u.password else ""
            porta = f":{u.port}" if u.port else ""
            urls[cam] = urllib.parse.urlunsplit(u._replace(netloc=f"{cred}{novo}{porta}"))
    if mudou:
        CASAS[casa]["ponte"] = urls
        db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('casas',?,?)", (json.dumps(CASAS), time.time()))
        for cam, (de, para) in mudou.items():
            log.warning("%s: IP mudou de %s para %s (nuvem Xiaomi); ponte avisada", cam, de, para)
    return urls


def _rede24(ip: str) -> str:
    return ".".join(str(ip).split(".")[:3]) if str(ip).count(".") == 3 else ""


def outra_rede(casa: str, cam: str, info: dict) -> tuple[str, str] | None:
    """(IP da câmera, IP do celular) quando estão em sub-redes /24 diferentes. Visto 02–03/11: a Rancho 2, no
    mesmo Wi-Fi do Deco, recebeu IP do modem (192.168.1.x) porque o satélite e o modem estão no mesmo switch —
    o app Xiaomi continua funcionando (nuvem) e a ponte não alcança a câmera."""
    url = CASAS.get(casa, {}).get("ponte", {}).get(cam, "")
    cam_ip = urllib.parse.urlsplit(url).hostname or ""
    cel_ip = (info.get("rede") or {}).get("ip", "")
    if _rede24(cam_ip) and _rede24(cel_ip) and _rede24(cam_ip) != _rede24(cel_ip):
        return cam_ip, cel_ip
    return None


def urls_para_ponte(casa: str, info: dict) -> dict:
    """Endereços que vão para o celular: câmera em outra sub-rede vai direto pela internet (servidores CS2, go2rtc
    com o patch de 03/11) em vez de perder 5 s procurando na rede local. O resto tenta a rede local e, se falhar,
    a internet sozinho."""
    urls = urls_ponte(casa)
    for cam, url in urls.items():
        if outra_rede(casa, cam, info) and "p2p=" not in url:
            urls[cam] = url + ("&" if "?" in url else "?") + "p2p=internet"
    return urls


def causa_outra_rede(cam: str, ips: tuple[str, str]) -> str:
    return (f"{nome_cam(cam)} pegou o IP {ips[0]}, de outra rede (o celular está em {ips[1]}): o app Xiaomi funciona "
            "pela nuvem, mas o celular-ponte não alcança. Causa: um cabo junta a rede do modem com a do Deco (Deco "
            "satélite e modem no mesmo switch). Ligue o modem só na porta WAN do Deco principal e o switch numa "
            "porta LAN dele; depois tire a câmera da tomada por 10 s.")


ACOES_PONTE = {"recriar": "reconectar a câmera", "reiniciar_go2rtc": "reiniciar o go2rtc", "reiniciar_ponte": "reiniciar a ponte",
               "rede": "testar a rede", "log": "pedir o log"}


@app.post("/api/pontes/{pid}/comando")
async def comando_ponte(pid: int, payload: dict):
    acao, cam = str(payload.get("acao", "")), str(payload.get("camera", ""))
    p = db.execute("SELECT * FROM pontes WHERE id=?", (pid,)).fetchone()
    if not p or acao not in ACOES_PONTE:
        raise HTTPException(400, "ponte ou comando inválido")
    if acao == "recriar" and cam not in CASAS.get(p["casa"], {}).get("ponte", {}):
        raise HTTPException(400, "câmera não é dessa ponte")
    if (p["visto"] or 0) < time.time() - PONTE_OFFLINE:
        raise HTTPException(409, "a ponte está sem sinal: o comando só chega quando ela falar com a VPS")
    db.execute("DELETE FROM comandos_ponte WHERE ponte=? AND enviado IS NULL AND acao=? AND camera=?", (pid, acao, cam))
    cur = db.execute("INSERT INTO comandos_ponte(ponte,acao,camera,criado) VALUES(?,?,?,?)", (pid, acao, cam, time.time()))
    return {"id": cur.lastrowid, "aviso": "chega no próximo sinal de vida (até 1 min); o resultado aparece aqui depois"}


@app.post("/api/ponte/estado")
async def estado_ponte(payload: dict, authorization: str = Header("")):
    p = _ponte(authorization)
    if not p:
        raise HTTPException(401, "ponte desconhecida ou revogada")
    agora = time.time()
    for res in payload.get("resultados") or []:
        db.execute("UPDATE comandos_ponte SET ok=?, resultado=?, fim=? WHERE id=? AND ponte=?",
                   (int(bool(res.get("ok"))), str(res.get("texto", ""))[:4000], res.get("fim") or agora, res.get("id"), p["id"]))
    comandos = [dict(r) for r in db.execute("SELECT id, acao, camera FROM comandos_ponte WHERE ponte=? AND enviado IS NULL "
                                            "AND criado > ?", (p["id"], agora - 900))]
    if comandos:
        db.execute(f"UPDATE comandos_ponte SET enviado=? WHERE id IN ({','.join('?' * len(comandos))})",
                   (agora, *[c["id"] for c in comandos]))
    db.execute("DELETE FROM comandos_ponte WHERE criado < ?", (agora - 7 * 86400,))
    for cam, c in (payload.get("cameras") or {}).items():
        k = (p["id"], cam)
        st = _parada.setdefault(k, {"bytes": -1, "desde": agora, "ok_desde": None, "avisado": False, "ultimo_aviso": 0.0})
        if c.get("bytes") != st["bytes"]:  # o "voltou" sai no vigia_pontes, depois de CAMERA_ESTAVEL s sem parar
            st.update(bytes=c.get("bytes"), desde=agora, ok_desde=st["ok_desde"] or agora)
        else:
            st["ok_desde"] = None
    info = json.dumps(payload)
    if len(info) > 12000:  # nunca fatiar o texto (JSON cortado quebra listar_pontes): descarta o log da ponte
        info = json.dumps({k: v for k, v in payload.items() if k != "log"})
    if len(info) > 12000:  # ainda grande: fica só o essencial
        info = json.dumps({k: payload.get(k) for k in ("versao", "bateria", "cameras", "go2rtc", "rede", "cartao", "atualizacao")})
    if payload.get("cartao_novo") and p["visto"] and agora - p["visto"] > 120:
        # ponte recém-atualizada (1.2) que ficou sem sinal: pede ao cartão o tempo desde o último sinal de vida
        for cam in CASAS.get(p["casa"], {}).get("ponte", {}):
            db.execute("INSERT INTO pedidos_cartao(ponte,camera,ini,fim) VALUES(?,?,?,?)", (p["id"], cam, p["visto"] - 60, agora))
        log.info("ponte %s: pedindo ao cartão o tempo sem sinal (%.0f min)", p["nome"], (agora - p["visto"]) / 60)
    db.execute("UPDATE pontes SET visto=?, info=? WHERE id=?", (agora, info, p["id"]))
    pedidos = [dict(r) for r in db.execute("SELECT camera, ini, fim FROM pedidos_cartao WHERE ponte=?", (p["id"],))]
    db.execute("DELETE FROM pedidos_cartao WHERE ponte=?", (p["id"],))
    return {"cameras": urls_para_ponte(p["casa"], payload), "cartao": pedidos, "versao": versao_ponte(), "comandos": comandos}


@app.post("/api/ponte/cartao")
async def cartao_ponte(request: Request, camera: str, inicio: float, duracao: float, authorization: str = Header("")):
    """A ponte manda (MPEG-TS, chunked) a gravação do cartão SD de um buraco do ao vivo. Vira um trecho comum de
    Gravações, com o nome do horário em que começa, marcado como "do cartão"."""
    p = _ponte(authorization)
    if not p or camera not in CASAS.get(p["casa"], {}).get("ponte", {}):
        raise HTTPException(401, "ponte desconhecida ou câmera de outra casa")
    if not 0 < duracao <= 1800 or not time.time() - 8 * 86400 < inicio < time.time():
        raise HTTPException(400, "intervalo inválido")
    pasta = REC / camera
    pasta.mkdir(exist_ok=True)
    ini = int(inicio)
    while (pasta / f"{datetime.fromtimestamp(ini):%Y-%m-%d_%H-%M-%S}.mp4").exists():  # mesmo segundo de um trecho
        ini += 1
    final = pasta / f"{datetime.fromtimestamp(ini):%Y-%m-%d_%H-%M-%S}.mp4"
    tmp = pasta / f".{final.stem}.parcial"  # fora do *.mp4 da lista até terminar
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "mpegts", "-i", "pipe:0", "-map", "0:v", "-c", "copy",
        "-t", f"{duracao + 5:.0f}", "-f", "mp4", "-movflags", "+faststart", "-y", str(tmp),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    recebido = 0
    try:
        async for b in request.stream():
            recebido += len(b)
            if proc.stdin.is_closing():
                continue  # o ffmpeg já tem a duração pedida: só esvazia o resto do corpo
            try:
                proc.stdin.write(b)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                proc.stdin.close()
    except Exception as e:  # noqa: BLE001 - ponte caiu no meio: guarda o que veio
        log.warning("cartão %s: envio interrompido (%s)", camera, e)
    finally:
        if not proc.stdin.is_closing():
            proc.stdin.close()
    try:
        _, erro = await asyncio.wait_for(proc.communicate(), 120)
    except asyncio.TimeoutError:
        proc.kill()
        erro = b"ffmpeg travou"
    segundos = 0.0
    if tmp.exists():
        pr = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(tmp),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await pr.communicate()
        try:
            segundos = float(out.decode().strip() or 0)
        except ValueError:
            segundos = 0.0
    if segundos < 3:
        tmp.unlink(missing_ok=True)
        log.warning("cartão %s: nada aproveitável (%d bytes; %s)", camera, recebido, (erro or b"").decode(errors="replace")[-200:])
        raise HTTPException(422, "nada aproveitável no vídeo do cartão")
    os.replace(tmp, final)
    os.utime(final, (ini + segundos, ini + segundos))  # a lista usa o mtime como fim do trecho
    db.execute("DELETE FROM trechos_cartao WHERE criado < ?", (time.time() - 9 * 86400,))
    db.execute("INSERT OR REPLACE INTO trechos_cartao VALUES(?,?,?,?,?)",
               (f"{camera}/{final.name}", camera, ini, ini + segundos, time.time()))
    log.info("cartão %s: %s gravado (%.0f s, %.1f MB)", camera, final.name, segundos, recebido / 1e6)
    return {"arquivo": f"{camera}/{final.name}", "segundos": segundos}


@app.get("/api/pontes")
async def listar_pontes():
    agora = time.time()
    return [{"id": r["id"], "casa": r["casa"], "nome": r["nome"], "criado": r["criado"], "visto": r["visto"],
             "online": agora - (r["visto"] or 0) < PONTE_OFFLINE, "info": json.loads(r["info"] or "{}"),
             "outra_rede": {cam: ips for cam in CASAS.get(r["casa"], {}).get("ponte", {})
                            if (ips := outra_rede(r["casa"], cam, json.loads(r["info"] or "{}")))},
             "versao_publicada": versao_ponte(), "urls": CASAS.get(r["casa"], {}).get("ponte", {}),
             "comandos": [dict(c) for c in db.execute(
                 "SELECT id, acao, camera, criado, enviado, ok, resultado, fim FROM comandos_ponte WHERE ponte=? "
                 "ORDER BY id DESC LIMIT 5", (r["id"],))]}
            for r in db.execute("SELECT * FROM pontes ORDER BY id").fetchall()]


@app.get("/api/aovivo/estado")
async def estado_aovivo(request: Request, casa: str = ""):
    """Por câmera de ponte: está mandando vídeo? Se não, desde quando e o motivo (o painel mostra no lugar do player)."""
    cams = cams_permitidas(request.state.usuario, casa)  # None = todas (admin sem filtro)
    agora, out = time.time(), {}
    for r in db.execute("SELECT id, casa, visto, info FROM pontes").fetchall():
        online = agora - (r["visto"] or 0) < PONTE_OFFLINE
        try:
            info = json.loads(r["info"] or "{}")
        except ValueError:
            info = {}
        for cam, c in (info.get("cameras") or {}).items():
            if cams is not None and cam not in cams:
                continue
            c = c or {}
            enviando = bool(online and c.get("enviando"))
            rede = outra_rede(r["casa"], cam, info)
            if not online:
                erro = "o celular-ponte está sem sinal (internet, energia ou Termux fechado)"
            elif rede:
                erro = (f"a câmera está em outra rede ({rede[0]}; o celular em {rede[1]}): o modem deu o IP a ela. "
                        "Tire a câmera da tomada por 10 s e confira os cabos do Deco")
            elif c.get("lan") is False:
                erro = "a câmera não responde na rede do celular (Wi-Fi, energia ou IP da câmera)"
            else:
                erro = str(c.get("erro") or "esperando a câmera")
            st = _parada.get((r["id"], cam))  # "desde": última vez que os bytes da câmera mudaram no celular
            out[cam] = {"enviando": enviando, "falhando_desde": None if enviando else (st["desde"] if st else r["visto"]),
                        "erro": "" if enviando else erro}
    return out


@app.delete("/api/pontes/{pid}")
async def revogar_ponte(pid: int):
    db.execute("DELETE FROM pontes WHERE id=?", (pid,))
    for k in [k for k in _parada if k[0] == pid]:  # senão o vigia seguiria olhando câmeras de ponte que não existe
        _parada.pop(k, None)
    return {"ok": True}


async def vigia_pontes():
    while True:
        await asyncio.sleep(60)
        try:
            agora = time.time()
            for r in db.execute("SELECT * FROM pontes").fetchall():
                nome_casa = CASAS.get(r["casa"], {}).get("nome", r["casa"])
                off = agora - (r["visto"] or 0) > PONTE_OFFLINE
                if off and not r["avisado"]:
                    await avisar(f"Ponte do {nome_casa.lower()} sem sinal",
                                 f"O {r['nome']} não fala com a VPS há {int((agora - r['visto']) / 60)} min "
                                 "(internet, energia ou Termux fechado). Sem vídeo, gravação e avisos de lá.", 4, "warning",
                                 casa=r["casa"])
                    db.execute("UPDATE pontes SET avisado=1 WHERE id=?", (r["id"],))
                elif not off and r["avisado"]:
                    await avisar(f"Ponte do {nome_casa.lower()} voltou", f"O {r['nome']} voltou a falar com a VPS.", 2,
                                 "white_check_mark", casa=r["casa"])
                    db.execute("UPDATE pontes SET avisado=0 WHERE id=?", (r["id"],))
                if off:
                    continue
                info_p = json.loads(r["info"] or "{}")
                cams_info = info_p.get("cameras") or {}
                for (pid, cam), st in list(_parada.items()):  # cópia: o await deixa estado_ponte mexer no dict
                    if pid != r["id"]:
                        continue
                    rede = outra_rede(r["casa"], cam, info_p)
                    if rede and st.get("aviso_rede") != rede[0] and agora - st["desde"] > 120:
                        # aviso próprio, uma vez por IP: a causa é o cabeamento, e não adianta esperar a câmera
                        st["aviso_rede"], st["avisado"], st["ultimo_aviso"] = rede[0], True, agora
                        await avisar(f"{nome_cam(cam)} em outra rede", causa_outra_rede(cam, rede), 4, "warning",
                                     casa=r["casa"])
                        continue
                    if not rede:
                        st.pop("aviso_rede", None)
                    if st["avisado"] and st["ok_desde"] and agora - st["ok_desde"] >= CAMERA_ESTAVEL:
                        await avisar(f"{nome_cam(cam)} voltou", "O vídeo voltou a chegar do celular e está estável há 10 min.",
                                     2, "white_check_mark", casa=r["casa"])
                        st["avisado"] = False
                    elif not st["avisado"] and agora - st["desde"] > CAMERA_PARADA and agora - st["ultimo_aviso"] > CAMERA_REAVISO:
                        st["avisado"], st["ultimo_aviso"] = True, agora
                        ci = cams_info.get(cam) or {}
                        # a ponte 1.7 diz se a câmera responde na rede do celular: a causa mais comum (02/11) é a
                        # câmera ter pegado outro IP ou outra rede Wi-Fi
                        causa = ("A câmera NÃO responde na rede local do celular: confira se ela está no mesmo Wi-Fi "
                                 "e com o mesmo IP." if ci.get("lan") is False else
                                 "Energia/Wi-Fi da câmera, ou falta entrar na conta Xiaomi no go2rtc do celular.")
                        erro = f"\nÚltimo erro: {ci['erro'][:160]}" if ci.get("erro") else ""
                        await avisar(f"{nome_cam(cam)} sem vídeo",
                                     f"A ponte está no ar mas a câmera não manda imagem há 5 min. {causa}{erro}", 4, "warning",
                                     casa=r["casa"])
        except Exception as e:  # noqa: BLE001
            log.warning("vigia das pontes: %s", e)


# ---- avisos no navegador (Web Push): cada um configura o próprio aparelho na aba Avisos ----------------------
def _nome_aparelho(ua: str) -> str:
    so = "iPhone" if "iPhone" in ua else "iPad" if "iPad" in ua else "Android" if "Android" in ua else \
        "Windows" if "Windows" in ua else "Mac" if "Mac OS" in ua else "Linux" if "Linux" in ua else "aparelho"
    nav = "Edge" if "Edg/" in ua else "Firefox" if "Firefox" in ua else "Samsung" if "SamsungBrowser" in ua else \
        "Chrome" if "Chrome" in ua else "Safari" if "Safari" in ua else ""
    return f"{so} {nav}".strip()


@app.get("/api/push/config")
async def push_config(request: Request):
    u = request.state.usuario
    perms = _perms(u)
    return {"chave": push.chave_publica(), "prefs": prefs_push(u.get("push_prefs")),
            "categorias": {c: t for c, t in CATEGORIAS.items() if PERM_CATEGORIA[c] in perms},
            "casas": [{"id": c, "nome": CASAS[c]["nome"]} for c in casas_do(u)],
            "aparelhos": [dict(r) for r in db.execute("SELECT id, aparelho, criado, ultimo, status, endpoint FROM push_subs "
                                                      "WHERE uid=? ORDER BY id", (u["id"],)).fetchall()],
            "ntfy": {"servidor": NTFY_URL_PUBLICA, "topicos": {c: CASAS[c].get("topico") for c in casas_do(u)}}}


NTFY_URL_PUBLICA = os.environ.get("NTFY_URL_PUBLICA", PAINEL_URL + ":8443")


@app.post("/api/push/inscrever")
async def push_inscrever(request: Request, payload: dict):
    insc = payload.get("inscricao") or {}
    ep, chaves = str(insc.get("endpoint", "")), insc.get("keys") or {}
    if not ep.startswith("https://") or not chaves.get("p256dh") or not chaves.get("auth"):
        raise HTTPException(400, "inscrição inválida")
    nome = str(payload.get("aparelho") or "").strip()[:60] or _nome_aparelho(request.headers.get("user-agent", ""))
    db.execute("INSERT INTO push_subs(uid,endpoint,chaves,aparelho,criado) VALUES(?,?,?,?,?) ON CONFLICT(endpoint) "
               "DO UPDATE SET uid=excluded.uid, chaves=excluded.chaves, aparelho=excluded.aparelho",
               (request.state.usuario["id"], ep, json.dumps({"p256dh": chaves["p256dh"], "auth": chaves["auth"]}),
                nome, time.time()))
    return {"ok": True, "aparelho": nome}


@app.post("/api/push/remover")
async def push_remover(request: Request, payload: dict):
    db.execute("DELETE FROM push_subs WHERE uid=? AND (endpoint=? OR id=?)",
               (request.state.usuario["id"], str(payload.get("endpoint", "")), int(payload.get("id") or 0)))
    return {"ok": True}


@app.put("/api/push/prefs")
async def push_prefs(request: Request, payload: dict):
    u = request.state.usuario
    casas = payload.get("casas")
    casas = None if casas is None else [c for c in casas if c in casas_do(u)]
    cats = {c: bool(v) for c, v in (payload.get("categorias") or {}).items() if c in CATEGORIAS}
    db.execute("UPDATE usuarios SET push_prefs=? WHERE id=?", (json.dumps({"casas": casas, "categorias": cats}), u["id"]))
    return {"ok": True}


@app.post("/api/push/teste")
async def push_teste(request: Request, payload: dict):
    u = request.state.usuario
    alvo = str(payload.get("endpoint", ""))
    rows = db.execute("SELECT * FROM push_subs WHERE uid=?" + (" AND endpoint=?" if alvo else ""),
                      (u["id"], alvo) if alvo else (u["id"],)).fetchall()
    res = []
    aviso_id = secrets.token_hex(8)
    db.execute("INSERT INTO avisos_log(id,ts,titulo,casa,categoria,prio,ntfy) VALUES(?,?,?,?,?,?,NULL)",
               (aviso_id, time.time(), "Teste de aviso", "", "teste", 3))
    for r in rows:
        envio = secrets.token_hex(8)
        st = await asyncio.to_thread(push.enviar, {"endpoint": r["endpoint"], "keys": json.loads(r["chaves"])},
                                     {"t": "Teste de aviso", "b": f"Olá, {u['nome'] or u['login']}! Os avisos chegam neste aparelho.",
                                      "u": "/#avisos", "tag": "teste", "p": 3, "a": [{"action": "abrir", "title": "Abrir"}],
                                      "id": envio}, True)
        db.execute("INSERT INTO push_envios(id,aviso,sub_id,uid,ts,status) VALUES(?,?,?,?,?,?)",
                   (envio, aviso_id, r["id"], u["id"], time.time(), st))
        if st in (404, 410):
            db.execute("DELETE FROM push_subs WHERE id=?", (r["id"],))
        else:
            db.execute("UPDATE push_subs SET ultimo=?, status=? WHERE id=?", (time.time(), st, r["id"]))
        res.append({"aparelho": r["aparelho"], "status": st, "ok": 200 <= st < 300})
    return {"enviados": res}


@app.post("/api/push/recebido")
async def push_recebido(payload: dict):
    """O sw.js do aparelho confirma que o push chegou (sem login: o id do envio é aleatório e só marca a hora)."""
    envio = str(payload.get("id", ""))
    if re.fullmatch(r"[0-9a-f]{16}", envio):
        db.execute("UPDATE push_envios SET recebido=COALESCE(recebido,?) WHERE id=?", (time.time(), envio))
    return {"ok": True}


@app.get("/api/push/historico")
async def push_historico(request: Request):
    """Últimos avisos que este usuário deveria ver, com o caminho de cada um: ntfy, envio ao Google/Apple e
    confirmação de chegada em cada aparelho dele."""
    u = request.state.usuario
    perms, casas = _perms(u), casas_do(u)
    subs = {r["id"]: r["aparelho"] for r in db.execute("SELECT id, aparelho FROM push_subs WHERE uid=?", (u["id"],))}
    pref = prefs_push(u.get("push_prefs"))
    out = []
    for a in db.execute("SELECT * FROM avisos_log ORDER BY ts DESC LIMIT 200").fetchall():
        envios = db.execute("SELECT sub_id, status, ts, recebido FROM push_envios WHERE aviso=? AND uid=?",
                            (a["id"], u["id"])).fetchall()
        if not envios and (a["casa"] not in casas or PERM_CATEGORIA.get(a["categoria"], "admin") not in perms
                           and "admin" not in perms):
            continue
        filtrado = (not envios and (not pref["categorias"].get(a["categoria"], True)
                                    or pref["casas"] is not None and a["casa"] not in pref["casas"]))
        out.append({"ts": a["ts"], "titulo": a["titulo"], "casa": a["casa"], "ntfy": a["ntfy"], "filtrado": filtrado,
                    "envios": [{"aparelho": subs.get(e["sub_id"], "aparelho removido"), "status": e["status"],
                                "recebido": e["recebido"], "ts": e["ts"]} for e in envios]})
        if len(out) >= 30:
            break
    return {"avisos": out}


# ---- arquivos do app instalável (PWA) e página do assistente da ponte --------------------------------------
@app.get("/sw.js")
async def service_worker():
    return FileResponse(STATIC / "sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
async def manifesto():
    return FileResponse(STATIC / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/icone-{nome}.png")
async def icone(nome: str):
    arq = STATIC / f"icone-{Path(nome).name}.png"
    if not arq.is_file():
        raise HTTPException(404)
    return FileResponse(arq, media_type="image/png", headers={"Cache-Control": "max-age=604800"})


@app.get("/static/{nome}")
async def estatico(nome: str):
    arq = STATIC / Path(nome).name
    if arq.suffix != ".js" or not arq.is_file():
        raise HTTPException(404)
    return FileResponse(arq, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/configurar-ponte", response_class=HTMLResponse)
async def configurar_ponte():
    return (STATIC / "ponte.html").read_text(encoding="utf-8")


# APKs oficiais do Termux (GitHub, assinatura de teste: app e plugins precisam vir todos da MESMA fonte).
# Se o publicar.sh espelhou os arquivos em vps/ponte/apk/, o site entrega direto da VPS.
TERMUX_APKS = {
    "termux": ("Termux", "https://github.com/termux/termux-app/releases/latest"),
    "termux32": ("Termux para celular antigo (32 bits)", ""),  # só aparece se espelhado
    "boot": ("Termux:Boot (liga sozinho ao reiniciar)", "https://github.com/termux/termux-boot/releases/latest"),
    "api": ("Termux:API (bateria no painel)", "https://github.com/termux/termux-api/releases/latest"),
}


@app.get("/api/pontes/apks")
async def apks_ponte():
    pasta = Path("/srv/ponte/apk")  # o mesmo diretório que o Caddy serve em /ponte/apk/ (montado só leitura)
    espelho = json.loads((pasta / "apks.json").read_text()) if (pasta / "apks.json").is_file() else {}
    return [{"id": k, "nome": n, "url": f"/ponte/apk/{espelho[k]['arquivo']}" if k in espelho else u,
             "mb": espelho.get(k, {}).get("mb"), "versao": espelho.get(k, {}).get("versao"), "espelho": k in espelho}
            for k, (n, u) in TERMUX_APKS.items() if u or k in espelho]


# ---- Admin: casas e câmeras ----------------------------------------------------------------------------------
_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,29}")


def _validar_casas(novas: dict) -> dict:
    if not isinstance(novas, dict) or not novas:
        raise HTTPException(400, "Precisa haver ao menos uma casa.")
    out, vistas = {}, set()
    for cid, d in novas.items():
        if not _ID.fullmatch(cid):
            raise HTTPException(400, f"Id de casa inválido: {cid!r} (minúsculas, números, - e _).")
        nome = str(d.get("nome", "")).strip()[:40]
        topico = str(d.get("topico") or cid).strip()
        if not nome or not _ID.fullmatch(topico):
            raise HTTPException(400, f"Casa {cid}: nome e tópico de aviso são obrigatórios.")
        cams = {}
        for cam, cnome in (d.get("cameras") or {}).items():
            if not _ID.fullmatch(cam) or cam in vistas:
                raise HTTPException(400, f"Id de câmera inválido ou repetido: {cam!r}.")
            vistas.add(cam)
            cams[cam] = str(cnome).strip()[:40] or cam
        c = {"nome": nome, "topico": topico, "cameras": cams}
        # parâmetros do reconhecimento por casa (números; ver ajuste()): aparencia_janela_s, repete_janela_s
        par = {k: v for k, v in (d.get("parametros") or {}).items()
               if _ID.fullmatch(str(k)) and isinstance(v, (int, float)) and not isinstance(v, bool)}
        if par:
            c["parametros"] = par
        for flag in ("robo", "presenca"):
            if d.get(flag):
                c[flag] = True
        for lista in ("ptz", "ajustes"):
            v = [x for x in d.get(lista) or [] if x in cams]
            if v:
                c[lista] = v
        ponte = {k: str(v) for k, v in (d.get("ponte") or {}).items() if k in cams}
        for k, v in ponte.items():
            if not v.startswith("xiaomi://"):
                raise HTTPException(400, f"Câmera {k}: endereço da ponte deve ser xiaomi://…")
        if ponte or "ponte" in d:
            c["ponte"] = ponte
        out[cid] = c
    return out


@app.get("/api/admin/casas")
async def admin_casas():
    aparelhos, ts = _ler_estado("xiaomi_aparelhos", []), None
    r = db.execute("SELECT ts FROM estado WHERE chave='xiaomi_aparelhos'").fetchone()
    if r:
        ts = r["ts"]
    usadas = {v.split("did=")[1].split("&")[0] for d in CASAS.values() for v in (d.get("ponte") or {}).values() if "did=" in v}
    return {"casas": CASAS, "xiaomi": [{**a, "usada": str(a.get("did")) in usadas} for a in aparelhos
                                       if "camera" in str(a.get("model", ""))], "xiaomi_ts": ts}


@app.put("/api/admin/casas")
async def salvar_casas(payload: dict):
    novas = _validar_casas(payload.get("casas"))
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('casas',?,?)", (json.dumps(novas), time.time()))
    mudou_cams = set(CAM_CASA) != {cam for d in novas.values() for cam in d["cameras"]}
    aplicar_casas(novas)
    res = await asyncio.to_thread(sincronizar_go2rtc) if mudou_cams else "sem mudança de câmeras"
    return {"ok": True, "go2rtc": res}


# ---- go2rtc da VPS: os streams seguem as câmeras das casas (o arquivo é do app; o Admin manda) ----------------
GO2RTC_API = os.environ.get("GO2RTC_API", "http://go2rtc:1984/ao-vivo")
GO2RTC_BASE = """# GERADO PELO APP (Admin > Casas e câmeras): não edite à mão, a lista de streams é reescrita.
# go2rtc da VPS: recebe o vídeo do PC (casa) e dos celulares-ponte via /ingest e serve o ao vivo (/ao-vivo,
# atrás do login). O RTSP interno é lido pelo gravador e pelo vigia. Sem porta publicada: só o Caddy fala com ele.
api:
  listen: ":1984"
  base_path: "/ao-vivo"
rtsp:
  listen: ":8554"
webrtc:
  listen: ""
log:
  level: info
streams:
"""


def sincronizar_go2rtc() -> str:
    try:
        with urllib.request.urlopen(GO2RTC_API + "/api/streams", timeout=10) as r:
            atuais = set(json.loads(r.read()))
    except (OSError, ValueError) as e:
        return f"go2rtc fora do ar: {e}"
    queremos = set(CAM_CASA)
    if atuais == queremos:
        return "ok"
    yaml = GO2RTC_BASE + "".join(f"  {c}:\n" for c in sorted(queremos))
    try:
        urllib.request.urlopen(urllib.request.Request(GO2RTC_API + "/api/config", data=yaml.encode(), method="POST"),
                               timeout=10).read()
        urllib.request.urlopen(urllib.request.Request(GO2RTC_API + "/api/restart", data=b"", method="POST"),
                               timeout=10).read()
    except (OSError, ValueError) as e:
        return f"não consegui reconfigurar o go2rtc: {e}"
    log.info("go2rtc: streams %s -> %s (reiniciado; PC e pontes reconectam sozinhos)", sorted(atuais), sorted(queremos))
    return "reiniciado com " + ", ".join(sorted(queremos))


async def sincronizar_go2rtc_inicio():
    await asyncio.sleep(15)  # deixa o go2rtc subir
    log.info("go2rtc: %s", await asyncio.to_thread(sincronizar_go2rtc))


@app.get("/api/agente/cameras", dependencies=[Depends(agente)])
async def cameras_agente():
    """Gravador grava todas; o vigia da VPS analisa as que chegam por ponte (o PC analisa as dele)."""
    return {"todas": sorted(CAM_CASA), "vigia": sorted({cam for d in CASAS.values() for cam in (d.get("ponte") or {})})}


# ---- Admin: sistema (IA e resumo) -------------------------------------------------------------------------
@app.get("/api/admin/sistema")
async def ver_sistema():
    return {"config": sistema(), "modelos": MODELOS_EVENTO}


@app.put("/api/admin/sistema")
async def salvar_sistema(payload: dict):
    atual = sistema()
    if "ia_analisar_conhecidos" in payload:
        atual["ia_analisar_conhecidos"] = bool(payload["ia_analisar_conhecidos"])
    if payload.get("ia_modelo_evento") in MODELOS_EVENTO:
        atual["ia_modelo_evento"] = payload["ia_modelo_evento"]
    if "resumo_diario" in payload:
        atual["resumo_diario"] = bool(payload["resumo_diario"])
    if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", str(payload.get("resumo_hora", ""))):
        atual["resumo_hora"] = payload["resumo_hora"]
    db.execute("INSERT OR REPLACE INTO estado(chave,valor,ts) VALUES('sistema',?,?)", (json.dumps(atual), time.time()))
    ia.MODELO_EVENTO = atual["ia_modelo_evento"]
    return atual
