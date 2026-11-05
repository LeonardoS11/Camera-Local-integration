import json
import sqlite3
import sys
import time
from datetime import datetime

import numpy as np

db = sqlite3.connect("/data/casa.db")
db.row_factory = sqlite3.Row
H = float(sys.argv[1]) if len(sys.argv) > 1 else 48
evs = [dict(r) for r in db.execute("SELECT * FROM eventos WHERE ts>=? ORDER BY ts", (time.time() - H * 3600,))]
con = [e for e in evs if e["tipo"] == "conhecido"]
for e in evs:
    if e["tipo"] != "pessoa":
        continue
    n = max((len(c) for c in json.loads(e["caixas"] or "[]")), default=0)
    perto = sorted(con, key=lambda c: abs(c["ts"] - e["ts"]))[:1]
    d = f"{perto[0]['ts'] - e['ts']:+.0f}s {perto[0]['camera']} {json.loads(perto[0]['conhecidos'])}" if perto else "-"
    ce = [np.frombuffer(c["emb"], np.float32) for c in db.execute("SELECT emb FROM corpos WHERE evento_id=?", (e["id"],))]
    sims = []
    if perto:
        cp = [np.frombuffer(c["emb"], np.float32) for c in db.execute("SELECT emb FROM corpos WHERE evento_id=?", (perto[0]["id"],))]
        sims = [float(a @ b) for a in ce for b in cp]
    print(f"{e['id']} {datetime.fromtimestamp(e['ts']):%d %H:%M} {e['camera']} pessoas={n} | conhecido mais perto: {d}"
          f" | aparência {max(sims) if sims else 0:.2f} | {(e['descricao'] or '')[:60]}")
