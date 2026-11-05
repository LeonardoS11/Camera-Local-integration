# Vigia das casas sem PC (rancho): o mesmo agente do PC, só a parte das câmeras, lendo o RTSP do go2rtc da VPS.
# agente.py, miio.py e o YOLOX são copiados do projeto pelo publicar.sh (agent/, tools/, models/).
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir "numpy>=2,<3" "opencv-python-headless>=4.10"
WORKDIR /srv
COPY vigia/agente.py agent/agente.py
COPY app/visao.py agent/visao.py
COPY vigia/miio.py tools/miio.py
COPY vigia/models/object_detection_yolox_2022nov.onnx app/models/face_detection_yunet_2023mar.onnx app/models/face_recognition_sface_2021dec.onnx models/
CMD ["python", "agent/agente.py"]
