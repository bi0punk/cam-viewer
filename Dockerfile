FROM python:3.11-slim

LABEL maintainer="ip-camera-recorder"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=America/Santiago

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    tzdata \
    procps \
    && rm -rf /var/lib/apt/lists/*

RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY recorder.py .
COPY validate_config.py .
COPY test_camera.py .
COPY web/ ./web/
RUN mkdir -p /recordings /config /logs /live_cache
