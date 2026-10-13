# Sleevenote in a container.
#
# The server is stdlib Python and needs nothing installed. Pillow is here only
# for imgtool.py, which reads photos of barcodes in a subprocess and is never
# imported by the server; app.py finds an interpreter that has it by probing,
# the same way it does on a bare machine.
#
# The collection is not in the image. It is /data, a volume, so the image can be
# rebuilt and thrown away without taking anybody's records with it.
FROM python:3.13-slim

RUN pip install --no-cache-dir "pillow>=11,<13" \
 && groupadd -g 10002 sleevenote \
 && useradd -u 10002 -g sleevenote -M -d /nonexistent -s /usr/sbin/nologin sleevenote \
 && mkdir -p /data && chown 10002:10002 /data

WORKDIR /app
COPY *.py ./
COPY static/ ./static/

# SN_BIND=local: inside the Tailscale sidecar's namespace, loopback is where
# `tailscale serve` connects from, and nothing else needs to reach it.
# SN_BEHIND_HTTPS=1: serve terminates TLS with the node's certificate, so the
# cookies are marked Secure and HSTS is sent.
ENV SN_DATA=/data \
    SN_PORT=6969 \
    SN_BIND=local \
    SN_BEHIND_HTTPS=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Never root, not even at start: the host data directory must be writable by
# uid 10002 before the container starts.
USER 10002:10002
VOLUME ["/data"]
EXPOSE 6969

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD ["python3", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:6969/healthz', timeout=4)"]

CMD ["python3", "app.py"]
