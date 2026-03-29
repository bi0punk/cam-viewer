# ─────────────────────────────────────────────────────────────
# Imagen base compartida para recorder y web
# ─────────────────────────────────────────────────────────────
FROM python:3.11-slim

LABEL maintainer="ip-camera-recorder"

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV TZ=America/Bogota
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar todos los archivos del proyecto
COPY recorder.py .
COPY test_camera.py .
COPY web/ ./web/

RUN mkdir -p /recordings /config /logs
