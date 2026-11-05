"""Simula a nota de reconhecimento com os rostos já guardados no banco, sem reprocessar eventos.

Rode na VPS:  docker compose exec -T app python - < diagnostico/simular_rosto.py
Compara, deixando um rosto de fora por vez (leave-one-out) entre os rostos cadastrados:
  - máximo contra todos (regra atual)  x  média dos 3 melhores por pessoa
  - embedding normal  x  normal + espelho (flip horizontal), recalculado dos recortes 112x112
e mostra como ficariam os rostos "sem nome" dos últimos dias com cada regra."""
import sqlite3
import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, "/app")
import visao  # noqa: E402

MIDIA = Path("/data/midia")
db = sqlite3.connect("/data/casa.db")
db.row_factory = sqlite3.Row
R = visao.Rostos(Path("/app/models"))


def emb_flip(im):
    e1, e2 = R.embedding(im), R.embedding(cv2.flip(im, 1))
    e = e1 + e2
    return e / (np.linalg.norm(e) + 1e-9)


cad = []  # (nome, emb, emb_flip)
for r in db.execute("SELECT p.nome, r.emb, r.img FROM rostos r JOIN pessoas p ON p.id=r.pessoa_id WHERE COALESCE(r.ignorado,0)=0"):
    im = cv2.imread(str(MIDIA / r["img"])) if r["img"] else None
    if im is None:
        continue
    cad.append((r["nome"], np.frombuffer(r["emb"], np.float32), emb_flip(im)))
print(f"{len(cad)} rostos cadastrados com recorte; pessoas: {sorted({n for n, *_ in cad})}")


def notas(emb, galeria, k):
    por = {}
    for nome, e in galeria:
        por.setdefault(nome, []).append(float(emb @ e))
    return {n: float(np.mean(sorted(v, reverse=True)[:k])) for n, v in por.items()}


def avaliar(idx, k):
    """Leave-one-out: nota da própria pessoa e a melhor nota de outra pessoa."""
    mesma, outra = [], []
    for i, (nome, *_) in enumerate(cad):
        gal = [(n, e[idx]) for j, (n, *e) in enumerate(cad) if j != i]
        ns = notas(cad[i][1 + idx], gal, k)
        mesma.append(ns.get(nome, -1))
        outra.append(max([v for n, v in ns.items() if n != nome], default=-1))
    m, o = np.array(mesma), np.array(outra)
    return m, o


for idx, rot in ((0, "normal"), (1, "flip")):
    for k, regra in ((1, "máximo"), (3, "top-3")):
        m, o = avaliar(idx, k)
        for lim in (0.24, 0.26, 0.28):
            acerto = ((m >= lim) & (m > o)).mean()
            falso = (o >= lim).mean()
            print(f"{rot:<7} {regra:<7} limiar {lim:.2f}: reconhece {acerto:5.1%}  | outra pessoa passaria {falso:5.1%} "
                  f"| mesma: med {np.median(m):.2f} p10 {np.percentile(m, 10):.2f} | outra: p90 {np.percentile(o, 90):.2f} máx {o.max():.2f}")

# rostos de eventos sem nome (últimos 14 dias): o que cada regra diria
sem = db.execute("SELECT id, sim, img, ts FROM rostos WHERE quem IS NULL AND evento_id IS NOT NULL AND ts > ? ORDER BY ts",
                 (time.time() - 14 * 86400,)).fetchall()
print(f"\n{len(sem)} rostos sem nome (14 d): id | sim atual | máx normal | top-3 normal | top-3 flip")
gal0 = [(n, e) for n, e, _ in cad]
gal1 = [(n, f) for n, _, f in cad]
for r in sem:
    im = cv2.imread(str(MIDIA / r["img"])) if r["img"] else None
    if im is None:
        continue
    e0 = R.embedding(im)
    n0, n3, nf = notas(e0, gal0, 1), notas(e0, gal0, 3), notas(emb_flip(im), gal1, 3)
    f = lambda d: max(d.items(), key=lambda x: x[1])  # noqa: E731
    print(f"{r['id']:>5} | {r['sim']:.2f} | {f(n0)[0][:4]} {f(n0)[1]:.2f} | {f(n3)[0][:4]} {f(n3)[1]:.2f} | {f(nf)[0][:4]} {f(nf)[1]:.2f}")
