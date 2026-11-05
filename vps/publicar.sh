#!/bin/sh
# Publica a VPS a partir do PC (Git Bash):  sh vps/publicar.sh
# Copia as peças que a VPS compartilha com o PC (visão, agente, miio, YOLOX, ponte do celular) e envia tudo
# para /opt/xiaomihome. Os modelos (~350 MB) só vão quando ainda não estão lá. O go2rtc.yaml da VPS é do app
# (Admin > Casas e câmeras): o daqui só serve de semente e não é reenviado.
set -e
cd "$(dirname "$0")/.."
cp agent/visao.py vps/app/visao.py
cp agent/agente.py vps/vigia/agente.py
cp tools/miio.py vps/vigia/miio.py
mkdir -p vps/vigia/models vps/ponte
cp ponte/ponte.py ponte/instalar.sh ponte/bin/go2rtc_arm64 ponte/bin/go2rtc_armv7 vps/ponte/
cp ponte/instalar.sh vps/ponte/i  # atalho curto para digitar: curl -L seu-dominio.exemplo.com/ponte/i | sh
(cd vps/ponte && sha256sum go2rtc_arm64 go2rtc_armv7 ponte.py instalar.sh | sed 's/ \*/  /' > SHA256SUMS)

faltam=$(ssh xiaomihome 'cd /opt/xiaomihome; for f in app/models/w600k_r50.onnx vigia/models/object_detection_yolox_2022nov.onnx; do [ -f "$f" ] || echo "$f"; done')
cp -u models/object_detection_yolox_2022nov.onnx vps/vigia/models/
for f in $faltam; do
  echo "enviando modelo $f"
  ssh xiaomihome "mkdir -p /opt/xiaomihome/$(dirname "$f")"
  scp "vps/$f" "xiaomihome:/opt/xiaomihome/$f"
done
tar --exclude=__pycache__ --exclude=.env --exclude='*/models/*' --exclude=./go2rtc/go2rtc.yaml -czf - -C vps . |
  ssh xiaomihome "tar -xzf - -C /opt/xiaomihome && cd /opt/xiaomihome && docker compose up -d --build"
