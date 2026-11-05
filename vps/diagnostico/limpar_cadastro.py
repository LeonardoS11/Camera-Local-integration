"""Acha rostos do cadastro que provavelmente são de outra pessoa (ou ruído) e monta uma prancha para conferir.

Na VPS:  docker compose exec -T app python - < diagnostico/limpar_cadastro.py            (só lista + prancha)
         docker compose exec -T app python - --aplicar 12,45,91 < diagnostico/limpar_cadastro.py   (tira esses do cadastro)
Para cada rosto automático de uma pessoa: nota top-3 contra os outros rostos dela (fora os do mesmo evento, que são
quase o mesmo quadro) e contra os das outras pessoas. Suspeito = mais parecido com outra pessoa, ou < 0,25 com a própria
(ruído: rosto de poucos px). A prancha sai em /data/midia/diag_suspeitos.jpg (id em cima de cada recorte)."""
import sqlite3
import sys
from pathlib import Path

import cv2
import numpy as np

MIDIA = Path("/data/midia")
db = sqlite3.connect("/data/casa.db")
db.row_factory = sqlite3.Row
rows = db.execute("SELECT r.id, r.evento_id, r.img, r.emb, r.sim, p.nome FROM rostos r JOIN pessoas p ON p.id=r.pessoa_id "
                  "WHERE COALESCE(r.ignorado,0)=0").fetchall()
cad = [(r["id"], r["evento_id"], r["nome"], np.frombuffer(r["emb"], np.float32), r["img"], r["sim"]) for r in rows]


def top3(emb, lista):
    v = sorted((float(emb @ e) for e in lista), reverse=True)[:3]
    return float(np.mean(v)) if v else -1.0


suspeitos = []
for rid, ev, nome, emb, img, sim in cad:
    if ev is None:
        continue  # cadastro manual (selfie): não se julga
    propria = [e for i, v, n, e, *_ in cad if n == nome and i != rid and (v is None or v != ev)]
    outras = {n2: [e for i, v, n, e, *_ in cad if n == n2] for n2 in {n for _, _, n, *_ in cad if n != nome}}
    p = top3(emb, propria)
    o_nome, o = max(((n2, top3(emb, l)) for n2, l in outras.items()), key=lambda x: x[1], default=("", -1))
    motivo = f"parece mais {o_nome} ({o:.2f} > {p:.2f})" if o >= p else (f"fraco com {nome} ({p:.2f})" if p < 0.25 else "")
    if motivo:
        suspeitos.append((rid, nome, img, motivo, p, o))

if len(sys.argv) > 2 and sys.argv[1] == "--aplicar":
    ids = [int(x) for x in sys.argv[2].split(",") if x.strip()]
    n = db.execute(f"UPDATE rostos SET pessoa_id=NULL, ignorado=1 WHERE id IN ({','.join('?' * len(ids))})", ids).rowcount
    db.commit()
    print(f"{n} rosto(s) tirados do cadastro (ficam no histórico como ignorados)")
    sys.exit()

print(f"{len(cad)} rostos no cadastro; {len(suspeitos)} suspeitos:")
for rid, nome, img, motivo, p, o in suspeitos:
    print(f"  {rid:>5} {nome:<10} {motivo}")

tiles = []
for rid, nome, img, motivo, p, o in suspeitos:
    im = cv2.imread(str(MIDIA / img)) if img else None
    im = cv2.resize(im, (112, 112)) if im is not None else np.zeros((112, 112, 3), np.uint8)
    im = cv2.copyMakeBorder(im, 18, 0, 0, 0, cv2.BORDER_CONSTANT, value=(30, 30, 30))
    cv2.putText(im, f"{rid} {nome[:4]}", (2, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (255, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(im, f"{p:.2f}/{o:.2f}", (2, 128), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 255, 255), 1, cv2.LINE_AA)
    tiles.append(im)
if tiles:
    por_linha = 8
    while len(tiles) % por_linha:
        tiles.append(np.zeros_like(tiles[0]))
    linhas = [np.hstack(tiles[i:i + por_linha]) for i in range(0, len(tiles), por_linha)]
    cv2.imwrite(str(MIDIA / "diag_suspeitos.jpg"), np.vstack(linhas), [cv2.IMWRITE_JPEG_QUALITY, 90])
    print("prancha: /data/midia/diag_suspeitos.jpg (amarelo: nota própria/nota da outra pessoa)")
