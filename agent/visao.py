"""Visão local com modelos do OpenCV Zoo (licenças Apache-2.0/MIT), só OpenCV + NumPy.

- Pessoas: YOLOX-S (COCO, classe 0)          -> usado no PC (agente) para disparar eventos
- Rostos:  YuNet (detecção) + SFace (embedding de 128 dim) -> usado na VPS para conhecido/estranho

O mesmo arquivo é copiado para a imagem Docker da VPS.
"""
import threading
from pathlib import Path

import cv2
import numpy as np

MODELS = Path(__file__).resolve().parent.parent / "models"
SFACE_LIMIAR = 0.363  # similaridade de cosseno recomendada pelo autor do SFace
# ArcFace r50 (InsightFace buffalo_l, uso não comercial): calibração de 21/10 com rostos das câmeras —
# pessoas diferentes máx 0,23; câmera x câmera da mesma pessoa a 0,25 acertou 4/7 sem erro
ARCFACE_LIMIAR = 0.28


def _modelo(nome: str, pasta: Path | None) -> str:
    return str((pasta or MODELS) / nome)


class DetectorPessoas:
    def __init__(self, pasta: Path | None = None, conf: float = 0.6, nms: float = 0.5):
        self.net = cv2.dnn.readNet(_modelo("object_detection_yolox_2022nov.onnx", pasta))
        self.conf, self.nms, self.size = conf, nms, 640
        grids, strides = [], []
        for s in (8, 16, 32):
            n = self.size // s
            xv, yv = np.meshgrid(np.arange(n), np.arange(n))
            grids.append(np.stack((xv, yv), 2).reshape(-1, 2))
            strides.append(np.full((n * n, 1), s))
        self.grids = np.concatenate(grids).astype(np.float32)
        self.strides = np.concatenate(strides).astype(np.float32)

    def detectar(self, bgr: np.ndarray) -> list[tuple[int, int, int, int, float]]:
        """Retorna caixas de pessoas (x, y, w, h, score) nas coordenadas da imagem original."""
        h, w = bgr.shape[:2]
        r = min(self.size / h, self.size / w)
        img = np.full((self.size, self.size, 3), 114.0, np.float32)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        img[: int(h * r), : int(w * r)] = cv2.resize(rgb, (int(w * r), int(h * r))).astype(np.float32)
        self.net.setInput(np.transpose(img, (2, 0, 1))[np.newaxis])
        d = self.net.forward(self.net.getUnconnectedOutLayersNames())[0][0]
        xy = (d[:, :2] + self.grids) * self.strides
        wh = np.exp(d[:, 2:4]) * self.strides
        score = d[:, 4] * d[:, 5]  # objectness * classe "person"
        # só quando "pessoa" é a classe mais provável (um cachorro pode ter score de pessoa razoável)
        sel = (score >= self.conf) & (np.argmax(d[:, 5:], axis=1) == 0)
        if not sel.any():
            return []
        xy, wh, score = xy[sel], wh[sel], score[sel]
        boxes = np.concatenate([xy - wh / 2, wh], 1) / r
        keep = cv2.dnn.NMSBoxes(boxes.tolist(), score.tolist(), self.conf, self.nms)
        out = []
        for i in np.array(keep).flatten():
            x, y, bw, bh = boxes[i]
            out.append((max(0, int(x)), max(0, int(y)), int(bw), int(bh), float(score[i])))
        return out


class Aparencia:
    """Assinatura do corpo (YoutuReID, OpenCV Zoo, Apache-2.0; 768 dim). Fraca à noite e entre câmeras
    (calibração de 21/10: mesma pessoa na mesma câmera med 0,60; entre câmeras 0,37; pessoa × cachorro/objeto
    máx 0,53). Por isso só serve para REBAIXAR um aviso, nunca para apagá-lo."""

    def __init__(self, pasta: Path | None = None):
        self.net = cv2.dnn.readNet(_modelo("person_reid_youtu_2021nov.onnx", pasta))
        self.mean = np.array([0.485, 0.456, 0.406])
        self.std = np.array([0.229, 0.224, 0.225])
        self.trava = threading.Lock()  # cv2.dnn.Net não aguenta dois forward() ao mesmo tempo (eventos de 2 câmeras)

    def assinatura(self, bgr: np.ndarray, caixa) -> np.ndarray | None:
        x, y, w, h = (int(v) for v in caixa[:4])
        c = bgr[max(0, y):y + h, max(0, x):x + w]
        if c.size == 0 or w < 16 or h < 32:
            return None
        c = cv2.resize(c, (128, 256))[:, :, ::-1] / 255.0
        with self.trava:
            self.net.setInput(cv2.dnn.blobFromImage(((c - self.mean) / self.std).astype(np.float32)))
            f = self.net.forward().flatten().astype(np.float32)
        return f / (np.linalg.norm(f) + 1e-9)


class Rostos:
    """YuNet (detecção) + alinhamento de 5 pontos 112x112 + embedding: ArcFace r50 se o modelo existir
    (bem melhor com rosto pequeno de câmera), senão SFace. `arcface=False` no PC: lá só detectamos."""

    def __init__(self, pasta: Path | None = None, conf: float = 0.85, arcface: bool = True):
        self.det = cv2.FaceDetectorYN.create(_modelo("face_detection_yunet_2023mar.onnx", pasta), "",
                                             (320, 320), conf, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(_modelo("face_recognition_sface_2021dec.onnx", pasta), "")
        arq = Path(_modelo("w600k_r50.onnx", pasta))
        self.arc = cv2.dnn.readNet(str(arq)) if arcface and arq.is_file() else None
        self.modelo = "arcface_r50" if self.arc is not None else "sface"
        self.limiar = ARCFACE_LIMIAR if self.arc is not None else SFACE_LIMIAR
        self.trava = threading.Lock()  # YuNet/SFace/ArcFace não aguentam duas chamadas ao mesmo tempo

    def embedding(self, alinhado: np.ndarray) -> np.ndarray:
        """Vetor normalizado de um rosto já alinhado em 112x112 (o alignCrop usa o mesmo molde do ArcFace)."""
        with self.trava:
            if self.arc is not None:
                self.arc.setInput(cv2.dnn.blobFromImage(cv2.resize(alinhado, (112, 112)), 1 / 127.5, (112, 112),
                                                        (127.5, 127.5, 127.5), swapRB=True))
                emb = self.arc.forward().flatten().astype(np.float32)
            else:
                emb = self.rec.feature(alinhado).flatten().astype(np.float32)
        return emb / (np.linalg.norm(emb) + 1e-9)

    def encontrar(self, bgr: np.ndarray, min_lado: int = 28) -> list[dict]:
        """Rostos com embedding normalizado. Rostos muito pequenos são descartados (não dá para reconhecer).
        Cada rosto traz os 5 pontos do YuNet e se encosta na borda (entram na nota de qualidade)."""
        h, w = bgr.shape[:2]
        with self.trava:
            self.det.setInputSize((w, h))
            _, faces = self.det.detect(bgr)
        out = []
        for f in faces if faces is not None else []:
            x, y, fw, fh = (int(v) for v in f[:4])
            if min(fw, fh) < min_lado:
                continue
            with self.trava:
                alinhado = self.rec.alignCrop(bgr, f)
            emb = self.embedding(alinhado)
            out.append({"caixa": (x, y, fw, fh), "score": float(f[14]), "emb": emb, "recorte": alinhado,
                        "pontos": f[4:14].reshape(5, 2).copy(), "lado_real": float(min(fw, fh)),
                        "borda": x <= 1 or y <= 1 or x + fw >= w - 1 or y + fh >= h - 1})
        return out

    @staticmethod
    def qualidade(f: dict) -> float:
        """0–1: nitidez do recorte × pose de frente (nariz entre os olhos) × inteiro (sem corte na borda).
        Serve para decidir o que entra sozinho no cadastro; rosto de perfil ou borrado reconhece mas não ensina."""
        nit = cv2.Laplacian(cv2.cvtColor(f["recorte"], cv2.COLOR_BGR2GRAY), cv2.CV_64F).var()
        od, oe, nz = f["pontos"][0], f["pontos"][1], f["pontos"][2]
        frontal = 1.0 - min(1.0, abs((nz[0] - od[0]) / (abs(oe[0] - od[0]) + 1e-6) - 0.5) * 2)  # 1 de frente, 0 perfil
        return min(1.0, nit / 60.0) * (0.5 + 0.5 * frontal) * (0.5 if f.get("borda") else 1.0)

    def em_pessoas(self, bgr: np.ndarray, caixas_pessoa, altura_alvo: int = 360) -> list[dict]:
        """Procura rosto na parte de cima de cada pessoa, ampliando o recorte: à noite e de longe o rosto fica
        pequeno demais para o YuNet no quadro inteiro. Cada rosto vem com `pessoa` = índice da caixa,
        `lado_real` em px da imagem original (com a ampliação de 8–12× das câmeras altas o YuNet acha "rosto" em
        5 px reais; o ArcFace não reconhece isso — eram os "estranhos" a 0,20–0,28 do rancho) e `qualidade`."""
        H, W = bgr.shape[:2]
        out = []
        for idx, (px, py, pw, ph, *_) in enumerate(caixas_pessoa):
            x0, x1 = max(0, int(px - pw * 0.15)), min(W, int(px + pw * 1.15))
            y0, y1 = max(0, int(py - ph * 0.05)), min(H, int(py + ph * 0.45))
            if x1 - x0 < 8 or y1 - y0 < 8:
                continue
            crop = bgr[y0:y1, x0:x1]
            esc = max(1.0, altura_alvo / crop.shape[0])
            if esc > 1:
                crop = cv2.resize(crop, None, fx=esc, fy=esc, interpolation=cv2.INTER_CUBIC)
            for f in self.encontrar(crop, min_lado=int(28 * min(esc, 2.0))):
                fx, fy, fw, fh = f["caixa"]
                f["caixa"] = (int(x0 + fx / esc), int(y0 + fy / esc), int(fw / esc), int(fh / esc))
                f["pessoa"] = idx
                f["lado_real"] = min(fw, fh) / esc
                f["qualidade"] = self.qualidade(f)
                out.append(f)
        return out

    @staticmethod
    def dentro(caixa_rosto, caixas_pessoa, folga: float = 0.15) -> bool:
        """O centro do rosto precisa cair na metade de cima de alguma caixa de pessoa (com folga)."""
        x, y, w, h = caixa_rosto
        cx, cy = x + w / 2, y + h / 2
        for px, py, pw, ph, *_ in caixas_pessoa:
            fx, fy = pw * folga, ph * folga
            if px - fx <= cx <= px + pw + fx and py - fy <= cy <= py + ph * 0.6:
                return True
        return False

    @staticmethod
    def notas(emb: np.ndarray, conhecidos: list[tuple[str, np.ndarray]], k: int = 3) -> list[tuple[float, str]]:
        """Nota por pessoa = média dos k melhores cossenos dela, em ordem decrescente. Um recorte ruim que entrou no
        cadastro não decide sozinho: na simulação de 02/11 (cadastro limpo, limiar 0,28) o máximo deixava a outra
        pessoa passar em 17 % dos rostos, a média dos 3 melhores em 9 %, reconhecendo os mesmos 96–97 %."""
        if not conhecidos:
            return []
        nomes = [n for n, _ in conhecidos]
        sims = np.stack([e for _, e in conhecidos]) @ emb
        por: dict[str, list[float]] = {}
        for n, s in zip(nomes, sims):
            por.setdefault(n, []).append(float(s))
        return sorted(((float(np.mean(sorted(v, reverse=True)[:k])), n) for n, v in por.items()), reverse=True)

    def comparar(self, emb: np.ndarray, conhecidos: list[tuple[str, np.ndarray]]) -> tuple[str | None, float]:
        """Melhor nome e similaridade de cosseno; nome=None se abaixo do limiar."""
        notas = self.notas(emb, conhecidos)
        if not notas:
            return None, -1.0
        sim, nome = notas[0]
        return (nome if sim >= self.limiar else None), sim

    def comparar_detalhe(self, emb: np.ndarray, conhecidos: list[tuple[str, np.ndarray]]) -> dict:
        """Como comparar(), mais `melhor` (nome mesmo abaixo do limiar) e `margem` para o 2º colocado: um rosto
        no limite (0,20–0,28) só vale com a aparência do corpo apontando a mesma pessoa."""
        notas = self.notas(emb, conhecidos)
        if not notas:
            return {"quem": None, "sim": -1.0, "melhor": None, "margem": 0.0}
        sim, nome = notas[0]
        return {"quem": nome if sim >= self.limiar else None, "sim": sim, "melhor": nome,
                "margem": sim - (notas[1][0] if len(notas) > 1 else -1.0)}
