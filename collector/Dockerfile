FROM python:3.12-slim

# Unbuffered stdout: Python block-buffers when stdout isn't a TTY, which
# hides poll lines from `docker compose logs` (and later, from Loki).
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY collector/ .

EXPOSE 9100

CMD ["python", "main.py"]
