"""Revisão 2: concordância de nomes entre câmeras, aparência dentro de 3 h, e os 'provavel'/'estranho'."""
import json
import sqlite3
import sys
import time
from collections import defaultdict

import numpy as np

H = float(sys.argv[1]) if len(sys.argv) > 1 else 48
db = sqlite3.connect("/data/casa.db")
db.row_factory = sqlite3.Row
desde = time.time() - H * 3600
evs = [dict(r) for r in db.execute("SELECT id, ts, camera, tipo, conhecidos FROM eventos WHERE ts>=? ORDER BY ts", (desde,))]

ok = ruim = 0
for i, a in enumerate(evs):
    na = set(json.loads(a["conhecidos"] or "[]"))
    for b in evs[i + 1:]:
        if b["ts"] - a["ts"] > 90:
            break
        nb = set(json.loads(b["conhecidos"] or "[]"))
        if b["camera"] != a["camera"] and na and nb and a["tipo"] == b["tipo"] == "conhecido":
            if na & nb:
                ok += 1
            else:
                ruim += 1
                print(f"  DISCORDA: ev{a['id']} {a['camera']} {sorted(na)} x ev{b['id']} {b['camera']} {sorted(nb)}")
print(f"== nomes entre câmeras (conhecido x conhecido, < 90 s): concordam {ok}, discordam {ruim}")

# mesma câmera: uma pessoa, mesmo nome, < 90 s, nomes diferentes = provável erro de rosto
troca = 0
for i, a in enumerate(evs):
    na = set(json.loads(a["conhecidos"] or "[]"))
    for b in evs[i + 1:]:
        if b["ts"] - a["ts"] > 30:
            break
        nb = set(json.loads(b["conhecidos"] or "[]"))
        if b["camera"] == a["camera"] and len(na) == len(nb) == 1 and na != nb:
            troca += 1
print(f"== mesma câmera, < 30 s, nome trocou: {troca}")

cs = db.execute("SELECT nome, camera, emb, evento_id, ts FROM corpos WHERE nome IS NOT NULL AND ts>=?", (desde,)).fetchall()
g = defaultdict(list)
for i, a in enumerate(cs):
    ea = np.frombuffer(a["emb"], np.float32)
    for b in cs[i + 1:]:
        if a["evento_id"] == b["evento_id"] or abs(a["ts"] - b["ts"]) > 3 * 3600:
            continue
        s = float(ea @ np.frombuffer(b["emb"], np.float32))
        k = ("mesma" if a["nome"] == b["nome"] else "outra") + "/" + ("mesma_cam" if a["camera"] == b["camera"] else "outra_cam")
        g[k].append(s)
print("== aparência dentro de 3 h")
for k in sorted(g):
    v = np.array(g[k])
    print(f"  {k}: n={len(v)} med={np.median(v):.2f} p90={np.percentile(v, 90):.2f} p99={np.percentile(v, 99):.2f} máx={v.max():.2f}")
for lim in (0.6, 0.65, 0.7, 0.75):
    m = np.array(g.get("mesma/mesma_cam", [0]))
    o = np.array(g.get("outra/mesma_cam", [0]))
    print(f"  mesma câmera, limiar {lim}: acerta {np.mean(m >= lim):.1%} | confunde {np.mean(o >= lim):.1%}")

# 'provavel': o nome dado pela aparência bate com algum rosto reconhecido na mesma visita (± 2 min)?
print("== conferência dos 'provavel' com rostos reconhecidos em ± 2 min")
bate = contra = sem = 0
for e in evs:
    if e["tipo"] != "provavel":
        continue
    nome = (json.loads(e["conhecidos"] or "[]") or [None])[0]
    vizinhos = {n for x in evs if x["tipo"] == "conhecido" and abs(x["ts"] - e["ts"]) < 120
                for n in json.loads(x["conhecidos"] or "[]")}
    if not vizinhos:
        sem += 1
    elif nome in vizinhos:
        bate += 1
    else:
        contra += 1
        print(f"  ev{e['id']} {e['camera']} provável {nome}, mas na visita: {sorted(vizinhos)}")
print(f"  batem {bate}, contradizem {contra}, sem rosto por perto {sem}")
print("== estranhos:", [(e["id"], e["camera"]) for e in evs if e["tipo"] == "estranho"])
