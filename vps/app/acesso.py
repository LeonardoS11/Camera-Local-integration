"""Login do painel: usuários com senha (bcrypt), sessão por cookie e as casas que cada um pode ver.

Substitui o basic_auth do Caddy (30/10). O primeiro administrador nasce do PAINEL_USUARIO/PAINEL_HASH do .env
(o hash bcrypt que o Caddy usava), então a senha de sempre continua valendo.

Perfis (31/10): cada usuário tem um perfil (conjunto de permissões, editável no Admin) e as casas que vê.
"""
import hashlib
import json
import secrets
import threading
import time

import bcrypt

COOKIE = "casa_sessao"
SESSAO_LONGA = 30 * 86400   # "manter conectado": 30 dias, renovados enquanto o aparelho usa
SESSAO_CURTA = 12 * 3600
# bloqueio de força bruta: 5 erros no mesmo usuário ou 20 no mesmo IP em 15 min
JANELA_ERROS, MAX_ERROS_LOGIN, MAX_ERROS_IP = 15 * 60, 5, 20

_erros: dict[str, list[float]] = {}
_lock = threading.Lock()
_HASH_FALSO = bcrypt.hashpw(b"x", bcrypt.gensalt(12)).decode()  # usuário inexistente custa o mesmo tempo

# o que cada permissão libera (o main.py traduz rota -> permissão; rota desconhecida exige "admin")
PERMISSOES = {
    "aovivo": "Ver ao vivo",
    "eventos": "Ver eventos e fotos",
    "gravacoes": "Ver gravações",
    "alarme": "Mudar alarme / silenciar",
    "identificar": "Dizer quem é a pessoa num evento",
    "ptz": "Girar as câmeras",
    "pessoas": "Cadastro de pessoas e rostos",
    "robo": "Controlar o robô",
    "ajustes": "Ajustes das câmeras",
    "perguntar": "Perguntar à IA",
    "admin": "Administração (usuários, perfis, casas, pontes)",
}
PERFIS_PADRAO = [
    ("Administrador", list(PERMISSOES), 1),
    ("Família", ["aovivo", "eventos", "gravacoes", "alarme", "identificar", "ptz", "robo", "perguntar"], 0),
    ("Só assistir", ["aovivo", "eventos", "gravacoes"], 0),
]


def iniciar(db, admin_login: str, admin_hash: str) -> None:
    db.executescript("""
CREATE TABLE IF NOT EXISTS usuarios(id INTEGER PRIMARY KEY, login TEXT UNIQUE, nome TEXT, hash TEXT, casas TEXT,
  admin INTEGER, criado REAL, ultimo REAL);
CREATE TABLE IF NOT EXISTS sessoes(hash TEXT PRIMARY KEY, uid INTEGER, criado REAL, expira REAL, lembrar INTEGER,
  ip TEXT, ua TEXT);
CREATE TABLE IF NOT EXISTS perfis(id INTEGER PRIMARY KEY, nome TEXT UNIQUE, permissoes TEXT, fixo INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS acessos(id INTEGER PRIMARY KEY, ts REAL, login TEXT, ip TEXT, ok INTEGER, ua TEXT);
CREATE INDEX IF NOT EXISTS ix_acessos_ts ON acessos(ts);
""")
    colunas = {c[1] for c in db.execute("PRAGMA table_info(usuarios)")}
    for col, tipo in (("perfil_id", "INTEGER"), ("ativo", "INTEGER DEFAULT 1"), ("push_prefs", "TEXT")):
        if col not in colunas:
            db.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {tipo}")
    if not db.execute("SELECT 1 FROM perfis").fetchone():
        for nome, perms, fixo in PERFIS_PADRAO:
            db.execute("INSERT INTO perfis(nome,permissoes,fixo) VALUES(?,?,?)", (nome, json.dumps(perms), fixo))
    adm = db.execute("SELECT id FROM perfis WHERE fixo=1").fetchone()[0]
    leitor = db.execute("SELECT id FROM perfis WHERE nome='Só assistir'").fetchone()
    db.execute("UPDATE usuarios SET perfil_id=? WHERE perfil_id IS NULL AND admin=1", (adm,))
    db.execute("UPDATE usuarios SET perfil_id=? WHERE perfil_id IS NULL", (leitor[0] if leitor else adm,))
    if admin_login and admin_hash.startswith("$2") and not db.execute("SELECT 1 FROM usuarios").fetchone():
        db.execute("INSERT INTO usuarios(login,nome,hash,casas,admin,criado,perfil_id,ativo) VALUES(?,?,?,?,1,?,?,1)",
                   (admin_login.strip().lower(), admin_login.strip().capitalize(), admin_hash, '["*"]', time.time(), adm))
    db.execute("DELETE FROM acessos WHERE ts<?", (time.time() - 90 * 86400,))


def registrar_acesso(db, login: str, ip: str, ok: bool, ua: str) -> None:
    db.execute("INSERT INTO acessos(ts,login,ip,ok,ua) VALUES(?,?,?,?,?)", (time.time(), login[:60], ip, int(ok), ua[:200]))


def gerar_hash(senha: str) -> str:
    return bcrypt.hashpw(senha.encode()[:72], bcrypt.gensalt(12)).decode()


def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _contar(chave: str, agora: float) -> int:
    v = [t for t in _erros.get(chave, []) if agora - t < JANELA_ERROS]
    _erros[chave] = v
    return len(v)


def _podar_erros(agora: float) -> None:
    """_erros ganha uma chave por login/IP tentado e nunca perdia nenhuma: um varredor enchia a memória."""
    if len(_erros) > 5000:
        for k in [k for k, v in _erros.items() if not v or agora - v[-1] >= JANELA_ERROS]:
            del _erros[k]


def bloqueado(login: str, ip: str) -> int:
    """Segundos até poder tentar de novo (0 = liberado)."""
    agora = time.time()
    with _lock:
        _podar_erros(agora)
        if _contar("u:" + login, agora) >= MAX_ERROS_LOGIN:
            return int(JANELA_ERROS - (agora - _erros["u:" + login][0])) + 1
        if _contar("ip:" + ip, agora) >= MAX_ERROS_IP:
            return int(JANELA_ERROS - (agora - _erros["ip:" + ip][0])) + 1
    return 0


def conferir(db, login: str, senha: str, ip: str) -> dict | None:
    login = login.strip().lower()
    u = db.execute("SELECT * FROM usuarios WHERE login=?", (login,)).fetchone()
    ok = bcrypt.checkpw(senha.encode()[:72], (u["hash"] if u else _HASH_FALSO).encode())
    if u and ok and u["ativo"] != 0:
        with _lock:
            _erros.pop("u:" + login, None)
        return dict(u)
    with _lock:
        agora = time.time()
        _erros.setdefault("u:" + login, []).append(agora)
        _erros.setdefault("ip:" + ip, []).append(agora)
    return None


def abrir_sessao(db, uid: int, lembrar: bool, ip: str, ua: str) -> tuple[str, int]:
    token = secrets.token_urlsafe(32)
    dur = SESSAO_LONGA if lembrar else SESSAO_CURTA
    agora = time.time()
    db.execute("INSERT INTO sessoes(hash,uid,criado,expira,lembrar,ip,ua) VALUES(?,?,?,?,?,?,?)",
               (_h(token), uid, agora, agora + dur, int(lembrar), ip, ua[:200]))
    db.execute("UPDATE usuarios SET ultimo=? WHERE id=?", (agora, uid))
    db.execute("DELETE FROM sessoes WHERE expira<?", (agora,))
    return token, dur


def usuario_da_sessao(db, token: str | None) -> dict | None:
    if not token:
        return None
    agora = time.time()
    r = db.execute("SELECT s.hash AS sessao, s.expira, s.lembrar, u.id, u.login, u.nome, u.casas, u.push_prefs, "
                   "p.nome AS perfil, p.permissoes FROM sessoes s JOIN usuarios u ON u.id=s.uid "
                   "LEFT JOIN perfis p ON p.id=u.perfil_id WHERE s.hash=? AND s.expira>? AND COALESCE(u.ativo,1)=1",
                   (_h(token), agora)).fetchone()
    if not r:
        return None
    if r["lembrar"] and r["expira"] - agora < SESSAO_LONGA / 2:
        db.execute("UPDATE sessoes SET expira=? WHERE hash=?", (agora + SESSAO_LONGA, r["sessao"]))
    u = dict(r)
    u["casas"] = json.loads(u["casas"] or "[]")
    u["perms"] = set(json.loads(u.pop("permissoes") or "[]"))
    u["admin"] = "admin" in u["perms"]
    return u


def fechar_sessao(db, token: str | None) -> None:
    if token:
        db.execute("DELETE FROM sessoes WHERE hash=?", (_h(token),))


def fechar_todas(db, uid: int, exceto: str | None = None) -> None:
    db.execute("DELETE FROM sessoes WHERE uid=? AND hash<>?", (uid, _h(exceto) if exceto else ""))


def token_hash(token: str) -> str:
    return _h(token)
