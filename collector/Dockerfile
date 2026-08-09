FROM python:3.12-slim

# Unbuffered stdout: Python block-buffers when stdout isn't a TTY, which
# hides poll lines from `docker compose logs` (and later, from Loki).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Non-root: the collector never writes anywhere in the image (fleet.yaml is
# a read-only mount, /etc/argus is never modified), so it doesn't need root.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin argus

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ .

EXPOSE 9100

USER argus

CMD ["python", "main.py"]
