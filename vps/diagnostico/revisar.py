"""Revisão do reconhecimento: histórico por tipo/câmera, passagens entre câmeras e calibração da aparência.
Uso (no container): python revisar.py [horas]
"""
import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime

import numpy as np

H = float(sys.argv[1]) if len(sys.argv) > 1 else 48
db = sqlite3.connect("/data/casa.db")
db.row_factory = sqlite3.Row
desde = time.time() - H * 3600
evs = [dict(r) for r in db.execute("SELECT id, ts, camera, tipo, conhecidos, notificado, descricao FROM eventos "
                                   "WHERE ts>=? ORDER BY ts", (desde,))]
print(f"== {len(evs)} eventos nas últimas {H:.0f} h")
por = Counter((e["camera"], e["tipo"]) for e in evs)
for cam in sorted({e["camera"] for e in evs}):
    print(f"  {cam}: " + ", ".join(f"{t}={n}" for (c, t), n in sorted(por.items()) if c == cam))
print("  notificados:", sum(e["notificado"] for e in evs))

# rostos por câmera: detectados / reconhecidos automaticamente
rost = db.execute("SELECT e.camera, r.quem, r.sim, r.pessoa_id FROM rostos r JOIN eventos e ON e.id=r.evento_id "
                  "WHERE e.ts>=?", (desde,)).fetchall()
print("== rostos detectados nos eventos")
for cam in sorted({r["camera"] for r in rost}):
    rs = [r for r in rost if r["camera"] == cam]
    auto = [r for r in rs if r["quem"]]
    sims = [r["sim"] for r in rs]
    print(f"  {cam}: {len(rs)} rostos, reconhecidos sozinhos {len(auto)}, sim med {np.median(sims):.2f} máx {max(sims):.2f}")

# passagens: eventos de câmeras diferentes a menos de 90 s um do outro
print("== passagens entre câmeras (< 90 s)")
pares = []
for i, a in enumerate(evs):
    for b in evs[i + 1:]:
        if b["ts"] - a["ts"] > 90:
            break
        if b["camera"] != a["camera"] and a["tipo"] != "sem_pessoa" and b["tipo"] != "sem_pessoa":
            pares.append((a, b))
for a, b in pares[-15:]:
    ra, rb = json.loads(a["conhecidos"] or "[]"), json.loads(b["conhecidos"] or "[]")
    print(f"  {datetime.fromtimestamp(a['ts']):%d %H:%M:%S} {a['camera']}:{a['tipo']}{ra} -> "
          f"+{b['ts'] - a['ts']:.0f}s {b['camera']}:{b['tipo']}{rb}")
print(f"  total {len(pares)}; com nome nos dois lados: "
      f"{sum(1 for a, b in pares if json.loads(a['conhecidos'] or '[]') and json.loads(b['conhecidos'] or '[]'))}; "
      f"nome só de um lado: {sum(1 for a, b in pares if bool(json.loads(a['conhecidos'] or '[]')) != bool(json.loads(b['conhecidos'] or '[]')))}")

# aparência: similaridade entre corpos nomeados (mesma pessoa x pessoas diferentes; mesma câmera x outra)
cs = db.execute("SELECT c.nome, c.camera, c.emb, c.evento_id FROM corpos c WHERE c.nome IS NOT NULL AND c.ts>=?",
                (desde,)).fetchall()
print(f"== aparência: {len(cs)} corpos com nome")
grupos = defaultdict(list)
for i, a in enumerate(cs):
    ea = np.frombuffer(a["emb"], np.float32)
    for b in cs[i + 1:]:
        if a["evento_id"] == b["evento_id"]:
            continue
        s = float(ea @ np.frombuffer(b["emb"], np.float32))
        k = ("mesma" if a["nome"] == b["nome"] else "outra") + "_pessoa/" + \
            ("mesma" if a["camera"] == b["camera"] else "outra") + "_camera"
        grupos[k].append(s)
for k in sorted(grupos):
    v = np.array(grupos[k])
    print(f"  {k}: n={len(v)} p10={np.percentile(v, 10):.2f} med={np.median(v):.2f} p90={np.percentile(v, 90):.2f} máx={v.max():.2f}")
for lim in (0.5, 0.55, 0.6, 0.65, 0.7):
    m = np.array(grupos.get("mesma_pessoa/outra_camera", [0]))
    o = np.array(grupos.get("outra_pessoa/outra_camera", [0]) + grupos.get("outra_pessoa/mesma_camera", [0]))
    print(f"  limiar {lim}: entre câmeras acerta {np.mean(m >= lim):.0%} | confunde pessoas {np.mean(o >= lim):.0%}")
