FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1
COPY gravador/gravador.py /gravador.py
CMD ["python", "/gravador.py"]
