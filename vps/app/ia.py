"""Claude: descrição de eventos (imagem), resumo do dia e perguntas sobre o histórico.

A identidade das pessoas vem do reconhecimento facial local (SFace). A Claude só descreve a cena:
roupas, ações, objetos, veículos, se algo parece suspeito. Ela não identifica ninguém pelo rosto.
"""
import base64
import json
import logging
import os

import anthropic

log = logging.getLogger("ia")
MODELO = os.environ.get("IA_MODELO", "claude-opus-5-5")  # perguntas, resumo do dia e rotinas do robô
# Descrição de evento (a chamada que mais se repete): Haiku 5.5 custa 1/40 do Opus por token (30/10, a pedido do
# usuário para cortar gasto). Para voltar: IA_MODELO_EVENTO=claude-opus-5-5 no .env.
MODELO_EVENTO = os.environ.get("IA_MODELO_EVENTO", "claude-haiku-5-5")
# US$ por milhão de tokens (entrada, saída) — tabela de 06/10/2026; só para estimar o custo no painel
PRECOS = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0), "claude-haiku-5-5": (0.10, 0.50)}
_registrar = None  # main.py liga isto numa função que grava o uso no banco


def _fallback(modelo: str) -> dict:
    # fallback do servidor só existe para Opus/Sonnet/Fable (o Haiku 5.5 não tem)
    if modelo.startswith(("claude-opus", "claude-sonnet", "claude-fable")):
        return {"betas": ["server-side-fallback-2026-07-01"], "extra_body": {"fallbacks": "default"}}
    return {}


_FALLBACK = _fallback(MODELO)


def _uso(tarefa: str, modelo: str, resp) -> None:
    u = getattr(resp, "usage", None)
    if u is None or _registrar is None:
        return
    ent = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0) + (getattr(u, "cache_read_input_tokens", 0) or 0)
    sai = u.output_tokens or 0
    pe, ps = PRECOS.get(modelo, (4.0, 20.0))
    try:
        _registrar(tarefa, modelo, ent, sai, ent / 1e6 * pe + sai / 1e6 * ps)
    except Exception as e:  # noqa: BLE001
        log.warning("não registrei o uso: %s", e)

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic | None:
    global _client
    if _client is None and os.environ.get("ANTHROPIC_API_KEY"):
        _client = anthropic.AsyncAnthropic(timeout=45.0, max_retries=2)
    return _client


SISTEMA_EVENTO = (
    "Você analisa imagens de câmeras de segurança de uma residência no Brasil e escreve avisos curtos "
    "para o morador. Descreva em uma frase, em português do Brasil, o que está acontecendo: quantas "
    "pessoas, roupas e aparência geral, o que estão fazendo, veículos, objetos carregados e onde estão "
    "na cena. Em pessoas, conte só seres humanos de verdade visíveis em alguma das imagens (0 se for "
    "objeto, sombra, animal, boneco ou reflexo). Não tente dizer quem é a pessoa nem adivinhar nomes; isso é feito por outro sistema. "
    "Marque suspeito=true só para sinais concretos: tentativa de abrir portão/porta/carro, pular muro, "
    "rosto coberto, ferramenta de arrombamento, alguém olhando para dentro repetidamente. "
    "Pessoas passando, entregadores e visitas comuns não são suspeitos."
)

_SCHEMA_EVENTO = {
    "type": "object",
    "properties": {
        "descricao": {"type": "string"},
        "suspeito": {"type": "boolean"},
        "pessoas": {"type": "integer"},
        "motivo": {"type": "string"},
    },
    "required": ["descricao", "pessoas", "suspeito", "motivo"],
    "additionalProperties": False,
}


def _texto(resp) -> str:
    return next((b.text for b in resp.content if b.type == "text"), "")


async def descrever_evento(camera: str, fotos: list[bytes], contexto: str) -> dict:
    """fotos: JPEGs (1–3). contexto: o que o reconhecimento facial já concluiu."""
    c = client()
    if c is None:
        return {"descricao": "(IA desligada: sem ANTHROPIC_API_KEY)", "pessoas": -1, "suspeito": False, "motivo": ""}
    conteudo = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                             "data": base64.standard_b64encode(f).decode()}} for f in fotos[:2]]
    conteudo.append({"type": "text", "text": f"Câmera: {camera}. {contexto}\nDescreva a cena."})
    try:
        resp = await c.beta.messages.create(
            model=MODELO_EVENTO, max_tokens=2000, system=SISTEMA_EVENTO,
            messages=[{"role": "user", "content": conteudo}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SCHEMA_EVENTO}},
            **_fallback(MODELO_EVENTO),
        )
        _uso("evento", MODELO_EVENTO, resp)
        if resp.stop_reason == "refusal":
            log.warning("descrição recusada: %s", getattr(resp, "stop_details", None))
            return {"descricao": "(a IA não descreveu esta imagem)", "pessoas": -1, "suspeito": False, "motivo": ""}
        return json.loads(_texto(resp))
    except anthropic.RateLimitError:
        log.warning("limite de requisições da API")
    except anthropic.APIStatusError as e:
        log.warning("erro da API %s: %s", e.status_code, e.message)
    except anthropic.APIConnectionError:
        log.warning("sem conexão com a API")
    except (json.JSONDecodeError, StopIteration):
        log.warning("resposta fora do formato")
    return {"descricao": "(descrição indisponível)", "pessoas": -1, "suspeito": False, "motivo": ""}


SISTEMA_ROBO = (
    "Você configura as rotinas de limpeza do robô aspirador Robson (Xiaomi C101, com base que lava o mop) a "
    "pedido do morador, em português do Brasil. Você recebe os cômodos do mapa (id e nome), as rotinas atuais e "
    "o pedido. Devolva só as operações necessárias para cumprir o pedido:\n"
    "- criar: rotina nova (id 0).\n- alterar: muda uma rotina existente (use o id dela e envie a rotina "
    "completa já com as mudanças).\n- apagar: remove (id da rotina; em rotina repita os dados atuais).\n"
    "- executar_agora: limpa já, sem agendar (id 0; dias e hora são ignorados).\n"
    "Campos: dias = lista de 0 a 6 (0 = segunda ... 6 = domingo); hora = HH:MM (24 h); comodos = ids do mapa "
    "(lista vazia = casa inteira); modo 0 aspirar, 1 aspirar e passar pano, 2 só pano, 3 aspirar e depois pano; "
    "succao 0 silenciosa, 1 padrão, 2 média, 3 turbo; agua 0 pouca, 1 média, 2 muita; repetir 0 uma passada, "
    "1 duas passadas; trajeto 0 em S, 1 em Y (mais caprichado). Use -1 em succao/agua/modo/repetir/trajeto "
    "para 'não mexer'.\n"
    "Boas práticas: pano em cômodo com tapete não é bom; cozinha e banheiro costumam pedir pano; quarto à noite, "
    "sucção silenciosa; turbo para pelos de pet. Se o pedido for ambíguo ou citar um cômodo que não existe, não "
    "invente: deixe operacoes vazia e pergunte em resposta. Em resposta, explique em 1 a 3 frases o que será feito."
)

_ROTINA_SCHEMA = {
    "type": "object",
    "properties": {
        "nome": {"type": "string"},
        "ativo": {"type": "boolean"},
        "dias": {"type": "array", "items": {"type": "integer"}},
        "hora": {"type": "string"},
        "comodos": {"type": "array", "items": {"type": "integer"}},
        "modo": {"type": "integer"}, "succao": {"type": "integer"}, "agua": {"type": "integer"},
        "repetir": {"type": "integer"}, "trajeto": {"type": "integer"},
    },
    "required": ["nome", "ativo", "dias", "hora", "comodos", "modo", "succao", "agua", "repetir", "trajeto"],
    "additionalProperties": False,
}
_SCHEMA_ROBO = {
    "type": "object",
    "properties": {
        "resposta": {"type": "string"},
        "operacoes": {"type": "array", "items": {
            "type": "object",
            "properties": {"tipo": {"type": "string", "enum": ["criar", "alterar", "apagar", "executar_agora"]},
                           "id": {"type": "integer"}, "rotina": _ROTINA_SCHEMA},
            "required": ["tipo", "id", "rotina"], "additionalProperties": False}},
    },
    "required": ["resposta", "operacoes"],
    "additionalProperties": False,
}


async def planejar_robo(texto: str, contexto: dict) -> dict:
    c = client()
    if c is None:
        return {"resposta": "IA desligada: falta ANTHROPIC_API_KEY no .env da VPS.", "operacoes": []}
    try:
        resp = await c.beta.messages.create(
            model=MODELO, max_tokens=4000, system=SISTEMA_ROBO,
            messages=[{"role": "user", "content": f"<contexto>\n{json.dumps(contexto, ensure_ascii=False)}\n"
                                                   f"</contexto>\n\nPedido: {texto}"}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": _SCHEMA_ROBO}},
            **_FALLBACK,
        )
        _uso("robo", MODELO, resp)
        if resp.stop_reason == "refusal":
            return {"resposta": "A IA não respondeu a este pedido.", "operacoes": []}
        out = json.loads(_texto(resp))
    except anthropic.APIStatusError as e:
        return {"resposta": f"Erro da API ({e.status_code}).", "operacoes": []}
    except anthropic.APIConnectionError:
        return {"resposta": "Sem conexão com a API da Claude.", "operacoes": []}
    except (json.JSONDecodeError, StopIteration):
        return {"resposta": "Resposta fora do formato; tente reformular.", "operacoes": []}
    for op in out.get("operacoes", []):  # -1 = "não mexer": vira ausência de ajuste
        r = op["rotina"]
        r["config"] = {k: r.pop(k) for k in ("modo", "succao", "agua", "repetir", "trajeto") if r.get(k, -1) >= 0}
        for k in ("modo", "succao", "agua", "repetir", "trajeto"):
            r.pop(k, None)
    return out


SISTEMA_HISTORICO = (
    "Você é o assistente de segurança da casa. Recebe a lista de eventos das câmeras (data, câmera, "
    "quem o reconhecimento facial identificou e a descrição da cena) e responde em português do "
    "Brasil, de forma direta. Use só os eventos fornecidos; se a resposta não estiver neles, diga isso."
)


async def sobre_historico(pergunta: str, eventos_txt: str, esforco: str = "medium") -> str:
    c = client()
    if c is None:
        return "IA desligada: falta ANTHROPIC_API_KEY no .env da VPS."
    try:
        resp = await c.beta.messages.create(
            model=MODELO, max_tokens=8000, system=SISTEMA_HISTORICO,
            messages=[{"role": "user", "content": f"<eventos>\n{eventos_txt}\n</eventos>\n\n{pergunta}"}],
            output_config={"effort": esforco},
            **_FALLBACK,
        )
        _uso("historico", MODELO, resp)
        if resp.stop_reason == "refusal":
            return "A IA não respondeu a esta pergunta."
        return _texto(resp).strip() or "(sem resposta)"
    except anthropic.APIStatusError as e:
        return f"Erro da API ({e.status_code})."
    except anthropic.APIConnectionError:
        return "Sem conexão com a API da Claude."
