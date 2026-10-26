# Obraz pro Raspberry Pi (arm64) i x86_64 – Debian ma ffmpeg pro obe architektury.
#
# Build (primo na Pi):
#   podman build -t reproduktor .
#
# Spusteni:
#   podman run -d --name reproduktor --net=host \
#     -v /home/pi/sounds:/sounds:ro,Z \
#     -e MULTICAST_ADDR=239.1.1.1 -e MULTICAST_PORT=5000 \
#     reproduktor

FROM docker.io/library/debian:trixie-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --system --no-create-home --shell /usr/sbin/nologin reproduktor

WORKDIR /app
COPY reproduktor.py .
COPY sounds-specific/ ./sounds-specific/

ENV PYTHONUNBUFFERED=1 \
    SOUNDS_DIR=/sounds     SPECIFIC_SOUNDS_DIR=/app/sounds-specific \
    HTTP_PORT=8080

USER reproduktor
EXPOSE 8080

CMD ["python3", "/app/reproduktor.py"]
