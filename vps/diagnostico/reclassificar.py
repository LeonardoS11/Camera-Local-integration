"""Reaplica as regras de 23/10 ao histórico (só rótulos; nenhum aviso). Uso no container: python reclassificar.py [horas]"""
import json
import sys
import time

import numpy as np

import main

H = float(sys.argv[1]) if len(sys.argv) > 1 else 48
db = main.db
desde = time.time() - H * 3600
fix_estranho = 0
for e in db.execute("SELECT id, caixas FROM eventos WHERE tipo='estranho' AND ts>=?", (desde,)).fetchall():
    if not main._uma_pessoa(e["caixas"]):
        continue
    r = db.execute("SELECT quem FROM rostos WHERE evento_id=? AND quem IS NOT NULL ORDER BY sim DESC LIMIT 1", (e["id"],)).fetchone()
    if r:
        db.execute("UPDATE eventos SET tipo='conhecido', conhecidos=?, desconhecidos=0 WHERE id=?", (json.dumps([r["quem"]]), e["id"]))
        db.execute("UPDATE rostos SET quem=? WHERE evento_id=? AND quem IS NULL", (r["quem"], e["id"]))
        db.execute("UPDATE corpos SET nome=? WHERE evento_id=? AND nome IS NULL", (r["quem"], e["id"]))
        fix_estranho += 1
ligados = 0
for e in db.execute("SELECT id, ts, camera, caixas, conhecidos FROM eventos WHERE tipo='conhecido' AND ts>=? ORDER BY ts",
                    (desde,)).fetchall():
    nomes = json.loads(e["conhecidos"] or "[]")
    if len(nomes) != 1 or not main._uma_pessoa(e["caixas"]):
        continue
    embs = [np.frombuffer(c["emb"], np.float32) for c in db.execute("SELECT emb FROM corpos WHERE evento_id=?", (e["id"],))]
    ligados += main.ligar_passagem(e["id"], e["ts"], nomes[0], embs, e["camera"])
print(f"estranhos -> conhecido: {fix_estranho}; eventos sem rosto que ganharam nome (visita): {ligados}")
print("tipos agora:", dict(db.execute("SELECT tipo, count(*) FROM eventos WHERE ts>=? GROUP BY tipo", (desde,)).fetchall()))
