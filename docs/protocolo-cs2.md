# Modo internet do CS2 (PPPP) para câmeras Xiaomi

O go2rtc só conectava câmeras Xiaomi na **mesma rede local**: mandava `LAN_SEARCH` para o IP da câmera, na porta
UDP 32108. O app Mi Home também conecta de fora de casa. O patch (`pkg/xiaomi/miss/cs2/internet.go`) faz o mesmo:
se a câmera não responde na rede local, tenta pelos servidores P2P da CS2 Network.

Os formatos abaixo vêm de duas fontes:
- os nomes de função da biblioteca cliente da CS2 que vai no app (`cs2p2p_PPPP_Proto_*`), que não vem ofuscada;
- testes reais.

Eles batem com o que já foi publicado nos PRs
[go2rtc#2264](https://github.com/AlexxIT/go2rtc/pull/2264) e [go2rtc#2631](https://github.com/AlexxIT/go2rtc/pull/2631)
e na [análise do protocolo PPPP de Wladimir Palant](https://palant.info/2025/11/05/an-overview-of-the-pppp-protocol-for-iot-cameras/).

## De onde vêm os dados

`/v2/device/miss_get_vendor`, o mesmo pedido que o go2rtc já faz, devolve para câmeras com `vendor = 4` (CS2):

- `p2p_id`: o DID da câmera, `PREFIXO-SERIE-CHECK` (ex.: `XMABCDE-123456-ABCDE`);
- `init_string`: a lista ofuscada dos servidores P2P;
- `license` e `crc_key`: usados **do lado da câmera** (login dela no servidor). O cliente não precisa deles.

## init_string → servidores

Considere só a parte antes de `:` (depois viria uma chave P2P; nas câmeras testadas não há). São pares de letras
`A`–`P`, um byte por par:

```
x = 0x39
para cada i:
    b = T[i % 54] ^ x ^ (16*c[2i] + c[2i+1] + 0xAF)      # c = bytes ASCII
    x ^= b
```

`T` é uma tabela fixa de 54 bytes, que está em `internet.go`. O resultado é `"ip1,ip2,ip3,"`. Os servidores escutam na
UDP **32100**.

## Pacotes

Cabeçalho: `F1 <tipo> <tamanho do corpo, u16 big endian>`.

- **DID** (20 bytes): prefixo ASCII completado com zeros até 8 bytes, depois a série em u32 big endian, depois o
  check code ASCII completado com zeros até 8 bytes.
- **sockaddr** (16 bytes): família `00 02`, porta em little endian, IPv4 com os bytes invertidos, 8 zeros.

| Tipo | Nome | Corpo |
|---|---|---|
| 0x00 | HELLO | vazio, para os servidores |
| 0x01 | HELLO_ACK | sockaddr: como o servidor vê você (IP/porta públicos) |
| 0x20 | P2P_REQ | DID + sockaddr local (36 bytes) |
| 0x21 | P2P_REQ_ACK | resultado: `00` ok, `FE` câmera offline, `FD` prefixo inválido, `FF` DID inválido |
| 0x40 | PUNCH_TO | sockaddr da câmera. Vem um com o endereço público e outro com o local |
| 0x41 | PUNCH_PKT | DID, mandado para os endereços do PUNCH_TO (porta ±3) |
| 0x42 | P2P_RDY | DID: a câmera respondeu e a sessão está aberta |

Depois do `P2P_RDY`, a sessão é exatamente a da rede local: DRW (0xD0/0xD1), login MISS etc.

O **vídeo vai direto entre a câmera e o cliente**. Os servidores só apresentam os dois.

**Relay** (0x67–0x84, quando nem o NAT de um lado nem o do outro deixam passar): ainda não implementado.

## No patch

A URL `xiaomi://` aceita `p2p=`:

| Valor | Comportamento |
|---|---|
| vazio | rede local; se falhar, internet |
| `lan` | só rede local |
| `internet` | só pelos servidores |

O `getMissURL` passa `p2p_id` e `init_string` na URL automaticamente.

## Resultados (câmeras chuangmi.camera.042a02 / EC3 Pro)

- **De outra cidade, com a câmera atrás do NAT do modem:**
  - `P2P_RDY` em ~0,7 s;
  - vídeo a 0,65 Mbit/s contínuos.
- **Câmera na mesma casa que o celular, mas em outra sub-rede** (o modem entregou o IP no lugar do roteador mesh):
  voltou a gravar pelo caminho da internet.
