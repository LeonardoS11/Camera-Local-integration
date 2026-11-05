"""Teste do login/casas (30/10): cria sessões temporárias direto no banco (admin e um usuário só do rancho),
confere o que cada um vê pela URL pública e apaga tudo no fim. Nenhuma senha é usada.

  docker compose exec -T app python - < diagnostico/testar_acesso.py
"""
import http.client
import json
import ssl
import sqlite3
import time
import urllib.error
import urllib.request

import acesso

HOST = "seu-dominio.exemplo.com"
db = sqlite3.connect("/data/casa.db", isolation_level=None)
db.row_factory = sqlite3.Row
admin = db.execute("SELECT id FROM usuarios WHERE admin=1 ORDER BY id LIMIT 1").fetchone()["id"]
leitor = db.execute("SELECT id FROM perfis WHERE nome='Só assistir'").fetchone()["id"]
cur = db.execute("INSERT INTO usuarios(login,nome,hash,casas,admin,criado,perfil_id,ativo) "
                 "VALUES('teste_rancho','Teste','x','[\"rancho\"]',0,?,?,1)", (time.time(), leitor))
uid_r = cur.lastrowid
tok_a, _ = acesso.abrir_sessao(db, admin, False, "teste", "teste")
tok_r, _ = acesso.abrir_sessao(db, uid_r, False, "teste", "teste")
falhas = 0


def pedir(caminho, tok=None, metodo="GET", corpo=None):
    h = {"User-Agent": "teste-acesso"}
    if tok:
        h["Cookie"] = f"{acesso.COOKIE}={tok}"
    if corpo is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(f"https://{HOST}{caminho}", data=json.dumps(corpo).encode() if corpo is not None else None,
                                 headers=h, method=metodo)

    class SemRedir(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    try:
        with urllib.request.build_opener(SemRedir).open(req, timeout=20) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def ws(src, tok):
    c = http.client.HTTPSConnection(HOST, 443, timeout=10, context=ssl.create_default_context())
    c.request("GET", f"/ao-vivo/api/ws?src={src}", headers={
        "Cookie": f"{acesso.COOKIE}={tok}" if tok else "", "Upgrade": "websocket", "Connection": "Upgrade",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version": "13"})
    st = c.getresponse().status
    c.close()
    return st


def conf(nome, obtido, esperado):
    global falhas
    ok = obtido in esperado if isinstance(esperado, tuple) else obtido == esperado
    falhas += not ok
    print(("ok   " if ok else "FALHA"), nome, "->", obtido)


try:
    conf("/ sem login redireciona", pedir("/")[0], 303)
    conf("/login abre", pedir("/login")[0], 200)
    conf("API sem login", pedir("/api/eventos")[0], 401)
    conf("ao vivo sem login", pedir("/ao-vivo/api/streams")[0], 401)
    conf("ingest sem token", pedir("/ingest/api/stream.ts?dst=garagem", metodo="POST", corpo={})[0], 401)
    st, b = pedir("/api/eu", tok_a)
    conf("admin /api/eu", st, 200)
    print("     casas do admin:", [c["id"] for c in json.loads(b)["casas"]])
    conf("admin eventos da casa", pedir("/api/eventos?casa=casa&limite=3", tok_a)[0], 200)
    conf("admin pessoas", pedir("/api/pessoas", tok_a)[0], 200)
    conf("admin pontes", pedir("/api/pontes", tok_a)[0], 200)
    conf("admin usuarios", pedir("/api/usuarios", tok_a)[0], 200)
    conf("admin lista de streams", pedir("/ao-vivo/api/streams", tok_a)[0], 200)
    conf("admin websocket garagem", ws("garagem", tok_a), 101)
    conf("admin player", pedir("/ao-vivo/stream.html?src=garagem&mode=mse", tok_a)[0], 200)
    st, b = pedir("/api/eu", tok_r)
    conf("rancho /api/eu", [c["id"] for c in json.loads(b)["casas"]], ["rancho"])
    st, b = pedir("/api/eventos?limite=50", tok_r)
    conf("rancho eventos só do rancho", st == 200 and all(e["camera"].startswith("rancho") for e in json.loads(b)), True)
    conf("rancho pede eventos da casa", pedir("/api/eventos?casa=casa", tok_r)[0], 403)
    conf("rancho pessoas", pedir("/api/pessoas", tok_r)[0], 403)
    conf("rancho robô", pedir("/api/robo", tok_r, "POST", {"acao": "limpar"})[0], 403)
    conf("rancho usuarios", pedir("/api/usuarios", tok_r)[0], 403)
    conf("rancho gravação da garagem", pedir("/api/gravacoes?camera=garagem", tok_r)[0], 404)
    conf("rancho gravação do rancho", pedir("/api/gravacoes?camera=rancho1", tok_r)[0], 200)
    foto = db.execute("SELECT fotos FROM eventos WHERE camera='garagem' ORDER BY id DESC LIMIT 1").fetchone()
    if foto:
        conf("rancho foto da garagem", pedir("/midia/" + json.loads(foto["fotos"])[0], tok_r)[0], 404)
        conf("admin foto da garagem", pedir("/midia/" + json.loads(foto["fotos"])[0], tok_a)[0], 200)
    conf("rancho lista de streams", pedir("/ao-vivo/api/streams", tok_r)[0], 403)
    conf("rancho websocket garagem", ws("garagem", tok_r), 403)
    conf("rancho websocket rancho1 (passa no login)", ws("rancho1", tok_r), (101, 404, 500))
    conf("rancho player", pedir("/ao-vivo/stream.html?src=rancho1&mode=mse", tok_r)[0], 200)
    conf("rancho alarme do rancho", pedir("/api/alarme?casa=rancho", tok_r)[0], 200)
    conf("rancho alarme da casa", pedir("/api/alarme?casa=casa", tok_r)[0], 403)
    conf("ponte com código errado", pedir("/api/ponte/parear", None, "POST", {"codigo": "000000"})[0], 401)
    # 31/10: perfis, Admin, PWA, push
    conf("sw.js sem login", pedir("/sw.js")[0], 200)
    conf("manifesto sem login", pedir("/manifest.webmanifest")[0], 200)
    conf("ícone sem login", pedir("/icone-192.png")[0], 200)
    conf("script do admin sem login (manda para o login)", pedir("/static/painel-admin.js")[0], 303)
    conf("script do admin logado (rancho)", pedir("/static/painel-admin.js", tok_r)[0], 200)
    conf("rancho perfis", pedir("/api/perfis", tok_r)[0], 403)
    conf("rancho admin casas", pedir("/api/admin/casas", tok_r)[0], 403)
    conf("rancho assistente da ponte", pedir("/configurar-ponte", tok_r)[0], 403)
    conf("rancho alarme POST (perfil só assistir)", pedir("/api/alarme", tok_r, "POST", {"casa": "rancho", "silenciar_horas": 0})[0], 403)
    conf("rancho config de avisos", pedir("/api/push/config", tok_r)[0], 200)
    conf("rancho inscrição inválida", pedir("/api/push/inscrever", tok_r, "POST", {"inscricao": {"endpoint": "x"}})[0], 400)
    conf("confirmação de push sem login", pedir("/api/push/recebido", None, "POST", {"id": "0" * 16})[0], 200)
    conf("histórico de avisos sem login", pedir("/api/push/historico")[0], 401)
    st, b = pedir("/api/push/historico", tok_r)
    conf("rancho histórico só do rancho", st == 200 and all(a["casa"] in ("rancho", "") for a in json.loads(b)["avisos"]), True)
    conf("admin histórico de avisos", pedir("/api/push/historico", tok_a)[0], 200)
    conf("admin perfis", pedir("/api/perfis", tok_a)[0], 200)
    conf("admin assistente da ponte", pedir("/configurar-ponte", tok_a)[0], 200)
    conf("admin acessos", pedir("/api/admin/acessos", tok_a)[0], 200)
    conf("admin sistema", pedir("/api/admin/sistema", tok_a)[0], 200)
    conf("admin apks", pedir("/api/pontes/apks", tok_a)[0], 200)
    st, b = pedir("/api/admin/casas", tok_a)
    casas = json.loads(b)["casas"]
    st, b = pedir("/api/admin/casas", tok_a, "PUT", {"casas": casas})
    conf("admin salva casas sem mudar", f"{st} {json.loads(b).get('go2rtc')}", "200 sem mudança de câmeras")
    conf("admin casa com id ruim", pedir("/api/admin/casas", tok_a, "PUT", {"casas": {"Casa X": {"nome": "x", "cameras": {}}}})[0], 400)
    conf("não sobra admin (apagar o único)", pedir(f"/api/usuarios/{admin}", tok_r, "DELETE")[0], 403)
    import main as _m  # noqa: E402  (mesmo processo não: só para a função de assinatura)
    if foto:
        url = _m.url_assinada(json.loads(foto["fotos"])[0], horas=0.05)
        conf("foto com link assinado, sem login", pedir(url)[0], 200)
        conf("link assinado adulterado", pedir(url[:-1] + ("0" if url[-1] != "0" else "1"))[0], 401)
finally:
    db.execute("DELETE FROM sessoes WHERE ip='teste'")
    db.execute("DELETE FROM usuarios WHERE id=?", (uid_r,))
    print("limpo; falhas:", falhas)
