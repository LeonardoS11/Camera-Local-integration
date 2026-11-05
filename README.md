# Camera-Local-integration

Câmeras e robô aspirador Xiaomi (Mi Home) **sem depender do app nem da nuvem de vídeo da Xiaomi**: o vídeo vai da
câmera para um servidor seu, que grava, detecta pessoas, reconhece rostos dos moradores e manda avisos no celular.
A nuvem Xiaomi só é usada para o login da conta e para pegar as chaves de sessão de cada aparelho (o mesmo que o
app faz); o vídeo nunca passa por ela.

> Projeto pessoal, sem relação com a Xiaomi. Use só com os seus aparelhos e a sua conta.

## Como funciona

```
 Casa com PC                                    Casa sem PC (sítio, praia...)
 ┌───────────────────────────┐                  ┌──────────────────────────────┐
 │ câmeras ──P2P──> go2rtc    │                  │ câmeras ──P2P──> go2rtc       │
 │ (LAN)    (patch próprio)   │                  │ (LAN ou internet) no celular  │
 │            │               │                  │            │  Android/Termux  │
 │      agent/agente.py       │                  │      ponte/ponte.py           │
 │  detecção, robô, presença  │                  │  envio, cartão SD, auto-update│
 └────────────┼──────────────┘                  └────────────┼─────────────────┘
              │ HTTPS (MPEG-TS + eventos)                      │ HTTPS
              ▼                                                ▼
 ┌────────────────────────────────────────────────────────────────────────────┐
 │ VPS (docker compose): Caddy · app FastAPI (painel, usuários, avisos, IA)     │
 │ go2rtc (ao vivo) · gravador (ffmpeg) · vigia (detecção das casas sem PC)    │
 │ ntfy (avisos) · Web Push                                                     │
 └────────────────────────────────────────────────────────────────────────────┘
```

- **Vídeo**: go2rtc com a fonte `xiaomi://` (protocolo MISS sobre P2P CS2/TUTK). O patch em
  `patches/go2rtc.patch` acrescenta:
  - giro (PTZ);
  - gravação do cartão SD (`PLAYBACK_REQ`);
  - **modo internet do CS2**: quando a câmera não está na rede local, ele vai pelos servidores P2P e faz hole
    punching, como o app (ver [docs/protocolo-cs2.md](docs/protocolo-cs2.md));
  - correção da sessão que caía com perda de pacotes;
  - DNS no Termux.
- **Detecção** (`agent/visao.py`, só OpenCV + NumPy):
  - YOLOX-S para pessoas, com filtro de movimento (MOG2) e "segunda olhada";
  - YuNet para rostos;
  - ArcFace r50 (ou SFace) para reconhecer;
  - YoutuReID para a aparência do corpo.
- **Avisos**:
  - só pessoa não identificada, rosto desconhecido ou atividade suspeita, analisada pela API da Claude;
  - morador reconhecido fica só no histórico;
  - saem pelo ntfy e pelo Web Push, com os botões "É Fulano" e "Silenciar".
- **Robô** (xiaomi.vacuum.c101): comandos e mapa pela LAN (miIO), rotinas e assistente em linguagem natural.
- **Ponte no celular** (`ponte/`): um Android velho com Termux leva o vídeo de uma casa sem PC.
  - Pareamento por código no painel.
  - Supervisor que religa a ponte.
  - Atualização automática com volta automática se a nova versão falhar.
  - Busca no cartão SD o que o ao vivo perdeu.
  - Recupera câmera travada.
  - Recebe comandos remotos do painel.

## Pastas

| Pasta | O quê |
|---|---|
| `agent/` | Agente do PC de casa. `agente.py`: câmeras → eventos, envio de vídeo, robô, presença, vigia do go2rtc. `visao.py`: modelos. `mapa.py`: mapa do robô |
| `tools/` | `mi_tokens.py`: login na nuvem Xiaomi e lista de aparelhos com tokens. `miio.py`: protocolo miIO local (sem dependências) |
| `ponte/` | `ponte.py` + `instalar.sh` do celular-ponte (Termux) |
| `vps/` | Stack da VPS: `docker-compose.yml`, `Caddyfile`, `app/` (FastAPI + painel web), `gravador/`, `vigia/`, `ntfy/`, `diagnostico/` (scripts de conferência e calibração) |
| `patches/` | Patch do go2rtc 1.9.14 |
| `docs/` | Specs MIoT dos aparelhos usados e notas do protocolo CS2 |

## Instalação (resumo)

### 1. Modelos (não vêm no repositório)

Coloque em `models/` (e o `publicar.sh` copia para a VPS):

| Arquivo | Origem | Licença |
|---|---|---|
| `object_detection_yolox_2022nov.onnx` | [OpenCV Zoo](https://github.com/opencv/opencv_zoo) | Apache-2.0 |
| `face_detection_yunet_2023mar.onnx` | OpenCV Zoo | MIT |
| `face_recognition_sface_2021dec.onnx` | OpenCV Zoo | Apache-2.0 |
| `person_reid_youtu_2021nov.onnx` | OpenCV Zoo | Apache-2.0 |
| `w600k_r50.onnx` (opcional, recomendado) | InsightFace `buffalo_l` | **só uso não comercial** |

Sem o ArcFace, o reconhecimento usa o SFace, que é bem pior com rosto pequeno de câmera.

### 2. go2rtc com o patch

Use Go 1.24 ou mais novo (é o que pede o `go.mod` do go2rtc 1.9.14).

```bash
git clone --branch v1.9.14 https://github.com/AlexxIT/go2rtc
cd go2rtc && git apply ../patches/go2rtc.patch
CGO_ENABLED=0 go build -trimpath -ldflags "-s -w"                           # PC
CGO_ENABLED=0 GOOS=linux GOARCH=arm64 go build -trimpath -ldflags "-s -w"   # celular arm64
CGO_ENABLED=0 GOOS=linux GOARCH=arm GOARM=7 go build -trimpath -ldflags "-s -w"  # celular armv7
```

Entre na conta Xiaomi pela interface do go2rtc (aba de fontes `xiaomi`). Ele grava a credencial na seção `xiaomi:`
do `go2rtc.yaml`, então **nunca publique esse arquivo**.

### 3. PC de casa

```bash
python -m venv agent/.venv
agent/.venv/Scripts/pip install numpy opencv-python pycryptodome vacuum-map-parser-ijai  # os 2 últimos só para o mapa do robô
python tools/mi_tokens.py        # login interativo -> secrets/devices.json e secrets/session.json
```

Crie `secrets/agente.json`:

```json
{"vps": "https://seu-dominio.exemplo.com", "token": "<AGENTE_TOKEN do .env da VPS>",
 "cameras": {"garagem": "rtsp://127.0.0.1:8554/garagem"}, "rede": "192.168.0"}
```

Opções:
- `aparelhos` e `enviar_video` (os dois `true` por padrão);
- `reiniciar_cameras`: reinicia uma câmera travada há 30 min, se o modelo tiver a ação;
- `threads` e `segunda_olhada_min`.

### 4. VPS

1. `cp vps/.env.example vps/.env`.
2. Troque `seu-dominio.exemplo.com` pelo seu domínio em `vps/Caddyfile`, `vps/ntfy/server.yml` e nos scripts.
3. Ajuste `vps/app/casas.json`, que serve só de semente: depois disso vale o que você salva em Admin > Casas.
4. Rode `vps/configurar.sh` na VPS: ele pede senha do painel, usuário do ntfy e chave da API da Claude.
5. Publique com `sh vps/publicar.sh`, que envia tudo por `ssh` para o host `xiaomihome` (em `/opt/xiaomihome`). Ajuste o host no script.

### 5. Celular-ponte (casa sem PC)

Em Admin > Pontes > "Configurar um celular", abra a página **no próprio celular**. Ela mostra:
1. o link do Termux;
2. um comando pronto, com o código de pareamento;
3. o botão de login Xiaomi.

Instale também o Termux:Boot e libere o Termux da economia de bateria do Android.

## Segurança

- O painel tem usuários, perfis, casas, sessão por cookie e bloqueio por tentativas.
- O go2rtc da VPS fica atrás do login (Caddy `forward_auth`).
- O token de cada ponte só vale para as câmeras da casa dela.
- Credenciais ficam fora do repositório:
  - `secrets/`;
  - `vps/.env`;
  - a seção `xiaomi:` do `go2rtc.yaml`.

  O `.gitignore` já cobre tudo isso.
- Os botões dos avisos do ntfy usam um token fixo (`ACAO_TOKEN`). Quem assina o tópico consegue usá-lo. Trocar
  por URL assinada está na lista de pendências.

## Licenças de terceiros

- go2rtc: MIT (o patch é derivado dele).
- Modelos: ver a tabela acima.
- As specs em `docs/spec-*.json` são as públicas do [miot-spec.org](https://miot-spec.org).
