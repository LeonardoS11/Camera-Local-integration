"""Simula as regras novas no histórico: melhor rosto em evento de 1 pessoa, passagem entre câmeras (90 s ≥ 0,40),
espera de 45 s, e não repetir aviso da mesma pessoa em 10 min (≥ 0,45). Conta avisos de pessoa antes/depois."""
import json
import sqlite3
import sys
import time
from collections import Counter

import numpy as np

H = float(sys.argv[1]) if len(sys.argv) > 1 else 48
db = sqlite3.connect("/data/casa.db")
db.row_factory = sqlite3.Row
evs = [dict(r) for r in db.execute("SELECT * FROM eventos WHERE ts>=? ORDER BY ts", (time.time() - H * 3600,))]
C = {e["id"]: [np.frombuffer(c["emb"], np.float32) for c in db.execute("SELECT emb FROM corpos WHERE evento_id=?", (e["id"],))]
     for e in evs}


def uma(cx):
    try:
        return max((len(c) for c in json.loads(cx)), default=0) <= 1
    except ValueError:
        return False


def sim(a, b):
    v = [float(x @ y) for x in C[a] for y in C[b]]
    return max(v) if v else 0.0


novo = {}
for e in evs:
    t = e["tipo"]
    if t == "estranho" and uma(e["caixas"]) and \
            db.execute("SELECT 1 FROM rostos WHERE evento_id=? AND quem IS NOT NULL", (e["id"],)).fetchone():
        t = "conhecido"
    novo[e["id"]] = t
# passagem: conhecido em ± 90 s (qualquer câmera), 1 pessoa dos dois lados, aparência ≥ 0,40 — só o que já
# tinha chegado até 45 s depois do evento conta para cancelar o aviso
for e in evs:
    if novo[e["id"]] != "pessoa" or not uma(e["caixas"]):
        continue
    for o in evs:
        if novo[o["id"]] == "conhecido" and uma(o["caixas"]) and -90 <= o["ts"] - e["ts"] <= 45 and sim(e["id"], o["id"]) >= 0.40:
            novo[e["id"]] = "visita"
            break
avisos, avisados = [], []
for e in evs:
    t = novo[e["id"]]
    if t == "estranho":
        avisos.append(e["id"])
        avisados.append(e)
    elif t == "pessoa":
        rep = [a for a in avisados if 0 < e["ts"] - a["ts"] <= 600 and sim(e["id"], a["id"]) >= 0.45]
        if not rep:
            avisos.append(e["id"])
            avisados.append(e)
antes = sum(1 for e in evs if e["tipo"] in ("estranho", "pessoa"))
print("tipos depois:", dict(Counter(novo.values())))
print(f"avisos de pessoa nas últimas {H:.0f} h: antes {antes} -> depois {len(avisos)}")
print("avisos que continuariam:", avisos)
