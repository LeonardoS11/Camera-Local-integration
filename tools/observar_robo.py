"""Observa propriedades do robô e registra cada mudança (para descobrir o que o app Mi Home grava ao
mandar limpar um cômodo). Uso: python tools/observar_robo.py [minutos] > saida.txt
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import miio  # noqa: E402

PROPS = [(2, 1), (2, 4), (2, 8), (2, 10), (7, 5), (7, 6), (7, 24), (7, 25), (7, 26), (7, 33), (7, 34), (10, 2)]
d = next(x for x in miio.load_devices() if x.get("model") == "xiaomi.vacuum.c101")
dev = miio.Device(d["localip"], d["token"], timeout=4)
fim = time.time() + 60 * float(sys.argv[1] if len(sys.argv) > 1 else 20)
antes = {}
while time.time() < fim:
    try:
        atual = {}
        for i in range(0, len(PROPS), 4):
            for r in dev.get_props(PROPS[i:i + 4]):
                atual[f"{r['siid']}.{r['piid']}"] = r.get("value")
        mud = {k: v for k, v in atual.items() if antes.get(k, "∅") != v}
        if mud:
            print(time.strftime("%H:%M:%S"), json.dumps(mud, ensure_ascii=False), flush=True)
        antes = atual
    except Exception as e:  # noqa: BLE001
        print(time.strftime("%H:%M:%S"), "erro:", e, flush=True)
        dev = miio.Device(d["localip"], d["token"], timeout=4)
    time.sleep(3)
print("fim", flush=True)
