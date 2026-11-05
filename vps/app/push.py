"""Avisos direto no navegador do celular (Web Push), sem app: Android pelo Chrome, iPhone pelo ícone na tela inicial
(iOS 16.4+). O conteúdo vai cifrado de ponta a ponta; o Google/Apple só entregam (31/10).

As chaves VAPID nascem na VPS (DADOS/vapid.pem) na primeira vez. Trocar a chave invalida todas as inscrições.
"""
import base64
import json
import logging
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush

log = logging.getLogger("push")
_privada: str = ""
_publica: str = ""
_contato = ""


def iniciar(pasta: Path, contato: str) -> None:
    """contato: URL do painel (o 'sub' do VAPID; não usamos e-mail de ninguém)."""
    global _privada, _publica, _contato
    arq = pasta / "vapid.pem"
    if not arq.exists():
        chave = ec.generate_private_key(ec.SECP256R1())
        arq.write_bytes(chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                            serialization.NoEncryption()))
        arq.chmod(0o600)
    chave = serialization.load_pem_private_key(arq.read_bytes(), None)
    pub = chave.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    _publica = base64.urlsafe_b64encode(pub).rstrip(b"=").decode()
    _privada = str(arq)
    _contato = contato


def chave_publica() -> str:
    return _publica


def enviar(inscricao: dict, dados: dict, urgente: bool) -> int:
    """Devolve o status HTTP do serviço de push (201 ok; 404/410 = inscrição morta, apagar)."""
    try:
        r = webpush(inscricao, json.dumps(dados, ensure_ascii=False), vapid_private_key=_privada,
                    vapid_claims={"sub": _contato}, ttl=6 * 3600,
                    headers={"Urgency": "high" if urgente else "normal"}, timeout=15)
        return r.status_code
    except WebPushException as e:
        st = e.response.status_code if e.response is not None else 0
        if st not in (404, 410):
            log.warning("push falhou (%s): %s", st, str(e)[:200])
        return st
    except Exception as e:  # noqa: BLE001
        log.warning("push falhou: %s", e)
        return 0
